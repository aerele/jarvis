from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar.settings import get_settings, update_safety_floor
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarSettings(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_settings")
		self.fx = CalendarFixtures()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_settings")
		frappe.clear_cache(doctype="Company")

	def test_absent_floor_is_unconfigured_not_an_invented_zero(self):
		result = get_settings(self.fx.company)
		self.assertIsNone(result["safety_floor"])
		self.assertFalse(result["configured"])
		self.assertEqual(result["revision"], 0)

	def test_floor_updates_increment_revision_and_reject_stale_writes(self):
		result = update_safety_floor(self.fx.company, "500000.00", 0)
		self.assertEqual(result["safety_floor"], "500000.00")
		self.assertEqual(result["revision"], 1)
		self.assertTrue(result["configured"])
		with self.assertRaises(frappe.TimestampMismatchError):
			update_safety_floor(self.fx.company, "1000.00", 0)
		self.assertEqual(get_settings(self.fx.company)["safety_floor"], "500000.00")
		self.assertEqual(update_safety_floor(self.fx.company, "0", 1)["revision"], 2)

	def test_invalid_floor_does_not_persist(self):
		for value in ("-0.01", "NaN", "Infinity", "not money", None):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				update_safety_floor(self.fx.company, value, 0)
		self.assertFalse(get_settings(self.fx.company)["configured"])

	def test_accounts_user_cannot_write_even_if_company_is_writable(self):
		with patch("frappe.get_roles", return_value=["Accounts User"]):
			with self.assertRaises(frappe.PermissionError):
				update_safety_floor(self.fx.company, "1000", 0)

	def test_manager_still_requires_company_write_permission(self):
		with (
			patch("frappe.get_roles", return_value=["Accounts Manager"]),
			patch("frappe.has_permission", return_value=False),
		):
			with self.assertRaises(frappe.PermissionError):
				update_safety_floor(self.fx.company, "1000", 0)

	def test_currency_change_makes_old_floor_unconfigured(self):
		update_safety_floor(self.fx.company, "1000", 0)
		frappe.db.set_value("Company", self.fx.company, "default_currency", "USD")
		result = get_settings(self.fx.company)
		self.assertFalse(result["configured"])
		self.assertIsNone(result["safety_floor"])
		self.assertEqual(result["currency"], "USD")

	def test_company_link_rename_does_not_orphan_the_settings_document(self):
		update_safety_floor(self.fx.company, "1000", 0)
		renamed = CalendarFixtures()
		name = frappe.db.get_value("Jarvis Payment Calendar Settings", {"company": self.fx.company}, "name")
		frappe.db.set_value("Jarvis Payment Calendar Settings", name, "company", renamed.company)
		self.assertTrue(get_settings(renamed.company)["configured"])
		self.assertEqual(update_safety_floor(renamed.company, "2000", 1)["revision"], 2)

	def test_real_accounts_user_is_refused_by_service_and_direct_document_validation(self):
		frappe.set_user(self.fx.user("Accounts User"))
		with self.assertRaises(frappe.PermissionError):
			update_safety_floor(self.fx.company, "1000", 0)
		doc = frappe.get_doc(
			{"doctype": "Jarvis Payment Calendar Settings", "company": self.fx.company, "safety_floor": 1000}
		)
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)

	def test_real_manager_cannot_write_outside_company_permission(self):
		other = CalendarFixtures()
		user = self.fx.user()
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "Company",
				"for_value": other.company,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			update_safety_floor(self.fx.company, "1000", 0)

	def test_real_manager_can_save_the_permitted_company_floor(self):
		frappe.set_user(self.fx.user())
		self.assertEqual(update_safety_floor(self.fx.company, "1000", 0)["revision"], 1)
		self.assertEqual(update_safety_floor(self.fx.company, "2000", 1)["safety_floor"], "2000.00")

	def test_real_manager_with_read_only_company_is_refused_by_both_write_paths(self):
		user = self.fx.user()
		frappe.db.set_value("Custom DocPerm", {"parent": "Company", "role": "Accounts Manager"}, "write", 0)
		self.fx.insert(
			"Custom DocPerm",
			"ReadOnlyCompany",
			parent="Company",
			role="Accounts Manager",
			permlevel=0,
			read=1,
			write=0,
			**{"select": 1},
		)
		frappe.clear_cache(doctype="Company")
		frappe.set_user(user)
		self.assertTrue(frappe.has_permission("Company", "read", doc=self.fx.company))
		self.assertFalse(frappe.has_permission("Company", "write", doc=self.fx.company))
		with self.assertRaises(frappe.PermissionError):
			update_safety_floor(self.fx.company, "1000", 0)
		doc = frappe.get_doc(
			{"doctype": "Jarvis Payment Calendar Settings", "company": self.fx.company, "safety_floor": 1000}
		)
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)

	def test_resaving_after_currency_change_configures_the_new_currency(self):
		update_safety_floor(self.fx.company, "1000", 0)
		frappe.db.set_value("Company", self.fx.company, "default_currency", "USD")
		result = update_safety_floor(self.fx.company, "2000", 1)
		self.assertEqual(result["currency"], "USD")
		self.assertTrue(result["configured"])
		self.assertEqual(result["safety_floor"], "2000.00")

	def test_configuration_identity_changes_after_delete_and_recreate(self):
		before = update_safety_floor(self.fx.company, "1000", 0)
		frappe.delete_doc("Jarvis Payment Calendar Settings", before["configuration_id"])
		after = update_safety_floor(self.fx.company, "2000", 0)
		self.assertEqual(before["revision"], after["revision"])
		self.assertNotEqual(before["configuration_id"], after["configuration_id"])
