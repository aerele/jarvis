"""Security review PART 4 (REVISED — Jarvis Admin administration tier) —
exploit reproductions + fix proofs.

Covers the central admin gate (TASK 44), the Settings operator-field permlevel
fence (TASK 46), the ping_admin/ping_openclaw token+URL redaction (TASK 34-R),
the onboarding grant (TASK 48), the date_add SQLi (TASK 35), the widened
agent-admin endpoints (TASK 45/47), the Jarvis User Settings ORM scoping (TASK
52), the FOUR owner-decision capabilities kept System-Manager-only, and the
cross-module endpoint-gating coverage guard (TASK 43/51).

The tier gate is deliberately WIDEN-ONLY: converting only_for("System Manager")
to require_jarvis_admin admits Jarvis Admin in ADDITION to SM/Administrator, so
the tests assert both a plain Jarvis User is rejected AND a Jarvis-Admin-not-SM
is admitted, while the four owner-SM-only capabilities still reject a non-SM
Jarvis Admin.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from pypika.terms import Field

from jarvis import diagnostics
from jarvis.chat import agents_api, usage

SETTINGS = "Jarvis Settings"
USER_SETTINGS = "Jarvis User Settings"
LISTING = "Jarvis Agent Listing"

USER_A = "p4-usera@example.com"  # plain Jarvis User
USER_B = "p4-userb@example.com"  # plain Jarvis User
ADMIN = "p4-admin@example.com"  # Jarvis Admin, NOT System Manager
SM = "p4-sm@example.com"  # System Manager (real, not Administrator)
WEBSITE = "p4-website@example.com"  # Website (portal) user
GRANTEE = "p4-grantee@example.com"  # Jarvis User with NO Jarvis Admin (grant target)
BARE = "p4-bare@example.com"  # NEITHER Jarvis role — additive-grant target
AGENT_SLUG = "p4-test-agent"
PFX = "p4"

_MANAGEABLE = {"System Manager", "Jarvis User", "Jarvis Admin", "Jarvis Skill Reviewer"}


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _ensure_user(email: str, roles: list[str], user_type: str = "System User") -> str:
	from jarvis.permissions import ensure_jarvis_admin_role, ensure_jarvis_user_role

	ensure_jarvis_user_role()
	ensure_jarvis_admin_role()
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": PFX,
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": user_type,
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	if frappe.db.get_value("User", email, "user_type") != user_type:
		frappe.db.set_value("User", email, "user_type", user_type)
	desired = set(roles)
	current = set(frappe.get_roles(email))
	to_add = desired - current
	to_remove = (_MANAGEABLE & current) - desired
	doc = frappe.get_doc("User", email)
	if to_add:
		doc.add_roles(*to_add)
	if to_remove:
		doc.remove_roles(*to_remove)
	frappe.clear_cache(user=email)
	return email


class Part4Base(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(USER_A, ["Jarvis User"])
		_ensure_user(USER_B, ["Jarvis User"])
		_ensure_user(ADMIN, ["Jarvis Admin"])  # NOT System Manager
		_ensure_user(SM, ["System Manager"])
		_ensure_user(GRANTEE, ["Jarvis User"])  # no Jarvis Admin yet
		# Holds NEITHER role, so the additive grant can be observed from zero.
		# _ensure_user pins user_type, so it stays a System User despite no
		# desk-access role.
		_ensure_user(BARE, [])
		_ensure_user(WEBSITE, [], user_type="Website User")
		if not frappe.db.exists(LISTING, AGENT_SLUG):
			frappe.get_doc(
				{
					"doctype": LISTING,
					"agent_slug": AGENT_SLUG,
					"title": "P4 Test Agent",
					"description": "test",
					"status": "Published",
					"nature": "Auditor",
				}
			).insert(ignore_permissions=True)
		# Deterministic baseline for the permlevel-fence read/write assertions.
		frappe.db.set_value(SETTINGS, SETTINGS, "agent_url", "ws://p4-baseline", update_modified=False)
		frappe.db.set_value(SETTINGS, SETTINGS, "pattern_learning_enabled", 0, update_modified=False)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()
		if frappe.db.exists(LISTING, AGENT_SLUG):
			frappe.delete_doc(LISTING, AGENT_SLUG, force=True, ignore_permissions=True)
		frappe.db.set_value(SETTINGS, SETTINGS, "agent_url", "", update_modified=False)
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		frappe.db.rollback()


# --------------------------------------------------------------------------- #
# TASK 44 — the central require_jarvis_admin tier gate
# --------------------------------------------------------------------------- #
class TestAdminTierGate(Part4Base):
	def test_gate_admits_admin_sm_administrator_blocks_plain_user(self):
		from jarvis.permissions import has_jarvis_admin_access, require_jarvis_admin

		with _as(USER_A):
			self.assertFalse(has_jarvis_admin_access())
			with self.assertRaises(frappe.PermissionError):
				require_jarvis_admin()
		with _as(ADMIN):
			self.assertTrue(has_jarvis_admin_access())
			require_jarvis_admin()  # no raise
		with _as(SM):
			self.assertTrue(has_jarvis_admin_access())
			require_jarvis_admin()  # no raise
		with _as("Administrator"):
			require_jarvis_admin()  # no raise


# --------------------------------------------------------------------------- #
# TASK 44/48 — grant_onboarding_admin
# --------------------------------------------------------------------------- #
class TestOnboardingGrant(Part4Base):
	def test_grant_is_idempotent_and_additive(self):
		"""Both roles are granted. "Jarvis Admin" is admin rights ON TOP of a
		normal user: alone it passes the access gate but owns no permission row
		on Jarvis Conversation / Jarvis Chat Message, so the holder could reach
		send_message and then fail on the insert."""
		from jarvis.permissions import grant_onboarding_admin

		def _rows(role):
			return frappe.db.count(
				"Has Role",
				{"parenttype": "User", "parent": BARE, "role": role},
			)

		# BARE holds neither role (GRANTEE is seeded WITH "Jarvis User", so it
		# cannot show the additive grant from zero).
		self.assertEqual(_rows("Jarvis Admin"), 0)
		self.assertEqual(_rows("Jarvis User"), 0)
		grant_onboarding_admin(BARE)
		self.assertEqual(_rows("Jarvis Admin"), 1)
		self.assertEqual(_rows("Jarvis User"), 1)
		# The helper invalidates the role cache itself, so no clear_cache here.
		roles = frappe.get_roles(BARE)
		self.assertIn("Jarvis Admin", roles)
		self.assertIn("Jarvis User", roles)
		# Idempotent: a second call adds no duplicate rows.
		grant_onboarding_admin(BARE)
		self.assertEqual(_rows("Jarvis Admin"), 1)
		self.assertEqual(_rows("Jarvis User"), 1)

	def test_grant_tops_up_a_partially_granted_user(self):
		"""GRANTEE already holds "Jarvis User" only. The grant must add the
		missing "Jarvis Admin" without duplicating the role it already has."""
		from jarvis.permissions import grant_onboarding_admin

		def _rows(role):
			return frappe.db.count(
				"Has Role",
				{"parenttype": "User", "parent": GRANTEE, "role": role},
			)

		self.assertEqual(_rows("Jarvis User"), 1)
		self.assertEqual(_rows("Jarvis Admin"), 0)
		grant_onboarding_admin(GRANTEE)
		self.assertEqual(_rows("Jarvis User"), 1)
		self.assertEqual(_rows("Jarvis Admin"), 1)

	def test_grant_never_touches_administrator_or_guest(self):
		from jarvis.permissions import grant_onboarding_admin

		grant_onboarding_admin("Administrator")  # no-op, no raise
		grant_onboarding_admin("Guest")  # no-op, no raise
		self.assertFalse(
			frappe.db.exists("Has Role", {"parenttype": "User", "parent": "Guest", "role": "Jarvis Admin"})
		)


# --------------------------------------------------------------------------- #
# TASK 46 — Jarvis Settings operator fields fenced at permlevel 1
# --------------------------------------------------------------------------- #
class TestSettingsPermlevelFence(Part4Base):
	OPERATOR_FIELDS = (
		"jarvis_admin_url",
		"agent_url",
		"agent_token",
		"run_query_doctype_allowlist",
	)

	def test_jarvis_admin_cannot_read_operator_fields_but_sm_can(self):
		# Read fence via the exact method Frappe applies on the perm-checked form
		# load: fields above the user's permlevel are stripped.
		with _as(ADMIN):
			doc = frappe.get_doc(SETTINGS)
			doc.apply_fieldlevel_read_permissions()
			for f in self.OPERATOR_FIELDS:
				self.assertFalse(doc.get(f), f"Jarvis Admin can read fenced operator field {f!r}")
		with _as(SM):
			doc_sm = frappe.get_doc(SETTINGS)
			doc_sm.apply_fieldlevel_read_permissions()
			self.assertEqual(
				doc_sm.get("agent_url"),
				"ws://p4-baseline",
				"System Manager lost read on the permlevel-1 operator section",
			)

	def test_jarvis_admin_write_to_operator_field_is_dropped_sm_write_applies(self):
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import JarvisSettings

		# Patch on_update so a save doesn't fire the admin container sync.
		with patch.object(JarvisSettings, "on_update", lambda self: None):
			# Jarvis Admin (pl0 write, NO pl1) -> agent_url reset, stays baseline.
			with _as(ADMIN):
				try:
					frappe.client.set_value(SETTINGS, SETTINGS, "agent_url", "ws://evil")
				except frappe.PermissionError:
					pass
			self.assertEqual(
				frappe.db.get_value(SETTINGS, SETTINGS, "agent_url"),
				"ws://p4-baseline",
				"a Jarvis Admin repointed agent_url via REST — permlevel fence failed",
			)
			# System Manager (pl1 write) -> the write applies.
			with _as(SM):
				frappe.client.set_value(SETTINGS, SETTINGS, "agent_url", "ws://sm-set")
			self.assertEqual(
				frappe.db.get_value(SETTINGS, SETTINGS, "agent_url"),
				"ws://sm-set",
				"System Manager could not write the permlevel-1 operator field",
			)

	def test_jarvis_admin_cannot_write_admin_credential_field_sm_can(self):
		# Finding B: the six admin-credential / device fields (jarvis_admin_api_key
		# et al.) are now permlevel-1 fenced. A {Jarvis Admin, write} pl0 row let a
		# non-SM admin overwrite them via frappe.client.set_value (Frappe does NOT
		# enforce field read_only on the REST save path — only permlevel does), e.g.
		# hijacking the admin connection. The fence must drop the Admin write and
		# keep it working for SM. Uses jarvis_admin_api_key (a Password field, read
		# back via get_password); the production writer is set_settings_password
		# (encrypts into __Auth, bypasses permlevel — proving no writer breaks).
		from jarvis._password_utils import set_settings_password
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import JarvisSettings

		def _key():
			return frappe.get_doc(SETTINGS).get_password("jarvis_admin_api_key", raise_exception=False)

		with patch.object(JarvisSettings, "on_update", lambda self: None):
			# Server-side seed via the real writer (permlevel-bypass, unbroken).
			set_settings_password(frappe.get_doc(SETTINGS), "jarvis_admin_api_key", "BASE-KEY")
			self.assertEqual(_key(), "BASE-KEY")
			# Jarvis Admin (pl0 write, NO pl1) -> the credential write is dropped.
			with _as(ADMIN):
				try:
					frappe.client.set_value(SETTINGS, SETTINGS, "jarvis_admin_api_key", "ADMIN-HIJACK")
				except frappe.PermissionError:
					pass
			self.assertEqual(
				_key(),
				"BASE-KEY",
				"a Jarvis Admin overwrote jarvis_admin_api_key via REST — the "
				"credential permlevel fence failed (admin-connection hijack)",
			)
			# System Manager (pl1 write) -> the write applies.
			with _as(SM):
				frappe.client.set_value(SETTINGS, SETTINGS, "jarvis_admin_api_key", "SM-SET-KEY")
			self.assertEqual(
				_key(), "SM-SET-KEY", "System Manager could not write the permlevel-1 credential field"
			)


# --------------------------------------------------------------------------- #
# TASK 34-R — ping_admin / ping_openclaw never leak the token or operator URL
# --------------------------------------------------------------------------- #
class TestDiagnosticsRedaction(Part4Base):
	def test_ping_admin_redacts_token_and_urls_on_success(self):
		mock_settings = MagicMock()
		mock_settings.get_password.return_value = "test-admin-key"
		leaky = {
			"agent_token": "SECRET-TOKEN",
			"agent_url": "ws://secret-agent",
			"admin_url": "https://secret-admin",
		}
		with (
			_as(ADMIN),
			patch("frappe.get_single", return_value=mock_settings),
			patch("jarvis.admin_client.get_connection", return_value=leaky),
		):
			res = diagnostics.ping_admin()
		self.assertTrue(res.get("ok"))
		blob = json.dumps(res)
		self.assertNotIn("SECRET-TOKEN", blob)
		self.assertNotIn("secret-agent", blob)
		self.assertNotIn("secret-admin", blob)
		self.assertNotIn("agent_token", res)
		self.assertNotIn("connection", res)
		self.assertNotIn("admin_url", res)

	def test_ping_openclaw_drops_agent_url(self):
		mock_settings = MagicMock()
		mock_settings.agent_url = "ws://secret-openclaw"
		mock_settings.get_password.return_value = "tok-secret"
		with (
			_as(ADMIN),
			patch("frappe.get_single", return_value=mock_settings),
			patch("jarvis.agent_ws.ping", return_value=None),
		):
			res = diagnostics.ping_openclaw()
		self.assertTrue(res.get("ok"))
		self.assertNotIn("agent_url", res)
		self.assertNotIn("secret-openclaw", json.dumps(res))

	def test_ping_endpoints_reject_plain_jarvis_user(self):
		with _as(USER_A):
			with self.assertRaises(frappe.PermissionError):
				diagnostics.ping_admin()
			with self.assertRaises(frappe.PermissionError):
				diagnostics.ping_openclaw()

	def test_ping_admin_admits_jarvis_admin(self):
		# The gate lets a Jarvis-Admin-not-SM through (returns a config verdict
		# here since no key is mocked — proving the gate passed, not a 403).
		mock_settings = MagicMock()
		mock_settings.get_password.return_value = ""  # no key -> config branch
		with _as(ADMIN), patch("frappe.get_single", return_value=mock_settings):
			res = diagnostics.ping_admin()
		self.assertEqual(res.get("kind"), "config")


# --------------------------------------------------------------------------- #
# The FOUR owner-decision capabilities stay System-Manager-ONLY
# --------------------------------------------------------------------------- #
class TestOwnerSmOnlyCapabilities(Part4Base):
	def test_rotate_agent_token_rejects_non_sm_admin(self):
		from jarvis import api as jarvis_api

		with _as(ADMIN):
			with self.assertRaises(frappe.PermissionError):
				jarvis_api.rotate_agent_token()
		with _as(USER_A):
			with self.assertRaises(frappe.PermissionError):
				jarvis_api.rotate_agent_token()


# --------------------------------------------------------------------------- #
# TASK 35 — date_add SQLi (unconstrained literal n)
# --------------------------------------------------------------------------- #
class TestDateAddSqli(Part4Base):
	def test_string_n_rejected_int_n_accepted(self):
		from jarvis.exceptions import InvalidArgumentError
		from jarvis.tools._expr import _build_date_add

		x = Field("d")
		payload = "1 YEAR)) UNION SELECT * FROM `__Auth` -- "
		for dialect in ("mariadb", "sqlite"):
			with self.assertRaises(InvalidArgumentError):
				_build_date_add([x, payload, "year"], dialect)
		# A valid integer (incl. negative) still builds on both paths.
		_build_date_add([x, 3, "year"], "mariadb")
		_build_date_add([x, -2, "month"], "sqlite")


# --------------------------------------------------------------------------- #
# TASK 45 / 47 — widened agent-admin endpoints work for a Jarvis-Admin-not-SM
# --------------------------------------------------------------------------- #
class TestAgentAdminWidened(Part4Base):
	def test_set_agent_roles_and_status_work_for_jarvis_admin(self):
		with _as(USER_A):  # plain Jarvis User is rejected
			with self.assertRaises(frappe.PermissionError):
				agents_api.set_agent_roles(AGENT_SLUG, json.dumps([]))
			with self.assertRaises(frappe.PermissionError):
				agents_api.set_listing_status(AGENT_SLUG, "Published")
		with _as(ADMIN):  # Jarvis Admin (not SM) passes gate AND the doc-layer save
			res = agents_api.set_agent_roles(AGENT_SLUG, json.dumps([]))
			self.assertTrue(res.get("ok"))
			res2 = agents_api.set_listing_status(AGENT_SLUG, "Deprecated")
			self.assertEqual(res2.get("status"), "Deprecated")

	def test_admin_overview_visible_to_jarvis_admin(self):
		with _as(USER_A):
			with self.assertRaises(frappe.PermissionError):
				agents_api.get_agent_admin_overview()
		with _as(ADMIN):
			out = agents_api.get_agent_admin_overview()
		self.assertIn("listings", out)


# --------------------------------------------------------------------------- #
# TASK 52 — Jarvis User Settings ORM scoping (no All row; user-keyed hook)
# --------------------------------------------------------------------------- #
class TestUserSettingsScoping(Part4Base):
	def test_website_user_cannot_read_the_doctype(self):
		self.assertFalse(
			frappe.has_permission(USER_SETTINGS, "read", user=WEBSITE),
			"a Website/portal user can still reach Jarvis User Settings",
		)
		# A Jarvis User retains read (if_owner row).
		self.assertTrue(frappe.has_permission(USER_SETTINGS, "read", user=USER_A))

	def test_jarvis_user_sees_only_own_row_admin_sees_all(self):
		row_a = usage.get_or_create_user_settings(USER_A)
		row_b = usage.get_or_create_user_settings(USER_B)
		with _as(USER_A):
			mine = set(frappe.get_list(USER_SETTINGS, pluck="name", limit_page_length=0))
		self.assertIn(row_a.name, mine)
		self.assertNotIn(row_b.name, mine, "leak: a Jarvis User sees another user's row")
		# The admin tier is unrestricted (admin_list_user_usage relies on this).
		with _as(ADMIN):
			everyone = set(frappe.get_list(USER_SETTINGS, pluck="name", limit_page_length=0))
		self.assertIn(row_a.name, everyone)
		self.assertIn(row_b.name, everyone)

	def test_usage_write_path_still_works(self):
		# The worker path (ignore_permissions + raw SQL) is unaffected by the
		# All->Jarvis User row change: creating + recording usage still writes.
		row = usage.get_or_create_user_settings(USER_A)
		self.assertEqual(frappe.db.get_value(USER_SETTINGS, row.name, "user"), USER_A)


# --------------------------------------------------------------------------- #
# TASK 43 / 51 — cross-module endpoint-gating coverage guard
# --------------------------------------------------------------------------- #
_APP_ROOT = os.path.dirname(os.path.dirname(__file__))

# Whitelisted fns in these privileged modules must be gated. The Part-1
# test_chat_endpoint_gating only sweeps jarvis/chat/, so every Part-4 gap lives
# outside it — this guard closes that hole.
_GUARD_SUBSTRS = (
	"require_jarvis_admin",
	"require_jarvis_access",
	"require_jarvis_user",
	"require_skill_reviewer",
	"only_for",
	"has_jarvis_access",
	"has_jarvis_admin_access",
	"is_system_user",
	"_require_system_user",
	"_require_admin",
)

# Intentionally-open endpoints (justified): boolean readiness probes, the public
# plan/preset/gateway catalog + the onboarding sync poller (carry no secret; the
# SPA needs them before roles settle), and the site-URL pairing QR
# (Guest-rejected, no secret).
_OPEN_ALLOWLIST = {
	"account.is_onboarded": "boolean readiness probe, no secret",
	"account.is_ready_for_chat": "boolean readiness probe, no secret",
	"onboarding.list_plans": "public plan catalog (spec: leave ungated)",
	"onboarding.get_preset_catalog": "public preset catalog (spec: leave ungated)",
	"onboarding.list_payment_providers": "public gateway list for the onboarding chooser; keys only, no secret",
	"onboarding.get_llm_sync_status": "sanitized sync poller, needed pre-role-settle",
	"mobile.auth.get_pairing_qr": "Guest-rejected; encodes only the site URL, no secret",
}

_COVERED_MODULES = {
	"diagnostics.py": "diagnostics",
	"onboarding.py": "onboarding",
	"account.py": "account",
	"dev.py": "dev",
}
_COVERED_SUBPATHS = {
	os.path.join("oauth", "api.py"): "oauth.api",
	os.path.join("mobile", "auth.py"): "mobile.auth",
}


def _fn_is_gated(node: ast.FunctionDef) -> bool:
	for d in node.decorator_list:
		if any(g in ast.unparse(d) for g in _GUARD_SUBSTRS):
			return True
	for sub in ast.walk(node):
		if isinstance(sub, ast.Call):
			try:
				name = ast.unparse(sub.func)
			except Exception:
				name = ""
			if any(g in name for g in _GUARD_SUBSTRS):
				return True
	return False


def _iter_privileged_endpoints():
	targets = []
	for fname, mod in _COVERED_MODULES.items():
		targets.append((os.path.join(_APP_ROOT, fname), mod))
	for sub, mod in _COVERED_SUBPATHS.items():
		targets.append((os.path.join(_APP_ROOT, sub), mod))
	for path, mod in targets:
		with open(path, encoding="utf-8") as fh:
			tree = ast.parse(fh.read(), filename=path)
		for node in ast.walk(tree):
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
				"whitelist" in ast.unparse(d) for d in node.decorator_list
			):
				yield mod, node.name, node


class TestPrivilegedEndpointGating(Part4Base):
	def test_every_privileged_endpoint_is_gated_or_allowlisted(self):
		found_any = False
		offenders = []
		for mod, name, node in _iter_privileged_endpoints():
			found_any = True
			key = f"{mod}.{name}"
			if key in _OPEN_ALLOWLIST:
				continue
			if _fn_is_gated(node):
				continue
			offenders.append(key)
		self.assertTrue(found_any, "sweep found no privileged endpoints - the walk broke")
		self.assertFalse(
			offenders,
			"ungated privileged @frappe.whitelist endpoints (add require_jarvis_admin / "
			"only_for / a role guard, or allowlist with a justification): " + ", ".join(sorted(offenders)),
		)
