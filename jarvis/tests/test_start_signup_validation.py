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
	"""The guard reads ONLY site_config ``host_name`` (never the request Host
	header, which is spoofable), so every case here drives it by mutating
	``frappe.conf`` rather than mocking ``_public_origin`` - the old
	implementation's dependency, which the guard no longer calls at all."""

	def setUp(self):
		frappe.flags.test_host_guard = True  # opt this suite into the real gate

	def tearDown(self):
		frappe.flags.test_host_guard = False

	def test_no_host_name_blocks(self):
		"""The key security case: with no configured public host, onboarding is
		blocked regardless of any request Host - there is nothing left for a
		spoofed header to influence."""
		with patch.dict(frappe.conf, {}, clear=False):
			frappe.conf.pop("host_name", None)
			frappe.conf.pop("hostname", None)
			self.assertFalse(admin_client._onboarding_host_ok())
			with self.assertRaises(frappe.ValidationError):
				admin_client.assert_public_onboarding_host()

	def test_localhost_host_name_blocked(self):
		with patch.dict(frappe.conf, {"host_name": "https://foo.localhost"}, clear=False):
			self.assertFalse(admin_client._onboarding_host_ok())

	def test_ip_host_name_blocked(self):
		with patch.dict(frappe.conf, {"host_name": "https://203.0.113.5"}, clear=False):
			self.assertFalse(admin_client._onboarding_host_ok())

	def test_http_scheme_host_name_blocked(self):
		"""_public_origin's host_name short-circuit only fires for https - a
		non-https host_name falls through to the spoofable request Host there,
		so the guard must reject it too rather than accept a real-looking
		domain reached over plain http."""
		with patch.dict(frappe.conf, {"host_name": "http://acme.klerk.in"}, clear=False):
			self.assertFalse(admin_client._onboarding_host_ok())

	def test_public_host_name_allows(self):
		"""The e2e path: a genuinely public ``host_name`` (as e2e sites configure)
		is what makes onboarding work, independent of the request's own Host."""
		with patch.dict(frappe.conf, {"host_name": "https://jarvis-e2e.aerele.in"}, clear=False):
			self.assertTrue(admin_client._onboarding_host_ok())
			admin_client.assert_public_onboarding_host()  # no raise

	def test_bare_public_host_name_allows(self):
		"""A scheme-less host_name (bare domain) is accepted - the impl adds https."""
		with patch.dict(frappe.conf, {"host_name": "jarvis-e2e.aerele.in"}, clear=False):
			self.assertTrue(admin_client._onboarding_host_ok())

	def test_probe_error_fails_open(self):
		with patch.dict(frappe.conf, {"host_name": "https://acme.example.com"}, clear=False):
			with patch.object(admin_client, "_is_real_public_host", side_effect=RuntimeError("boom")):
				self.assertTrue(admin_client._onboarding_host_ok())  # error => allow

	def test_start_signup_blocks_on_localhost(self):
		# setUp already set frappe.flags.test_host_guard = True.
		frappe.set_user("Administrator")
		with patch.dict(frappe.conf, {}, clear=False):
			frappe.conf.pop("host_name", None)
			frappe.conf.pop("hostname", None)
			with self.assertRaisesRegex(frappe.ValidationError, "local or non-public address"):
				onboarding.start_signup(
					email="x@acme.com",
					company="Acme",
					plan="Pro",
					terms_accepted=True,
				)
