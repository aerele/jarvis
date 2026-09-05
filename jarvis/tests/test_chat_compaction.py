"""Context meter + Compact (spec 2026-09-05-chat-context-meter-compact-design)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

CHAT_SESSION = "Jarvis Chat Session"
CONV = "Jarvis Conversation"


class TestCompactionFields(FrappeTestCase):
	def test_session_has_budget_fields(self):
		meta = frappe.get_meta(CHAT_SESSION)
		for f, t in (
			("budget_route", "Data"),
			("reserve_tokens", "Int"),
			("compaction_count", "Int"),
			("last_compacted_at", "Datetime"),
		):
			self.assertEqual(meta.get_field(f).fieldtype, t, f)

	def test_conversation_has_compacting_since(self):
		self.assertEqual(frappe.get_meta(CONV).get_field("compacting_since").fieldtype, "Datetime")
