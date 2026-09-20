import unittest.mock as m

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import agent_audit, api
from jarvis.chat import user_settings_api as usapi

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
		# An UNEXPECTED exception re-raises (Frappe full-rollback → 500). record_write
		# is NOT called on that branch, so no row exists even within this open txn.
		with m.patch("jarvis.api.dispatch", side_effect=RuntimeError("boom")):
			with self.assertRaises(RuntimeError):
				api._dispatch_and_wrap(
					"submit_doc", {"doctype": "ToDo", "name": "T-1"}, is_write=True, provenance="chat"
				)
		self.assertEqual(len(self._rows(tool="submit_doc")), 0)

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


class TestAdminReads(FrappeTestCase):
	def _seed(self):
		# Relative timestamps (not absolute strings — those become a time-bomb as the
		# 7-day window slides past them) with a fixed ordering for the newest-first test.
		now = frappe.utils.now_datetime()
		_row(
			actor="a@x.com",
			tool="create_doc",
			outcome="applied",
			at=frappe.utils.add_to_date(now, minutes=-30),
		)
		_row(
			actor="a@x.com",
			tool="submit_doc",
			outcome="failed",
			at=frappe.utils.add_to_date(now, minutes=-20),
		)
		_row(
			actor="b@x.com",
			tool="delete_doc",
			outcome="discarded",
			at=frappe.utils.add_to_date(now, minutes=-10),
		)

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
		# newest (t-10 discarded) first, oldest (t-30 applied) last
		self.assertEqual(mine[0]["outcome"], "discarded")
		self.assertEqual(mine[-1]["outcome"], "applied")
		# no content-adjacent key ever leaves the server
		self.assertFalse(_FORBIDDEN & set(rows[0].keys()))

	def test_actor_and_outcome_filters(self):
		self._seed()
		# free-text person search matches the User id as a substring (not exact)
		by_substr = usapi.admin_list_agent_writes(actor="a@")["data"]["rows"]
		self.assertTrue(by_substr and all("a@" in (r["actor"] or "") for r in by_substr))
		by_outcome = usapi.admin_list_agent_writes(outcome="failed")["data"]["rows"]
		self.assertTrue(by_outcome and all(r["outcome"] == "failed" for r in by_outcome))
		# an unknown outcome value is IGNORED (returns all) — proven by count, not just ok
		allrows = usapi.admin_list_agent_writes(page_length=200)["data"]["rows"]
		bogus = usapi.admin_list_agent_writes(outcome="bogus", page_length=200)["data"]["rows"]
		self.assertEqual(len(bogus), len(allrows))

	def test_hostile_type_filters_are_coerced(self):
		# runtime type coercion is OFF here (from __future__ import annotations) → _s
		# must force a list/dict arg to a harmless string, never a filter list/dict.
		self.assertTrue(usapi.admin_list_agent_writes(actor=["x"], outcome={"a": 1})["ok"])

	def test_clamps(self):
		# page_length/days clamps don't crash on 0 / negative / huge inputs.
		self.assertTrue(usapi.admin_list_agent_writes(page_length=-5)["ok"])
		self.assertTrue(usapi.admin_list_agent_writes(page_length=99999)["ok"])
		self.assertTrue(usapi.admin_agent_write_summary(days=-5)["ok"])
		self.assertTrue(usapi.admin_agent_write_summary(days=99999)["ok"])

	def test_has_more_lookahead(self):
		self._seed()
		page = usapi.admin_list_agent_writes(page_length=2)["data"]
		self.assertEqual(len(page["rows"]), 2)
		self.assertTrue(page["has_more"])  # ≥3 rows exist → page of 2 trips the +1 look-ahead

	def test_summary_counts_and_window(self):
		now = frappe.utils.now_datetime()
		recent = frappe.utils.add_to_date(now, hours=-2)
		old = frappe.utils.add_to_date(now, days=-400)
		_row(actor="s1@x.com", tool="create_doc", outcome="applied", at=recent)
		_row(actor="s2@x.com", tool="create_doc", outcome="applied", at=recent)
		_row(actor="s1@x.com", tool="submit_doc", outcome="failed", at=recent)
		_row(actor="s1@x.com", tool="delete_doc", outcome="discarded", at=recent)
		_row(actor="s3@x.com", tool="create_doc", outcome="applied", at=old)  # outside the 7-day window
		wk = usapi.admin_agent_write_summary(days=7)["data"]
		# "writes" counts APPLIED only — the failed + discarded rows are NOT writes
		self.assertGreaterEqual(wk["writes"], 2)
		self.assertGreaterEqual(wk["failed"], 1)
		self.assertGreaterEqual(wk["actors"], 2)
		yr = usapi.admin_agent_write_summary(days=3650)["data"]
		self.assertGreater(yr["writes"], wk["writes"])  # the 400-day-old APPLIED row is excluded from 7d


class TestDiscardCapture(FrappeTestCase):
	def _rows(self, tool):
		return frappe.get_all("Jarvis Agent Write", filters={"tool": tool}, fields=["outcome"])

	def test_dismiss_records_discarded_for_write_tool(self):
		from jarvis.chat import actions_api

		rec = {"tool": "delete_doc", "args": {"doctype": "ToDo", "name": "T-1"}, "conversation": None}
		with (
			m.patch("jarvis.chat.pending_confirm.peek", return_value=rec),
			m.patch("jarvis.chat.pending_confirm.consume", return_value=rec),
		):
			res = actions_api.dismiss_tool("tok")
		self.assertEqual(res["data"]["status"], "discarded")
		rows = self._rows("delete_doc")
		self.assertTrue(rows and all(r["outcome"] == "discarded" for r in rows))

	def test_dismiss_no_row_for_non_write_tool(self):
		from jarvis.chat import actions_api

		rec = {"tool": "read_doc", "args": {}, "conversation": None}
		with (
			m.patch("jarvis.chat.pending_confirm.peek", return_value=rec),
			m.patch("jarvis.chat.pending_confirm.consume", return_value=rec),
		):
			actions_api.dismiss_tool("tok")
		self.assertFalse(self._rows("read_doc"))


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
