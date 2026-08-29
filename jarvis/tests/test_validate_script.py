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

	def test_syntax_error_is_reported(self):
		out = validate_script("def broken(:\n    pass\n")
		self.assertFalse(out["ok"])
		self.assertTrue(any("SyntaxError" in e for e in out["errors"]))

	def test_empty_code_raises(self):
		with self.assertRaises(InvalidArgumentError):
			validate_script("   ")
