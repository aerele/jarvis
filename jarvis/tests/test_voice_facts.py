"""Tests for the voice-note feature slice this app owns: the ``Jarvis Voice
Note`` doctype + ``jarvis.chat.voice_notes_api`` endpoints and the daily
``jarvis.learning.voice_facts`` sweep (extraction -> learned-pattern
candidates under one manual run, wiki routing seams, settings stamps).

``jarvis.chat.voice`` / ``jarvis.chat.wiki`` are built in parallel; the mock
helpers patch their attributes when the modules exist and fall back to stub
modules during the pre-integration window, so the suite is deterministic
either way (the sweep itself only ever lazy-imports them).
"""

from __future__ import annotations

import contextlib
import sys
import types
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from jarvis.chat import voice_notes_api
from jarvis.learning import voice_facts
from jarvis.permissions import JARVIS_USER_ROLE, ensure_jarvis_user_role

NOTE = "Jarvis Voice Note"
JLP = "Jarvis Learned Pattern"
RUN = "Jarvis Pattern Run"
CONV = "Jarvis Conversation"
SETTINGS = "Jarvis Settings"

USER_A = "voice-user-a@example.com"
USER_B = "voice-user-b@example.com"
USER_SM = "voice-user-sm@example.com"

RULE_STATEMENT = "Deliveries to Acme Traders always ship from the Mumbai warehouse."

_SETTINGS_FIELDS = ("voice_notes_last_processed_at", "voice_notes_last_process_status")


def _ensure_user(email: str, roles: tuple = ()) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				# Explicit: a role-less insert becomes a Website User, which
				# the voice endpoints' System-User gate rejects.
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	if user.user_type != "System User":
		# Repair a stale fixture user left behind by an earlier run.
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
		frappe.clear_cache(user=email)
		user = frappe.get_doc("User", email)
	# Jarvis access is a ROLE now (chat permission hardening, #284): the voice
	# endpoints gate on System Manager or Jarvis User. Every fixture user here is
	# meant to be able to REACH Jarvis — what varies is whether they're an admin —
	# so all of them get Jarvis User, and the System-Manager-only distinction that
	# these tests turn on is preserved by the roles argument below.
	ensure_jarvis_user_role()
	user.add_roles(JARVIS_USER_ROLE)
	if roles:
		user.add_roles(*roles)
	elif "System Manager" in frappe.get_roles(email):
		user.remove_roles("System Manager")
	frappe.clear_cache(user=email)
	frappe.db.commit()
	return email


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


@contextlib.contextmanager
def _enforcing_permissions():
	"""Make ``frappe.only_for`` actually enforce.

	``only_for`` returns early when ``frappe.flags.in_test`` is set, so a
	role-gate built on it is a no-op for the whole suite — asserting against it
	without clearing the flag tests nothing. Scoped to the single call under
	test; the flag is restored afterwards."""
	prev = frappe.flags.in_test
	frappe.flags.in_test = False
	try:
		yield
	finally:
		frappe.flags.in_test = prev


@contextlib.contextmanager
def _mock_voice(result):
	"""Patch ``jarvis.chat.voice.openrouter_complete``. ``result``: a str
	return value, or an Exception / list side_effect."""
	kwargs = {"side_effect": result} if isinstance(result, (Exception, list)) else {"return_value": result}
	try:
		import jarvis.chat.voice

		have_real = True
	except Exception:
		have_real = False
	if have_real:
		with mock.patch("jarvis.chat.voice.openrouter_complete", create=True, **kwargs) as m:
			yield m
	else:
		import jarvis.chat as chat_pkg

		stub = types.ModuleType("jarvis.chat.voice")
		m = mock.MagicMock(**kwargs)
		stub.openrouter_complete = m
		stub.stt_config = lambda: None
		with (
			mock.patch.dict(sys.modules, {"jarvis.chat.voice": stub}),
			mock.patch.object(chat_pkg, "voice", stub, create=True),
		):
			yield m


@contextlib.contextmanager
def _mock_wiki():
	"""Patch the two ``jarvis.chat.wiki`` seams the sweep calls. Yields a dict
	with ``apply`` (apply_extracted_page_updates) and ``ingest``
	(enqueue_ingest_note) mocks."""
	try:
		import jarvis.chat.wiki

		have_real = True
	except Exception:
		have_real = False
	if have_real:
		with (
			mock.patch("jarvis.chat.wiki.apply_extracted_page_updates", create=True) as apply_mock,
			mock.patch("jarvis.chat.wiki.enqueue_ingest_note", create=True) as ingest_mock,
		):
			yield frappe._dict(apply=apply_mock, ingest=ingest_mock)
	else:
		import jarvis.chat as chat_pkg

		stub = types.ModuleType("jarvis.chat.wiki")
		stub.apply_extracted_page_updates = mock.MagicMock()
		stub.enqueue_ingest_note = mock.MagicMock()
		with (
			mock.patch.dict(sys.modules, {"jarvis.chat.wiki": stub}),
			mock.patch.object(chat_pkg, "wiki", stub, create=True),
		):
			yield frappe._dict(apply=stub.apply_extracted_page_updates, ingest=stub.enqueue_ingest_note)


def _fact_json(*items) -> str:
	return frappe.as_json(list(items))


def _rule_item(statement=RULE_STATEMENT, domain="stock", names_party=True, kind="rule") -> dict:
	return {"statement": statement, "domain": domain, "names_party": names_party, "kind": kind}


class VoiceFactsTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_user(USER_A)
		_ensure_user(USER_B)
		_ensure_user(USER_SM, roles=("System Manager",))

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		singles = frappe.db.get_singles_dict(SETTINGS)
		self._settings_before = {f: singles.get(f) for f in _SETTINGS_FIELDS}

	def tearDown(self):
		frappe.set_user("Administrator")
		# The sweep commits mid-run, so FrappeTestCase's rollback cannot undo
		# everything: sweep our own rows explicitly.
		# Scratch-site doctype owned by this module: clear ALL rows so stale
		# fixtures from earlier (broken-owner) runs cannot leak into sweeps.
		frappe.db.delete(NOTE)
		frappe.db.delete(JLP, {"detector_id": voice_facts.DETECTOR_ID})
		frappe.db.delete(RUN, {"scan_mode": "voice"})
		frappe.db.delete(CONV, {"owner": ["in", [USER_A, USER_B, USER_SM]]})
		for field, value in self._settings_before.items():
			frappe.db.set_single_value(SETTINGS, field, value, update_modified=False)
		frappe.db.commit()
		super().tearDown()

	def _note(self, owner, transcript, context_type="Business", conversation=None):
		# Insert AS the owner: frappe stamps owner from the session user on
		# insert, silently ignoring a passed "owner" key.
		prev = frappe.session.user
		frappe.set_user(owner)
		try:
			doc = frappe.get_doc(
				{
					"doctype": NOTE,
					"transcript": transcript,
					"context_type": context_type,
					"conversation": conversation,
					"source": "Business Tab",
					"status": "New",
				}
			)
			doc.insert(ignore_permissions=True)
			frappe.db.commit()
		finally:
			frappe.set_user(prev)
		return doc

	def _conversation(self, owner):
		# Insert AS the owner (frappe stamps owner from the session user).
		prev = frappe.session.user
		frappe.set_user(owner)
		try:
			doc = frappe.get_doc({"doctype": CONV, "title": "voice test"})
			doc.insert(ignore_permissions=True)
			frappe.db.commit()
		finally:
			frappe.set_user(prev)
		return doc

	def _stamped(self) -> dict:
		singles = frappe.db.get_singles_dict(SETTINGS)
		return {f: singles.get(f) for f in _SETTINGS_FIELDS}


# --------------------------------------------------------------------------- #
# the daily sweep
# --------------------------------------------------------------------------- #
class TestVoiceFactsSweep(VoiceFactsTestCase):
	def test_rule_fact_creates_surfaced_pattern(self):
		n1 = self._note(USER_A, "We always send Acme's deliveries from Mumbai.")
		n2 = self._note(USER_B, "Acme shipments go out of the Mumbai warehouse.")

		with _mock_wiki(), _mock_voice(_fact_json(_rule_item())) as m:
			voice_facts._process_all()

		# one extraction call per owner batch
		self.assertEqual(m.call_count, 2)

		key = voice_facts._pattern_key(RULE_STATEMENT)
		name = frappe.db.exists(JLP, {"pattern_key": key})
		self.assertTrue(name)
		row = frappe.get_doc(JLP, name)
		self.assertEqual(row.detector_id, "voice-context")
		self.assertEqual(row.domain, "stock")
		self.assertEqual(row.status, "Proposed")
		self.assertEqual(row.support_n, 2)
		self.assertEqual(row.strength_band, "Medium")
		self.assertEqual(round(row.confidence_pct), 100)
		# names_party escalates to B (declared == effective)
		self.assertEqual(row.sensitivity, "B")
		self.assertEqual(row.effective_sensitivity, "B")
		# surfaced immediately (voice facts skip the mining surfacing cap)
		self.assertEqual(int(row.surfaced), 1)
		self.assertIsNotNone(row.surfaced_at)
		# bullet grammar: "- <statement>. Evidence: stated in <n> voice
		# note(s) by <m> user(s), last <YYYY-MM-DD>."
		self.assertRegex(
			row.skill_draft,
			r"^- .+\. Evidence: stated in 2 voice note\(s\) by 2 user\(s\), "
			r"last \d{4}-\d{2}-\d{2}\.$",
		)
		ev = frappe.parse_json(row.evidence)
		self.assertEqual(ev["source"], "voice")
		self.assertEqual(sorted(ev["users"]), sorted([USER_A, USER_B]))
		self.assertIn(n1.name, ev["notes"])
		self.assertIn(n2.name, ev["notes"])

		for nm in (n1.name, n2.name):
			st = frappe.db.get_value(NOTE, nm, ["status", "processed_at"], as_dict=True)
			self.assertEqual(st.status, "Processed")
			self.assertTrue(st.processed_at)

		stamped = self._stamped()
		self.assertTrue(stamped["voice_notes_last_processed_at"])
		self.assertTrue(str(stamped["voice_notes_last_process_status"]).startswith("ok"))

		runs = frappe.get_all(
			RUN,
			filters={"scan_mode": "voice"},
			fields=["name", "trigger", "status", "coverage_note", "proposals_created"],
		)
		self.assertEqual(len(runs), 1)
		self.assertEqual(runs[0].trigger, "manual")
		self.assertEqual(runs[0].status, "Completed")
		self.assertIn("Voice-note", runs[0].coverage_note or "")
		self.assertEqual(runs[0].proposals_created, 1)

	def test_pattern_key_is_normalization_stable(self):
		k1 = voice_facts._pattern_key("  Deliveries   to ACME go out of  Mumbai. ")
		k2 = voice_facts._pattern_key("deliveries to acme go out of mumbai.")
		self.assertEqual(k1, k2)
		self.assertEqual(len(k1), 40)
		self.assertNotEqual(k1, voice_facts._pattern_key("deliveries to acme go out of mumbai.", "Some Co"))

	def test_rerun_updates_same_row(self):
		self._note(USER_A, "Acme ships from Mumbai.")
		with _mock_wiki(), _mock_voice(_fact_json(_rule_item())):
			voice_facts._process_all()
		self._note(USER_B, "Reminder: Acme always ships from Mumbai.")
		with _mock_wiki(), _mock_voice(_fact_json(_rule_item())):
			voice_facts._process_all()
		rows = frappe.get_all(JLP, filters={"detector_id": voice_facts.DETECTOR_ID}, pluck="name")
		self.assertEqual(len(rows), 1)

	def test_no_party_fact_gets_sensitivity_a(self):
		statement = "Quotations are usually valid for fifteen days."
		self._note(USER_A, "Our quotes are valid fifteen days.")
		with (
			_mock_wiki(),
			_mock_voice(_fact_json(_rule_item(statement=statement, domain="selling", names_party=False))),
		):
			voice_facts._process_all()
		name = frappe.db.exists(JLP, {"pattern_key": voice_facts._pattern_key(statement)})
		self.assertTrue(name)
		row = frappe.get_doc(JLP, name)
		self.assertEqual(row.sensitivity, "A")
		self.assertEqual(row.effective_sensitivity, "A")

	def test_context_fact_routes_to_wiki_not_jlp(self):
		statement = "Acme Traders prefers morning deliveries."
		note = self._note(USER_A, "Acme likes their deliveries in the morning.")
		with (
			_mock_wiki() as w,
			_mock_voice(_fact_json(_rule_item(statement=statement, domain="selling", kind="context"))),
		):
			voice_facts._process_all()
		self.assertFalse(frappe.db.exists(JLP, {"pattern_key": voice_facts._pattern_key(statement)}))
		self.assertEqual(w.apply.call_count, 1)
		updates, source, user = w.apply.call_args.args
		self.assertEqual(source, "voice")
		self.assertEqual(user, USER_A)
		self.assertEqual(updates[0]["slug"], "org-notes--selling")
		self.assertIn(statement, updates[0]["append_md"])
		# the note is still consumed
		self.assertEqual(frappe.db.get_value(NOTE, note.name, "status"), "Processed")

	def test_malformed_items_are_skipped(self):
		good = "Purchase invoices are booked with update stock."
		self._note(USER_A, "PIs get booked with update stock. Also some noise.")
		fixture = frappe.as_json(
			[
				"nonsense",
				{"domain": "selling"},
				{"statement": good, "domain": "buying", "names_party": False, "kind": "rule"},
			]
		)
		with _mock_wiki(), _mock_voice(fixture):
			voice_facts._process_all()
		rows = frappe.get_all(JLP, filters={"detector_id": voice_facts.DETECTOR_ID}, pluck="name")
		self.assertEqual(len(rows), 1)
		self.assertTrue(frappe.db.exists(JLP, {"pattern_key": voice_facts._pattern_key(good)}))

	def test_failed_extraction_leaves_notes_new(self):
		note = self._note(USER_A, "Something the model never saw.")
		with _mock_wiki(), _mock_voice(frappe.ValidationError("openrouter down")):
			voice_facts._process_all()
		self.assertEqual(frappe.db.get_value(NOTE, note.name, "status"), "New")
		self.assertTrue(str(self._stamped()["voice_notes_last_process_status"]).startswith("partial"))

	def test_unparseable_output_leaves_notes_new(self):
		note = self._note(USER_A, "More audio.")
		with _mock_wiki(), _mock_voice("Sorry, I cannot help with that."):
			voice_facts._process_all()
		self.assertEqual(frappe.db.get_value(NOTE, note.name, "status"), "New")

	def test_conversation_notes_swept_to_wiki_ingest(self):
		conv = self._conversation(USER_A)
		note = self._note(
			USER_A, "Context about this thread.", context_type="Conversation", conversation=conv.name
		)
		with _mock_wiki() as w, _mock_voice("[]"):
			voice_facts._process_all()
		w.ingest.assert_called_once_with(note.name)
		# the wiki ingest owns the status flip; the sweep must not steal it
		self.assertEqual(frappe.db.get_value(NOTE, note.name, "status"), "New")


# --------------------------------------------------------------------------- #
# process_daily gates
# --------------------------------------------------------------------------- #
class TestProcessDailyGates(VoiceFactsTestCase):
	def _run_daily(self, kill_switch=False, flag_on=True):
		conf = dict(frappe.conf)
		conf["jarvis_voice_learning_disabled"] = 1 if kill_switch else 0
		with (
			mock.patch.object(frappe.local, "conf", frappe._dict(conf)),
			mock.patch.object(voice_facts, "_flag_on", return_value=flag_on),
			mock.patch.object(voice_facts, "_enqueue") as enq,
		):
			voice_facts.process_daily()
		return enq

	def test_new_note_enqueues(self):
		self._note(USER_A, "Fresh note.")
		enq = self._run_daily()
		enq.assert_called_once()

	def test_kill_switch_blocks(self):
		self._note(USER_A, "Fresh note.")
		enq = self._run_daily(kill_switch=True)
		enq.assert_not_called()

	def test_disabled_flag_blocks(self):
		self._note(USER_A, "Fresh note.")
		enq = self._run_daily(flag_on=False)
		enq.assert_not_called()

	def test_empty_backlog_skips(self):
		if frappe.db.exists(NOTE, {"status": "New"}):
			self.skipTest("pre-existing New voice notes on this site")
		enq = self._run_daily()
		enq.assert_not_called()

	def test_flag_on_absent_row_reads_on_explicit_zero_reads_off(self):
		"""NULL=ON via row-existence probe: a genuinely-absent tabSingles row
		reads enabled, an explicit 0 reads off, an explicit 1 reads on."""
		field = "voice_features_enabled"
		rows = frappe.db.sql(
			"select value from tabSingles where doctype=%s and field=%s",
			(SETTINGS, field),
		)
		try:
			frappe.db.sql("delete from tabSingles where doctype=%s and field=%s", (SETTINGS, field))
			self.assertTrue(voice_facts._flag_on(field))
			frappe.db.set_single_value(SETTINGS, field, 0, update_modified=False)
			self.assertFalse(voice_facts._flag_on(field))
			frappe.db.set_single_value(SETTINGS, field, 1, update_modified=False)
			self.assertTrue(voice_facts._flag_on(field))
		finally:
			frappe.db.sql("delete from tabSingles where doctype=%s and field=%s", (SETTINGS, field))
			if rows:
				frappe.db.set_single_value(SETTINGS, field, cint(rows[0][0]), update_modified=False)


# --------------------------------------------------------------------------- #
# whitelisted endpoints
# --------------------------------------------------------------------------- #
class TestVoiceNotesApi(VoiceFactsTestCase):
	def test_save_and_list_envelope(self):
		with _as(USER_A):
			out = voice_notes_api.save_voice_note(
				transcript="We always ship Acme from Mumbai.", duration_s=12
			)
			self.assertTrue(out["name"])
			voice_notes_api.save_voice_note(transcript="Second note body.")
			voice_notes_api.save_voice_note(transcript="x" * 400)
			page = voice_notes_api.list_my_voice_notes_page(page_length=2)

		self.assertEqual(page["total"], 3)
		self.assertEqual(len(page["rows"]), 2)
		self.assertTrue(page["has_more"])
		self.assertEqual(page["start"], 0)
		self.assertEqual(page["page_length"], 2)
		for key in ("name", "transcript", "excerpt", "context_type", "status", "creation", "conversation"):
			self.assertIn(key, page["rows"][0])
		# newest first; the 400-char transcript is excerpted to 300
		self.assertEqual(len(page["rows"][0]["excerpt"]), 300)
		self.assertEqual(len(page["rows"][0]["transcript"]), 400)
		self.assertEqual(page["rows"][0]["status"], "New")

		with _as(USER_B):
			page_b = voice_notes_api.list_my_voice_notes_page()
		self.assertEqual(page_b["total"], 0)
		self.assertFalse(page_b["has_more"])

	def test_save_validates_inputs(self):
		with _as(USER_A):
			with self.assertRaises(frappe.ValidationError):
				voice_notes_api.save_voice_note(transcript="   ")
			with self.assertRaises(frappe.ValidationError):
				voice_notes_api.save_voice_note(transcript="ok", context_type="Weird")
			with self.assertRaises(frappe.ValidationError):
				voice_notes_api.save_voice_note(transcript="ok", source="Elsewhere")
			# Conversation context requires a conversation
			with self.assertRaises(frappe.ValidationError):
				voice_notes_api.save_voice_note(transcript="ok", context_type="Conversation")

	def test_conversation_note_requires_owned_conversation(self):
		conv = self._conversation(USER_A)
		with _as(USER_B), self.assertRaises(frappe.PermissionError):
			voice_notes_api.save_voice_note(
				transcript="not mine", context_type="Conversation", conversation=conv.name
			)
		with _as(USER_A), _mock_wiki() as w:
			out = voice_notes_api.save_voice_note(
				transcript="thread context", context_type="Conversation", conversation=conv.name
			)
		w.ingest.assert_called_once_with(out["name"])
		self.assertEqual(frappe.db.get_value(NOTE, out["name"], "context_type"), "Conversation")

	def test_delete_is_owner_only(self):
		with _as(USER_A):
			name = voice_notes_api.save_voice_note(transcript="mine to delete")["name"]
		with _as(USER_B), self.assertRaises(frappe.PermissionError):
			voice_notes_api.delete_voice_note(name)
		with _as(USER_A):
			out = voice_notes_api.delete_voice_note(name)
		self.assertTrue(out["ok"])
		self.assertFalse(frappe.db.exists(NOTE, name))

	def test_guest_is_rejected(self):
		with _as("Guest"):
			with self.assertRaises(frappe.PermissionError):
				voice_notes_api.save_voice_note(transcript="nope")
			with self.assertRaises(frappe.PermissionError):
				voice_notes_api.list_my_voice_notes_page()
			with self.assertRaises(frappe.PermissionError):
				voice_notes_api.get_business_status()

	def test_business_status_shape_and_sm_extras(self):
		self._note(USER_A, "one new note")
		with _as(USER_A):
			st = voice_notes_api.get_business_status()
		self.assertIsInstance(st["stt_enabled"], bool)
		self.assertEqual(st["my_notes"], 1)
		self.assertIsNone(st["org_new_notes"])
		self.assertFalse(st["can_process"])
		self.assertIn("last_processed_at", st)
		self.assertIn("last_process_status", st)

		with _as(USER_SM):
			st_sm = voice_notes_api.get_business_status()
		self.assertTrue(st_sm["can_process"])
		self.assertGreaterEqual(st_sm["org_new_notes"], 1)

	def test_process_now_is_sm_gated_and_dedupes(self):
		# The gate is frappe.only_for("System Manager"), which SHORT-CIRCUITS when
		# frappe.flags.in_test is set — so under a plain test run it enforces
		# nothing. This assertion used to pass only because the fixture user was
		# role-less and blew up earlier for an unrelated reason; now that USER_A is
		# a proper (non-admin) Jarvis user, the flag has to come off for the test
		# to exercise the gate at all.
		with _as(USER_A), _enforcing_permissions(), self.assertRaises(frappe.PermissionError):
			voice_notes_api.process_voice_notes_now()

		with _as(USER_SM):
			with (
				mock.patch("frappe.utils.background_jobs.is_job_enqueued", return_value=False),
				mock.patch.object(voice_facts, "_enqueue") as enq,
			):
				out = voice_notes_api.process_voice_notes_now()
			self.assertEqual(out, {"ok": True})
			enq.assert_called_once()

			with (
				mock.patch("frappe.utils.background_jobs.is_job_enqueued", return_value=True),
				mock.patch.object(voice_facts, "_enqueue") as enq2,
			):
				out2 = voice_notes_api.process_voice_notes_now()
			self.assertFalse(out2["ok"])
			self.assertTrue(out2["reason"])
			enq2.assert_not_called()
