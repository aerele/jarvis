"""Tests for the Jarvis no-access page (jarvis.www.jarvis_no_access) and boot flag.

Covers:
- Boot flag: set_jarvis_boot(bootinfo) sets jarvis_has_access True/False per access.
- No-access page gates: unauthorized (render), authorized (redirect to /jarvis),
  Guest (redirect to /login).
- No-access page branding (#773): context.agent_name mirrors the tenant's
  whitelabel Jarvis Settings.agent_name, same lookup as www/jarvis.py and
  www/jarvis_mobile.py, falling back to "Jarvis" only when unset.
- Main app gates: roleless → /jarvis-no-access on both; Guest → /app on jarvis.py,
  but gets the shell on jarvis_mobile.py (PWA scope — its own login route handles Guests).

Hermetic: throwaway System User rows (one per role shape) are created in setUp
and deleted in tearDown; the Jarvis User role is seeded and dropped idempotently.
"""

import os
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.boot import set_jarvis_boot
from jarvis.permissions import JARVIS_USER_ROLE
from jarvis.www import jarvis, jarvis_mobile, jarvis_no_access

JARVIS_ROLE = JARVIS_USER_ROLE

# One throwaway user per role shape under test.
USER_JARVIS = "jarvis-noaccess-juser@example.test"
USER_SM = "jarvis-noaccess-sm@example.test"
USER_NONE = "jarvis-noaccess-none@example.test"


def _ensure_role() -> bool:
	"""Seed the Jarvis User role if absent. Returns True iff we created it
	(so tearDown only drops a role this test introduced)."""
	if frappe.db.exists("Role", JARVIS_ROLE):
		return False
	frappe.get_doc(
		{
			"doctype": "Role",
			"role_name": JARVIS_ROLE,
			"desk_access": 1,
			"is_custom": 1,
		}
	).insert(ignore_permissions=True)
	return True


def _ensure_user(email: str, roles: tuple[str, ...] = ()) -> None:
	"""Create a disposable enabled System User (idempotent) and attach roles."""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "JarvisNoAccess",
				"last_name": "TestUser",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	if roles:
		frappe.get_doc("User", email).add_roles(*roles)


def _delete_user(email: str) -> None:
	"""Delete a user; clean up any owned Jarvis data first."""
	# Drop any conversations the user owns, then the user row itself.
	for conv in frappe.get_all("Jarvis Conversation", filters={"owner": email}, pluck="name"):
		for msg in frappe.get_all("Jarvis Chat Message", filters={"conversation": conv}, pluck="name"):
			frappe.delete_doc("Jarvis Chat Message", msg, ignore_permissions=True, force=True)
		frappe.delete_doc("Jarvis Conversation", conv, ignore_permissions=True, force=True)
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, ignore_permissions=True, force=True)


class TestJarvisNoAccessPage(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		"""Seed the Jarvis User role once for all tests."""
		super().setUpClass()
		cls._created_role = _ensure_role()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		"""Drop the role only if we created it."""
		super().tearDownClass()
		if cls._created_role and frappe.db.exists("Role", JARVIS_ROLE):
			frappe.delete_doc("Role", JARVIS_ROLE, ignore_permissions=True, force=True)
		frappe.db.commit()

	def setUp(self):
		"""Create throwaway test users."""
		self._orig_user = frappe.session.user
		# A prior test's redirect (e.g. a Guest -> /login bounce) must not leak
		# into a test that never triggers a redirect of its own.
		frappe.local.flags.redirect_location = None
		_ensure_user(USER_JARVIS, (JARVIS_ROLE,))
		_ensure_user(USER_SM, ("System Manager",))
		_ensure_user(USER_NONE, ())
		frappe.db.commit()

	def tearDown(self):
		"""Restore original user and clean up test users."""
		frappe.set_user(self._orig_user)
		for email in (USER_JARVIS, USER_SM, USER_NONE):
			_delete_user(email)
		frappe.db.commit()

	# --- (a) set_jarvis_boot ------------------------------------------------- #

	def test_boot_flag_true_for_jarvis_user(self):
		"""Boot sets jarvis_has_access=True for a user with Jarvis User role."""
		frappe.set_user(USER_JARVIS)
		bootinfo = frappe._dict()
		set_jarvis_boot(bootinfo)
		self.assertTrue(bootinfo.jarvis_has_access)

	def test_boot_flag_true_for_system_manager(self):
		"""Boot sets jarvis_has_access=True for System Manager."""
		frappe.set_user(USER_SM)
		bootinfo = frappe._dict()
		set_jarvis_boot(bootinfo)
		self.assertTrue(bootinfo.jarvis_has_access)

	def test_boot_flag_true_for_administrator(self):
		"""Boot sets jarvis_has_access=True for Administrator."""
		frappe.set_user("Administrator")
		bootinfo = frappe._dict()
		set_jarvis_boot(bootinfo)
		self.assertTrue(bootinfo.jarvis_has_access)

	def test_boot_flag_false_for_roleless_user(self):
		"""Boot sets jarvis_has_access=False for a user with no access roles."""
		frappe.set_user(USER_NONE)
		bootinfo = frappe._dict()
		set_jarvis_boot(bootinfo)
		self.assertFalse(bootinfo.jarvis_has_access)

	def test_boot_flag_fail_closed_on_exception(self):
		"""Boot fail-closes to False when has_jarvis_access raises an exception."""
		frappe.set_user(USER_JARVIS)
		bootinfo = frappe._dict()
		with patch("jarvis.permissions.has_jarvis_access", side_effect=Exception("boom")):
			set_jarvis_boot(bootinfo)
		self.assertFalse(bootinfo.jarvis_has_access)

	# --- (b) jarvis_no_access.get_context ----------------------------------- #

	def test_no_access_guest_redirects_to_login(self):
		"""Guest on no-access page redirects to /login."""
		frappe.set_user("Guest")
		with self.assertRaises(frappe.Redirect):
			jarvis_no_access.get_context(frappe._dict())
		self.assertEqual(frappe.local.flags.redirect_location, "/login")

	def test_no_access_authorized_redirects_to_jarvis(self):
		"""Authorized user (has Jarvis User role) redirects to /jarvis."""
		frappe.set_user(USER_JARVIS)
		with self.assertRaises(frappe.Redirect):
			jarvis_no_access.get_context(frappe._dict())
		self.assertEqual(frappe.local.flags.redirect_location, "/jarvis")

	def test_no_access_system_manager_redirects_to_jarvis(self):
		"""System Manager is authorized, so redirects to /jarvis."""
		frappe.set_user(USER_SM)
		with self.assertRaises(frappe.Redirect):
			jarvis_no_access.get_context(frappe._dict())
		self.assertEqual(frappe.local.flags.redirect_location, "/jarvis")

	def test_no_access_unauthorized_renders(self):
		"""Unauthorized signed-in user renders the no-access page with context."""
		frappe.set_user(USER_NONE)
		context = frappe._dict()
		result = jarvis_no_access.get_context(context)
		# Should not raise; should have context keys.
		self.assertIn("user_fullname", result)

	def test_no_access_unauthorized_has_user_fullname(self):
		"""Unauthorized user context includes the expected user_fullname."""
		frappe.set_user(USER_NONE)
		expected_fullname = frappe.utils.get_fullname(USER_NONE)
		context = frappe._dict()
		jarvis_no_access.get_context(context)
		self.assertEqual(context.user_fullname, expected_fullname)

	# --- (b2) whitelabel brand on the no-access page (#773) ------------------ #

	def test_no_access_uses_custom_agent_name(self):
		"""A white-label tenant's agent_name renders on the no-access page
		instead of the hardcoded "Jarvis" (the #773 brand leak)."""
		settings = frappe.get_single("Jarvis Settings")
		original = settings.agent_name or ""
		settings.db_set("agent_name", "Acme Assistant", update_modified=False)
		frappe.db.commit()
		try:
			frappe.set_user(USER_NONE)
			context = frappe._dict()
			jarvis_no_access.get_context(context)
			self.assertEqual(context.agent_name, "Acme Assistant")
		finally:
			frappe.get_single("Jarvis Settings").db_set("agent_name", original, update_modified=False)
			frappe.db.commit()

	def test_no_access_falls_back_to_jarvis_when_agent_name_blank(self):
		"""A non-white-label tenant (agent_name unset) keeps the "Jarvis"
		default, so this fix does not change existing tenants."""
		settings = frappe.get_single("Jarvis Settings")
		original = settings.agent_name or ""
		settings.db_set("agent_name", "", update_modified=False)
		frappe.db.commit()
		try:
			frappe.set_user(USER_NONE)
			context = frappe._dict()
			jarvis_no_access.get_context(context)
			self.assertEqual(context.agent_name, "Jarvis")
		finally:
			frappe.get_single("Jarvis Settings").db_set("agent_name", original, update_modified=False)
			frappe.db.commit()

	def test_no_access_html_renders_custom_agent_name_not_jarvis(self):
		"""The template itself (not just the context dict) consumes agent_name:
		rendering jarvis_no_access.html with a custom name must not fall back
		to the literal "Jarvis" copy anywhere in the visible strings."""
		import jinja2

		html_path = os.path.join(frappe.get_app_path("jarvis"), "www", "jarvis_no_access.html")
		with open(html_path) as f:
			template_src = f.read()
		html = jinja2.Template(template_src).render(agent_name="Acme Assistant", user_fullname="Priya Sharma")
		self.assertIn("You need access to Acme Assistant", html)
		self.assertIn("and Acme Assistant is all yours", html)
		self.assertNotIn("You need access to Jarvis", html)
		self.assertNotIn("and Jarvis is all yours", html)

	# --- (c) www.jarvis gate ------------------------------------------------- #

	def test_jarvis_guest_redirects_to_app(self):
		"""Guest on /jarvis redirects to /app."""
		frappe.set_user("Guest")
		with self.assertRaises(frappe.Redirect):
			jarvis.get_context(frappe._dict())
		self.assertEqual(frappe.local.flags.redirect_location, "/app")

	def test_jarvis_unauthorized_redirects_to_no_access(self):
		"""Roleless user on /jarvis redirects to /jarvis-no-access."""
		frappe.set_user(USER_NONE)
		with self.assertRaises(frappe.Redirect):
			jarvis.get_context(frappe._dict())
		self.assertEqual(frappe.local.flags.redirect_location, "/jarvis-no-access")

	def test_jarvis_authorized_renders(self):
		"""Authorized user on /jarvis renders the app."""
		frappe.set_user(USER_JARVIS)
		context = frappe._dict()
		result = jarvis.get_context(context)
		# Should not raise; should have boot context.
		self.assertIn("boot", result)
		self.assertIsInstance(result.boot, dict)

	def test_jarvis_system_manager_renders(self):
		"""System Manager on /jarvis renders the app."""
		frappe.set_user(USER_SM)
		context = frappe._dict()
		result = jarvis.get_context(context)
		# Should not raise; should have boot context.
		self.assertIn("boot", result)
		self.assertIsInstance(result.boot, dict)

	# --- (d) www.jarvis_mobile gate ------------------------------------------ #

	def test_jarvis_mobile_guest_gets_shell(self):
		"""Guest on /jarvis-mobile gets the shell (PWA scope: the app's own
		login route handles them — bouncing out of /jarvis-mobile would eject
		the installed PWA into a browser tab)."""
		frappe.set_user("Guest")
		context = frappe._dict()
		result = jarvis_mobile.get_context(context)
		self.assertIn("boot", result)

	def test_jarvis_mobile_unauthorized_redirects_to_no_access(self):
		"""Roleless user on /jarvis-mobile redirects to /jarvis-no-access."""
		frappe.set_user(USER_NONE)
		with self.assertRaises(frappe.Redirect):
			jarvis_mobile.get_context(frappe._dict())
		self.assertEqual(frappe.local.flags.redirect_location, "/jarvis-no-access")

	def test_jarvis_mobile_authorized_renders(self):
		"""Authorized user on /jarvis-mobile renders the app."""
		frappe.set_user(USER_JARVIS)
		context = frappe._dict()
		result = jarvis_mobile.get_context(context)
		# Should not raise; should have boot context.
		self.assertIn("boot", result)
		self.assertIsInstance(result.boot, dict)

	def test_jarvis_mobile_system_manager_renders(self):
		"""System Manager on /jarvis-mobile renders the app."""
		frappe.set_user(USER_SM)
		context = frappe._dict()
		result = jarvis_mobile.get_context(context)
		# Should not raise; should have boot context.
		self.assertIn("boot", result)
		self.assertIsInstance(result.boot, dict)
