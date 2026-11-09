"""Who sees the Payment Calendar entry points: the boot flag helper."""

import sys
from unittest import TestCase
from unittest.mock import patch

import frappe

from jarvis.payment_calendar import visibility
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site

REPORTER = "jarvis.holiday_touch.report.report"
CONTRACT = "jarvis.payment_calendar.sources.check_contract"
APPS = "jarvis.chat.agent_installability.installed_apps"


class TestPaymentCalendarVisibility(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_visibility")
		visibility.reset_contract_cache()
		self.fx = CalendarFixtures()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_visibility")
		frappe.clear_cache(doctype="Purchase Invoice")
		frappe.clear_document_cache("Report", "Accounts Payable")
		frappe.clear_document_cache("Report", "Accounts Receivable")
		visibility.reset_contract_cache()

	def as_user(self, *roles):
		user = self.fx.user(role=roles[0])
		frappe.get_doc("User", user).add_roles(*roles[1:])
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		return user

	def company_list(self, **outcome):
		"""Patch get_list for Company only: other checks use it through get_all."""
		real = frappe.get_list

		def get_list(doctype, *args, **kwargs):
			if doctype == "Company":
				if "error" in outcome:
					raise outcome["error"]
				return outcome["rows"]
			return real(doctype, *args, **kwargs)

		return patch("frappe.get_list", side_effect=get_list)

	def test_administrator_sees_it(self):
		self.assertIs(visibility.can_view_payment_calendar(), True)

	def test_accounts_user_sees_it(self):
		self.as_user("Jarvis User", "Accounts User")
		self.assertIs(visibility.can_view_payment_calendar(), True)

	def test_auditor_sees_it(self):
		self.as_user("Jarvis User", "Auditor")
		self.assertIs(visibility.can_view_payment_calendar(), True)

	def test_user_without_report_access_is_hidden_at_the_report_check(self):
		self.as_user("Jarvis User", "HR Manager")
		with patch(REPORTER) as reporter:
			self.assertIs(visibility.can_view_payment_calendar(), False)
		self.assertIs(visibility._can_run_ap_report(), False)
		self.assertTrue(frappe.get_list("Company", pluck="name", limit=1))
		reporter.assert_not_called()

	def test_a_site_without_erpnext_is_hidden_before_the_contract_check(self):
		with patch(APPS, return_value={"frappe", "jarvis"}), patch(CONTRACT) as contract:
			self.assertIs(visibility.can_view_payment_calendar(), False)
		contract.assert_not_called()

	def test_contract_drift_hides_is_remembered_and_is_reported_once(self):
		with (
			patch(CONTRACT, side_effect=CalendarSourceUnavailable()) as contract,
			patch(REPORTER) as reporter,
		):
			self.assertIs(visibility.can_view_payment_calendar(), False)
			self.assertIs(visibility.can_view_payment_calendar(), False)
		self.assertEqual(contract.call_count, 1)
		reporter.assert_called_once()
		self.assertEqual(reporter.call_args.kwargs["kind"], "payment_calendar_visibility:contract")
		self.assertIn("source contract failed", reporter.call_args.kwargs["title"])
		self.assertIn(frappe.__version__, reporter.call_args.kwargs["title"])

	def test_unexpected_contract_failure_hides_is_not_remembered_and_is_reported_each_time(self):
		for exc in (AttributeError("renamed"), ImportError("gone")):
			visibility.reset_contract_cache()
			with patch(CONTRACT, side_effect=exc) as contract, patch(REPORTER) as reporter:
				self.assertIs(visibility.can_view_payment_calendar(), False)
				self.assertIs(visibility.can_view_payment_calendar(), False)
			self.assertEqual(contract.call_count, 2, exc)
			self.assertEqual(reporter.call_count, 2, exc)
			self.assertEqual(reporter.call_args.kwargs["kind"], "payment_calendar_visibility:contract")

	def test_erpnext_that_cannot_be_imported_hides_is_not_remembered_and_is_reported(self):
		with (
			patch.dict(sys.modules, {"erpnext": None}),
			patch(CONTRACT) as contract,
			patch(REPORTER) as reporter,
		):
			self.assertIs(visibility._source_contract_ok(), False)
			self.assertIs(visibility._source_contract_ok(), False)
		contract.assert_not_called()
		self.assertEqual(reporter.call_count, 2)
		self.assertEqual(reporter.call_args.kwargs["kind"], "payment_calendar_visibility:contract")
		self.assertEqual(visibility._contract_memo, {})

	def test_a_passing_contract_is_remembered(self):
		with patch(CONTRACT) as contract:
			self.assertIs(visibility.can_view_payment_calendar(), True)
			self.assertIs(visibility.can_view_payment_calendar(), True)
		self.assertEqual(contract.call_count, 1)

	def test_accounts_receivable_is_not_required(self):
		frappe.db.set_value("Report", "Accounts Receivable", "disabled", 1)
		frappe.clear_document_cache("Report", "Accounts Receivable")
		self.assertIs(visibility.can_view_payment_calendar(), True)

	def test_a_disabled_report_hides_it(self):
		frappe.db.set_value("Report", "Accounts Payable", "disabled", 1)
		frappe.clear_document_cache("Report", "Accounts Payable")
		self.assertIs(visibility.can_view_payment_calendar(), False)

	def test_a_non_standard_report_hides_it(self):
		frappe.db.set_value("Report", "Accounts Payable", "is_standard", "No")
		frappe.clear_document_cache("Report", "Accounts Payable")
		self.assertIs(visibility.can_view_payment_calendar(), False)

	def test_the_data_gate_still_refuses_a_non_standard_report(self):
		from jarvis.payment_calendar.permissions import require_report

		self.assertEqual(require_report(self.fx.company, "Payable").name, "Accounts Payable")
		frappe.db.set_value("Report", "Accounts Payable", "is_standard", "No")
		frappe.clear_document_cache("Report", "Accounts Payable")
		with self.assertRaises(frappe.PermissionError):
			require_report(self.fx.company, "Payable")

	def test_custom_role_on_the_report_is_respected(self):
		frappe.get_doc(
			{
				"doctype": "Custom Role",
				"report": "Accounts Payable",
				"roles": [{"role": "Accounts Manager"}],
			}
		).insert(ignore_permissions=True)
		self.as_user("Jarvis User", "Accounts User")
		self.assertIs(visibility.can_view_payment_calendar(), False)

	def test_no_visible_company_hides_it(self):
		with self.company_list(rows=[]):
			self.assertIs(visibility.can_view_payment_calendar(), False)

	def test_company_denial_is_quiet(self):
		with self.company_list(error=frappe.PermissionError), patch(REPORTER) as reporter:
			self.assertIs(visibility.can_view_payment_calendar(), False)
		reporter.assert_not_called()

	def test_unexpected_failure_in_the_report_or_company_check_hides_and_is_reported(self):
		for target in (
			patch.object(visibility, "_can_run_ap_report", side_effect=RuntimeError("boom")),
			self.company_list(error=RuntimeError("boom")),
		):
			with target, patch(REPORTER) as reporter:
				self.assertIs(visibility.can_view_payment_calendar(), False)
			reporter.assert_called_once()
			self.assertEqual(reporter.call_args.kwargs["kind"], "payment_calendar_visibility:check")

	def test_report_permission_on_the_reference_doctype_is_required(self):
		report = frappe.get_cached_doc("Report", "Accounts Payable")
		self.assertTrue(report.is_permitted())
		real = frappe.has_permission

		def has_permission(doctype=None, *args, **kwargs):
			if doctype == report.ref_doctype and (args[:1] == ("report",) or kwargs.get("ptype") == "report"):
				return False
			return real(doctype, *args, **kwargs)

		with patch("frappe.has_permission", side_effect=has_permission), patch(REPORTER) as reporter:
			self.assertIs(visibility.can_view_payment_calendar(), False)
		reporter.assert_not_called()

	def test_only_a_standard_accounts_script_report_counts(self):
		for field, value in (("report_type", "Query Report"), ("module", "Buying")):
			original = frappe.db.get_value("Report", "Accounts Payable", field)
			frappe.db.set_value("Report", "Accounts Payable", field, value)
			frappe.clear_document_cache("Report", "Accounts Payable")
			self.assertIs(visibility.can_view_payment_calendar(), False, field)
			frappe.db.set_value("Report", "Accounts Payable", field, original)
			frappe.clear_document_cache("Report", "Accounts Payable")

	def test_the_data_gate_refuses_a_query_report_and_another_module(self):
		from jarvis.payment_calendar.permissions import require_report

		for field, value in (("report_type", "Query Report"), ("module", "Buying")):
			original = frappe.db.get_value("Report", "Accounts Payable", field)
			frappe.db.set_value("Report", "Accounts Payable", field, value)
			frappe.clear_document_cache("Report", "Accounts Payable")
			with self.assertRaises(frappe.PermissionError, msg=field):
				require_report(self.fx.company, "Payable")
			frappe.db.set_value("Report", "Accounts Payable", field, original)
			frappe.clear_document_cache("Report", "Accounts Payable")

	def test_a_missing_accounts_payable_report_hides_quietly(self):
		real = frappe.db.exists

		def exists(doctype, name=None, *args, **kwargs):
			if doctype == "Report" and name == "Accounts Payable":
				return None
			return real(doctype, name, *args, **kwargs)

		with patch.object(frappe.db, "exists", side_effect=exists), patch(REPORTER) as reporter:
			self.assertIs(visibility.can_view_payment_calendar(), False)
		reporter.assert_not_called()

	def test_a_thrown_message_does_not_leak_into_the_response(self):
		def throws():
			frappe.msgprint("noise")
			frappe.throw("denied inside the check")

		frappe.local.message_log = [{"message": "before"}]
		before = list(frappe.local.message_log)
		with patch.object(visibility, "_can_run_ap_report", side_effect=throws), patch(REPORTER):
			self.assertIs(visibility.can_view_payment_calendar(), False)
		self.assertEqual(frappe.local.message_log, before)
		frappe.local.message_log = []
