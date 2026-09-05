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
from jarvis.connectors import broker, oauth
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
