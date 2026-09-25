"""PR-3b's deploy pieces: the never-expire patch (keyed on pending-action existence,
AC-U13) and ``reconcile_action_cards``' orphan repair driven from the pending action."""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm, session_lifecycle
from jarvis.chat.pending_actions import _store
from jarvis.patches import v2_23_never_expire_chat_cards as patch_module
from jarvis.tests._pending_action_helpers import MSG, OWNER, PendingActionTestMixin, as_user

TODO = {"doctype": "ToDo", "values": {"description": "pa-test never expire"}}
PREVIEW = {"would": {"doctype": "ToDo"}, "card": {"title": "Create a ToDo", "fields": []}}


class _Base(PendingActionTestMixin, FrappeTestCase):
	def mint(self, conv):
		return pending_confirm.mint(
			conversation=conv,
			owner=OWNER,
			run_id="",
			tool="create_doc",
			args=dict(TODO),
			preview=dict(PREVIEW),
		)

	def legacy_row(self, conv, token, expires_in_s):
		"""A Redis-era display row (no pending action behind it)."""
		with as_user(OWNER):
			api._insert_pending_row(conv, "create_doc", PREVIEW, token, int(time.time()) + expires_in_s)
		frappe.db.commit()

	def display(self, token):
		return frappe.db.get_value(
			MSG,
			{"tool_call_id": token, "role": "tool"},
			["tool_status", "action_outcome", "expires_at", "pending_card"],
			as_dict=True,
		)

	def set_expiry(self, token, when):
		frappe.db.sql(
			f"UPDATE `tab{MSG}` SET expires_at=%(w)s WHERE tool_call_id=%(t)s", {"w": when, "t": token}
		)
		frappe.db.commit()


class TestNeverExpirePatch(_Base):
	def test_the_patch_touches_only_the_rows_its_rule_selects(self):
		now = frappe.utils.now_datetime()
		conv = self.make_conv()
		pending = self.mint(conv)
		self.set_expiry(pending, now + timedelta(minutes=5))  # a PR-3a era row
		busy_conv = self.make_conv()
		executing = self.mint(busy_conv)
		self.set_expiry(executing, now - timedelta(hours=1))
		self.set_col(executing, status="Executing")
		legacy_conv, live_conv = self.make_conv(), self.make_conv()
		self.legacy_row(legacy_conv, "zz-legacy-past", -3600)
		self.legacy_row(live_conv, "zz-legacy-live", 600)

		patch_module.execute()
		patch_module.execute()  # idempotent

		self.assertIsNone(self.display(pending).expires_at)
		self.assertEqual(self.display(pending).tool_status, "pending")
		self.assertIsNotNone(self.display(executing).expires_at, "an executing card is its own writer's")
		self.assertEqual(self.display(executing).tool_status, "pending")
		past = self.display("zz-legacy-past")
		self.assertEqual((past.tool_status, past.action_outcome, past.pending_card), ("", "expired", None))
		live = self.display("zz-legacy-live")
		self.assertEqual((live.tool_status, live.action_outcome or ""), ("pending", ""))


class TestOrphanRepair(_Base):
	def repair(self):
		with patch.object(pending_confirm, "cards_open_gauge", return_value=0):
			return session_lifecycle.reconcile_action_cards()["repaired"]

	def test_a_row_left_pending_behind_a_settled_decision_gets_its_chip(self):
		conv = self.make_conv()
		token = self.mint(conv)
		_store._terminal_update(token, ["Pending"], "Discarded", reason_code="discarded")
		frappe.db.commit()
		self.set_col(token, settled=1)
		self.assertGreaterEqual(self.repair(), 1)
		self.assertEqual(self.display(token).action_outcome, "discarded")

	def test_an_unsettled_decision_is_settled(self):
		conv = self.make_conv()
		token = self.mint(conv)
		_store._terminal_update(token, ["Pending"], "Cancelled", reason_code="cancelled")
		frappe.db.commit()
		self.repair()
		self.assertEqual(self.display(token).action_outcome, "cancelled")
		self.assertEqual(self.row(token).settled, 1)

	def test_pending_and_executing_cards_and_legacy_rows_are_left_alone(self):
		conv, busy, legacy = self.make_conv(), self.make_conv(), self.make_conv()
		live = self.mint(conv)
		executing = self.mint(busy)
		self.set_col(executing, status="Executing")
		self.legacy_row(legacy, "zz-legacy-orphan", 600)
		self.repair()
		for token in (live, executing, "zz-legacy-orphan"):
			self.assertEqual(self.display(token).tool_status, "pending", token)

	def test_a_row_whose_pending_action_is_gone_reads_unknown(self):
		conv = self.make_conv()
		token = self.mint(conv)
		frappe.db.sql("DELETE FROM `tabJarvis Pending Action` WHERE name=%(n)s", {"n": token})
		frappe.db.commit()
		self.repair()
		self.assertEqual(self.display(token).action_outcome, "unknown")
