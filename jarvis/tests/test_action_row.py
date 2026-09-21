"""Tests for the durable action-row (PR 1 of the action-card overhaul).

The gated-write confirmation card becomes a durable ``role="tool"`` Jarvis Chat
Message (``tool_status="pending"``) — the pre-action twin of the receipt chip —
delivered and reloaded by the message pipeline. The Redis ``pending_confirm``
token stays the SOLE execution authority; the row is display-only for execution.

Spec: docs/superpowers/specs/2026-09-21-action-card-overhaul-design.md (§9).
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


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
