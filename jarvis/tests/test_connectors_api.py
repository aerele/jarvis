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

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis.chat import connectors_api
from jarvis.connectors import broker, catalog, mcp_oauth_store, oauth
from jarvis.connectors.mcp_client import McpClient
from jarvis.connectors.mcp_oauth import canonical_resource, pkce_challenge
from jarvis.connectors.mcp_oauth.transport import HttpResult
from jarvis.permissions import (
	JARVIS_ADMIN_ROLE,
	JARVIS_USER_ROLE,
	ensure_jarvis_admin_role,
	ensure_jarvis_user_role,
)

CONNECTOR = "Jarvis Connector"
ACTION_DT = "Jarvis Connector Action"
SETTINGS = "Jarvis Settings"
CLIENT_DT = "MCP OAuth Client"
TOKEN_DT = "MCP OAuth Token"

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
		self._connected_apps: list[str] = []
		self._token_caches: list[str] = []
		self._saved_singles: dict[str, object] = {}
		# The feature flag defaults OFF on a fresh DB, and test_connector now refuses
		# to probe when it is off. Every test here exercises an ENABLED workspace, so
		# turn it on (tearDown restores the original via _saved_singles).
		self._set_single("connectors_enabled", 1)

	def tearDown(self):
		frappe.set_user(self._orig_user)
		for name in self._connectors:
			# Defensive: the connector's own on_trash purges these, but a test that
			# asserts on the cascade should not depend on the cascade for cleanup.
			mcp_oauth_store.purge_connector(name)
			if frappe.db.exists(CONNECTOR, name):
				frappe.delete_doc(CONNECTOR, name, ignore_permissions=True, force=True)
		for name in self._token_caches:
			if frappe.db.exists("Token Cache", name):
				frappe.delete_doc("Token Cache", name, ignore_permissions=True, force=True)
		for name in self._connected_apps:
			if frappe.db.exists("Connected App", name):
				frappe.delete_doc("Connected App", name, ignore_permissions=True, force=True)
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

	def _mk_connected_app(self, provider_name: str = "GitHub") -> str:
		"""A minimal Connected App fixture - never touches a real provider (no
		network happens on insert; ``authorization_uri``/``token_uri`` are just
		strings until a flow actually runs)."""
		prev = frappe.session.user
		frappe.set_user("Administrator")
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Connected App",
					"name": provider_name,
					"provider_name": provider_name,
					"client_id": "test-client-id",
					"client_secret": "test-client-secret",
					"authorization_uri": "https://example.invalid/authorize",
					"token_uri": "https://example.invalid/token",
				}
			).insert(ignore_permissions=True)
			self._connected_apps.append(doc.name)
			frappe.db.commit()
			return doc.name
		finally:
			frappe.set_user(prev)

	def _mk_connected_app_named(self, name: str, provider_name: str) -> str:
		"""A Connected App whose docname differs from its provider_name, so two apps
		can share one provider_name (Frappe does not enforce uniqueness on it)."""
		prev = frappe.session.user
		frappe.set_user("Administrator")
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Connected App",
					"name": name,
					"provider_name": provider_name,
					"client_id": "test-client-id",
					"client_secret": "test-client-secret",
					"authorization_uri": "https://example.invalid/authorize",
					"token_uri": "https://example.invalid/token",
				}
			).insert(ignore_permissions=True)
			self._connected_apps.append(doc.name)
			frappe.db.commit()
			return doc.name
		finally:
			frappe.set_user(prev)

	def _mk_token_cache(
		self,
		connected_app: str,
		user: str,
		access_token: str = "live-token",
		expires_in: int = 3600,
		refresh_token: str | None = None,
	) -> str:
		"""A Token Cache for ``user`` - bypasses the real authorize/exchange dance
		entirely, which is exactly what ``get_active_token``/``get_token_cache``
		read. ``expires_in=0`` + no ``refresh_token`` models a classic GitHub
		OAuth-App token (long-lived, non-refreshable), which Frappe otherwise treats
		as instantly expired."""
		prev = frappe.session.user
		frappe.set_user("Administrator")
		try:
			fields = {
				"doctype": "Token Cache",
				"connected_app": connected_app,
				"user": user,
				"access_token": access_token,
				"token_type": "Bearer",
				"expires_in": expires_in,
			}
			if refresh_token is not None:
				fields["refresh_token"] = refresh_token
			doc = frappe.get_doc(fields).insert(ignore_permissions=True)
			self._token_caches.append(doc.name)
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

	def test_label_derived_from_preset_when_omitted(self):
		frappe.set_user(PLAIN_A)
		out = connectors_api.add_connector(
			preset="GitHub",
			base_url="",
			scope="Personal",
			credential="tok",
		)
		self._connectors.append(out["name"])
		self.assertEqual(out["label"], "GitHub")
		self.assertEqual(out["key"], "github")

	def test_label_and_key_derived_from_host_for_custom_url(self):
		self._set_single("allow_custom_urls", 1)
		frappe.set_user(PLAIN_A)
		out = connectors_api.add_connector(
			preset="Custom URL",
			base_url="https://mcp.example.com/mcp",
			scope="Personal",
			credential="tok",
		)
		self._connectors.append(out["name"])
		self.assertEqual(out["label"], "mcp.example.com")
		self.assertEqual(out["key"], "mcp_example_com")


class TestDisabledCatalogPreset(_ConnectorApiTestCase):
	"""Plaid ships disabled (its endpoint speaks the older transport). Disabled
	means "not offered for new connectors", NOT "existing rows are frozen": Frappe
	validates the ``preset`` Select on every save, so the name has to stay in the
	options or a row already on it can never be relabelled, disabled, or given a
	new credential again."""

	DISABLED_PRESET = "Plaid"

	def test_the_preset_is_a_real_disabled_catalog_entry(self):
		provider = catalog.by_name(self.DISABLED_PRESET)
		self.assertIsNotNone(provider)
		self.assertFalse(provider.enabled)

	def test_a_row_on_a_disabled_preset_still_saves(self):
		# Created directly: add_connector refuses a disabled preset, which is the
		# point of disabling it. This models the row that predates the change.
		name = self._mk(
			"Personal",
			"plaid-legacy",
			owner=PLAIN_A,
			preset=self.DISABLED_PRESET,
			base_url=catalog.base_urls()[self.DISABLED_PRESET],
		)
		frappe.set_user(PLAIN_A)

		out = connectors_api.update_connector(name, label="Renamed by owner")

		self.assertEqual(out["label"], "Renamed by owner")
		self.assertEqual(frappe.db.get_value(CONNECTOR, name, "preset"), self.DISABLED_PRESET)

	def test_a_disabled_preset_cannot_back_a_new_connector(self):
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.ValidationError):
			connectors_api.add_connector(preset=self.DISABLED_PRESET, base_url="", scope="Personal")

	def test_the_picker_never_offers_it(self):
		frappe.set_user(PLAIN_A)
		names = {row["name"] for row in connectors_api.list_connectors()["catalog"]}
		self.assertNotIn(self.DISABLED_PRESET, names)


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


class TestListConnectorsCatalog(_ConnectorApiTestCase):
	"""The SPA builds its whole preset picker from ``list_connectors()["catalog"]``,
	so this is a contract, not an implementation detail."""

	def test_ships_the_enabled_catalog_and_no_secrets(self):
		frappe.set_user(PLAIN_A)
		entries = connectors_api.list_connectors()["catalog"]

		self.assertEqual(len(entries), sum(1 for p in catalog.PROVIDERS if p.enabled))
		self.assertEqual(len(entries), 30, "the SPA is built against this count")

		names = {e["name"] for e in entries}
		self.assertIn("Razorpay", names)
		self.assertNotIn("Plaid", names, "a disabled catalog entry is never offered")
		self.assertNotIn("Custom URL", names, "Custom URL is a flow, not a provider")

		allowed = {"name", "key", "auth", "category", "logo", "help_url", "hint"}
		for entry in entries:
			self.assertEqual(set(entry), allowed, entry.get("name"))
			self.assertIn(entry["auth"], {"dcr", "static", "token", "open", "connected_app"})

	def test_rows_carry_the_flow_the_spa_should_render(self):
		github = self._mk("Personal", "gh-class", owner=PLAIN_A, preset="GitHub")
		custom = self._mk("Personal", "custom-class", owner=PLAIN_A, preset="Custom URL")
		razorpay = self._mk("Personal", "rzp-class", owner=PLAIN_A, preset="Razorpay")
		open_row = self._mk("Personal", "ms-class", owner=PLAIN_A, preset="Microsoft Learn")

		frappe.set_user(PLAIN_A)
		rows = {r["name"]: r for r in connectors_api.list_connectors()["mine"]}
		self.assertEqual(rows[github]["auth_class"], "connected_app")
		self.assertEqual(rows[custom]["auth_class"], "custom")
		self.assertEqual(rows[razorpay]["auth_class"], "dcr")
		self.assertEqual(rows[open_row]["auth_class"], "open")


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


# --------------------------------------------------------------------------- #
# OAuth tier v1 (OAUTH_CONNECTORS_DESIGN.md) - GitHub flagship.
#
# The real authorize/token-exchange dance is never exercised here - only
# ``jarvis.connectors.oauth`` (this module's seam into Connected App) and the
# API surface built around it. A Connected App/Token Cache fixture is real
# (both are plain Frappe doctypes, no provider is ever contacted for it), so
# ``get_active_token``/``get_token_cache`` run for real against them.
# --------------------------------------------------------------------------- #
class TestOauthModule(_ConnectorApiTestCase):
	def test_is_oauth_true_for_oauth_auth_method(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-oauth", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		doc = frappe.get_doc(CONNECTOR, name)
		self.assertTrue(oauth.is_oauth(doc))

	def test_is_oauth_false_for_api_key_and_unset(self):
		name = self._mk("Personal", "gh-key", owner=PLAIN_A, preset="GitHub")
		doc = frappe.get_doc(CONNECTOR, name)
		self.assertFalse(oauth.is_oauth(doc))
		self.assertFalse(oauth.is_oauth({}))

	def test_resolve_access_token_returns_none_without_connected_app(self):
		# A bare dict-like row is enough here - Jarvis Connector's own
		# mandatory_depends_on already forbids saving auth_method=OAuth with no
		# connected_app, so this exercises resolve_access_token's OWN "unset"
		# guard rather than a state the DocType itself would ever persist.
		self.assertIsNone(oauth.resolve_access_token({"auth_method": "OAuth"}))

	def test_resolve_access_token_returns_none_without_a_token_cache(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-no-cache", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(CONNECTOR, name)
		self.assertIsNone(oauth.resolve_access_token(doc))

	def test_resolve_access_token_returns_the_live_token(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-live", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		self._mk_token_cache(app, PLAIN_A, access_token="secret-token")
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(CONNECTOR, name)
		self.assertEqual(oauth.resolve_access_token(doc), "secret-token")

	def test_nonrefreshable_token_is_used_as_is_never_refreshed(self):
		"""A classic GitHub OAuth-App token has expires_in=0 and no refresh token,
		so Frappe reports it expired one second in. resolve_access_token must return
		the stored token directly, NOT route it through get_active_token (whose
		doomed refresh returns None and leaks the client secret). We assert this by
		making get_active_token blow up: it must never be reached."""
		app_name = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal",
			"gh-noref",
			owner=PLAIN_A,
			preset="GitHub",
			auth_method="OAuth",
			connected_app=app_name,
		)
		self._mk_token_cache(app_name, PLAIN_A, access_token="long-lived", expires_in=0, refresh_token=None)
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(CONNECTOR, name)

		def _boom(*a, **k):
			raise AssertionError("get_active_token must not be called for a non-refreshable token")

		with patch(
			"frappe.integrations.doctype.connected_app.connected_app.ConnectedApp.get_active_token",
			_boom,
		):
			self.assertEqual(oauth.resolve_access_token(doc), "long-lived")


class TestBrokerCredentialOauth(_ConnectorApiTestCase):
	def test_credential_raises_connector_not_ready_without_a_token(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-broker", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(CONNECTOR, name)
		with self.assertRaises(broker._BrokerError) as ctx:
			broker._credential(doc)
		self.assertEqual(ctx.exception.code, "connector_not_ready")

	def test_credential_returns_the_live_token(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-broker-ok", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		self._mk_token_cache(app, PLAIN_A, access_token="secret-token")
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(CONNECTOR, name)
		self.assertEqual(broker._credential(doc), "secret-token")

	def test_credential_unchanged_for_api_key_row(self):
		name = self._mk("Personal", "gh-apikey", owner=PLAIN_A, preset="GitHub", credential="my-pat")
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(CONNECTOR, name)
		self.assertEqual(broker._credential(doc), "my-pat")


class TestAddConnectorOauth(_ConnectorApiTestCase):
	def test_oauth_ignores_credential_and_sets_connected_app_server_side(self):
		app = self._mk_connected_app("GitHub")
		frappe.set_user(PLAIN_A)
		out = connectors_api.add_connector(
			preset="GitHub",
			base_url="https://attacker.invalid/steal",
			scope="Personal",
			credential="a-pasted-secret",
			auth_method="OAuth",
		)
		self._connectors.append(out["name"])
		self.assertEqual(out["auth_method"], "OAuth")
		self.assertNotIn("credential", out)
		reloaded = frappe.get_doc(CONNECTOR, out["name"])
		self.assertEqual(reloaded.connected_app, app)
		self.assertFalse(reloaded.get_password("credential", raise_exception=False))

	def test_oauth_create_succeeds_without_base_url_or_credential(self):
		"""The SPA's Connect flow posts only preset + scope + auth_method (no
		base_url, no credential). add_connector must accept that - base_url and
		credential are defaulted - or the only OAuth create path 500s before the
		body runs."""
		app = self._mk_connected_app("GitHub")
		frappe.set_user(PLAIN_A)
		out = connectors_api.add_connector(preset="GitHub", scope="Personal", auth_method="OAuth")
		self._connectors.append(out["name"])
		self.assertEqual(out["auth_method"], "OAuth")
		reloaded = frappe.get_doc(CONNECTOR, out["name"])
		self.assertEqual(reloaded.connected_app, app)

	def test_oauth_without_a_configured_connected_app_is_rejected(self):
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.ValidationError):
			connectors_api.add_connector(
				preset="GitHub",
				base_url="",
				scope="Personal",
				credential="",
				auth_method="OAuth",
			)

	def test_invalid_auth_method_rejected(self):
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.ValidationError):
			connectors_api.add_connector(
				preset="GitHub",
				base_url="",
				scope="Personal",
				credential="tok",
				auth_method="Bearer Token",
			)


class TestConnectorOauthFieldGuard(_ConnectorApiTestCase):
	"""The controller must stop a raw DocType write (a Jarvis User has create/write)
	from steering an OAuth row at an arbitrary Connected App, and must strip a
	Connected App off a non-OAuth row - the API's server-side pinning is not the
	only write path."""

	def _insert_as(self, user, **fields):
		prev = frappe.session.user
		frappe.set_user(user)
		try:
			doc = frappe.get_doc(
				{
					"doctype": CONNECTOR,
					"scope": "Personal",
					"base_url": "https://api.githubcopilot.com/mcp/",
					"label": "GH",
					"preset": "GitHub",
					**fields,
				}
			).insert()  # no ignore_permissions - runs the user-facing guard
			self._connectors.append(doc.name)
			return doc
		finally:
			frappe.set_user(prev)

	def test_oauth_row_cannot_point_at_an_arbitrary_connected_app(self):
		self._mk_connected_app("GitHub")  # the preset's real app
		other = self._mk_connected_app("Other")  # a stranger app the user should not reach
		with self.assertRaises(frappe.PermissionError):
			self._insert_as(PLAIN_A, key="gh-evil", auth_method="OAuth", connected_app=other)

	def test_key_row_never_keeps_a_connected_app_link(self):
		app = self._mk_connected_app("GitHub")
		doc = self._insert_as(PLAIN_A, key="gh-key", auth_method="API Key", connected_app=app)
		self.assertFalse(doc.connected_app)

	def test_legit_oauth_row_resaves_even_if_preset_now_resolves_elsewhere(self):
		"""A row created against app A must not lock when a second same-provider app
		later wins the resolver (provider_name is not unique). Resave with the link
		unchanged must NOT re-derive-and-403, or the row can never be disabled."""
		app_a = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-resave", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app_a
		)
		# A second Connected App with the same provider_name now exists; the resolver
		# (get_all limit=1) may return it instead of app_a.
		self._mk_connected_app_named("GitHub-2", provider_name="GitHub")
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(CONNECTOR, name)
		doc.label = "renamed by owner"
		doc.save()  # no ignore_permissions - the guard runs; must not throw
		self.assertEqual(frappe.get_doc(CONNECTOR, name).connected_app, app_a)

	def test_raw_write_cannot_repoint_a_catalog_preset(self):
		"""The API ignores a caller's base_url for a preset; the controller must too,
		or a raw DocType write could aim a preset row at any public host and have
		the credential sent there. Custom URL is the only caller-chosen endpoint."""
		from jarvis.connectors import catalog

		doc = self._insert_as(
			PLAIN_A,
			key="rzp-evil",
			preset="Razorpay",
			auth_method="API Key",
			credential="tok",
			base_url="https://attacker.invalid/steal",
		)
		self.assertEqual(doc.base_url, catalog.base_urls()["Razorpay"])
		self.assertEqual(frappe.get_doc(CONNECTOR, doc.name).base_url, catalog.base_urls()["Razorpay"])


class TestListConnectorsOauth(_ConnectorApiTestCase):
	def test_annotates_oauth_configured_and_connected(self):
		app = self._mk_connected_app("GitHub")
		connected = self._mk(
			"Personal", "gh-connected", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		self._mk_token_cache(app, PLAIN_A)
		not_connected = self._mk(
			"Personal",
			"gh-not-connected",
			owner=PLAIN_A,
			preset="GitHub",
			auth_method="OAuth",
			connected_app=app,
		)

		frappe.set_user(PLAIN_A)
		out = connectors_api.list_connectors()
		by_name = {r["name"]: r for r in out["mine"]}

		self.assertTrue(by_name[connected]["oauth_configured"])
		self.assertTrue(by_name[connected]["oauth_connected"])
		self.assertTrue(by_name[not_connected]["oauth_configured"])
		self.assertFalse(by_name[not_connected]["oauth_connected"])
		self.assertNotIn("connected_app", by_name[connected])

	def test_api_key_rows_are_not_annotated_with_oauth_fields(self):
		name = self._mk("Personal", "gh-plain", owner=PLAIN_A, preset="GitHub")
		frappe.set_user(PLAIN_A)
		out = connectors_api.list_connectors()
		row = next(r for r in out["mine"] if r["name"] == name)
		self.assertEqual(row["auth_method"], "API Key")
		self.assertNotIn("oauth_connected", row)


class TestTestConnectorOauth(_ConnectorApiTestCase):
	def test_no_token_returns_connector_not_ready_without_probing(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-test", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		frappe.set_user(PLAIN_A)
		with patch.object(broker, "test_connector") as probe:
			out = connectors_api.test_connector(name)
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "connector_not_ready")
		probe.assert_not_called()

	def test_live_token_reaches_the_broker_probe(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-test-ok", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		self._mk_token_cache(app, PLAIN_A, access_token="secret-token")
		frappe.set_user(PLAIN_A)
		with patch.object(broker, "test_connector", return_value={"ok": True, "tools": _TOOLS}) as probe:
			out = connectors_api.test_connector(name)
		self.assertTrue(out["ok"])
		probe.assert_called_once()


class TestConnectOauth(_ConnectorApiTestCase):
	def test_returns_authorize_url_for_oauth_row(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-connect", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		frappe.set_user(PLAIN_A)
		out = connectors_api.connect_oauth(name)
		self.assertTrue(out["ok"])
		self.assertIn("https://example.invalid/authorize", out["url"])
		# initiate_web_application_flow creates the user's Token Cache to hold state.
		self._token_caches.append(f"{app}-{PLAIN_A}")

	def test_started_but_unfinished_flow_is_not_reported_as_connected(self):
		# initiate_web_application_flow creates the Token Cache up front to hold
		# `state`, before the user ever reaches the provider's consent screen - it
		# must NOT read as "connected" (a bare get_token_cache truthy check would
		# say the opposite; see _oauth_status).
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal",
			"gh-unfinished",
			owner=PLAIN_A,
			preset="GitHub",
			auth_method="OAuth",
			connected_app=app,
		)
		frappe.set_user(PLAIN_A)
		connectors_api.connect_oauth(name)
		self._token_caches.append(f"{app}-{PLAIN_A}")

		out = connectors_api.list_connectors()
		row = next(r for r in out["mine"] if r["name"] == name)
		self.assertTrue(row["oauth_configured"])
		self.assertFalse(row["oauth_connected"], "a state-only Token Cache is not a completed sign-in")

	def test_not_oauth_row_returns_not_oauth_error(self):
		name = self._mk("Personal", "gh-notoauth", owner=PLAIN_A, preset="GitHub")
		frappe.set_user(PLAIN_A)
		out = connectors_api.connect_oauth(name)
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "not_oauth")

	def test_unconfigured_connected_app_returns_friendly_error(self):
		# auth_method OAuth with no connected_app can only happen on a row whose
		# Connected App was removed (or edited out-of-band) after creation - the
		# endpoint must still fail cleanly, not raise. Create with a real Connected
		# App (the DocType's own mandatory_depends_on forbids saving without one),
		# then blank it via a raw write to simulate that later state.
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-unconf", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		frappe.db.set_value(CONNECTOR, name, "connected_app", None, update_modified=False)
		frappe.set_user(PLAIN_A)
		out = connectors_api.connect_oauth(name)
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "oauth_not_configured")

	def test_stranger_cannot_connect_someone_elses_personal_connector(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-private", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		frappe.set_user(PLAIN_B)
		with self.assertRaises(frappe.PermissionError):
			connectors_api.connect_oauth(name)


class TestDisconnectOauth(_ConnectorApiTestCase):
	def test_deletes_the_current_users_token_cache(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal",
			"gh-disconnect",
			owner=PLAIN_A,
			preset="GitHub",
			auth_method="OAuth",
			connected_app=app,
		)
		tc_name = self._mk_token_cache(app, PLAIN_A)
		frappe.set_user(PLAIN_A)
		out = connectors_api.disconnect_oauth(name)
		self.assertEqual(out, {"ok": True})
		self.assertFalse(frappe.db.exists("Token Cache", tc_name))
		self._token_caches.remove(tc_name)

	def test_idempotent_when_never_connected(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-never", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		frappe.set_user(PLAIN_A)
		out = connectors_api.disconnect_oauth(name)
		self.assertEqual(out, {"ok": True})

	def test_idempotent_when_connected_app_missing(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "gh-noapp", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		frappe.db.set_value(CONNECTOR, name, "connected_app", None, update_modified=False)
		frappe.set_user(PLAIN_A)
		out = connectors_api.disconnect_oauth(name)
		self.assertEqual(out, {"ok": True})


# --------------------------------------------------------------------------- #
# Spec-compliant sign-in engine (MCP_OAUTH_CLIENT_DESIGN.md) - Custom URL rows.
#
# NO NETWORK. Every outbound hop goes through the module-level transport hooks
# (``connectors_api.MCP_OAUTH_TRANSPORT`` for the API's own hops,
# ``oauth.MCP_OAUTH_TRANSPORT`` for the broker's refresh), and every test here
# patches one of them with a scripted fake. An unscripted URL raises, so a test
# can prove a path did NOT reach the network as easily as that it did.
# --------------------------------------------------------------------------- #
MCP_BASE_URL = "https://mcp.example.invalid/mcp/"
MCP_RESOURCE = "https://mcp.example.invalid/mcp"
MCP_RM_URL = "https://mcp.example.invalid/.well-known/oauth-protected-resource/mcp"
MCP_AS_URL = "https://as.example.invalid"
MCP_AS_META_URL = "https://as.example.invalid/.well-known/oauth-authorization-server"
MCP_OIDC_META_URL = "https://as.example.invalid/.well-known/openid-configuration"
MCP_AUTHORIZE = "https://as.example.invalid/authorize"
MCP_TOKEN = "https://as.example.invalid/token"
MCP_REGISTER = "https://as.example.invalid/register"


class _StepClock:
	"""Stands in for the ``time`` module a caller reads ``monotonic`` off, handing
	out a scripted sequence (the last value repeating). Lets a test say "the
	credential took seven seconds" without spending seven seconds."""

	def __init__(self, ticks: list[float]):
		self._ticks = list(ticks)

	def monotonic(self) -> float:
		return self._ticks.pop(0) if len(self._ticks) > 1 else self._ticks[0]


class _ScriptedTransport:
	"""Stands in for the pinned transport's call shape. Returns canned results per
	URL (a list is consumed in order, the last entry repeating) and records every
	call. An unscripted URL is an AssertionError, never a real request."""

	def __init__(self, script: dict):
		self._script = {u: (list(r) if isinstance(r, list) else [r]) for u, r in script.items()}
		self.calls: list[dict] = []

	def __call__(
		self,
		url,
		*,
		method="GET",
		headers=None,
		body=None,
		connect_timeout=5.0,
		read_timeout=20.0,
		total_timeout=None,
		egress_allowed=None,
		**kwargs,
	):
		self.calls.append(
			{
				"url": url,
				"method": method,
				"headers": headers,
				"body": body,
				"total_timeout": total_timeout,
				"egress_allowed": egress_allowed,
			}
		)
		queue = self._script.get(url)
		if not queue:
			raise AssertionError(f"unscripted transport call to {url}")
		return queue.pop(0) if len(queue) > 1 else queue[0]

	def urls(self) -> list[str]:
		return [c["url"] for c in self.calls]

	def hits(self, url: str) -> int:
		return sum(1 for c in self.calls if c["url"] == url)

	def form_for(self, url: str) -> dict:
		for call in self.calls:
			if call["url"] == url and call["body"]:
				return {k: v[0] for k, v in parse_qs(call["body"].decode("utf-8")).items()}
		raise AssertionError(f"no form was posted to {url}")


def _json_result(payload, status: int = 200) -> HttpResult:
	return HttpResult(
		status=status,
		headers={"content-type": "application/json"},
		json=payload,
		text=json.dumps(payload),
	)


def _discovery_script(registration: bool = True, rm_overrides: dict | None = None) -> dict:
	"""The three hops a discovery run makes: the unauthenticated probe (401 with a
	challenge), the protected-resource document, and the sign-in service's
	metadata. The challenge names a NARROWER scope than the resource document
	advertises, so tests can prove the challenge wins."""
	challenge = f'Bearer error="invalid_request", resource_metadata="{MCP_RM_URL}", scope="repo read:org"'
	rm_doc = {
		"resource": MCP_RESOURCE,
		"authorization_servers": [MCP_AS_URL],
		"scopes_supported": ["repo", "read:org", "admin:everything"],
	}
	rm_doc.update(rm_overrides or {})
	as_doc = {
		"issuer": MCP_AS_URL,
		"authorization_endpoint": MCP_AUTHORIZE,
		"token_endpoint": MCP_TOKEN,
		"authorization_response_iss_parameter_supported": True,
	}
	if registration:
		as_doc["registration_endpoint"] = MCP_REGISTER
	return {
		MCP_BASE_URL: HttpResult(status=401, headers={"www-authenticate": challenge}, json=None, text=""),
		MCP_RM_URL: _json_result(rm_doc),
		MCP_AS_META_URL: _json_result(as_doc),
	}


# --- catalog presets, discovered against their PINNED endpoints -------------- #
# Copied from ``jarvis.connectors.catalog``'s own entries on purpose: a test that
# imported the pinned URL from the catalog could not prove the API pins it.
RAZORPAY_BASE_URL = "https://mcp.razorpay.com/mcp"
SLACK_BASE_URL = "https://mcp.slack.com/mcp"
MS_LEARN_BASE_URL = "https://learn.microsoft.com/api/mcp"
ATTACKER_BASE_URL = "https://attacker.invalid/steal"


def _discovery_script_at(base_url: str, *, registration: bool = True) -> dict:
	"""``_discovery_script`` for an arbitrary address, so a CATALOG preset's own
	pinned endpoint can be scripted. The resource document must name that same
	address in canonical form or the anti-phishing gate rejects it."""
	resource = canonical_resource(base_url)
	parsed = urlparse(resource)
	rm_url = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{parsed.path}"
	challenge = f'Bearer error="invalid_request", resource_metadata="{rm_url}", scope="read"'
	as_doc = {
		"issuer": MCP_AS_URL,
		"authorization_endpoint": MCP_AUTHORIZE,
		"token_endpoint": MCP_TOKEN,
		"authorization_response_iss_parameter_supported": True,
	}
	if registration:
		as_doc["registration_endpoint"] = MCP_REGISTER
	return {
		base_url: HttpResult(status=401, headers={"www-authenticate": challenge}, json=None, text=""),
		rm_url: _json_result(
			{"resource": resource, "authorization_servers": [MCP_AS_URL], "scopes_supported": ["read"]}
		),
		MCP_AS_META_URL: _json_result(as_doc),
	}


def _token_response(
	access="fresh-access", refresh="fresh-refresh", expires_in=3600, scope="repo", token_type="Bearer"
) -> HttpResult:
	"""A token response. A ``None`` field is OMITTED, not sent as null - that is
	the shape a real server produces when it has nothing to say about a field, and
	the shape the store's "keep what we had" rule is written for."""
	payload = {"access_token": access}
	for key, value in (
		("refresh_token", refresh),
		("expires_in", expires_in),
		("scope", scope),
		("token_type", token_type),
	):
		if value is not None:
			payload[key] = value
	return _json_result(payload)


class _McpOauthTestCase(_ConnectorApiTestCase):
	"""Fixtures for a Custom URL connector backed by the discovery engine."""

	def _mk_client(self, connector: str, *, mode: str = "dcr", client_id: str = "cid-1"):
		doc = frappe.get_doc(
			{
				"doctype": CLIENT_DT,
				"connector": connector,
				"registration_mode": mode,
				"client_id": client_id,
				"issuer": MCP_AS_URL,
				"authorization_endpoint": MCP_AUTHORIZE,
				"token_endpoint": MCP_TOKEN,
				"registration_endpoint": MCP_REGISTER if mode == "dcr" else "",
				"resource": MCP_RESOURCE,
				"scope": "repo read:org",
				"iss_param_supported": 1,
				"as_metadata": frappe.as_json(
					{"issuer": MCP_AS_URL, "authorization_response_iss_parameter_supported": True}
				),
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(CONNECTOR, connector, "mcp_oauth_client", doc.name, update_modified=False)
		frappe.db.commit()
		return doc

	def _mk_mcp_connector(
		self,
		key: str,
		*,
		owner: str = PLAIN_A,
		scope: str = "Personal",
		mode: str = "dcr",
		client_id: str = "cid-1",
		base_url: str = MCP_BASE_URL,
	) -> str:
		name = self._mk(scope, key, owner=owner, preset="Custom URL", base_url=base_url, auth_method="OAuth")
		self._mk_client(name, mode=mode, client_id=client_id)
		return name

	def _mk_mcp_token(
		self,
		connector: str,
		user: str,
		*,
		access_token: str = "live-token",
		refresh_token: str | None = None,
		expires_at=None,
		resource: str = MCP_RESOURCE,
	):
		fields = {
			"doctype": TOKEN_DT,
			"connector": connector,
			"user": user,
			"access_token": access_token,
			"resource": resource,
			"token_type": "Bearer",
		}
		if refresh_token is not None:
			fields["refresh_token"] = refresh_token
		if expires_at is not None:
			fields["expires_at"] = expires_at
		doc = frappe.get_doc(fields).insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	def _connect(self, name: str) -> tuple[str, str]:
		"""Run connect_oauth and return (authorize_url, state)."""
		out = connectors_api.connect_oauth(name)
		self.assertTrue(out.get("ok"), out)
		return out["url"], parse_qs(urlparse(out["url"]).query)["state"][0]

	def _callback(self, **params):
		"""Call the callback with a fresh response object and return it."""
		frappe.local.response = frappe._dict()
		connectors_api.mcp_oauth_callback(**params)
		return frappe.local.response

	def _reload_doc(self, name: str):
		frappe.clear_document_cache(CONNECTOR, name)
		return frappe.get_doc(CONNECTOR, name)


class TestProbeConnectorAuth(_McpOauthTestCase):
	def test_reports_where_the_user_would_sign_in(self):
		transport = _ScriptedTransport(_discovery_script())
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.probe_connector_auth(MCP_BASE_URL)

		self.assertTrue(out["ok"])
		self.assertTrue(out["needs_signin"])
		self.assertEqual(out["signin_host"], "as.example.invalid")
		self.assertEqual(out["registration"], "dcr")
		self.assertIn("repo", out["scopes"])
		# Every hop carried the operator egress policy, not None.
		for call in transport.calls:
			self.assertIs(call["egress_allowed"], broker._egress_allowed)

	def test_static_registration_when_no_self_registration_offered(self):
		transport = _ScriptedTransport(_discovery_script(registration=False))
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.probe_connector_auth(MCP_BASE_URL)
		self.assertEqual(out["registration"], "static")

	def test_address_that_answers_needs_no_signin(self):
		transport = _ScriptedTransport({MCP_BASE_URL: _json_result({"result": "ok"})})
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.probe_connector_auth(MCP_BASE_URL)
		self.assertEqual(out, {"ok": True, "needs_signin": False})

	def test_gate_failure_returns_a_stable_code_and_friendly_message(self):
		# The protected-resource document describes a DIFFERENT resource: the
		# anti-phishing gate must reject it rather than follow it anywhere.
		script = _discovery_script(rm_overrides={"resource": "https://elsewhere.invalid/mcp"})
		transport = _ScriptedTransport(script)
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.probe_connector_auth(MCP_BASE_URL)

		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "resource_mismatch")
		for word in ("OAuth", "MCP", "bearer", "PKCE", "JSON-RPC"):
			self.assertNotIn(word, out["error"]["message"])
		# It never reached the sign-in service's metadata.
		self.assertNotIn(MCP_AS_META_URL, transport.urls())

	def test_rejects_a_non_http_address_without_probing(self):
		transport = _ScriptedTransport({})
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.probe_connector_auth("file:///etc/passwd")
		self.assertEqual(out["error"]["code"], "invalid_arguments")
		self.assertEqual(transport.calls, [])

	def test_custom_urls_off_blocks_the_probe(self):
		self._set_single("allow_custom_urls", 0)
		transport = _ScriptedTransport({})
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.probe_connector_auth(MCP_BASE_URL)
		self.assertEqual(out["error"]["code"], "custom_urls_disabled")
		self.assertEqual(transport.calls, [])

	def test_kill_switch_off_blocks_the_probe(self):
		self._set_single("connectors_enabled", 0)
		transport = _ScriptedTransport({})
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.probe_connector_auth(MCP_BASE_URL)
		self.assertEqual(out["error"]["code"], "connectors_disabled")
		self.assertEqual(transport.calls, [])


class TestAddConnectorMcpOauth(_McpOauthTestCase):
	def test_dcr_registers_and_links_the_client(self):
		script = _discovery_script()
		script[MCP_REGISTER] = _json_result(
			{"client_id": "dcr-client", "client_secret": "dcr-secret"}, status=201
		)
		transport = _ScriptedTransport(script)
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.add_connector(
				preset="Custom URL", base_url=MCP_BASE_URL, scope="Personal", auth_method="OAuth"
			)
		self._connectors.append(out["name"])

		self.assertTrue(out["oauth_configured"])
		self.assertFalse(out["oauth_connected"])
		self.assertFalse(out["needs_static_client"])
		self.assertEqual(out["signin_host"], "as.example.invalid")

		row = frappe.get_doc(CONNECTOR, out["name"])
		self.assertEqual(row.mcp_oauth_client, out["name"], "the client is named after its connector")
		self.assertFalse(row.connected_app, "the Custom URL path must never take the preset link")

		client = frappe.get_doc(CLIENT_DT, out["name"])
		self.assertEqual(client.registration_mode, "dcr")
		self.assertEqual(client.client_id, "dcr-client")
		self.assertEqual(client.resource, MCP_RESOURCE, "the resource is canonical, no trailing slash")
		self.assertTrue(client.iss_param_supported)
		# The challenge named a narrower scope than the resource document listed,
		# and least privilege means the challenge wins.
		self.assertEqual(client.scope, "repo read:org")
		self.assertNotIn("admin:everything", client.scope)

		# The registration named the ONE callback this app answers on, and the same
		# string the authorize URL and the token exchange will later send.
		posted = json.loads(
			next(c["body"] for c in transport.calls if c["url"] == MCP_REGISTER).decode("utf-8")
		)
		self.assertEqual(posted["redirect_uris"], [connectors_api.oauth_redirect_uri()])

	def test_the_registered_client_auth_method_is_stored(self):
		# We ASK for the secret in the body; a service that registers us for the
		# header instead has its choice recorded and honoured from then on.
		script = _discovery_script()
		script[MCP_REGISTER] = _json_result(
			{
				"client_id": "dcr-client",
				"client_secret": "dcr-secret",
				"token_endpoint_auth_method": "client_secret_basic",
			},
			status=201,
		)
		transport = _ScriptedTransport(script)
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.add_connector(
				preset="Custom URL", base_url=MCP_BASE_URL, scope="Personal", auth_method="OAuth"
			)
		self._connectors.append(out["name"])

		posted = json.loads(
			next(c["body"] for c in transport.calls if c["url"] == MCP_REGISTER).decode("utf-8")
		)
		self.assertEqual(posted["application_type"], "web")
		self.assertEqual(posted["token_endpoint_auth_method"], "client_secret_post")
		self.assertEqual(frappe.db.get_value(CLIENT_DT, out["name"], "auth_method"), "client_secret_basic")

	def test_the_apps_own_wording_of_its_address_is_what_goes_on_the_wire(self):
		# The app declares itself with a trailing slash. The canonical form is the
		# pin; the declared form is the resource indicator every request carries.
		script = _discovery_script(rm_overrides={"resource": MCP_BASE_URL})
		script[MCP_REGISTER] = _json_result({"client_id": "dcr-client"}, status=201)
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", _ScriptedTransport(script)):
			out = connectors_api.add_connector(
				preset="Custom URL", base_url=MCP_BASE_URL, scope="Personal", auth_method="OAuth"
			)
		name = out["name"]
		self._connectors.append(name)

		client = frappe.get_doc(CLIENT_DT, name)
		self.assertEqual(client.resource, MCP_RESOURCE, "the pin stays canonical")
		self.assertEqual(client.resource_indicator, MCP_BASE_URL, "the wire value is verbatim")

		url, state = self._connect(name)
		self.assertEqual(parse_qs(urlparse(url).query)["resource"], [MCP_BASE_URL])

		transport = _ScriptedTransport({MCP_TOKEN: _token_response()})
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			self._callback(code="the-code", state=state, iss=MCP_AS_URL)

		self.assertEqual(transport.form_for(MCP_TOKEN)["resource"], MCP_BASE_URL)
		token = frappe.get_doc(TOKEN_DT, f"{name}-{PLAIN_A}")
		self.assertEqual(token.resource, MCP_RESOURCE, "the token is pinned to the canonical form")

	def test_static_path_asks_an_admin_for_credentials(self):
		transport = _ScriptedTransport(_discovery_script(registration=False))
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.add_connector(
				preset="Custom URL", base_url=MCP_BASE_URL, scope="Personal", auth_method="OAuth"
			)
		self._connectors.append(out["name"])

		self.assertFalse(out["oauth_configured"])
		self.assertTrue(out["needs_static_client"])
		self.assertEqual(out["oauth_redirect_uri"], connectors_api.oauth_redirect_uri())
		client = frappe.get_doc(CLIENT_DT, out["name"])
		self.assertEqual(client.registration_mode, "static")
		self.assertFalse(client.client_id)
		self.assertNotIn(MCP_REGISTER, transport.urls(), "nothing to register with")

	def test_pasted_credential_is_ignored(self):
		script = _discovery_script()
		script[MCP_REGISTER] = _json_result({"client_id": "dcr-client"}, status=201)
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", _ScriptedTransport(script)):
			out = connectors_api.add_connector(
				preset="Custom URL",
				base_url=MCP_BASE_URL,
				scope="Personal",
				auth_method="OAuth",
				credential="a-pasted-secret",
			)
		self._connectors.append(out["name"])
		row = frappe.get_doc(CONNECTOR, out["name"])
		self.assertFalse(row.get_password("credential", raise_exception=False))
		self.assertNotIn("credential", out)

	def test_discovery_failure_leaves_no_connector_behind(self):
		transport = _ScriptedTransport({MCP_BASE_URL: HttpResult(status=500, headers={}, json=None, text="")})
		frappe.set_user(PLAIN_A)
		with (
			patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport),
			self.assertRaises(frappe.ValidationError),
		):
			connectors_api.add_connector(
				preset="Custom URL", base_url=MCP_BASE_URL, scope="Personal", auth_method="OAuth"
			)
		self.assertFalse(
			frappe.db.exists(CONNECTOR, {"key": "mcp_example_invalid", "owner": PLAIN_A}),
			"a failed setup must not leave an unusable row behind",
		)

	def test_permission_is_checked_before_any_outbound_request(self):
		# A plain user asking for a Shared row is refused by the controller. That
		# must happen BEFORE discovery, or the endpoint is an unmetered outbound
		# request amplifier pointed at any host the caller names.
		transport = _ScriptedTransport(_discovery_script())
		frappe.set_user(PLAIN_A)
		with (
			patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport),
			self.assertRaises(frappe.PermissionError),
		):
			connectors_api.add_connector(
				preset="Custom URL", base_url=MCP_BASE_URL, scope="Shared", auth_method="OAuth"
			)
		self.assertEqual(transport.calls, [], "no egress before the permission gate")


class TestCatalogPresetFlows(_McpOauthTestCase):
	"""Phase D2: the in-app catalog routes every preset to its connection flow.
	``dcr``/``static`` presets run the SAME discovery engine a Custom URL row
	runs, against the catalog's PINNED address; ``token``/``open`` presets have
	no sign-in at all."""

	def test_dcr_preset_discovers_the_pinned_url_not_the_callers(self):
		script = _discovery_script_at(RAZORPAY_BASE_URL)
		script[MCP_REGISTER] = _json_result({"client_id": "rzp-client"}, status=201)
		transport = _ScriptedTransport(script)
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.add_connector(
				preset="Razorpay",
				# A caller-supplied address that must be ignored end to end: an
				# unscripted URL would raise inside the fake transport, so reaching
				# it at all fails this test rather than silently passing.
				base_url=ATTACKER_BASE_URL,
				scope="Personal",
				auth_method="OAuth",
			)
		self._connectors.append(out["name"])

		self.assertEqual(out["base_url"], RAZORPAY_BASE_URL)
		self.assertEqual(out["auth_class"], "dcr")
		self.assertEqual(transport.urls()[0], RAZORPAY_BASE_URL, "discovery starts at the pinned address")
		for url in transport.urls():
			self.assertNotIn("attacker.invalid", url)

		row = frappe.get_doc(CONNECTOR, out["name"])
		self.assertEqual(row.base_url, RAZORPAY_BASE_URL)
		self.assertEqual(row.mcp_oauth_client, out["name"])
		self.assertFalse(row.connected_app, "a catalog dcr preset never takes the Connected App link")
		self.assertEqual(row.key, "razorpay")

		client = frappe.get_doc(CLIENT_DT, out["name"])
		self.assertEqual(client.registration_mode, "dcr")
		self.assertEqual(client.client_id, "rzp-client")
		self.assertEqual(client.resource, RAZORPAY_BASE_URL)
		self.assertTrue(out["oauth_configured"])
		self.assertFalse(out["oauth_connected"])
		self.assertEqual(out["signin_host"], "as.example.invalid")

	def test_static_preset_asks_an_admin_for_credentials(self):
		transport = _ScriptedTransport(_discovery_script_at(SLACK_BASE_URL, registration=False))
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.add_connector(preset="Slack", scope="Personal", auth_method="OAuth")
		self._connectors.append(out["name"])

		self.assertEqual(out["auth_class"], "static")
		self.assertTrue(out["needs_static_client"])
		self.assertFalse(out["oauth_configured"])
		self.assertEqual(out["oauth_redirect_uri"], connectors_api.oauth_redirect_uri())
		self.assertEqual(out["base_url"], SLACK_BASE_URL)
		client = frappe.get_doc(CLIENT_DT, out["name"])
		self.assertEqual(client.registration_mode, "static")
		self.assertFalse(client.client_id)
		self.assertNotIn(MCP_REGISTER, transport.urls(), "nothing to register with")

	def test_token_preset_cannot_ask_for_a_signin(self):
		transport = _ScriptedTransport({})
		frappe.set_user(PLAIN_A)
		with (
			patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport),
			self.assertRaises(frappe.ValidationError),
		):
			connectors_api.add_connector(preset="Stripe", scope="Personal", auth_method="OAuth")
		self.assertEqual(transport.calls, [], "refused before any request leaves")
		self.assertFalse(frappe.db.exists(CONNECTOR, {"key": "stripe", "owner": PLAIN_A}))

	def test_open_preset_cannot_ask_for_a_signin(self):
		transport = _ScriptedTransport({})
		frappe.set_user(PLAIN_A)
		with (
			patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport),
			self.assertRaises(frappe.ValidationError),
		):
			connectors_api.add_connector(preset="Microsoft Learn", scope="Personal", auth_method="OAuth")
		self.assertEqual(transport.calls, [])
		self.assertFalse(frappe.db.exists(CONNECTOR, {"key": "microsoft_learn", "owner": PLAIN_A}))

	def test_refusal_message_names_no_protocol(self):
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.ValidationError) as caught:
			connectors_api.add_connector(preset="Stripe", scope="Personal", auth_method="OAuth")
		message = str(caught.exception)
		for word in ("OAuth", "MCP", "bearer", "PKCE", "token"):
			self.assertNotIn(word, message)

	def test_open_preset_needs_no_credential_and_sends_no_authorization(self):
		frappe.set_user(PLAIN_A)
		out = connectors_api.add_connector(preset="Microsoft Learn", scope="Personal")
		self._connectors.append(out["name"])

		self.assertEqual(out["base_url"], MS_LEARN_BASE_URL)
		self.assertEqual(out["auth_class"], "open")
		self.assertEqual(out["auth_method"], "API Key")
		row = frappe.get_doc(CONNECTOR, out["name"])
		self.assertFalse(row.get_password("credential", raise_exception=False))

		# One level below broker.test_connector, so the real _credential read runs:
		# an empty Password resolves to "" and reaches the client as None.
		with patch.object(broker.mcp_client, "fetch_tools", return_value=_TOOLS) as fetch:
			result = connectors_api.test_connector(out["name"])
		self.assertTrue(result["ok"], result)
		self.assertEqual(fetch.call_args.args[0], MS_LEARN_BASE_URL)
		self.assertIsNone(fetch.call_args.args[1], "no credential is handed to the outbound call")

	def test_client_sends_no_authorization_header_without_a_token(self):
		headers = McpClient(MS_LEARN_BASE_URL, None)._headers(is_initialize=False)
		self.assertNotIn("Authorization", headers)

	def test_api_key_on_a_signin_preset_is_still_the_shipped_path(self):
		# The new routing branches only when OAuth is ASKED for: a key on a dcr
		# preset stays the plain stored-credential row it has always been, and
		# nothing is discovered.
		transport = _ScriptedTransport({})
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			out = connectors_api.add_connector(preset="Linear", scope="Personal", credential="tok")
		self._connectors.append(out["name"])
		self.assertEqual(transport.calls, [])
		row = frappe.get_doc(CONNECTOR, out["name"])
		self.assertFalse(row.mcp_oauth_client)
		self.assertFalse(row.connected_app)
		self.assertEqual(row.get_password("credential", raise_exception=False), "tok")


class TestSetOauthClientCredentials(_McpOauthTestCase):
	def test_plain_user_is_refused(self):
		name = self._mk_mcp_connector("static-cred", mode="static", client_id="")
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.PermissionError):
			connectors_api.set_oauth_client_credentials(name, "stolen-client", "stolen-secret")
		self.assertFalse(frappe.db.get_value(CLIENT_DT, name, "client_id"))

	def test_admin_cannot_configure_someone_elses_personal_connector(self):
		# The admin tier says WHO may configure a sign-in; the row's own write
		# permission says WHICH rows. A Personal row stays private to its owner,
		# admin tier included, so planting client credentials on one is refused.
		name = self._mk_mcp_connector("static-private", mode="static", client_id="")
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.PermissionError):
			connectors_api.set_oauth_client_credentials(name, "admin-client", "admin-secret")
		self.assertFalse(frappe.db.get_value(CLIENT_DT, name, "client_id"))

	def test_admin_supplies_the_static_client(self):
		name = self._mk_mcp_connector("static-ok", scope="Shared", owner=None, mode="static", client_id="")
		frappe.set_user(ADMIN_USER)
		out = connectors_api.set_oauth_client_credentials(name, "admin-client", "admin-secret")

		self.assertTrue(out["oauth_configured"])
		self.assertFalse(out["needs_static_client"])
		client = frappe.get_doc(CLIENT_DT, name)
		self.assertEqual(client.client_id, "admin-client")
		self.assertEqual(client.get_password("client_secret", raise_exception=False), "admin-secret")

	def test_blank_secret_keeps_the_stored_one(self):
		name = self._mk_mcp_connector("static-keep", scope="Shared", owner=None, mode="static", client_id="")
		frappe.set_user(ADMIN_USER)
		connectors_api.set_oauth_client_credentials(name, "admin-client", "admin-secret")
		connectors_api.set_oauth_client_credentials(name, "admin-client-2", "")
		client = frappe.get_doc(CLIENT_DT, name)
		self.assertEqual(client.client_id, "admin-client-2")
		self.assertEqual(client.get_password("client_secret", raise_exception=False), "admin-secret")

	def test_rejected_for_a_self_registered_client(self):
		name = self._mk_mcp_connector(
			"dcr-cred", scope="Shared", owner=None, mode="dcr", client_id="dcr-client"
		)
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.ValidationError):
			connectors_api.set_oauth_client_credentials(name, "override", "")

	def test_rejected_for_a_preset_row(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk("Shared", "gh-static", preset="GitHub", auth_method="OAuth", connected_app=app)
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.ValidationError):
			connectors_api.set_oauth_client_credentials(name, "override", "")


class TestConnectOauthMcp(_McpOauthTestCase):
	def test_mints_single_use_state_and_a_bound_authorize_url(self):
		name = self._mk_mcp_connector("connect-me")
		frappe.set_user(PLAIN_A)
		url, state = self._connect(name)

		self.assertTrue(url.startswith(MCP_AUTHORIZE))
		query = parse_qs(urlparse(url).query)
		self.assertEqual(query["code_challenge_method"], ["S256"])
		self.assertEqual(query["resource"], [MCP_RESOURCE])
		self.assertEqual(query["client_id"], ["cid-1"])
		self.assertEqual(query["redirect_uri"], [connectors_api.oauth_redirect_uri()])
		self.assertEqual(query["scope"], ["repo read:org"])
		self.assertEqual(query["state"], [state])

		record = frappe.cache().get_value(f"jarvis:mcp_oauth_state:{state}", expires=True)
		self.assertEqual(record["user"], PLAIN_A)
		self.assertEqual(record["connector"], name)
		self.assertEqual(record["issuer"], MCP_AS_URL)
		self.assertEqual(record["redirect_uri"], connectors_api.oauth_redirect_uri())
		# The verifier stays server-side; only its challenge is ever in the URL.
		self.assertEqual(query["code_challenge"], [pkce_challenge(record["code_verifier"])])
		self.assertNotIn(record["code_verifier"], url)

	def test_starting_a_signin_is_rate_limited(self):
		name = self._mk_mcp_connector("connect-flood")
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "_over_test_rate_limit", return_value=True):
			out = connectors_api.connect_oauth(name)
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "rate_limited")

	def test_unconfigured_static_client_cannot_start_a_signin(self):
		name = self._mk_mcp_connector("connect-unconf", mode="static", client_id="")
		frappe.set_user(PLAIN_A)
		out = connectors_api.connect_oauth(name)
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "oauth_not_configured")

	def test_stranger_cannot_start_a_signin_for_someone_elses_connector(self):
		name = self._mk_mcp_connector("connect-private")
		frappe.set_user(PLAIN_B)
		with self.assertRaises(frappe.PermissionError):
			connectors_api.connect_oauth(name)


class TestMcpOauthCallback(_McpOauthTestCase):
	def _script(self, **kw):
		return _ScriptedTransport({MCP_TOKEN: _token_response(**kw)})

	def test_happy_path_stores_the_token_for_the_right_user(self):
		name = self._mk_mcp_connector("cb-happy")
		frappe.set_user(PLAIN_A)
		_url, state = self._connect(name)
		transport = self._script()
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			response = self._callback(code="the-code", state=state, iss=MCP_AS_URL)

		self.assertEqual(response["type"], "redirect")
		self.assertEqual(response["location"], f"/jarvis?settings=connectors&oauth={name}")

		token = frappe.get_doc(TOKEN_DT, f"{name}-{PLAIN_A}")
		self.assertEqual(token.user, PLAIN_A)
		self.assertEqual(token.get_password("access_token", raise_exception=False), "fresh-access")
		self.assertEqual(token.get_password("refresh_token", raise_exception=False), "fresh-refresh")
		self.assertEqual(token.resource, MCP_RESOURCE)
		self.assertTrue(token.expires_at)

		form = transport.form_for(MCP_TOKEN)
		self.assertEqual(form["grant_type"], "authorization_code")
		self.assertEqual(form["resource"], MCP_RESOURCE)
		self.assertEqual(form["redirect_uri"], connectors_api.oauth_redirect_uri())
		self.assertIn("code_verifier", form)
		# Nothing secret rode back in the redirect.
		for secret in ("fresh-access", "fresh-refresh", "the-code", state):
			self.assertNotIn(secret, response["location"])

	def test_replayed_callback_cannot_mint_a_second_token(self):
		name = self._mk_mcp_connector("cb-replay")
		frappe.set_user(PLAIN_A)
		_url, state = self._connect(name)
		transport = self._script()
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			self._callback(code="the-code", state=state, iss=MCP_AS_URL)
			replay = self._callback(code="the-code", state=state, iss=MCP_AS_URL)

		self.assertEqual(replay["location"], "/jarvis?settings=connectors&oauth_error=expired")
		self.assertEqual(transport.hits(MCP_TOKEN), 1, "the replay must not reach the token endpoint")

	def test_rejects_a_state_minted_by_a_different_user(self):
		name = self._mk_mcp_connector("cb-other-user", scope="Shared", owner=None)
		frappe.set_user(PLAIN_A)
		_url, state = self._connect(name)

		frappe.set_user(PLAIN_B)
		transport = self._script()
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			response = self._callback(code="the-code", state=state, iss=MCP_AS_URL)

		self.assertEqual(response["location"], "/jarvis?settings=connectors&oauth_error=denied")
		self.assertEqual(transport.calls, [], "no token is ever requested for a stolen state")
		self.assertFalse(frappe.db.exists(TOKEN_DT, f"{name}-{PLAIN_B}"))
		self.assertFalse(frappe.db.exists(TOKEN_DT, f"{name}-{PLAIN_A}"))

	def test_rejects_a_mismatched_issuer_before_any_token_request(self):
		name = self._mk_mcp_connector("cb-iss")
		frappe.set_user(PLAIN_A)
		_url, state = self._connect(name)
		transport = self._script()
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			response = self._callback(code="the-code", state=state, iss="https://evil.invalid")

		self.assertEqual(response["location"], "/jarvis?settings=connectors&oauth_error=denied")
		self.assertEqual(transport.calls, [])
		self.assertFalse(frappe.db.exists(TOKEN_DT, f"{name}-{PLAIN_A}"))

	def test_rejects_a_missing_issuer_when_the_service_declared_one(self):
		name = self._mk_mcp_connector("cb-noiss")
		frappe.set_user(PLAIN_A)
		_url, state = self._connect(name)
		transport = self._script()
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			response = self._callback(code="the-code", state=state)
		self.assertEqual(response["location"], "/jarvis?settings=connectors&oauth_error=denied")
		self.assertEqual(transport.calls, [])

	def test_provider_error_is_never_echoed_back(self):
		name = self._mk_mcp_connector("cb-error")
		frappe.set_user(PLAIN_A)
		_url, state = self._connect(name)
		transport = self._script()
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			response = self._callback(
				state=state,
				iss=MCP_AS_URL,
				error="access_denied",
				error_description="<script>alert(1)</script>",
			)
		self.assertEqual(response["location"], "/jarvis?settings=connectors&oauth_error=denied")
		self.assertNotIn("script", response["location"])
		self.assertEqual(transport.calls, [])

	def test_unknown_state_redirects_without_touching_anything(self):
		transport = self._script()
		frappe.set_user(PLAIN_A)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			response = self._callback(code="the-code", state="never-minted", iss=MCP_AS_URL)
		self.assertEqual(response["location"], "/jarvis?settings=connectors&oauth_error=expired")
		self.assertEqual(transport.calls, [])

	def test_a_failed_exchange_redirects_instead_of_raising(self):
		name = self._mk_mcp_connector("cb-fail")
		frappe.set_user(PLAIN_A)
		_url, state = self._connect(name)
		transport = _ScriptedTransport(
			{MCP_TOKEN: HttpResult(status=400, headers={}, json={"error": "invalid_grant"}, text="")}
		)
		with patch.object(connectors_api, "MCP_OAUTH_TRANSPORT", transport):
			response = self._callback(code="the-code", state=state, iss=MCP_AS_URL)
		self.assertEqual(response["location"], "/jarvis?settings=connectors&oauth_error=denied")
		self.assertFalse(frappe.db.exists(TOKEN_DT, f"{name}-{PLAIN_A}"))


class TestBrokerMcpOauthToken(_McpOauthTestCase):
	def test_credential_returns_the_stored_token(self):
		name = self._mk_mcp_connector("broker-ok")
		self._mk_mcp_token(name, PLAIN_A, access_token="secret-token")
		frappe.set_user(PLAIN_A)
		self.assertEqual(broker._credential(self._reload_doc(name)), "secret-token")

	def test_credential_not_ready_without_a_signin(self):
		name = self._mk_mcp_connector("broker-none")
		frappe.set_user(PLAIN_A)
		with self.assertRaises(broker._BrokerError) as ctx:
			broker._credential(self._reload_doc(name))
		self.assertEqual(ctx.exception.code, "connector_not_ready")

	def test_one_users_token_is_never_handed_to_another(self):
		name = self._mk_mcp_connector("broker-peruser", scope="Shared", owner=None)
		self._mk_mcp_token(name, PLAIN_A, access_token="a-token")
		frappe.set_user(PLAIN_B)
		with self.assertRaises(broker._BrokerError) as ctx:
			broker._credential(self._reload_doc(name))
		self.assertEqual(ctx.exception.code, "connector_not_ready")

	def test_expiring_token_is_refreshed_and_the_rotation_persisted(self):
		name = self._mk_mcp_connector("broker-refresh")
		self._mk_mcp_token(
			name,
			PLAIN_A,
			access_token="stale-token",
			refresh_token="old-refresh",
			expires_at=add_to_date(now_datetime(), seconds=30),
		)
		transport = _ScriptedTransport(
			{MCP_TOKEN: _token_response(access="rotated-access", refresh="rotated-refresh")}
		)
		frappe.set_user(PLAIN_A)
		with patch.object(oauth, "MCP_OAUTH_TRANSPORT", transport):
			self.assertEqual(broker._credential(self._reload_doc(name)), "rotated-access")

		form = transport.form_for(MCP_TOKEN)
		self.assertEqual(form["grant_type"], "refresh_token")
		self.assertEqual(form["refresh_token"], "old-refresh")
		self.assertEqual(form["resource"], MCP_RESOURCE)

		token = frappe.get_doc(TOKEN_DT, f"{name}-{PLAIN_A}")
		self.assertEqual(token.get_password("access_token", raise_exception=False), "rotated-access")
		self.assertEqual(token.get_password("refresh_token", raise_exception=False), "rotated-refresh")

	def test_a_rotating_service_that_omits_the_refresh_token_keeps_the_old_one(self):
		name = self._mk_mcp_connector("broker-keepref")
		self._mk_mcp_token(
			name,
			PLAIN_A,
			access_token="stale-token",
			refresh_token="old-refresh",
			expires_at=add_to_date(now_datetime(), seconds=30),
		)
		transport = _ScriptedTransport({MCP_TOKEN: _token_response(access="new-access", refresh=None)})
		frappe.set_user(PLAIN_A)
		with patch.object(oauth, "MCP_OAUTH_TRANSPORT", transport):
			self.assertEqual(broker._credential(self._reload_doc(name)), "new-access")
		token = frappe.get_doc(TOKEN_DT, f"{name}-{PLAIN_A}")
		self.assertEqual(token.get_password("refresh_token", raise_exception=False), "old-refresh")

	def test_a_refresh_that_reports_no_expiry_keeps_the_row_refreshable(self):
		# A response that omits expires_in says nothing new, it does not say "never
		# expires". Blanking the stored expiry would make the row read as immortal
		# and it would never be refreshed again - it would just start failing.
		name = self._mk_mcp_connector("broker-keepexp")
		expires_at = add_to_date(now_datetime(), seconds=30)
		self._mk_mcp_token(
			name,
			PLAIN_A,
			access_token="stale-token",
			refresh_token="old-refresh",
			expires_at=expires_at,
		)
		bare = _ScriptedTransport(
			{
				MCP_TOKEN: _token_response(
					access="first", refresh=None, expires_in=None, scope=None, token_type=None
				)
			}
		)
		frappe.set_user(PLAIN_A)
		with patch.object(oauth, "MCP_OAUTH_TRANSPORT", bare):
			self.assertEqual(broker._credential(self._reload_doc(name)), "first")

		token = frappe.get_doc(TOKEN_DT, f"{name}-{PLAIN_A}")
		self.assertEqual(str(token.expires_at)[:19], str(expires_at)[:19], "the known expiry survives")
		self.assertEqual(token.token_type, "Bearer", "an omitted token_type keeps the stored one")

		# ...and because it survived, the NEXT call refreshes again rather than
		# handing over a token nothing will ever renew.
		again = _ScriptedTransport({MCP_TOKEN: _token_response(access="second", refresh=None)})
		with patch.object(oauth, "MCP_OAUTH_TRANSPORT", again):
			self.assertEqual(broker._credential(self._reload_doc(name)), "second")
		self.assertEqual(again.hits(MCP_TOKEN), 1)

	def test_a_refresh_spends_the_calls_budget_not_extra_time(self):
		# refresh + call must stay under the plugin's 30s abort, so whatever the
		# refresh took comes OUT of the call's 20s rather than being added to it.
		name = self._mk_mcp_connector("broker-budget")
		self._mk_mcp_token(
			name,
			PLAIN_A,
			access_token="stale-token",
			refresh_token="old-refresh",
			expires_at=add_to_date(now_datetime(), seconds=30),
		)
		transport = _ScriptedTransport({MCP_TOKEN: _token_response(access="fresh-access")})
		captured: dict = {}

		def _capture(base_url, token, action, args, **kwargs):
			captured.update(kwargs)
			captured["token"] = token
			return {"content": []}

		frappe.set_user(PLAIN_A)
		with (
			patch.object(oauth, "MCP_OAUTH_TRANSPORT", transport),
			patch.object(broker.mcp_client, "run_tool", side_effect=_capture),
			patch.object(broker, "time", _StepClock([0.0, 7.0])),
		):
			broker._execute(self._reload_doc(name), "list_issues", {})

		self.assertEqual(captured["token"], "fresh-access")
		self.assertEqual(captured["total_timeout"], broker.TOTAL_TIMEOUT_S - 7.0)
		self.assertLess(captured["total_timeout"], broker.TOTAL_TIMEOUT_S)
		self.assertEqual(transport.calls[0]["total_timeout"], broker.REFRESH_BUDGET_S)

	def test_a_slow_refresh_still_leaves_the_call_a_floor(self):
		name = self._mk_mcp_connector("broker-floor")
		self._mk_mcp_token(name, PLAIN_A, access_token="live-token")
		captured: dict = {}

		def _capture(base_url, token, action, args, **kwargs):
			captured.update(kwargs)
			return {"content": []}

		frappe.set_user(PLAIN_A)
		with (
			patch.object(broker.mcp_client, "run_tool", side_effect=_capture),
			patch.object(broker, "time", _StepClock([0.0, 60.0])),
		):
			broker._execute(self._reload_doc(name), "list_issues", {})

		self.assertEqual(captured["total_timeout"], broker.MIN_CALL_TIMEOUT_S)

	def test_a_token_without_an_expiry_is_never_refreshed(self):
		name = self._mk_mcp_connector("broker-noexp")
		self._mk_mcp_token(name, PLAIN_A, access_token="long-lived", refresh_token="unused-refresh")
		transport = _ScriptedTransport({})
		frappe.set_user(PLAIN_A)
		with patch.object(oauth, "MCP_OAUTH_TRANSPORT", transport):
			self.assertEqual(broker._credential(self._reload_doc(name)), "long-lived")
		self.assertEqual(transport.calls, [], "an empty expiry must never read as expired")

	def test_a_failed_refresh_surfaces_as_reconsent(self):
		name = self._mk_mcp_connector("broker-badref")
		self._mk_mcp_token(
			name,
			PLAIN_A,
			access_token="stale-token",
			refresh_token="old-refresh",
			expires_at=add_to_date(now_datetime(), seconds=30),
		)
		transport = _ScriptedTransport(
			{MCP_TOKEN: HttpResult(status=400, headers={}, json={"error": "invalid_grant"}, text="")}
		)
		frappe.set_user(PLAIN_A)
		with (
			patch.object(oauth, "MCP_OAUTH_TRANSPORT", transport),
			self.assertRaises(broker._BrokerError) as ctx,
		):
			broker._credential(self._reload_doc(name))
		self.assertEqual(ctx.exception.code, "connector_not_ready")

	def test_a_token_is_never_sent_to_a_re_pointed_address(self):
		# The resource pin is the no-passthrough rule: a token issued FOR one
		# address must not follow the connector to another, however the row got
		# re-pointed (this simulates a raw DocType write, since the API refuses).
		name = self._mk_mcp_connector("broker-repoint")
		self._mk_mcp_token(name, PLAIN_A, access_token="good-host-token")
		frappe.db.set_value(CONNECTOR, name, "base_url", "https://evil.invalid/mcp", update_modified=False)
		frappe.db.commit()
		frappe.set_user(PLAIN_A)
		with self.assertRaises(broker._BrokerError) as ctx:
			broker._credential(self._reload_doc(name))
		self.assertEqual(ctx.exception.code, "connector_not_ready")

	def test_test_connector_fails_before_probing_without_a_signin(self):
		name = self._mk_mcp_connector("broker-test")
		frappe.set_user(PLAIN_A)
		with patch.object(broker, "test_connector") as probe:
			out = connectors_api.test_connector(name)
		self.assertEqual(out["error"]["code"], "connector_not_ready")
		probe.assert_not_called()

	def test_test_connector_probes_once_signed_in(self):
		name = self._mk_mcp_connector("broker-test-ok")
		self._mk_mcp_token(name, PLAIN_A, access_token="secret-token")
		frappe.set_user(PLAIN_A)
		with patch.object(broker, "test_connector", return_value={"ok": True, "tools": _TOOLS}) as probe:
			out = connectors_api.test_connector(name)
		self.assertTrue(out["ok"])
		probe.assert_called_once()


class TestDisconnectMcpOauth(_McpOauthTestCase):
	def test_deletes_only_the_current_users_signin(self):
		name = self._mk_mcp_connector("dis-mine", scope="Shared", owner=None)
		self._mk_mcp_token(name, PLAIN_A)
		self._mk_mcp_token(name, PLAIN_B)
		frappe.set_user(PLAIN_A)
		self.assertEqual(connectors_api.disconnect_oauth(name), {"ok": True})
		self.assertFalse(frappe.db.exists(TOKEN_DT, f"{name}-{PLAIN_A}"))
		self.assertTrue(frappe.db.exists(TOKEN_DT, f"{name}-{PLAIN_B}"))

	def test_idempotent_when_never_connected(self):
		name = self._mk_mcp_connector("dis-never")
		frappe.set_user(PLAIN_A)
		self.assertEqual(connectors_api.disconnect_oauth(name), {"ok": True})


class TestListConnectorsMcpOauth(_McpOauthTestCase):
	def test_annotates_rows_and_never_ships_the_internal_link(self):
		connected = self._mk_mcp_connector("list-connected")
		self._mk_mcp_token(connected, PLAIN_A)
		pending = self._mk_mcp_connector("list-static", mode="static", client_id="")

		frappe.set_user(PLAIN_A)
		rows = {r["name"]: r for r in connectors_api.list_connectors()["mine"]}

		self.assertTrue(rows[connected]["oauth_configured"])
		self.assertTrue(rows[connected]["oauth_connected"])
		self.assertFalse(rows[connected]["needs_static_client"])
		self.assertEqual(rows[connected]["signin_host"], "as.example.invalid")
		self.assertNotIn("mcp_oauth_client", rows[connected])
		self.assertNotIn("connected_app", rows[connected])

		self.assertFalse(rows[pending]["oauth_configured"])
		self.assertTrue(rows[pending]["needs_static_client"])
		self.assertEqual(rows[pending]["oauth_redirect_uri"], connectors_api.oauth_redirect_uri())

	def test_preset_rows_report_a_signin_host_too(self):
		app = self._mk_connected_app("GitHub")
		name = self._mk(
			"Personal", "list-gh", owner=PLAIN_A, preset="GitHub", auth_method="OAuth", connected_app=app
		)
		frappe.set_user(PLAIN_A)
		row = next(r for r in connectors_api.list_connectors()["mine"] if r["name"] == name)
		self.assertEqual(row["signin_host"], "example.invalid")


class TestUpdateConnectorMcpOauth(_McpOauthTestCase):
	def test_a_signed_in_row_cannot_be_re_pointed(self):
		name = self._mk_mcp_connector("upd-repoint")
		frappe.set_user(PLAIN_A)
		with self.assertRaises(frappe.ValidationError):
			connectors_api.update_connector(name, base_url="https://elsewhere.invalid/mcp")
		self.assertEqual(frappe.db.get_value(CONNECTOR, name, "base_url"), MCP_BASE_URL)

	def test_relabel_still_works(self):
		name = self._mk_mcp_connector("upd-label")
		frappe.set_user(PLAIN_A)
		out = connectors_api.update_connector(name, label="Renamed")
		self.assertEqual(out["label"], "Renamed")


class TestConnectorTwoEngineGuard(_McpOauthTestCase):
	"""The controller is the last line: a Jarvis User has raw create/write on
	Jarvis Connector, so neither engine's link may be steerable from a direct
	DocType write."""

	def _insert_as(self, user, **fields):
		prev = frappe.session.user
		frappe.set_user(user)
		try:
			doc = frappe.get_doc(
				{
					"doctype": CONNECTOR,
					"scope": "Personal",
					"base_url": MCP_BASE_URL,
					"label": "Custom",
					"preset": "Custom URL",
					**fields,
				}
			).insert()
			self._connectors.append(doc.name)
			return doc
		finally:
			frappe.set_user(prev)

	def test_row_cannot_borrow_another_connectors_client(self):
		victim = self._mk_mcp_connector("guard-victim")
		with self.assertRaises(frappe.PermissionError):
			self._insert_as(PLAIN_A, key="guard-thief", auth_method="OAuth", mcp_oauth_client=victim)

	def test_row_cannot_carry_both_engines(self):
		app = self._mk_connected_app("GitHub")
		victim = self._mk_mcp_connector("guard-both-src")
		with self.assertRaises(frappe.PermissionError):
			self._insert_as(
				PLAIN_A,
				key="guard-both",
				auth_method="OAuth",
				connected_app=app,
				mcp_oauth_client=victim,
			)

	def test_custom_url_oauth_row_may_not_take_the_preset_link(self):
		app = self._mk_connected_app("GitHub")
		with self.assertRaises(frappe.PermissionError):
			self._insert_as(PLAIN_A, key="guard-preset", auth_method="OAuth", connected_app=app)

	def test_key_row_keeps_neither_link(self):
		app = self._mk_connected_app("GitHub")
		victim = self._mk_mcp_connector("guard-key-src")
		doc = self._insert_as(
			PLAIN_A, key="guard-key", auth_method="API Key", connected_app=app, mcp_oauth_client=victim
		)
		self.assertFalse(doc.connected_app)
		self.assertFalse(doc.mcp_oauth_client)

	def test_unchanged_resave_does_not_lock_the_row(self):
		name = self._mk_mcp_connector("guard-resave")
		frappe.set_user(PLAIN_A)
		doc = self._reload_doc(name)
		doc.label = "renamed by owner"
		doc.save()  # no ignore_permissions - the guard runs and must not throw
		self.assertEqual(self._reload_doc(name).mcp_oauth_client, name)

	def test_preset_cannot_be_flipped_under_an_existing_client(self):
		# Both links sit still, so only watching ``preset`` catches this: the row
		# would end up pinned at Razorpay's endpoint while still presenting the
		# client (and credentials) registered with the Custom URL app.
		name = self._mk_mcp_connector("guard-flip")
		frappe.set_user(PLAIN_A)
		doc = self._reload_doc(name)
		doc.preset = "Razorpay"
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(self._reload_doc(name).preset, "Custom URL")

	def test_catalog_signin_preset_may_not_take_the_preset_link(self):
		# Razorpay is a dcr preset: the discovery engine backs it, so the Connected
		# App link is not its to claim even though it is not a Custom URL row.
		app = self._mk_connected_app("GitHub")
		with self.assertRaises(frappe.PermissionError):
			self._insert_as(
				PLAIN_A,
				key="guard-rzp-app",
				preset="Razorpay",
				base_url=RAZORPAY_BASE_URL,
				auth_method="OAuth",
				connected_app=app,
			)

	def test_catalog_signin_preset_cannot_borrow_another_connectors_client(self):
		victim = self._mk_mcp_connector("guard-rzp-victim")
		with self.assertRaises(frappe.PermissionError):
			self._insert_as(
				PLAIN_A,
				key="guard-rzp-thief",
				preset="Razorpay",
				base_url=RAZORPAY_BASE_URL,
				auth_method="OAuth",
				mcp_oauth_client=victim,
			)

	def test_connected_app_preset_may_not_take_the_discovery_link(self):
		victim = self._mk_mcp_connector("guard-gh-victim")
		with self.assertRaises(frappe.PermissionError):
			self._insert_as(
				PLAIN_A,
				key="guard-gh-thief",
				preset="GitHub",
				base_url="https://api.githubcopilot.com/mcp/",
				auth_method="OAuth",
				mcp_oauth_client=victim,
			)

	def test_token_preset_cannot_claim_a_signin_by_raw_write(self):
		with self.assertRaises(frappe.PermissionError):
			self._insert_as(
				PLAIN_A,
				key="guard-stripe",
				preset="Stripe",
				base_url="https://mcp.stripe.com/",
				auth_method="OAuth",
			)

	def test_open_preset_cannot_claim_a_signin_by_raw_write(self):
		with self.assertRaises(frappe.PermissionError):
			self._insert_as(
				PLAIN_A,
				key="guard-mslearn",
				preset="Microsoft Learn",
				base_url=MS_LEARN_BASE_URL,
				auth_method="OAuth",
			)

	def test_deleting_a_connector_purges_its_client_and_tokens(self):
		name = self._mk_mcp_connector("guard-cascade")
		self._mk_mcp_token(name, PLAIN_A)
		frappe.set_user(PLAIN_A)
		connectors_api.delete_connector(name)
		self._connectors.remove(name)
		self.assertFalse(frappe.db.exists(CONNECTOR, name))
		self.assertFalse(frappe.db.exists(CLIENT_DT, name), "the client must not outlive its connector")
		self.assertFalse(frappe.db.exists(TOKEN_DT, f"{name}-{PLAIN_A}"), "tokens must not be orphaned")
