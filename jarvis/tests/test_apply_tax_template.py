"""A card that sets a tax template but sends no taxes gets the template's rows,
on the preview and the save alike (#2252). No site needed: ERPNext is stubbed."""

import sys
import types
import unittest
from unittest.mock import patch

import frappe

from jarvis.tools.create_doc import apply_tax_template

ROWS = [{"charge_type": "On Net Total", "account_head": "IGST - X", "rate": 18}]


class FakeDoc:
	def __init__(self, options, **values):
		field = frappe._dict(options=options) if options else None
		self.meta = types.SimpleNamespace(get_field=lambda name: field if name == "taxes_and_charges" else None)
		self.values = values
		self.appended = []

	def get(self, key):
		return self.values.get(key)

	def append(self, table, row):
		self.appended.append((table, row))


class TestApplyTaxTemplate(unittest.TestCase):
	def run_apply(self, doc, exists=True, can_read=True):
		calls = []

		def fake_get(master_doctype, master_name):
			calls.append((master_doctype, master_name))
			return ROWS

		stub = types.ModuleType("erpnext.controllers.accounts_controller")
		stub.get_taxes_and_charges = fake_get
		with (
			patch.dict(sys.modules, {"erpnext.controllers.accounts_controller": stub}),
			patch.object(frappe, "db", types.SimpleNamespace(exists=lambda *a: exists)),
			patch.object(frappe, "has_permission", lambda *a, **k: can_read),
		):
			apply_tax_template(doc)
		return calls

	def test_template_without_rows_appends_the_rows(self):
		doc = FakeDoc("Sales Taxes and Charges Template", taxes_and_charges="T")
		calls = self.run_apply(doc)
		self.assertEqual(doc.appended, [("taxes", ROWS[0])])
		self.assertEqual(calls, [("Sales Taxes and Charges Template", "T")])

	def test_purchase_side_uses_its_own_master(self):
		doc = FakeDoc("Purchase Taxes and Charges Template", taxes_and_charges="P")
		calls = self.run_apply(doc)
		self.assertEqual(calls, [("Purchase Taxes and Charges Template", "P")])
		self.assertEqual(len(doc.appended), 1)

	def test_rows_present_are_left_alone(self):
		doc = FakeDoc("Sales Taxes and Charges Template", taxes_and_charges="T", taxes=[{"rate": 5}])
		self.assertEqual(self.run_apply(doc), [])
		self.assertEqual(doc.appended, [])

	def test_no_template_is_a_no_op(self):
		doc = FakeDoc("Sales Taxes and Charges Template")
		self.assertEqual(self.run_apply(doc), [])
		self.assertEqual(doc.appended, [])

	def test_doctype_without_the_field_is_a_no_op(self):
		doc = FakeDoc(None, taxes_and_charges="T")
		self.assertEqual(self.run_apply(doc), [])
		self.assertEqual(doc.appended, [])

	def test_missing_template_is_a_no_op(self):
		doc = FakeDoc("Sales Taxes and Charges Template", taxes_and_charges="gone")
		self.assertEqual(self.run_apply(doc, exists=False), [])
		self.assertEqual(doc.appended, [])

	def test_template_the_user_cannot_read_is_a_no_op(self):
		doc = FakeDoc("Sales Taxes and Charges Template", taxes_and_charges="T")
		self.assertEqual(self.run_apply(doc, can_read=False), [])
		self.assertEqual(doc.appended, [])

	def test_non_string_template_is_a_no_op(self):
		doc = FakeDoc("Sales Taxes and Charges Template", taxes_and_charges={"name": ["like", "%"]})
		self.assertEqual(self.run_apply(doc), [])
		self.assertEqual(doc.appended, [])
