"""ERPNext's Link-to-DocType report columns must not reach Frappe's per-row filter."""

from unittest import TestCase

from jarvis.payment_calendar.permissions import row_filter_columns


class TestRowFilterColumns(TestCase):
	def test_doctype_links_become_plain_data_and_other_columns_are_untouched(self):
		party_type = {"fieldname": "party_type", "fieldtype": "Link", "options": "DocType"}
		voucher_type = {"fieldname": "voucher_type", "fieldtype": "Link", "options": "DocType"}
		account = {"fieldname": "party_account", "fieldtype": "Link", "options": "Account"}
		voucher_no = {"fieldname": "voucher_no", "fieldtype": "Dynamic Link", "options": "voucher_type"}
		result = row_filter_columns([party_type, voucher_type, account, voucher_no])
		self.assertEqual(
			result[:2],
			[
				{"fieldname": "party_type", "fieldtype": "Data", "options": None},
				{"fieldname": "voucher_type", "fieldtype": "Data", "options": None},
			],
		)
		self.assertEqual(result[2:], [account, voucher_no])
		self.assertEqual(voucher_type["fieldtype"], "Link")  # the caller's columns are not mutated
