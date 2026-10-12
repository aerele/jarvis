"""#707 B3: the agent's database reads run under a statement time limit.

What is proven here, through the real agent entry point
(``api._dispatch_from_session``) with a real Jarvis Chat Session row:

  * a read slower than the limit comes back as the coded ``QueryTooSlowError``
    envelope, at the limit, and its transcript receipt is still written;
  * the session's ``@@max_statement_time`` is the same afterwards, on success, on
    a breach and when the tool raises something else;
  * write tools and the non-agent path (``_dispatch_current_user``) run unlimited;
  * the site config key sets the limit, 0 switches it off, a bad value uses the
    default, and a tighter limit the session already has is kept;
  * a breach leaves one line in the server log, and the read tools that catch
    every exception let the limit through instead of answering with a fallback.

The fake tools read ``@@max_statement_time`` back from inside the call, so most
cases need no slow query at all.
"""

import time
import unittest
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase

from jarvis import _failure_kind, api
from jarvis.tools import (
	_agent_run_ctx,
	get_creation_context,
	get_gstr1_summary,
	preview_doc,
	registry,
	resolve_links,
)

SESSION_KEY = "agent:main:i707-b3-read-limit"
READ_TOOL = "get_schema"
WRITE_TOOL = "export_query"
CONFIG_KEY = api._AGENT_READ_TIMEOUT_KEY


def _session_limit() -> float:
	return float(frappe.db.sql("SELECT @@max_statement_time")[0][0])


def _reports_limit(doctype=None, **kwargs):
	"""A tool body that reports the limit its own statements run under."""
	return {"limit": _session_limit()}


def _sleeps(doctype=None, **kwargs):
	frappe.db.sql("SELECT SLEEP(3)")
	return {"slept": True}


def _timeout_error() -> Exception:
	"""MariaDB error 1969 as the driver raises it (pymysql and mysqlclient alike)."""
	return frappe.db.OperationalError(1969, "Query execution was interrupted (max_statement_time exceeded)")


def _lines(logs, text: str) -> list:
	return [line for line in logs.output if text in line]


class TestAgentReadTimeLimit(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.db_type != "mariadb":
			raise unittest.SkipTest("the read limit is MariaDB only")

	def setUp(self):
		frappe.set_user("Administrator")
		self._cleanup()
		self.conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "B3 read limit"}).insert(
			ignore_permissions=True
		)
		frappe.db.set_value("Jarvis Conversation", self.conv.name, "session_key", SESSION_KEY)
		frappe.get_doc(
			{"doctype": "Jarvis Chat Session", "session_key": SESSION_KEY, "user": "Administrator"}
		).insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- the receipt path commits too
		self.before = _session_limit()

	def tearDown(self):
		_agent_run_ctx.clear_session_key()
		frappe.set_user("Administrator")
		frappe.conf.pop(CONFIG_KEY, None)
		self._cleanup()
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- undo the committed fixtures

	def _cleanup(self):
		convs = frappe.get_all("Jarvis Conversation", {"session_key": SESSION_KEY}, pluck="name")
		if convs:
			frappe.db.delete("Jarvis Chat Message", {"conversation": ("in", convs)})
			frappe.db.delete("Jarvis Conversation", {"name": ("in", convs)})
		frappe.db.delete("Jarvis Chat Session", {"session_key": SESSION_KEY})

	def _dispatch(self, tool: str, fn, args: dict | None = None) -> dict:
		with (
			patch.dict(registry._TOOLS, {tool: fn}),
			patch("jarvis.api._get_header", return_value=None),
		):
			return api._dispatch_from_session("Administrator", SESSION_KEY, tool, args or {})

	def _receipts(self) -> list:
		return frappe.get_all(
			"Jarvis Chat Message",
			{"conversation": self.conv.name, "role": "tool"},
			["tool_name", "tool_status", "tool_result"],
		)

	# ------------------------------------------------------------------ #
	# the limit itself
	# ------------------------------------------------------------------ #
	def test_a_read_runs_under_the_default_limit(self):
		res = self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
		self.assertTrue(res["ok"], res)
		self.assertEqual(res["data"]["limit"], api._AGENT_READ_TIMEOUT_DEFAULT_S)
		self.assertEqual(_session_limit(), self.before)

	def test_the_site_config_sets_the_limit(self):
		frappe.conf[CONFIG_KEY] = 7
		res = self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
		self.assertEqual(res["data"]["limit"], 7)

	def test_zero_switches_the_limit_off(self):
		frappe.conf[CONFIG_KEY] = 0
		res = self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
		self.assertEqual(res["data"]["limit"], self.before)

	def test_a_bad_value_uses_the_default(self):
		# int() would read 0.5 as 0 (off), True as a 1 s limit and 2.5 as 2.
		for bad in ("abc", -5, "", [3], True, False, 0.5, 2.5, "2.5"):
			with self.subTest(bad=bad):
				frappe.conf[CONFIG_KEY] = bad
				res = self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
				self.assertEqual(res["data"]["limit"], api._AGENT_READ_TIMEOUT_DEFAULT_S)

	def test_a_whole_number_float_is_accepted(self):
		frappe.conf[CONFIG_KEY] = 7.0
		res = self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
		self.assertEqual(res["data"]["limit"], 7)

	def test_a_tighter_session_limit_is_kept(self):
		frappe.db.sql("SET SESSION max_statement_time = 5")
		try:
			res = self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
			self.assertEqual(res["data"]["limit"], 5)
			self.assertEqual(_session_limit(), 5)
		finally:
			frappe.db.sql("SET SESSION max_statement_time = %s", (self.before,))

	def test_a_server_that_cannot_set_the_limit_runs_the_read_unlimited(self):
		frappe.conf[CONFIG_KEY] = 1
		with patch.object(frappe.db, "set_execution_timeout", side_effect=Exception("unknown variable")):
			res = self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
		self.assertTrue(res["ok"], res)
		self.assertEqual(res["data"]["limit"], self.before)
		self.assertEqual(_session_limit(), self.before)

	def test_a_server_that_cannot_set_the_limit_is_logged_once(self):
		frappe.conf[CONFIG_KEY] = 1
		with (
			patch.object(api, "_limit_unsettable_logged", False),
			patch.object(frappe.db, "set_execution_timeout", side_effect=Exception("unknown variable")),
			self.assertLogs(frappe.logger("jarvis"), level="WARNING") as logs,
		):
			self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
			self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
		self.assertEqual(len(_lines(logs, "could not set max_statement_time")), 1, logs.output)

	def test_another_database_is_left_alone(self):
		# MariaDB only: on any other database not one statement is sent.
		with (
			patch.object(frappe.db, "db_type", "postgres"),
			patch.object(frappe.db, "set_execution_timeout") as setter,
			patch.object(frappe.db, "sql") as sql,
		):
			with api._agent_read_time_limit(READ_TOOL):
				pass
		setter.assert_not_called()
		sql.assert_not_called()

	def test_a_restore_that_fails_keeps_the_result(self):
		real_sql = frappe.db.sql

		def _restore_fails(query, *args, **kwargs):
			if str(query).startswith("SET SESSION max_statement_time"):
				raise Exception("server has gone away")
			return real_sql(query, *args, **kwargs)

		try:
			with (
				patch.object(frappe.db, "sql", side_effect=_restore_fails),
				self.assertLogs(frappe.logger("jarvis"), level="WARNING") as logs,
			):
				res = self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
			self.assertTrue(res["ok"], res)
			self.assertEqual(res["data"]["limit"], api._AGENT_READ_TIMEOUT_DEFAULT_S)
			self.assertEqual(len(_lines(logs, "could not restore max_statement_time")), 1, logs.output)
		finally:
			frappe.db.sql("SET SESSION max_statement_time = %s", (self.before,))

	def test_a_looser_session_limit_is_tightened_then_restored(self):
		frappe.db.sql("SET SESSION max_statement_time = 120")
		try:
			res = self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
			self.assertEqual(res["data"]["limit"], api._AGENT_READ_TIMEOUT_DEFAULT_S)
			self.assertEqual(_session_limit(), 120)
		finally:
			frappe.db.sql("SET SESSION max_statement_time = %s", (self.before,))

	# ------------------------------------------------------------------ #
	# a breach
	# ------------------------------------------------------------------ #
	def test_a_slow_read_comes_back_coded_at_the_limit(self):
		frappe.conf[CONFIG_KEY] = 1
		started = time.monotonic()
		res = self._dispatch(READ_TOOL, _sleeps, {"doctype": "ToDo"})
		elapsed = time.monotonic() - started
		# Under the 3 s sleep, with room for dispatch, the receipt commit and the audit.
		self.assertLess(elapsed, 2.9, f"the limit did not stop the query ({elapsed:.1f}s)")
		self.assertFalse(res["ok"], res)
		err = res["error"]
		self.assertEqual(err["code"], "QueryTooSlowError")
		self.assertEqual(err["kind"], _failure_kind.FIXABLE)
		self.assertIn("date range", err["message"])
		self.assertEqual(err["hint"], api._ERROR_HINTS["QueryTooSlowError"])
		self.assertEqual(_failure_kind.envelope_kind(res), _failure_kind.FIXABLE)
		self.assertEqual(_session_limit(), self.before)

	def test_a_breach_leaves_a_line_in_the_server_log(self):
		frappe.conf[CONFIG_KEY] = 1
		with self.assertLogs(frappe.logger("jarvis"), level="WARNING") as logs:
			self._dispatch(READ_TOOL, _sleeps, {"doctype": "ToDo"})
		lines = _lines(logs, "agent read stopped at the statement time limit")
		self.assertEqual(len(lines), 1, logs.output)
		for part in (
			f"tool={READ_TOOL}",
			"user=Administrator",
			f"conversation={self.conv.name}",
			"limit_s=1",
			"duration_ms=",
		):
			self.assertIn(part, lines[0])
		self.assertNotIn(SESSION_KEY, lines[0])  # the session key is a bearer

	def test_the_receipt_is_written_after_a_breach(self):
		frappe.conf[CONFIG_KEY] = 1
		self._dispatch(READ_TOOL, _sleeps, {"doctype": "ToDo"})
		frappe.db.rollback()  # only what was committed counts
		rows = self._receipts()
		self.assertEqual(len(rows), 1, rows)
		self.assertEqual(rows[0].tool_name, READ_TOOL)
		self.assertEqual(rows[0].tool_status, "error")
		self.assertIn("QueryTooSlowError", rows[0].tool_result)
		# the connection still works
		self.assertEqual(frappe.db.sql("SELECT 1")[0][0], 1)

	def test_the_limit_is_restored_when_the_tool_raises(self):
		seen = []

		def _raises(doctype=None):
			seen.append(_session_limit())
			raise frappe.ValidationError("boom")

		res = self._dispatch(READ_TOOL, _raises, {"doctype": "ToDo"})
		self.assertFalse(res["ok"], res)
		self.assertEqual(seen, [api._AGENT_READ_TIMEOUT_DEFAULT_S])
		self.assertEqual(_session_limit(), self.before)

	def test_the_limit_is_restored_when_the_tool_raises_past_the_envelope(self):
		seen = []

		def _crashes(doctype=None):
			seen.append(_session_limit())
			raise RuntimeError("bug")

		with self.assertRaises(RuntimeError):
			self._dispatch(READ_TOOL, _crashes, {"doctype": "ToDo"})
		self.assertEqual(seen, [api._AGENT_READ_TIMEOUT_DEFAULT_S])
		self.assertEqual(_session_limit(), self.before)

	# ------------------------------------------------------------------ #
	# what is left alone
	# ------------------------------------------------------------------ #
	def test_a_write_tool_is_not_limited(self):
		self.assertIn(WRITE_TOOL, api._WRITE_TOOLS)
		frappe.conf[CONFIG_KEY] = 1
		res = self._dispatch(WRITE_TOOL, _reports_limit, {"doctype": "ToDo"})
		self.assertTrue(res["ok"], res)
		self.assertEqual(res["data"]["limit"], self.before)

	def test_the_non_agent_path_is_not_limited(self):
		frappe.conf[CONFIG_KEY] = 1
		with patch.dict(registry._TOOLS, {READ_TOOL: _reports_limit}):
			res = api._dispatch_current_user(READ_TOOL, {"doctype": "ToDo"})
		self.assertTrue(res["ok"], res)
		self.assertEqual(res["data"]["limit"], self.before)


class TestTooSlowKind(FrappeTestCase):
	def test_a_statement_timeout_is_fixable(self):
		err = _timeout_error()
		self.assertTrue(_failure_kind.too_slow(err))
		self.assertEqual(_failure_kind.kind_of(err), _failure_kind.FIXABLE)

	def test_a_wrapped_statement_timeout_is_found(self):
		inner = _timeout_error()
		try:
			raise RuntimeError("wrapped") from inner
		except RuntimeError as outer:
			self.assertTrue(_failure_kind.too_slow(outer))

	def test_only_a_read_is_translated(self):
		err = _timeout_error()
		read = api._translate_write_error(err, api._msglog_mark())
		self.assertEqual(read["error"]["code"], "QueryTooSlowError")
		self.assertIsNone(api._translate_write_error(err, api._msglog_mark(), is_write=True))

	def test_an_exception_without_args_is_not_too_slow(self):
		# A bare ``raise frappe.PermissionError`` carries no args; Frappe 15's
		# helper indexes args[0] unguarded, so this must not raise IndexError.
		self.assertFalse(_failure_kind.too_slow(frappe.PermissionError()))
		self.assertEqual(
			_failure_kind.kind_of(frappe.PermissionError()),
			_failure_kind.kind_of(frappe.PermissionError("x")),
		)

	def test_the_args_guard_does_not_lean_on_frappe(self):
		# Holds whatever the Frappe helper does: this stub reads args[0] unguarded,
		# as Frappe 15's did, and a driver error can be built without args.
		def _unguarded(e):
			return e.args[0] == 1969

		with patch.object(frappe.db, "is_statement_timeout", _unguarded):
			self.assertFalse(_failure_kind.too_slow(frappe.db.OperationalError()))
			self.assertFalse(_failure_kind.too_slow(frappe.PermissionError()))
			self.assertTrue(_failure_kind.too_slow(_timeout_error()))

	def test_only_a_database_error_is_too_slow(self):
		# The same args on anything else (a dict keyed by year) is not the limit.
		self.assertFalse(_failure_kind.too_slow(KeyError(1969)))
		self.assertFalse(_failure_kind.too_slow(Exception(1969, "max_statement_time exceeded")))
		self.assertEqual(_failure_kind.kind_of(KeyError(1969)), _failure_kind.NOT_FIXABLE)

	def test_a_lock_wait_is_not_too_slow(self):
		self.assertFalse(_failure_kind.too_slow(frappe.QueryTimeoutError("lock wait")))
		self.assertEqual(_failure_kind.kind_of(frappe.QueryTimeoutError("t")), _failure_kind.RETRY_LATER)


class TestReadToolsLetTheLimitThrough(FrappeTestCase):
	"""These read tools catch every exception around a query and answer with a
	fallback. A query stopped at the limit must reach the dispatcher instead, or
	the agent is told "missing" / "invalid" / "error" about data it never read."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.db_type != "mariadb":
			raise unittest.SkipTest("the read limit is MariaDB only")

	def setUp(self):
		frappe.set_user("Administrator")

	def test_resolve_links_does_not_report_a_cut_search_as_missing(self):
		with patch("frappe.get_list", side_effect=_timeout_error()):
			with self.assertRaises(frappe.db.OperationalError):
				resolve_links._candidates("User", "nobody-707", 5)
			# the exact-match probe, whose fallback is "unchecked"
			with self.assertRaises(frappe.db.OperationalError):
				resolve_links._resolve_one("allocated_to", "User", "nobody-707", 5)
		# any other failure still falls back
		with patch("frappe.get_list", side_effect=Exception("boom")):
			self.assertEqual(resolve_links._candidates("User", "nobody-707", 5), [])

	def test_preview_doc_does_not_report_a_cut_dry_run_as_invalid(self):
		with patch.object(Document, "insert", side_effect=_timeout_error()):
			with self.assertRaises(frappe.db.OperationalError):
				preview_doc.preview_doc("ToDo", {"description": "B3 dry run"})

	def test_get_creation_context_does_not_drop_a_cut_link_probe(self):
		meta = frappe.get_meta("ToDo")
		with patch.object(frappe.db, "exists", side_effect=_timeout_error()):
			with self.assertRaises(frappe.db.OperationalError):
				get_creation_context._resolve_link_field(meta, "nobody-707")

	def test_get_creation_context_does_not_drop_cut_facets(self):
		meta = frappe.get_meta("ToDo")
		with patch("frappe.get_list", side_effect=_timeout_error()):
			with self.assertRaises(frappe.db.OperationalError):
				get_creation_context._facets("ToDo", meta)

	def test_get_gstr1_summary_does_not_report_a_cut_read_as_an_error(self):
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch("frappe.get_installed_apps", side_effect=_timeout_error()),
			patch.object(frappe, "log_error"),
		):
			with self.assertRaises(frappe.db.OperationalError):
				get_gstr1_summary.get_gstr1_summary("B3 Company", "122026")
