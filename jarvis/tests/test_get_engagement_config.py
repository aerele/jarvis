"""Direct tests for the zero-argument engagement-configuration tool.

The delegate gets only the immutable snapshot stamped on its *running* Run. It
must never recover configuration from the mutable installation row: an operator
save after dispatch belongs to the next run, not the in-flight evidence.
"""

import unittest
from unittest import mock

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.get_engagement_config import get_engagement_config

RUN = "Jarvis Agent Run"
INSTALLATION = "Jarvis Agent Installation"


class TestGetEngagementConfig(unittest.TestCase):
	def _call(self, *, session_key, run_row):
		"""Run the tool with the trusted session and Run query mocked.

		The fake preserves the SQL status predicate: a terminal row is not an
		active match even when it has the same session key.
		"""

		def fake_get_value(doctype, filters, fieldname=None, **kw):
			if doctype != RUN:
				raise AssertionError(f"the config tool must not read {doctype}")
			if filters != {"session_key": session_key, "status": "running"}:
				raise AssertionError(f"unexpected Run filters: {filters!r}")
			if fieldname != ["name", "engagement_config_json"]:
				raise AssertionError(f"unexpected Run fields: {fieldname!r}")
			if not run_row or run_row.get("status") != "running":
				return None
			return run_row

		with (
			mock.patch("jarvis.tools._agent_run_ctx.get_session_key", return_value=session_key),
			mock.patch.object(frappe.db, "get_value", side_effect=fake_get_value) as get_value,
			mock.patch.object(frappe, "log_error") as log_error,
		):
			result = get_engagement_config()
		return result, get_value, log_error

	def test_no_session_key_raises_before_any_database_read(self):
		with self.assertRaises(InvalidArgumentError):
			self._call(
				session_key=None,
				run_row=frappe._dict(name="R1", status="running", engagement_config_json="{}"),
			)

	def test_no_active_run_bound_to_session_raises(self):
		with self.assertRaises(InvalidArgumentError):
			self._call(session_key="agent:agent-x:R1", run_row=None)

	def test_terminal_run_is_not_a_valid_config_source(self):
		with self.assertRaises(InvalidArgumentError):
			self._call(
				session_key="agent:agent-x:R1",
				run_row=frappe._dict(
					name="R1", status="completed", engagement_config_json=frappe.as_json({"x": 1})
				),
			)

	def test_happy_path_returns_the_stamped_snapshot_only(self):
		cfg = {"depr_run_posted": False, "materiality": {"percentage": 5}}
		result, get_value, _ = self._call(
			session_key="agent:agent-x:R1",
			run_row=frappe._dict(name="R1", status="running", engagement_config_json=frappe.as_json(cfg)),
		)
		self.assertEqual(dict(result), cfg)
		get_value.assert_called_once_with(
			RUN,
			{"session_key": "agent:agent-x:R1", "status": "running"},
			["name", "engagement_config_json"],
			as_dict=True,
		)

	def test_legacy_run_without_snapshot_fails_closed_to_empty_config(self):
		result, get_value, _ = self._call(
			session_key="agent:agent-x:R1",
			run_row=frappe._dict(name="R1", status="running", engagement_config_json=None),
		)
		self.assertEqual(result, {})
		self.assertEqual([call.args[0] for call in get_value.call_args_list], [RUN])

	def test_empty_snapshot_fails_closed_to_empty_config(self):
		result, get_value, _ = self._call(
			session_key="agent:agent-x:R1",
			run_row=frappe._dict(name="R1", status="running", engagement_config_json=""),
		)
		self.assertEqual(result, {})
		self.assertEqual([call.args[0] for call in get_value.call_args_list], [RUN])

	def test_empty_object_snapshot_fails_closed_to_empty_config(self):
		result, get_value, _ = self._call(
			session_key="agent:agent-x:R1",
			run_row=frappe._dict(name="R1", status="running", engagement_config_json="{}"),
		)
		self.assertEqual(result, {})
		self.assertEqual([call.args[0] for call in get_value.call_args_list], [RUN])

	def test_malformed_snapshot_fails_closed_to_empty_config(self):
		result, get_value, log_error = self._call(
			session_key="agent:agent-x:R1",
			run_row=frappe._dict(name="R1", status="running", engagement_config_json="{not json"),
		)
		self.assertEqual(result, {})
		self.assertEqual([call.args[0] for call in get_value.call_args_list], [RUN])
		log_error.assert_called_once()

	def test_non_object_snapshot_fails_closed_to_empty_config(self):
		result, get_value, _ = self._call(
			session_key="agent:agent-x:R1",
			run_row=frappe._dict(name="R1", status="running", engagement_config_json="[1, 2, 3]"),
		)
		self.assertEqual(result, {})
		self.assertEqual([call.args[0] for call in get_value.call_args_list], [RUN])

	def test_mutable_installation_config_cannot_change_a_running_run(self):
		"""A later save that changes `depr_run_posted` cannot rewrite this run."""
		# The hypothetical Installation now says True. The mocked DB throws if the
		# tool even attempts to read that row; only the server-stamped False snapshot
		# may reach the delegate.
		mutable_installation = {"depr_run_posted": True}
		self.assertTrue(mutable_installation["depr_run_posted"])
		result, get_value, _ = self._call(
			session_key="agent:agent-x:R1",
			run_row=frappe._dict(
				name="R1", status="running", engagement_config_json='{"depr_run_posted": false}'
			),
		)
		self.assertEqual(result, {"depr_run_posted": False})
		# `_call` raises on any non-Run query. This exact call list makes the
		# prohibited mutable Installation read an explicit regression assertion.
		self.assertEqual([call.args[0] for call in get_value.call_args_list], [RUN])
		self.assertNotIn(INSTALLATION, [call.args[0] for call in get_value.call_args_list])
