"""Tests for jarvis.chat.connector_permissions + the Jarvis Connector
controller's scope/uniqueness gates.

NOTE: this worktree is not installed on any bench (per MCP_CONNECTORS_PLAN.md
P0 rules), so these do not run here. They are exercised in the integration
deploy, and MUST pass against a FRESH DB - the local dev site is role-polluted
(a prior migrate seeded ``Jarvis User`` and granted it to existing users), so a
permission test that only looks correct locally is not trustworthy. Mirrors
the fixture shape of test_dashboard_permissions.py: explicit per-user roles,
nothing relies on ambient/seeded role grants.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.connector_permissions import (
	can_edit_connector,
	can_read_connector,
	has_connector_permission,
)
from jarvis.permissions import (
	JARVIS_ADMIN_ROLE,
	JARVIS_USER_ROLE,
	ensure_jarvis_admin_role,
	ensure_jarvis_user_role,
)

CONNECTOR = "Jarvis Connector"

ADMIN_USER = "jarvis-conn-admin@example.com"
PLAIN_A = "jarvis-conn-user-a@example.com"
PLAIN_B = "jarvis-conn-user-b@example.com"


def _ensure_user(email: str, roles: list[str]) -> None:
	"""Create the fixture user if missing; idempotent."""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Conn",
				"last_name": "Test",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", email)
	doc.add_roles(*roles)
	frappe.db.commit()


class _ConnectorPermTestCase(FrappeTestCase):
	def setUp(self):
		ensure_jarvis_user_role()
		ensure_jarvis_admin_role()
		_ensure_user(ADMIN_USER, [JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE])
		_ensure_user(PLAIN_A, [JARVIS_USER_ROLE])
		_ensure_user(PLAIN_B, [JARVIS_USER_ROLE])
		self._orig_user = frappe.session.user
		self._connectors: list[str] = []

	def tearDown(self):
		frappe.set_user(self._orig_user)
		for name in self._connectors:
			if frappe.db.exists(CONNECTOR, name):
				frappe.delete_doc(CONNECTOR, name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _mk(self, scope: str, key: str, owner: str | None = None, **kw) -> str:
		"""Create a connector as Administrator (passes every gate); optionally
		force the owner afterwards (mirrors test_dashboard_permissions._mk -
		set_user_and_timestamp always stamps the session user on insert)."""
		prev = frappe.session.user
		frappe.set_user("Administrator")
		try:
			doc = frappe.get_doc(
				{
					"doctype": CONNECTOR,
					"key": key,
					"label": kw.pop("label", f"perm-{key}"),
					"scope": scope,
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

	def _doc(self, name: str):
		return frappe.get_doc(CONNECTOR, name)

	def _visible_names(self, user: str, names: list[str]) -> set:
		frappe.set_user(user)
		rows = frappe.get_list(CONNECTOR, filters={"name": ["in", names]}, pluck="name")
		return set(rows)


class TestReadMatrix(_ConnectorPermTestCase):
	def test_shared_visible_to_all_jarvis_users(self):
		name = self._mk("Shared", "github")
		doc = self._doc(name)
		for user in (PLAIN_A, PLAIN_B, ADMIN_USER):
			self.assertTrue(can_read_connector(doc, user), user)
		self.assertIn(name, self._visible_names(PLAIN_A, [name]))
		self.assertIn(name, self._visible_names(PLAIN_B, [name]))

	def test_personal_invisible_to_a_different_user(self):
		name = self._mk("Personal", "github", owner=PLAIN_A)
		doc = self._doc(name)
		self.assertTrue(can_read_connector(doc, PLAIN_A))
		self.assertFalse(can_read_connector(doc, PLAIN_B))
		# admin tier does NOT get a bypass on someone else's Personal connector.
		self.assertFalse(can_read_connector(doc, ADMIN_USER))
		self.assertIn(name, self._visible_names(PLAIN_A, [name]))
		self.assertNotIn(name, self._visible_names(PLAIN_B, [name]))
		self.assertNotIn(name, self._visible_names(ADMIN_USER, [name]))
		# Exercise the real dispatcher (frappe.has_permission), not just the pure
		# function above - this is what actually proves the hooks.py wiring
		# (has_permission["Jarvis Connector"]) resolves to the right callable.
		self.assertTrue(frappe.has_permission(CONNECTOR, "read", doc=name, user=PLAIN_A))
		self.assertFalse(frappe.has_permission(CONNECTOR, "read", doc=name, user=PLAIN_B))
		self.assertFalse(frappe.has_permission(CONNECTOR, "read", doc=name, user=ADMIN_USER))

	def test_administrator_reads_everything(self):
		shared = self._mk("Shared", "linear")
		personal = self._mk("Personal", "linear-mine", owner=PLAIN_A)
		for name in (shared, personal):
			self.assertTrue(can_read_connector(self._doc(name), "Administrator"))

	def test_get_list_hook_parity(self):
		shared = self._mk("Shared", "stripe")
		private_a = self._mk("Personal", "stripe-mine", owner=PLAIN_A)
		names = [shared, private_a]
		self.assertEqual(self._visible_names(PLAIN_A, names), {shared, private_a})
		self.assertEqual(self._visible_names(PLAIN_B, names), {shared})


class TestWriteMatrix(_ConnectorPermTestCase):
	def test_shared_writable_only_by_admin_tier(self):
		name = self._mk("Shared", "atlassian")
		doc = self._doc(name)
		self.assertFalse(can_edit_connector(doc, PLAIN_A))
		self.assertTrue(can_edit_connector(doc, ADMIN_USER))
		frappe.set_user(PLAIN_A)
		foreign = frappe.get_doc(CONNECTOR, name)
		foreign.label = "hijack"
		self.assertRaises(frappe.PermissionError, foreign.save)
		frappe.set_user(ADMIN_USER)
		mine = frappe.get_doc(CONNECTOR, name)
		mine.label = "relabeled"
		mine.save()
		self.assertEqual(frappe.db.get_value(CONNECTOR, name, "label"), "relabeled")

	def test_personal_writable_only_by_owner_not_admin_tier(self):
		name = self._mk("Personal", "custom", owner=PLAIN_A)
		doc = self._doc(name)
		self.assertTrue(can_edit_connector(doc, PLAIN_A))
		self.assertFalse(can_edit_connector(doc, PLAIN_B))
		self.assertFalse(can_edit_connector(doc, ADMIN_USER))
		self.assertTrue(has_connector_permission(doc, "delete", PLAIN_A))
		self.assertFalse(has_connector_permission(doc, "delete", ADMIN_USER))

	def test_plain_user_cannot_create_shared_connector(self):
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(
			{
				"doctype": CONNECTOR,
				"key": "gate-shared",
				"label": "Gate Shared",
				"scope": "Shared",
				"base_url": "https://example.invalid/mcp",
			}
		)
		self.assertRaises(frappe.PermissionError, doc.insert)

	def test_plain_user_can_create_own_personal_connector(self):
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(
			{
				"doctype": CONNECTOR,
				"key": "gate-personal",
				"label": "Gate Personal",
				"scope": "Personal",
				"base_url": "https://example.invalid/mcp",
			}
		).insert()
		self._connectors.append(doc.name)
		self.assertEqual(doc.owner, PLAIN_A)

	def test_plain_user_cannot_widen_personal_to_shared(self):
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(
			{
				"doctype": CONNECTOR,
				"key": "gate-widen",
				"label": "Gate Widen",
				"scope": "Personal",
				"base_url": "https://example.invalid/mcp",
			}
		).insert()
		self._connectors.append(doc.name)
		doc.scope = "Shared"
		self.assertRaises(frappe.PermissionError, doc.save)


class TestUniqueness(_ConnectorPermTestCase):
	def test_duplicate_personal_key_for_same_owner_rejected(self):
		self._mk("Personal", "dup", owner=PLAIN_A)
		frappe.set_user(PLAIN_A)
		dup = frappe.get_doc(
			{
				"doctype": CONNECTOR,
				"key": "dup",
				"label": "Dup",
				"scope": "Personal",
				"base_url": "https://example.invalid/mcp",
			}
		)
		self.assertRaises(frappe.ValidationError, dup.insert)

	def test_duplicate_shared_key_rejected(self):
		self._mk("Shared", "dup-shared")
		frappe.set_user(ADMIN_USER)
		dup = frappe.get_doc(
			{
				"doctype": CONNECTOR,
				"key": "dup-shared",
				"label": "Dup Shared",
				"scope": "Shared",
				"base_url": "https://example.invalid/mcp",
			}
		)
		self.assertRaises(frappe.ValidationError, dup.insert)

	def test_personal_and_shared_with_same_key_coexist(self):
		shared = self._mk("Shared", "coexist")
		personal = self._mk("Personal", "coexist", owner=PLAIN_A)
		self.assertNotEqual(shared, personal)
		self.assertTrue(frappe.db.exists(CONNECTOR, shared))
		self.assertTrue(frappe.db.exists(CONNECTOR, personal))

	def test_two_users_personal_connectors_with_same_key_coexist(self):
		a = self._mk("Personal", "twin", owner=PLAIN_A)
		frappe.set_user(PLAIN_B)
		b = frappe.get_doc(
			{
				"doctype": CONNECTOR,
				"key": "twin",
				"label": "Twin",
				"scope": "Personal",
				"base_url": "https://example.invalid/mcp",
			}
		).insert()
		self._connectors.append(b.name)
		self.assertNotEqual(a, b.name)


class TestControllerValidation(_ConnectorPermTestCase):
	def test_non_http_base_url_rejected(self):
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(
			{
				"doctype": CONNECTOR,
				"key": "badurl",
				"label": "Bad URL",
				"scope": "Personal",
				"base_url": "ftp://example.invalid/mcp",
			}
		)
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_key_normalized_to_lowercase(self):
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(
			{
				"doctype": CONNECTOR,
				"key": "GitHub",
				"label": "GitHub",
				"scope": "Personal",
				"base_url": "https://example.invalid/mcp",
			}
		).insert()
		self._connectors.append(doc.name)
		self.assertEqual(doc.key, "github")

	def test_credential_round_trips_via_get_password(self):
		frappe.set_user(PLAIN_A)
		doc = frappe.get_doc(
			{
				"doctype": CONNECTOR,
				"key": "withcred",
				"label": "With Credential",
				"scope": "Personal",
				"base_url": "https://example.invalid/mcp",
				"credential": "super-secret-token",
			}
		).insert()
		self._connectors.append(doc.name)
		reloaded = frappe.get_doc(CONNECTOR, doc.name)
		self.assertEqual(reloaded.get_password("credential", raise_exception=False), "super-secret-token")
		# never plaintext in as_dict / the row's own attribute after reload
		self.assertNotEqual(reloaded.credential, "super-secret-token")
