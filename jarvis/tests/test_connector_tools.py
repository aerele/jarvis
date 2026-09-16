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
    surfaces only the actions ``policy.action_decision`` currently allows;
  * with an ``action``, ``list_connector_actions`` returns that one action's
    ``input_schema`` (read from ``tools_cache`` in one bounded ``get_values``)
    only when the gate allows it and size-capped at 16 KiB - a denied or
    unknown action reads no cache and yields empty actions, and the no-action
    output is unchanged.

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

import json
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


class TestListConnectorActionsSchema(unittest.TestCase):
	"""Named-action view: with ``action`` the tool returns the one action's
	``input_schema`` (from ``tools_cache``) - but only when
	``policy.action_decision`` allows it, only for the resolved row, and only
	after the untrusted schema is size-capped. The gate and the ``tools_cache``
	read are both driven off the frappe-free ``policy`` module, with
	``frappe.get_list``, the shared ``action_rows`` fetch, and
	``frappe.db.get_values`` mocked."""

	def _action(self, parent, action, allowed=0, read_only=0, destructive=0, description="d"):
		return {
			"parent": parent,
			"action": action,
			"allowed": allowed,
			"read_only": read_only,
			"destructive": destructive,
			"description": description,
		}

	def _cache(self, schemas_by_action):
		"""A ``tools_cache`` JSON blob mapping each action to its ``inputSchema``,
		shaped exactly as ``policy.input_schema`` parses it."""
		return json.dumps(
			{"tools": [{"name": name, "inputSchema": schema} for name, schema in schemas_by_action.items()]}
		)

	def _fake_frappe(self, connectors, actions, caches=None):
		fake = mock.MagicMock()
		fake.get_list.return_value = connectors
		# Set explicitly in every test: a bare MagicMock iterates as empty, so a
		# forgotten return value would pass vacuously with zero actions.
		fake.get_all.return_value = actions
		# {connector name: raw tools_cache}; get_values returns the as_dict rows
		# the module maps by name. Extra names are harmless (the code picks by
		# name), a missing one reads back as no cache.
		fake.db.get_values.return_value = [
			{"name": name, "tools_cache": cache} for name, cache in (caches or {}).items()
		]
		return fake

	def _run(self, fake_frappe, connector=None, action=None):
		with (
			mock.patch.object(list_connector_actions, "frappe", fake_frappe),
			mock.patch.object(action_rows, "frappe", fake_frappe),
		):
			return list_connector_actions.list_connector_actions(connector, action)

	def test_named_action_returns_schema_only_when_allowed(self):
		schema = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
		fake = self._fake_frappe(
			[{"name": "conn-1", "key": "github", "label": "GitHub", "scope": "Shared"}],
			[self._action("conn-1", "read_issue", read_only=1)],
			caches={"conn-1": self._cache({"read_issue": schema})},
		)
		result = self._run(fake, connector="github", action="read_issue")
		self.assertEqual(
			result,
			{
				"connectors": [
					{
						"connector": "github",
						"label": "GitHub",
						"scope": "Shared",
						"actions": [{"action": "read_issue", "description": "d", "input_schema": schema}],
					}
				]
			},
		)
		# tools_cache is read once, bounded to the resolved row, via get_values
		# (a read) - never get_doc, never get_all against the parent.
		fake.db.get_values.assert_called_once()
		self.assertEqual(fake.db.get_values.call_args.args[0], "Jarvis Connector")
		self.assertEqual(fake.db.get_values.call_args.args[1], {"name": ["in", ["conn-1"]]})
		self.assertEqual(fake.db.get_values.call_args.args[2], ["name", "tools_cache"])
		self.assertIs(fake.db.get_values.call_args.kwargs["as_dict"], True)
		fake.get_doc.assert_not_called()

	def test_denied_action_returns_empty_actions_and_never_reads_cache(self):
		fake = self._fake_frappe(
			[{"name": "conn-1", "key": "github", "label": "GitHub", "scope": "Shared"}],
			[self._action("conn-1", "delete_repo", destructive=1, allowed=0)],
			caches={"conn-1": self._cache({"delete_repo": {"type": "object"}})},
		)
		result = self._run(fake, connector="github", action="delete_repo")
		self.assertEqual(result["connectors"][0]["actions"], [])
		self.assertEqual(result["connectors"][0]["connector"], "github")
		# Denied -> the untrusted schema blob is never pulled.
		fake.db.get_values.assert_not_called()

	def test_unknown_action_returns_empty_actions_and_never_reads_cache(self):
		fake = self._fake_frappe(
			[{"name": "conn-1", "key": "github", "label": "GitHub", "scope": "Shared"}],
			[self._action("conn-1", "read_issue", read_only=1)],
		)
		result = self._run(fake, connector="github", action="does_not_exist")
		self.assertEqual(result["connectors"][0]["actions"], [])
		fake.db.get_values.assert_not_called()

	def test_unresolved_connector_returns_empty(self):
		fake = self._fake_frappe([], [])
		result = self._run(fake, connector="nope", action="read_issue")
		self.assertEqual(result, {"connectors": []})
		fake.db.get_values.assert_not_called()

	def test_allowed_but_cache_lacks_the_tool_returns_null_schema(self):
		fake = self._fake_frappe(
			[{"name": "conn-1", "key": "github", "label": "GitHub", "scope": "Shared"}],
			[self._action("conn-1", "read_issue", read_only=1)],
			# cache lists a DIFFERENT tool, so there is no schema for read_issue.
			caches={"conn-1": self._cache({"other": {"type": "object"}})},
		)
		result = self._run(fake, connector="github", action="read_issue")
		self.assertEqual(
			result["connectors"][0]["actions"],
			[{"action": "read_issue", "description": "d", "input_schema": None}],
		)

	def test_allowed_but_cache_missing_returns_null_schema(self):
		fake = self._fake_frappe(
			[{"name": "conn-1", "key": "github", "label": "GitHub", "scope": "Shared"}],
			[self._action("conn-1", "read_issue", read_only=1)],
			caches={"conn-1": None},
		)
		result = self._run(fake, connector="github", action="read_issue")
		self.assertEqual(result["connectors"][0]["actions"][0]["input_schema"], None)

	def test_personal_wins_and_cache_read_targets_personal(self):
		schema = {"type": "object", "properties": {"id": {"type": "string"}}}
		fake = self._fake_frappe(
			[
				{"name": "conn-personal", "key": "github", "label": "My GitHub", "scope": "Personal"},
				{"name": "conn-shared", "key": "github", "label": "Team GitHub", "scope": "Shared"},
			],
			[self._action("conn-personal", "read_issue", read_only=1)],
			caches={"conn-personal": self._cache({"read_issue": schema})},
		)
		result = self._run(fake, connector="github", action="read_issue")
		self.assertEqual(len(result["connectors"]), 1)
		self.assertEqual(result["connectors"][0]["scope"], "Personal")
		self.assertEqual(result["connectors"][0]["actions"][0]["input_schema"], schema)
		# The resolved (Personal) row is the only one whose cache is read; the
		# shadowed Shared duplicate is never touched.
		self.assertEqual(fake.db.get_values.call_args.args[1], {"name": ["in", ["conn-personal"]]})

	def test_no_connector_searches_all_and_returns_only_matches(self):
		schema = {"type": "object", "properties": {"id": {"type": "string"}}}
		fake = self._fake_frappe(
			[
				{"name": "conn-gh", "key": "github", "label": "GitHub", "scope": "Shared"},
				{"name": "conn-jira", "key": "jira", "label": "Jira", "scope": "Shared"},
			],
			[
				self._action("conn-gh", "read_issue", read_only=1),
				# jira does not offer read_issue at all.
				self._action("conn-jira", "create_ticket", allowed=1),
			],
			caches={"conn-gh": self._cache({"read_issue": schema})},
		)
		result = self._run(fake, action="read_issue")
		self.assertEqual([c["connector"] for c in result["connectors"]], ["github"])
		self.assertEqual(result["connectors"][0]["actions"][0]["input_schema"], schema)
		# Only the matching connector's cache is read.
		self.assertEqual(fake.db.get_values.call_args.args[1], {"name": ["in", ["conn-gh"]]})

	def test_schema_under_cap_passes_through_unchanged(self):
		schema = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
		fake = self._fake_frappe(
			[{"name": "conn-1", "key": "github", "label": "GitHub", "scope": "Shared"}],
			[self._action("conn-1", "read_issue", read_only=1)],
			caches={"conn-1": self._cache({"read_issue": schema})},
		)
		result = self._run(fake, connector="github", action="read_issue")
		self.assertEqual(result["connectors"][0]["actions"][0]["input_schema"], schema)

	def test_oversize_schema_returns_top_level_summary(self):
		# > 16 KiB serialised; required carries a non-string the summary must drop.
		big = {
			"type": "object",
			"properties": {f"field_{i}": {"type": "string", "description": "x" * 300} for i in range(100)},
			"required": ["field_0", 123, "field_1"],
		}
		self.assertGreater(len(json.dumps(big)), 16 * 1024)
		fake = self._fake_frappe(
			[{"name": "conn-1", "key": "github", "label": "GitHub", "scope": "Shared"}],
			[self._action("conn-1", "read_issue", read_only=1)],
			caches={"conn-1": self._cache({"read_issue": big})},
		)
		result = self._run(fake, connector="github", action="read_issue")
		schema_out = result["connectors"][0]["actions"][0]["input_schema"]
		self.assertTrue(schema_out["truncated"])
		self.assertEqual(schema_out["required"], ["field_0", "field_1"])
		self.assertEqual(schema_out["properties"], [f"field_{i}" for i in range(100)])

	def test_no_action_path_is_unchanged_and_reads_no_cache(self):
		connectors = [{"name": "conn-1", "key": "github", "label": "GitHub", "scope": "Shared"}]
		fake = self._fake_frappe(connectors, [self._action("conn-1", "read_issue", read_only=1)])
		for action in (None, ""):
			result = self._run(fake, action=action)
			self.assertEqual(
				result,
				{
					"connectors": [
						{
							"connector": "github",
							"label": "GitHub",
							"scope": "Shared",
							"actions": [{"action": "read_issue", "description": "d"}],
						}
					]
				},
			)
		# The listing view never reads tools_cache.
		fake.db.get_values.assert_not_called()
		# Pin the resolution query so the shared _resolve_connectors refactor
		# cannot drift the fields / ordering / cap that make Personal win.
		kwargs = fake.get_list.call_args.kwargs
		self.assertEqual(kwargs["fields"], ["name", "key", "label", "scope"])
		self.assertEqual(kwargs["order_by"], "scope asc, label asc")
		self.assertEqual(kwargs["limit_page_length"], 30)
		self.assertEqual(kwargs["filters"], {"enabled": 1})


class TestSchemaSummaryBounds(unittest.TestCase):
	def test_summary_caps_name_count_and_length(self):
		from jarvis.tools import list_connector_actions as mod

		props = {f"p{i}_" + "n" * 300: {"type": "string"} for i in range(500)}
		summary = mod._schema_summary({"required": list(props.keys()), "properties": props})
		self.assertTrue(summary["truncated"])
		self.assertEqual(len(summary["properties"]), mod._SUMMARY_MAX_NAMES)
		self.assertEqual(len(summary["required"]), mod._SUMMARY_MAX_NAMES)
		self.assertTrue(all(len(n) <= mod._SUMMARY_NAME_MAX for n in summary["properties"]))


if __name__ == "__main__":
	unittest.main()
