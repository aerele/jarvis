"""Tests for Slice B auto-tell of Data Import completion (jarvis.chat.import_announce).

T1 here: the confirm-time binding (``bind_after_run_import``) + the conversation cascade.
Reuses the ephemeral parent+child + CSV fixtures from test_import_preview.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis.chat import import_announce
from jarvis.chat.import_announce import announce_finished_imports
from jarvis.tests.test_import_preview import (
	_FLAT_CSV,
	PARENT_DT,
	_ensure_doctypes,
	_make_csv,
)

ANN = "Jarvis Import Announcement"


class TestImportAnnounceBinding(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_doctypes()
		cls.flat_url = _make_csv(_FLAT_CSV, "jarvis_ann_flat.csv")
		# A real Data Import to be the Link target - staged only, not run.
		di = frappe.new_doc("Data Import")
		di.reference_doctype = PARENT_DT
		di.import_type = "Insert New Records"
		di.import_file = cls.flat_url
		di.mute_emails = 1
		di.insert(ignore_permissions=True)
		cls.di_name = di.name
		# A conversation to bind to (autoname hash, no reqd fields).
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "slice-b ann"}).insert(
			ignore_permissions=True
		)
		cls.conv = conv.name
		cls.owner = frappe.session.user
		# The DI + conversation must outlive the per-test savepoint rollbacks.
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(ANN, {"data_import": cls.di_name})
		frappe.db.delete("Data Import", {"name": cls.di_name})
		if frappe.db.exists("Jarvis Conversation", cls.conv):
			frappe.delete_doc("Jarvis Conversation", cls.conv, force=True, ignore_permissions=True)
		existing = frappe.db.exists("File", {"file_name": "jarvis_ann_flat.csv"})
		if existing:
			frappe.delete_doc("File", existing, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		# Announcement rows created by bind() are not reliably undone by the per-test
		# rollback in this suite - clean explicitly so each test starts independent
		# (mirrors the Slice A run_import test discipline).
		frappe.db.delete(ANN)
		frappe.db.commit()

	# --- helpers ------------------------------------------------------------

	def _record(self, **over):
		r = {"tool": "run_import", "conversation": self.conv, "owner": self.owner, "args": {}}
		r.update(over)
		return r

	def _ok(self, di=None):
		return {"ok": True, "data": {"data_import": di or self.di_name, "status": "queued"}}

	# --- T1: bind_after_run_import ------------------------------------------

	def test_bind_creates_one_announcement(self):
		import_announce.bind_after_run_import(self._record(), self._ok())
		rows = frappe.get_all(
			ANN,
			filters={"data_import": self.di_name},
			fields=["name", "conversation", "owner", "announced", "kicked_off_at"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].conversation, self.conv)
		self.assertEqual(rows[0].owner, self.owner)
		self.assertEqual(rows[0].announced, 0)
		self.assertTrue(rows[0].kicked_off_at)

	def test_bind_skips_non_run_import(self):
		import_announce.bind_after_run_import(self._record(tool="delete_doc"), self._ok())
		self.assertFalse(frappe.db.exists(ANN, {"data_import": self.di_name}))

	def test_bind_skips_failed_result(self):
		import_announce.bind_after_run_import(self._record(), {"ok": False, "error": {"code": "X"}})
		self.assertFalse(frappe.db.exists(ANN, {"data_import": self.di_name}))

	def test_bind_skips_missing_conversation(self):
		# A conversation-less token: no announcement (accepted O-1 residual), no crash.
		import_announce.bind_after_run_import(self._record(conversation=None), self._ok())
		self.assertFalse(frappe.db.exists(ANN, {"data_import": self.di_name}))

	def test_bind_is_idempotent(self):
		import_announce.bind_after_run_import(self._record(), self._ok())
		import_announce.bind_after_run_import(self._record(), self._ok())
		rows = frappe.get_all(ANN, filters={"data_import": self.di_name})
		self.assertEqual(len(rows), 1)

	def test_bind_never_raises_on_bad_data_import(self):
		# A Link to a non-existent Data Import fails validation inside insert(); the helper
		# must swallow + log_error, never propagate into the confirm path (PC-3).
		import_announce.bind_after_run_import(self._record(), self._ok(di="No Such Data Import xyz"))
		self.assertFalse(frappe.db.exists(ANN, {"data_import": "No Such Data Import xyz"}))

	def test_bind_uses_the_token_conversation_only(self):
		# The helper reads record["conversation"] (the guarded token conv) - never a
		# client-supplied value (PC-4). Binding is always to that conversation.
		import_announce.bind_after_run_import(self._record(), self._ok())
		row = frappe.get_all(ANN, filters={"data_import": self.di_name}, fields=["conversation"])
		self.assertEqual(row[0].conversation, self.conv)

	# --- T5: conversation cascade -------------------------------------------

	def test_cascade_deletes_rows_on_conversation_trash(self):
		# A conversation with a live announcement can be deleted (no LinkExistsError) and
		# leaves no orphan announced=0 row for the poll.
		throwaway = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "cascade"}).insert(
			ignore_permissions=True
		)
		import_announce.bind_after_run_import(
			{"tool": "run_import", "conversation": throwaway.name, "owner": self.owner, "args": {}},
			self._ok(),
		)
		self.assertTrue(frappe.db.exists(ANN, {"conversation": throwaway.name}))
		frappe.delete_doc("Jarvis Conversation", throwaway.name, force=True, ignore_permissions=True)
		self.assertFalse(frappe.db.exists(ANN, {"conversation": throwaway.name}))


class TestImportAnnounceClassifier(FrappeTestCase):
	"""The classifier + message-post (T2/T3): job-done trigger, completeness, the
	status branches, the stale guard (grace + two-tick), poison/owner/archived
	handling, exactly-once, the O-2 ceiling, AJ-3 (works as a non-admin invoker),
	and the kill-switch."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_doctypes()
		cls.flat_url = _make_csv(_FLAT_CSV, "jarvis_ann_cls.csv")
		di = frappe.new_doc("Data Import")
		di.reference_doctype = PARENT_DT
		di.import_type = "Insert New Records"
		di.import_file = cls.flat_url
		di.mute_emails = 1
		di.insert(ignore_permissions=True)
		cls.di_name = di.name
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "slice-b cls"}).insert(
			ignore_permissions=True
		)
		cls.conv = conv.name
		cls.owner = frappe.session.user
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cls._reap()
		frappe.db.delete("Data Import", {"name": cls.di_name})
		if frappe.db.exists("Jarvis Conversation", cls.conv):
			frappe.delete_doc("Jarvis Conversation", cls.conv, force=True, ignore_permissions=True)
		existing = frappe.db.exists("File", {"file_name": "jarvis_ann_cls.csv"})
		if existing:
			frappe.delete_doc("File", existing, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _reap(cls):
		frappe.db.delete(ANN)
		frappe.db.delete("Jarvis Chat Message", {"conversation": cls.conv})
		frappe.db.delete("Data Import Log", {"data_import": cls.di_name})
		frappe.db.set_value(
			"Data Import",
			cls.di_name,
			{"status": "Pending", "payload_count": 0, "template_warnings": None},
			update_modified=False,
		)
		frappe.db.commit()

	def tearDown(self):
		self._reap()
		frappe.set_user("Administrator")

	# --- helpers ------------------------------------------------------------

	def _ann(self, *, di=None, conv=None, kicked_off_min_ago=0, reason=""):
		doc = frappe.new_doc(ANN)
		doc.data_import = di or self.di_name
		doc.conversation = conv or self.conv
		doc.owner = self.owner
		doc.kicked_off_at = add_to_date(now_datetime(), minutes=-kicked_off_min_ago)
		doc.announced = 0
		doc.reason = reason
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def _set_di(self, status, *, success=0, failed=0, payload=None, warnings=None):
		frappe.db.set_value(
			"Data Import",
			self.di_name,
			{
				"status": status,
				"payload_count": payload if payload is not None else (success + failed),
				"template_warnings": warnings,
			},
			update_modified=False,
		)
		for i in range(success):
			frappe.get_doc(
				{"doctype": "Data Import Log", "data_import": self.di_name, "success": 1, "log_index": i}
			).insert(ignore_permissions=True)
		for i in range(failed):
			frappe.get_doc(
				{
					"doctype": "Data Import Log",
					"data_import": self.di_name,
					"success": 0,
					"log_index": 1000 + i,
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	def _run(self, *, enqueued=False, source="poll", di=None):
		with patch("jarvis.chat.import_announce._is_job_enqueued", return_value=enqueued):
			return announce_finished_imports(data_import=di or self.di_name, source=source)

	def _messages(self, di=None):
		return frappe.get_all(
			"Jarvis Chat Message",
			filters={"conversation": self.conv, "role": "assistant", "ref_name": di or self.di_name},
			fields=["name", "content", "hidden"],
		)

	def _row(self, di=None):
		return frappe.db.get_value(
			ANN, {"data_import": di or self.di_name}, ["announced", "reason", "announced_via"], as_dict=True
		)

	# --- job-done trigger + terminal branches -------------------------------

	def test_done_posts_one_records_message(self):
		self._ann()
		self._set_di("Success", success=2, payload=2)
		self._run(enqueued=False)
		msgs = self._messages()
		self.assertEqual(len(msgs), 1)
		self.assertIn("2", msgs[0].content)
		self.assertEqual(msgs[0].hidden, 0)
		row = self._row()
		self.assertEqual(row.announced, 1)
		self.assertEqual(row.reason, "done")
		self.assertEqual(row.announced_via, "poll")

	def test_still_enqueued_posts_nothing(self):
		# Poll lands mid-import ("Partial Success" written after row 1, job still enqueued).
		self._ann()
		self._set_di("Partial Success", success=1, payload=2)
		self._run(enqueued=True)
		self.assertEqual(len(self._messages()), 0)
		self.assertEqual(self._row().announced, 0)

	def test_partial_covered_is_partial(self):
		self._ann()
		self._set_di("Partial Success", success=1, failed=1, payload=2)
		self._run(enqueued=False)
		msgs = self._messages()
		self.assertEqual(len(msgs), 1)
		self.assertIn("failed", msgs[0].content.lower())
		self.assertEqual(self._row().reason, "partial")

	def test_partial_not_covered_is_interrupted(self):
		# Terminal Partial Success but success+failed < payload_count -> hard-kill (PC-2).
		self._ann()
		self._set_di("Partial Success", success=1, failed=0, payload=100)
		self._run(enqueued=False)
		msgs = self._messages()
		self.assertEqual(len(msgs), 1)
		self.assertIn("didn't complete", msgs[0].content)
		self.assertEqual(self._row().reason, "interrupted")

	def test_all_failed_zero_success_log_no_keyerror(self):
		self._ann()
		self._set_di("Error", success=0, failed=3, payload=3)
		self._run(enqueued=False)
		msgs = self._messages()
		self.assertEqual(len(msgs), 1)
		self.assertIn("failed", msgs[0].content.lower())
		self.assertEqual(self._row().reason, "error")

	def test_blocked_posts_blocked(self):
		self._ann()
		self._set_di("Pending", payload=0, warnings='[{"type":"invalid_value"}]')
		self._run(enqueued=False)
		msgs = self._messages()
		self.assertEqual(len(msgs), 1)
		self.assertIn("blocked", msgs[0].content.lower())
		self.assertEqual(self._row().reason, "blocked")

	# --- stale guard: grace + two-tick --------------------------------------

	def test_within_grace_no_post(self):
		self._ann(kicked_off_min_ago=1)  # inside the 5-min grace
		self._set_di("Pending", payload=2)  # non-terminal, not enqueued
		self._run(enqueued=False)
		self.assertEqual(len(self._messages()), 0)
		self.assertEqual(self._row().announced, 0)

	def test_stuck_requires_two_ticks(self):
		self._ann(kicked_off_min_ago=10)  # past grace
		self._set_di("Pending", payload=2)
		self._run(enqueued=False)  # first tick: mark provisional, no post
		self.assertEqual(len(self._messages()), 0)
		self.assertEqual(self._row().reason, "stuck_seen")
		self.assertEqual(self._row().announced, 0)
		self._run(enqueued=False)  # second tick: confirmed -> post
		self.assertEqual(len(self._messages()), 1)
		self.assertEqual(self._row().reason, "interrupted")

	def test_enqueue_blip_clears_stuck_marker(self):
		self._ann(kicked_off_min_ago=10)
		self._set_di("Pending", payload=2)
		self._run(enqueued=False)  # marks stuck_seen
		self.assertEqual(self._row().reason, "stuck_seen")
		self._run(enqueued=True)  # blip resolved: job enqueued again -> clear marker, no post
		self.assertEqual(self._row().reason, "")
		self.assertEqual(len(self._messages()), 0)

	# --- exactly-once + crash window ----------------------------------------

	def test_exactly_once_across_two_ticks(self):
		self._ann()
		self._set_di("Success", success=2, payload=2)
		self._run(enqueued=False)
		self._run(enqueued=False)
		self.assertEqual(len(self._messages()), 1)

	def test_crash_window_no_double_post(self):
		# Message committed but announced flag not (simulate: pre-insert the message, leave
		# the row announced=0). Next tick must dedupe (no 2nd message) AND flip announced=1.
		self._ann()
		self._set_di("Success", success=2, payload=2)
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": self.conv,
				"seq": 1,
				"role": "assistant",
				"content": "pre-existing",
				"hidden": 0,
				"ref_doctype": "Data Import",
				"ref_name": self.di_name,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self._run(enqueued=False)
		self.assertEqual(len(self._messages()), 1)  # no double-post
		self.assertEqual(self._row().announced, 1)  # recovered to terminal

	# --- owner / poison / archived / ceiling --------------------------------

	def test_poison_row_marks_terminal_no_post(self):
		ghost = frappe.new_doc("Data Import")
		ghost.reference_doctype = PARENT_DT
		ghost.import_type = "Insert New Records"
		ghost.import_file = self.flat_url
		ghost.mute_emails = 1
		ghost.insert(ignore_permissions=True)
		gname = ghost.name
		self._ann(di=gname)
		frappe.db.commit()
		frappe.db.delete("Data Import", {"name": gname})  # raw delete -> dangling Link
		frappe.db.commit()
		self._run(enqueued=False, di=gname)
		self.assertEqual(len(self._messages(di=gname)), 0)
		self.assertEqual(self._row(di=gname).reason, "data_import_deleted")
		self.assertEqual(self._row(di=gname).announced, 1)

	def test_missing_conversation_aborts_no_admin_post(self):
		throw = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "gone"}).insert(
			ignore_permissions=True
		)
		self._ann(conv=throw.name)
		# RAW delete the conversation (bypass the cascade) so the row dangles, as a
		# poll-vs-delete race would leave it.
		frappe.db.delete("Jarvis Conversation", {"name": throw.name})
		frappe.db.commit()
		self._set_di("Success", success=2, payload=2)
		self._run(enqueued=False)
		self.assertEqual(frappe.db.count("Jarvis Chat Message", {"conversation": throw.name}), 0)
		self.assertEqual(self._row().reason, "no_conversation")
		self.assertEqual(self._row().announced, 1)

	def test_archived_conversation_skips_and_marks(self):
		frappe.db.set_value("Jarvis Conversation", self.conv, "status", "Archived", update_modified=False)
		frappe.db.commit()
		try:
			self._ann()
			self._set_di("Success", success=2, payload=2)
			self._run(enqueued=False)
			self.assertEqual(len(self._messages()), 0)
			self.assertEqual(self._row().reason, "archived")
			self.assertEqual(self._row().announced, 1)
		finally:
			frappe.db.set_value("Jarvis Conversation", self.conv, "status", "Active", update_modified=False)
			frappe.db.commit()

	def test_ceiling_announces_status_unknown(self):
		self._ann(kicked_off_min_ago=800)  # past the 720-min O-2 ceiling
		self._set_di("Partial Success", success=1, payload=2)
		self._run(enqueued=True)  # is_job_enqueued STILL true (worker died, hash unreaped)
		msgs = self._messages()
		self.assertEqual(len(msgs), 1)
		self.assertIn("status unknown", msgs[0].content.lower())
		self.assertEqual(self._row().reason, "status_unknown")

	# --- AJ-3: works as a non-admin invoker (the fast-path runs as the import user) ---

	def test_classify_works_as_non_admin_invoker(self):
		self._ann()
		self._set_di("Success", success=2, payload=2)
		user = "slice_b_importer@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{"doctype": "User", "email": user, "first_name": "Importer", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		frappe.set_user(user)  # simulate the after_job-enqueued classify running as the import user
		self._run(enqueued=False, source="hook")
		frappe.set_user("Administrator")
		msgs = self._messages()
		self.assertEqual(len(msgs), 1)  # posted despite the invoker having no read grant
		self.assertEqual(self._row().announced_via, "hook")

	# --- kill-switch --------------------------------------------------------

	def test_kill_switch_skips(self):
		self._ann()
		self._set_di("Success", success=2, payload=2)
		frappe.conf.jarvis_import_announce_disabled = 1
		try:
			out = announce_finished_imports(data_import=self.di_name)
		finally:
			del frappe.conf["jarvis_import_announce_disabled"]
		self.assertEqual(out, {"skipped": "disabled"})
		self.assertEqual(len(self._messages()), 0)
		self.assertEqual(self._row().announced, 0)

	# --- T6: diagnostics attribution (hook vs poll) -------------------------

	def test_stats_reports_hook_attribution(self):
		from jarvis.diagnostics import import_announce_stats

		self._ann()
		self._set_di("Success", success=2, payload=2)
		self._run(enqueued=False, source="hook")
		stats = import_announce_stats()
		self.assertEqual(stats["24h"]["via_hook"], 1)
		self.assertEqual(stats["hook_rate_24h"], 1.0)
		self.assertIn("done", stats["by_reason_24h"])
		self.assertEqual(stats["pending"], 0)

	# --- review fix-wave regressions ---------------------------------------

	def test_tool_receipt_for_same_import_does_not_suppress(self):
		# CRITICAL regression: a role="tool" receipt (e.g. a get_doc of the Data Import
		# mid-run) carries ref_doctype="Data Import"/ref_name=di. The dedupe MUST be scoped
		# to role="assistant" so it does not false-match and silently drop the completion.
		self._ann()
		self._set_di("Success", success=2, payload=2)
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": self.conv,
				"seq": 1,
				"role": "tool",
				"content": "get_doc receipt",
				"hidden": 0,
				"ref_doctype": "Data Import",
				"ref_name": self.di_name,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self._run(enqueued=False)
		self.assertEqual(len(self._messages()), 1)  # completion still posted
		self.assertEqual(self._row().announced, 1)

	def test_error_status_with_zero_rows_is_not_done(self):
		# An Error import with no logged rows must NOT render as a green "done".
		self._ann()
		self._set_di("Error", success=0, failed=0, payload=0)
		self._run(enqueued=False)
		msgs = self._messages()
		self.assertEqual(len(msgs), 1)
		self.assertNotIn("✓", msgs[0].content)
		self.assertIn("✗", msgs[0].content)
		self.assertEqual(self._row().reason, "error")

	def test_partial_copy_places_counts_in_correct_slots(self):
		# Asymmetric counts so a success/failed swap would fail (was mutation-insensitive).
		self._ann()
		self._set_di("Partial Success", success=3, failed=1, payload=4)
		self._run(enqueued=False)
		self.assertIn("**3** imported, **1** failed", self._messages()[0].content)

	def test_untrusted_filename_link_injection_neutralized(self):
		frappe.db.set_value(
			"Data Import",
			self.di_name,
			"import_file",
			"/files/[Click here](https://evil.example/x).csv",
			update_modified=False,
		)
		self._ann()
		self._set_di("Success", success=2, payload=2)
		self._run(enqueued=False)
		content = self._messages()[0].content
		self.assertNotIn("](https://evil.example", content)  # no injected markdown link
		self.assertNotIn("[Click here]", content)

	def test_data_import_link_is_absolute(self):
		# A relative /app/... link renders as inert text; the link must be absolute http(s).
		self._ann()
		self._set_di("Error", success=0, failed=2, payload=2)
		self._run(enqueued=False)
		content = self._messages()[0].content
		self.assertIn("](http", content)  # absolute markdown link (a relative one renders inert)
		self.assertIn("/app/data-import/", content)

	def test_message_is_owned_by_the_conversation_owner(self):
		# impersonate(owner) is load-bearing: the posted row must be owned by the conversation
		# owner, not whatever user invoked the classify.
		user = "slice_b_importer2@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{"doctype": "User", "email": user, "first_name": "Imp2", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		self._ann()
		self._set_di("Success", success=2, payload=2)
		frappe.set_user(user)
		self._run(enqueued=False, source="hook")
		frappe.set_user("Administrator")
		owner = frappe.db.get_value("Jarvis Chat Message", self._messages()[0].name, "owner")
		self.assertEqual(owner, self.owner)  # the conversation owner, not the invoker

	def test_bounded_retry_quarantines_a_poison_row(self):
		# A row whose classify raises every tick is quarantined terminal after the cap, so it
		# can't re-log forever / sit un-announced below the deadman threshold.
		self._ann()
		with patch("jarvis.chat.import_announce._classify_and_announce", side_effect=RuntimeError("boom")):
			for _ in range(import_announce._MAX_ROW_ATTEMPTS):
				announce_finished_imports(data_import=self.di_name, source="poll")
		row = self._row()
		self.assertEqual(row.announced, 1)
		self.assertEqual(row.reason, "poison_error")

	def test_one_bad_row_does_not_block_the_batch(self):
		di2 = frappe.new_doc("Data Import")
		di2.reference_doctype = PARENT_DT
		di2.import_type = "Insert New Records"
		di2.import_file = self.flat_url
		di2.mute_emails = 1
		di2.insert(ignore_permissions=True)
		conv2 = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "iso2"}).insert(
			ignore_permissions=True
		)
		try:
			self._ann()  # good row (di_name)
			self._ann(di=di2.name, conv=conv2.name)  # bad row
			self._set_di("Success", success=2, payload=2)  # di_name -> done
			real = import_announce._read_tally

			def flaky(di):
				if di == di2.name:
					raise RuntimeError("boom")
				return real(di)

			with (
				patch("jarvis.chat.import_announce._read_tally", side_effect=flaky),
				patch("jarvis.chat.import_announce._is_job_enqueued", return_value=False),
			):
				counts = announce_finished_imports(source="poll")
			self.assertEqual(len(self._messages(di=self.di_name)), 1)  # good row posted
			self.assertGreaterEqual(counts.get("errored", 0), 1)
			bad = frappe.db.get_value(ANN, {"data_import": di2.name}, ["announced", "attempts"], as_dict=True)
			self.assertEqual(bad.announced, 0)  # bad row still live for re-poll
			self.assertEqual(bad.attempts, 1)  # bumped toward quarantine
		finally:
			frappe.db.delete(ANN, {"data_import": di2.name})
			frappe.db.delete("Data Import", {"name": di2.name})
			if frappe.db.exists("Jarvis Conversation", conv2.name):
				frappe.delete_doc("Jarvis Conversation", conv2.name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_deadman_alarms_on_backlog_and_dedupes(self):
		title = "import_announce: un-announced backlog"
		frappe.db.delete("Error Log", {"method": title})
		frappe.db.commit()
		try:
			self._ann(kicked_off_min_ago=60)  # old + stays announced=0 (enqueued below)
			self._set_di("Pending", payload=2)
			with (
				patch("jarvis.chat.import_announce._DEADMAN_BACKLOG", 1),
				patch("jarvis.chat.import_announce._is_job_enqueued", return_value=True),
			):
				announce_finished_imports(source="poll")
				n1 = frappe.db.count("Error Log", {"method": title})
				announce_finished_imports(source="poll")
				n2 = frappe.db.count("Error Log", {"method": title})
			self.assertEqual(n1, 1)
			self.assertEqual(n2, 1)  # deduped within the window
		finally:
			frappe.db.delete("Error Log", {"method": title})
			frappe.db.commit()

	def test_deadman_not_run_on_hook_path(self):
		title = "import_announce: un-announced backlog"
		frappe.db.delete("Error Log", {"method": title})
		frappe.db.commit()
		try:
			self._ann(kicked_off_min_ago=60)
			self._set_di("Pending", payload=2)
			with (
				patch("jarvis.chat.import_announce._DEADMAN_BACKLOG", 1),
				patch("jarvis.chat.import_announce._is_job_enqueued", return_value=True),
			):
				announce_finished_imports(data_import=self.di_name, source="hook")  # scoped hook path
			self.assertEqual(frappe.db.count("Error Log", {"method": title}), 0)
		finally:
			frappe.db.delete("Error Log", {"method": title})
			frappe.db.commit()


class TestImportAnnounceAfterJob(FrappeTestCase):
	"""The after_job fast path (T8): method-anchored identification (AJ-1), never-raise
	+ import-safe (AJ-2), no self-recursion, kill-switch, v16-shaped call, and the
	real-dispatch registration test (AJ-2's only true defense)."""

	_START = "frappe.core.doctype.data_import.data_import.start_import"

	def test_matching_job_enqueues_one_scoped_classify(self):
		with patch("frappe.enqueue") as enq:
			import_announce.on_after_job(method=self._START, kwargs={"data_import": "DI-1"}, result=None)
		enq.assert_called_once()
		args, kw = enq.call_args
		self.assertEqual(args[0], "jarvis.chat.import_announce.announce_finished_imports")
		self.assertEqual(kw["data_import"], "DI-1")
		self.assertEqual(kw["source"], "hook")
		self.assertEqual(kw["queue"], "short")
		self.assertEqual(kw["job_id"], "jarvis_announce||DI-1")
		self.assertTrue(kw["deduplicate"])

	def test_non_data_import_job_is_noop(self):
		with patch("frappe.enqueue") as enq:
			import_announce.on_after_job(method="some.other.module.fn", kwargs={"x": 1}, result=None)
		enq.assert_not_called()

	def test_no_self_recursion(self):
		# The enqueued classify's own after_job carries method=announce_finished_imports;
		# method-anchoring (not "kwargs has data_import") means it never re-enqueues.
		with patch("frappe.enqueue") as enq:
			import_announce.on_after_job(
				method="jarvis.chat.import_announce.announce_finished_imports",
				kwargs={"data_import": "DI-1"},
				result=None,
			)
		enq.assert_not_called()

	def test_missing_data_import_kwarg_is_noop(self):
		with patch("frappe.enqueue") as enq:
			import_announce.on_after_job(method=self._START, kwargs={}, result=None)
		enq.assert_not_called()

	def test_v16_shaped_call_without_result(self):
		# A v16 call site may pass fewer args; the defaulted signature must still work.
		with patch("frappe.enqueue") as enq:
			import_announce.on_after_job(method=self._START, kwargs={"data_import": "DI-2"})
		enq.assert_called_once()

	def test_never_raises_when_enqueue_throws(self):
		# Redis down / short queue error inside the job's finally must be swallowed, not
		# propagated (a raise would replace the host job's result bench-wide).
		with patch("frappe.enqueue", side_effect=RuntimeError("redis down")):
			import_announce.on_after_job(method=self._START, kwargs={"data_import": "DI-3"}, result=None)

	def test_kill_switch_blocks_enqueue(self):
		frappe.conf.jarvis_import_announce_disabled = 1
		try:
			with patch("frappe.enqueue") as enq:
				import_announce.on_after_job(method=self._START, kwargs={"data_import": "DI-4"}, result=None)
		finally:
			del frappe.conf["jarvis_import_announce_disabled"]
		enq.assert_not_called()

	def test_registered_and_dispatchable_via_real_hook(self):
		# AJ-2: the ONLY defense against a broken hooks.py path / module-top ImportError -
		# drive the REAL after_job registration + frappe.call, not a direct call.
		hooks = frappe.get_hooks("after_job")
		self.assertIn("jarvis.chat.import_announce.on_after_job", hooks)
		with patch("frappe.enqueue") as enq:
			frappe.call("jarvis.chat.import_announce.on_after_job", method="unrelated.job", kwargs={})
		enq.assert_not_called()


class TestImportAnnounceIntegration(FrappeTestCase):
	"""One real end-to-end pass: run_import actually imports (synchronous in test mode),
	then bind + the REAL classifier read the REAL Data Import Log and post the tally."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_doctypes()
		cls.flat_url = _make_csv(_FLAT_CSV, "jarvis_ann_e2e.csv")
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "slice-b e2e"}).insert(
			ignore_permissions=True
		)
		cls.conv = conv.name
		cls.owner = frappe.session.user
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if frappe.db.exists("Jarvis Conversation", cls.conv):
			frappe.delete_doc("Jarvis Conversation", cls.conv, force=True, ignore_permissions=True)
		existing = frappe.db.exists("File", {"file_name": "jarvis_ann_e2e.csv"})
		if existing:
			frappe.delete_doc("File", existing, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_end_to_end_real_import_posts_tally(self):
		from jarvis.tools.run_import import run_import

		out = run_import(doctype=PARENT_DT, file_url=self.flat_url)
		di = out["data_import"]
		try:
			import_announce.bind_after_run_import(
				{"tool": "run_import", "conversation": self.conv, "owner": self.owner, "args": {}},
				{"ok": True, "data": out},
			)
			with patch("jarvis.chat.import_announce._is_job_enqueued", return_value=False):
				announce_finished_imports(data_import=di, source="poll")
			msgs = frappe.get_all(
				"Jarvis Chat Message",
				filters={"conversation": self.conv, "role": "assistant", "ref_name": di},
				fields=["content"],
			)
			self.assertEqual(len(msgs), 1)
			self.assertIn("2", msgs[0].content)
		finally:
			for title in ("REC-1", "REC-2"):
				if frappe.db.exists(PARENT_DT, title):
					frappe.delete_doc(PARENT_DT, title, force=True, ignore_permissions=True)
			frappe.db.delete("Jarvis Import Announcement", {"data_import": di})
			frappe.db.delete("Jarvis Chat Message", {"conversation": self.conv})
			frappe.db.delete("Data Import Log", {"data_import": di})
			frappe.db.delete("Data Import", {"name": di})
			frappe.db.commit()
