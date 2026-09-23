"""Unit tests for jarvis.chat.dashboard_filters (parse / validate / bind).
Pure functions plus frappe.db.exists lookups; no dashboards are saved here."""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.dashboard_filters import (
	FilterRequiredError,
	bind_spec,
	check_placeholders,
	coerce_values,
	normalize_filter_rows,
	parse_filters_block,
	resolve_defaults,
	validate_filter_defs,
)
from jarvis.exceptions import InvalidArgumentError

ITEM = {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item"}
ITEM_REQ = {**ITEM, "reqd": 1}
HTML = (
	'<html><body><script type="application/json" id="jarvis-filters">'
	'{"filters": [{"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item"}]}'
	"</script></body></html>"
)


class TestParseAndValidate(FrappeTestCase):
	def test_no_block_is_empty(self):
		self.assertEqual(parse_filters_block("<h1>x</h1>"), [])

	def test_block_parses(self):
		self.assertEqual(parse_filters_block(HTML), [ITEM])

	def test_malformed_block_throws(self):
		bad = HTML.replace('{"filters"', '{"filtres"')
		with self.assertRaises(frappe.ValidationError):
			parse_filters_block(bad)

	def test_normalize_fills_defaults(self):
		rows = normalize_filter_rows([ITEM])
		self.assertEqual(
			rows,
			[
				{
					"fieldname": "item",
					"label": "Item",
					"fieldtype": "Link",
					"options": "Item",
					"default_value": "",
					"reqd": 0,
				}
			],
		)

	def test_validate_ok(self):
		validate_filter_defs(normalize_filter_rows([ITEM]))

	def test_validate_rejects_unknown_key(self):
		with self.assertRaises(frappe.ValidationError):
			validate_filter_defs(normalize_filter_rows([{**ITEM, "bogus": 1}]))

	def test_validate_rejects_bad_fieldname(self):
		with self.assertRaises(frappe.ValidationError):
			validate_filter_defs(normalize_filter_rows([{**ITEM, "fieldname": "Item-1"}]))

	def test_validate_rejects_duplicate(self):
		with self.assertRaises(frappe.ValidationError):
			validate_filter_defs(normalize_filter_rows([ITEM, ITEM]))

	def test_validate_rejects_non_link_type(self):
		with self.assertRaises(frappe.ValidationError):
			validate_filter_defs(normalize_filter_rows([{**ITEM, "fieldtype": "Date", "options": ""}]))

	def test_validate_rejects_missing_or_child_options(self):
		with self.assertRaises(frappe.ValidationError):
			validate_filter_defs(normalize_filter_rows([{**ITEM, "options": ""}]))
		with self.assertRaises(frappe.ValidationError):
			validate_filter_defs(normalize_filter_rows([{**ITEM, "options": "Sales Invoice Item"}]))
		with self.assertRaises(frappe.ValidationError):
			validate_filter_defs(normalize_filter_rows([{**ITEM, "options": "No Such DocType"}]))

	def test_validate_rejects_too_many(self):
		rows = normalize_filter_rows([{**ITEM, "fieldname": f"f{i}"} for i in range(9)])
		with self.assertRaises(frappe.ValidationError):
			validate_filter_defs(rows)


class TestPlaceholders(FrappeTestCase):
	def test_query_where_value_ok(self):
		spec = {
			"from": "Sales Invoice",
			"where": [{"field": "customer", "op": "=", "value": {"$filter": "item"}}],
		}
		self.assertEqual(check_placeholders("query", spec, {"item"}), {"item"})

	def test_query_nested_exists_ok(self):
		spec = {
			"from": "Sales Invoice",
			"where": [
				{
					"op": "exists",
					"value": {
						"from": "Sales Invoice Item",
						"where": [{"field": "item_code", "op": "=", "value": {"$filter": "item"}}],
					},
				}
			],
		}
		self.assertEqual(check_placeholders("query", spec, {"item"}), {"item"})

	def test_query_placeholder_in_field_rejected(self):
		spec = {"from": "Sales Invoice", "where": [{"field": {"$filter": "item"}, "op": "=", "value": "x"}]}
		with self.assertRaises(InvalidArgumentError):
			check_placeholders("query", spec, {"item"})

	def test_query_placeholder_in_from_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			check_placeholders("query", {"from": {"$filter": "item"}}, {"item"})

	def test_undeclared_name_rejected(self):
		spec = {
			"from": "Sales Invoice",
			"where": [{"field": "customer", "op": "=", "value": {"$filter": "nope"}}],
		}
		with self.assertRaises(InvalidArgumentError):
			check_placeholders("query", spec, {"item"})

	def test_get_list_dict_and_list_forms(self):
		d = {"doctype": "Sales Invoice", "filters": {"customer": {"$filter": "item"}}}
		lst = {"doctype": "Sales Invoice", "filters": [["customer", "=", {"$filter": "item"}]]}
		self.assertEqual(check_placeholders("get_list", d, {"item"}), {"item"})
		self.assertEqual(check_placeholders("get_list", lst, {"item"}), {"item"})

	def test_get_list_placeholder_in_doctype_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			check_placeholders("get_list", {"doctype": {"$filter": "item"}}, {"item"})

	def test_run_report_filters_ok_and_name_rejected(self):
		ok = {"report_name": "R", "filters": {"item_code": {"$filter": "item"}}}
		self.assertEqual(check_placeholders("run_report", ok, {"item"}), {"item"})
		with self.assertRaises(InvalidArgumentError):
			check_placeholders("run_report", {"report_name": {"$filter": "item"}}, {"item"})


class TestValuesAndBind(FrappeTestCase):
	def setUp(self):
		self.rows = normalize_filter_rows([ITEM])
		self.rows_req = normalize_filter_rows([ITEM_REQ])

	def test_coerce_ok_and_empty(self):
		self.assertEqual(coerce_values(self.rows, {"item": "ITEM-001"}), {"item": "ITEM-001"})
		self.assertEqual(coerce_values(self.rows, None), {"item": ""})
		self.assertEqual(coerce_values(self.rows, {"item": None}), {"item": ""})

	def test_coerce_rejects_unknown_and_bad(self):
		with self.assertRaises(InvalidArgumentError):
			coerce_values(self.rows, {"other": "x"})
		with self.assertRaises(InvalidArgumentError):
			coerce_values(self.rows, {"item": ["a"]})
		with self.assertRaises(InvalidArgumentError):
			coerce_values(self.rows, {"item": "x" * 141})

	def test_resolve_defaults_literal_and_user_default(self):
		rows = normalize_filter_rows([{**ITEM, "default": "ITEM-001"}])
		self.assertEqual(resolve_defaults(rows, "Administrator"), {"item": "ITEM-001"})
		rows = normalize_filter_rows([{**ITEM, "options": "Company", "default": "$user_default"}])
		# frappe.defaults.get_user_default("Company", ...) falls back to the SCRUBBED
		# key ("company") for any key that differs from its scrub (every DocType name
		# does): a value set under the raw "Company" key is never read back. Use the
		# scrubbed key here, matching how ERPNext itself calls set_user_default.
		frappe.defaults.set_user_default("company", "Jarvis Agebt", "Administrator")
		self.assertEqual(resolve_defaults(rows, "Administrator"), {"item": "Jarvis Agebt"})
		frappe.defaults.clear_user_default("company", "Administrator")

	def test_bind_query_substitutes(self):
		spec = {
			"from": "Sales Invoice",
			"where": [{"field": "customer", "op": "=", "value": {"$filter": "item"}}],
		}
		out = bind_spec("query", spec, self.rows, {"item": "C-1"})
		self.assertEqual(out["where"], [{"field": "customer", "op": "=", "value": "C-1"}])
		# input untouched
		self.assertEqual(spec["where"][0]["value"], {"$filter": "item"})

	def test_bind_query_drops_empty_optional(self):
		spec = {
			"from": "Sales Invoice",
			"where": [
				{"field": "docstatus", "op": "=", "value": 1},
				{"field": "customer", "op": "=", "value": {"$filter": "item"}},
			],
			"having": [{"agg": "sum", "field": "grand_total", "op": ">", "value": {"$filter": "item"}}],
		}
		out = bind_spec("query", spec, self.rows, {"item": ""})
		self.assertEqual(out["where"], [{"field": "docstatus", "op": "=", "value": 1}])
		self.assertNotIn("having", out)

	def test_bind_query_required_empty_raises(self):
		spec = {
			"from": "Sales Invoice",
			"where": [{"field": "customer", "op": "=", "value": {"$filter": "item"}}],
		}
		with self.assertRaises(FilterRequiredError) as cm:
			bind_spec("query", spec, self.rows_req, {"item": ""})
		self.assertEqual(cm.exception.fieldname, "item")
		self.assertEqual(cm.exception.label, "Item")

	def test_bind_get_list_dict_and_list(self):
		d = {"doctype": "Sales Invoice", "filters": {"customer": {"$filter": "item"}, "docstatus": 1}}
		self.assertEqual(
			bind_spec("get_list", d, self.rows, {"item": "C-1"})["filters"],
			{"customer": "C-1", "docstatus": 1},
		)
		self.assertEqual(bind_spec("get_list", d, self.rows, {"item": ""})["filters"], {"docstatus": 1})
		lst = {
			"doctype": "Sales Invoice",
			"filters": [["customer", "=", {"$filter": "item"}], ["docstatus", "=", 1]],
		}
		self.assertEqual(
			bind_spec("get_list", lst, self.rows, {"item": ""})["filters"], [["docstatus", "=", 1]]
		)

	def test_bind_run_report(self):
		spec = {"report_name": "R", "filters": {"item_code": {"$filter": "item"}, "company": "X"}}
		self.assertEqual(
			bind_spec("run_report", spec, self.rows, {"item": "I-1"})["filters"],
			{"item_code": "I-1", "company": "X"},
		)
		self.assertEqual(bind_spec("run_report", spec, self.rows, {"item": ""})["filters"], {"company": "X"})
