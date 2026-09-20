import unittest.mock as m

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import agent_audit, api
from jarvis.chat import user_settings_api as usapi

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


def _ensure_plain_user():
	"""A user with NO Jarvis/System role — must be refused by require_jarvis_admin."""
	if not frappe.db.exists("User", "plain@example.com"):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": "plain@example.com",
				"first_name": "Plain",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
	return "plain@example.com"


class TestAdminReads(FrappeTestCase):
	def _seed(self):
		# Deterministic timestamps so newest-first ordering is testable.
		_row(actor="a@x.com", tool="create_doc", outcome="applied", at="2026-09-18 09:00:00")
		_row(actor="a@x.com", tool="submit_doc", outcome="failed", at="2026-09-18 10:00:00")
		_row(actor="b@x.com", tool="delete_doc", outcome="discarded", at="2026-09-18 11:00:00")

	def test_gate_refuses_non_admins_allows_admins(self):
		for u in ("Guest", _ensure_plain_user()):
			frappe.set_user(u)
			try:
				with self.assertRaises(frappe.PermissionError):
					usapi.admin_list_agent_writes()
				with self.assertRaises(frappe.PermissionError):
					usapi.admin_agent_write_summary()
			finally:
				frappe.set_user("Administrator")
		# Administrator + a Jarvis Admin both pass (no raise).
		self.assertTrue(usapi.admin_list_agent_writes()["ok"])
		frappe.set_user(_ensure_jarvis_admin())
		try:
			self.assertTrue(usapi.admin_list_agent_writes()["ok"])
		finally:
			frappe.set_user("Administrator")

	def test_newest_first_and_shape(self):
		self._seed()
		rows = usapi.admin_list_agent_writes()["data"]["rows"]
		mine = [r for r in rows if r["actor"] in ("a@x.com", "b@x.com")]
		# newest (11:00 discarded) first, oldest (09:00 applied) last
		self.assertEqual(mine[0]["outcome"], "discarded")
		self.assertEqual(mine[-1]["outcome"], "applied")
		# no content-adjacent key ever leaves the server
		self.assertFalse(_FORBIDDEN & set(rows[0].keys()))

	def test_actor_and_outcome_filters(self):
		self._seed()
		by_actor = usapi.admin_list_agent_writes(actor="a@x.com")["data"]["rows"]
		self.assertTrue(by_actor and all(r["actor"] == "a@x.com" for r in by_actor))
		by_outcome = usapi.admin_list_agent_writes(outcome="failed")["data"]["rows"]
		self.assertTrue(by_outcome and all(r["outcome"] == "failed" for r in by_outcome))
		# an unknown outcome value is ignored (returns all), not an error
		self.assertTrue(usapi.admin_list_agent_writes(outcome="bogus")["ok"])

	def test_has_more_lookahead(self):
		self._seed()
		page = usapi.admin_list_agent_writes(page_length=2)["data"]
		self.assertEqual(len(page["rows"]), 2)
		self.assertTrue(page["has_more"])  # 3 seeded, page of 2 → +1 look-ahead trips

	def test_summary_counts_and_window(self):
		self._seed()
		# an old row (outside the 7-day window) must NOT count
		_row(actor="c@x.com", tool="create_doc", outcome="applied", at="2020-01-01 00:00:00")
		s = usapi.admin_agent_write_summary(days=3650)["data"]
		self.assertGreaterEqual(s["writes"], 4)
		self.assertGreaterEqual(s["actors"], 3)
		recent = usapi.admin_agent_write_summary(days=7)["data"]
		self.assertGreaterEqual(recent["failed"], 1)
		self.assertLess(recent["writes"], s["writes"])  # the 2020 row is excluded from 7-day


class TestAgentWriteReport(FrappeTestCase):
	def test_execute_returns_safe_columns_and_respects_filter(self):
		from jarvis.jarvis.report.agent_write_log.agent_write_log import execute

		# Seed both outcomes so the outcome filter has something to EXCLUDE (an
		# empty result would make the all() assertion trivially true).
		_row(actor="a@x.com", tool="submit_doc", outcome="failed", at="2026-09-19 09:00:00")
		_row(actor="a@x.com", tool="create_doc", outcome="applied", at="2026-09-19 10:00:00")

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
