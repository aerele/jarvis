from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools._report_scope import attach_scope, resolve_scope
from jarvis.tools.run_report import run_report

MODULE = "jarvis.tools._report_scope"
REPORT = "Accounts Payable Summary"


class TestReportScope(TestCase):
	def setUp(self):
		permissions = patch(f"{MODULE}.assert_company_permitted")
		permissions.start()
		self.addCleanup(permissions.stop)
		for target, value in (
			(
				"get_cached_doc",
				frappe._dict(report_type="Script Report", is_standard="Yes", module="Accounts"),
			),
			("db.get_single_value", "Example Company"),
			("has_permission", True),
			("get_cached_value", "INR"),
		):
			mock = patch(f"{MODULE}.frappe.{target}", return_value=value)
			mock.start()
			self.addCleanup(mock.stop)
		date = patch(f"{MODULE}.nowdate", return_value="2026-09-24")
		date.start()
		self.addCleanup(date.stop)

	def test_defaults_are_passed_to_execution_without_mutating_input(self):
		original = {"supplier": "Example Supplier"}
		with (
			patch("jarvis.tools.run_report.frappe.db.exists", return_value=True),
			patch("jarvis.tools.run_report.frappe.db.get_value", return_value=False),
			patch("jarvis.tools.run_report.frappe_run_report", return_value={"result": []}) as execute,
		):
			result = run_report(REPORT, original)
		self.assertEqual(original, {"supplier": "Example Supplier"})
		self.assertEqual(execute.call_args.kwargs["filters"]["company"], "Example Company")
		self.assertEqual(execute.call_args.kwargs["filters"]["report_date"], "2026-09-24")
		self.assertEqual(result["report_scope"]["currency"], "INR")

	def test_explicit_company_date_and_account_currency(self):
		filters, scope = resolve_scope(
			REPORT, {"company": "Other Company", "report_date": "2026-01-31", "party_account": "Creditors"}
		)
		self.assertEqual(scope["company"], "Other Company")
		self.assertEqual(scope["report_date"], filters["report_date"])
		self.assertEqual(scope["currency_mode"], "account")
		self.assertNotIn("currency", attach_scope({"result": []}, scope)["report_scope"])

	def test_default_company_is_permission_checked(self):
		with patch(f"{MODULE}.frappe.has_permission", return_value=False):
			with self.assertRaises(PermissionDeniedError):
				resolve_scope(REPORT, {})

	def test_prepared_lookup_uses_resolved_filters_and_only_ready_gets_scope(self):
		for status in ("started", "generating", "failed", "ready"):
			with (
				self.subTest(status=status),
				patch("jarvis.tools.run_report.frappe.db.exists", return_value=True),
				patch("jarvis.tools.run_report.frappe.db.get_value", return_value=True),
				patch(
					"jarvis.tools.run_report._prepared_reports.handle_prepared",
					return_value={"prepared_report": True, "status": status, "result": []},
				) as execute,
			):
				result = run_report(REPORT)
				self.assertEqual(execute.call_args.args[1]["report_date"], "2026-09-24")
				self.assertEqual("report_scope" in result, status == "ready")
				if status == "ready":
					# A historical prepared copy cannot claim today's currency code.
					self.assertNotIn("currency", result["report_scope"])

	def test_no_scope_for_custom_or_unrecognised_reports(self):
		self.assertEqual(resolve_scope("Sales Register", {}), ({}, None))
		with patch(f"{MODULE}.frappe.get_cached_doc", return_value=frappe._dict(report_type="Custom Report")):
			self.assertEqual(resolve_scope(REPORT, {}), ({}, None))

	def test_error_and_missing_result_do_not_get_scope(self):
		_, scope = resolve_scope(REPORT, {})
		for result in ({"status": "failed", "result": []}, {}, {"result": None}):
			self.assertNotIn("report_scope", attach_scope(result, scope))


class TestReportScopeIntegration(FrappeTestCase):
	def test_standard_reports_execute_with_the_displayed_scope(self):
		frappe.set_user("Administrator")
		if "erpnext" not in frappe.get_installed_apps():
			self.skipTest("ERPNext is not installed on this test site")
		company = frappe.db.get_value("Company", {}, "name")
		if not company:
			self.skipTest("ERPNext company fixture required")
		frappe.db.commit()
		for report in (
			"Accounts Payable",
			"Accounts Payable Summary",
			"Accounts Receivable",
			"Accounts Receivable Summary",
		):
			with self.subTest(report=report):
				result = run_report(report, {"company": company, "report_date": "1900-01-01"})
				self.assertEqual(result["result"], [])
				self.assertEqual(result["report_scope"]["company"], company)
				self.assertEqual(result["report_scope"]["report_date"], "1900-01-01")
				self.assertEqual(
					result["report_scope"]["currency"],
					frappe.get_cached_value("Company", company, "default_currency"),
				)
