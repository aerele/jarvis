"""Tests for jarvis.site_profile.apps - the app-level custom/core
classification every customization-discovery consumer shares."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.site_profile import apps as sp_apps

FAKE_APP = "custdisc_fake_app"
FAKE_MODULE = "CustDisc Fake Module"


def _set_override(value: str) -> None:
	frappe.db.set_single_value("Jarvis Settings", "core_apps_override", value)
	# known_apps() reads via get_cached_doc; the Singles write alone won't
	# invalidate an already-cached doc.
	frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")


class TestSiteProfileApps(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("Module Def", FAKE_MODULE):
			frappe.get_doc(
				{
					"doctype": "Module Def",
					"module_name": FAKE_MODULE,
					"custom": 1,
					"app_name": FAKE_APP,
				}
			).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		if frappe.db.exists("Module Def", FAKE_MODULE):
			frappe.delete_doc("Module Def", FAKE_MODULE, force=True, ignore_permissions=True)
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_set_override("")

	def tearDown(self):
		_set_override("")

	def test_custom_apps_subtracts_known(self):
		with patch(
			"jarvis.site_profile.apps._installed_apps",
			return_value=["frappe", "erpnext", "jarvis", FAKE_APP],
		):
			self.assertEqual(sp_apps.custom_apps(), [FAKE_APP])

	def test_vanilla_site_has_no_custom_apps(self):
		with patch(
			"jarvis.site_profile.apps._installed_apps",
			return_value=["frappe", "erpnext", "hrms", "jarvis"],
		):
			self.assertEqual(sp_apps.custom_apps(), [])

	def test_india_compliance_is_core(self):
		"""Persona-taught apps are core: a GST site must not report
		india_compliance's doctypes as customizations (explicit product
		decision, 2026-07-17)."""
		with patch(
			"jarvis.site_profile.apps._installed_apps",
			return_value=["frappe", "erpnext", "india_compliance", "jarvis"],
		):
			self.assertEqual(sp_apps.custom_apps(), [])

	def test_override_extends_known_set(self):
		"""An operator listing an app in core_apps_override stops it being
		reported as a customization."""
		_set_override(FAKE_APP)
		with patch("jarvis.site_profile.apps._installed_apps", return_value=["frappe", FAKE_APP]):
			self.assertEqual(sp_apps.custom_apps(), [])

	def test_override_splits_commas_and_newlines(self):
		_set_override("alpha_app,\nbeta_app")
		known = sp_apps.known_apps()
		self.assertIn("alpha_app", known)
		self.assertIn("beta_app", known)

	def test_custom_module_names_maps_module_def(self):
		with patch("jarvis.site_profile.apps._installed_apps", return_value=["frappe", FAKE_APP]):
			self.assertIn(FAKE_MODULE, sp_apps.custom_module_names())
			self.assertTrue(sp_apps.is_custom_doctype_module(FAKE_MODULE))
			self.assertFalse(sp_apps.is_custom_doctype_module("Accounts"))

	def test_no_custom_apps_means_no_custom_modules(self):
		with patch("jarvis.site_profile.apps._installed_apps", return_value=["frappe", "jarvis"]):
			self.assertEqual(sp_apps.custom_module_names(), set())
			self.assertFalse(sp_apps.is_custom_doctype_module(FAKE_MODULE))

	def test_known_module_names_maps_core_apps(self):
		"""The reverse mapping: core-app modules in, custom-app modules out."""
		known = sp_apps.known_module_names()
		self.assertIn("Accounts", known)  # erpnext module, always on this bench
		self.assertNotIn(FAKE_MODULE, known)
