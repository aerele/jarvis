"""Synthetic accounting inputs, confined to the disposable test site.

Raw inserts intentionally exercise ERPNext's real report/ledger allocation without
requiring an unrelated purchase/stock workflow. Nothing is committed by these helpers.
"""

import os
from unittest import SkipTest
from uuid import uuid4

import frappe
from frappe.utils import add_days, nowdate


def require_calendar_test_site():
	allowed = frappe.local.site == "test_jarvis" or (
		os.environ.get("CI") and frappe.local.site == "jarvis-ci.test"
	)
	assert allowed, "Calendar fixtures require the disposable local or CI test site"
	if "erpnext" not in frappe.get_installed_apps():
		raise SkipTest("Payment Calendar requires ERPNext v16")
	import erpnext

	if erpnext.__version__.split(".")[0] != "16":
		raise SkipTest("The Payment Calendar source contract currently supports ERPNext v16")


class CalendarFixtures:
	def __init__(self):
		require_calendar_test_site()
		self.prefix = "_JPC_" + uuid4().hex[:10]
		self.created = []
		self.today = nowdate()
		# The canonical engine needs these real setup records to apply its
		# company/account/party filters. They remain transactional like all fixtures.
		for party_type, account_type in (("Supplier", "Payable"), ("Customer", "Receivable")):
			if not frappe.db.exists("Party Type", party_type):
				frappe.get_doc(
					{
						"doctype": "Party Type",
						"name": party_type,
						"party_type": party_type,
						"account_type": account_type,
					}
				).db_insert()
				self.created.append(("Party Type", party_type))
		self.company = self.insert(
			"Company",
			"Company",
			company_name=self.prefix,
			abbr=self.prefix[-5:],
			default_currency="INR",
			country="India",
		)
		self.supplier = self.insert(
			"Supplier", "Supplier", supplier_name=self.prefix, supplier_type="Company", disabled=0
		)
		self.customer = self.insert(
			"Customer", "Customer", customer_name=self.prefix, customer_type="Company", disabled=0
		)
		self.payable = self.account("Payable", "Payable", "Liability")
		self.receivable = self.account("Receivable", "Receivable", "Asset")
		self.bank = self.account("Bank", "Bank", "Asset")
		self.serial = 0

	def insert(self, doctype, suffix, **fields):
		name = self.prefix + suffix
		frappe.get_doc({"doctype": doctype, "name": name, **fields}).db_insert()
		self.created.append((doctype, name))
		return name

	def account(self, suffix, account_type, root_type, **fields):
		return self.insert(
			"Account",
			suffix,
			account_name=self.prefix + suffix,
			company=self.company,
			account_type=account_type,
			account_currency="INR",
			is_group=0,
			disabled=0,
			report_type="Balance Sheet",
			root_type=root_type,
			lft=1,
			rgt=2,
			**fields,
		)

	def invoice(
		self,
		amounts=(40, 60),
		*,
		paid=0,
		side="Payable",
		schedules=True,
		supplier=None,
		credit=0,
		advance=0,
		account=None,
		offset=0,
		schedule_paid=None,
		discounts=None,
		gross_total=None,
	):
		self.serial += 1
		suffix = str(self.serial)
		doctype = "Purchase Invoice" if side == "Payable" else "Sales Invoice"
		party_type = "Supplier" if side == "Payable" else "Customer"
		party = supplier or (self.supplier if side == "Payable" else self.customer)
		account = account or (self.payable if side == "Payable" else self.receivable)
		total = gross_total if gross_total is not None else sum(amounts)
		name = self.insert(
			doctype,
			"Invoice" + suffix,
			company=self.company,
			**{party_type.lower(): party},
			posting_date=add_days(self.today, offset),
			due_date=add_days(self.today, 20),
			docstatus=1,
			currency="INR",
			party_account_currency="INR",
			conversion_rate=1,
			grand_total=total,
			base_grand_total=total,
			total_advance=advance,
			is_return=0,
			**{"credit_to" if side == "Payable" else "debit_to": account},
		)
		if schedules:
			for index, amount in enumerate(amounts):
				term_paid = (schedule_paid or {}).get(index, 0)
				self.insert(
					"Payment Schedule",
					"Term" + suffix + "_" + str(index),
					parent=name,
					parenttype=doctype,
					parentfield="payment_schedule",
					due_date=add_days(self.today, 10 + index * 10),
					payment_amount=amount,
					base_payment_amount=amount,
					paid_amount=term_paid,
					base_paid_amount=term_paid,
					discounted_amount=(discounts or {}).get(index, 0),
					idx=index + 1,
				)
		self.ple("InvoicePLE" + suffix, doctype, name, total, side, party, account, posting_offset=offset)
		if paid:
			self.ple("PaymentPLE" + suffix, "Payment Entry", name, -paid, side, party, account)
		if credit:
			returned = self.insert(
				doctype,
				"Return" + suffix,
				company=self.company,
				**{party_type.lower(): party},
				posting_date=self.today,
				docstatus=1,
				is_return=1,
				return_against=name,
				update_outstanding_for_self=0,
			)
			self.ple("CreditPLE" + suffix, doctype, name, -credit, side, party, account, voucher_no=returned)
		return name

	def ple(
		self,
		suffix,
		voucher_type,
		invoice,
		amount,
		side="Payable",
		party=None,
		account=None,
		posting_offset=0,
		voucher_no=None,
		finance_book=None,
	):
		return self.insert(
			"Payment Ledger Entry",
			suffix,
			company=self.company,
			posting_date=add_days(self.today, posting_offset),
			account_type=side,
			account=account or self.payable,
			account_currency="INR",
			party_type="Supplier" if side == "Payable" else "Customer",
			party=party or self.supplier,
			voucher_type=voucher_type,
			voucher_no=voucher_no
			or (invoice if voucher_type in ("Purchase Invoice", "Sales Invoice") else self.prefix + suffix),
			against_voucher_type="Purchase Invoice" if side == "Payable" else "Sales Invoice",
			against_voucher_no=invoice,
			amount=amount,
			amount_in_account_currency=amount,
			delinked=0,
			finance_book=finance_book,
		)

	def user(self, role="Accounts Manager"):
		self.serial += 1
		email = self.prefix.lower() + str(self.serial) + "@example.com"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Calendar fixture",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
				"roles": [{"role": role}],
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=email)
		return email

	def fiscal_year(self):
		year = int(self.today[:4])
		return self.insert(
			"Fiscal Year",
			"FY",
			year=self.prefix,
			year_start_date=f"{year}-01-01",
			year_end_date=f"{year}-12-31",
			disabled=0,
		)

	def gl(self, amount, *, account=None, finance_book=None):
		self.serial += 1
		return self.insert(
			"GL Entry",
			"GL" + str(self.serial),
			company=self.company,
			account=account or self.bank,
			posting_date=self.today,
			debit=max(amount, 0),
			credit=max(-amount, 0),
			debit_in_account_currency=max(amount, 0),
			credit_in_account_currency=max(-amount, 0),
			account_currency="INR",
			is_cancelled=0,
			is_opening="No",
			finance_book=finance_book,
			voucher_type="Journal Entry",
			voucher_no=self.prefix + "BankOpening",
		)
