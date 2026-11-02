"""Narrow, read-only fallbacks to ERPNext's own invoice-level balance path."""

from copy import copy, deepcopy
from decimal import Decimal
from hashlib import sha256
from inspect import getsource

import frappe
from frappe.query_builder.functions import Count, Min, Sum
from frappe.utils import flt

from jarvis.payment_calendar.compatibility import source_profile
from jarvis.payment_calendar.errors import CalendarSourceUnavailable


def check_allocation_contract():
	"""Classification formulas are pinned to inspected v15/v16 controller paths."""
	from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import PurchaseInvoice
	from erpnext.controllers.accounts_controller import AccountsController

	profile = source_profile()
	for method, expected in (
		(
			AccountsController.set_payment_schedule,
			profile.payment_schedule_hash,
		),
		(
			PurchaseInvoice.make_gl_entries_for_tax_withholding,
			"eba00badaa00e40559423bad139e53041a90e94e2d27079cc903b18f49af2ee5",
		),
	):
		try:
			actual = sha256(getsource(method).encode()).hexdigest()
		except (OSError, TypeError) as exc:
			raise CalendarSourceUnavailable() from exc
		if actual != expected:
			raise CalendarSourceUnavailable()


class InvoiceReconciler:
	def __init__(self, engine, money, key):
		self.engine, self.key = engine, key
		self.money = lambda value: money(value, engine.currency_precision)
		self.documents, self.schedules, self.withholding = {}, {}, {}
		for doctype in ("Purchase Invoice", "Sales Invoice"):
			names = [r.voucher_no for r in engine.pre.values() if r.voucher_type == doctype]
			for offset in range(0, len(names), 500):
				chunk = names[offset : offset + 500]
				for doc in frappe.get_all(
					doctype,
					filters={"name": ["in", chunk]},
					fields=[
						"name",
						"currency",
						"party_account_currency",
						"conversion_rate",
						"grand_total",
						"rounded_total",
						"base_grand_total",
						"base_rounded_total",
						"total_advance",
						"write_off_amount",
					],
					limit_page_length=0,
				):
					self.documents[(doctype, doc.name)] = doc
				schedule = frappe.qb.DocType("Payment Schedule")
				query = (
					frappe.qb.from_(schedule)
					.select(
						schedule.parent,
						Count(schedule.name).as_("terms"),
						Min(schedule.due_date).as_("due_date"),
						Sum(schedule.base_payment_amount).as_("base_sum"),
					)
					.where((schedule.parent.isin(chunk)) & (schedule.parenttype == doctype))
					.groupby(schedule.parent)
				)
				for row in query.run(as_dict=True):
					self.schedules[(doctype, row.parent)] = row
				if doctype == "Purchase Invoice":
					tax = frappe.qb.DocType("Purchase Taxes and Charges")
					query = (
						frappe.qb.from_(tax)
						.select(tax.parent, Sum(tax.base_tax_amount_after_discount_amount).as_("amount"))
						.where(
							tax.parent.isin(chunk)
							& (tax.parenttype == doctype)
							& (tax.is_tax_withholding_account == 1)
						)
						.groupby(tax.parent)
					)
					for row in query.run(as_dict=True):
						self.withholding[row.parent] = self.money(row.amount)

	def cause(self, row):
		identity = (row.voucher_type, row.voucher_no)
		doc, schedule = self.documents.get(identity), self.schedules.get(identity)
		if not doc or not schedule or schedule.terms != 1:
			return None
		scheduled = self.money(schedule.base_sum)
		chosen = self.money(doc.base_rounded_total or doc.base_grand_total)
		tds = (
			self.withholding.get(row.voucher_no, Decimal(0)) if row.voucher_type == "Purchase Invoice" else 0
		)
		if (
			tds
			and self.money(row.invoiced) == chosen + tds
			and scheduled
			in {
				chosen,
				chosen + tds,
				self.money(doc.base_grand_total) + tds,
			}
		):
			return "withholding"
		if (
			doc.total_advance
			and doc.party_account_currency
			and doc.party_account_currency == row.account_currency
			and doc.party_account_currency != self.engine.company_currency
		):
			# Classify the known upstream unit mismatch, never use this arithmetic
			# as an obligation. This is the pinned controller's schedule formula.
			net = flt(doc.rounded_total or doc.grand_total) - flt(doc.write_off_amount)
			net -= doc.total_advance
			expected_schedule = self.money(flt(net * doc.conversion_rate, self.engine.currency_precision))
			if scheduled == expected_schedule:
				return "foreign_advance"
		return None

	def native(self, row, *, due_date=None, force=False):
		"""Use an isolated upstream engine; never mutate the running report."""
		from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

		clone = copy(self.engine)
		clone.filters = deepcopy(self.engine.filters)
		clone.filters.based_on_payment_terms = 0
		clone.data = []
		clone.invoice_details = {row.voucher_no: deepcopy(self.engine.metadata.get(row.voucher_no, {}))}
		value = frappe._dict(deepcopy(row))
		if due_date is not None:
			value.due_date = due_date
		clone.voucher_balance = {self.key(row): value}
		ReceivablePayableReport.build_data(clone)
		if force and not clone.data:
			# Settled overlay or an excluded FX proof, never a forecast event.
			clone.append_row(value)
		return clone.data

	def excluded_fx(self, row, present):
		account_residual = self.money(
			row.invoiced_in_account_currency
			- row.paid_in_account_currency
			- row.credit_note_in_account_currency
		)
		doc = self.documents.get((row.voucher_type, row.voucher_no))
		return (
			not present
			and doc is not None
			and bool(doc.party_account_currency)
			and row.account_currency == doc.party_account_currency
			and row.account_currency != self.engine.company_currency
			and account_residual == 0
			and self.money(row.invoiced - row.paid - row.credit_note) != 0
			and row.voucher_no not in self.engine.err_journals
			and not self.native(row)
		)

	def repair(self, row, expected, *, settled=False):
		cause = self.cause(row)
		if not cause:
			raise CalendarSourceUnavailable("reconciliation_mismatch")
		schedule = self.schedules[(row.voucher_type, row.voucher_no)]
		native = self.native(row, due_date=schedule.due_date, force=settled)
		if len(native) != 1 or self.money(native[0].outstanding) != expected:
			raise CalendarSourceUnavailable("reconciliation_mismatch")
		value = native[0]
		# A single genuinely undated term remains undated, even if invoice header
		# metadata has another date. Never invent a date for an unmatched split.
		value.due_date = schedule.due_date
		value._calendar_basis = cause
		return value
