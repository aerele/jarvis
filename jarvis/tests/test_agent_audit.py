import unittest.mock as m

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import agent_audit, api

# The audit sink is metadata-only BY CONSTRUCTION: this INDEPENDENT literal (never
# imported from agent_audit — the guard must not derive from what it guards) is the
# only field set the doctype may ever carry, and none of the content-adjacent ones
# may appear. Adding a content column to the doctype trips test_schema_is_metadata_only.
_META_ONLY = {
	"at",
	"actor",
	"actor_name",
	"tool",
	"outcome",
	"provenance",
	"provenance_name",
	"ref_doctype",
	"ref_name",
	"bulk_count",
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
	# ignore_links: `actor` is a Link(User) and the synthetic a@x.com/... actors are
	# not real Users; _validate_links runs regardless of ignore_permissions.
	return frappe.get_doc(d).insert(ignore_permissions=True, ignore_links=True).name


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

	def test_append_only_jarvis_admin_cannot_insert(self):
		# No role holds `create` — a Jarvis Admin hand-crafting a row must be refused
		# (server-authored rows use ignore_permissions=True; a role never can).
		frappe.set_user(_ensure_jarvis_admin())
		try:
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{
						"doctype": "Jarvis Agent Write",
						"actor": "Administrator",
						"tool": "create_doc",
						"outcome": "applied",
						"provenance": "chat",
						"at": frappe.utils.now(),
					}
				).insert(ignore_permissions=False)
		finally:
			frappe.set_user("Administrator")


# Isolation helpers: record_write is wired into the REAL choke-point, so many
# other suite tests commit Jarvis Agent Write rows (via the downstream
# persist_tool_receipt commit). A broad tool= query therefore sees rows this test
# did NOT create — so every assertion diffs against the row names present BEFORE
# the action and inspects only the rows this test produced.
def _all_names():
	# pluck="name" yields the name strings directly (not row objects).
	return set(frappe.get_all("Jarvis Agent Write", pluck="name"))


def _new_rows(before, **f):
	fields = ["name", "tool", "outcome", "provenance", "provenance_name", "bulk_count", "ref_doctype"]
	return [r for r in frappe.get_all("Jarvis Agent Write", filters=f, fields=fields) if r.name not in before]


class TestCaptureAtChokepoint(FrappeTestCase):
	"""Exercise the REAL hook (patch jarvis.api.dispatch + call _dispatch_and_wrap
	directly), not just record_write — the pattern in test_action_error_detail.py."""

	def test_success_write_records_one_row_with_provenance(self):
		before = _all_names()
		with m.patch("jarvis.api.dispatch", return_value={"name": "X-1"}):
			api._dispatch_and_wrap(
				"create_doc",
				{"doctype": "ToDo", "description": "t"},
				is_write=True,
				provenance="auto_apply",
			)
		new = _new_rows(before, tool="create_doc")
		self.assertEqual(len(new), 1)
		self.assertEqual(new[0]["outcome"], "applied")
		self.assertEqual(new[0]["provenance"], "auto_apply")
		self.assertEqual(new[0]["ref_doctype"], "ToDo")

	def test_handled_failure_is_recorded_as_failed(self):
		# A KNOWN failure is a RAISED, translatable exception (ValidationError etc.) —
		# not a returned {ok:false}. The savepoint rolls the tool back; the audit row
		# rides the envelope commit.
		before = _all_names()
		with m.patch("jarvis.api.dispatch", side_effect=frappe.ValidationError("bad")):
			env = api._dispatch_and_wrap(
				"update_doc", {"doctype": "ToDo", "name": "T-1"}, is_write=True, provenance="chat"
			)
		self.assertFalse(env["ok"])
		new = _new_rows(before, tool="update_doc")
		self.assertEqual(len(new), 1)
		self.assertEqual(new[0]["outcome"], "failed")
		self.assertEqual(new[0]["ref_doctype"], "ToDo")

	def test_hard_500_is_not_recorded(self):
		# An UNEXPECTED exception re-raises (Frappe full-rollback → 500). record_write
		# is NOT called on that branch, so this call produces no row.
		before = _all_names()
		with m.patch("jarvis.api.dispatch", side_effect=RuntimeError("boom")):
			with self.assertRaises(RuntimeError):
				api._dispatch_and_wrap(
					"submit_doc", {"doctype": "ToDo", "name": "T-1"}, is_write=True, provenance="chat"
				)
		self.assertEqual(len(_new_rows(before, tool="submit_doc")), 0)

	def test_bulk_is_one_row_with_count(self):
		before = _all_names()
		with m.patch("jarvis.api.dispatch", return_value={}):
			api._dispatch_and_wrap(
				"create_docs",
				{"docs": [{"doctype": "ToDo"}, {"doctype": "ToDo"}, {"doctype": "ToDo"}]},
				is_write=True,
				provenance="chat",
			)
		new = _new_rows(before, tool="create_docs")
		self.assertEqual(len(new), 1)
		self.assertEqual(new[0]["bulk_count"], 3)
		self.assertEqual(new[0]["ref_doctype"], "ToDo")

	# NOTE (version-16-hotfix backport): the develop test
	# test_connector_inner_failure_is_filed_as_failed is intentionally omitted here.
	# It asserts envelope_ok files a dispatched-but-{ok:false} call_connector as
	# "failed"; envelope_ok / _ENVELOPE_TOOLS are part of the connector-envelope
	# feature that is not on this branch, so a dispatched write records "applied".

	def test_unknown_outcome_coerced_to_applied(self):
		name = agent_audit.record_write(
			actor="Administrator",
			tool="create_doc",
			args={"doctype": "ToDo"},
			result={"name": "Z-1"},
			outcome="bogus",
			provenance="chat",
		)
		self.assertTrue(name)
		self.assertEqual(frappe.db.get_value("Jarvis Agent Write", name, "outcome"), "applied")

	def test_record_write_never_raises_on_construction_failure(self):
		# Failure BEFORE the savepoint (doc construction) → logged + None, txn intact.
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
		self.assertTrue(frappe.db.sql("SELECT 1"))

	def test_record_write_survives_insert_failure_after_savepoint(self):
		# Failure AT insert (AFTER the savepoint is taken) — the savepoint rollback
		# must recover the txn, record_write must still return None (never raise), and
		# the outer transaction must remain usable. This is the invariant the docstring
		# names by name and the construction-failure test above does NOT reach.
		from frappe.model.document import Document

		def boom(self, *a, **k):
			raise frappe.ValidationError("insert boom")

		with m.patch.object(Document, "insert", boom):
			self.assertIsNone(
				agent_audit.record_write(
					actor="Administrator",
					tool="create_doc",
					args={"doctype": "ToDo"},
					result={},
					outcome="applied",
					provenance="chat",
				)
			)
		# txn recovered by the savepoint rollback — a real insert still works:
		self.assertTrue(_row(actor="Administrator"))


class TestDiscardCapture(FrappeTestCase):
	def test_dismiss_records_discarded_for_write_tool(self):
		from jarvis.chat import actions_api

		before = _all_names()
		rec = {"tool": "delete_doc", "args": {"doctype": "ToDo", "name": "T-1"}, "conversation": None}
		with (
			m.patch("jarvis.chat.pending_confirm.peek", return_value=rec),
			m.patch("jarvis.chat.pending_confirm.consume", return_value=rec),
		):
			res = actions_api.dismiss_tool("tok")
		self.assertEqual(res["data"]["status"], "discarded")
		new = _new_rows(before, tool="delete_doc")
		self.assertEqual(len(new), 1)
		self.assertEqual(new[0]["outcome"], "discarded")

	def test_dismiss_no_row_for_non_write_tool(self):
		from jarvis.chat import actions_api

		before = _all_names()
		rec = {"tool": "read_doc", "args": {}, "conversation": None}
		with (
			m.patch("jarvis.chat.pending_confirm.peek", return_value=rec),
			m.patch("jarvis.chat.pending_confirm.consume", return_value=rec),
		):
			actions_api.dismiss_tool("tok")
		self.assertEqual(len(_new_rows(before, tool="read_doc")), 0)


class TestAgentWriteReport(FrappeTestCase):
	def test_execute_returns_safe_columns_and_respects_filter(self):
		from jarvis.jarvis.report.agent_write_log.agent_write_log import execute

		# Seed both outcomes so the outcome filter has something to EXCLUDE (an
		# empty result would make the all() assertion trivially true).
		now = frappe.utils.now_datetime()
		_row(
			actor="a@x.com",
			tool="submit_doc",
			outcome="failed",
			at=frappe.utils.add_to_date(now, minutes=-20),
		)
		_row(
			actor="a@x.com",
			tool="create_doc",
			outcome="applied",
			at=frappe.utils.add_to_date(now, minutes=-10),
		)

		columns, data = execute({"outcome": "failed"})
		safe = {c["fieldname"] for c in columns}
		self.assertFalse(_FORBIDDEN & safe)  # no content columns, ever
		self.assertTrue(data)  # the seeded failed row is present
		self.assertTrue(all(r.get("outcome") == "failed" for r in data))

	def test_execute_gate_refuses_non_admin(self):
		from jarvis.jarvis.report.agent_write_log.agent_write_log import execute

		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				execute({})
		finally:
			frappe.set_user("Administrator")
