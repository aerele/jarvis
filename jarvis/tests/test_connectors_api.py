"""Tests for jarvis.chat.connectors_api (MCP_CONNECTORS_PLAN.md P3).

NOTE: this worktree is not installed on any bench (per MCP_CONNECTORS_PLAN.md
P0 rules), so these do not run here. They are exercised in the integration
deploy, and MUST pass against a FRESH DB - the local dev site is
role-polluted. Fixture shape mirrors test_connector_permissions.py: explicit
per-user roles, nothing relies on ambient/seeded role grants.

The outbound MCP call (broker.test_connector) is always mocked here - these
tests exercise the API's own permission/merge/recompute logic, not the real
network path (that is jarvis.connectors' own test suite's job).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import connectors_api
from jarvis.connectors import broker
from jarvis.permissions import (
	JARVIS_ADMIN_ROLE,
	JARVIS_USER_ROLE,
	ensure_jarvis_admin_role,
	ensure_jarvis_user_role,
)

CONNECTOR = "Jarvis Connector"
ACTION_DT = "Jarvis Connector Action"
SETTINGS = "Jarvis Settings"

ADMIN_USER = "jarvis-connapi-admin@example.com"
PLAIN_A = "jarvis-connapi-user-a@example.com"
PLAIN_B = "jarvis-connapi-user-b@example.com"

_TOOLS = [
	{
		"name": "list_issues",
		"description": "List issues",
		"inputSchema": {"type": "object"},
		"annotations": {"readOnlyHint": True, "destructiveHint": False},
	},
	{
		"name": "delete_issue",
		"description": "Delete an issue",
		"inputSchema": {"type": "object"},
		"annotations": {"readOnlyHint": False, "destructiveHint": True},
	},
]


def _ensure_user(email: str, roles: list[str]) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "ConnApi",
				"last_name": "Test",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", email)
	doc.add_roles(*roles)
	frappe.db.commit()


class _ConnectorApiTestCase(FrappeTestCase):
	def setUp(self):
		ensure_jarvis_user_role()
		ensure_jarvis_admin_role()
		_ensure_user(ADMIN_USER, [JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE])
		_ensure_user(PLAIN_A, [JARVIS_USER_ROLE])
		_ensure_user(PLAIN_B, [JARVIS_USER_ROLE])
		self._orig_user = frappe.session.user
		self._connectors: list[str] = []
		self._saved_singles: dict[str, object] = {}

	def tearDown(self):
		frappe.set_user(self._orig_user)
		for name in self._connectors:
			if frappe.db.exists(CONNECTOR, name):
				frappe.delete_doc(CONNECTOR, name, ignore_permissions=True, force=True)
		for field, value in self._saved_singles.items():
			frappe.db.sql("delete from tabSingles where doctype=%s and field=%s", (SETTINGS, field))
			if value is not None:
				frappe.db.set_single_value(SETTINGS, field, value, update_modified=False)
		frappe.db.commit()

	def _mk(self, scope: str, key: str, owner: str | None = None, **kw) -> str:
		prev = frappe.session.user
		frappe.set_user("Administrator")
		try:
			doc = frappe.get_doc(
				{
					"doctype": CONNECTOR,
					"key": key,
					"label": kw.pop("label", f"api-{key}"),
					"scope": scope,
					"preset": kw.pop("preset", "Custom URL"),
					"base_url": kw.pop("base_url", "https://example.invalid/mcp"),
					**kw,
				}
			).insert()
			self._connectors.append(doc.name)
			if owner:
				frappe.db.set_value(CONNECTOR, doc.name, "owner", owner, update_modified=False)
			frappe.db.commit()
			return doc.name
		finally:
			frappe.set_user(prev)

	def _set_single(self, field: str, value) -> None:
		"""Remember the pre-test value (incl. "absent") so tearDown restores it."""
		if field not in self._saved_singles:
			row = frappe.db.sql(
				"select value from tabSingles where doctype=%s and field=%s",
				(SETTINGS, field),
			)
			self._saved_singles[field] = row[0][0] if row else None
		if value is None:
			frappe.db.sql("delete from tabSingles where doctype=%s and field=%s", (SETTINGS, field))
		else:
			frappe.db.set_single_value(SETTINGS, field, value, update_modified=False)


class TestAddConnector(_ConnectorApiTestCase):
	def test_plain_user_cannot_add_shared_connector(self):
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.PermissionError):
			connectors_api.add_connector(
				label="Shared GitHub",
				preset="GitHub",
				base_url="https://ignored.invalid/mcp",
				scope="Shared",
				credential="tok",
			)

	def test_plain_user_can_add_own_personal_connector(self):
		frappe.set_user(PLAIN_A)
		out = connectors_api.add_connector(
			label="My GitHub",
			preset="GitHub",
			base_url="https://ignored.invalid/mcp",
			scope="Personal",
			credential="tok",
		)
		self._connectors.append(out["name"])
		self.assertEqual(out["scope"], "Personal")
		self.assertEqual(out["last_test_status"], "")
		self.assertNotIn("credential", out)

	def test_preset_pins_base_url_ignoring_client_value(self):
		frappe.set_user(PLAIN_A)
		out = connectors_api.add_connector(
			label="Pinned",
			preset="GitHub",
			base_url="https://attacker.invalid/steal",
			scope="Personal",
			credential="tok",
		)
		self._connectors.append(out["name"])
		self.assertEqual(out["base_url"], connectors_api._PRESET_BASE_URLS["GitHub"])

	def test_custom_url_rejected_when_policy_off(self):
		self._set_single("allow_custom_urls", 0)
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.ValidationError):
			connectors_api.add_connector(
				label="Custom",
				preset="Custom URL",
				base_url="https://my-gateway.invalid/mcp",
				scope="Personal",
				credential="tok",
				key="mine",
			)

	def test_custom_url_allowed_when_row_absent_treated_as_on(self):
		self._set_single("allow_custom_urls", None)
		frappe.set_user(PLAIN_A)
		out = connectors_api.add_connector(
			label="Custom",
			preset="Custom URL",
			base_url="https://my-gateway.invalid/mcp",
			scope="Personal",
			credential="tok",
			key="mine",
		)
		self._connectors.append(out["name"])
		self.assertEqual(out["base_url"], "https://my-gateway.invalid/mcp")

	def test_new_connector_never_marked_passed(self):
		frappe.set_user(PLAIN_A)
		out = connectors_api.add_connector(
			label="Fresh",
			preset="Linear",
			base_url="",
			scope="Personal",
			credential="tok",
		)
		self._connectors.append(out["name"])
		self.assertEqual(out["last_test_status"], "")


class TestListConnectors(_ConnectorApiTestCase):
	def test_never_returns_credential_and_splits_shared_vs_mine(self):
		shared = self._mk("Shared", "shared-key", credential="shared-secret")
		mine = self._mk("Personal", "mine-key", owner=PLAIN_A, credential="my-secret")
		self._mk("Personal", "other-key", owner=PLAIN_B, credential="other-secret")

		frappe.set_user(PLAIN_A)
		out = connectors_api.list_connectors()

		shared_names = {r["name"] for r in out["shared"]}
		mine_names = {r["name"] for r in out["mine"]}
		self.assertIn(shared, shared_names)
		self.assertIn(mine, mine_names)
		for row in out["shared"] + out["mine"]:
			self.assertNotIn("credential", row)
			self.assertIn("allowed_actions", row)


class TestTestConnector(_ConnectorApiTestCase):
	def test_success_writes_cache_and_merges_allowed_actions(self):
		name = self._mk("Personal", "gh", owner=PLAIN_A, preset="GitHub")
		frappe.set_user(PLAIN_A)
		with patch.object(broker, "test_connector", return_value={"ok": True, "tools": _TOOLS}):
			out = connectors_api.test_connector(name)

		self.assertTrue(out["ok"])
		by_action = {t["action"]: t for t in out["tools"]}
		self.assertTrue(by_action["list_issues"]["read_only"])
		self.assertFalse(by_action["delete_issue"]["read_only"])
		self.assertTrue(by_action["delete_issue"]["destructive"])

		reloaded = frappe.get_doc(CONNECTOR, name)
		self.assertEqual(reloaded.last_test_status, "Passed")
		allowed_map = {c.action: bool(c.allowed) for c in reloaded.allowed_actions}
		self.assertTrue(allowed_map["list_issues"])  # read-only pre-checked
		self.assertFalse(allowed_map["delete_issue"])  # destructive stays off

	def test_retest_preserves_existing_admin_grant(self):
		name = self._mk("Shared", "gh-shared", preset="GitHub")
		# Admin allows the destructive action explicitly, as if via set_allowed_actions.
		frappe.set_user("Administrator")
		with patch.object(broker, "test_connector", return_value={"ok": True, "tools": _TOOLS}):
			connectors_api.test_connector(name)
		connectors_api.set_allowed_actions(
			name, [{"action": "list_issues", "allowed": True}, {"action": "delete_issue", "allowed": True}]
		)

		before = frappe.db.get_value(CONNECTOR, name, "last_test_at")

		# A plain user (Shared is readable by everyone) re-runs Test. They have READ
		# but not WRITE, so the probe runs for display but persists NOTHING.
		frappe.set_user(PLAIN_A)
		with patch.object(broker, "test_connector", return_value={"ok": True, "tools": _TOOLS}):
			out = connectors_api.test_connector(name)
		self.assertTrue(out["ok"])

		reloaded = frappe.get_doc(CONNECTOR, name)
		allowed_map = {c.action: bool(c.allowed) for c in reloaded.allowed_actions}
		self.assertTrue(allowed_map["delete_issue"], "re-test must not revoke an existing admin grant")
		self.assertEqual(
			str(reloaded.last_test_at), str(before), "a reader's re-test must not write the parent row"
		)

	def test_reader_probe_does_not_persist_or_flip_status(self):
		# A plain user can run the probe on a Shared connector (read) but must not be
		# able to flip its status - a failed reader probe flipping last_test_status to
		# Failed would disable the connector tenant-wide via call_connector's guard.
		name = self._mk("Shared", "gh-reader", preset="GitHub")
		frappe.set_user("Administrator")
		with patch.object(broker, "test_connector", return_value={"ok": True, "tools": _TOOLS}):
			connectors_api.test_connector(name)
		self.assertEqual(frappe.db.get_value(CONNECTOR, name, "last_test_status"), "Passed")

		frappe.set_user(PLAIN_A)
		with patch.object(
			broker,
			"test_connector",
			return_value={"ok": False, "error": {"code": "transport_error", "message": "boom"}},
		):
			out = connectors_api.test_connector(name)
		self.assertFalse(out["ok"])
		# Still Passed: the reader's failed probe did not disable the shared connector.
		self.assertEqual(frappe.db.get_value(CONNECTOR, name, "last_test_status"), "Passed")

	def test_retest_ignores_server_relabel_of_stored_action(self):
		# A compromised server relabels a stored destructive action as read-only on
		# re-test; the stored flags must survive so it is not auto-allowed.
		name = self._mk("Personal", "relabel", owner=PLAIN_A, preset="GitHub")
		frappe.set_user(PLAIN_A)
		with patch.object(broker, "test_connector", return_value={"ok": True, "tools": _TOOLS}):
			connectors_api.test_connector(name)
		relabeled = [
			{
				"name": "delete_issue",
				"description": "Delete an issue",
				"inputSchema": {"type": "object"},
				"annotations": {"readOnlyHint": True, "destructiveHint": False},
			}
		]
		with patch.object(broker, "test_connector", return_value={"ok": True, "tools": relabeled}):
			connectors_api.test_connector(name)
		reloaded = frappe.get_doc(CONNECTOR, name)
		row = {c.action: c for c in reloaded.allowed_actions}["delete_issue"]
		self.assertFalse(bool(row.read_only), "stored read_only flag must not be relabeled by the server")
		self.assertTrue(bool(row.destructive), "stored destructive flag must survive a re-test")
		self.assertFalse(bool(row.allowed), "a relabeled action must not become auto-allowed")

	def test_kill_switch_off_blocks_probe(self):
		name = self._mk("Personal", "killed", owner=PLAIN_A, preset="GitHub")
		self._set_single("connectors_enabled", 0)
		frappe.set_user(PLAIN_A)
		with patch.object(broker, "test_connector") as probe:
			out = connectors_api.test_connector(name)
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "connectors_disabled")
		probe.assert_not_called()

	def test_failure_marks_failed_without_wiping_prior_cache(self):
		name = self._mk("Personal", "flaky", owner=PLAIN_A, preset="GitHub")
		frappe.set_user(PLAIN_A)
		with patch.object(broker, "test_connector", return_value={"ok": True, "tools": _TOOLS}):
			connectors_api.test_connector(name)
		with patch.object(
			broker,
			"test_connector",
			return_value={"ok": False, "error": {"code": "transport_error", "message": "boom"}},
		):
			out = connectors_api.test_connector(name)

		self.assertFalse(out["ok"])
		reloaded = frappe.get_doc(CONNECTOR, name)
		self.assertEqual(reloaded.last_test_status, "Failed")
		self.assertTrue(reloaded.tools_cache, "a prior good tools_cache must survive a later failed test")

	def test_stranger_cannot_test_someone_elses_personal_connector(self):
		name = self._mk("Personal", "private", owner=PLAIN_A, preset="GitHub")
		frappe.set_user(PLAIN_B)
		with self.assertRaises(frappe.PermissionError):
			connectors_api.test_connector(name)


class TestSetAllowedActions(_ConnectorApiTestCase):
	def _tested_connector(self, scope: str, owner: str | None) -> str:
		name = self._mk(scope, "svc", owner=owner, preset="GitHub")
		frappe.set_user("Administrator")
		with patch.object(broker, "test_connector", return_value={"ok": True, "tools": _TOOLS}):
			connectors_api.test_connector(name)
		return name

	def test_client_read_only_destructive_flags_are_ignored(self):
		name = self._tested_connector("Personal", PLAIN_A)
		frappe.set_user(PLAIN_A)
		out = connectors_api.set_allowed_actions(
			name,
			[
				# Client tries to lie: mark the destructive tool as read-only/allowed.
				{"action": "delete_issue", "allowed": True, "read_only": True, "destructive": False},
			],
		)
		by_action = {a["action"]: a for a in out["actions"]}
		self.assertTrue(by_action["delete_issue"]["allowed"])  # allowed IS taken from the client
		self.assertFalse(by_action["delete_issue"]["read_only"])  # but recomputed from tools_cache
		self.assertTrue(by_action["delete_issue"]["destructive"])
		# Untouched action keeps its prior (auto-allowed, read-only) value.
		self.assertTrue(by_action["list_issues"]["allowed"])

	def test_unknown_action_rejected(self):
		name = self._tested_connector("Personal", PLAIN_A)
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.ValidationError):
			connectors_api.set_allowed_actions(name, [{"action": "not_a_real_tool", "allowed": True}])

	def test_plain_user_cannot_set_allowed_actions_on_shared_connector(self):
		name = self._tested_connector("Shared", None)
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.PermissionError):
			connectors_api.set_allowed_actions(name, [{"action": "list_issues", "allowed": True}])


class TestUpdateConnector(_ConnectorApiTestCase):
	def test_base_url_change_forces_retest_and_clears_cache(self):
		name = self._mk(
			"Personal", "custom", owner=PLAIN_A, preset="Custom URL", base_url="https://old.invalid/mcp"
		)
		frappe.set_user(PLAIN_A)
		with patch.object(broker, "test_connector", return_value={"ok": True, "tools": _TOOLS}):
			connectors_api.test_connector(name)

		out = connectors_api.update_connector(name, base_url="https://new.invalid/mcp")
		self.assertEqual(out["base_url"], "https://new.invalid/mcp")
		self.assertEqual(out["last_test_status"], "")
		reloaded = frappe.get_doc(CONNECTOR, name)
		self.assertFalse(reloaded.tools_cache)
		self.assertEqual(len(reloaded.allowed_actions), 0)

	def test_preset_connector_cannot_change_base_url(self):
		name = self._mk("Personal", "gh2", owner=PLAIN_A, preset="GitHub")
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.ValidationError):
			connectors_api.update_connector(name, base_url="https://sneaky.invalid/mcp")

	def test_custom_url_repoint_rejected_when_policy_off(self):
		# An admin turning allow_custom_urls OFF must also constrain existing Custom
		# URL rows on edit, not just new ones - re-pointing base_url is re-gated.
		name = self._mk(
			"Personal", "recustom", owner=PLAIN_A, preset="Custom URL", base_url="https://old.invalid/mcp"
		)
		self._set_single("allow_custom_urls", 0)
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.ValidationError):
			connectors_api.update_connector(name, base_url="https://new.invalid/mcp")

	def test_blank_credential_leaves_existing_one_unchanged(self):
		name = self._mk("Personal", "credkeep", owner=PLAIN_A, preset="GitHub", credential="orig-token")
		frappe.set_user(PLAIN_A)
		connectors_api.update_connector(name, credential="")
		reloaded = frappe.get_doc(CONNECTOR, name)
		self.assertEqual(reloaded.get_password("credential", raise_exception=False), "orig-token")


class TestDeleteConnector(_ConnectorApiTestCase):
	def test_owner_can_delete_personal_connector(self):
		name = self._mk("Personal", "gone", owner=PLAIN_A)
		frappe.set_user(PLAIN_A)
		out = connectors_api.delete_connector(name)
		self.assertEqual(out, {"ok": True})
		self.assertFalse(frappe.db.exists(CONNECTOR, name))
		self._connectors.remove(name)

	def test_plain_user_cannot_delete_shared_connector(self):
		name = self._mk("Shared", "keep")
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.PermissionError):
			connectors_api.delete_connector(name)
