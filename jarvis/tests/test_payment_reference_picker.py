"""admin-v2#785: a Payment Entry reference's Type and Name pickers narrow as Desk's do.

erpnext/accounts/doctype/payment_entry/payment_entry.js sets two queries on the references
table: Type (reference_doctype, a Link to DocType) is limited to the documents a payment can
be allocated to, and Name (reference_name, a Dynamic Link on Type) to submitted documents of
the payment's company, and of its party for invoices, orders and Dunning. The draft card
carries them as ``link_query_filters`` (jarvis.chat.link_filters); the card applies the Name
filters of the row's current Type.
"""

from copy import deepcopy

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.link_filters import draft_link_query_filters

CHILD, PARENT, TABLE = "Payment Entry Reference", "Payment Entry", "references"


def _df(fieldname):
	return frappe.get_meta(CHILD).get_field(fieldname)


class TestPaymentReferencePicker(FrappeTestCase):
	def test_type_is_limited_to_the_documents_a_payment_can_reference(self):
		self.assertEqual(
			draft_link_query_filters(CHILD, _df("reference_doctype"), PARENT, TABLE),
			[
				[
					"DocType",
					"name",
					"in",
					[
						"Sales Order",
						"Sales Invoice",
						"Purchase Order",
						"Purchase Invoice",
						"Journal Entry",
						"Dunning",
					],
				]
			],
		)

	def test_name_carries_each_types_desk_filters(self):
		filters = draft_link_query_filters(CHILD, _df("reference_name"), PARENT, TABLE)
		by_type = {}
		for doctype, fieldname, op, value in filters:
			by_type.setdefault(doctype, []).append([fieldname, op, value])
		self.assertEqual(
			by_type["Sales Invoice"],
			[
				["docstatus", "=", 1],
				["company", "=", "eval:parent.company"],
				["customer", "=", "eval:parent.party"],
			],
		)
		self.assertEqual(by_type["Purchase Order"][2], ["supplier", "=", "eval:parent.party"])
		self.assertEqual(by_type["Dunning"][2], ["customer", "=", "eval:parent.party"])
		# Journal Entry has no party field of its own: company and docstatus only, as in Desk.
		self.assertEqual(
			by_type["Journal Entry"], [["docstatus", "=", 1], ["company", "=", "eval:parent.company"]]
		)
		# every filtered field exists on its DocType
		for doctype, rows in by_type.items():
			meta = frappe.get_meta(doctype)
			for fieldname, _op, _value in rows:
				self.assertTrue(fieldname == "docstatus" or meta.has_field(fieldname), (doctype, fieldname))

	def test_only_the_payment_entry_references_table_gets_them(self):
		for child, parent, table in (
			(CHILD, "Custom Payment", TABLE),
			(CHILD, PARENT, "custom_references"),
			("Custom Reference", PARENT, TABLE),
		):
			with self.subTest(child=child, parent=parent, table=table):
				self.assertEqual(draft_link_query_filters(child, _df("reference_name"), parent, table), [])
		other = deepcopy(_df("reference_name"))
		other.fieldname = "custom_reference"
		self.assertEqual(draft_link_query_filters(CHILD, other, PARENT, TABLE), [])
		# the table's other fields keep no filters here
		self.assertEqual(draft_link_query_filters(CHILD, _df("payment_term"), PARENT, TABLE), [])
