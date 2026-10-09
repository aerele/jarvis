"""#707 B3: the agent's database reads run under a statement time limit.

What is proven here, through the real agent entry point
(``api._dispatch_from_session``) with a real Jarvis Chat Session row:

  * a read slower than the limit comes back as the coded ``QueryTooSlowError``
    envelope, at the limit, and its transcript receipt is still written;
  * the session's ``@@max_statement_time`` is the same afterwards, on success, on
    a breach and when the tool raises something else;
  * write tools and the non-agent path (``_dispatch_current_user``) run unlimited;
  * the site config key sets the limit, 0 switches it off, a bad value uses the
    default, and a tighter limit the session already has is kept.

The fake tools read ``@@max_statement_time`` back from inside the call, so most
cases need no slow query at all.
"""

import time
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import _failure_kind, api
from jarvis.tools import _agent_run_ctx, registry

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
		for bad in ("abc", -5, "", [3]):
			with self.subTest(bad=bad):
				frappe.conf[CONFIG_KEY] = bad
				res = self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
				self.assertEqual(res["data"]["limit"], api._AGENT_READ_TIMEOUT_DEFAULT_S)

	def test_a_tighter_session_limit_is_kept(self):
		frappe.db.sql("SET SESSION max_statement_time = 5")
		try:
			res = self._dispatch(READ_TOOL, _reports_limit, {"doctype": "ToDo"})
			self.assertEqual(res["data"]["limit"], 5)
			self.assertEqual(_session_limit(), 5)
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
		self.assertLess(elapsed, 2.5, f"the limit did not stop the query ({elapsed:.1f}s)")
		self.assertFalse(res["ok"], res)
		err = res["error"]
		self.assertEqual(err["code"], "QueryTooSlowError")
		self.assertEqual(err["kind"], _failure_kind.FIXABLE)
		self.assertIn("date range", err["message"])
		self.assertEqual(err["hint"], api._ERROR_HINTS["QueryTooSlowError"])
		self.assertEqual(_failure_kind.envelope_kind(res), _failure_kind.FIXABLE)
		self.assertEqual(_session_limit(), self.before)

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
		err = Exception(1969, "Query execution was interrupted (max_statement_time exceeded)")
		self.assertTrue(_failure_kind.too_slow(err))
		self.assertEqual(_failure_kind.kind_of(err), _failure_kind.FIXABLE)

	def test_a_wrapped_statement_timeout_is_found(self):
		inner = Exception(1969, "max_statement_time exceeded")
		try:
			raise RuntimeError("wrapped") from inner
		except RuntimeError as outer:
			self.assertTrue(_failure_kind.too_slow(outer))

	def test_a_lock_wait_is_not_too_slow(self):
		self.assertFalse(_failure_kind.too_slow(frappe.QueryTimeoutError("lock wait")))
		self.assertEqual(_failure_kind.kind_of(frappe.QueryTimeoutError("t")), _failure_kind.RETRY_LATER)
