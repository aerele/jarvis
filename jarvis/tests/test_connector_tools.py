"""Unit tests for the two agent-facing connector tools
(``jarvis.tools.call_connector`` / ``jarvis.tools.list_connector_actions``) and
their shared kill switch (``jarvis.tools._connector_gate``).

Plain ``unittest`` with ``frappe`` mocked out at the call boundary - this
worktree is not installed on any bench (MCP_CONNECTORS_PLAN.md P0/P1 rule), so
these run with no site. What is proven here:

  * the kill switch (``connectors_enabled``) reads the site_config override
    first, falls back to ``Jarvis Settings.connectors_enabled``, and fails
    CLOSED (False) on any read error - never a silent fall-open;
  * ``call_connector`` fast-fails with a ``connectors_disabled`` error and
    NEVER calls the broker when the switch is off;
  * ``call_connector`` fast-fails with ``connector_not_ready`` - again without
    calling the broker - for an ENABLED connector that has never passed a
    connection test (or lost that pass to a later credential/URL edit, which
    clears ``last_test_status``/``tools_cache``), but lets an unknown or an
    explicitly disabled connector fall through unchanged so the model sees
    the broker's own, more specific error instead;
  * when the switch is on and the connector is ready, ``call_connector``
    delegates to ``jarvis.connectors.broker.call`` verbatim (result
    unmodified);
  * ``list_connector_actions`` returns an EMPTY (not an error) structure when
    the switch is off, without ever touching ``frappe.get_list``.

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

from jarvis.tools import _connector_gate, call_connector, list_connector_actions


class TestConnectorGate(unittest.TestCase):
	def test_site_config_override_true_wins(self):
		fake_frappe = mock.MagicMock()
		fake_frappe.conf.get.return_value = True
		with mock.patch.object(_connector_gate, "frappe", fake_frappe):
			self.assertTrue(_connector_gate.connectors_enabled())
		fake_frappe.db.get_single_value.assert_not_called()

	def test_site_config_override_false_wins_even_if_settings_on(self):
		fake_frappe = mock.MagicMock()
		fake_frappe.conf.get.return_value = False
		fake_frappe.db.get_single_value.return_value = 1
		with mock.patch.object(_connector_gate, "frappe", fake_frappe):
			self.assertFalse(_connector_gate.connectors_enabled())

	def test_falls_back_to_jarvis_settings_when_unset(self):
		fake_frappe = mock.MagicMock()
		fake_frappe.conf.get.return_value = None
		fake_frappe.db.get_single_value.return_value = 1
		with mock.patch.object(_connector_gate, "frappe", fake_frappe):
			self.assertTrue(_connector_gate.connectors_enabled())
		fake_frappe.db.get_single_value.assert_called_once_with(
			_connector_gate.SETTINGS_DOCTYPE, "connectors_enabled"
		)

	def test_settings_falsy_default_is_off(self):
		fake_frappe = mock.MagicMock()
		fake_frappe.conf.get.return_value = None
		fake_frappe.db.get_single_value.return_value = 0
		with mock.patch.object(_connector_gate, "frappe", fake_frappe):
			self.assertFalse(_connector_gate.connectors_enabled())

	def test_read_error_fails_closed(self):
		fake_frappe = mock.MagicMock()
		fake_frappe.conf.get.return_value = None
		fake_frappe.db.get_single_value.side_effect = RuntimeError("db down")
		with mock.patch.object(_connector_gate, "frappe", fake_frappe):
			self.assertFalse(_connector_gate.connectors_enabled())


class _ConnectorRow(dict):
	"""Minimal Document-like stand-in exposing ``.get`` like the real
	``Jarvis Connector`` row ``broker.resolve_for_status`` returns."""


def _ready_row(**overrides) -> _ConnectorRow:
	row = {"enabled": 1, "last_test_status": "Passed", "tools_cache": '{"tools": []}'}
	row.update(overrides)
	return _ConnectorRow(row)


class TestCallConnectorKillSwitch(unittest.TestCase):
	def test_disabled_fast_fails_without_calling_broker(self):
		with (
			mock.patch.object(call_connector, "connectors_enabled", return_value=False),
			mock.patch.object(call_connector.broker, "call") as broker_call,
			mock.patch.object(call_connector.broker, "resolve_for_status") as resolve,
		):
			result = call_connector.call_connector("github", "create_issue", {"title": "x"})
		broker_call.assert_not_called()
		resolve.assert_not_called()
		self.assertEqual(
			result,
			{
				"ok": False,
				"error": {
					"code": "connectors_disabled",
					"message": "Connectors are not enabled for this workspace.",
				},
			},
		)

	def test_enabled_and_ready_delegates_to_broker_verbatim(self):
		broker_result = {"ok": True, "result": {"content": [{"type": "text", "text": "done"}]}}
		with (
			mock.patch.object(call_connector, "connectors_enabled", return_value=True),
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
			mock.patch.object(call_connector, "connectors_enabled", return_value=True),
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
			mock.patch.object(call_connector, "connectors_enabled", return_value=True),
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
			mock.patch.object(call_connector, "connectors_enabled", return_value=True),
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
			mock.patch.object(call_connector, "connectors_enabled", return_value=True),
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
			mock.patch.object(call_connector, "connectors_enabled", return_value=True),
			mock.patch.object(call_connector.broker, "resolve_for_status", return_value=_ready_row()),
			mock.patch.object(call_connector.broker, "call", return_value=broker_result) as broker_call,
		):
			result = call_connector.call_connector("github", "create_issue")
		broker_call.assert_called_once()
		self.assertEqual(result, broker_result)


class TestListConnectorActionsKillSwitch(unittest.TestCase):
	def test_disabled_returns_empty_without_touching_frappe(self):
		fake_frappe = mock.MagicMock()
		with (
			mock.patch.object(list_connector_actions, "connectors_enabled", return_value=False),
			mock.patch.object(list_connector_actions, "frappe", fake_frappe),
		):
			result = list_connector_actions.list_connector_actions()
		self.assertEqual(result, {"connectors": []})
		fake_frappe.get_list.assert_not_called()
		fake_frappe.get_doc.assert_not_called()

	def test_disabled_returns_empty_even_when_connector_named(self):
		with mock.patch.object(list_connector_actions, "connectors_enabled", return_value=False):
			result = list_connector_actions.list_connector_actions(connector="github")
		self.assertEqual(result, {"connectors": []})


class _Row(dict):
	"""Minimal Document-like stand-in exposing attribute access + ``.get``,
	matching what ``jarvis.connectors.policy`` and the tool expect."""

	def __getattr__(self, name):
		try:
			return self[name]
		except KeyError as exc:
			raise AttributeError(name) from exc


class TestListConnectorActionsShape(unittest.TestCase):
	"""Enabled path: dedupe-by-key (Personal wins), and only policy-allowed
	actions are surfaced. Exercises the real ``jarvis.connectors.policy`` gate
	(frappe-free), with ``frappe.get_list``/``get_doc`` mocked."""

	def _action(self, action, allowed=0, read_only=0, destructive=0, description="d"):
		return _Row(action=action, allowed=allowed, read_only=read_only, destructive=destructive, description=description)

	def _connector_doc(self, name, key, label, scope, actions):
		return _Row(name=name, key=key, label=label, scope=scope, allowed_actions=actions)

	def test_personal_wins_over_shared_same_key(self):
		fake_frappe = mock.MagicMock()
		# order_by="scope asc, ..." puts Personal ("P") before Shared ("S").
		fake_frappe.get_list.return_value = [
			{"name": "conn-personal", "key": "github", "label": "My GitHub", "scope": "Personal"},
			{"name": "conn-shared", "key": "github", "label": "Team GitHub", "scope": "Shared"},
		]
		personal_doc = self._connector_doc(
			"conn-personal",
			"github",
			"My GitHub",
			"Personal",
			[self._action("read_issue", read_only=1)],
		)
		fake_frappe.get_doc.return_value = personal_doc
		with (
			mock.patch.object(list_connector_actions, "connectors_enabled", return_value=True),
			mock.patch.object(list_connector_actions, "frappe", fake_frappe),
		):
			result = list_connector_actions.list_connector_actions()
		self.assertEqual(len(result["connectors"]), 1)
		self.assertEqual(result["connectors"][0]["scope"], "Personal")
		fake_frappe.get_doc.assert_called_once_with("Jarvis Connector", "conn-personal")

	def test_only_policy_allowed_actions_are_surfaced(self):
		fake_frappe = mock.MagicMock()
		fake_frappe.get_list.return_value = [
			{"name": "conn-1", "key": "github", "label": "GitHub", "scope": "Shared"}
		]
		doc = self._connector_doc(
			"conn-1",
			"github",
			"GitHub",
			"Shared",
			[
				self._action("read_issue", read_only=1, destructive=0),
				self._action("delete_repo", read_only=0, destructive=1, allowed=0),
				self._action("create_issue", allowed=1),
			],
		)
		fake_frappe.get_doc.return_value = doc
		with (
			mock.patch.object(list_connector_actions, "connectors_enabled", return_value=True),
			mock.patch.object(list_connector_actions, "frappe", fake_frappe),
		):
			result = list_connector_actions.list_connector_actions()
		actions = {a["action"] for a in result["connectors"][0]["actions"]}
		self.assertEqual(actions, {"read_issue", "create_issue"})
		self.assertNotIn("delete_repo", actions)


if __name__ == "__main__":
	unittest.main()
