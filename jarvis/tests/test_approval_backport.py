"""The approval backport must work before shared-chat schema is installed."""

from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.chat import approval_backport as compat


class TestApprovalBackportGuard(TestCase):
	def test_absent_schema_never_reads_the_missing_column(self):
		with (
			patch.object(frappe.db, "has_column", return_value=False),
			patch.object(frappe.db, "get_value") as read,
		):
			self.assertFalse(compat.is_collaborative("legacy-chat"))
			self.assertIsNotNone(
				compat.legacy_approval_condition(frappe.qb.DocType("Jarvis Approval Request"))
			)
			read.assert_not_called()

	def test_active_shared_chat_is_refused(self):
		with (
			patch.object(frappe.db, "has_column", return_value=True),
			patch.object(frappe.db, "get_value", return_value=1),
		):
			with self.assertRaises(frappe.PermissionError):
				compat.require_legacy_conversation("shared-chat")

	def test_binding_cannot_enter_the_legacy_controller(self):
		with self.assertRaises(frappe.PermissionError):
			compat.guard_record(frappe._dict(collaboration_binding="foreign-binding"))
