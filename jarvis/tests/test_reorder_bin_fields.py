"""The Reorder review reads Bin fields on an ERPNext that predates the Bin constants."""

import builtins
import unittest
from unittest.mock import patch

from jarvis.chat import reorder_review

_CONSTANTS = {"RECALCULATED_FIELDS", "PROJECTED_QTY_ADDITIONS", "PROJECTED_QTY_DEDUCTIONS"}


def _older_erpnext():
	real_import = builtins.__import__

	def _import(name, globals=None, locals=None, fromlist=(), level=0):
		if name == "erpnext.stock.doctype.bin.bin" and _CONSTANTS & set(fromlist or ()):
			raise ImportError("cannot import name from 'erpnext.stock.doctype.bin.bin'")
		return real_import(name, globals, locals, fromlist, level)

	return patch("builtins.__import__", side_effect=_import)


class TestBinFields(unittest.TestCase):
	def test_older_erpnext_gets_the_projected_qty_and_recalculated_fields(self):
		with _older_erpnext():
			additions, deductions = reorder_review.bin_projected_qty_fields()
			recalculated = reorder_review.bin_recalculated_fields()
		self.assertIn("actual_qty", additions)
		self.assertIn("reserved_qty_for_production_plan", deductions)
		self.assertEqual(set(recalculated), {"valuation_rate", "stock_value", *additions, *deductions})

	def test_fallbacks_match_erpnext_when_it_has_the_constants(self):
		try:
			from erpnext.stock.doctype.bin.bin import (
				PROJECTED_QTY_ADDITIONS,
				PROJECTED_QTY_DEDUCTIONS,
				RECALCULATED_FIELDS,
			)
		except ImportError:
			self.skipTest("this ERPNext predates the Bin constants")
		with _older_erpnext():
			additions, deductions = reorder_review.bin_projected_qty_fields()
			recalculated = reorder_review.bin_recalculated_fields()
		self.assertEqual(additions, tuple(PROJECTED_QTY_ADDITIONS))
		self.assertEqual(deductions, tuple(PROJECTED_QTY_DEDUCTIONS))
		self.assertEqual(set(recalculated), set(RECALCULATED_FIELDS))
