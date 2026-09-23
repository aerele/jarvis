"""Jarvis Pending Action lifecycle (PR-2a): settle, the reconciler, purge, discard,
Stop/archive, conversation delete, operator levers and the single terminal writer
(§4.5 / §4.6; AC-U1 / U11)."""

from __future__ import annotations

import ast
import os
import re
from unittest.mock import patch

import frappe
from cryptography.fernet import Fernet
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import pending_actions as pa
from jarvis.chat.pending_actions import _reconcile, _settle
from jarvis.tests._pending_action_helpers import (
	AGENT_WRITE,
	CONV,
	DEFAULT_ARGS,
	MSG,
	OTHER,
	OWNER,
	PA,
	SM_USER,
	WAITER,
	PendingActionTestMixin,
	as_user,
	fake_dispatch,
)

_CONTINUATION_PATH = "jarvis.tests.test_pending_action_lifecycle._continuation"
_SEEN: list = []


def _continuation(conversation, items):
	"""A registry continuation: claims settlement in its own transaction (D5)."""
	won = pa.claim_settled([i["name"] for i in items])
	_SEEN.append((conversation, [i for i in items if i["name"] in won]))


def _lazy_continuation(conversation, items):
	_SEEN.append((conversation, items))  # forgets the CAS: nothing may be marked settled


def _ago(**kw):
	return frappe.utils.add_to_date(frappe.utils.now_datetime(), **{k: -v for k, v in kw.items()})


class _Base(PendingActionTestMixin, FrappeTestCase):
	def setUp(self):
		super().setUp()
		_SEEN.clear()

	def run_now(self, name, **kw):
		with fake_dispatch(), as_user(OWNER):
			return pa.execute(name, **kw)

	def display_row(self, conv):
		"""The PR-3a-style pending chat row, inserted in park's transaction."""

		def insert(doc):
			msg = frappe.get_doc(
				{
					"doctype": MSG,
					"conversation": conv,
					"seq": 1,
					"role": "tool",
					"tool_name": doc.tool,
					"tool_status": "pending",
					"tool_call_id": doc.name,
				}
			)
			msg.flags.jarvis_server_write = True
			msg.insert(ignore_permissions=True)

		return insert

	def receipts(self, conv, name):
		return frappe.get_all(
			MSG, filters={"conversation": conv, "tool_call_id": name}, pluck="action_outcome"
		)


class TestOutcomeTable(FrappeTestCase):
	def test_every_end_state_has_its_chip(self):
		cases = [
			("Executed", "", "update_doc", "confirmed"),
			("Failed", "failed", "update_doc", "failed"),
			("Failed", "failed", "call_connector", "unknown"),
			("Failed", "failed", "run_method", "unknown"),
			("Failed", "stale", "run_method", "failed"),
			("Failed", "target_missing", "update_doc", "failed"),
			("Failed", "tampered", "call_connector", "failed"),
			("Failed", "unverifiable", "update_doc", "failed"),
			("Failed", "interrupted", "update_doc", "unknown"),
			("Failed", "partial", "run_import", "partial"),
			("Discarded", "discarded", "update_doc", "discarded"),
			("Superseded", "superseded", "update_doc", "superseded"),
			("Cancelled", "cancelled", "update_doc", "cancelled"),
			("Cancelled", "archived", "update_doc", "cancelled"),
			("Cancelled", "owner_disabled", "update_doc", "cancelled"),
			("Cancelled", "expired", "update_doc", "expired"),
		]
		for status, code, tool, chip in cases:
			self.assertEqual(pa.outcome_for(status, code, tool), chip, (status, code, tool))


class TestSettle(_Base):
	def test_idempotent_receipt_flip_and_seals_dropped(self):
		conv = self.make_conv()
		name = self.park(conv, display_row=self.display_row(conv))
		out = self.run_now(name, defer_continuation=True)
		self.assertTrue(out["ok"])
		row = self.row(name)
		self.assertEqual((row.status, row.settled), ("Executed", 0))
		self.assertTrue(row.sealed_settlement)
		self.assertEqual(self.receipts(conv, name), [""], "unsettled: the display row is still pending")
		self.assertTrue(pa.settle(name))
		self.assertFalse(pa.settle(name))
		self.assertEqual(self.receipts(conv, name), ["confirmed"], "flipped in place, exactly one row")
		row = self.row(name)
		self.assertEqual((row.settled, row.sealed_call, row.sealed_settlement), (1, None, None))

	def test_real_outcome_overwrites_a_sweeps_cancelled_chip(self):
		conv = self.make_conv()
		name = self.park(conv, display_row=self.display_row(conv))
		frappe.db.sql(
			f"UPDATE `tab{MSG}` SET tool_status='', action_outcome='cancelled' WHERE tool_call_id=%(n)s",
			{"n": name},
		)
		frappe.db.commit()
		self.run_now(name)
		self.assertEqual(self.receipts(conv, name), ["confirmed"])

	def test_registered_continuation_gets_items_and_owns_the_cas(self):
		conv = self.make_conv()
		name = self.park(conv)
		with patch.dict(_settle.CONTINUATIONS, {"chat": _CONTINUATION_PATH}):
			self.run_now(name)
		self.assertEqual(len(_SEEN), 1)
		seen_conv, items = _SEEN[0]
		self.assertEqual(seen_conv, conv)
		item = items[0]
		self.assertEqual((item["name"], item["outcome"], item["status"]), (name, "confirmed", "Executed"))
		self.assertEqual(item["args"], DEFAULT_ARGS)
		self.assertEqual(item["result"], {"ok": True, "data": {"name": "RESULT-1"}})
		self.assertEqual(self.row(name).settled, 1)

	def test_continuation_that_skips_the_cas_leaves_the_row_unsettled(self):
		name = self.park(self.make_conv())
		with patch.dict(
			_settle.CONTINUATIONS, {"chat": "jarvis.tests.test_pending_action_lifecycle._lazy_continuation"}
		):
			self.run_now(name)
		row = self.row(name)
		self.assertEqual(row.settled, 0)
		self.assertTrue(row.sealed_call, "seals are dropped only once settled")

	def test_missing_conversation_settles_without_a_receipt(self):
		conv = self.make_conv()
		name = self.park(conv)
		self.run_now(name, defer_continuation=True)
		frappe.db.delete(CONV, {"name": conv})
		frappe.db.commit()
		self.assertTrue(pa.settle(name))
		self.assertEqual(frappe.db.count(MSG, {"tool_call_id": name}), 0)

	def test_quarantine_after_five_failed_settles_then_operator_settle(self):
		conv = self.make_conv()
		name = self.park(conv)
		self.run_now(name, defer_continuation=True)
		with patch("jarvis.api.persist_tool_receipt", side_effect=RuntimeError("receipt down")):
			for _ in range(7):
				self.assertFalse(pa.settle(name))
		self.assertEqual(self.row(name).settle_attempts, 6)
		self.assertEqual(len(self.logged("settle_quarantined", name)), 1)
		self.assertEqual(self.row(name).settled, 0)
		self.assertFalse(pa.settle(name), "quarantined rows are left to an operator")
		with as_user(SM_USER):
			out = pa.operator_settle(name)
		self.assertEqual(out, {"ok": True, "delivered": True})
		self.assertEqual(self.receipts(conv, name), ["confirmed"])
		self.assertEqual(len(self.logged("operator_settle", name)), 1)

	def test_batch_settles_with_one_continuation(self):
		conv = self.make_conv()
		first = self.park(conv)
		second = self.park(None)  # conversation-less; adopts conv on confirm
		with fake_dispatch(), as_user(OWNER):
			pa.execute(first, defer_continuation=True, batch_id="batch-1")
			pa.execute(second, conversation=conv, defer_continuation=True, batch_id="batch-1")
		with patch.dict(_settle.CONTINUATIONS, {"chat": _CONTINUATION_PATH}):
			self.assertEqual(pa.settle_batch("batch-1"), 2)
		self.assertEqual(len(_SEEN), 1)
		self.assertEqual(sorted(i["name"] for i in _SEEN[0][1]), sorted([first, second]))
		self.assertEqual(self.receipts(conv, first) + self.receipts(conv, second), ["confirmed", "confirmed"])


class TestReconcile(_Base):
	def test_interrupted_cutoff_is_ten_minutes(self):
		fresh, stale = self.park(self.make_conv()), self.park(self.make_conv())
		self.set_col(fresh, status="Executing", executing_at=_ago(minutes=9))
		self.set_col(stale, status="Executing", executing_at=_ago(minutes=11))
		pa.reconcile()
		self.assertEqual(self.row(fresh).status, "Executing")
		self.assertEqual((self.row(stale).status, self.row(stale).reason_code), ("Failed", "interrupted"))
		self.assertEqual(len(self.logged("interrupted", stale)), 1)
		self.assertEqual(self.logged("interrupted", fresh), [])

	def test_unsettled_cutoff_is_two_minutes(self):
		fresh, lost = self.park(self.make_conv()), self.park(self.make_conv())
		for name in (fresh, lost):
			self.run_now(name, defer_continuation=True)
		self.set_col(fresh, modified=_ago(minutes=1))
		self.set_col(lost, modified=_ago(minutes=3))
		pa.reconcile()
		self.assertEqual((self.row(fresh).settled, self.row(lost).settled), (0, 1))

	def test_unsettled_cutoff_keys_on_the_terminal_write_not_the_claim(self):
		# A deferred batch row claimed long ago but terminal just now: its batch still runs.
		name = self.park(self.make_conv())
		self.run_now(name, defer_continuation=True, batch_id="batch-slow")
		self.set_col(name, decided_at=_ago(minutes=5), executing_at=_ago(minutes=5))
		pa.reconcile()
		self.assertEqual(self.row(name).settled, 0)
		self.set_col(name, modified=_ago(minutes=3))
		pa.reconcile()
		self.assertEqual(self.row(name).settled, 1)

	def test_unsettled_batch_is_settled_as_a_group(self):
		conv = self.make_conv()
		first, second = self.park(conv), self.park(None)
		with fake_dispatch(), as_user(OWNER):
			pa.execute(first, defer_continuation=True, batch_id="batch-r")
			pa.execute(second, conversation=conv, defer_continuation=True, batch_id="batch-r")
		self.set_col(first, modified=_ago(minutes=5))
		self.set_col(second, modified=_ago(minutes=5))
		with patch.dict(_settle.CONTINUATIONS, {"chat": _CONTINUATION_PATH}):
			pa.reconcile()
		self.assertEqual(len(_SEEN), 1)
		self.assertEqual(len(_SEEN[0][1]), 2)

	def test_a_batch_still_running_a_card_waits_for_it(self):
		"""C4: settling a batch's decided rows while another still runs would split its
		one continuation in two."""
		conv = self.make_conv()
		done, running = self.park(conv), self.park(None)
		with fake_dispatch(), as_user(OWNER):
			pa.execute(done, defer_continuation=True, batch_id="batch-c4")
		self.set_col(done, modified=_ago(minutes=5))
		self.set_col(
			running,
			status="Executing",
			batch_id="batch-c4",
			conversation=conv,
			executing_at=frappe.utils.now_datetime(),
		)
		with patch.dict(_settle.CONTINUATIONS, {"chat": _CONTINUATION_PATH}):
			_reconcile._settle_unsettled()
			self.assertEqual((_SEEN, self.row(done).settled), ([], 0))
			self.set_col(running, status="Executed", modified=_ago(minutes=5))
			_reconcile._settle_unsettled()
		self.assertEqual(len(_SEEN), 1)
		self.assertEqual(len(_SEEN[0][1]), 2)

	def test_a_reaped_batch_card_settles_with_its_batch(self):
		"""C2-2: a typed batch whose worker died mid-card still yields ONE continuation."""
		conv = self.make_conv()
		done, crashed, lone = self.park(conv), self.park(None), self.park(self.make_conv())
		with fake_dispatch(), as_user(OWNER):
			pa.execute(done, defer_continuation=True, batch_id="batch-reap")
		self.set_col(
			crashed,
			status="Executing",
			batch_id="batch-reap",
			conversation=conv,
			executing_at=_ago(minutes=11),
		)
		self.set_col(lone, status="Executing", executing_at=_ago(minutes=11))
		with patch.dict(_settle.CONTINUATIONS, {"chat": _CONTINUATION_PATH}):
			self.assertEqual(_reconcile._reap_interrupted(), 2)
			for name in (done, crashed):
				self.set_col(name, modified=_ago(minutes=5))
			_reconcile._settle_unsettled()
		batch = [sorted(i["name"] for i in items) for c, items in _SEEN if c == conv]
		self.assertEqual(batch, [sorted([done, crashed])])
		self.assertEqual(len(_SEEN), 2)
		self.assertEqual(self.row(crashed).settled, 1)

	def test_disabled_or_missing_owner_cancels_pending_rows(self):
		disabled = self.park(self.make_conv(OTHER), owner=OTHER)
		ghost = self.park(None, owner=OWNER)
		self.set_col(ghost, owner_user="pa-ghost@example.com")
		frappe.db.set_value("User", OTHER, "enabled", 0)
		frappe.db.commit()
		try:
			pa.reconcile()
			for name in (disabled, ghost):
				self.assertEqual(
					(self.row(name).status, self.row(name).reason_code), ("Cancelled", "owner_disabled")
				)
				self.assertTrue(self.logged("owner_disabled", name))
		finally:
			frappe.db.set_value("User", OTHER, "enabled", 1)
			frappe.db.sql(f"DELETE FROM `tab{PA}` WHERE name=%(n)s", {"n": ghost})
			frappe.db.commit()

	def test_pending_chat_cards_of_a_deleted_conversation_are_cancelled(self):
		conv = self.make_conv()
		orphan, loose = self.park(conv), self.park(None)
		held = self.park(conv, kind="file_box_held")
		frappe.db.delete(CONV, {"name": conv})  # a raw delete skips on_trash
		frappe.db.commit()
		self.assertGreaterEqual(_reconcile._cancel_orphaned_chat(), 1)
		row = self.row(orphan)
		self.assertEqual((row.status, row.reason_code, row.settled), ("Cancelled", "cancelled", 1))
		self.assertTrue(self.logged("orphan_cancel", orphan))
		self.assertEqual(self.row(loose).status, "Pending", "a conversation-less card is not an orphan")
		self.assertEqual(self.row(held).status, "Pending", "held rows are the waiters' business")
		self.assertIn("cancel_orphaned_chat", pa.reconcile())

	def test_waiters_fail_past_the_budget_and_the_rest_retry_through_the_hook(self):
		exhausted = self.park(self.make_conv(), kind="file_box_held")
		retry = self.park(self.make_conv(), kind="file_box_held")
		frappe.db.sql(
			f"UPDATE `tab{WAITER}` SET resume_state='due', resume_attempts=5 WHERE parent=%(p)s",
			{"p": exhausted},
		)
		frappe.db.sql(
			f"UPDATE `tab{WAITER}` SET resume_state='due', resume_attempts=1, modified=%(m)s WHERE parent=%(p)s",
			{"p": retry, "m": _ago(minutes=5)},
		)
		frappe.db.commit()
		resumed = []
		with (
			patch.object(_reconcile, "HELD_RESUME", "jarvis.tests.test_pending_action_lifecycle._resume"),
			patch(
				"jarvis.tests.test_pending_action_lifecycle._resume", side_effect=resumed.append, create=True
			),
		):
			out = pa.reconcile()
		self.assertEqual(frappe.db.get_value(WAITER, {"parent": exhausted}, "resume_state"), "failed")
		self.assertEqual(resumed, [retry])
		self.assertEqual(out["retry_waiters"], {"failed": 1, "retried": 1})

	def test_health_signals(self):
		frappe.db.delete("Error Log", {"method": "jarvis.pending_action.cards_aged"})
		old = self.park(self.make_conv())
		self.set_col(old, creation=_ago(days=31))
		self.park(self.make_conv())
		self.assertEqual(_reconcile._warn_old_pending(), 1)
		with patch.object(_reconcile, "CARD_AGE_ALERT", 0):
			self.assertEqual(_reconcile._cards_health(), {"cards_open": 2, "aged": 1})
			_reconcile._cards_health()
		self.assertEqual(len(self.logged("cards_aged")), 1)
		frappe.db.delete("Error Log", {"method": "jarvis.pending_action.cards_aged"})
		frappe.db.commit()

	def test_a_failing_step_never_blocks_the_rest(self):
		def _reap_interrupted():
			raise RuntimeError("step down")

		orphan = self.park(None)
		self.set_col(orphan, owner_user="pa-ghost@example.com")
		try:
			with patch.object(_reconcile, "_reap_interrupted", _reap_interrupted):
				out = _reconcile.reconcile()
			self.assertNotIn("reap_interrupted", out)
			self.assertEqual(out["cancel_disabled_owners"], 1)
			self.assertEqual(self.row(orphan).status, "Cancelled")
			self.assertEqual(len(self.logged("reconcile_failed", "step down")), 1)
		finally:
			frappe.db.sql(f"DELETE FROM `tab{PA}` WHERE name=%(n)s", {"n": orphan})
			frappe.db.commit()


class TestPurge(_Base):
	def _terminal(self, *, settled_days=None, status="Executed", settled=1, kind="chat"):
		name = self.park(self.make_conv(), kind=kind)
		cols = {"status": status, "settled": settled}
		if settled_days is not None:
			cols["settled_at"] = _ago(days=settled_days)
		self.set_col(name, **cols)
		return name

	def test_purge_respects_d3(self):
		old = self._terminal(settled_days=8)
		recent = self._terminal(settled_days=6)
		unsettled = self._terminal(settled=0, status="Failed")
		pending = self.park(self.make_conv())
		self.set_col(pending, creation=_ago(days=30))
		executing = self._terminal(status="Executing", settled=0)
		held_due = self._terminal(settled_days=8, kind="file_box_held")
		held_done = self._terminal(settled_days=8, kind="file_box_held")
		frappe.db.sql(f"UPDATE `tab{WAITER}` SET resume_state='due' WHERE parent=%(p)s", {"p": held_due})
		frappe.db.sql(f"UPDATE `tab{WAITER}` SET resume_state='done' WHERE parent=%(p)s", {"p": held_done})
		frappe.db.commit()
		self.assertEqual(pa.purge(), 2)
		gone = {old, held_done}
		for name in (old, recent, unsettled, pending, executing, held_due, held_done):
			self.assertEqual(frappe.db.exists(PA, name) is None, name in gone, name)
		self.assertEqual(frappe.db.count(WAITER, {"parent": held_done}), 0, "children go with the parent")
		self.assertEqual(frappe.db.count(WAITER, {"parent": held_due}), 1)


class TestDiscard(_Base):
	def test_owner_discards_with_audit_chip_and_autorun_cleared(self):
		conv = self.make_conv()
		name = self.park(
			conv, tool="update_doc", args={"doctype": "ToDo", "name": self.make_todo(), "changes": {}}
		)
		frappe.db.set_value(CONV, conv, {"skill_autorun": 1, "request_autorun": 1}, update_modified=False)
		frappe.db.commit()
		with as_user(OWNER):
			out = pa.discard(name)
		self.assertEqual((out["ok"], out["reason_code"]), (True, "discarded"))
		row = self.row(name)
		self.assertEqual((row.status, row.decided_by, row.settled), ("Discarded", OWNER, 1))
		self.assertEqual(self.receipts(conv, name), ["discarded"])
		self.assertEqual(
			frappe.get_all(AGENT_WRITE, filters={"actor": OWNER}, pluck="outcome"), ["discarded"]
		)
		flags = frappe.db.get_value(CONV, conv, ["skill_autorun", "request_autorun"], as_dict=True)
		self.assertEqual((flags.skill_autorun, flags.request_autorun), (0, 0))
		with as_user(OWNER):
			self.assertEqual(pa.discard(name)["reason_code"], "already_handled")

	def test_discarding_an_executing_row_is_refused(self):
		name = self.park(self.make_conv())
		self.set_col(name, status="Executing")
		with as_user(OWNER):
			out = pa.discard(name)
		self.assertEqual((out["ok"], out["reason_code"], out["pa_status"]), (False, "executing", "Executing"))
		self.assertEqual(self.row(name).status, "Executing")

	def test_non_owner_and_nested_discard_are_refused(self):
		name = self.park(self.make_conv())
		for user in (OTHER, SM_USER):
			with as_user(user):
				self.assertEqual(pa.discard(name)["reason_code"], "not_found")
		frappe.local.jarvis_dispatch_depth = 1
		try:
			with as_user(OWNER), self.assertRaises(frappe.PermissionError):
				pa.discard(name)
		finally:
			frappe.local.jarvis_dispatch_depth = 0
		self.assertEqual(self.row(name).status, "Pending")

	def test_system_manager_may_skip_a_held_row(self):
		name = self.park(self.make_conv(), kind="file_box_held")
		with as_user(SM_USER):
			self.assertEqual(pa.discard(name, kind="file_box_held")["reason_code"], "discarded")
		self.assertEqual(self.row(name).status, "Discarded")
		audit = frappe.get_all(
			AGENT_WRITE, filters={"actor": SM_USER}, fields=["provenance", "provenance_name"]
		)
		self.assertEqual([(a.provenance, a.provenance_name) for a in audit], [("approval", name)])


class TestCancelForConversation(_Base):
	def test_stop_cancels_pending_chat_only_and_drops_held_waiters(self):
		conv, other = self.make_conv(), self.make_conv()
		chat = self.park(conv)
		executing = self.park(None)
		self.set_col(executing, status="Executing", conversation=conv)
		shared = self.park(conv, kind="file_box_held", waiters=[conv, other])
		solo = self.park(conv, kind="file_box_held", dedup_key="solo")
		cancelled = pa.cancel_for_conversation(conv)
		self.assertEqual(cancelled, [chat])
		self.assertEqual((self.row(chat).status, self.row(chat).reason_code), ("Cancelled", "cancelled"))
		self.assertEqual(self.receipts(conv, chat), ["cancelled"])
		self.assertEqual(self.row(executing).status, "Executing", "Stop never retires an executing card")
		self.assertEqual(self.row(shared).status, "Pending")
		self.assertEqual(self.row(shared).conversation, other, "the next waiter is promoted")
		waiters = frappe.get_all(WAITER, filters={"parent": shared}, fields=["conversation", "role"])
		self.assertEqual([(w.conversation, w.role) for w in waiters], [(other, "primary")])
		solo_row = self.row(solo)
		self.assertEqual(
			(solo_row.status, solo_row.open_key, solo_row.conversation), ("Cancelled", None, None)
		)

	def test_archive_cancels_with_the_archived_code(self):
		from jarvis.chat import api as chat_api

		conv = self.make_conv()
		name = self.park(conv)
		with as_user(OWNER):
			chat_api.archive_conversation(conv)
		self.assertEqual((self.row(name).status, self.row(name).reason_code), ("Cancelled", "archived"))
		self.assertEqual(self.receipts(conv, name), ["cancelled"])


class TestConversationDelete(_Base):
	def test_delete_cleans_up_every_kind(self):
		conv, other = self.make_conv(), self.make_conv()
		chat = self.park(conv)
		done = self.park(None)
		self.set_col(done, status="Executed", conversation=conv)
		executing = self.park(None)
		self.set_col(executing, status="Executing", conversation=conv)
		shared = self.park(conv, kind="file_box_held", waiters=[conv, other])
		solo = self.park(conv, kind="file_box_held")
		solo_done = self.park(conv, kind="file_box_held")
		self.set_col(solo_done, status="Executed")
		frappe.delete_doc(CONV, conv, ignore_permissions=True)
		frappe.db.commit()
		self.assertFalse(frappe.db.exists(PA, chat))
		self.assertFalse(frappe.db.exists(PA, done))
		self.assertEqual(self.row(executing).status, "Executing", "its final write copes with the gap")
		self.assertEqual(self.row(shared).conversation, other)
		self.assertEqual((self.row(solo).status, self.row(solo).settled), ("Cancelled", 1))
		self.assertEqual((self.row(solo_done).settled, self.row(solo_done).sealed_call), (1, None))


class TestOperatorLevers(_Base):
	def test_operator_fail_is_system_manager_only_and_truthful(self):
		pending, executing = self.park(self.make_conv()), self.park(self.make_conv())
		self.set_col(executing, status="Executing")
		with as_user(OTHER), self.assertRaises(frappe.PermissionError):
			pa.operator_fail(pending, reason="stuck")
		with as_user(SM_USER):
			self.assertEqual(pa.operator_fail(pending, reason="PA-OPERATOR-NOTE")["reason_code"], "failed")
			self.assertEqual(pa.operator_fail(executing)["reason_code"], "interrupted")
			self.assertEqual(pa.operator_fail(pending)["reason_code"], "already_handled")
		self.assertEqual(pa.outcome_for("Failed", self.row(executing).reason_code, "update_doc"), "unknown")
		self.assertEqual(self.row(pending).reason, "Failed by an operator.")
		logs = self.logged("operator_fail", pending)
		self.assertEqual(len(logs), 1)
		self.assertIn(SM_USER, logs[0])

	def test_operator_settle_refuses_non_system_managers(self):
		name = self.park(self.make_conv())
		with as_user(OTHER), self.assertRaises(frappe.PermissionError):
			pa.operator_settle(name)


class TestRollbackTools(_Base):
	def test_cancel_all_chat_withdraw_all_held(self):
		chat = self.park(self.make_conv())
		held = self.park(self.make_conv(), kind="file_box_held")
		self.assertGreaterEqual(pa.cancel_all_chat_pending(), 1)
		self.assertEqual((self.row(chat).status, self.row(held).status), ("Cancelled", "Pending"))
		self.assertGreaterEqual(pa.withdraw_all_held(), 1)
		self.assertEqual(self.row(held).status, "Cancelled")

	def test_cancel_unverifiable_fails_only_rows_that_no_longer_open(self):
		with patch(
			"jarvis.chat.pending_actions._seal.get_encryption_key",
			return_value=Fernet.generate_key().decode(),
		):
			lost = self.park(self.make_conv())
		kept = self.park(self.make_conv())
		self.assertEqual(pa.cancel_unverifiable(), 1)
		self.assertEqual((self.row(lost).status, self.row(lost).reason_code), ("Failed", "unverifiable"))
		self.assertEqual(self.row(kept).status, "Pending")


_PA_NAMES = frozenset({"Jarvis Pending Action", "tabJarvis Pending Action"})
_SET_STATUS = re.compile(
	r"UPDATE\s+`tab(?:Jarvis Pending Action|\{PA\})`\s+SET\b(.*?)(?:\bWHERE\b|$)", re.S | re.I
)


def _pa_aliases(tree) -> set[str]:
	"""Names bound to the doctype: ``PA``, ``X = "Jarvis Pending Action"``, ``import PA as X``."""
	names = {"PA"}
	for node in ast.walk(tree):
		if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
			if node.value.value in _PA_NAMES:
				names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
		elif isinstance(node, ast.ImportFrom):
			names |= {a.asname for a in node.names if a.name == "PA" and a.asname}
	return names


def _sql_text(node, aliases) -> str | None:
	"""A string expression as SQL text: literals, f-strings (the doctype alias reads
	``{PA}``) and ``+`` chains; any other operand reads ``{}``."""
	if isinstance(node, ast.Constant) and isinstance(node.value, str):
		return node.value
	if isinstance(node, ast.JoinedStr):
		parts = []
		for v in node.values:
			if isinstance(v, ast.Constant):
				parts.append(str(v.value))
			else:
				is_pa = isinstance(v.value, ast.Name) and v.value.id in aliases
				parts.append("{PA}" if is_pa else "{}")
		return "".join(parts)
	if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
		left, right = _sql_text(node.left, aliases), _sql_text(node.right, aliases)
		if left is None and right is None:
			return None
		return (left or "{}") + (right or "{}")
	return None


def _names_pa(node, aliases, bound=frozenset()) -> bool:
	"""``node`` mentions the doctype (a literal, an alias, or a local bound to one)."""
	for sub in ast.walk(node):
		if isinstance(sub, ast.Constant) and sub.value in _PA_NAMES:
			return True
		if isinstance(sub, ast.Name) and (sub.id in aliases or sub.id in bound):
			return True
	return False


def _is_status(node) -> bool:
	if isinstance(node, ast.Constant):
		return node.value == "status"
	if isinstance(node, ast.Attribute):
		return node.attr == "status"
	if isinstance(node, ast.Dict):
		return any(_is_status(k) for k in node.keys if k is not None)
	return False


def _qb_update_target(call):
	"""The ``qb.update(...)`` call at the root of a ``.set(...)`` chain, if any."""
	node = call.func.value if isinstance(call.func, ast.Attribute) else None
	while isinstance(node, (ast.Call, ast.Attribute)):
		if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update":
			owner = node.func.value
			if (isinstance(owner, ast.Attribute) and owner.attr == "qb") or (
				isinstance(owner, ast.Name) and owner.id == "qb"
			):
				return node
		node = node.func if isinstance(node, ast.Call) else node.value
	return None


def status_writers(source: str) -> set[str]:
	"""Functions in ``source`` that write the PA ``status`` column: raw UPDATE SQL
	(literal, joined, f-string ``tab{PA}``), ``qb.update(PA).set(status)`` or
	``set_value(PA, ..., status)``."""
	tree = ast.parse(source)
	aliases = _pa_aliases(tree)
	found = set()
	for fn in ast.walk(tree):
		if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
			continue
		bound = {
			t.id
			for n in ast.walk(fn)
			if isinstance(n, ast.Assign) and _names_pa(n.value, aliases)
			for t in n.targets
			if isinstance(t, ast.Name)
		}
		for node in ast.walk(fn):
			text = _sql_text(node, aliases)
			if text and any(re.search(r"\bstatus`?\s*=", m) for m in _SET_STATUS.findall(text)):
				found.add(fn.name)
			if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
				continue
			if node.func.attr == "set" and node.args and _is_status(node.args[0]):
				target = _qb_update_target(node)
				if target and _names_pa(target, aliases, bound):
					found.add(fn.name)
			if node.func.attr == "set_value" and node.args and _names_pa(node.args[0], aliases, bound):
				if any(_is_status(a) for a in node.args[1:]) or any(
					_is_status(k.value) for k in node.keywords
				):
					found.add(fn.name)
	return found


class TestTerminalWriter(_Base):
	_ROOT = os.path.dirname(os.path.dirname(__file__))
	_ALLOWED = {
		("chat/pending_actions/_store.py", "_terminal_update"),
		("chat/pending_actions/_store.py", "claim"),
	}

	def test_only_terminal_update_and_claim_write_status(self):
		writers = set()
		for dirpath, dirs, files in os.walk(self._ROOT):
			dirs[:] = [d for d in dirs if d not in ("tests", "node_modules", "__pycache__")]
			for fname in files:
				if not fname.endswith(".py"):
					continue
				path = os.path.join(dirpath, fname)
				with open(path, encoding="utf-8") as fh:
					source = fh.read()
				rel = os.path.relpath(path, self._ROOT)
				writers |= {(rel, fn) for fn in status_writers(source)}
		self.assertEqual(writers, self._ALLOWED)

	def test_the_scan_sees_every_writer_form(self):
		caught = {
			"literal": 'def w():\n\tfrappe.db.sql("UPDATE `tabJarvis Pending Action` SET status=%s", x)',
			"joined": (
				'def w():\n\tfrappe.db.sql("UPDATE `tabJarvis Pending Action` SET reason=%s,"\n'
				"\t\t\" status='Failed' WHERE name=%s\", x)"
			),
			"concat": 'def w():\n\tq = "UPDATE `tabJarvis Pending Action` SET " + col + ", status=%s"',
			"fstring": "from x import PA\ndef w():\n\tfrappe.db.sql(f\"UPDATE `tab{PA}` SET status='Failed'\")",
			"alias": (
				'DT = "Jarvis Pending Action"\n'
				"def w():\n\tfrappe.db.sql(f\"UPDATE `tab{DT}` SET `status`='Failed'\")"
			),
			"qb": (
				"def w():\n\tt = frappe.qb.DocType(PA)\n"
				"\tfrappe.qb.update(t).set(t.status, 'Failed').where(t.name == n).run()"
			),
			"qb_literal": (
				"def w():\n\tfrappe.qb.update(frappe.qb.DocType('Jarvis Pending Action'))"
				".set('status', 'Failed').run()"
			),
			"set_value": 'def w():\n\tfrappe.db.set_value(PA, n, "status", "Failed")',
			"set_value_dict": 'def w():\n\tfrappe.db.set_value("Jarvis Pending Action", n, {"status": "x"})',
		}
		for label, source in caught.items():
			self.assertEqual(status_writers(source), {"w"}, label)
		ignored = (
			"def w():\n\tfrappe.db.sql(\"UPDATE `tabJarvis Pending Action` SET reason=%s WHERE status='Executing'\")",
			'def w():\n\tfrappe.db.sql("UPDATE `tabJarvis Pending Action Waiter` SET status=%s")',
			'def w():\n\tfrappe.db.sql(f"UPDATE `tab{WAITER}` SET status=%s")',
			"def w():\n\tt = frappe.qb.DocType('ToDo')\n\tfrappe.qb.update(t).set(t.status, 'Closed').run()",
		)
		for source in ignored:
			self.assertEqual(status_writers(source), set(), source)

	def test_claim_and_settled_cas_win_once(self):
		from jarvis.chat.pending_actions import _store

		name = self.park(self.make_conv())
		self.assertTrue(_store.claim(name, OWNER))
		self.assertFalse(_store.claim(name, OWNER), "only a Pending row can be claimed")
		self.assertTrue(pa._terminal_update(name, ["Executing"], "Executed"))
		self.assertEqual(pa.claim_settled([name]), [name])
		self.assertEqual(pa.claim_settled([name]), [], "the settled compare-and-set wins once")
		frappe.db.commit()

	def test_transition_skips_a_locked_row(self):
		import threading

		name = self.park(self.make_conv())
		site, locked, release = frappe.local.site, threading.Event(), threading.Event()

		def holder():
			frappe.init(site=site)
			frappe.connect()
			try:
				frappe.db.sql(f"SELECT name FROM `tab{PA}` WHERE name=%(n)s FOR UPDATE", {"n": name})
				locked.set()
				release.wait(timeout=20)
				frappe.db.rollback()
			finally:
				locked.set()
				frappe.destroy()

		t = threading.Thread(target=holder)
		t.start()
		try:
			self.assertTrue(locked.wait(timeout=20))
			frappe.db.commit()
			self.assertEqual(
				pa._transition(name, ["Pending"], "Cancelled", reason_code="cancelled"), "locked"
			)
			frappe.db.rollback()
		finally:
			release.set()
			t.join(timeout=20)
		self.assertEqual(pa._transition("no-such-row", ["Pending"], "Cancelled"), "missing")
		self.assertEqual(self.row(name).status, "Pending")

	def test_terminal_update_nulls_open_key_and_refuses_non_terminal(self):
		name = self.park(self.make_conv(), kind="file_box_held", dedup_key="party:x")
		self.assertTrue(self.row(name).open_key)
		with self.assertRaises(ValueError):
			pa._terminal_update(name, ["Pending"], "Executing")
		self.assertFalse(pa._terminal_update(name, ["Executing"], "Failed", reason_code="failed"))
		self.assertTrue(pa._terminal_update(name, ["Pending"], "Cancelled", reason_code="cancelled"))
		frappe.db.commit()
		row = self.row(name)
		self.assertEqual(
			(row.status, row.open_key, row.reason), ("Cancelled", None, "The action was cancelled.")
		)
		# The dedup slot is free again once terminal.
		self.park(self.make_conv(), kind="file_box_held", dedup_key="party:x")
