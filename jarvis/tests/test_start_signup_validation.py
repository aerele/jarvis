"""Tests for jarvis.onboarding.start_signup's email validation - a malformed
address is rejected with a clean, actionable message right after the
permission gate, before anything else (admin URL config, the reconciliation
check, onboarding_contract.update, admin_client.signup) is ever touched."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import onboarding


class TestStartSignupEmailValidation(FrappeTestCase):
	def test_invalid_email_raises_before_reaching_admin(self):
		"""admin_client.signup is mocked to raise loudly if reached at all - the
		validation must short-circuit before _require_admin_url, so this proves
		the ordering as well as the rejection itself."""
		with patch(
			"jarvis.onboarding.admin_client.signup",
			side_effect=AssertionError("admin_client.signup should never be reached"),
		):
			with self.assertRaises(frappe.ValidationError):
				onboarding.start_signup(
					email="not-an-email",
					company="Acme",
					plan="pro",
					billing={"contact_number": "+91 90000 00000"},
					terms_accepted=True,
				)

	def test_blank_email_also_rejected(self):
		with patch(
			"jarvis.onboarding.admin_client.signup",
			side_effect=AssertionError("admin_client.signup should never be reached"),
		):
			with self.assertRaises(frappe.ValidationError):
				onboarding.start_signup(
					email="   ",
					company="Acme",
					plan="pro",
					billing={"contact_number": "+91 90000 00000"},
					terms_accepted=True,
				)
