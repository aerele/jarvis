"""The shared column policy of stored reports, with stub metas and no database."""

import unittest
from unittest.mock import patch

import frappe


class _Meta:
	"""The part of a DocType meta that the column policy reads."""

	def __init__(self, fields, permitted=None, masked=()):
		self.fields = [frappe._dict(fieldname=name) for name in fields]
		self._permitted = list(fields if permitted is None else permitted)
		self._masked = [frappe._dict(fieldname=name) for name in masked]

	def get_permitted_fieldnames(self, user=None, permission_type=None):
		return self._permitted

	def get_masked_fields(self, parenttype=None):
		return self._masked


def _column(fieldname):
	return {"fieldname": fieldname, "sql_column": "c_" + fieldname}


COLUMNS = [_column(name) for name in ("name", "secret", "party_name", "customer_name", "supplier_name")]
USER = "report-policy@example.test"


def _policy(ref_meta, report_name, party=None):
	from jarvis.tools._report_query_source import _column_policy

	metas = {"Customer": _Meta(["customer_name"]), "Supplier": _Meta(["supplier_name"]), **(party or {})}
	with patch.object(frappe, "get_meta", side_effect=metas.__getitem__):
		return _column_policy(ref_meta, COLUMNS, USER, report_name)


class TestColumnPolicy(unittest.TestCase):
	def test_a_ref_doctype_field_the_user_cannot_read_is_denied(self):
		ref = _Meta(["name", "secret"], permitted=["name"])
		masked, denied = _policy(ref, "Other Report")
		self.assertEqual(denied, {"c_secret"})
		self.assertEqual(masked, set())

	def test_a_masked_ref_doctype_field_is_masked(self):
		ref = _Meta(["name", "secret"], masked=["secret"])
		masked, denied = _policy(ref, "Other Report")
		self.assertEqual(masked, {"c_secret"})
		self.assertEqual(denied, set())

	def test_administrator_is_never_denied(self):
		from jarvis.tools._report_query_source import _column_policy

		ref = _Meta(["name", "secret"], permitted=["name"])
		self.assertEqual(_column_policy(ref, COLUMNS, "Administrator", "Other Report"), (set(), set()))

	def test_receivable_masks_party_name_when_customer_name_is_masked(self):
		party = {"Customer": _Meta(["customer_name"], masked=["customer_name"])}
		masked, denied = _policy(_Meta(["name"]), "Accounts Receivable", party)
		self.assertEqual(masked, {"c_party_name", "c_customer_name"})
		self.assertEqual(denied, set())

	def test_receivable_denies_party_name_when_customer_name_is_unreadable(self):
		party = {"Customer": _Meta(["customer_name"], permitted=[])}
		masked, denied = _policy(_Meta(["name"]), "Accounts Receivable Summary", party)
		self.assertEqual(denied, {"c_party_name", "c_customer_name"})
		self.assertEqual(masked, set())

	def test_payable_masks_party_name_when_supplier_name_is_masked(self):
		party = {"Supplier": _Meta(["supplier_name"], masked=["supplier_name"])}
		masked, denied = _policy(_Meta(["name"]), "Accounts Payable", party)
		self.assertEqual(masked, {"c_party_name", "c_supplier_name"})
		self.assertEqual(denied, set())

	def test_payable_denies_party_name_when_supplier_name_is_unreadable(self):
		party = {"Supplier": _Meta(["supplier_name"], permitted=[])}
		masked, denied = _policy(_Meta(["name"]), "Accounts Payable Summary", party)
		self.assertEqual(denied, {"c_party_name", "c_supplier_name"})
		self.assertEqual(masked, set())

	def test_an_unrelated_report_has_no_party_aliasing(self):
		party = {
			"Customer": _Meta(["customer_name"], masked=["customer_name"], permitted=[]),
			"Supplier": _Meta(["supplier_name"], masked=["supplier_name"], permitted=[]),
		}
		self.assertEqual(_policy(_Meta(["name"]), "Sales Register", party), (set(), set()))
