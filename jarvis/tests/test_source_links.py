"""A document built from another keeps its source links.

``create_doc(mapped_from=...)`` re-runs the ERP's mapper and fills only the source link fields
the agent left empty (``_source_links``). The real Purchase Invoice meta is used; the mapper and
the source lookups are stubbed, so no order or company is needed on the test site.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _source_links
from jarvis.tools._source_links import restore_source_links

PO = "PUR-ORD-TEST-0001"
MF = {"source_doctype": "Purchase Order", "source_name": PO}
MAPPED = {
	"supplier": "Sup A",
	"items": [
		{
			"item_code": "A",
			"qty": 84,
			"rate": 140,
			"warehouse": "Stores",
			"purchase_order": PO,
			"po_detail": "row-1",
		},
		{
			"item_code": "A",
			"qty": 52,
			"rate": 450,
			"warehouse": "Stores",
			"purchase_order": PO,
			"po_detail": "row-2",
		},
		{
			"item_code": "B",
			"qty": 1,
			"rate": 10,
			"warehouse": "Stores",
			"purchase_order": PO,
			"po_detail": "row-3",
		},
	],
}


def _restore(values, mapped=MAPPED, note=None, exists=True, mf=MF):
	with (
		patch.object(_source_links, "mapped_values", return_value=(mapped, note)),
		patch.object(_source_links, "_source_row_names", return_value={"row-1", "row-2", "row-3"}),
		patch.object(frappe.db, "exists", return_value=exists),
	):
		return restore_source_links("Purchase Invoice", values, mf)


class TestSourceLinks(FrappeTestCase):
	def test_each_row_gets_its_own_order_line_matched_by_item_in_order(self):
		out = _restore(
			{"supplier": "Sup A", "items": [{"item_code": "A", "qty": 84}, {"item_code": "A", "qty": 52}]}
		)
		self.assertEqual(
			[(r["purchase_order"], r["po_detail"]) for r in out["items"]], [(PO, "row-1"), (PO, "row-2")]
		)

	def test_only_link_fields_are_copied_and_the_agents_values_win(self):
		out = _restore({"items": [{"item_code": "B", "qty": 3, "rate": 99}]})
		self.assertEqual(
			out["items"],
			[{"item_code": "B", "qty": 3, "rate": 99, "purchase_order": PO, "po_detail": "row-3"}],
		)
		# a link the agent set is never replaced
		out = _restore({"items": [{"item_code": "A", "po_detail": "row-2"}]})
		self.assertEqual(out["items"][0]["po_detail"], "row-2")
		# header: no Link to Purchase Order on a Purchase Invoice header, nothing added
		self.assertNotIn("purchase_order", _restore({"items": [{"item_code": "A"}]}))

	def test_a_row_with_no_match_gets_nothing(self):
		out = _restore({"items": [{"item_code": "A"}, {"item_code": "ZZZ"}]})
		self.assertEqual(out["items"][1], {"item_code": "ZZZ"})

	def test_rows_without_an_item_match_by_position_when_the_counts_agree(self):
		out = _restore({"items": [{"qty": 1}, {"qty": 2}, {"qty": 3}]})
		self.assertEqual([r["po_detail"] for r in out["items"]], ["row-1", "row-2", "row-3"])
		out = _restore({"items": [{"qty": 1}]})  # counts differ: no guessing
		self.assertEqual(out["items"], [{"qty": 1}])

	def test_the_callers_values_are_not_mutated(self):
		values = {"items": [{"item_code": "A"}]}
		_restore(values)
		self.assertEqual(values, {"items": [{"item_code": "A"}]})

	def test_a_bad_or_unmappable_source_is_refused_clearly(self):
		for bad in (None, "PO-1", {"source_doctype": "Purchase Order"}, {"source_name": PO}):
			with self.subTest(bad=bad), self.assertRaises(InvalidArgumentError):
				_restore({"items": []}, mf=bad)
		with self.assertRaisesRegex(InvalidArgumentError, "does not exist"):
			_restore({"items": []}, exists=False)
		with self.assertRaisesRegex(InvalidArgumentError, "nothing left to bill"):
			_restore({"items": []}, mapped=None, note="nothing left to bill")

	def test_create_doc_refuses_it_on_a_batch(self):
		from jarvis.tools.create_doc import create_doc

		with self.assertRaisesRegex(InvalidArgumentError, "single document"):
			create_doc(docs=[{"doctype": "ToDo", "values": {"description": "x"}}], mapped_from=MF)
