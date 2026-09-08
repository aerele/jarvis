"""Unit tests for the two agent-facing connector tools
(``jarvis.tools.call_connector`` / ``jarvis.tools.list_connector_actions``).

Plain ``unittest`` with ``frappe`` mocked out at the call boundary - this
worktree is not installed on any bench (MCP_CONNECTORS_PLAN.md P0/P1 rule), so
these run with no site. What is proven here:

  * ``call_connector`` fast-fails with ``connector_not_ready`` - without
    calling the broker - for an ENABLED connector that has never passed a
    connection test (or lost that pass to a later credential/URL edit, which
    clears ``last_test_status``/``tools_cache``), but lets an unknown or an
    explicitly disabled connector fall through unchanged so the model sees
    the broker's own, more specific error instead;
  * when the connector is ready, ``call_connector`` delegates to
    ``jarvis.connectors.broker.call`` verbatim (result unmodified);
  * ``list_connector_actions`` dedupes Personal-over-Shared by key, fetches
    child rows in one bounded ``get_all``, and
    surfaces only the actions ``policy.action_decision`` currently allows.

Real broker dispatch (row resolution, credential decrypt, the allowed-actions
gate, SSRF, the circuit breaker, the audit log) is exercised by
``tests/test_connector_policy.py`` / ``test_connector_ssrf.py`` /
``test_connector_limits.py`` and, end to end, by the local integration deploy -
not re-proven here. Registry wiring (both tool names present, and the
registered callable matching the module on disk) is already covered by
``tests/test_registry.py::test_registered_tools_match_modules_exactly``, which
walks every ``jarvis/tools/*.py`` file automatically - no new assertion
needed for that here, just confirmed by inspection at review time.
"""

from __future__ import annotations

import unittest
from unittest import mock

from jarvis.connectors import action_rows
from jarvis.tools import call_connector, list_connector_actions


class _ConnectorRow(dict):
	"""Minimal Document-like stand-in exposing ``.get`` like the real
	``Jarvis Connector`` row ``broker.resolve_for_status`` returns."""


def _ready_row(**overrides) -> _ConnectorRow:
	row = {"enabled": 1, "last_test_status": "Passed", "tools_cache": '{"tools": []}'}
	row.update(overrides)
	return _ConnectorRow(row)


class TestCallConnectorDelegation(unittest.TestCase):
	def test_enabled_and_ready_delegates_to_broker_verbatim(self):
		broker_result = {"ok": True, "result": {"content": [{"type": "text", "text": "done"}]}}
		with (
			mock.patch.object(call_connector, "get_session_key", return_value="sess-1"),
			mock.patch.object(call_connector.broker, "resolve_for_status", return_value=_ready_row()),
			mock.patch.object(call_connector.broker, "call", return_value=broker_result) as broker_call,
		):
			result = call_connector.call_connector("github", "create_issue", {"title": "x"})
		broker_call.assert_called_once_with("github", "create_issue", {"title": "x"}, run_id="sess-1")
		self.assertIs(result, broker_result)

	def test_broker_error_result_passed_through_unmodified(self):
		broker_result = {"ok": False, "error": {"code": "action_denied", "message": "nope"}}
		with (
			mock.patch.object(call_connector, "get_session_key", return_value=None),
			mock.patch.object(call_connector.broker, "resolve_for_status", return_value=_ready_row()),
			mock.patch.object(call_connector.broker, "call", return_value=broker_result),
		):
			result = call_connector.call_connector("github", "delete_repo")
		self.assertEqual(result, broker_result)

	def test_unresolvable_connector_falls_through_to_broker(self):
		"""Unknown / not-visible-to-caller is broker.call's own error to raise
		(connector_not_found) - the readiness pre-check must not invent a
		different one when it cannot even resolve the row."""
		broker_result = {"ok": False, "error": {"code": "connector_not_found", "message": "nope"}}
		with (
			mock.patch.object(call_connector, "get_session_key", return_value=None),
			mock.patch.object(call_connector.broker, "resolve_for_status", return_value=None),
			mock.patch.object(call_connector.broker, "call", return_value=broker_result) as broker_call,
		):
			result = call_connector.call_connector("nope", "x")
		broker_call.assert_called_once()
		self.assertEqual(result, broker_result)

	def test_disabled_row_falls_through_to_broker_not_the_not_ready_error(self):
		"""An admin explicitly turning a connector off must read as
		connector_disabled (broker.call's own error), not the misleading
		"needs to be tested" wording - even though it is also untested here."""
		broker_result = {"ok": False, "error": {"code": "connector_disabled", "message": "off"}}
		row = _ready_row(enabled=0, last_test_status="", tools_cache=None)
		with (
			mock.patch.object(call_connector, "get_session_key", return_value=None),
			mock.patch.object(call_connector.broker, "resolve_for_status", return_value=row),
			mock.patch.object(call_connector.broker, "call", return_value=broker_result) as broker_call,
		):
			result = call_connector.call_connector("github", "x")
		broker_call.assert_called_once()
		self.assertEqual(result, broker_result)


class TestCallConnectorReadiness(unittest.TestCase):
	def _not_ready(self, row):
		with (
			mock.patch.object(call_connector.broker, "resolve_for_status", return_value=row),
			mock.patch.object(call_connector.broker, "call") as broker_call,
		):
			result = call_connector.call_connector("github", "create_issue")
		broker_call.assert_not_called()
		self.assertEqual(
			result,
			{
				"ok": False,
				"error": {
					"code": "connector_not_ready",
					"message": "This connector needs to be tested in Settings before it can be used.",
				},
			},
		)

	def test_never_tested_is_not_ready(self):
		self._not_ready(_ready_row(last_test_status="", tools_cache=None))

	def test_last_test_failed_is_not_ready(self):
		self._not_ready(_ready_row(last_test_status="Failed"))

	def test_passed_but_cache_cleared_by_a_later_edit_is_not_ready(self):
		# update_connector clears tools_cache on a credential/base_url change
		# without necessarily rewriting last_test_status in the same edit.
		self._not_ready(_ready_row(tools_cache=None))

	def test_enabled_and_passed_with_cache_is_ready(self):
		broker_result = {"ok": True, "result": {}}
		with (
			mock.patch.object(call_connector.broker, "resolve_for_status", return_value=_ready_row()),
			mock.patch.object(call_connector.broker, "call", return_value=broker_result) as broker_call,
		):
			result = call_connector.call_connector("github", "create_issue")
		broker_call.assert_called_once()
		self.assertEqual(result, broker_result)


class TestListConnectorActionsShape(unittest.TestCase):
	"""Enabled path: dedupe-by-key (Personal wins), child rows fetched in ONE
	``get_all`` bounded to the surviving parent names, and only policy-allowed
	actions are surfaced. Exercises the real ``jarvis.connectors.policy`` gate
	(frappe-free), with ``frappe.get_list`` and the shared ``action_rows`` fetch mocked."""

	def _action(self, parent, action, allowed=0, read_only=0, destructive=0, description="d"):
		return {
			"parent": parent,
			"action": action,
			"allowed": allowed,
			"read_only": read_only,
			"destructive": destructive,
			"description": description,
		}

	def _fake_frappe(self, connectors, actions):
		fake = mock.MagicMock()
		fake.get_list.return_value = connectors
		# Set explicitly in every test: a bare MagicMock iterates as empty, so a
		# forgotten return value would pass vacuously with zero actions.
		fake.get_all.return_value = actions
		return fake

	def _run(self, fake_frappe, connector=None):
		with (
			mock.patch.object(list_connector_actions, "frappe", fake_frappe),
			mock.patch.object(action_rows, "frappe", fake_frappe),
		):
			return list_connector_actions.list_connector_actions(connector)

	def test_personal_wins_over_shared_same_key(self):
		fake = self._fake_frappe(
			[
				# order_by="scope asc, ..." puts Personal ("P") before Shared ("S").
				{"name": "conn-personal", "key": "github", "label": "My GitHub", "scope": "Personal"},
				{"name": "conn-shared", "key": "github", "label": "Team GitHub", "scope": "Shared"},
			],
			[self._action("conn-personal", "read_issue", read_only=1)],
		)
		result = self._run(fake)
		self.assertEqual(len(result["connectors"]), 1)
		self.assertEqual(result["connectors"][0]["scope"], "Personal")
		self.assertEqual([a["action"] for a in result["connectors"][0]["actions"]], ["read_issue"])
		# One child-table query, bounded to the surviving parent only: the
		# shadowed Shared duplicate's rows are never fetched, and no full
		# document is ever loaded.
		fake.get_all.assert_called_once()
		self.assertEqual(fake.get_all.call_args.args[0], "Jarvis Connector Action")
		filters = fake.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["parent"], ["in", ["conn-personal"]])
		self.assertEqual(filters["parenttype"], "Jarvis Connector")
		self.assertEqual(filters["parentfield"], "allowed_actions")
		fake.get_doc.assert_not_called()

	def test_only_policy_allowed_actions_are_surfaced(self):
		fake = self._fake_frappe(
			[{"name": "conn-1", "key": "github", "label": "GitHub", "scope": "Shared"}],
			[
				self._action("conn-1", "read_issue", read_only=1, destructive=0),
				self._action("conn-1", "delete_repo", read_only=0, destructive=1, allowed=0),
				self._action("conn-1", "create_issue", allowed=1),
			],
		)
		result = self._run(fake)
		actions = {a["action"] for a in result["connectors"][0]["actions"]}
		self.assertEqual(actions, {"read_issue", "create_issue"})
		self.assertNotIn("delete_repo", actions)

	def test_child_rows_are_grouped_per_connector_in_query_order(self):
		fake = self._fake_frappe(
			[
				{"name": "conn-gh", "key": "github", "label": "GitHub", "scope": "Shared"},
				{"name": "conn-jira", "key": "jira", "label": "Jira", "scope": "Shared"},
			],
			[
				self._action("conn-gh", "read_issue", read_only=1),
				self._action("conn-jira", "create_issue", allowed=1),
				self._action("conn-gh", "create_pr", allowed=1),
			],
		)
		result = self._run(fake)
		by_key = {c["connector"]: [a["action"] for a in c["actions"]] for c in result["connectors"]}
		self.assertEqual(by_key, {"github": ["read_issue", "create_pr"], "jira": ["create_issue"]})

	def test_connector_without_child_rows_has_empty_actions(self):
		fake = self._fake_frappe(
			[{"name": "conn-1", "key": "github", "label": "GitHub", "scope": "Shared"}], []
		)
		result = self._run(fake)
		self.assertEqual(result["connectors"][0]["actions"], [])

	def test_no_visible_connectors_skips_child_query(self):
		fake = self._fake_frappe([], [])
		result = self._run(fake)
		self.assertEqual(result, {"connectors": []})
		fake.get_all.assert_not_called()


if __name__ == "__main__":
	unittest.main()
