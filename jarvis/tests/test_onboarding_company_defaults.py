"""onboarding.get_company_onboarding_defaults — ERP-derived billing defaults for
one selected Company (Plan 01, WS-A).

Mock-based on purpose: jarvis is a frappe-only app (ERPNext/India-Compliance are
OPTIONAL — C01-1), so these tests must pass on a frappe-only CI bench where the
Company doctype does not exist and no ERPNext helper is importable. Each test
still exercises the REAL resolution logic — the primary-address filter, the phone
fallback order, the permission short-circuits, the gstin-metadata branch — by
mocking only the frappe data-access boundary underneath it.
"""

import sys
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import onboarding


def _dispatch_get_all(mapping):
	"""Return a frappe.get_all side_effect dispatching on the doctype (1st arg)."""

	def _inner(doctype, *args, **kwargs):
		val = mapping.get(doctype, [])
		return list(val)

	return _inner


class TestCompanyDefaultsEndpoint(FrappeTestCase):
	"""Gates + assembly + allowlist. The two resolvers are patched so this class
	isolates the endpoint's own behavior."""

	def test_non_admin_caller_rejected(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				onboarding.get_company_onboarding_defaults("Aerele")
		finally:
			frappe.set_user("Administrator")

	def test_blank_company_not_found(self):
		out = onboarding.get_company_onboarding_defaults("   ")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "COMPANY_DEFAULTS_NOT_FOUND")

	def test_unknown_company_not_found(self):
		with patch("frappe.db.exists", return_value=False):
			out = onboarding.get_company_onboarding_defaults("Ghost Co")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "COMPANY_DEFAULTS_NOT_FOUND")

	def test_unreadable_company_forbidden(self):
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.has_permission", return_value=False),
		):
			out = onboarding.get_company_onboarding_defaults("Aerele")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["code"], "COMPANY_DEFAULTS_FORBIDDEN")

	def test_assembles_and_allowlists(self):
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.has_permission", return_value=True),
			patch.object(
				onboarding,
				"_resolve_company_contact",
				return_value={"name": "CNT-1", "display_name": "V", "phone": "+91"},
			),
			patch.object(
				onboarding,
				"_resolve_company_billing_address",
				return_value={"name": "ADD-1", "city": "Chennai", "gstin": "33ABCDE1234F1Z5"},
			),
		):
			out = onboarding.get_company_onboarding_defaults("Aerele")
		self.assertTrue(out["ok"])
		self.assertEqual(set(out["data"].keys()), {"company", "contact", "billing_address"})
		self.assertEqual(out["data"]["company"], "Aerele")

	def test_missing_links_are_omitted_not_faked(self):
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.has_permission", return_value=True),
			patch.object(onboarding, "_resolve_company_contact", return_value=None),
			patch.object(onboarding, "_resolve_company_billing_address", return_value=None),
		):
			out = onboarding.get_company_onboarding_defaults("Aerele")
		self.assertTrue(out["ok"])
		self.assertEqual(set(out["data"].keys()), {"company"})


class TestResolveContact(FrappeTestCase):
	_C = "frappe.contacts.doctype.contact.contact.get_default_contact"

	def _contact_row(self, mobile="", phone=""):
		return frappe._dict(
			name="CNT-1", first_name="Vignesh", last_name="S", company_name="Aerele",
			mobile_no=mobile, phone=phone,
		)

	def test_no_default_contact_returns_none(self):
		with patch(self._C, return_value=None):
			self.assertIsNone(onboarding._resolve_company_contact("Aerele"))

	def test_unreadable_contact_leaks_nothing(self):
		with (
			patch(self._C, return_value="CNT-1"),
			patch("frappe.has_permission", return_value=False),
		):
			self.assertIsNone(onboarding._resolve_company_contact("Aerele"))

	def test_mobile_no_wins(self):
		with (
			patch(self._C, return_value="CNT-1"),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.db.get_value", return_value=self._contact_row(mobile="+91-MOBILE", phone="+91-PHONE")),
		):
			out = onboarding._resolve_company_contact("Aerele")
		self.assertEqual(out["phone"], "+91-MOBILE")
		self.assertEqual(out["display_name"], "Vignesh S")

	def test_phone_fallback_when_no_mobile(self):
		with (
			patch(self._C, return_value="CNT-1"),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.db.get_value", return_value=self._contact_row(mobile="", phone="+91-PHONE")),
		):
			out = onboarding._resolve_company_contact("Aerele")
		self.assertEqual(out["phone"], "+91-PHONE")

	def test_child_phone_fallback_when_denormalized_blank(self):
		child_rows = [frappe._dict(phone="+91-CHILD", is_primary_phone=1, is_primary_mobile_no=0)]
		with (
			patch(self._C, return_value="CNT-1"),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.db.get_value", return_value=self._contact_row(mobile="", phone="")),
			patch("frappe.get_all", return_value=child_rows),
		):
			out = onboarding._resolve_company_contact("Aerele")
		self.assertEqual(out["phone"], "+91-CHILD")

	def test_no_phone_anywhere_omits_phone(self):
		with (
			patch(self._C, return_value="CNT-1"),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.db.get_value", return_value=self._contact_row(mobile="", phone="")),
			patch("frappe.get_all", return_value=[]),
		):
			out = onboarding._resolve_company_contact("Aerele")
		self.assertNotIn("phone", out)
		self.assertEqual(out["name"], "CNT-1")


class TestChildPhone(FrappeTestCase):
	"""_contact_child_phone deterministic order: primary mobile > primary phone >
	first non-empty by idx (C01-4)."""

	def _run(self, rows):
		with patch("frappe.get_all", return_value=[frappe._dict(r) for r in rows]):
			return onboarding._contact_child_phone("CNT-1")

	def test_primary_mobile_wins(self):
		phone = self._run([
			{"phone": "A", "is_primary_phone": 1, "is_primary_mobile_no": 0},
			{"phone": "B", "is_primary_phone": 0, "is_primary_mobile_no": 1},
		])
		self.assertEqual(phone, "B")

	def test_primary_phone_over_first(self):
		phone = self._run([
			{"phone": "A", "is_primary_phone": 0, "is_primary_mobile_no": 0},
			{"phone": "B", "is_primary_phone": 1, "is_primary_mobile_no": 0},
		])
		self.assertEqual(phone, "B")

	def test_first_non_empty_fallback(self):
		phone = self._run([
			{"phone": "", "is_primary_phone": 0, "is_primary_mobile_no": 0},
			{"phone": "C", "is_primary_phone": 0, "is_primary_mobile_no": 0},
		])
		self.assertEqual(phone, "C")

	def test_no_rows(self):
		self.assertEqual(self._run([]), "")


class TestResolveAddress(FrappeTestCase):
	def _address_row(self, gstin=None):
		row = frappe._dict(
			name="ADD-1", address_line1="12 MG Road", address_line2="", city="Chennai",
			state="Tamil Nadu", pincode="600001", country="India",
		)
		if gstin is not None:
			row.gstin = gstin
		return row

	def test_primary_filter_is_explicit(self):
		"""C01-2: the Address query MUST filter is_primary_address=1 and exclude
		disabled — never rely on an arbitrary max()/SQL-order row."""
		captured = {}

		def _get_all(doctype, *args, **kwargs):
			if doctype == "Dynamic Link":
				return ["ADD-1", "ADD-2"]
			if doctype == "Address":
				captured["filters"] = kwargs.get("filters")
				return ["ADD-1"]
			return []

		meta = MagicMock()
		meta.has_field.return_value = False
		with (
			patch("frappe.get_all", side_effect=_get_all),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.get_meta", return_value=meta),
			patch("frappe.db.get_value", return_value=self._address_row()),
		):
			onboarding._resolve_company_billing_address("Aerele")
		self.assertEqual(captured["filters"]["is_primary_address"], 1)
		self.assertEqual(captured["filters"]["disabled"], 0)

	def test_no_linked_address_returns_none(self):
		with patch("frappe.get_all", side_effect=_dispatch_get_all({"Dynamic Link": []})):
			self.assertIsNone(onboarding._resolve_company_billing_address("Aerele"))

	def test_no_primary_flagged_returns_none(self):
		"""Linked addresses exist but none is primary -> return NOTHING (never a
		warehouse/shipping address dressed up as billing)."""
		with patch(
			"frappe.get_all",
			side_effect=_dispatch_get_all({"Dynamic Link": ["ADD-1"], "Address": []}),
		):
			self.assertIsNone(onboarding._resolve_company_billing_address("Aerele"))

	def test_unreadable_address_leaks_nothing(self):
		with (
			patch(
				"frappe.get_all",
				side_effect=_dispatch_get_all({"Dynamic Link": ["ADD-1"], "Address": ["ADD-1"]}),
			),
			patch("frappe.has_permission", return_value=False),
		):
			self.assertIsNone(onboarding._resolve_company_billing_address("Aerele"))

	def _resolve_with_meta(self, has_gstin, address_row):
		meta = MagicMock()
		meta.has_field.return_value = has_gstin
		with (
			patch(
				"frappe.get_all",
				side_effect=_dispatch_get_all({"Dynamic Link": ["ADD-1"], "Address": ["ADD-1"]}),
			),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.get_meta", return_value=meta),
			patch("frappe.db.get_value", return_value=address_row),
		):
			return onboarding._resolve_company_billing_address("Aerele")

	def test_gstin_absent_from_metadata(self):
		out = self._resolve_with_meta(has_gstin=False, address_row=self._address_row())
		self.assertNotIn("gstin", out)
		self.assertEqual(out["city"], "Chennai")

	def test_gstin_blank_is_omitted(self):
		out = self._resolve_with_meta(has_gstin=True, address_row=self._address_row(gstin=""))
		self.assertNotIn("gstin", out)

	def test_gstin_present_returned(self):
		out = self._resolve_with_meta(has_gstin=True, address_row=self._address_row(gstin="33ABCDE1234F1Z5"))
		self.assertEqual(out["gstin"], "33ABCDE1234F1Z5")

	def test_response_allowlist_only(self):
		out = self._resolve_with_meta(has_gstin=True, address_row=self._address_row(gstin="33ABCDE1234F1Z5"))
		allowed = {"name", "address_line1", "address_line2", "city", "state", "pincode", "country", "gstin"}
		self.assertTrue(set(out.keys()).issubset(allowed))

	def test_resolves_without_erpnext(self):
		"""C01-1 no-ERPNext leg: even with the ERPNext company helper unimportable,
		address resolution succeeds — proof the frappe-only Dynamic Link path is the
		mechanism, not erpnext.get_default_company_address."""
		with patch.dict(sys.modules, {"erpnext.setup.doctype.company.company": None}):
			out = self._resolve_with_meta(has_gstin=True, address_row=self._address_row(gstin="33ABCDE1234F1Z5"))
		self.assertEqual(out["city"], "Chennai")
