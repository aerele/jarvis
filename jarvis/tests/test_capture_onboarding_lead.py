"""Tests for jarvis.onboarding.capture_onboarding_lead - the fire-and-forget
mirror of admin's guest lead-capture endpoint (lead-capture + T&C frozen
contract). The one contract this mirror MUST meet: never raise, whatever
admin (or the permission gate) does."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import onboarding


class TestCaptureOnboardingLead(FrappeTestCase):
	def test_swallows_admin_failure(self):
		"""admin_client.capture_onboarding_lead raising must not propagate - the
		Plan-step caller fires this without awaiting it in a way that could break
		the step transition, so a raise here would be a bug regardless of what the
		SPA does with the promise."""
		with patch(
			"jarvis.onboarding.admin_client.capture_onboarding_lead",
			side_effect=RuntimeError("admin unreachable"),
		):
			out = onboarding.capture_onboarding_lead(
				email="lead@example.com",
				company="Acme",
				plan="pro",
				step="plan",
			)
		self.assertEqual(out, {"ok": False})

	def test_forwards_to_admin_client_on_success(self):
		"""The happy path: params land on admin_client.capture_onboarding_lead
		under their FROZEN names, and admin's own answer is returned verbatim."""
		with patch(
			"jarvis.onboarding.admin_client.capture_onboarding_lead",
			return_value={"ok": True},
		) as mock_capture:
			out = onboarding.capture_onboarding_lead(
				email="lead@example.com",
				company="Acme",
				billing={"contact_number": "+91 90000 00000"},
				plan="pro",
				step="plan",
				contact_consent=1,
				site_origin="https://spoofed.example",
				partner_code="PARTNER-1",
			)
		self.assertEqual(out, {"ok": True})
		mock_capture.assert_called_once_with(
			"lead@example.com",
			company="Acme",
			billing={"contact_number": "+91 90000 00000"},
			plan="pro",
			step="plan",
			contact_consent=1,
			site_origin="https://spoofed.example",
			partner_code="PARTNER-1",
		)

	def test_missing_args_never_raises(self):
		"""A call with nothing but the whitelist defaults (every param optional at
		the mirror layer) must not TypeError before the guard even runs."""
		with patch(
			"jarvis.onboarding.admin_client.capture_onboarding_lead",
			return_value={"ok": False},
		):
			out = onboarding.capture_onboarding_lead()
		self.assertEqual(out, {"ok": False})

	def test_permission_failure_swallowed_too(self):
		"""require_jarvis_admin living INSIDE the try means a caller with no
		session/role degrades exactly like an admin-side failure - {"ok": False},
		never a PermissionError escaping a background call the view never
		surfaces."""
		frappe.set_user("Guest")
		try:
			out = onboarding.capture_onboarding_lead(email="lead@example.com", company="Acme")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(out, {"ok": False})

	def test_invalid_email_still_forwards_to_admin(self):
		"""A malformed email must still reach admin_client - this bench does not
		screen it out. Admin validates on its own side, and a dropped attempt would
		just hide the lead from operator follow-up instead of catching anything."""
		with patch(
			"jarvis.onboarding.admin_client.capture_onboarding_lead",
			return_value={"ok": True},
		) as mock_capture:
			out = onboarding.capture_onboarding_lead(email="not-an-email", company="Acme", step="plan")
		self.assertEqual(out, {"ok": True})
		mock_capture.assert_called_once_with(
			"not-an-email",
			company="Acme",
			billing=None,
			plan=None,
			step="plan",
			contact_consent=False,
			site_origin=None,
			partner_code=None,
		)

	def test_blank_email_still_forwards_as_before(self):
		"""A blank/missing email is left to admin_client exactly like before -
		nothing about this endpoint screens on email validity."""
		with patch(
			"jarvis.onboarding.admin_client.capture_onboarding_lead",
			return_value={"ok": False},
		) as mock_capture:
			out = onboarding.capture_onboarding_lead(email="", company="Acme", step="plan")
		self.assertEqual(out, {"ok": False})
		mock_capture.assert_called_once()
