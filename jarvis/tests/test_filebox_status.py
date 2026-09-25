"""File Box truthful status + result (PR-1 §5-6, AC7).

Every File Box row reads exactly one of processing / needs_approval / draft_created
/ failed / no_draft (a SQL ladder, so it stays filterable), with a one-line result.
Also: the server-only ``filebox_*`` conversation fields, the fast-path draft stamp,
``drop_file`` by File docname (M13), ``_send_inbound`` failure stamping, Clear
Processed, the delete refusals and the result backfill patch."""

from __future__ import annotations

import contextlib
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from jarvis import api
from jarvis.chat import api as chat_api
from jarvis.chat import filebox
from jarvis.chat.stale_scan import ORPHAN_MAX_AGE_SECONDS

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
APPROVAL = "Jarvis Approval Request"
USER = "fbs-owner@example.com"
OTHER = "fbs-other@example.com"
PREFIX = "File: fbs-"


def _ensure_user(email: str) -> None:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	if "Jarvis User" not in frappe.get_roles(email):
		frappe.get_doc("User", email).add_roles("Jarvis User")
	frappe.db.commit()


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _cleanup() -> None:
	for conv in frappe.get_all(CONV, filters={"owner": ["in", (USER, OTHER)]}, pluck="name"):
		frappe.db.delete(TURN, {"conversation": conv})
		frappe.db.delete(MSG, {"conversation": conv})
		frappe.db.delete(APPROVAL, {"conversation": conv})
		for f in frappe.get_all("File", filters={"attached_to_name": conv}, pluck="name"):
			frappe.delete_doc("File", f, force=True, ignore_permissions=True)
		frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
	for f in frappe.get_all("File", filters={"file_name": ["like", "fbs-%"]}, pluck="name"):
		frappe.delete_doc("File", f, force=True, ignore_permissions=True)
	frappe.db.commit()


def _submittable_doctype() -> str | None:
	from jarvis.tests._pending_action_helpers import draft_doctype

	return draft_doctype()


class _Base(FrappeTestCase):
	def setUp(self):
		super().setUp()
		_ensure_user(USER)
		_ensure_user(OTHER)
		_cleanup()
		self.addCleanup(_cleanup)

	def _conv(self, owner: str = USER, title: str = "x.pdf", file_box: int = 1, **fields) -> str:
		with _as(owner):
			doc = frappe.get_doc({"doctype": CONV, "title": PREFIX + title, "status": "Active"})
			doc.insert(ignore_permissions=True)
		frappe.db.set_value(CONV, doc.name, {"file_box": file_box, **fields}, update_modified=False)
		return doc.name

	def _msg(self, conv: str, seq: int, role: str, content: str = "", age_s: int = 0, **extra) -> str:
		doc = frappe.get_doc(
			{"doctype": MSG, "conversation": conv, "seq": seq, "role": role, "content": content, **extra}
		)
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		if age_s:
			frappe.db.set_value(
				MSG, doc.name, "creation", now_datetime() - timedelta(seconds=age_s), update_modified=False
			)
		return doc.name

	def _turn(self, conv: str, seed: str, state: str) -> str:
		run_id = f"fbs-{frappe.generate_hash(length=8)}"
		frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": run_id,
				"conversation": conv,
				"relay_target_id": "default",
				"turn_class": "background",
				"state": state,
				"seed_message": seed,
			}
		).insert(ignore_permissions=True)
		return run_id

	def _answered(self, conv: str, reply: str = "Done - drafted the bill.") -> None:
		self._msg(conv, 1, "user", "process this file")
		self._msg(conv, 2, "assistant", reply)

	def _ar(self, conv: str, title: str = "Which supplier?", source: str = "File Box", **extra) -> str:
		doc = frappe.get_doc(
			{
				"doctype": APPROVAL,
				"title": title,
				"question": "q?",
				"status": "Pending",
				"conversation": conv,
				"source": source,
				**extra,
			}
		)
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		return doc.name

	def _rows(self, user: str = USER, **kw) -> dict:
		with _as(user):
			page = filebox.list_inbound_page(page_length=100, **kw)
		return {r["name"]: r for r in page["rows"]}

	def _row(self, conv: str) -> dict:
		return self._rows()[conv]


class TestStatusLadder(_Base):
	def test_needs_approval_links_the_first_pending_request(self):
		conv = self._conv()
		self._answered(conv)
		ar = self._ar(conv)
		row = self._row(conv)
		self.assertEqual(row["status"], "needs_approval")
		self.assertEqual(row["result"], "1 approval waiting: Which supplier?")
		self.assertEqual(row["result_link"], f"/approvals/{ar}")

	def test_a_wiki_only_pending_proposal_is_not_needs_approval(self):
		conv = self._conv()
		self._answered(conv)
		self._ar(conv, title="wiki note", source="File Box Wiki")
		self.assertEqual(self._row(conv)["status"], "no_draft")

	def test_needs_approval_outranks_a_draft_and_mentions_it(self):
		conv = self._conv(filebox_result_doctype="Purchase Invoice", filebox_result_name="PI-1")
		self._answered(conv)
		self._ar(conv)
		self._ar(conv, title="Second")
		row = self._row(conv)
		self.assertEqual(row["status"], "needs_approval")
		self.assertEqual(
			row["result"], "Draft Purchase Invoice PI-1 created · 2 approvals waiting: Which supplier?"
		)

	def test_needs_approval_outranks_a_live_run(self):
		conv = self._conv()
		self._msg(conv, 1, "user", "process")
		self._msg(conv, 2, "assistant", "...", streaming=1)
		self._ar(conv)
		self.assertEqual(self._row(conv)["status"], "needs_approval")

	def test_processing_while_the_reply_streams(self):
		conv = self._conv()
		self._msg(conv, 1, "user", "process")
		self._msg(conv, 2, "assistant", "...", streaming=1)
		self.assertEqual(self._row(conv)["status"], "processing")

	def test_processing_while_a_turn_is_live(self):
		conv = self._conv()
		seed = self._msg(conv, 1, "user", "process")
		self._msg(conv, 2, "assistant", "done")
		self._turn(conv, seed, "queued")
		self.assertEqual(self._row(conv)["status"], "processing")

	def test_processing_outranks_a_draft(self):
		conv = self._conv(filebox_result_doctype="Purchase Invoice", filebox_result_name="PI-1")
		self._msg(conv, 1, "user", "process")
		self._msg(conv, 2, "assistant", "...", streaming=1)
		self.assertEqual(self._row(conv)["status"], "processing")

	def test_a_young_unanswered_drop_is_processing(self):
		conv = self._conv()
		self._msg(conv, 1, "user", "process")
		self.assertEqual(self._row(conv)["status"], "processing")

	def test_an_unanswered_drop_past_the_orphan_threshold_failed(self):
		conv = self._conv()
		self._msg(conv, 1, "user", "process", age_s=ORPHAN_MAX_AGE_SECONDS + 60)
		row = self._row(conv)
		self.assertEqual(row["status"], "failed")
		self.assertTrue(row["result"].startswith("Couldn't start"))

	def test_a_stale_streaming_reply_is_not_processing_forever(self):
		conv = self._conv()
		self._msg(conv, 1, "user", "process", age_s=ORPHAN_MAX_AGE_SECONDS + 120)
		a = self._msg(conv, 2, "assistant", "...", streaming=1)
		frappe.db.set_value(
			MSG,
			a,
			"modified",
			now_datetime() - timedelta(seconds=ORPHAN_MAX_AGE_SECONDS + 60),
			update_modified=False,
		)
		self.assertEqual(self._row(conv)["status"], "failed")

	def test_draft_created_links_to_desk(self):
		conv = self._conv(
			filebox_result_doctype="Purchase Invoice", filebox_result_name="PI-1", filebox_result_count=1
		)
		self._answered(conv)
		row = self._row(conv)
		self.assertEqual(row["status"], "draft_created")
		self.assertEqual(row["result"], "Draft Purchase Invoice PI-1 created")
		self.assertEqual(row["result_link"], "/app/purchase-invoice/PI-1")

	def test_draft_created_outranks_a_later_error_and_counts_the_rest(self):
		conv = self._conv(
			filebox_result_doctype="Purchase Invoice", filebox_result_name="PI-1", filebox_result_count=3
		)
		self._msg(conv, 1, "user", "process")
		self._msg(conv, 2, "assistant", "", error="LLM timed out")
		row = self._row(conv)
		self.assertEqual(row["status"], "draft_created")
		self.assertEqual(
			row["result"], "Draft Purchase Invoice PI-1 created and 2 more · ended with an error"
		)

	def test_failed_on_a_reply_error(self):
		conv = self._conv()
		self._msg(conv, 1, "user", "process")
		self._msg(conv, 2, "assistant", "", error="LLM timed out\nTraceback ...")
		row = self._row(conv)
		self.assertEqual(row["status"], "failed")
		self.assertEqual(row["result"], "Failed: LLM timed out")

	def test_failed_when_stopped(self):
		conv = self._conv()
		self._msg(conv, 1, "user", "process")
		self._msg(conv, 2, "assistant", "partial", stopped=1)
		self.assertEqual(self._row(conv)["status"], "failed")

	def test_failed_when_the_turn_was_cancelled(self):
		conv = self._conv()
		seed = self._msg(conv, 1, "user", "process")
		self._turn(conv, seed, "cancelled")
		row = self._row(conv)
		self.assertEqual(row["status"], "failed")
		self.assertIn("Cancelled", row["result"])

	def test_a_young_drop_whose_turn_ended_without_a_reply_failed(self):
		conv = self._conv()
		seed = self._msg(conv, 1, "user", "process")
		self._turn(conv, seed, "errored")
		row = self._row(conv)
		self.assertEqual(row["status"], "failed")
		self.assertTrue(row["result"].startswith("Couldn't start"))

	def test_a_send_error_newer_than_the_drop_outranks_the_unanswered_arm(self):
		conv = self._conv()
		self._msg(conv, 1, "user", "process", age_s=30)
		frappe.db.set_value(
			CONV,
			conv,
			{
				"filebox_last_error": "Couldn't start: the site is busy",
				"filebox_last_error_at": now_datetime(),
			},
			update_modified=False,
		)
		row = self._row(conv)
		self.assertEqual(row["status"], "failed")
		self.assertEqual(row["result"], "Couldn't start: the site is busy")

	def test_an_older_send_error_does_not_mask_a_later_drop(self):
		conv = self._conv(
			filebox_last_error="Couldn't start: busy",
			filebox_last_error_at=now_datetime() - timedelta(minutes=5),
		)
		self._msg(conv, 1, "user", "process")
		self.assertEqual(self._row(conv)["status"], "processing")

	def test_failed_with_no_user_row_at_all(self):
		conv = self._conv()
		self.assertEqual(self._row(conv)["status"], "failed")

	def test_failed_on_an_empty_final_reply(self):
		conv = self._conv()
		self._answered(conv, reply="  ")
		row = self._row(conv)
		self.assertEqual(row["status"], "failed")
		self.assertEqual(row["result"], "Ended without a reply")

	def test_a_hidden_placeholder_is_ignored(self):
		conv = self._conv()
		self._answered(conv)
		self._msg(conv, 3, "assistant", "", hidden=1)
		self.assertEqual(self._row(conv)["status"], "no_draft")

	def test_no_draft_shows_the_summary_first_line(self):
		conv = self._conv()
		self._answered(conv, reply="**Summary:** logged as `expense` <b>only</b>\nsecond line")
		row = self._row(conv)
		self.assertEqual(row["status"], "no_draft")
		self.assertEqual(row["result"], "Summary: logged as expense only")
		self.assertIsNone(row["result_link"])

	def test_long_summaries_are_truncated(self):
		conv = self._conv()
		self._answered(conv, reply="x" * 400)
		self.assertLessEqual(len(self._row(conv)["result"]), 141)

	def test_identity_is_the_file_box_flag(self):
		fb = self._conv()
		plain = self._conv(file_box=0)
		rows = self._rows()
		self.assertIn(fb, rows)
		self.assertNotIn(plain, rows)

	def test_status_filters_and_legacy_aliases(self):
		draft = self._conv(filebox_result_doctype="Purchase Invoice", filebox_result_name="PI-1")
		self._answered(draft)
		nodraft = self._conv()
		self._answered(nodraft)
		failed = self._conv()
		self._msg(failed, 1, "user", "process")
		self._msg(failed, 2, "assistant", "", error="boom")
		cases = {
			"draft_created": {draft},
			"no_draft": {nodraft},
			"failed": {failed},
			"done": {draft, nodraft},
			"error": {failed},
		}
		for status, expected in cases.items():
			with self.subTest(status=status):
				self.assertEqual(set(self._rows(filters={"status": status})), expected)
		with self.assertRaises(frappe.ValidationError):
			self._rows(filters={"status": "bogus"})

	def test_count_matches_rows_under_a_filter(self):
		for _ in range(2):
			c = self._conv()
			self._answered(c)
		with _as(USER):
			page = filebox.list_inbound_page(filters={"status": "no_draft"})
		self.assertEqual(page["total"], 2)


class TestClearAndDelete(_Base):
	def test_clear_processed_removes_draft_created_and_no_draft_only(self):
		draft = self._conv(filebox_result_doctype="Purchase Invoice", filebox_result_name="PI-1")
		self._answered(draft)
		nodraft = self._conv()
		self._answered(nodraft)
		failed = self._conv()
		self._msg(failed, 1, "user", "process")
		self._msg(failed, 2, "assistant", "", error="boom")
		waiting = self._conv()
		self._answered(waiting)
		self._ar(waiting)
		busy = self._conv()
		self._msg(busy, 1, "user", "process")
		with _as(USER):
			res = filebox.clear_processed_inbound()
		self.assertEqual(res["deleted"], 2)
		for gone in (draft, nodraft):
			self.assertFalse(frappe.db.exists(CONV, gone))
		for kept in (failed, waiting, busy):
			self.assertTrue(frappe.db.exists(CONV, kept))

	def test_a_row_that_fails_mid_cascade_is_rolled_back_not_half_deleted(self):
		"""R-m5: each row commits or rolls back on its own."""
		real = filebox._cascade

		def cascade(conversation):
			if conversation == broken:
				frappe.db.delete(MSG, {"conversation": conversation})
				raise RuntimeError("file store down")
			real(conversation)

		for clear in (True, False):
			with self.subTest(clear=clear):
				fine, broken = self._conv(), self._conv()
				self._answered(fine)
				self._answered(broken)
				frappe.db.commit()
				with _as(USER), patch.object(filebox, "_cascade", side_effect=cascade):
					if clear:
						filebox.clear_processed_inbound()
					else:
						res = filebox.delete_inbound_bulk([fine, broken])
						self.assertEqual([s["conversation"] for s in res["skipped"]], [broken])
				self.assertFalse(frappe.db.exists(CONV, fine))
				self.assertTrue(frappe.db.exists(CONV, broken))
				self.assertEqual(frappe.db.count(MSG, {"conversation": broken}), 2, "its messages survive")

	def _refused(self, conv: str) -> None:
		with _as(USER), self.assertRaises(frappe.ValidationError):
			filebox.delete_inbound(conv)
		self.assertTrue(frappe.db.exists(CONV, conv))

	def test_delete_refuses_a_live_turn(self):
		conv = self._conv()
		seed = self._msg(conv, 1, "user", "process")
		self._msg(conv, 2, "assistant", "done")
		self._turn(conv, seed, "dispatching")
		self._refused(conv)

	def test_delete_refuses_a_young_unanswered_drop(self):
		conv = self._conv()
		self._msg(conv, 1, "user", "process")
		self._refused(conv)

	def test_delete_allows_an_old_orphan(self):
		conv = self._conv()
		self._msg(conv, 1, "user", "process", age_s=ORPHAN_MAX_AGE_SECONDS + 60)
		with _as(USER):
			self.assertTrue(filebox.delete_inbound(conv)["ok"])

	def test_delete_allows_a_drop_whose_send_failed(self):
		conv = self._conv()
		self._msg(conv, 1, "user", "process", age_s=30)
		frappe.db.set_value(
			CONV,
			conv,
			{"filebox_last_error": "Couldn't start: busy", "filebox_last_error_at": now_datetime()},
			update_modified=False,
		)
		with _as(USER):
			self.assertTrue(filebox.delete_inbound(conv)["ok"])

	def test_delete_refuses_while_a_wiki_write_is_applying(self):
		conv = self._conv()
		self._answered(conv)
		self._ar(
			conv,
			title="wiki",
			source="File Box Wiki",
			apply_status="Applying",
			status="Approved",
			decision="ok",
		)
		self._refused(conv)

	def _drafted(self) -> str:
		conv = self._conv(filebox_result_doctype="Purchase Invoice", filebox_result_name="PI-1")
		self._answered(conv)
		return conv

	def _wiki(self, conv: str, status: str = "Pending", apply_status: str | None = None) -> str:
		extra = {"decision": "ok"} if status != "Pending" else {}
		if apply_status:
			extra["apply_status"] = apply_status
		return self._ar(conv, title="wiki", source="File Box Wiki", status=status, **extra)

	def test_clear_keeps_rows_whose_wiki_note_is_still_with_a_reviewer(self):
		pending = self._drafted()
		self._wiki(pending)
		unlanded = self._drafted()
		self._wiki(unlanded, status="Approved", apply_status="Failed")
		landed = self._drafted()
		self._wiki(landed, status="Approved", apply_status="Applied")
		rejected = self._drafted()
		self._wiki(rejected, status="Rejected")
		self.assertEqual(self._row(pending)["status"], "draft_created")
		with _as(USER), patch.object(filebox, "_delete_one", wraps=filebox._delete_one) as one:
			res = filebox.clear_processed_inbound()
		self.assertEqual(res["deleted"], 2)
		# excluded by the Clear query itself, not just refused per row
		self.assertEqual({c.args[0] for c in one.call_args_list}, {landed, rejected})
		for kept in (pending, unlanded):
			self.assertTrue(frappe.db.exists(CONV, kept))
			self.assertTrue(frappe.db.exists(APPROVAL, {"conversation": kept}))
		for gone in (landed, rejected):
			self.assertFalse(frappe.db.exists(CONV, gone))

	def test_delete_refuses_and_bulk_skips_a_wiki_note_awaiting_review(self):
		pending = self._drafted()
		self._wiki(pending)
		unlanded = self._drafted()
		self._wiki(unlanded, status="Approved", apply_status="Pending")
		for conv in (pending, unlanded):
			self._refused(conv)
		frappe.db.commit()  # each bulk row commits or rolls back on its own
		with _as(USER):
			res = filebox.delete_inbound_bulk([pending, unlanded])
		self.assertEqual(res["deleted"], 0)
		self.assertEqual({s["conversation"] for s in res["skipped"]}, {pending, unlanded})
		self.assertIn("wiki note", res["skipped"][0]["reason"])
		self.assertTrue(frappe.db.exists(CONV, pending) and frappe.db.exists(CONV, unlanded))

	def test_delete_refuses_a_non_file_box_conversation(self):
		conv = self._conv(file_box=0)
		self._refused(conv)

	def test_delete_is_owner_gated(self):
		conv = self._conv()
		self._answered(conv)
		with _as(OTHER), self.assertRaises(frappe.PermissionError):
			filebox.delete_inbound(conv)


class TestConversationFieldGuard(_Base):
	"""M12: the filebox_* fields are server-only - no Admin/SM exemption."""

	FIELDS = (
		("filebox_source_file", "f-1"),
		("filebox_result_doctype", "Purchase Invoice"),
		("filebox_result_name", "PI-9"),
		("filebox_result_count", 3),
		("filebox_last_error", "forged"),
		("filebox_last_error_at", "2026-01-01 00:00:00"),
		("filebox_rerun_at", "2026-01-01 00:00:00"),
	)

	def test_admin_save_is_refused(self):
		conv = self._conv()
		for field, value in self.FIELDS:
			doc = frappe.get_doc(CONV, conv)
			doc.set(field, value)
			with self.subTest(field=field), self.assertRaises(frappe.PermissionError):
				doc.save(ignore_permissions=True)

	def test_admin_insert_is_refused(self):
		for field, value in self.FIELDS:
			doc = frappe.get_doc({"doctype": CONV, "title": PREFIX + "ins", field: value})
			with self.subTest(field=field), self.assertRaises(frappe.PermissionError):
				doc.insert(ignore_permissions=True)

	def test_server_flag_allows_and_other_saves_pass(self):
		conv = self._conv(filebox_result_name="PI-1")
		doc = frappe.get_doc(CONV, conv)
		doc.title = PREFIX + "renamed"
		doc.save(ignore_permissions=True)  # untouched guarded fields: no raise
		doc.filebox_result_name = "PI-2"
		doc.flags.jarvis_server_write = True
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_result_name"), "PI-2")


class TestStaleConversationSave(_Base):
	"""m-2: a send that loaded the conversation before a live run stamped a filebox_*
	field neither trips the guard nor reverts the stamp."""

	def _stamp_behind(self, conv: str):
		real = chat_api._next_seq

		def stamp(conversation):
			frappe.db.set_value(
				CONV,
				conv,
				{"filebox_result_doctype": "Purchase Invoice", "filebox_result_name": "PI-LIVE"},
				update_modified=False,
			)
			return real(conversation)

		return patch("jarvis.chat.api._next_seq", side_effect=stamp)

	@contextlib.contextmanager
	def _no_dispatch(self):
		with (
			patch("jarvis.chat.api._dispatch_turn", return_value=None),
			patch("jarvis.chat.admission.turn_machine_enabled", return_value=False),
			patch("jarvis.chat.api.validate_can_send", return_value=(True, None)),
		):
			yield

	def test_send_message(self):
		conv = self._conv()
		with _as(USER), self._no_dispatch(), self._stamp_behind(conv):
			self.assertTrue(chat_api.send_message(conv, "process it again")["ok"])
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_result_name"), "PI-LIVE")

	def test_enqueue_turn(self):
		conv = self._conv()
		with _as(USER), self._no_dispatch(), self._stamp_behind(conv):
			chat_api.enqueue_continuation(conv, "created Purchase Invoice PI-LIVE")
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_result_name"), "PI-LIVE")

	def test_a_column_not_migrated_yet_is_skipped(self):
		"""R-m4: new code before its migrate: the re-read names only migrated columns."""
		from frappe.model.meta import Meta

		conv = self._conv()
		doc = frappe.get_doc(CONV, conv)
		real = Meta.has_field
		with (
			patch.object(Meta, "has_field", lambda m, f: f != "filebox_last_error_at" and real(m, f)),
			patch("frappe.db.get_value", wraps=frappe.db.get_value) as read,
		):
			doc.sync_file_box_fields()
		self.assertNotIn("filebox_last_error_at", read.call_args.args[2])
		self.assertIn("filebox_result_name", read.call_args.args[2])


class TestFastPathStamp(_Base):
	def setUp(self):
		super().setUp()
		self.dt = _submittable_doctype()
		if not self.dt:
			self.skipTest("no submittable doctype installed")

	def _create(self, conv: str, doctype: str, name: str, ok: bool = True, tool: str = "create_doc"):
		ret = {"ok": ok, "data": {"doctype": doctype, "name": name}} if ok else {"ok": False, "error": {}}
		with patch("jarvis.api.dispatch_confirmed", return_value=ret):
			return api._run_tool(tool, {"doctype": doctype, "values": {"x": 1}}, conversation=conv)

	def _stamp(self, conv: str):
		return frappe.db.get_value(
			CONV,
			conv,
			["filebox_result_doctype", "filebox_result_name", "filebox_result_count"],
			as_dict=True,
		)

	def test_a_submittable_create_stamps_the_first_draft_and_counts(self):
		conv = self._conv()
		self._create(conv, self.dt, "D-1")
		self._create(conv, self.dt, "D-2")
		s = self._stamp(conv)
		self.assertEqual(
			(s.filebox_result_doctype, s.filebox_result_name, s.filebox_result_count), (self.dt, "D-1", 2)
		)

	def test_no_stamp_for_a_master_a_failure_or_an_update(self):
		conv = self._conv()
		self._create(conv, "ToDo", "T-1")
		self._create(conv, self.dt, "D-1", ok=False)
		self._create(conv, self.dt, "D-1", tool="update_doc")
		self.assertFalse(self._stamp(conv).filebox_result_name)


class TestDropFile(_Base):
	def _file(self, name: str = "fbs-invoice.txt", content: str = "fbs-content", owner: str = USER, **extra):
		with _as(owner):
			return frappe.get_doc(
				{"doctype": "File", "file_name": name, "content": content, "is_private": 1, **extra}
			).insert(ignore_permissions=True)

	def _drop(self, send=None, **kw):
		send = send or (lambda **_: {"ok": True, "run_id": "r1"})
		with _as(USER), patch("jarvis.chat.api.send_message", side_effect=send) as sm:
			res = filebox.drop_file(**kw)
		return res, sm

	def test_drop_by_docname_stamps_the_source_file(self):
		f = self._file()
		res, sm = self._drop(file=f.name)
		self.assertTrue(res["ok"])
		conv = res["conversation_id"]
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_source_file"), f.name)
		self.assertEqual(frappe.db.get_value("File", f.name, "attached_to_name"), conv)
		self.assertIn(f.file_url, sm.call_args.kwargs["attachments"])

	def test_a_re_upload_never_moves_the_earlier_drops_file(self):
		first = self._file()
		earlier = self._drop(file=first.name)[0]["conversation_id"]
		second = self._file()  # identical content: Frappe reuses the file_url
		self.assertEqual(second.file_url, first.file_url)
		later = self._drop(file=second.name)[0]["conversation_id"]
		self.assertEqual(frappe.db.get_value("File", first.name, "attached_to_name"), earlier)
		self.assertEqual(frappe.db.get_value("File", second.name, "attached_to_name"), later)

	def test_file_url_fallback_still_works(self):
		f = self._file()
		res, _ = self._drop(file_url=f.file_url, file_name="fbs-invoice.txt")
		self.assertEqual(frappe.db.get_value(CONV, res["conversation_id"], "filebox_source_file"), f.name)

	def test_a_file_attached_elsewhere_is_refused(self):
		with _as(USER):
			todo = frappe.get_doc({"doctype": "ToDo", "description": "fbs-owner-todo"}).insert(
				ignore_permissions=True
			)
		self.addCleanup(frappe.delete_doc, "ToDo", todo.name, force=True, ignore_permissions=True)
		f = self._file(attached_to_doctype="ToDo", attached_to_name=todo.name)
		with self.assertRaisesRegex(frappe.PermissionError, "attached"):
			self._drop(file=f.name)
		self.assertEqual(frappe.db.get_value("File", f.name, "attached_to_name"), todo.name)

	def test_a_file_on_someone_elses_conversation_is_refused(self):
		theirs = self._conv(owner=OTHER)
		f = self._file(name="fbs-shared.txt", owner=USER)
		frappe.db.set_value(
			"File", f.name, {"attached_to_doctype": CONV, "attached_to_name": theirs}, update_modified=False
		)
		with self.assertRaisesRegex(frappe.PermissionError, "attached"):
			self._drop(file=f.name)
		self.assertEqual(frappe.db.get_value("File", f.name, "attached_to_name"), theirs)

	def test_a_file_on_the_callers_own_conversation_is_re_parented(self):
		mine = self._conv()
		f = self._file(name="fbs-mine.txt")
		frappe.db.set_value(
			"File", f.name, {"attached_to_doctype": CONV, "attached_to_name": mine}, update_modified=False
		)
		res, _ = self._drop(file=f.name)
		self.assertEqual(frappe.db.get_value("File", f.name, "attached_to_name"), res["conversation_id"])

	def test_someone_elses_unattached_public_file_is_refused(self):
		# e.g. a branding asset: public, unattached, readable by everyone
		f = self._file(name="fbs-brand.txt", content="fbs-logo", owner="Administrator", is_private=0)
		before = frappe.db.count(CONV, {"owner": USER})
		for kw in ({"file": f.name}, {"file_url": f.file_url}):
			with self.subTest(**kw), self.assertRaisesRegex(frappe.PermissionError, "someone else"):
				self._drop(**kw)
		self.assertFalse(frappe.db.get_value("File", f.name, "attached_to_name"))
		self.assertEqual(frappe.db.count(CONV, {"owner": USER}), before)

	def test_a_raising_send_is_stamped_and_logged_not_raised(self):
		f = self._file()

		def boom(**_):
			raise RuntimeError("worker queue exploded")

		before = frappe.db.count("Error Log", {"method": "jarvis.file_box.send_failed"})
		res, _ = self._drop(send=boom, file=f.name)
		self.assertFalse(res["ok"])
		self.assertNotIn("exploded", res["reason"])
		conv = res["conversation_id"]
		err = frappe.db.get_value(CONV, conv, ["filebox_last_error", "filebox_last_error_at"], as_dict=True)
		self.assertTrue(err.filebox_last_error.startswith("Couldn't start"))
		self.assertIsNotNone(err.filebox_last_error_at)
		self.assertEqual(frappe.db.count("Error Log", {"method": "jarvis.file_box.send_failed"}), before + 1)
		self.assertEqual(self._row(conv)["status"], "failed")

	def test_a_refused_send_stamps_its_reason_and_a_later_success_clears_it(self):
		f = self._file()
		res, _ = self._drop(send=lambda **_: {"ok": False, "reason": "The site is busy"}, file=f.name)
		conv = res["conversation_id"]
		self.assertEqual(
			frappe.db.get_value(CONV, conv, "filebox_last_error"), "Couldn't start: The site is busy"
		)
		with patch("jarvis.chat.api.send_message", return_value={"ok": True}):
			filebox._send_inbound(conv, frappe.get_doc("File", f.name), None)
		err = frappe.db.get_value(CONV, conv, ["filebox_last_error", "filebox_last_error_at"], as_dict=True)
		self.assertFalse(err.filebox_last_error)
		self.assertIsNone(err.filebox_last_error_at)

	def test_list_inbound_is_gone(self):
		self.assertFalse(hasattr(filebox, "list_inbound"))


class TestResultBackfillPatch(_Base):
	def setUp(self):
		super().setUp()
		self.dt = _submittable_doctype()
		if not self.dt:
			self.skipTest("no submittable doctype installed")

	def _tool_row(self, conv, seq, doctype, name, status="completed", tool="create_doc"):
		self._msg(
			conv,
			seq,
			"tool",
			f"{tool} → {status}",
			tool_name=tool,
			tool_status=status,
			ref_doctype=doctype,
			ref_name=name,
		)

	def test_backfill_stamps_from_completed_submittable_creates(self):
		from jarvis.patches import v2_22_backfill_filebox_results as patch_mod

		conv = self._conv()
		self._msg(conv, 1, "user", "process")
		self._tool_row(conv, 2, "ToDo", "T-1")
		self._tool_row(conv, 3, self.dt, "D-1")
		self._tool_row(conv, 4, self.dt, "D-9", status="error")
		self._tool_row(conv, 5, self.dt, "D-2")
		stamped = self._conv(
			filebox_result_doctype=self.dt, filebox_result_name="KEEP", filebox_result_count=1
		)
		self._tool_row(stamped, 1, self.dt, "D-3")
		plain = self._conv(file_box=0)
		self._tool_row(plain, 1, self.dt, "D-4")
		patch_mod.execute()
		row = frappe.db.get_value(
			CONV,
			conv,
			["filebox_result_doctype", "filebox_result_name", "filebox_result_count"],
			as_dict=True,
		)
		self.assertEqual(
			(row.filebox_result_doctype, row.filebox_result_name, row.filebox_result_count),
			(self.dt, "D-1", 2),
		)
		self.assertEqual(frappe.db.get_value(CONV, stamped, "filebox_result_name"), "KEEP")
		self.assertFalse(frappe.db.get_value(CONV, plain, "filebox_result_name"))
		patch_mod.execute()  # idempotent
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_result_count"), 2)
