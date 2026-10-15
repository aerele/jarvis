"""admin_client: the Private-mode origin, the widened onboarding host gate and the
signup body (design spec 5.2, 5.3; contract section 4.2)."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client

PUBLIC = "https://8.8.8.8"


def _private(mode="Private", address=PUBLIC):
	return patch.object(admin_client, "_private_network_settings", return_value=(mode, address))


class TestPrivateOrigin(FrappeTestCase):
	def test_private_mode_reports_the_public_address_even_with_a_host_name(self):
		with _private(), patch.dict(frappe.conf, {"host_name": "https://other.example.com"}, clear=False):
			self.assertEqual(admin_client._public_origin(), PUBLIC)
			self.assertEqual(admin_client._public_origin(trust_host=True), PUBLIC)

	def test_not_private_keeps_the_host_name_behaviour(self):
		with _private(mode="", address=""), patch.dict(
			frappe.conf, {"host_name": "https://tenant.example.com"}, clear=False
		):
			self.assertEqual(admin_client._public_origin(), "https://tenant.example.com")

	def test_private_mode_with_an_invalid_address_falls_back(self):
		with _private(address="http://nope.example.com"), patch.dict(
			frappe.conf, {"host_name": "https://tenant.example.com"}, clear=False
		):
			self.assertEqual(admin_client._public_origin(), "https://tenant.example.com")

	def test_test_mode_accepts_a_loopback_http_address(self):
		with _private(address="http://127.0.0.1:8000/"), patch.dict(
			frappe.conf, {"jarvis_private_network_test_mode": 1}, clear=False
		):
			self.assertEqual(admin_client._public_origin(), "http://127.0.0.1:8000")

	def test_settings_read_never_raises(self):
		with patch("frappe.db.get_single_value", side_effect=Exception("no such field")):
			self.assertEqual(admin_client._private_network_settings(), ("", ""))
			self.assertEqual(admin_client.private_public_address(), "")
			self.assertFalse(admin_client.is_private_mode())


class TestOnboardingHostGate(FrappeTestCase):
	def setUp(self):
		frappe.flags.test_host_guard = True  # opt in to the real gate (inert under test otherwise)

	def tearDown(self):
		frappe.flags.test_host_guard = False

	def _no_host_name(self):
		patcher = patch.dict(frappe.conf, {}, clear=False)
		patcher.start()
		self.addCleanup(patcher.stop)
		frappe.conf.pop("host_name", None)
		frappe.conf.pop("hostname", None)

	def test_a_completed_private_setup_passes_while_the_public_check_does_not(self):
		self._no_host_name()
		with _private():
			self.assertFalse(admin_client._public_host_configured())
			self.assertTrue(admin_client._onboarding_host_ok())
			admin_client.assert_public_onboarding_host()  # no raise
			self.assertTrue(admin_client.is_private_mode())

	def test_no_private_setup_and_no_host_name_still_blocks(self):
		self._no_host_name()
		with _private(mode="", address=""):
			self.assertFalse(admin_client._onboarding_host_ok())
			with self.assertRaises(frappe.ValidationError):
				admin_client.assert_public_onboarding_host()

	def test_a_spoofed_request_host_cannot_open_the_gate(self):
		"""The Private branch reads stored settings only. A request Host that looks public
		changes nothing (the original guard's reason for existing)."""
		self._no_host_name()
		with _private(mode="", address=""), patch(
			"frappe.utils.get_url", return_value="https://evil.example.com"
		):
			self.assertFalse(admin_client._onboarding_host_ok())

	def test_private_mode_with_a_bad_address_does_not_open_the_gate(self):
		self._no_host_name()
		with _private(address="https://10.0.0.5"):
			self.assertFalse(admin_client._onboarding_host_ok())

	def test_public_host_behaviour_is_unchanged(self):
		with _private(mode="", address=""), patch.dict(
			frappe.conf, {"host_name": "https://jarvis-e2e.aerele.in"}, clear=False
		):
			self.assertTrue(admin_client._public_host_configured())
			self.assertTrue(admin_client._onboarding_host_ok())


class TestSignupBody(FrappeTestCase):
	def _signup(self, **kwargs):
		with patch.object(admin_client, "_post_guest", return_value={"ok": True}) as post:
			admin_client.signup("e@x.com", "Co", "Annual Plan", **kwargs)
		return post.call_args.kwargs["body"]

	def test_public_signup_body_is_unchanged(self):
		with _private(mode="", address=""):
			body = self._signup()
		self.assertNotIn("network_mode", body)
		self.assertEqual(body["client_capabilities"], admin_client._client_capabilities())
		self.assertNotIn("private_network", body["client_capabilities"])

	def test_private_signup_sends_mode_capability_and_the_public_address(self):
		with _private():
			body = self._signup()
		self.assertEqual(body["network_mode"], "private")
		self.assertEqual(body["client_capabilities"]["private_network"], "v1")
		self.assertEqual(body["client_capabilities"]["pay_page"], admin_client.PAY_PAGE_CAPABILITY)
		self.assertEqual(body["frappe_site_url"], PUBLIC)

	def test_the_capability_constant_is_exact(self):
		self.assertEqual(admin_client.PRIVATE_NETWORK_CAPABILITY, "v1")
