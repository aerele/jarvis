"""Party scopes omitted by the standard report must never imply company coverage."""

from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar.sources import read_obligations
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site

_selected_supplier = None


def supplier_query_condition(user=None, doctype=None):
	return "`tabSupplier`.`name` = " + frappe.db.escape(_selected_supplier)


class TestPaymentCalendarPartyPermissions(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_party_scope")
		self.fx = CalendarFixtures()
		self.fx.invoice((100,))
		if not frappe.db.exists("Party Type", "Employee"):
			frappe.get_doc(
				{
					"doctype": "Party Type",
					"name": "Employee",
					"party_type": "Employee",
					"account_type": "Payable",
				}
			).db_insert()
		else:
			frappe.db.set_value("Party Type", "Employee", "account_type", "Payable")
		self.a = self.employee("EmployeeA", 30)
		self.b = self.employee("EmployeeB", 70)

	def tearDown(self):
		global _selected_supplier
		_selected_supplier = None
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_party_scope")
		frappe.clear_cache(doctype="Employee")
		frappe.clear_cache(doctype="Purchase Invoice")

	def employee(self, suffix, amount):
		name = self.fx.insert(
			"Employee",
			suffix,
			employee_name="Calendar employee",
			first_name="Calendar",
			status="Active",
			company=self.fx.company,
		)
		voucher = self.fx.insert(
			"Journal Entry",
			suffix + "JE",
			company=self.fx.company,
			posting_date=self.fx.today,
			docstatus=1,
			bill_no=suffix,
			bill_date=self.fx.today,
		)
		self.fx.insert(
			"Payment Ledger Entry",
			suffix + "PLE",
			company=self.fx.company,
			posting_date=self.fx.today,
			account=self.fx.payable,
			account_type="Payable",
			account_currency="INR",
			party_type="Employee",
			party=name,
			voucher_type="Journal Entry",
			voucher_no=voucher,
			against_voucher_type="Journal Entry",
			against_voucher_no=voucher,
			amount=amount,
			amount_in_account_currency=amount,
			delinked=0,
		)
		return name

	def read(self):
		return read_obligations(
			self.fx.company, "Payable", self.fx.today, self.fx.today, add_days(self.fx.today, 40)
		)

	def user(self, applicable=None):
		user = self.fx.user()
		if applicable is not False:
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": user,
					"allow": "Employee",
					"for_value": self.a,
					"apply_to_all_doctypes": int(applicable is None),
					"applicable_for": applicable,
				}
			).insert(ignore_permissions=True)
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		return user

	def test_employee_permission_is_an_incomplete_company_obligation_scope(self):
		self.user()
		result = self.read()
		self.assertTrue(any(row["party"] == self.a for row in result["rows"]))
		self.assertFalse(any(row["party"] == self.b for row in result["rows"]))
		self.assertFalse(result["complete_for_company"])

	def test_permission_applicable_to_another_document_cannot_claim_company_coverage(self):
		self.user("Expense Claim")
		result = self.read()
		self.assertFalse(any(row["party_type"] == "Employee" for row in result["rows"]))
		self.assertFalse(result["complete_for_company"])

	def test_select_only_employee_access_is_sufficient_for_financial_party_ids(self):
		frappe.db.set_value(
			"Custom DocPerm", {"parent": "Employee", "role": "Accounts Manager"}, {"read": 0, "select": 1}
		)
		self.fx.insert(
			"Custom DocPerm",
			"EmployeeSelect",
			parent="Employee",
			role="Accounts Manager",
			permlevel=0,
			read=0,
			**{"select": 1},
		)
		frappe.clear_cache(doctype="Employee")
		self.user(False)
		self.assertFalse(frappe.has_permission("Employee", "read", doc=self.a))
		self.assertIn(
			self.a, frappe.get_list("Employee", filters={"name": ["in", [self.a, self.b]]}, pluck="name")
		)
		result = self.read()
		self.assertTrue(any(row["party"] == self.a for row in result["rows"]))
		self.assertTrue(any(row["party"] == self.b for row in result["rows"]))
		self.assertTrue(result["complete_for_company"])

	def test_supplier_custom_query_scope_cannot_leak_a_hidden_party(self):
		global _selected_supplier
		_selected_supplier = self.fx.supplier
		hidden = self.fx.insert(
			"Supplier", "HiddenSupplier", supplier_name="Hidden fixture", supplier_type="Company", disabled=0
		)
		self.fx.invoice((100,), supplier=hidden)
		self.user(False)
		original = frappe.get_hooks

		def hooks(name=None, *args, **kwargs):
			value = original(name, *args, **kwargs)
			if name == "permission_query_conditions":
				return {
					**value,
					"Supplier": [
						"jarvis.tests.test_payment_calendar_party_permissions.supplier_query_condition"
					],
				}
			return value

		with patch("frappe.get_hooks", side_effect=hooks):
			result = self.read()
		self.assertFalse(any(row["party"] == hidden for row in result["rows"]))
		self.assertFalse(result["complete_for_company"])

	def test_snapshot_permission_proof_contains_relationships_without_financial_or_free_text_fields(self):
		result = self.read()
		row = next(row for row in result["rows"] if row["voucher_type"] == "Purchase Invoice")
		self.assertEqual(row["_permission"]["party_account"], self.fx.payable)
		self.assertNotIn("remarks", row["_permission"])
		self.assertNotIn("paid", row["_permission"])
		self.assertTrue(result["permission_columns"])

	def test_owner_only_invoice_read_is_incomplete_even_when_all_current_rows_are_owned(self):
		user = self.fx.user()
		name = frappe.db.get_value("Purchase Invoice", {"company": self.fx.company}, "name")
		frappe.db.set_value("Purchase Invoice", name, "owner", user)
		frappe.db.set_value(
			"Custom DocPerm",
			{"parent": "Purchase Invoice", "role": "Accounts Manager"},
			{"read": 0, "report": 1, "if_owner": 0},
		)
		self.fx.insert(
			"Custom DocPerm",
			"InvoiceReport",
			parent="Purchase Invoice",
			role="Accounts Manager",
			permlevel=0,
			read=0,
			report=1,
			if_owner=0,
			**{"select": 1},
		)
		self.fx.insert(
			"Custom DocPerm",
			"InvoiceOwnerRead",
			parent="Purchase Invoice",
			role="Accounts Manager",
			permlevel=0,
			read=1,
			report=0,
			if_owner=1,
		)
		frappe.clear_cache(doctype="Purchase Invoice")
		frappe.clear_cache(user=user)
		frappe.set_user(user)
		result = self.read()
		self.assertTrue(any(row["voucher_no"] == name for row in result["rows"]))
		self.assertFalse(result["complete_for_company"])
