import unittest.mock as m

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import agent_audit, api

# The audit sink is metadata-only BY CONSTRUCTION: these are the only fields it
# may ever carry, and none of the content-adjacent ones may appear.
_META_ONLY = {
	"actor",
	"actor_name",
	"tool",
	"outcome",
	"provenance",
	"provenance_name",
	"ref_doctype",
	"ref_name",
	"bulk_count",
	"conversation",
	"model",
	"at",
}
_FORBIDDEN = {"tool_args", "tool_result", "content", "error"}


def _row(**kw):
	d = {
		"doctype": "Jarvis Agent Write",
		"actor": "Administrator",
		"tool": "create_doc",
		"outcome": "applied",
		"provenance": "chat",
		"at": frappe.utils.now(),
	}
	d.update(kw)
	return frappe.get_doc(d).insert(ignore_permissions=True).name


def _ensure_jarvis_admin():
	"""A realistic tamper actor: a Jarvis Admin who is NOT a superuser."""
	if not frappe.db.exists("User", "mgr@example.com"):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": "mgr@example.com",
				"first_name": "Mgr",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		u.add_roles("Jarvis Admin")
	return "mgr@example.com"


class TestAgentWriteDoctype(FrappeTestCase):
	def test_schema_is_metadata_only(self):
		fields = {f.fieldname for f in frappe.get_meta("Jarvis Agent Write").fields}
		self.assertTrue(fields <= _META_ONLY, msg=f"unexpected fields: {fields - _META_ONLY}")
		self.assertFalse(_FORBIDDEN & fields)  # content can never be stored

	def test_append_only_jarvis_admin_cannot_delete(self):
		name = _row()
		frappe.set_user(_ensure_jarvis_admin())  # the realistic tamper actor, not Guest
		try:
			with self.assertRaises(frappe.PermissionError):
				frappe.delete_doc("Jarvis Agent Write", name, ignore_permissions=False)
		finally:
			frappe.set_user("Administrator")


class TestCaptureAtChokepoint(FrappeTestCase):
	"""Exercise the REAL hook (patch jarvis.api.dispatch + call _dispatch_and_wrap
	directly), not just record_write — the pattern in test_action_error_detail.py."""

	def _rows(self, **f):
		return frappe.get_all(
			"Jarvis Agent Write",
			filters=f,
			fields=["tool", "outcome", "provenance", "provenance_name", "bulk_count", "ref_doctype"],
		)

	def test_success_write_records_one_row_with_provenance(self):
		with m.patch("jarvis.api.dispatch", return_value={"name": "X-1"}):
			api._dispatch_and_wrap(
				"create_doc",
				{"doctype": "ToDo", "description": "t"},
				is_write=True,
				provenance="auto_apply",
			)
		rows = self._rows(tool="create_doc", provenance="auto_apply")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["outcome"], "applied")
		self.assertEqual(rows[0]["ref_doctype"], "ToDo")

	def test_handled_failure_is_recorded_as_failed(self):
		# A KNOWN failure is a RAISED, translatable exception (ValidationError etc.) —
		# not a returned {ok:false}. The savepoint rolls the tool back; the audit row
		# rides the envelope commit.
		with m.patch("jarvis.api.dispatch", side_effect=frappe.ValidationError("bad")):
			env = api._dispatch_and_wrap(
				"update_doc", {"doctype": "ToDo", "name": "T-1"}, is_write=True, provenance="chat"
			)
		self.assertFalse(env["ok"])
		rows = self._rows(tool="update_doc", outcome="failed", provenance="chat")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["ref_doctype"], "ToDo")

	def test_hard_500_is_not_recorded(self):
		# An UNEXPECTED exception re-raises (Frappe full-rollback → 500). No row is
		# written; recording here would be lost to the rollback anyway. We assert the
		# re-raise; the "no row survives" half is proven in the bench run (this txn
		# rolls back at teardown).
		with m.patch("jarvis.api.dispatch", side_effect=RuntimeError("boom")):
			with self.assertRaises(RuntimeError):
				api._dispatch_and_wrap(
					"submit_doc", {"doctype": "ToDo", "name": "T-1"}, is_write=True, provenance="chat"
				)

	def test_bulk_is_one_row_with_count(self):
		with m.patch("jarvis.api.dispatch", return_value={}):
			api._dispatch_and_wrap(
				"create_docs",
				{"docs": [{"doctype": "ToDo"}, {"doctype": "ToDo"}, {"doctype": "ToDo"}]},
				is_write=True,
				provenance="chat",
			)
		rows = self._rows(tool="create_docs")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["bulk_count"], 3)
		self.assertEqual(rows[0]["ref_doctype"], "ToDo")

	def test_connector_inner_failure_is_filed_as_failed(self):
		# call_connector is a write that dispatches but returns its OWN {ok:false}
		# envelope. envelope_ok must file it as failed, not a false ERP-write success.
		inner_fail = {"ok": False, "error": {"code": "transport_error", "message": "x"}}
		with m.patch("jarvis.api.dispatch", return_value=inner_fail):
			api._dispatch_and_wrap(
				"call_connector", {"connector": "c", "action": "a"}, is_write=True, provenance="chat"
			)
		rows = self._rows(tool="call_connector")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["outcome"], "failed")

	def test_record_write_never_raises_and_leaves_txn_usable(self):
		with m.patch("frappe.get_doc", side_effect=Exception("db down")):
			self.assertIsNone(
				agent_audit.record_write(
					actor="a@x.com",
					tool="create_doc",
					args={},
					result={},
					outcome="applied",
					provenance="chat",
				)
			)
		# txn still usable afterwards (nothing poisoned it):
		self.assertTrue(frappe.db.sql("SELECT 1"))
