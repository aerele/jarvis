"""Synthetic accounting inputs, confined to the disposable test site.

Raw inserts intentionally exercise ERPNext's real report/ledger allocation without
requiring an unrelated purchase/stock workflow. Nothing is committed by these helpers.
"""

import os
from unittest import SkipTest
from uuid import uuid4

import frappe
from frappe.model.meta import get_field_precision
from frappe.utils import add_days, flt, nowdate

from jarvis.payment_calendar.compatibility import source_profile


def is_calendar_test_site() -> bool:
	"""The disposable sites: `test_jarvis`, or `jarvis-ci.test` when the CI environment variable is set."""
	return frappe.local.site == "test_jarvis" or bool(
		os.environ.get("CI") and frappe.local.site == "jarvis-ci.test"
	)


def require_calendar_test_site():
	assert is_calendar_test_site(), "Calendar fixtures require the disposable local or CI test site"
	if "erpnext" not in frappe.get_installed_apps():
		raise SkipTest("Payment Calendar requires ERPNext")
	source_profile()


def clear_error_throttle():
	"""The API writes one Error Log row per (operation, exception class) in 300 s; start with none written."""
	frappe.cache.delete_keys("jarvis:pcal:errlog:")


# doctype -> (party fieldname, party type, ledger account fieldname) of the two invoice kinds
_INVOICE_HEADERS = {
	"Sales Invoice": ("customer", "Customer", "debit_to"),
	"Purchase Invoice": ("supplier", "Supplier", "credit_to"),
}


def sync_outstanding(company: str) -> int:
	"""Bring the stored ``outstanding_amount`` of every submitted Sales/Purchase Invoice of ``company``
	in step with the ledger, the way ERPNext's ``update_voucher_outstanding`` does.

	The value is ERPNext's own ``QueryPaymentLedger.get_voucher_outstandings`` figure for the
	invoice (party and account filter, no posting-date filter: later-dated lines count), rounded
	to the precision of the field in the invoice's own party account currency. Fixtures raw-insert
	invoices without a header, and tests edit ledger rows after the insert, so the reader (which
	discovers open invoices from the header) needs this step; today's read never reads the header.
	Status, dunning and notify steps are omitted.

	An invoice with no ledger row gives an empty outstanding list, and ERPNext then leaves the
	header unchanged (erpnext/accounts/utils.py:2205): so does this helper. Invoices without a
	party are skipped, as ERPNext skips them. Returns the number of headers that changed.
	"""
	from erpnext.accounts.utils import QueryPaymentLedger

	ple = frappe.qb.DocType("Payment Ledger Entry")
	changed = 0
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	for doctype, (party_field, party_type, account_field) in _INVOICE_HEADERS.items():
		field = frappe.get_meta(doctype).get_field("outstanding_amount")
		invoices = frappe.get_all(
			doctype,
			filters={"company": company, "docstatus": 1},
			fields=["name", "outstanding_amount", "party_account_currency", party_field, account_field],
			order_by="name",
			ignore_permissions=True,
		)
		groups = {}
		for invoice in invoices:
			if invoice[party_field]:
				groups.setdefault((invoice[party_field], invoice[account_field]), []).append(invoice)
		# One ERPNext call per (party, account): same filter as the per-invoice call, fewer queries.
		for (party, account), members in groups.items():
			common_filter = [ple.party_type == party_type, ple.party == party]
			if account:
				common_filter.append(ple.account == account)
			outstandings = QueryPaymentLedger().get_voucher_outstandings(
				[frappe._dict(voucher_type=doctype, voucher_no=m["name"]) for m in members],
				common_filter=common_filter,
			)
			# The (party, account) filter leaves one row per invoice, which ERPNext takes as outstanding[0];
			# summing keeps a dropped filter visible instead of depending on row order.
			by_voucher = {}
			for row in outstandings:
				by_voucher[row["voucher_no"]] = by_voucher.get(row["voucher_no"], 0) + flt(
					row["outstanding_in_account_currency"]
				)
			for member in members:
				if member["name"] not in by_voucher:
					continue
				# ERPNext rounds with ref_doc.precision("outstanding_amount"): the field's currency is the
				# invoice's own party_account_currency (a doc without one falls back to the company's).
				precision = get_field_precision(
					field, currency=member["party_account_currency"] or company_currency
				)
				value = flt(by_voucher[member["name"]], precision)
				if flt(member["outstanding_amount"], precision) == value:
					continue
				frappe.db.set_value(
					doctype, member["name"], "outstanding_amount", value, update_modified=False
				)
				changed += 1
	return changed


class CalendarFixtures:
	def __init__(self):
		require_calendar_test_site()
		self.prefix = "_JPC_" + uuid4().hex[:10]
		clear_error_throttle()
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
		# Raw standalone ledger fixtures need submitted voucher headers so real
		# source/delivery document permissions can be checked consistently.
		voucher_type, voucher = fields.get("voucher_type"), fields.get("voucher_no")
		if (
			doctype == "Payment Ledger Entry"
			and voucher_type in ("Payment Entry", "Journal Entry")
			and voucher
			and not frappe.db.exists(voucher_type, voucher)
		):
			frappe.get_doc(
				{
					"doctype": voucher_type,
					"name": voucher,
					"company": fields.get("company"),
					"posting_date": fields.get("posting_date"),
					"docstatus": 1,
					"party_type": fields.get("party_type"),
					"party": fields.get("party"),
				}
			).db_insert()
			self.created.append((voucher_type, voucher))
		frappe.get_doc({"doctype": doctype, "name": name, **fields}).db_insert()
		self.created.append((doctype, name))
		return name

	def voucher_lines(
		self, voucher_type: str, voucher_no: str, lines: list[dict], *, header: dict | None = None
	) -> str:
		"""Insert the ledger ``lines`` of one voucher, preceded by a minimal submitted header.

		The header is inserted only when ``voucher_type`` is a DocType of the site and no such document
		exists, and carries only the company and posting date fields the DocType has (a raw ``db_insert``,
		like ``insert`` does for Payment and Journal Entries); ``header`` adds more fields (for example
		``voucher_type`` of a Journal Entry), again only those the DocType has. Each line is a dict of
		Payment Ledger Entry values and needs ``suffix`` and ``amount``; the others default to a Payable
		line of the fixture supplier against the voucher itself, dated today.

		A line may carry ``advance_voucher=(doctype, name)``: the advance link of an advance payment. ERPNext
		writes it to an Advance Payment Ledger Entry (erpnext/accounts/utils.py:2100-2141), never to the
		Payment Ledger Entry, which keeps the voucher's own reference (a Payment Entry against itself,
		payment_entry.py:1597-1610). That row is inserted when the DocType exists on the site.
		"""
		if frappe.db.exists("DocType", voucher_type) and not frappe.db.exists(voucher_type, voucher_no):
			meta = frappe.get_meta(voucher_type)
			values = {"company": self.company, "posting_date": self.today, **(header or {})}
			doc = {"doctype": voucher_type, "name": voucher_no, "docstatus": 1}
			doc.update({key: value for key, value in values.items() if meta.has_field(key)})
			frappe.get_doc(doc).db_insert()
			self.created.append((voucher_type, voucher_no))
		for line in lines:
			values = {
				"company": self.company,
				"posting_date": self.today,
				"account_type": "Payable",
				"account": self.payable,
				"account_currency": "INR",
				"party_type": "Supplier",
				"party": self.supplier,
				"voucher_type": voucher_type,
				"voucher_no": voucher_no,
				"against_voucher_type": voucher_type,
				"against_voucher_no": voucher_no,
				"delinked": 0,
				**{key: value for key, value in line.items() if key not in ("suffix", "advance_voucher")},
			}
			values.setdefault("amount_in_account_currency", values["amount"])
			self.insert("Payment Ledger Entry", line["suffix"], **values)
			if line.get("advance_voucher"):
				self.advance_ledger_entry(line["suffix"], values, *line["advance_voucher"])
		return voucher_no

	def advance_ledger_entry(self, suffix, line, advance_type, advance_no):
		"""The Advance Payment Ledger Entry ERPNext writes next to a line that settles an advance document."""
		if not frappe.db.exists("DocType", "Advance Payment Ledger Entry"):
			return
		own = (line["against_voucher_type"], line["against_voucher_no"]) == (
			line["voucher_type"],
			line["voucher_no"],
		)
		extra = {}
		if advance_type == "Employee Advance":
			# erpnext utils.py (get_payment_ledger_entries, `if gle.advance_voucher_no`): only an Employee Advance
			# also carries the company-currency amount and the transaction exchange rate of its GL line.
			in_account = line["amount_in_account_currency"]
			extra = {
				"base_amount": line["amount"],
				"exchange_rate": line.get("exchange_rate")
				or (line["amount"] / in_account if in_account else 1),
			}
		self.insert(
			"Advance Payment Ledger Entry",
			suffix + "Advance",
			company=line["company"],
			voucher_type=line["voucher_type"],
			voucher_no=line["voucher_no"],
			against_voucher_type=advance_type,
			against_voucher_no=advance_no,
			amount=line["amount_in_account_currency"],
			currency=line["account_currency"],
			event="Submit" if own else "Adjustment",
			delinked=0,
			**extra,
		)

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
		voucher = voucher_no or (
			invoice if voucher_type in ("Purchase Invoice", "Sales Invoice") else self.prefix + suffix
		)
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
			voucher_no=voucher,
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
