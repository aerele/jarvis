"""Tests for jarvis.onboarding.start_signup's email validation - a malformed
address is rejected with a clean, actionable message right after the
permission gate, before anything else (admin URL config, the reconciliation
check, onboarding_contract.update, admin_client.signup) is ever touched."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client, onboarding


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


class TestOnboardingHostGuard(FrappeTestCase):
	def setUp(self):
		frappe.flags.test_host_guard = True  # opt this suite into the real gate

	def tearDown(self):
		frappe.flags.test_host_guard = False

	def test_localhost_host_is_blocked(self):
		with (
			patch.object(admin_client, "_public_origin", return_value="http://localhost:8002"),
			patch.dict(frappe.conf, {}, clear=False),
		):
			frappe.conf.pop("jarvis_allow_localhost_onboarding", None)
			self.assertFalse(admin_client._onboarding_host_ok())
			with self.assertRaises(frappe.ValidationError):
				admin_client.assert_public_onboarding_host()

	def test_bypass_flag_allows_localhost(self):
		with (
			patch.object(admin_client, "_public_origin", return_value="http://localhost:8002"),
			patch.dict(frappe.conf, {"jarvis_allow_localhost_onboarding": 1}),
		):
			self.assertTrue(admin_client._onboarding_host_ok())
			admin_client.assert_public_onboarding_host()  # no raise

	def test_public_host_passes(self):
		with patch.object(admin_client, "_public_origin", return_value="https://acme.example.com"):
			self.assertTrue(admin_client._onboarding_host_ok())
			admin_client.assert_public_onboarding_host()  # no raise

	def test_probe_error_fails_open(self):
		with patch.object(admin_client, "_public_origin", side_effect=RuntimeError("boom")):
			self.assertTrue(admin_client._onboarding_host_ok())  # error => allow

	def test_start_signup_blocks_on_localhost(self):
		# setUp already set frappe.flags.test_host_guard = True.
		frappe.set_user("Administrator")
		with patch.object(admin_client, "_public_origin", return_value="http://localhost:8002"), \
			 patch.dict(frappe.conf, {}, clear=False):
			frappe.conf.pop("jarvis_allow_localhost_onboarding", None)
			with self.assertRaisesRegex(frappe.ValidationError, "local or non-public address"):
				onboarding.start_signup(
					email="x@acme.com", company="Acme", plan="Pro",
					terms_accepted=True,
				)
