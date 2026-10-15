"""Jarvis Settings gained network_mode and public_address (design spec 5.2). A Desk edit
of a Private address goes through the same rules as the wizard."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestPublicAddressFields(FrappeTestCase):
	def _doc(self, mode, address):
		# Loaded only to call a Document method on it; nothing here is saved.
		doc = frappe.get_single("Jarvis Settings")
		doc.network_mode = mode
		doc.public_address = address
		return doc

	def test_the_fields_exist_on_the_doctype(self):
		meta = frappe.get_meta("Jarvis Settings")
		self.assertTrue(meta.has_field("network_mode"))
		self.assertTrue(meta.has_field("public_address"))
		self.assertEqual(meta.get_field("network_mode").fieldtype, "Select")
		self.assertEqual(meta.get_field("network_mode").options.split("\n"), ["", "Private"])
		self.assertEqual(meta.get_field("public_address").fieldtype, "Data")

	def test_private_mode_with_a_bad_address_is_refused(self):
		doc = self._doc("Private", "http://erp.example.com")
		with self.assertRaises(frappe.ValidationError):
			doc._validate_public_address()

	def test_private_mode_with_a_good_address_is_canonicalised(self):
		doc = self._doc("Private", "HTTPS://Erp.Example.com/")
		doc._validate_public_address()
		self.assertEqual(doc.public_address, "https://erp.example.com")

	def test_blank_mode_ignores_the_address(self):
		doc = self._doc("", "not even a url")
		doc._validate_public_address()  # must not raise
		self.assertEqual(doc.public_address, "not even a url")

	def test_test_mode_allows_a_loopback_http_address(self):
		doc = self._doc("Private", "http://127.0.0.1:8000/")
		with patch.dict(frappe.conf, {"jarvis_private_network_test_mode": 1}, clear=False):
			doc._validate_public_address()
		self.assertEqual(doc.public_address, "http://127.0.0.1:8000")
