"""Item 1 of the run-as / reset gates package: a workspace reset that also wipes
data needs System Manager (the same gate as ``reset_onboarding``); a reset that
keeps the data keeps the Jarvis Admin gate.

Every admin-plane call and every state-changing helper is patched, so the test
only exercises the permission gate.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import onboarding

JADMIN = "reset-gate-jadmin@example.com"
SM = "reset-gate-sm@example.com"


def _ensure_user(email: str, roles: tuple) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
	frappe.get_doc("User", email).add_roles(*roles)
	frappe.clear_cache(user=email)
	return email


class TestResetWipeGate(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from jarvis.permissions import ensure_jarvis_admin_role

		frappe.set_user("Administrator")
		ensure_jarvis_admin_role()
		_ensure_user(JADMIN, ("Jarvis Admin",))
		_ensure_user(SM, ("Jarvis Admin", "System Manager"))
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _call(self, user: str, **kwargs):
		"""Run request_workspace_reset as ``user`` with the admin plane and the
		destructive helpers stubbed; return the mocks."""
		frappe.set_user(user)
		with (
			patch("jarvis.onboarding.admin_client.post_subscription_disconnect"),
			patch("jarvis.onboarding._surface", return_value={"status": "Applied"}) as surface,
			patch("jarvis.onboarding._wipe_workspace_content") as wipe,
			patch("jarvis.onboarding._disconnect_agent_transport"),
		):
			try:
				out = onboarding.request_workspace_reset(**kwargs)
			finally:
				frappe.set_user("Administrator")
		return out, surface, wipe

	def test_jarvis_admin_cannot_reset_with_data_wipe(self):
		frappe.set_user(JADMIN)
		with (
			patch("jarvis.onboarding._surface") as surface,
			patch("jarvis.onboarding._wipe_workspace_content") as wipe,
			patch("jarvis.onboarding._disconnect_agent_transport"),
			self.assertRaises(frappe.PermissionError),
		):
			try:
				onboarding.request_workspace_reset(wipe_data=True)
			finally:
				frappe.set_user("Administrator")
		surface.assert_not_called()
		wipe.assert_not_called()

	def test_string_truthy_wipe_flag_is_gated_too(self):
		frappe.set_user(JADMIN)
		with (
			patch("jarvis.onboarding._surface") as surface,
			patch("jarvis.onboarding._wipe_workspace_content") as wipe,
			self.assertRaises(frappe.PermissionError),
		):
			try:
				onboarding.request_workspace_reset(wipe_data="1")
			finally:
				frappe.set_user("Administrator")
		surface.assert_not_called()
		wipe.assert_not_called()

	def test_jarvis_admin_may_reset_without_data_wipe(self):
		out, surface, wipe = self._call(JADMIN, reason="stuck")
		self.assertEqual(out["status"], "Applied")
		surface.assert_called_once()
		wipe.assert_not_called()

	def test_system_manager_may_reset_with_data_wipe(self):
		out, surface, wipe = self._call(SM, wipe_data=True)
		self.assertEqual(out["status"], "Applied")
		surface.assert_called_once()
		wipe.assert_called_once()
