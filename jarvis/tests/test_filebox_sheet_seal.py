"""File Box sheets, sealing and the board (T3 S1b).

A document's collecting sheet seals exactly once when ITS turn ends (the finalize
effect, the errored paths, the legacy completion, a later turn's first write, the
reconciler backstop); a user Stop / archive / delete discards it, a re-run
supersedes it, and every ended sheet closes its linked questions centrally. The
board, the badge and the File Box ladder read sealed sheets without the card."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jarvis.chat import (
	approvals_api,
	filebox,
	finalize,
	held_sheet_seal,
	held_sheets,
	held_writes,
	settlement,
)
from jarvis.chat import pending_actions as pa
from jarvis.chat import turn_state as ts
from jarvis.chat.pending_actions import _reconcile, _seal, _store
from jarvis.chat.turn_message_binding import clear_run_cancel, request_run_cancel
from jarvis.tests._pending_action_helpers import as_user, ensure_user
from jarvis.tests.test_filebox_sheets import (
	AR,
	CONV,
	MSG,
	OTHER,
	OWNER,
	PA,
	PARTY,
	TAX,
	WAITER,
	_address,
	_Base,
	_batch,
	_item,
	_question,
	_supplier,
)

TURN = "Jarvis Chat Turn"
SM = "fbss-sm@example.com"
SEAL_EFFECT = "file_box_sheet_seal"
_TITLES = (
	"jarvis.file_box.sheet_seal_backstop",
	"jarvis.file_box.sheet_collecting_stuck",
	"jarvis.file_box.routing_backlog",
	"jarvis.file_box.sheet_seal_failed",
)


class _SealBase(_Base):
	def setUp(self):
		super().setUp()
		ensure_user(SM, ("Jarvis User", "System Manager"))
		self.enqueued = []
		q = patch("frappe.enqueue", side_effect=lambda *a, **k: self.enqueued.append((a, k)))
		q.start()
		self.addCleanup(q.stop)
		self.events = []
		ev = patch("jarvis.chat.events.publish_to_user", side_effect=lambda u, p: self.events.append((u, p)))
		ev.start()
		self.addCleanup(ev.stop)
		fenced = patch.object(ts, "publish_to_user", lambda *a, **k: None)
		fenced.start()
		self.addCleanup(fenced.stop)
		self._clear_logs()
		self.addCleanup(self._clear_logs)

	def _clear_logs(self):
		frappe.db.delete("Error Log", {"method": ["in", _TITLES]})
		frappe.db.delete("Jarvis Turn Effect", {"turn": ["like", "fbss-%"]})
		frappe.db.commit()

	# -- fixtures ---------------------------------------------------------------- #
	def turn(self, conv, state="streaming", **extra) -> str:
		seq = (frappe.db.sql(f"SELECT MAX(seq) FROM `tab{MSG}` WHERE conversation=%s", conv)[0][0] or 0) + 1
		seed = frappe.get_doc(
			{"doctype": MSG, "conversation": conv, "seq": seq, "role": "user", "content": "go"}
		).insert(ignore_permissions=True)
		rid = "fbss-" + frappe.generate_hash(length=10)
		frappe.get_doc(
			{
				"doctype": TURN,
				"run_id": rid,
				"conversation": conv,
				"relay_target_id": "fbss-target",
				"turn_class": "interactive",
				"state": state,
				"version": 1,
				"seed_message": seed.name,
				**extra,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return rid

	def set_turn(self, rid, **cols):
		frappe.db.set_value(TURN, rid, cols, update_modified=False)
		frappe.db.commit()

	def collecting(self, conv=None, *, questions=0, owner=OWNER):
		"""A live turn with a supplier + address (+ questions) on its collecting sheet."""
		conv = conv or self.conv(owner)
		rid = self.turn(conv)
		self.assert_added(self.call("create_doc", _batch(_supplier(), _address()), conv, owner))
		for i in range(questions):
			self.assertTrue(self.call("create_doc", _question(f"zz-fbs q{i}"), conv, owner)["ok"])
		return conv, rid, self.sheet(conv)

	def sealed(self, **kw):
		conv, rid, sheet = self.collecting(**kw)
		self.set_turn(rid, state="done")
		self.assertEqual(held_sheet_seal.seal_turn(conv, rid), sheet.name)
		return conv, rid, self.row(sheet.name)

	def row(self, name):
		return _store.get_row(name)

	def questions(self, sheet):
		return frappe.get_all(AR, filters={"sheet": sheet}, fields=["name", "status", "decision"])

	def notices(self, owner=OWNER):
		return frappe.db.count(
			"Notification Log", {"for_user": owner, "subject": ["like", "Approval needed%"]}
		)

	def approval_events(self, name):
		return [p for _u, p in self.events if p.get("kind") == "approval:new" and p.get("name") == name]

	def resumes(self):
		return [k for a, k in self.enqueued if a and a[0] == "jarvis.chat.held_writes.resume_waiters"]

	def finalize(self, rid, effects=(SEAL_EFFECT,)):
		ts.insert_required_effects(rid, effects)
		frappe.db.commit()
		return finalize.run_finalize(rid)

	def logged(self, title):
		return frappe.get_all("Error Log", filters={"method": title}, pluck="error")

	def live_sheets(self, conv):
		return [s.name for s in self.sheets() if s.conversation == conv and s.status == "Pending"]


# --------------------------------------------------------------------------- #
# The finalize effect
# --------------------------------------------------------------------------- #
class TestTurnEndSeal(_SealBase):
	def test_the_effect_is_visible_owed_by_every_terminal_and_ordered_after_asks(self):
		names = ts.EFFECT_NAMES
		self.assertEqual(names.index(SEAL_EFFECT), names.index("chat_asks") + 1)
		self.assertIn(SEAL_EFFECT, ts.VISIBLE_EFFECT_NAMES)
		self.assertIn(SEAL_EFFECT, settlement.TERMINAL_EFFECTS)
		self.assertIn(SEAL_EFFECT, settlement.FINAL_EFFECTS)
		self.assertIn(SEAL_EFFECT, finalize._RUNNERS)

	def test_a_completed_turn_seals_its_sheet_once_and_notifies_once(self):
		conv, rid, sheet = self.collecting(questions=1)
		self.set_turn(rid, state="finalizing")
		self.finalize(rid)
		row = self.row(sheet.name)
		self.assertEqual((row.status, row.collecting), ("Pending", 0))
		self.assertEqual(self.notices(), 1)
		self.assertEqual(len(self.approval_events(sheet.name)), 1)
		# A replayed effect, and any later seal of the same turn, seal and notify nothing.
		ctx = finalize._Ctx(run_id=rid, turn={}, conversation=conv, owner=OWNER, errored=False, payload={})
		finalize._RUNNERS[SEAL_EFFECT](ctx)
		self.assertIsNone(held_sheet_seal.seal_turn(conv, rid))
		self.assertEqual(self.notices(), 1)
		self.assertEqual(len(self.approval_events(sheet.name)), 1)
		self.assertEqual(self.resumes(), [], "a sealed sheet waits for a human")

	def test_only_the_seal_winner_notifies(self):
		conv, rid, sheet = self.collecting()
		read_before = self.row(sheet.name)  # two sealers that read it collecting
		self.assertEqual(held_sheet_seal._seal_locked(read_before), sheet.name)
		self.assertIsNone(held_sheet_seal._seal_locked(read_before))
		self.assertEqual((self.notices(), len(self.approval_events(sheet.name))), (1, 1))

	def test_errored_and_system_cancelled_turns_seal_too(self):
		for state in ("errored", "cancelled"):
			with self.subTest(state=state):
				conv, rid, sheet = self.collecting()
				self.set_turn(rid, state=state)
				self.finalize(rid, settlement.TERMINAL_EFFECTS)
				self.assertEqual(self.row(sheet.name).collecting, 0)
				self.assertEqual(self.row(sheet.name).status, "Pending")

	def test_a_user_stopped_turn_discards_its_sheet(self):
		conv, rid, sheet = self.collecting(questions=1)
		self.set_turn(rid, state="cancelled", cancel_requested=1)
		self.finalize(rid, settlement.TERMINAL_EFFECTS)
		row = self.row(sheet.name)
		self.assertEqual((row.status, row.collecting, row.settled), ("Discarded", 0, 1))
		self.assertEqual({q.status for q in self.questions(sheet.name)}, {"Dismissed"})
		self.assertEqual((self.notices(), self.resumes()), (0, []))

	def test_a_stop_that_raced_the_completion_still_discards(self):
		conv, rid, sheet = self.collecting(questions=1)
		self.set_turn(rid, state="finalizing", cancel_requested=1)
		self.finalize(rid)
		self.assertEqual(self.row(sheet.name).status, "Discarded")
		self.assertEqual(self.notices(), 0)

	def test_a_retried_effect_never_seals_the_next_turns_sheet(self):
		conv, t1, first = self.collecting()
		self.set_turn(t1, state="done")
		_store._terminal_update(first.name, ["Pending"], "Discarded", reason_code="discarded")
		frappe.db.commit()
		t2 = self.turn(conv)
		self.assert_added(self.call("create_doc", _item("zz-fbs I1"), conv))
		second = self.sheet_of_turn(conv, t2)
		self.assertIsNone(held_sheet_seal.seal_turn(conv, t1), "T1's effect seals only T1's sheet")
		self.assertEqual(self.row(second.name).collecting, 1)
		self.assertEqual(held_sheet_seal.seal_turn(conv, t2), second.name)

	def sheet_of_turn(self, conv, rid):
		[row] = [s for s in self.sheets() if s.conversation == conv and s.collecting_turn == rid]
		return row

	def test_an_empty_sheet_is_discarded_quietly(self):
		conv = self.conv()
		rid = self.turn(conv)
		with as_user(OWNER):  # none opens empty now: open one by hand
			name = held_sheets._open(conv, OWNER, [])
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_sheet_count"), 1)
		self.set_turn(rid, state="done")
		self.assertIsNone(held_sheet_seal.seal_turn(conv, rid))
		row = self.row(name)
		self.assertEqual((row.status, row.collecting, row.settled), ("Discarded", 0, 1))
		self.assertEqual(frappe.db.count(WAITER, {"parent": name}), 0)
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_sheet_count"), 0, "no pause was used")
		self.assertEqual((self.notices(), self.resumes()), (0, []))

	def test_a_non_file_box_turn_is_untouched(self):
		conv = self.conv()
		frappe.db.set_value(CONV, conv, "file_box", 0)
		rid = self.turn(conv, state="finalizing")
		out = self.finalize(rid)
		self.assertTrue(out["ok"])
		self.assertEqual(frappe.db.get_value("Jarvis Turn Effect", f"{rid}::{SEAL_EFFECT}", "status"), "done")


# --------------------------------------------------------------------------- #
# Turn ends that bypass settlement
# --------------------------------------------------------------------------- #
class TestErroredPaths(_SealBase):
	def test_prepare_errored_seals(self):
		from jarvis.chat import prepare

		conv, rid, sheet = self.collecting()
		self.set_turn(rid, state="preparing", version=4)
		prepare._prepare_error(rid, 4, None, conv, OWNER, "boom")
		self.assertEqual(frappe.db.get_value(TURN, rid, "state"), "errored")
		self.assertEqual(self.row(sheet.name).collecting, 0)

	def test_dispatch_errored_seals(self):
		from jarvis.chat import pump
		from jarvis.exceptions import AgentUnreachableError

		conv, rid, sheet = self.collecting()
		self.set_turn(rid, state="dispatching", version=3, pump_epoch=2)
		ctx = SimpleNamespace(epoch=2, relay_target_id="fbss-target")
		rs = SimpleNamespace(run_id=rid, assistant_message=None, owner=None, conversation=conv)
		pump._handle_ack_failure(ctx, rs, AgentUnreachableError("rejected", code="policy_denied"))
		self.assertEqual(frappe.db.get_value(TURN, rid, "state"), "errored")
		self.assertEqual(self.row(sheet.name).collecting, 0)

	def test_recover_errored_seals(self):
		from jarvis.chat import pump

		conv, rid, sheet = self.collecting()
		self.set_turn(rid, state="recovering", version=7)
		self.assertTrue(pump._settle_recover_errored(rid, 7, conv, None))
		self.assertEqual(self.row(sheet.name).collecting, 0)

	def test_the_legacy_completion_seals_its_own_turn(self):
		from jarvis.chat import turn_handler

		conv, rid, sheet = self.collecting()
		other = self.turn(self.conv())
		turn_handler._advance_macro(conv, errored=False, run_id=other)
		self.assertEqual(self.row(sheet.name).collecting, 1, "another run's completion seals nothing")
		turn_handler._advance_macro(conv, errored=False, run_id=rid)
		self.assertEqual(self.row(sheet.name).collecting, 0)

	def test_the_legacy_recovery_seals_once_its_turn_is_over(self):
		from jarvis.chat import turn_recovery

		conv, rid, sheet = self.collecting()
		turn_recovery._advance_macro(conv, errored=True)
		self.assertEqual(self.row(sheet.name).collecting, 1, "its turn still runs")
		self.set_turn(rid, state="errored")
		turn_recovery._advance_macro(conv, errored=True)
		self.assertEqual(self.row(sheet.name).collecting, 0)

	def test_the_legacy_end_discards_a_sheet_its_stopped_run_collected(self):
		# It runs before admission clears the turn's cancel intent; the signal may be gone.
		from jarvis.chat import turn_handler

		for signal, flagged in ((True, 0), (False, 1)):
			with self.subTest(signal=signal, flagged=flagged):
				conv, rid, sheet = self.collecting()
				self.set_turn(rid, state="dispatching", cancel_requested=flagged)
				if signal:
					request_run_cancel(conv)
				try:
					turn_handler._advance_macro(conv, errored=True, run_id=rid)
				finally:
					clear_run_cancel(conv)
				self.assertEqual(self.row(sheet.name).status, "Discarded")
				self.assertEqual(self.notices(), 0)

	def test_recovery_with_no_run_never_seals_a_turn_less_sheet(self):
		from jarvis.chat import turn_recovery

		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv))
		sheet = self.sheet(conv)
		turn_recovery._advance_macro(conv, errored=True)
		self.assertEqual(self.row(sheet.name).collecting, 1, "it may be another run's")

	def test_a_turn_less_legacy_sheet_seals_at_its_completion(self):
		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv))
		sheet = self.sheet(conv)
		self.assertEqual(sheet.collecting_turn or "", "")
		held_sheet_seal.after_turn(conv, "fbss-legacy-run", legacy=True)
		self.assertEqual(self.row(sheet.name).collecting, 0)

	def test_a_hook_failure_is_logged_never_raised(self):
		conv, rid, sheet = self.collecting()
		with patch.object(held_sheet_seal, "seal_turn", side_effect=RuntimeError("down")):
			held_sheet_seal.after_turn(conv, rid)
		self.assertEqual(len(self.logged("jarvis.file_box.sheet_seal_failed")), 1)
		self.assertEqual(self.row(sheet.name).collecting, 1, "the reconciler seals it later")


# --------------------------------------------------------------------------- #
# A later turn's first write
# --------------------------------------------------------------------------- #
class TestInlineSeal(_SealBase):
	def test_an_older_turns_empty_sheet_gives_way_to_a_new_one(self):
		conv = self.conv()
		t1 = self.turn(conv)
		with as_user(OWNER):
			old = held_sheets._open(conv, OWNER, [])
		self.set_turn(t1, state="done")
		t2 = self.turn(conv)
		self.assert_added(self.call("create_doc", _supplier(), conv), count=1)
		self.assertEqual(self.row(old).status, "Discarded")
		[new] = self.live_sheets(conv)
		row = self.row(new)
		self.assertEqual((row.collecting_turn, row.collecting, row.record_count), (t2, 1, 1))
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_sheet_count"), 1)
		self.assertEqual(self.notices(), 0)

	def test_a_stopped_turn_adds_nothing_once_the_signal_is_gone(self):
		conv, rid, sheet = self.collecting()
		self.set_turn(rid, cancel_requested=1)
		res = self.call("create_doc", _item("zz-fbs I1"), conv)
		self.assertFalse(res["ok"])
		self.assertEqual(res["error"]["code"], "FileBoxRefusedError")
		self.assertEqual(self.row(sheet.name).record_count, 2)

	def test_the_same_turn_keeps_appending(self):
		conv, rid, sheet = self.collecting()
		self.assert_added(self.call("create_doc", _item("zz-fbs I1"), conv), count=3)
		self.assertEqual(self.row(sheet.name).collecting, 1)

	def test_a_new_turn_seals_the_older_turns_sheet_then_waits(self):
		for older in ("done", "finalizing"):
			with self.subTest(older=older):
				conv, t1, sheet = self.collecting()
				self.set_turn(t1, state=older)
				self.turn(conv)
				res = self.call("create_doc", _item("zz-fbs I1"), conv)
				self.assertFalse(res["ok"])
				self.assertEqual(res["error"]["code"], "ApprovalPendingError")
				row = self.row(sheet.name)
				self.assertEqual((row.collecting, row.record_count), (0, 2), "never appended by T2")
				self.assertEqual([s.name for s in self.sheets() if s.conversation == conv], [sheet.name])
				self.assertEqual(len(self.approval_events(sheet.name)), 1)


# --------------------------------------------------------------------------- #
# The reconciler backstop
# --------------------------------------------------------------------------- #
class TestBackstop(_SealBase):
	def ahead(self, **delta):
		"""The backstop's clock (it reads ``frappe.utils.now_datetime``), ``delta`` on."""
		later = frappe.utils.add_to_date(frappe.utils.now_datetime(), **delta)
		return patch("frappe.utils.now_datetime", return_value=later)

	def test_the_reconciler_runs_the_backstop(self):
		steps = [
			n
			for n in dir(_reconcile)
			if n.startswith(("_reap", "_settle_", "_retry", "_cancel", "_warn", "_cards"))
		]
		with (
			patch.multiple(_reconcile, **{n: (lambda: 0) for n in steps}),
			patch.object(held_sheet_seal, "backstop", return_value={"sealed": 7}),
		):
			self.assertEqual(_reconcile.reconcile()["sheet_backstop"], {"sealed": 7})

	def test_a_stranded_sheet_seals_after_five_minutes(self):
		conv, rid, sheet = self.collecting()
		self.set_turn(rid, state="errored")
		held_sheet_seal.backstop()
		self.assertEqual(self.row(sheet.name).collecting, 1, "never on age alone: too young")
		with self.ahead(minutes=6):
			out = held_sheet_seal.backstop()
		self.assertEqual(out["sealed"], 1)
		self.assertEqual(self.row(sheet.name).collecting, 0)
		self.assertEqual(self.notices(), 1)
		[log] = self.logged("jarvis.file_box.sheet_seal_backstop")
		self.assertIn(sheet.name, log)

	def test_a_long_running_turn_is_never_sealed(self):
		conv, rid, sheet = self.collecting()
		for minutes in (6, 60, 119):
			with self.ahead(minutes=minutes):
				held_sheet_seal.backstop()
		self.assertEqual(self.row(sheet.name).collecting, 1)
		self.assertEqual(self.notices(), 0)

	def test_a_streaming_reply_holds_the_backstop(self):
		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv))
		sheet = self.sheet(conv)
		reply = frappe.get_doc(
			{"doctype": MSG, "conversation": conv, "seq": 90, "role": "assistant", "streaming": 1}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		with self.ahead(minutes=10):
			held_sheet_seal.backstop()
		self.assertEqual(self.row(sheet.name).collecting, 1)
		frappe.db.set_value(MSG, reply.name, "streaming", 0)
		frappe.db.commit()
		with self.ahead(minutes=10):
			held_sheet_seal.backstop()
		self.assertEqual(self.row(sheet.name).collecting, 0)

	def reply(self, conv, seq, **cols):
		return frappe.get_doc(
			{"doctype": MSG, "conversation": conv, "seq": seq, "role": "assistant", **cols}
		).insert(ignore_permissions=True)

	def test_an_orphaned_streaming_row_never_holds_the_backstop(self):
		# An older reply left streaming, then a stale latest one: neither is a live reply.
		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv))
		sheet = self.sheet(conv)
		self.reply(conv, 5, streaming=1)
		self.reply(conv, 90, streaming=0)
		frappe.db.commit()
		with self.ahead(minutes=10):
			held_sheet_seal.backstop()
		self.assertEqual(self.row(sheet.name).collecting, 0)
		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv))
		sheet = self.sheet(conv)
		latest = self.reply(conv, 90, recovering=1)
		frappe.db.sql(
			f"UPDATE `tab{MSG}` SET modified=%s WHERE name=%s",
			(frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-4), latest.name),
		)
		frappe.db.commit()
		with self.ahead(minutes=10):
			held_sheet_seal.backstop()
		self.assertEqual(self.row(sheet.name).collecting, 0)

	def test_a_streaming_reply_older_than_the_freshness_window_no_longer_blocks_the_seal(self):
		"""STREAMING_FRESH_S is its own constant, well under COLLECTING_MAX_S (2h): a
		streaming latest row past it lets the backstop seal gracefully, long before
		_fail_stuck would hard-fail the sheet as "stuck"."""
		self.assertLess(held_sheet_seal.STREAMING_FRESH_S, held_sheet_seal.COLLECTING_MAX_S)
		conv = self.conv()
		self.assert_added(self.call("create_doc", _supplier(), conv))
		sheet = self.sheet(conv)
		reply = self.reply(conv, 90, streaming=1)
		frappe.db.sql(
			f"UPDATE `tab{MSG}` SET modified=%s WHERE name=%s",
			(
				frappe.utils.add_to_date(
					frappe.utils.now_datetime(), seconds=-(held_sheet_seal.STREAMING_FRESH_S + 60)
				),
				reply.name,
			),
		)
		frappe.db.commit()
		with self.ahead(minutes=10):
			held_sheet_seal.backstop()
		self.assertEqual(self.row(sheet.name).collecting, 0, "past the window: the seal isn't held")

	def test_collecting_for_two_hours_fails_and_alerts_once(self):
		first = self.collecting(questions=1)[2]
		second = self.collecting()[2]
		with self.ahead(hours=2, minutes=1):
			out = held_sheet_seal.backstop()
		self.assertEqual(out["failed"], 2)
		for sheet in (first, second):
			row = self.row(sheet.name)
			self.assertEqual((row.status, row.collecting, row.settled), ("Failed", 0, 1))
			self.assertEqual(row.reason_code, "stuck")
		self.assertIn("stuck while it was still being listed", held_sheet_seal.resume_message(row))
		self.assertEqual({q.status for q in self.questions(first.name)}, {"Dismissed"})
		self.assertEqual(len(self.logged("jarvis.file_box.sheet_collecting_stuck")), 1)
		third = self.collecting()[2]
		with self.ahead(hours=2, minutes=5):
			held_sheet_seal.backstop()
		self.assertEqual(self.row(third.name).status, "Failed")
		self.assertEqual(len(self.logged("jarvis.file_box.sheet_collecting_stuck")), 1, "deduped")

	def test_a_routing_backlog_alerts_once(self):
		# Relative to what the site already holds: only this test's questions tip it.
		with patch.object(held_sheet_seal, "ROUTING_BACKLOG_ALERT", 10**9):
			base = held_sheet_seal._routing_backlog()
		conv, extra = self.conv(), 3
		for i in range(extra):
			frappe.get_doc(
				{
					"doctype": AR,
					"title": f"zz-fbs route {i}",
					"question": "Which skill?",
					"conversation": conv,
				}
			).insert(ignore_permissions=True)
		frappe.db.sql(f"UPDATE `tab{AR}` SET routing='skill_missing' WHERE conversation=%s", conv)
		frappe.db.commit()
		with patch.object(held_sheet_seal, "ROUTING_BACKLOG_ALERT", base + extra - 1):
			held_sheet_seal.backstop()
			self.assertEqual(self.logged("jarvis.file_box.routing_backlog"), [], "young questions")
			# Aged by backdating, so the first alert's own log stays inside the dedupe window.
			aged = frappe.utils.add_to_date(
				frappe.utils.now_datetime(), hours=-(held_sheet_seal.ROUTING_BACKLOG_HOURS + 1)
			)
			frappe.db.sql(f"UPDATE `tab{AR}` SET creation=%s WHERE conversation=%s", (aged, conv))
			frappe.db.commit()
			held_sheet_seal.backstop()
			held_sheet_seal.backstop()
		self.assertEqual(len(self.logged("jarvis.file_box.routing_backlog")), 1)

	def test_orphaned_linked_questions_are_closed(self):
		conv, rid, sheet = self.sealed(questions=1)
		# An ended, settled sheet whose question stayed Pending (settled before S1b).
		frappe.db.sql(f"UPDATE `tab{PA}` SET status='Failed', settled=1 WHERE name=%s", sheet.name)
		frappe.db.commit()
		self.assertEqual(held_sheet_seal.backstop()["closed"], 1)
		self.assertEqual({q.status for q in self.questions(sheet.name)}, {"Dismissed"})

	def test_questions_of_a_purged_sheet_are_closed(self):
		_conv, _rid, sheet = self.sealed(questions=1)
		frappe.db.sql(f"DELETE FROM `tab{WAITER}` WHERE parent=%s", sheet.name)
		frappe.db.sql(f"DELETE FROM `tab{PA}` WHERE name=%s", sheet.name)
		frappe.db.commit()
		self.assertGreaterEqual(held_sheet_seal.backstop()["closed"], 1)
		self.assertEqual(
			{(q.status, q.decision) for q in self.questions(sheet.name)},
			{("Dismissed", held_sheet_seal.CLOSED_NOTE)},
		)


# --------------------------------------------------------------------------- #
# Stop / archive / delete / re-run, and the central question closure
# --------------------------------------------------------------------------- #
class TestStopAndClosure(_SealBase):
	def assert_closed(self, sheet, status, *, reason=None):
		row = self.row(sheet)
		self.assertEqual((row.status, row.collecting, row.settled), (status, 0, 1))
		if reason:
			self.assertEqual(row.reason_code, reason)
		qs = self.questions(sheet)
		self.assertTrue(qs)
		self.assertEqual({(q.status, q.decision) for q in qs}, {("Dismissed", held_sheet_seal.CLOSED_NOTE)})
		self.assertEqual(json.loads(row.sheet_outcome), held_sheet_seal.EMPTY_OUTCOME)

	def test_stop_discards_a_collecting_or_sealed_sheet_without_a_resume(self):
		for make in (self.collecting, self.sealed):
			with self.subTest(make=make.__name__):
				conv, _rid, sheet = make(questions=1)
				with as_user(OWNER):
					pa.cancel_for_conversation(conv)
				self.assert_closed(sheet.name, "Discarded", reason="cancelled")
				self.assertEqual(frappe.db.count(WAITER, {"parent": sheet.name}), 0)
				self.assertEqual(self.resumes(), [])

	def test_archive_discards(self):
		conv, _rid, sheet = self.collecting(questions=1)
		with as_user(OWNER):
			pa.cancel_for_conversation(conv, reason="archived")
		self.assert_closed(sheet.name, "Discarded", reason="archived")

	def test_stop_through_the_endpoint(self):
		from jarvis.chat import api as chat_api

		conv, _rid, sheet = self.collecting(questions=1)
		with as_user(OWNER), patch.object(chat_api, "admission"):
			chat_api.stop_run(conv)
		clear_run_cancel(conv)
		self.assert_closed(sheet.name, "Discarded")

	def racing(self, conv, results):
		"""A write of the still-running turn right after each sweep commits."""
		real = pa.cancel_for_conversation

		def sweep(*a, **k):
			out = real(*a, **k)
			results.append(self.call("create_doc", _item(f"zz-fbs R{len(results)}"), conv))
			return out

		return patch.object(pa, "cancel_for_conversation", side_effect=sweep)

	def assert_all_refused(self, results):
		self.assertTrue(results)
		for res in results:
			self.assertFalse(res["ok"], res)
			self.assertEqual(res["error"]["code"], "FileBoxRefusedError")

	def test_a_write_racing_the_stop_opens_no_fresh_sheet(self):
		from jarvis.chat import api as chat_api
		from jarvis.chat import turn_handler

		for path in ("pump", "legacy"):
			with self.subTest(path=path):
				conv, rid, sheet = self.collecting(questions=1)
				if path == "legacy":
					self.set_turn(rid, state="dispatching")
				results = []
				try:
					with as_user(OWNER), patch.object(chat_api, "admission"), self.racing(conv, results):
						chat_api.stop_run(conv)
					self.assert_all_refused(results)
					self.assertEqual(self.live_sheets(conv), [])
					self.set_turn(rid, cancel_requested=1)
					if path == "pump":
						self.set_turn(rid, state="cancelled")
						self.finalize(rid, settlement.TERMINAL_EFFECTS)
					else:
						turn_handler._advance_macro(conv, errored=True, run_id=rid)
				finally:
					clear_run_cancel(conv)
				self.assertEqual(self.live_sheets(conv), [])
				self.assert_closed(sheet.name, "Discarded", reason="cancelled")
				self.assertEqual(self.notices(), 0)

	def test_a_write_racing_the_archive_opens_no_fresh_sheet(self):
		from jarvis.chat import api as chat_api

		conv, _rid, sheet = self.collecting(questions=1)
		results = []
		try:
			with as_user(OWNER), self.racing(conv, results):
				chat_api.archive_conversation(conv)
			self.assert_all_refused(results)
		finally:
			clear_run_cancel(conv)
		self.assertEqual(self.live_sheets(conv), [])
		self.assert_closed(sheet.name, "Discarded", reason="archived")

	def test_delete_discards_a_collecting_or_sealed_sheet(self):
		for make in (self.collecting, self.sealed):
			with self.subTest(make=make.__name__):
				conv, _rid, sheet = make(questions=1)
				pa.on_conversation_trash(frappe.get_doc(CONV, conv))
				frappe.db.commit()
				self.assert_closed(sheet.name, "Discarded")

	def test_a_rerun_supersedes(self):
		conv, _rid, sheet = self.sealed(questions=1)
		parents = filebox._supersede_held(conv)
		frappe.db.commit()
		self.assertEqual(parents, [sheet.name])
		pa.settle(sheet.name)
		self.assert_closed(sheet.name, "Superseded")

	def test_operator_fail_on_a_collecting_or_sealed_sheet(self):
		for make in (self.collecting, self.sealed):
			with self.subTest(make=make.__name__):
				_conv, _rid, sheet = make(questions=1)
				with as_user(SM):
					self.assertTrue(pa.operator_fail(sheet.name, reason="runbook")["ok"])
				stuck = make == self.collecting
				self.assert_closed(sheet.name, "Failed", reason="stuck" if stuck else "failed")
				message = held_sheet_seal.resume_message(self.row(sheet.name))
				self.assertEqual("stuck while it was still being listed" in message, stuck)
				self.assertEqual("could not be applied" in message, not stuck)
				self.assertEqual(len(self.resumes()), 1, "its run is told it needs attention")
				self.enqueued.clear()

	def test_operator_settle_closes_a_quarantined_sheet(self):
		_conv, _rid, sheet = self.sealed(questions=1)
		_store._terminal_update(sheet.name, ["Pending"], "Failed", reason_code="failed")
		frappe.db.sql(f"UPDATE `tab{PA}` SET settle_attempts=6 WHERE name=%s", sheet.name)
		frappe.db.commit()
		with patch.object(held_sheet_seal, "on_settled", side_effect=RuntimeError("down")), as_user(SM):
			self.assertEqual(pa.operator_settle(sheet.name), {"ok": True, "delivered": False})
		self.assert_closed(sheet.name, "Failed")

	def test_a_tampered_sheet_closes_its_questions_without_a_resume(self):
		conv, _rid, sheet = self.collecting(questions=1)
		frappe.db.sql(f"UPDATE `tab{PA}` SET sealed_call='x' WHERE name=%s", sheet.name)
		frappe.db.commit()
		res = self.call("create_doc", _item("zz-fbs I1"), conv)
		self.assertFalse(res["ok"])
		self.assert_closed(sheet.name, "Failed")
		self.assertEqual(self.resumes(), [], "the live run was told to stop already")

	def test_closure_runs_once_behind_the_settled_claim(self):
		_conv, _rid, sheet = self.sealed(questions=1)
		_store._terminal_update(sheet.name, ["Pending"], "Cancelled", reason_code="cancelled")
		frappe.db.commit()
		with patch.object(held_sheet_seal, "close_ended", wraps=held_sheet_seal.close_ended) as close:
			self.assertTrue(pa.settle(sheet.name))
			self.assertFalse(pa.settle(sheet.name))
			held_sheet_seal.on_settled(None, [{"name": sheet.name}])
		self.assertEqual(close.call_count, 1)
		self.assert_closed(sheet.name, "Cancelled")

	def test_generic_sweeps_leave_a_collecting_sheet_and_close_a_sealed_one(self):
		_c1, _r1, live = self.collecting(questions=1)
		_c2, _r2, sealed = self.sealed(questions=1)
		frappe.db.set_value("User", OWNER, "enabled", 0)
		frappe.db.commit()
		try:
			_reconcile._cancel_disabled_owners()
		finally:
			frappe.db.set_value("User", OWNER, "enabled", 1)
			frappe.db.commit()
		self.assertEqual(self.row(live.name).status, "Pending")
		self.assert_closed(sealed.name, "Cancelled", reason="owner_disabled")

	def test_the_rollback_runbooks_skip_a_collecting_sheet(self):
		_c1, _r1, live = self.collecting()
		_c2, _r2, sealed = self.sealed(questions=1)
		frappe.db.sql(f"UPDATE `tab{PA}` SET sealed_call='x' WHERE name IN %s", ((live.name, sealed.name),))
		frappe.db.commit()
		pa.cancel_unverifiable()
		self.assertEqual(self.row(live.name).status, "Pending")
		self.assert_closed(sealed.name, "Failed", reason="unverifiable")
		_c3, _r3, other = self.sealed(questions=1)
		pa.withdraw_all_held()
		self.assertEqual(self.row(live.name).status, "Pending")
		self.assert_closed(other.name, "Cancelled")


# --------------------------------------------------------------------------- #
# The kind checklist
# --------------------------------------------------------------------------- #
class TestKindChecklist(_SealBase):
	def test_authorize_lets_a_system_manager_act_and_refuses_a_collecting_sheet(self):
		from jarvis.chat.pending_actions._execute import _PROVENANCE

		self.assertEqual(_PROVENANCE[_store.SHEET], "approval")
		_c, _r, live = self.collecting()
		_c, _r, sealed = self.sealed()
		self.assertTrue(pa.authorize(sealed, SM, _store.SHEET)[0])
		self.assertTrue(pa.authorize(sealed, OWNER, _store.SHEET)[0])
		self.assertFalse(pa.authorize(sealed, OTHER, _store.SHEET)[0])
		self.assertFalse(pa.authorize(self.row(live.name), OWNER, _store.SHEET)[0])
		self.assertTrue(pa.authorize(self.row(live.name), OWNER, _store.SHEET, acting=False)[0])
		with as_user(OWNER):
			self.assertFalse(pa.discard(live.name, kind=_store.SHEET)["ok"])
		self.assertEqual(self.row(live.name).status, "Pending")

	def test_a_failed_sheet_resumes_its_run_once_with_a_sheet_message(self):
		conv, _rid, sheet = self.sealed()
		with as_user(SM):
			pa.operator_fail(sheet.name)
		self.assertEqual(frappe.db.get_value(WAITER, {"parent": sheet.name}, "resume_state"), "due")
		sent = []
		with patch.object(
			approvals_api,
			"_resume_conversation",
			side_effect=lambda c, m, **k: sent.append((c, m)) or {"ok": True},
		):
			self.assertEqual(held_writes.resume_waiters(sheet.name), 1)
			self.assertEqual(held_writes.resume_waiters(sheet.name), 0)
		[(to, message)] = sent
		self.assertEqual(to, conv)
		self.assertIn("approval sheet", message)
		self.assertIn("needs attention", message)


# --------------------------------------------------------------------------- #
# Board: lane, detail, candidates, badge
# --------------------------------------------------------------------------- #
class TestBoard(_SealBase):
	def lane(self, user=OWNER):
		with as_user(user):
			return [r for r in approvals_api.list_pending_actions_lane()["rows"] if r["kind"] == _store.SHEET]

	def detail(self, name, user=OWNER):
		with as_user(user):
			return approvals_api.get_pending_action(name)

	def assert_no_seal(self, payload):
		text = json.dumps(payload, default=str)
		for leak in ("sealed_call", "open_key", "dedup_keys", "gAAAA"):
			self.assertNotIn(leak, text)

	def test_the_lane_lists_sealed_and_collecting_sheets_from_plain_columns(self):
		_c, _r, live = self.collecting()
		_c, _r, sealed = self.sealed(questions=1)
		rows = {r["name"]: r for r in self.lane()}
		self.assertEqual(rows[live.name]["collecting"], 1)
		row = rows[sealed.name]
		self.assertEqual((row["collecting"], row["record_count"], row["question_count"]), (0, 2, 1))
		self.assertEqual(row["counts"], {"Supplier": 1, "Address": 1})
		self.assertEqual(row["counts_line"], "1 supplier · 1 address · 1 question")
		self.assert_no_seal(rows)
		self.assertNotIn(TAX, json.dumps(rows), "no record values in the lane")
		self.assertIn(sealed.name, {r["name"] for r in self.lane(SM)})
		self.assertEqual(self.lane(OTHER), [])
		self.assertEqual(rows[sealed.name]["for_user"], "")

	def test_a_lane_row_names_its_file_and_chat(self):
		conv, _r, sealed = self.sealed()
		file_name = f"fbs-{conv}.txt"
		[row] = self.lane()
		self.assertEqual(
			(row["file_name"], row["conversation_title"], row["conversation"]),
			(file_name, "File: fbs.pdf", conv),
		)
		# Unstamped: the conversation's earliest attachment (``filebox._source_file``).
		frappe.db.set_value(CONV, conv, "filebox_source_file", None, update_modified=False)
		frappe.db.commit()
		self.assertEqual(self.lane()[0]["file_name"], file_name)
		frappe.db.delete("File", {"attached_to_name": conv})
		frappe.db.commit()
		self.assertEqual(self.lane()[0]["file_name"], "")

	def test_the_detail_is_the_records_view_and_its_questions_never_the_seal(self):
		conv, _rid, sealed = self.sealed(questions=1)
		d = self.detail(sealed.name)
		self.assert_no_seal(d)
		self.assertEqual((d["kind"], d["can_act"], d["collecting"]), (_store.SHEET, 1, 0))
		self.assertEqual((d["record_count"], d["question_count"]), (2, 1))
		self.assertEqual(d["conversation"], conv, "the board links the chat")
		self.assertEqual(d["card_sha256"], _seal.card_sha256(self.row(sealed.name)))
		sup, addr = d["records"]
		self.assertEqual((sup["category"], sup["doctype"], sup["op"]), ("party", "Supplier", "create"))
		self.assertEqual(sup["values"]["supplier_name"], PARTY)
		self.assertIn("tax_id", sup["locked"])
		self.assertEqual(addr["depends_on"], [0])
		self.assertIn("links.1.link_name", addr["locked"])
		self.assertEqual((sup["needs_input"], sup["needs_fix"]), ([], None))
		[q] = d["questions"]
		self.assertEqual((q["question"], q["status"]), ("Which vendor?", "Pending"))
		self.assertIn("options", q)
		self.assertIn("routing", q)
		self.assertIn("context_md", q)
		self.assertEqual(
			self.detail(sealed.name, SM)["for_user"], frappe.db.get_value("User", OWNER, "full_name")
		)
		with self.assertRaises(frappe.DoesNotExistError):
			self.detail(sealed.name, OTHER)

	def test_a_questions_only_sheet_is_actionable(self):
		conv = self.conv()
		rid = self.turn(conv)
		self.assertTrue(self.call("create_doc", _question(), conv)["ok"])
		sheet = self.sheet(conv)
		self.set_turn(rid, state="done")
		self.assertEqual(held_sheet_seal.seal_turn(conv, rid), sheet.name)
		d = self.detail(sheet.name)
		self.assertEqual((d["can_act"], d["records"], d["record_count"], len(d["questions"])), (1, [], 0, 1))
		self.assertEqual(d["counts_line"], "1 question")

	def test_a_collecting_sheet_reads_but_cannot_be_acted_on(self):
		_c, _r, live = self.collecting()
		d = self.detail(live.name)
		self.assertEqual((d["collecting"], d["can_act"]), (1, 0))

	def test_needs_fix_and_needs_input_ride_their_record(self):
		_c, _r, sealed = self.sealed()
		fix = {"field": "address_line1", "label": "Address Line 1", "message": "Line 1 is too long"}
		for stored, shown in (
			("HSN required", {"field": None, "label": None, "message": "HSN required"}),  # a legacy entry
			(fix, fix),
			({**fix, "zz": "x"}, fix),
		):
			with self.subTest(stored=stored):
				frappe.db.sql(
					f"UPDATE `tab{PA}` SET needs_fix=%s WHERE name=%s",
					(json.dumps({"1": stored}), sealed.name),
				)
				frappe.db.commit()
				d = self.detail(sealed.name)
				self.assertEqual([r["needs_fix"] for r in d["records"]], [None, shown])

	def test_candidates_are_read_lazily_as_the_dropper(self):
		_c, _r, sealed = self.sealed()
		existing = frappe.get_doc(
			{"doctype": "Supplier", "supplier_name": f"{PARTY} Ltd", "tax_id": TAX}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		seen = []

		def _as(user):
			seen.append(user)
			return as_user(user)

		with as_user(SM), patch.object(approvals_api, "impersonate", side_effect=_as):
			out = approvals_api.get_sheet_candidates(sealed.name, json.dumps([0, 1, 99]))
		self.assertEqual(set(seen), {OWNER})
		self.assertIn(existing.name, [c["name"] for c in out["0"]])
		self.assert_no_seal(out)
		with as_user(OTHER), self.assertRaises(frappe.DoesNotExistError):
			approvals_api.get_sheet_candidates(sealed.name)

	def test_malformed_candidate_indexes_are_ignored_without_an_error_log(self):
		_c, _r, sealed = self.sealed()
		crashed = "jarvis.pending_action.candidates_crashed"
		frappe.db.delete("Error Log", {"method": crashed})
		with as_user(OWNER):
			for raw in ("[[1]]", '[{"i": 0}]', "[true]", '["0"]'):
				with self.subTest(raw=raw):
					self.assertEqual(approvals_api.get_sheet_candidates(sealed.name, raw), {})
		self.assertFalse(frappe.db.exists("Error Log", {"method": crashed}))

	def test_the_badge_leaves_the_sheet_clause_out_before_its_migrate(self):
		from jarvis.jarvis.doctype.jarvis_approval_request import jarvis_approval_request as ar

		self.sealed(questions=1)
		for user in (OWNER, SM):
			with self.subTest(user=user), as_user(user):
				now = approvals_api.pending_count()
				with patch.object(ar, "sheet_ready", return_value=False):
					self.assertEqual(approvals_api.pending_count(), now + 1, "no sheet column named")

	def test_the_badge_counts_a_sealed_sheet_once_and_never_its_questions(self):
		with as_user(OWNER):
			base = approvals_api.pending_count()
		_c, _r, live = self.collecting(questions=2)
		with as_user(OWNER):
			self.assertEqual(approvals_api.pending_count(), base, "collecting: not yet")
		self.set_turn(_r, state="done")
		held_sheet_seal.seal_turn(_c, _r)
		with as_user(OWNER):
			self.assertEqual(approvals_api.pending_count(), base + 1)


# --------------------------------------------------------------------------- #
# The File Box ladder
# --------------------------------------------------------------------------- #
class TestLadder(_SealBase):
	def row_of(self, conv):
		with as_user(OWNER):
			rows = filebox.list_inbound_page(page_length=100)["rows"]
		[row] = [r for r in rows if r["name"] == conv]
		return row

	def answered(self, conv):
		frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv,
				"seq": 80,
				"role": "assistant",
				"content": "Listed.",
				"streaming": 0,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def test_collecting_reads_processing_then_needs_approval_with_the_sheet_line(self):
		conv, rid, sheet = self.collecting(questions=1)
		self.assert_added(self.call("create_doc", _batch(_item("zz-fbs I1"), _item("zz-fbs I2")), conv))
		self.set_turn(rid, state="done")
		self.answered(conv)
		self.assertEqual(self.row_of(conv)["status"], "processing")
		held_sheet_seal.seal_turn(conv, rid)
		row = self.row_of(conv)
		self.assertEqual(row["status"], "needs_approval")
		self.assertEqual(row["pending_approvals"], 1, "its questions count through the sheet")
		self.assertEqual(row["result"], "Waiting for you: 1 supplier · 1 address · 2 items · 1 question")
		self.assertEqual(row["result_link"], f"/approvals?held={sheet.name}")

	def test_a_waiting_sheet_blocks_delete_and_its_resume_reads_processing(self):
		conv, _rid, sheet = self.sealed()
		self.answered(conv)
		with as_user(OWNER), self.assertRaisesRegex(frappe.ValidationError, "waiting on an approval"):
			filebox.delete_inbound(conv)
		with as_user(SM):
			pa.operator_fail(sheet.name)
		self.assertEqual(self.row_of(conv)["status"], "processing", "its resume is on its way")
		frappe.db.sql(f"UPDATE `tab{WAITER}` SET resume_state='done' WHERE parent=%s", sheet.name)
		frappe.db.commit()
		self.assertNotEqual(self.row_of(conv)["status"], "processing")

	def test_an_executing_sheet_reads_applying(self):
		conv, _rid, sheet = self.sealed()
		frappe.db.sql(f"UPDATE `tab{PA}` SET status='Executing' WHERE name=%s", sheet.name)
		frappe.db.commit()
		self.assertEqual(self.row_of(conv)["status"], "applying")
		self.assertIn("applying", filebox._STATUSES)

	def test_clear_processed_never_deletes_a_row_with_a_collecting_sheet(self):
		conv, rid, _sheet = self.collecting()
		self.set_turn(rid, state="done")
		self.answered(conv)
		with as_user(OWNER):
			filebox.clear_processed_inbound()
		self.assertTrue(frappe.db.exists(CONV, conv))


# --------------------------------------------------------------------------- #
# INBOUND_PROMPT step 4
# --------------------------------------------------------------------------- #
class TestPrompt(_SealBase):
	def test_the_switch_picks_the_sheet_wording_and_off_is_the_legacy_text(self):
		on = filebox.build_inbound_prompt()
		self.assertIn("keep going: propose EVERY missing master for this document (batches of up to 20)", on)
		self.assertIn("file every question as a Jarvis Approval Request in this same turn", on)
		self.assertIn("END the turn once nothing else is missing", on)
		self.assertNotIn("END the turn right away", on)
		self.assertNotIn("create ALL of them in ONE create_doc batch", on)
		self.assertIn("5. Extract the document fully", on)
		with patch.object(held_sheets, "enabled", return_value=False):
			self.assertEqual(filebox.build_inbound_prompt(), filebox.INBOUND_PROMPT)
			self.assertIn("create ALL of them in ONE create_doc batch", filebox.build_inbound_prompt())
