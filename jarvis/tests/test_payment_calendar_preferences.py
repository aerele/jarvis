import json
from unittest import TestCase

import frappe

from jarvis.payment_calendar import lifecycle, preferences, store
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import get_settings, update_safety_floor
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarPreferences(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_preferences")
		self.fx = CalendarFixtures()
		self.user = self.fx.user()
		frappe.get_doc("User", self.user).add_roles("Jarvis User")
		frappe.set_user(self.user)

	def tearDown(self):
		store.purge_actor(self.user)
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_preferences")

	def test_roundtrip_contains_only_account_ids_and_company(self):
		result = preferences.save_preferences(self.fx.company, [self.fx.bank, self.fx.bank])
		self.assertEqual(result, {"company": self.fx.company, "accounts": [self.fx.bank]})
		self.assertEqual(preferences.get_preferences(), result)
		doc = frappe.get_doc("Jarvis User Settings", {"user": self.user})
		self.assertEqual(doc.owner, self.user)
		self.assertEqual(json.loads(doc.payment_calendar_preferences), result)

	def test_direct_document_saves_cannot_smuggle_amounts_or_other_accounts(self):
		preferences.save_preferences(self.fx.company, [self.fx.bank])
		doc = frappe.get_doc("Jarvis User Settings", {"user": self.user})
		other = CalendarFixtures()
		for data in (
			{"company": self.fx.company, "accounts": [self.fx.bank], "balance": "9999"},
			{"company": self.fx.company, "accounts": [other.bank]},
		):
			with self.subTest(data=data), self.assertRaises(frappe.ValidationError):
				doc.payment_calendar_preferences = json.dumps(data)
				doc.save()

	def test_read_revalidates_disabled_accounts(self):
		preferences.save_preferences(self.fx.company, [self.fx.bank])
		frappe.db.set_value("Account", self.fx.bank, "disabled", 1)
		self.assertIsNone(preferences.get_preferences())

	def test_normal_profile_save_does_not_release_active_job_slots(self):
		sid, _ = store.admit(self.user, "e" * 64, normalize_scope(self.fx.company, []), "proof")
		doc = frappe.get_doc("User", self.user)
		doc.first_name = "Calendar updated"
		doc.save(ignore_permissions=True)
		self.assertIsNotNone(store.load_status(self.user, sid))

	def test_user_disable_purges_only_feature_preferences_and_snapshots(self):
		preferences.save_preferences(self.fx.company, [self.fx.bank])
		frappe.db.set_value("Jarvis User Settings", {"user": self.user}, "sidebar_order", "keep")
		scope = normalize_scope(self.fx.company, [])
		sid, _ = store.admit(self.user, "a" * 64, scope, "proof")
		frappe.set_user("Administrator")
		update_safety_floor(self.fx.company, "30", 0)
		doc = frappe.get_doc("User", self.user)
		doc.enabled = 0
		lifecycle.on_user_update(doc)
		self.assertIsNone(store.load_status(self.user, sid))
		self.assertFalse(
			frappe.db.get_value("Jarvis User Settings", {"user": self.user}, "payment_calendar_preferences")
		)
		self.assertEqual(
			frappe.db.get_value("Jarvis User Settings", {"user": self.user}, "sidebar_order"), "keep"
		)
		self.assertEqual(get_settings(self.fx.company)["safety_floor"], "30.00")

	def test_workspace_reset_keeps_company_configuration_and_other_preferences(self):
		preferences.save_preferences(self.fx.company, [])
		frappe.set_user("Administrator")
		update_safety_floor(self.fx.company, "30", 0)
		lifecycle.reset_workspace()
		self.assertFalse(
			frappe.db.get_value("Jarvis User Settings", {"user": self.user}, "payment_calendar_preferences")
		)
		self.assertTrue(get_settings(self.fx.company)["configured"])

	def test_another_users_settings_owner_check_is_unchanged(self):
		preferences.save_preferences(self.fx.company, [])
		doc = frappe.get_doc("Jarvis User Settings", {"user": self.user})
		frappe.set_user("Administrator")
		other = self.fx.user()
		frappe.get_doc("User", other).add_roles("Jarvis User")
		frappe.set_user(other)
		doc.notify_enabled = not doc.notify_enabled
		with self.assertRaises(frappe.PermissionError):
			doc.save()
