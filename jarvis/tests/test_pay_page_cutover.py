"""plan-09 WS7 backend: the bench half of the admin-hosted pay-page cutover.

Covers the pieces the frontend cannot: the ``augment_pay_page`` attestation that
turns admin's token answer into a navigable one (or fails it closed), the shared
non-navigable origin digest, and the ``payment_ui_v2`` rollout flag. All pure /
config-driven; no network.
"""

import hashlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import onboarding
from jarvis import onboarding_contract as oc


class TestNormalizePayOrigin(FrappeTestCase):
	def test_a_clean_https_origin_passes(self):
		self.assertEqual(oc._normalize_pay_origin("https://fleet.klerk.in"), "https://fleet.klerk.in")

	def test_host_is_lowercased_and_a_trailing_slash_is_dropped(self):
		self.assertEqual(oc._normalize_pay_origin("https://Fleet.Klerk.IN/"), "https://fleet.klerk.in")

	def test_non_https_is_rejected(self):
		self.assertEqual(oc._normalize_pay_origin("http://fleet.klerk.in"), "")

	def test_a_path_query_or_fragment_is_rejected(self):
		self.assertEqual(oc._normalize_pay_origin("https://fleet.klerk.in/jarvis-checkout"), "")
		self.assertEqual(oc._normalize_pay_origin("https://fleet.klerk.in?x=1"), "")

	def test_empty_or_dotless_is_rejected(self):
		self.assertEqual(oc._normalize_pay_origin(""), "")
		self.assertEqual(oc._normalize_pay_origin(None), "")
		self.assertEqual(oc._normalize_pay_origin("https://localhost"), "")

	def test_the_digest_is_sha256_of_the_normalized_origin(self):
		origin = "https://fleet.klerk.in"
		self.assertEqual(oc.pay_origin_digest(origin), hashlib.sha256(origin.encode("utf-8")).hexdigest())


class TestAugmentPayPage(FrappeTestCase):
	ORIGIN = "https://fleet.klerk.in"

	def setUp(self):
		self.digest = hashlib.sha256(self.ORIGIN.encode("utf-8")).hexdigest()

	def tearDown(self):
		frappe.conf.pop("jarvis_pay_origin", None)

	def test_no_token_answer_is_returned_untouched(self):
		data = {"code": "PAYMENT_CONFIRMATION_PENDING", "amount_inr": 1500}
		self.assertEqual(oc.augment_pay_page(dict(data)), data)
		self.assertNotIn("pay_origin", oc.augment_pay_page(dict(data)))

	def test_token_with_matching_origin_is_attested(self):
		frappe.conf["jarvis_pay_origin"] = self.ORIGIN
		out = oc.augment_pay_page({"pay_page_token": "tok", "pay_origin_digest": self.digest})
		self.assertEqual(out["pay_origin"], self.ORIGIN)
		self.assertTrue(out["pay_origin_attested"])

	def test_token_with_a_MISMATCHED_digest_is_NOT_attested(self):
		# The digest assertion / mutation: a split-brain (bench pointed at one
		# origin, admin minted a token for another) fails closed - never navigates.
		frappe.conf["jarvis_pay_origin"] = self.ORIGIN
		out = oc.augment_pay_page({"pay_page_token": "tok", "pay_origin_digest": "deadbeef"})
		self.assertEqual(out["pay_origin"], self.ORIGIN)
		self.assertFalse(out["pay_origin_attested"])

	def test_token_with_NO_configured_origin_fails_closed(self):
		frappe.conf.pop("jarvis_pay_origin", None)
		out = oc.augment_pay_page({"pay_page_token": "tok", "pay_origin_digest": self.digest})
		self.assertEqual(out["pay_origin"], "")
		self.assertFalse(out["pay_origin_attested"])

	def test_token_with_no_admin_digest_cannot_be_attested(self):
		frappe.conf["jarvis_pay_origin"] = self.ORIGIN
		out = oc.augment_pay_page({"pay_page_token": "tok"})
		self.assertFalse(out["pay_origin_attested"])

	def test_success_envelope_carries_the_attestation_through(self):
		frappe.conf["jarvis_pay_origin"] = self.ORIGIN
		env = oc.success({"pay_page_token": "tok", "pay_origin_digest": self.digest})
		self.assertEqual(env["data"]["pay_origin"], self.ORIGIN)
		self.assertTrue(env["data"]["pay_origin_attested"])

	def test_success_envelope_strips_credentials_before_augmenting(self):
		# augment must run over the credential-stripped copy, not raw admin data.
		frappe.conf["jarvis_pay_origin"] = self.ORIGIN
		env = oc.success({"pay_page_token": "tok", "pay_origin_digest": self.digest, "api_secret": "s"})
		self.assertNotIn("api_secret", env["data"])
		self.assertTrue(env["data"]["pay_origin_attested"])


class TestPaymentUiV2Flag(FrappeTestCase):
	def tearDown(self):
		frappe.conf.pop("jarvis_payment_ui_v2", None)

	def test_default_is_ON(self):
		frappe.conf.pop("jarvis_payment_ui_v2", None)
		self.assertTrue(onboarding._payment_ui_v2_enabled())

	def test_explicit_zero_disables(self):
		frappe.conf["jarvis_payment_ui_v2"] = 0
		self.assertFalse(onboarding._payment_ui_v2_enabled())

	def test_list_payment_providers_carries_the_flag(self):
		frappe.conf["jarvis_payment_ui_v2"] = 0
		with patch(
			"jarvis.admin_client.get_payment_providers",
			return_value={"providers": ["razorpay"], "default": "razorpay"},
		):
			out = onboarding.list_payment_providers()
		self.assertFalse(out["payment_ui_v2"])

	def test_list_payment_providers_flag_present_even_on_the_fail_open_path(self):
		frappe.conf.pop("jarvis_payment_ui_v2", None)
		with patch("jarvis.admin_client.get_payment_providers", side_effect=Exception("boom")):
			out = onboarding.list_payment_providers()
		self.assertEqual(out["providers"], ["razorpay"])
		self.assertTrue(out["payment_ui_v2"])
