"""validate_script static-checks a Server Script / Script Report body against
the safe_exec sandbox without running it."""

import unittest

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.validate_script import validate_script


class TestValidateScript(unittest.TestCase):
	def test_clean_get_list_script_is_ok(self):
		code = (
			"rows = frappe.get_list('Sales Invoice', filters={'docstatus': 1}, fields=['grand_total'])\n"
			"total = sum(r.grand_total for r in rows)\n"
			"columns = [{'label': 'Total', 'fieldname': 'total', 'fieldtype': 'Currency'}]\n"
			"result = [{'total': total}]\n"
			"data = [columns, result]\n"
		)
		out = validate_script(code, script_type="Script Report")
		self.assertTrue(out["ok"], out["errors"])
		self.assertEqual(out["errors"], [])
		self.assertEqual(out["warnings"], [])

	def test_import_is_rejected(self):
		out = validate_script("import os\nx = 1\n")
		self.assertFalse(out["ok"])
		self.assertTrue(any("import" in e for e in out["errors"]))

	def test_from_import_is_rejected(self):
		out = validate_script("from frappe.utils import flt\nx = flt('1')\n")
		self.assertFalse(out["ok"])
		self.assertTrue(any("import" in e for e in out["errors"]))

	def test_get_all_is_rejected_as_permission_bypass(self):
		out = validate_script("rows = frappe.get_all('Sales Invoice')\n")
		self.assertFalse(out["ok"])
		self.assertTrue(any("User Permissions" in e and "get_list" in e for e in out["errors"]))

	def test_db_sql_is_rejected_as_permission_bypass(self):
		out = validate_script("rows = frappe.db.sql('select name from `tabSales Invoice`')\n")
		self.assertFalse(out["ok"])
		self.assertTrue(any("User Permissions" in e for e in out["errors"]))

	def test_open_and_eval_are_rejected(self):
		out = validate_script("f = open('/etc/passwd')\ny = eval('1+1')\n")
		self.assertFalse(out["ok"])
		self.assertTrue(any("open" in e for e in out["errors"]))
		self.assertTrue(any("eval" in e for e in out["errors"]))

	def test_dunder_access_is_rejected(self):
		out = validate_script("cls = doc.__class__\n")
		self.assertFalse(out["ok"])
		self.assertTrue(any("dunder" in e or "__class__" in e for e in out["errors"]))

	def test_sql_function_field_warns_not_errors(self):
		# get_list with an SQL function as a string field: compiles fine, so it is a
		# warning (ok stays True), not an error - Frappe rejects it only at query time.
		code = "rows = frappe.get_list('Sales Invoice', fields=['customer', 'sum(grand_total) as total'], group_by='customer')\n"
		out = validate_script(code, script_type="Script Report")
		self.assertTrue(out["ok"], out["errors"])
		self.assertEqual(out["errors"], [])
		self.assertTrue(
			any("SQL function" in w and "get_list" in w for w in out["warnings"]), out["warnings"]
		)

	def test_sql_function_outside_fields_is_not_warned(self):
		# a `sum(` in an unrelated string (a message) must not false-warn.
		out = validate_script("frappe.msgprint('The sum(x) total is below')\ndata = 1\n")
		self.assertTrue(out["ok"])
		self.assertEqual(out["warnings"], [])

	def test_frappe_defaults_is_flagged_as_out_of_namespace(self):
		# frappe.defaults.* compiles but is None in safe_exec -> runtime NoneType error.
		out = validate_script("b = frappe.defaults.get_user_default('Branch')\ndata = b\n")
		self.assertFalse(out["ok"])
		self.assertTrue(any("frappe.defaults" in e and "safe_exec namespace" in e for e in out["errors"]))

	def test_get_user_default_is_flagged_as_out_of_namespace(self):
		out = validate_script("c = frappe.get_user_default('Company')\ndata = c\n")
		self.assertFalse(out["ok"])
		self.assertTrue(any("frappe.get_user_default" in e for e in out["errors"]))

	def test_valid_frappe_namespace_calls_are_not_flagged(self):
		# get_list / get_doc / get_meta / db.get_default / utils are all in the namespace.
		code = (
			"co = frappe.db.get_default('company')\n"
			"rows = frappe.get_list('Sales Invoice', fields=['grand_total'], limit_page_length=0)\n"
			"m = frappe.get_meta('Sales Invoice')\n"
			"data = rows\n"
		)
		out = validate_script(code, script_type="Script Report")
		self.assertTrue(out["ok"], out["errors"])
		self.assertEqual(out["errors"], [])

	def test_frappe_qb_is_rejected_as_permission_bypass(self):
		# frappe.qb runs raw SQL (bypasses User Permissions) + Sum/Count NameError.
		code = "si = frappe.qb.DocType('Sales Invoice')\nresult = frappe.qb.from_(si).select(si.name).run()\ndata = result\n"
		out = validate_script(code, script_type="Script Report")
		self.assertFalse(out["ok"])
		self.assertTrue(any("frappe.qb" in e for e in out["errors"]))
		# deduped to a single error even though frappe.qb appears twice
		self.assertEqual(len([e for e in out["errors"] if "frappe.qb" in e]), 1)

	def test_result_columns_pair_is_warned(self):
		# result = [columns, rows] renders blank; the pair must go in `data`.
		code = "columns = [{'label': 'A'}]\nrows = []\nresult = [columns, rows]\n"
		out = validate_script(code, script_type="Script Report")
		self.assertTrue(out["ok"], out["errors"])
		self.assertTrue(any("data" in w and "blank" in w for w in out["warnings"]))

	def test_correct_data_pair_is_not_warned(self):
		code = "columns = [{'label': 'A'}]\nrows = [{'a': 1}]\ndata = [columns, rows]\n"
		out = validate_script(code, script_type="Script Report")
		self.assertTrue(out["ok"], out["errors"])
		self.assertEqual(out["warnings"], [])

	def test_syntax_error_is_reported(self):
		out = validate_script("def broken(:\n    pass\n")
		self.assertFalse(out["ok"])
		self.assertTrue(any("SyntaxError" in e for e in out["errors"]))

	def test_format_calls_are_flagged_with_each_line(self):
		# str.format is a sandbox-blocked attribute read: it compiles but fails at run time.
		code = "a = 1\nx = '{}'.format(a)\ny = 2\nz = '{}'.format(y)\ndata = [x, z]\n"
		out = validate_script(code)
		self.assertFalse(out["ok"])
		blocked = [e for e in out["errors"] if ".format is blocked" in e]
		self.assertEqual(len(blocked), 2, out["errors"])
		self.assertIn("line 2", blocked[0])
		self.assertIn("line 4", blocked[1])

	def test_translated_format_and_frappe_format_are_flagged(self):
		code = "msg = _('Hello {0}').format(doc.name)\nv = frappe.format(doc.grand_total)\n"
		out = validate_script(code)
		self.assertFalse(out["ok"])
		blocked = [e for e in out["errors"] if ".format is blocked" in e]
		self.assertEqual(len(blocked), 2, out["errors"])
		self.assertIn("line 1", blocked[0])
		self.assertIn("line 2", blocked[1])
		self.assertIn("frappe.format_value", blocked[1])

	def test_fstring_script_is_ok(self):
		code = "msg = f'PO {doc.name}'\nlabel = 'PO %s' % doc.name\ndata = [msg, label]\n"
		out = validate_script(code)
		self.assertTrue(out["ok"], out["errors"])
		self.assertEqual(out["errors"], [])

	def test_result_shape_is_unchanged(self):
		out = validate_script("x = '{}'.format(1)\n")
		self.assertEqual(set(out), {"ok", "errors", "warnings"})
		self.assertIsInstance(out["ok"], bool)
		self.assertIsInstance(out["errors"], list)
		self.assertIsInstance(out["warnings"], list)

	def test_syntax_error_is_reported_once_without_helper_error(self):
		out = validate_script("x = '{}'.format(\n")
		self.assertFalse(out["ok"])
		self.assertEqual(len(out["errors"]), 1, out["errors"])
		self.assertIn("SyntaxError", out["errors"][0])
		self.assertFalse(any("blocked" in e for e in out["errors"]))

	def test_empty_code_raises(self):
		with self.assertRaises(InvalidArgumentError):
			validate_script("   ")
