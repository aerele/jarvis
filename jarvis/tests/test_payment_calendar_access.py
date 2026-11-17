"""Live permission/config changes must invalidate a viewer's snapshot proof."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days

from jarvis.exceptions import PermissionDeniedError
from jarvis.payment_calendar import access, banks
from jarvis.payment_calendar.compatibility import has_native_field_masks
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import update_safety_floor
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class AccessCase(TestCase):
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


class TestPaymentCalendarAccess(AccessCase):
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


class TestAuthorizeScopeForReads(AccessCase):
	"""Cheap-read options of authorize_scope: one account gate, one actor refresh, per-side checks."""

	def scope_fingerprint(self):
		scope = self.current_scope()
		return scope, access.authorize_scope(scope)["fingerprint"]

	def test_the_account_gate_runs_once_per_authorization(self):
		scope = self.current_scope()
		gate = MagicMock(wraps=banks.validate_bank_accounts)
		with (
			patch("jarvis.payment_calendar.scope.validate_bank_accounts", gate),
			patch("jarvis.payment_calendar.banks.validate_bank_accounts", gate),
			patch("jarvis.payment_calendar.access.validate_bank_accounts", gate, create=True),
		):
			access.authorize_scope(scope)
		self.assertEqual(gate.call_count, 1)

	def test_the_account_gate_can_be_skipped_for_a_read(self):
		scope, fingerprint = self.scope_fingerprint()
		with patch("jarvis.payment_calendar.scope.validate_bank_accounts") as gate:
			proof = access.authorize_scope(scope, fingerprint, check_accounts=False)
		gate.assert_not_called()
		self.assertEqual(proof["fingerprint"], fingerprint)

	def test_access_no_longer_repeats_the_account_gate(self):
		self.assertFalse(hasattr(access, "validate_bank_accounts"))

	def test_a_disabled_scope_account_is_a_scope_change_when_a_fingerprint_is_expected(self):
		scope, fingerprint = self.scope_fingerprint()
		frappe.db.set_value("Account", self.fx.bank, "disabled", 1)
		with self.assertRaises(access.ScopeChanged):
			access.authorize_scope(scope, fingerprint)

	def test_a_disabled_scope_account_is_still_invalid_for_a_new_proof(self):
		scope = self.current_scope()
		frappe.db.set_value("Account", self.fx.bank, "disabled", 1)
		with self.assertRaises(frappe.ValidationError):
			access.authorize_scope(scope)

	def test_a_disabled_account_is_a_scope_change_even_without_the_gate(self):
		scope, fingerprint = self.scope_fingerprint()
		frappe.db.set_value("Account", self.fx.bank, "disabled", 1)
		with self.assertRaises(access.ScopeChanged):
			access.authorize_scope(scope, fingerprint, check_accounts=False)

	def test_a_permission_or_lookup_error_of_the_scope_rebuild_is_a_scope_change_when_a_fingerprint_is_expected(
		self,
	):
		scope, fingerprint = self.scope_fingerprint()
		errors = (
			frappe.PermissionError("gone"),
			PermissionDeniedError("gone"),
			frappe.DoesNotExistError("gone"),
		)
		rebuilds = (
			"jarvis.payment_calendar.access.normalize_scope",
			"jarvis.payment_calendar.scope.validate_bank_accounts",
		)
		for error in errors:
			for rebuild in rebuilds:
				with self.subTest(error=type(error).__name__, rebuild=rebuild):
					with patch(rebuild, side_effect=error), self.assertRaises(access.ScopeChanged):
						access.authorize_scope(scope, fingerprint)

	def test_the_same_errors_stay_themselves_for_a_new_proof(self):
		scope = self.current_scope()
		with (
			patch(
				"jarvis.payment_calendar.access.normalize_scope", side_effect=frappe.DoesNotExistError("x")
			),
			self.assertRaises(frappe.DoesNotExistError),
		):
			access.authorize_scope(scope)

	def test_a_changed_site_day_alone_is_an_anchor_change_and_any_other_change_is_a_scope_change(self):
		scope, fingerprint = self.scope_fingerprint()
		tomorrow = add_days(scope["anchor"], 1)
		with patch("jarvis.payment_calendar.scope.nowdate", return_value=tomorrow):
			with self.assertRaises(access.ScopeChanged) as caught:
				access.authorize_scope(scope, fingerprint)
		self.assertEqual(caught.exception.kind, "anchor")
		other = {**scope, "protocol": 99}
		with self.assertRaises(access.ScopeChanged) as caught:
			access.authorize_scope(other, fingerprint)
		self.assertEqual(caught.exception.kind, "scope")

	def test_a_changed_site_day_and_another_change_together_is_a_scope_change(self):
		scope, fingerprint = self.scope_fingerprint()
		tomorrow = add_days(scope["anchor"], 1)
		other = {**scope, "protocol": 99}
		with patch("jarvis.payment_calendar.scope.nowdate", return_value=tomorrow):
			with self.assertRaises(access.ScopeChanged) as caught:
				access.authorize_scope(other, fingerprint)
		self.assertEqual(caught.exception.kind, "scope")

	def test_the_actor_is_refreshed_once_by_default(self):
		scope = self.current_scope()
		with patch.object(access, "refresh_actor_access", wraps=access.refresh_actor_access) as refresh:
			access.authorize_scope(scope)
		self.assertEqual(refresh.call_count, 1)

	def test_a_caller_that_refreshed_the_actor_skips_the_second_refresh(self):
		scope, fingerprint = self.scope_fingerprint()
		with patch.object(access, "refresh_actor_access") as refresh:
			access.authorize_scope(scope, fingerprint, actor_refreshed=True)
		refresh.assert_not_called()

	def test_a_skipped_refresh_still_refuses_a_scope_of_another_actor(self):
		scope = self.current_scope()
		self.finance_user()
		with self.assertRaises(frappe.PermissionError):
			access.authorize_scope(scope, actor_refreshed=True)

	def test_a_read_skips_the_scope_reasons_and_the_floor_lookup_and_a_full_proof_keeps_them(self):
		scope, fingerprint = self.scope_fingerprint()
		with (
			patch.object(access, "incomplete_scope_reasons") as reasons,
			patch.object(access, "get_settings") as floor,
		):
			read = access.authorize_scope(scope, fingerprint, actor_refreshed=True)
			reasons.assert_not_called()
			floor.assert_not_called()
			self.assertEqual(read["fingerprint"], fingerprint)
		with (
			patch.object(access, "incomplete_scope_reasons", return_value=[]) as reasons,
			patch.object(access, "get_settings", return_value={}) as floor,
		):
			full = access.authorize_scope(scope, fingerprint)
			reasons.assert_called_once()
			floor.assert_called_once()
			self.assertEqual(full["fingerprint"], fingerprint)

	def test_each_requested_side_is_checked_for_obligation_scope(self):
		scope, fingerprint = self.scope_fingerprint()
		with patch.object(access, "require_obligation_scope") as check:
			access.authorize_scope(scope, fingerprint, sides=("Payable", "Receivable"))
		self.assertEqual([call.args[0] for call in check.call_args_list], ["Payable", "Receivable"])

	def test_the_sides_of_a_published_result_are_checked_and_an_unavailable_one_is_not(self):
		scope = self.current_scope()
		result = {"sources": {"Payable": {"rows": [], "permission_columns": []}, "Receivable": None}}
		with patch.object(access, "require_obligation_scope") as check:
			access.authorize_scope(scope, result=result)
		self.assertEqual([call.args[0] for call in check.call_args_list], ["Payable"])

	def test_requested_and_published_sides_are_each_checked_once(self):
		scope = self.current_scope()
		result = {"sources": {"Payable": {"rows": [], "permission_columns": []}, "Receivable": None}}
		with patch.object(access, "require_obligation_scope") as check:
			access.authorize_scope(scope, result=result, sides=("Payable", "Receivable"))
		self.assertEqual([call.args[0] for call in check.call_args_list], ["Payable", "Receivable"])

	def test_no_side_is_checked_when_none_is_requested(self):
		with patch.object(access, "require_obligation_scope") as check:
			access.authorize_scope(self.current_scope())
		check.assert_not_called()

	def test_a_v15_sales_person_scope_refuses_receivables_on_a_read(self):
		scope, fingerprint = self.scope_fingerprint()
		v15 = SimpleNamespace(major="15")
		with (
			patch("jarvis.payment_calendar.compatibility.source_profile", return_value=v15),
			patch(
				"jarvis.payment_calendar.compatibility.get_user_permissions",
				return_value={"Sales Person": ["x"]},
			),
		):
			access.authorize_scope(scope, fingerprint, sides=("Payable",))
			with self.assertRaises(CalendarSourceUnavailable):
				access.authorize_scope(scope, fingerprint, sides=("Payable", "Receivable"))
