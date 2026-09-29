from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
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

	def test_string_filters_are_parsed_before_scope_resolution(self):
		for prepared in (False, True):
			with (
				self.subTest(prepared=prepared),
				patch("jarvis.tools.run_report.frappe.db.exists", return_value=True),
				patch("jarvis.tools.run_report.frappe.db.get_value", return_value=prepared),
				patch("jarvis.tools.run_report.frappe_run_report", return_value={"result": []}) as inline,
				patch(
					"jarvis.tools.run_report._prepared_reports.handle_prepared",
					return_value={"prepared_report": True, "status": "ready", "result": []},
				) as queued,
			):
				result = run_report("Accounts Payable", '{"company": "Other Company"}')
				executed = queued.call_args.args[1] if prepared else inline.call_args.kwargs["filters"]
				self.assertEqual(executed["company"], "Other Company")
				self.assertEqual(result["report_scope"]["company"], "Other Company")

	def test_malformed_filters_on_scoped_report_raise_invalid_argument(self):
		with patch("jarvis.tools.run_report.frappe.db.exists", return_value=True):
			for filters in ("not json", "[1, 2]", [1, 2], 5):
				with self.subTest(filters=filters), self.assertRaises(InvalidArgumentError):
					run_report("Accounts Payable", filters)

	def test_invalid_report_date_raises_invalid_argument(self):
		for report_date in ("not a date", "2026-13-45", "0000-00-00", 20260924):
			with self.subTest(report_date=report_date), self.assertRaises(InvalidArgumentError):
				resolve_scope(REPORT, {"report_date": report_date})

	def test_explicit_company_date_and_account_currency(self):
		filters, scope = resolve_scope(
			REPORT, {"company": "Other Company", "report_date": "2026-01-31", "party_account": "Creditors"}
		)
		self.assertEqual(scope["company"], "Other Company")
		self.assertEqual(scope["report_date"], filters["report_date"])
		self.assertEqual(scope["currency_mode"], "account")
		self.assertNotIn("currency", attach_scope({"result": []}, scope)["report_scope"])

	def test_currency_codes_come_from_all_returned_rows(self):
		_, scope = resolve_scope(REPORT, {"in_party_currency": 1})
		result = attach_scope(
			{"result": [{"currency": "USD"}, {"currency": "INR"}, {"currency": "USD"}]}, scope
		)
		self.assertEqual(result["report_scope"]["currencies"], ["INR", "USD"])
		for rows in ([], [{"currency": "USD"}, {}], [["USD"]]):
			self.assertNotIn("currencies", attach_scope({"result": rows}, scope)["report_scope"])

	def test_prepared_freshness_and_partial_result_are_preserved(self):
		_, scope = resolve_scope(REPORT, {})
		result = attach_scope(
			{
				"result": [],
				"prepared_report": True,
				"status": "ready",
				"as_of": "2026-09-22 10:00:00",
				"row_note": "Showing first 500 rows",
			},
			scope,
		)
		self.assertEqual(result["as_of"], "2026-09-22 10:00:00")
		self.assertEqual(result["row_note"], "Showing first 500 rows")
		self.assertNotIn("currency", result["report_scope"])

	def test_default_company_is_permission_checked(self):
		with patch(f"{MODULE}.frappe.has_permission", return_value=False):
			with self.assertRaises(PermissionDeniedError):
				resolve_scope(REPORT, {})

	def test_denied_default_company_is_not_named(self):
		for gate in ("assert_company_permitted", "frappe.has_permission"):
			side = PermissionDeniedError("no access to company 'Example Company'")
			with (
				self.subTest(gate=gate),
				patch(f"{MODULE}.{gate}", side_effect=side if "assert" in gate else None, return_value=False),
				self.assertRaises(PermissionDeniedError) as denied,
			):
				resolve_scope(REPORT, {})
			self.assertNotIn("Example Company", str(denied.exception))
			self.assertIn("specify a company", str(denied.exception))

	def test_denied_supplied_company_keeps_its_message(self):
		with (
			patch(f"{MODULE}.frappe.has_permission", return_value=False),
			self.assertRaises(PermissionDeniedError) as denied,
		):
			resolve_scope(REPORT, {"company": "Other Company"})
		self.assertEqual(str(denied.exception), "No permission to access the report company")

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
