"""Tests for the durable action-row (PR 1 of the action-card overhaul).

The gated-write confirmation card becomes a durable ``role="tool"`` Jarvis Chat
Message (``tool_status="pending"``) — the pre-action twin of the receipt chip —
delivered and reloaded by the message pipeline. The Redis ``pending_confirm``
token stays the SOLE execution authority; the row is display-only for execution.

Spec: docs/superpowers/specs/2026-09-21-action-card-overhaul-design.md (§9).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm
from jarvis.chat.actions_api import confirm_tool, dismiss_tool


def _make_conversation() -> str:
	return (
		frappe.get_doc({"doctype": "Jarvis Conversation", "title": "action-row test"})
		.insert(ignore_permissions=True)
		.name
	)


class TestActionRowDoctype(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.conv = _make_conversation()
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Conversation", self.conv, force=True, ignore_permissions=True)
		)

	def test_pending_status_and_fields_exist(self):
		"""AC-C1 (spec §9.4 AC-migrate): a role="tool" row accepts tool_status="pending"
		(Select option present) and carries pending_card + expires_at."""
		m = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": self.conv,
				"seq": 1,
				"role": "tool",
				"tool_status": "pending",
				"tool_call_id": "tok_actionrow_test",
				"pending_card": frappe.as_json({"kind": "verb", "verb": "submit"}),
				"expires_at": frappe.utils.now_datetime(),
			}
		)
		m.insert(ignore_permissions=True)  # must NOT raise InvalidSelectError
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Chat Message", m.name, force=True, ignore_permissions=True)
		)
		row = frappe.db.get_value(
			"Jarvis Chat Message",
			m.name,
			["tool_status", "pending_card", "expires_at"],
			as_dict=True,
		)
		self.assertEqual(row.tool_status, "pending")
		self.assertTrue(row.pending_card)
		self.assertIsNotNone(row.expires_at)


class TestParkActionRow(FrappeTestCase):
	"""Task 1.1: parking a gated write writes a durable pending row (fail-closed)."""

	def setUp(self):
		super().setUp()
		self.owner = frappe.session.user  # Administrator under the test runner
		# Redis is NOT rolled back by FrappeTestCase — clear the owner index so
		# list_for_owner assertions are isolated from other suites' parked tokens.
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
		self.conv = _make_conversation()
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		for rec in pending_confirm.list_for_owner(self.owner):
			try:
				pending_confirm.consume(
					rec["token"], owner=self.owner, conversation=rec.get("conversation") or ""
				)
			except Exception:
				pass
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
		frappe.delete_doc("Jarvis Conversation", self.conv, force=True, ignore_permissions=True)

	def _pending_rows(self):
		return frappe.get_all(
			"Jarvis Chat Message",
			filters={"conversation": self.conv, "role": "tool", "tool_status": "pending"},
			fields=["name", "pending_card", "tool_args", "tool_call_id", "expires_at"],
		)

	def test_park_writes_pending_row_no_raw_args(self):
		"""AC-R1 groundwork: a gated park writes exactly one pending row carrying the
		card + token + expiry, and NOT raw tool_args (an unconfirmed row never exposes
		them). Nothing executes — the Redis token is the authority, the row is display-only."""
		r = api._run_tool(
			"create_doc",
			{"doctype": "ToDo", "values": {"description": "actionrow-park-xyz"}},
			conversation=self.conv,
		)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		rows = self._pending_rows()
		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0].pending_card)  # card delivered on the durable row
		self.assertFalse(rows[0].tool_args)  # NO raw args on a pending row
		self.assertTrue(rows[0].tool_call_id)  # bound to the token
		self.assertIsNotNone(rows[0].expires_at)  # countdown survives a reload
		self.assertFalse(frappe.db.exists("ToDo", {"description": "actionrow-park-xyz"}))

	def test_park_row_failure_rolls_back_token(self):
		"""AC-park-fail: if the row insert fails after a successful mint, the gate rolls
		the token back (no orphan in the owner index), returns the retryable error, and
		executes nothing."""
		with patch("jarvis.api.persist_pending_action", side_effect=Exception("db blip")):
			r = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "actionrow-fail-xyz"}},
				conversation=self.conv,
			)
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["code"], "ConfirmationUnavailableError")
		self.assertEqual(pending_confirm.list_for_owner(self.owner, self.conv), [])
		self.assertEqual(len(self._pending_rows()), 0)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "actionrow-fail-xyz"}))


class TestFlipActionRow(FrappeTestCase):
	"""Task 1.2: confirm/discard/approve flip the parked pending row into the receipt
	IN PLACE (flip-else-insert), so there is exactly one terminal row per token."""

	def setUp(self):
		super().setUp()
		self.owner = frappe.session.user
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
		self.conv = _make_conversation()
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		for rec in pending_confirm.list_for_owner(self.owner):
			try:
				pending_confirm.consume(
					rec["token"], owner=self.owner, conversation=rec.get("conversation") or ""
				)
			except Exception:
				pass
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self.owner)
		frappe.db.delete("ToDo", {"description": ["like", "flip-%"]})
		frappe.delete_doc("Jarvis Conversation", self.conv, force=True, ignore_permissions=True)

	def _rows(self):
		return frappe.get_all(
			"Jarvis Chat Message",
			filters={"conversation": self.conv, "role": "tool"},
			fields=["name", "tool_status", "action_outcome", "tool_call_id", "pending_card", "tool_args"],
		)

	def _pending_rows(self):
		return [r for r in self._rows() if r.tool_status == "pending"]

	def _park(self, desc):
		api._run_tool(
			"create_doc",
			{"doctype": "ToDo", "values": {"description": desc}},
			conversation=self.conv,
		)
		pending = self._pending_rows()
		self.assertEqual(len(pending), 1)
		return pending[0].name, pending[0].tool_call_id

	def test_confirm_flips_pending_row_in_place(self):
		"""AC-flip: confirm flips the SAME row to 'confirmed' (no duplicate), clears the
		card, and the write actually ran."""
		name, token = self._park("flip-confirm-xyz")
		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(token, conversation=self.conv)
		self.assertTrue(res["ok"])
		same = [r for r in self._rows() if r.tool_call_id == token]
		self.assertEqual(len(same), 1)  # exactly one terminal row, no orphan/dup
		self.assertEqual(same[0].name, name)  # SAME row, flipped in place
		self.assertEqual(same[0].action_outcome, "confirmed")
		self.assertFalse(same[0].pending_card)  # minimized on flip
		self.assertTrue(frappe.db.exists("ToDo", {"description": "flip-confirm-xyz"}))

	def test_direct_mint_confirm_falls_back_to_insert(self):
		"""AC-flip fallback: a directly-minted token (no gate -> no pending row) still
		yields exactly one confirmed receipt row (insert path)."""
		token = pending_confirm.mint(
			conversation=self.conv,
			owner=self.owner,
			exec_user=self.owner,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "flip-directmint-xyz"}},
			run_id="",
		)
		self.assertEqual(len(self._pending_rows()), 0)  # nothing parked a row
		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(token, conversation=self.conv)
		self.assertTrue(res["ok"])
		rows = [r for r in self._rows() if r.tool_call_id == token]
		self.assertEqual(len(rows), 1)  # inserted exactly one
		self.assertEqual(rows[0].action_outcome, "confirmed")

	def test_discard_flips_pending_row_to_discarded(self):
		"""AC-flip: discard flips the SAME row to 'discarded' + clears the card; nothing runs."""
		name, token = self._park("flip-discard-xyz")
		res = dismiss_tool(token, conversation=self.conv)
		self.assertTrue(res["ok"])
		same = [r for r in self._rows() if r.tool_call_id == token]
		self.assertEqual(len(same), 1)
		self.assertEqual(same[0].name, name)
		self.assertEqual(same[0].action_outcome, "discarded")
		self.assertFalse(same[0].pending_card)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "flip-discard-xyz"}))
