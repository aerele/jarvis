"""A thin versioned contract around ERPNext's actual AP/AR term allocation.

The ordinary report remains unchanged. Open terms come from its output; zero
terms are captured separately, using pre-build balances and metadata. No ledger
data, schedule, payment or invoice is written by this adapter.
"""

from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from functools import cache
from hashlib import sha256
from inspect import signature

import frappe
from frappe.utils import flt, getdate

from jarvis.payment_calendar.books import validate_obligation_books
from jarvis.payment_calendar.compatibility import require_obligation_scope, source_profile
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.permissions import (
	incomplete_scope_reasons,
	require_company,
	require_report,
	row_filter_columns,
	visible_names,
)
from jarvis.payment_calendar.reconciliation import InvoiceReconciler, check_allocation_contract

MAX_LEDGER_ROWS = 100_000
MAX_VOUCHERS = 10_000


def money(value, precision: int) -> Decimal:
	try:
		result = Decimal(str(value or 0))
		if not result.is_finite():
			raise InvalidOperation
		return Decimal(str(flt(value or 0, precision))).quantize(Decimal(1).scaleb(-precision))
	except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
		raise CalendarSourceUnavailable("invalid_source_amount") from exc


def _key(row):
	return row.party_account, row.voucher_type, row.voucher_no, row.party


def _check_contract():
	from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

	source_profile()
	check_allocation_contract()
	for name, arguments in {
		"build_data": ("self",),
		"allocate_outstanding_based_on_payment_terms": ("self", "row"),
		"append_payment_term": ("self", "row", "d", "term", "company_currency"),
		"get_payment_terms": ("self", "row"),
		"append_row": ("self", "row"),
		"set_party_details": ("self", "row"),
		"is_invoice": ("self", "ple"),
	}.items():
		method = getattr(ReceivablePayableReport, name, None)
		if not callable(method) or tuple(signature(method).parameters) != arguments:
			raise CalendarSourceUnavailable()


@cache
def _report_class():
	from erpnext.accounts.report.accounts_receivable.accounts_receivable import ReceivablePayableReport

	class CalendarReport(ReceivablePayableReport):
		def __init__(self, filters, window_start, window_end):
			super().__init__(filters)
			self.window_start, self.window_end = getdate(window_start), getdate(window_end)
			self.terms = {}

		def append_payment_term(self, row, d, term, company_currency):
			super().append_payment_term(row, d, term, company_currency)
			row.payment_terms[-1]._calendar_discount = d.discounted_amount or 0

		def allocate_outstanding_based_on_payment_terms(self, row):
			super().allocate_outstanding_based_on_payment_terms(row)
			self.terms[_key(row)] = deepcopy(row.payment_terms)

		def build_data(self):
			if not isinstance(self.voucher_balance, dict):
				raise CalendarSourceUnavailable()
			# The upstream accumulator also contains zero-invoiced Payment Entry
			# placeholders. Only actual positive invoice obligations need an overlay.
			self.pre = {
				k: frappe._dict(deepcopy(r))
				for k, r in self.voucher_balance.items()
				if self.is_invoice(r) and r.invoiced > 0
			}
			if len(self.pre) > MAX_VOUCHERS:
				raise CalendarSourceUnavailable("too_large")
			self.metadata = deepcopy(self.invoice_details)
			self.ledger_residuals = {
				_key(row): money(row.invoiced - row.paid - row.credit_note, self.currency_precision)
				for row in self.voucher_balance.values()
			}
			reconciler = InvoiceReconciler(self, money, _key)
			self.excluded = []
			suppressed = set()
			counts = Counter(
				(r.voucher_type, r.voucher_no)
				for r in self.pre.values()
				if self.is_invoice(r) and r.invoiced > 0
			)
			if any(count > 1 for count in counts.values()):
				raise CalendarSourceUnavailable("multiple_invoice_accounts")
			super().build_data()
			# The report's output alone cannot prove reconciliation: a malformed
			# payment schedule can redefine an invoice's total. Compare allocated
			# output with the independent, pre-mutation ledger residual per invoice.
			allocated = {}
			for row in self.data:
				key = _key(row)
				allocated[key] = allocated.get(key, Decimal(0)) + money(
					row.outstanding, self.currency_precision
				)
			for key, row in self.pre.items():
				expected = money(row.invoiced - row.paid - row.credit_note, self.currency_precision)
				if allocated.get(key, Decimal(0)) != expected:
					if reconciler.excluded_fx(row, key in allocated):
						self.excluded.extend(reconciler.native(row, force=True))
						suppressed.add(key)
						continue
					replacement = reconciler.repair(row, expected)
					self.data = [r for r in self.data if _key(r) != key] + [replacement]
					self.terms[key] = []
			for key, row in self.pre.items():
				if (
					self.is_invoice(row)
					and row.invoiced > 0
					and key not in self.terms
					and key not in suppressed
				):
					company_residual = money(
						row.invoiced - row.paid - row.credit_note, self.currency_precision
					)
					account_residual = money(
						row.invoiced_in_account_currency
						- row.paid_in_account_currency
						- row.credit_note_in_account_currency,
						self.currency_precision,
					)
					if company_residual or account_residual:
						raise CalendarSourceUnavailable("currency_reconciliation_unavailable")
			candidates = {
				k: r
				for k, r in self.pre.items()
				if self.is_invoice(r)
				and r.invoiced > 0
				and k not in self.terms
				and k not in suppressed
				and money(r.invoiced - r.paid - r.credit_note, self.currency_precision) == 0
			}
			in_window = self._settled_in_window(candidates)
			for key in in_window:
				row = frappe._dict(deepcopy(candidates[key]))
				row.invoice_grand_total = row.invoiced
				self.allocate_outstanding_based_on_payment_terms(row)
				if any(money(term.outstanding, self.currency_precision) for term in row.payment_terms):
					try:
						self.terms[key] = [reconciler.repair(candidates[key], Decimal(0), settled=True)]
					except CalendarSourceUnavailable as exc:
						raise CalendarSourceUnavailable("settlement_schedule_mismatch") from exc
				if not row.payment_terms:
					row.outstanding = 0
					self.terms[key] = [row]

		def _settled_in_window(self, candidates):
			selected = set()
			for doctype in ("Purchase Invoice", "Sales Invoice"):
				keys = {k for k, r in candidates.items() if r.voucher_type == doctype}
				if not keys:
					continue
				names = [k[2] for k in keys]
				parents = set()
				for offset in range(0, len(names), 500):
					parents.update(
						frappe.get_all(
							"Payment Schedule",
							filters={
								"parent": ["in", names[offset : offset + 500]],
								"parenttype": doctype,
								"due_date": ["between", [self.window_start, self.window_end]],
							},
							pluck="parent",
						)
					)
				for key in keys:
					due = self.metadata.get(key[2], {}).get("due_date")
					if key[2] in parents or (due and self.window_start <= getdate(due) <= self.window_end):
						selected.add(key)
			return selected

	return CalendarReport


def _visible_rows(engine, report, rows):
	from frappe.desk.query_report import get_filtered_data

	filtered = get_filtered_data(
		report.ref_doctype, row_filter_columns(engine.columns), rows, frappe.session.user
	)
	# Apply the exact delivery predicate to every voucher, including JE/PE.
	allowed = {}
	for doctype in {r.voucher_type for r in filtered}:
		names = sorted({r.voucher_no for r in filtered if r.voucher_type == doctype})
		filters = {"docstatus": 1}
		if frappe.get_meta(doctype).has_field("company"):
			filters["company"] = engine.filters.company
		allowed[doctype] = visible_names(doctype, names, filters)
	parties = {}
	for doctype in {r.party_type for r in filtered if r.party_type and r.party}:
		parties[doctype] = visible_names(doctype, [r.party for r in filtered if r.party_type == doctype])
	return [
		r
		for r in filtered
		if (r.voucher_type not in allowed or r.voucher_no in allowed[r.voucher_type])
		and r.party in parties.get(r.party_type, set())
	]


def _permission_columns(columns):
	return [
		{key: col.get(key) for key in ("fieldname", "fieldtype", "options")}
		for col in columns
		if col.get("fieldtype") in ("Link", "Dynamic Link")
	]


def _permission_row(row, columns):
	fields = {
		"party",
		"party_type",
		"party_account",
		"voucher_type",
		"voucher_no",
		"posting_date",
		"due_date",
	}
	for col in _permission_columns(columns):
		fields.add(col["fieldname"])
		if col["fieldtype"] == "Dynamic Link" and col.get("options"):
			fields.add(col["options"])
	return {field: row.get(field) for field in sorted(fields)}


def read_obligations(company: str, account_type: str, as_of: str, window_start: str, window_end: str) -> dict:
	"""All authorized residuals, plus settled terms in the requested due window."""
	try:
		return _read_obligations(company, account_type, as_of, window_start, window_end)
	except (AttributeError, KeyError, TypeError) as exc:
		raise CalendarSourceUnavailable() from exc


def _read_obligations(company, account_type, as_of, window_start, window_end):
	if not as_of or not window_start or not window_end:
		raise frappe.ValidationError("The accounting date and calendar window are required.")
	report = require_report(company, account_type)
	_check_contract()
	require_obligation_scope(account_type)
	# ERPNext skips its common company/account filters when the Party Type
	# mapping is absent, and skips its account filter when no account matches.
	# Incomplete accounting setup must never widen this reader's source scope.
	party_type = "Supplier" if account_type == "Payable" else "Customer"
	if frappe.db.get_value("Party Type", party_type, "account_type") != account_type or not frappe.db.exists(
		"Account", {"company": company, "account_type": account_type}
	):
		raise CalendarSourceUnavailable("source_configuration_unavailable")
	start, end, anchor = getdate(window_start), getdate(window_end), getdate(as_of)
	if start > end:
		raise frappe.ValidationError("Invalid calendar window.")
	company_values = require_company(company)
	validate_obligation_books(company, account_type, anchor)
	if (
		frappe.db.count(
			"Payment Ledger Entry",
			{"company": company, "account_type": account_type, "delinked": 0, "posting_date": ["<=", anchor]},
		)
		> MAX_LEDGER_ROWS
	):
		raise CalendarSourceUnavailable("too_large")
	engine = _report_class()(
		{
			"company": company,
			"report_date": anchor,
			"based_on_payment_terms": 1,
			"ageing_based_on": "Due Date",
			"range": "30,60,90,120",
			"group_by_party": 0,
			"ignore_accounts": 0,
			"show_future_payments": 0,
			"in_party_currency": 0,
			"for_revaluation_journals": 0,
			"finance_book": None,
		},
		start,
		end,
	)
	if engine.ple_fetch_method not in ("Buffered Cursor", "UnBuffered Cursor"):
		raise CalendarSourceUnavailable("source_configuration_unavailable")
	try:
		engine.run(
			{
				"account_type": account_type,
				"naming_by": ["Buying Settings", "supp_master_name"]
				if account_type == "Payable"
				else ["Selling Settings", "cust_master_name"],
			}
		)
	except (AttributeError, KeyError, TypeError) as exc:
		raise CalendarSourceUnavailable() from exc
	if any(
		not hasattr(engine, field) for field in ("currency_precision", "columns", "data", "terms", "metadata")
	):
		raise CalendarSourceUnavailable()
	precision = engine.currency_precision
	canonical = _visible_rows(engine, report, engine.data)
	# Upstream keeps positive float residues, including values below the currency
	# precision. Those terms already belong to the settled overlay, exactly once.
	rows = [
		(r, r.get("_calendar_discount", 0))
		for r in canonical
		if not (money(r.outstanding, precision) == 0 and _key(r) in engine.terms)
	]
	overlay = []
	for terms in engine.terms.values():
		for term in terms:
			if money(term.outstanding, precision) != 0 or term.invoiced <= 0:
				continue
			due = term.due_date or engine.metadata.get(term.voucher_no, {}).get("due_date")
			if not due or not start <= getdate(due) <= end:
				continue
			term.due_date = due
			engine.set_party_details(term)
			overlay.append(term)
	visible_overlay = _visible_rows(engine, report, overlay)
	rows.extend((term, term.get("_calendar_discount", 0)) for term in visible_overlay)
	result = []
	for index, (row, discount) in enumerate(rows):
		original, remaining = money(row.invoiced, precision), money(row.outstanding, precision)
		if not original and not remaining:
			continue
		credit, discount = money(row.credit_note, precision), money(discount, precision)
		status = "Unpaid" if remaining >= original else "Partially paid"
		if remaining <= 0:
			# Payment Entry deductions and journal write-offs are not proof of cash payment.
			status = "Settled"
		adjustment = remaining < 0 or original <= 0
		if adjustment:
			status = "Adjustment"
		basis = row.get("_calendar_basis")
		if basis and remaining > 0 and remaining < original:
			status = "Partially settled"
		result.append(
			{
				"id": sha256(f"{account_type}:{_key(row)}:{index}".encode()).hexdigest()[:24],
				"voucher_type": row.voucher_type,
				"voucher_no": row.voucher_no,
				"party_type": row.party_type or ("Supplier" if account_type == "Payable" else "Customer"),
				"party": row.party,
				"party_name": row.get("supplier_name") or row.get("customer_name") or row.party,
				"account": row.party_account,
				"posting_date": str(getdate(row.posting_date)),
				"due_date": str(getdate(row.due_date)) if row.due_date else None,
				"term": row.payment_term or "",
				"original": str(original),
				"paid": None if basis else str(money(row.paid, precision) - discount),
				"credit": None if basis else str(credit),
				"discount": None if basis else str(discount),
				**({"allocation_basis": "invoice", "allocation_reason": basis} if basis else {}),
				"remaining": str(remaining),
				"settlement": status,
				"kind": "adjustment" if adjustment else "obligation",
				"_permission": _permission_row(row, engine.columns),
			}
		)
	canonical_total = sum((money(r.outstanding, precision) for r in canonical), Decimal(0))
	if sum((Decimal(r["remaining"]) for r in result), Decimal(0)) != canonical_total:
		raise CalendarSourceUnavailable("reconciliation_mismatch")
	result.sort(key=lambda r: (r["due_date"] or "", r["voucher_type"], r["voucher_no"], r["id"]))
	visible_excluded = _visible_rows(engine, report, engine.excluded)
	excluded_rows = [
		{
			"voucher_type": row.voucher_type,
			"voucher_no": row.voucher_no,
			"party_type": row.party_type,
			"party": row.party,
			"remaining": str(money(row.outstanding, precision)),
			"kind": "settled_fx",
			"account_remaining": str(money(row.outstanding_in_account_currency, precision)),
			"_permission": _permission_row(row, engine.columns),
		}
		for row in visible_excluded
	]
	visible_keys = {_key(row) for row in canonical + visible_overlay + visible_excluded}
	fx_total = sum((money(row.outstanding, precision) for row in visible_excluded), Decimal(0))
	if canonical_total + fx_total != sum((engine.ledger_residuals[k] for k in visible_keys), Decimal(0)):
		raise CalendarSourceUnavailable("reconciliation_mismatch")
	notes = {}
	fallback_count = sum(row.get("allocation_basis") == "invoice" for row in result)
	if fallback_count:
		notes["invoice_level"] = {"count": fallback_count}
	if excluded_rows:
		notes["currency_residue_excluded"] = {"count": len(excluded_rows), "amount": str(fx_total)}
	reasons = incomplete_scope_reasons()
	if excluded_rows:
		reasons = sorted({*reasons, "currency_residue_excluded"})
	if len(visible_excluded) != len(engine.excluded):
		reasons = sorted({*reasons, "filtered_obligations"})
	if len(canonical) != len(engine.data) or len(visible_overlay) != len(overlay):
		reasons = sorted({*reasons, "filtered_obligations"})
	return {
		"rows": result,
		"excluded_rows": excluded_rows,
		"notes": notes,
		"canonical_total": str(canonical_total),
		"currency": company_values.default_currency,
		"precision": precision,
		"complete_for_company": not reasons,
		"scope_reasons": reasons,
		"permission_columns": _permission_columns(engine.columns),
	}
