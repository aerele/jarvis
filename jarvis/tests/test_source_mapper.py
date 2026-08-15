"""Tests for jarvis.tools._source_mapper - resolving and running the ERP's own
make_* mapper for a (source -> target) pair.

Split in two:

- Resolution runs against REAL erpnext meta (Sales Order / Quotation): the three
  filters only earn their keep against the real form-JS scrape, and the two
  negative cases (a foreign mapper, a non-source doctype) are the whole point.
- Execution is tested with a FAKE mapper. Building a submitted Sales Order needs
  a full ERPNext company (chart of accounts, warehouses, price lists) that this
  site does not have, and a fake lets us assert the thing that actually matters -
  that a mapper which WRITES leaves nothing behind - using a lightweight doctype.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.tools._source_mapper import (
	_strip,
	mapped_values,
	resolve_mapper,
)


class TestResolveMapper(FrappeTestCase):
	"""Against real erpnext meta."""

	def test_resolves_the_sales_invoice_mapper(self):
		method = resolve_mapper("Sales Order", "Sales Invoice")
		self.assertTrue(method, "expected a Sales Order -> Sales Invoice mapper")
		self.assertTrue(method.endswith(".make_sales_invoice"), method)

	def test_foreign_mapper_is_not_resolved(self):
		"""THE load-bearing filter. Sales Order's form JS references
		quotation.mapper.make_sales_order, so a naive scrape offers target
		"Sales Order" from source "Sales Order" - and that mapper expects a
		QUOTATION. Feeding it a Sales Order would be silently wrong."""
		self.assertIsNone(resolve_mapper("Sales Order", "Sales Order"))

	def test_no_false_positive_for_a_non_source_doctype(self):
		"""Customer is a real DocType and a common context hint, but has no
		mapper to a Sales Invoice - it must resolve to nothing rather than
		something plausible."""
		self.assertIsNone(resolve_mapper("Customer", "Sales Invoice"))

	def test_resolves_quotation_to_sales_order(self):
		method = resolve_mapper("Quotation", "Sales Order")
		self.assertTrue(method and method.endswith(".make_sales_order"), method)

	def test_unknown_doctypes_resolve_to_none_without_raising(self):
		self.assertIsNone(resolve_mapper("Definitely Not A DocType", "ToDo"))
		self.assertIsNone(resolve_mapper("", "ToDo"))
		self.assertIsNone(resolve_mapper("Sales Order", ""))


def _fake(returns, *, writes=False):
	"""A stand-in mapper. ``writes=True`` makes it insert a real row first -
	which is what make_sales_invoice does via bundle.save()."""

	def fn(source_name=None, target_doc=None):
		if writes:
			frappe.get_doc(
				{
					"doctype": "ToDo",
					"description": "sandbox-probe",
				}
			).insert(ignore_permissions=True)
		return returns

	return fn


# The fake the dotted path below resolves to. Tests point `resolve_mapper` at
# `_fake_entry`'s real dotted path and let the production code's own
# `frappe.get_attr` load it - patching `frappe.get_attr` instead would replace it
# for FRAPPE TOO (same module object), and frappe calls it internally on every
# get_doc, which recurses until the stack dies.
_current_fake = None


def _fake_entry(source_name=None, target_doc=None):
	return _current_fake(source_name=source_name, target_doc=target_doc)


class TestMappedValues(FrappeTestCase):
	def setUp(self):
		self.source = frappe.get_doc(
			{
				"doctype": "Note",
				"title": f"src-{frappe.generate_hash(length=8)}",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete("Note", {"name": self.source.name})
		frappe.db.delete("ToDo", {"description": "sandbox-probe"})
		frappe.db.commit()

	def _run(self, mapper_fn):
		global _current_fake
		_current_fake = mapper_fn
		with patch(
			"jarvis.tools._source_mapper.resolve_mapper",
			return_value="jarvis.tests.test_source_mapper._fake_entry",
		):
			return mapped_values("Note", self.source.name, "ToDo")

	def test_a_writing_mapper_persists_nothing(self):
		"""THE central invariant. Mappers are not side-effect free -
		make_sales_invoice's postprocess reaches bundle.save() and inserts a
		Serial and Batch Bundle, make_purchase_order calls doc.insert() outright.
		Frappe commits any normally-returning request, so without the sandbox a
		mere context lookup would leave rows behind."""
		target = frappe.get_doc({"doctype": "ToDo", "description": "mapped"})
		before = frappe.db.count("ToDo")
		values, note = self._run(_fake(target, writes=True))
		frappe.db.commit()  # fresh snapshot: prove it did not merely defer
		self.assertIsNone(note)
		self.assertEqual(values.get("description"), "mapped")
		self.assertEqual(
			frappe.db.count("ToDo"),
			before,
			"the mapper's write escaped the sandbox and was persisted",
		)

	def test_none_returning_mapper_is_treated_as_no_mapper(self):
		"""make_maintenance_schedule guards its whole body with
		`if not maint_schedule:` and implicitly returns None when one already
		exists; make_purchase_order returns None without selected_items. Passing
		the resolution filters does NOT make a mapper usable - so validate the
		result, and never let a None reach `.doctype`."""
		values, note = self._run(_fake(None))
		self.assertIsNone(values)
		self.assertIsNone(note)

	def test_list_returning_mapper_is_treated_as_no_mapper(self):
		"""make_purchase_order returns a LIST when it works."""
		values, note = self._run(_fake([frappe.get_doc({"doctype": "ToDo"})]))
		self.assertIsNone(values)
		self.assertIsNone(note, "a shape mismatch is 'no mapper', not an error")

	def test_wrong_doctype_result_is_rejected(self):
		other = frappe.get_doc({"doctype": "Note", "title": "wrong-type"})
		values, note = self._run(_fake(other))
		self.assertIsNone(values)
		self.assertIsNone(note)

	def test_mapper_error_is_surfaced_not_swallowed(self):
		"""The ERP's own refusal ("Sales Order is not submitted", "nothing left
		to bill") usually says exactly what to fix - far more useful than
		silence."""

		def boom(source_name=None, target_doc=None):
			raise frappe.ValidationError("Sales Order is not submitted")

		values, note = self._run(boom)
		self.assertIsNone(values)
		self.assertIn("not submitted", note)

	def test_no_mapper_is_a_clean_none(self):
		with patch("jarvis.tools._source_mapper.resolve_mapper", return_value=None):
			self.assertEqual(mapped_values("Note", self.source.name, "ToDo"), (None, None))

	def test_read_permission_on_the_source_is_enforced(self):
		user = "mapper-noperm@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user,
					"first_name": "NoPerm",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		frappe.set_user(user)
		try:
			with patch("jarvis.tools._source_mapper.resolve_mapper", return_value="fake.method"):
				values, note = mapped_values("Note", self.source.name, "ToDo")
			self.assertIsNone(values)
			self.assertIn("no read permission", note)
		finally:
			frappe.set_user("Administrator")


class TestStrip(FrappeTestCase):
	def test_drops_framework_bookkeeping_including_child_rows(self):
		"""A draft must never carry a name/docstatus copied from the mapping."""
		out = _strip(
			{
				"name": "SINV-0001",
				"docstatus": 1,
				"owner": "Administrator",
				"creation": "2026-01-01",
				"doctype": "Sales Invoice",
				"customer": "Acme",
				"items": [
					{
						"name": "row1",
						"parent": "SINV-0001",
						"idx": 1,
						"item_code": "Widget",
						"so_detail": "abc",
					}
				],
			}
		)
		self.assertEqual(
			set(out.keys()),
			{"customer", "items"},
			f"bookkeeping leaked: {sorted(out.keys())}",
		)
		row = out["items"][0]
		self.assertEqual(set(row.keys()), {"item_code", "so_detail"})

	def test_keeps_the_source_linkage(self):
		"""so_detail / sales_order are the whole point - they keep the order's
		billed status correct."""
		out = _strip({"items": [{"sales_order": "SO-1", "so_detail": "row-1"}]})
		self.assertEqual(out["items"][0]["sales_order"], "SO-1")
		self.assertEqual(out["items"][0]["so_detail"], "row-1")

	def test_empty_values_and_empty_child_tables_are_dropped(self):
		out = _strip({"customer": "Acme", "note": "", "ref": None, "items": []})
		self.assertEqual(set(out.keys()), {"customer"})
