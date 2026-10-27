"""Live permission/config changes must invalidate a viewer's snapshot proof."""

from unittest import TestCase

import frappe

from jarvis.payment_calendar import access
from jarvis.payment_calendar.compatibility import has_native_field_masks
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import update_safety_floor
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarAccess(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_access")
		self.fx = CalendarFixtures()
		self.fx.invoice((100,))
		update_safety_floor(self.fx.company, "20", 0)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_access")
		frappe.clear_cache(doctype="Purchase Invoice")
		frappe.clear_document_cache("Report", "Accounts Receivable")

	def current_scope(self):
		return normalize_scope(self.fx.company, [self.fx.bank])

	def finance_user(self):
		user = self.fx.user()
		frappe.get_doc("User", user).add_roles("Jarvis User")
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		return user

	def test_permission_proof_is_stable_without_changes(self):
		scope = self.current_scope()
		first = access.authorize_scope(scope)
		second = access.authorize_scope(scope, expected_fingerprint=first["fingerprint"])
		self.assertEqual(first["fingerprint"], second["fingerprint"])
		self.assertTrue(first["reports"]["Payable"])
		self.assertTrue(first["reports"]["Receivable"])
		self.assertEqual(first["floor"]["safety_floor"], "20.00")

	def test_actual_administrator_actor_is_allowed_without_impersonation(self):
		self.assertEqual(access.authorize_scope(self.current_scope())["actor"], "Administrator")

	def test_a_scope_for_another_actor_is_refused(self):
		scope = self.current_scope()
		self.finance_user()
		with self.assertRaises(frappe.PermissionError):
			access.authorize_scope(scope)

	def test_floor_change_invalidates_the_old_result(self):
		scope = self.current_scope()
		before = access.authorize_scope(scope)
		update_safety_floor(self.fx.company, "30", 1)
		with self.assertRaises(access.ScopeChanged):
			access.authorize_scope(scope, expected_fingerprint=before["fingerprint"])

	def test_site_timezone_change_invalidates_the_old_result(self):
		scope = self.current_scope()
		before = access.authorize_scope(scope)
		current = frappe.db.get_single_value("System Settings", "time_zone")
		frappe.db.set_single_value(
			"System Settings", "time_zone", "UTC" if current != "UTC" else "Asia/Kolkata"
		)
		with self.assertRaises(access.ScopeChanged):
			access.authorize_scope(scope, expected_fingerprint=before["fingerprint"])

	def test_missing_company_currency_is_configuration_unavailable(self):
		frappe.db.set_value("Company", self.fx.company, "default_currency", None)
		with self.assertRaises(CalendarSourceUnavailable):
			access.authorize_scope(self.current_scope())

	def test_disabled_ar_report_preserves_payables_access_without_forecast_coverage(self):
		frappe.db.set_value("Report", "Accounts Receivable", "disabled", 1)
		frappe.clear_document_cache("Report", "Accounts Receivable")
		profile = access.authorize_scope(self.current_scope())
		self.assertTrue(profile["reports"]["Payable"])
		self.assertFalse(profile["reports"]["Receivable"])
		self.assertIn("receivables_unavailable", profile["scope_reasons"])

	def test_role_revocation_is_seen_without_trusting_the_roles_cache(self):
		user = self.finance_user()
		scope = self.current_scope()
		before = access.authorize_scope(scope)
		frappe.set_user("Administrator")
		frappe.db.delete("Has Role", {"parent": user, "parenttype": "User", "role": "Accounts Manager"})
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			access.authorize_scope(scope, expected_fingerprint=before["fingerprint"])
		with self.assertRaises(frappe.PermissionError):
			access.authorize_scope(scope)

	def test_user_deactivation_is_checked_even_if_the_session_exists(self):
		user = self.finance_user()
		scope = self.current_scope()
		frappe.db.set_value("User", user, "enabled", 0, update_modified=False)
		with self.assertRaises(frappe.PermissionError):
			access.authorize_scope(scope)

	def test_permission_row_contents_are_hashed_even_if_modified_does_not_change(self):
		scope = self.current_scope()
		before = access.authorize_scope(scope)
		name = frappe.db.get_value(
			"DocPerm", {"parent": "Company", "role": "Accounts Manager", "permlevel": 0}, "name"
		)
		self.assertTrue(name)
		frappe.db.set_value("DocPerm", name, "write", 0, update_modified=False)
		with self.assertRaises(access.ScopeChanged):
			access.authorize_scope(scope, expected_fingerprint=before["fingerprint"])

	def test_new_supplier_permission_invalidates_the_old_snapshot(self):
		user = self.finance_user()
		scope = self.current_scope()
		before = access.authorize_scope(scope)
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "Supplier",
				"for_value": self.fx.supplier,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.set_user(user)
		with self.assertRaises(access.ScopeChanged):
			access.authorize_scope(scope, expected_fingerprint=before["fingerprint"])

	def test_masked_financial_fields_are_not_read_as_zero_or_unknown_dates(self):
		if not has_native_field_masks():
			self.skipTest("This framework has no native field-mask permissions")
		user = self.finance_user()
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "Property Setter",
				"doctype_or_field": "DocField",
				"doc_type": "Purchase Invoice",
				"field_name": "due_date",
				"property": "mask",
				"property_type": "Check",
				"value": "1",
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Custom DocPerm", {"parent": "Purchase Invoice", "role": "Accounts Manager"}, "mask", 0
		)
		self.fx.insert(
			"Custom DocPerm",
			"InvoiceMask",
			parent="Purchase Invoice",
			role="Accounts Manager",
			permlevel=0,
			read=1,
			report=1,
			mask=0,
			**{"select": 1},
		)
		frappe.clear_cache(doctype="Purchase Invoice")
		frappe.set_user(user)
		with self.assertRaises(CalendarSourceUnavailable):
			access.authorize_scope(self.current_scope())
