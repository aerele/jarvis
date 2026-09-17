"""Tenant-local GSTIN autofill: capability gate (India Compliance installed + its autofill API
usable), fail-open lookup, and the require_jarvis_admin gate. IC calls are stubbed at the module
seams (_ic_autofill_usable / _raw_gstin_info) so these run WITHOUT India Compliance installed
(CI's test_site has erpnext+hrms+jarvis, no IC)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jarvis import onboarding

GOOD = "29AAACS0000A1ZC"
_AVAIL = "jarvis.onboarding._gstin_autofill_available"
_RAW = "jarvis.onboarding._raw_gstin_info"
_USABLE = "jarvis.onboarding._ic_autofill_usable"
_APPS = "jarvis.onboarding.frappe.get_installed_apps"


def _ic_info(**over):
	d = frappe._dict(
		gstin=GOOD,
		business_name="Acme Traders Pvt Ltd",
		gst_category="Registered Regular",
		status="Active",
	)
	addr = frappe._dict(
		address_line1="1 Road",
		address_line2="Near Park",
		city="Bengaluru",
		state="Karnataka",
		pincode="560001",
		country="India",
	)
	d.all_addresses = [addr]
	d.permanent_address = addr
	d.update(over)
	return d


class TestGstinAutofillCapability(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def test_unavailable_when_ic_absent(self):
		# No india_compliance -> False, and IC is never imported (the guard short-circuits).
		with patch(_APPS, return_value=["frappe", "erpnext", "jarvis"]):
			self.assertFalse(onboarding._gstin_autofill_available())

	def test_available_gated_by_ic_usable(self):
		apps = ["frappe", "erpnext", "india_compliance", "jarvis"]
		with patch(_APPS, return_value=apps):
			with patch(_USABLE, return_value=True):
				self.assertTrue(onboarding._gstin_autofill_available())
			with patch(_USABLE, return_value=False):
				self.assertFalse(onboarding._gstin_autofill_available())

	def test_fail_safe_false_on_error(self):
		with patch(_APPS, side_effect=RuntimeError("boom")):
			self.assertFalse(onboarding._gstin_autofill_available())

	def test_available_endpoint(self):
		with patch(_AVAIL, return_value=True):
			self.assertEqual(onboarding.gstin_autofill_available(), {"available": True})


class TestGstinAutofillGate(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def test_available_requires_jarvis_admin(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			onboarding.gstin_autofill_available()

	def test_lookup_requires_jarvis_admin(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			onboarding.gstin_autofill(GOOD)


class TestGstinAutofillLookup(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def test_unavailable_when_capability_false(self):
		with patch(_AVAIL, return_value=False), patch(_RAW) as raw:
			self.assertEqual(onboarding.gstin_autofill(GOOD), {"available": False})
			raw.assert_not_called()

	def test_maps_ic_response(self):
		with patch(_AVAIL, return_value=True), patch(_RAW, return_value=_ic_info()):
			r = onboarding.gstin_autofill(GOOD)
		self.assertTrue(r["found"])
		self.assertEqual(r["gstin"], GOOD)
		self.assertEqual(r["business_name"], "Acme Traders Pvt Ltd")
		self.assertEqual(r["status"], "Active")
		self.assertEqual(r["address"]["state"], "Karnataka")
		self.assertEqual(r["address"]["country"], "India")

	def test_invalid_when_validationerror(self):
		# IC's _get_gstin_info validates before its throw_error guard -> raises on a bad GSTIN.
		with patch(_AVAIL, return_value=True), patch(_RAW, side_effect=frappe.ValidationError("bad")):
			self.assertEqual(onboarding.gstin_autofill(GOOD), {"found": False, "reason": "invalid"})

	def test_unavailable_on_error(self):
		with patch(_AVAIL, return_value=True), patch(_RAW, side_effect=RuntimeError("gsp down")):
			self.assertEqual(onboarding.gstin_autofill(GOOD), {"found": False, "reason": "unavailable"})

	def test_mapping_error_is_unavailable(self):
		# The mapping runs INSIDE the fail-open try, so even a _gstin_contract fault degrades to a
		# soft miss rather than a 500 (fail-open is total, not just around the IC call).
		with (
			patch(_AVAIL, return_value=True),
			patch(_RAW, return_value=_ic_info()),
			patch("jarvis.onboarding._gstin_contract", side_effect=RuntimeError("mapping boom")),
		):
			self.assertEqual(onboarding.gstin_autofill(GOOD), {"found": False, "reason": "unavailable"})

	def test_empty_is_unavailable(self):
		with patch(_AVAIL, return_value=True), patch(_RAW, return_value=frappe._dict()):
			self.assertEqual(onboarding.gstin_autofill(GOOD), {"found": False, "reason": "unavailable"})

	def test_status_invalid_is_not_found(self):
		info = _ic_info(status="Invalid", permanent_address=None, all_addresses=[])
		with patch(_AVAIL, return_value=True), patch(_RAW, return_value=info):
			self.assertEqual(onboarding.gstin_autofill(GOOD), {"found": False, "reason": "not_found"})

	def test_null_address_when_no_pradr(self):
		info = _ic_info()
		del info["permanent_address"]
		del info["all_addresses"]
		with patch(_AVAIL, return_value=True), patch(_RAW, return_value=info):
			r = onboarding.gstin_autofill(GOOD)
		self.assertTrue(r["found"])
		self.assertIsNone(r["address"])

	def test_strips_and_clamps(self):
		# \x00 (Cc), \u200b zero-width (Cf), \u2028 line sep (Zl), \u2029 para sep (Zp) placed
		# MID-STRING must be stripped (not just trailing via .strip()); the result clamps to 200.
		bad = "\x00\u200b\u2028\u2029"
		addr = frappe._dict(
			address_line1=f"A{bad}B" + "X" * 300,
			address_line2="",
			city="Bengaluru",
			state="Karnataka",
			pincode="560001",
			country="India",
		)
		with (
			patch(_AVAIL, return_value=True),
			patch(_RAW, return_value=_ic_info(permanent_address=addr, all_addresses=[addr])),
		):
			r = onboarding.gstin_autofill(GOOD)
		for ch in bad:
			self.assertNotIn(ch, r["address"]["address_line1"])
		self.assertEqual(len(r["address"]["address_line1"]), 200)
