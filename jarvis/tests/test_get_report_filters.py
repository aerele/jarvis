"""Tests for jarvis.tools.get_report_filters - parse filters from the report's
client script (through nested function braces), fall back to doc-declared
filters, and validate the report name."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.get_report_filters import _parse_js_filters, get_report_filters

# A report .js with: a required Link filter, a filter whose body holds a
# nested-brace function, a Break separator (no fieldname), a second required
# filter, and a trailing formatter() that must NOT leak into the parse.
_SAMPLE_JS = """
frappe.query_reports["Sample"] = {
    filters: [
        { fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company", reqd: 1 },
        {
            fieldname: "account",
            fieldtype: "MultiSelectList",
            get_data: function (txt) {
                return frappe.db.get_link_options("Account", txt, { company: 1 });
            },
        },
        { fieldtype: "Break" },
        { fieldname: "to_date", label: __("To Date"), fieldtype: "Date", reqd: 1 },
    ],
    formatter: function (value, row, column, data) {
        return value;
    },
};
"""


def _a_report() -> str:
	return frappe.get_all("Report", limit=1, pluck="name")[0]


class TestParseJsFilters(FrappeTestCase):
	def test_parses_through_nested_braces_skips_break_and_formatter(self):
		fs = _parse_js_filters(_SAMPLE_JS)
		self.assertEqual([f["fieldname"] for f in fs], ["company", "account", "to_date"])
		by = {f["fieldname"]: f for f in fs}
		self.assertEqual(by["company"]["fieldtype"], "Link")
		self.assertEqual(by["company"]["options"], "Company")
		self.assertTrue(by["company"]["reqd"])
		self.assertEqual(by["account"]["fieldtype"], "MultiSelectList")
		self.assertFalse(by["account"]["reqd"])
		self.assertTrue(by["to_date"]["reqd"])

	def test_no_filters_array_returns_empty(self):
		self.assertEqual(_parse_js_filters("frappe.query_reports['x'] = {};"), [])


class TestGetReportFilters(FrappeTestCase):
	def test_parses_from_script_when_doc_filters_empty(self):
		with patch(
			"jarvis.tools.get_report_filters.get_script", return_value={"script": _SAMPLE_JS, "filters": []}
		):
			out = get_report_filters(_a_report())
		self.assertEqual([f["fieldname"] for f in out["filters"]], ["company", "account", "to_date"])
		self.assertEqual(out["required"], ["company", "to_date"])
		self.assertIn("note", out)

	def test_prefers_doc_declared_filters(self):
		with patch(
			"jarvis.tools.get_report_filters.get_script",
			return_value={
				"script": _SAMPLE_JS,
				"filters": [
					{
						"fieldname": "fiscal_year",
						"label": "Fiscal Year",
						"fieldtype": "Link",
						"options": "Fiscal Year",
						"mandatory": 1,
					}
				],
			},
		):
			out = get_report_filters(_a_report())
		self.assertEqual([f["fieldname"] for f in out["filters"]], ["fiscal_year"])
		self.assertEqual(out["required"], ["fiscal_year"])

	def test_unknown_report_raises(self):
		with self.assertRaises(InvalidArgumentError):
			get_report_filters("No Such Report ZZZ")

	def test_blank_report_raises(self):
		with self.assertRaises(InvalidArgumentError):
			get_report_filters("")
