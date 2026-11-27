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
from frappe.query_builder import Order
from frappe.query_builder.functions import Count
from frappe.utils import flt, getdate

from jarvis.payment_calendar import compatibility, read_log
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
# Names per payment-schedule query; the same chunk size as the settled-in-window read.
TERM_CHUNK = 500
PARTY_CHUNK = 500
# False once any speed override fell back to upstream in this process; it stays False until restart.
FAST_PATH = True
_warned_fallbacks = set()


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


def _deep_dict(row):
	return frappe._dict(deepcopy(row))


def check_contract():
	"""Raise CalendarSourceUnavailable unless the installed Frappe/ERPNext pair fits the calendar."""
	_check_contract()


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
			# Upstream creates the accumulator in get_data; batched reads scan it. None until then, so a
			# renamed upstream accumulator makes build_data refuse instead of reading an empty one.
			self.voucher_balance = None
			# Shared with the shallow copy that `InvoiceReconciler.native` makes. The term order is
			# fixed when a chunk is read, while upstream reads is_historical_report() on every call;
			# a midnight crossing during one run is accepted.
			self._term_cache = {}
			self._company_currency = None

		def get_payment_terms(self, row):
			"""ERPNext's body, with the per-invoice schedule query batched per chunk of invoices.

			Keep in sync with ERPNext accounts_receivable.py get_payment_terms; pinned in compatibility._SPEED_PINS.
			"""
			if not compatibility.upstream_matches("get_payment_terms"):
				_speed_fallback("get_payment_terms")
				return super().get_payment_terms(row)
			details = self._schedule_rows(row.voucher_type, row.voucher_no)
			original_row = frappe._dict(row)
			row.payment_terms = []
			# Cr Note's don't have Payment Terms
			if not details:
				return
			if self._company_currency is None:
				self._company_currency = frappe.get_value(
					"Company", self.filters.get("company"), "default_currency"
				)
			company_currency = self._company_currency
			# Advance allocated during invoicing is not considered in payment terms
			row.paid -= self.get_advance_in_report_currency(details[0], company_currency)
			if len(details) == 1 and details[0].payment_term:
				self.append_payment_term(row, details[0], original_row, company_currency)
				return
			for d in details:
				self.append_payment_term(row, d, frappe._dict(original_row), company_currency)

		def _schedule_rows(self, voucher_type, voucher_no):
			key = (voucher_type, voucher_no)
			if key not in self._term_cache:
				self._fetch_schedule_rows(voucher_type, voucher_no)
			return self._term_cache[key]

		def _fetch_schedule_rows(self, voucher_type, missed_name):
			"""One query for the missed invoice and the next not-yet-read invoices of the same type."""
			names = {missed_name: []}
			# Every invoice of the engine, returns included: upstream allocates them too.
			for entry in (self.voucher_balance or {}).values():
				if len(names) >= TERM_CHUNK:
					break
				if (
					entry.voucher_type == voucher_type
					and entry.voucher_no not in names
					and (voucher_type, entry.voucher_no) not in self._term_cache
					and self.is_invoice(entry)
				):
					names[entry.voucher_no] = []
			# MariaDB's IN matches letter case loosely, so a parent can come back in another case than the
			# ledger's voucher_no. This folds letter case only; accent and trailing-space differences are
			# not matched (theoretical for validated links). Postgres matches exactly.
			folded = {name.casefold(): name for name in names} if frappe.db.db_type == "mariadb" else {}
			for detail in self._schedule_query(voucher_type, list(names)).run(as_dict=True):
				parent = detail.parent if detail.parent in names else folded[detail.parent.casefold()]
				names[parent].append(detail)
			for name, details in names.items():
				self._term_cache[(voucher_type, name)] = details

		def _schedule_query(self, voucher_type, names):
			invoice, schedule = frappe.qb.DocType(voucher_type), frappe.qb.DocType("Payment Schedule")
			query = (
				frappe.qb.from_(invoice)
				.join(schedule)
				.on(invoice.name == schedule.parent)
				.select(
					invoice.name,
					invoice.party_account_currency,
					invoice.currency,
					invoice.conversion_rate,
					invoice.total_advance,
					schedule.due_date,
					schedule.payment_term,
					schedule.payment_amount,
					schedule.base_payment_amount,
					schedule.description,
					schedule.paid_amount,
					schedule.base_paid_amount,
					schedule.discounted_amount,
					schedule.parent,
				)
				.where(
					(schedule.parenttype == voucher_type)
					& invoice.name.isin(names)
					& (invoice.is_return == 0)
				)
				.orderby(schedule.parent)
			)
			if self.is_historical_report():
				query = query.orderby(schedule.due_date)
			else:
				query = query.orderby(schedule.paid_amount, order=Order.desc).orderby(schedule.due_date)
			return query.orderby(schedule.idx)

		def prepare_ple_query(self):
			super().prepare_ple_query()
			# Upstream orders by (posting_date, party) only, so tied lines come back in an undefined
			# order, and the last line of a tie wins the voucher's cost_center/project.
			if source_profile().major == "16":
				self.ple_query = self.ple_query.orderby(self.ple.name)

		def _seed_party_details(self):
			"""Fill `party_details` for every party of the ledger in one query per chunk of parties.

			Reproduces ERPNext's get_party_details lookup (accounts_receivable.py): the doctype by account
			type, the same fields, None for a missing party. Upstream then only reads the dict; if it
			stops reading `party_details`, the seed is unused and upstream looks parties up itself.
			"""
			parties = list(
				dict.fromkeys(
					entry.party
					for entry in self.voucher_balance.values()
					if entry.party and entry.party not in self.party_details
				)
			)
			if self.account_type == "Receivable":
				doctype = "Customer"
				fields = ["customer_name", "territory", "customer_group", "customer_primary_contact"]
			else:
				doctype, fields = "Supplier", ["supplier_name", "supplier_group"]
			is_mariadb = frappe.db.db_type == "mariadb"
			for start in range(0, len(parties), PARTY_CHUNK):
				chunk = parties[start : start + PARTY_CHUNK]
				found = {
					record.pop("name"): record
					for record in frappe.get_all(
						doctype, filters={"name": ["in", chunk]}, fields=["name", *fields]
					)
				}
				# MariaDB's get_value matches an id that differs only in letter case; Postgres does not.
				# This folds letter case only; accent and trailing-space differences are not matched
				# (theoretical for validated links).
				folded = {name.casefold(): record for name, record in found.items()}
				# None for a missing party, as upstream's get_value returns; it also stops a re-read.
				for party in chunk:
					record = found.get(party)
					if record is None and is_mariadb:
						record = folded.get(party.casefold())
					self.party_details[party] = record

		def append_payment_term(self, row, d, term, company_currency):
			super().append_payment_term(row, d, term, company_currency)
			row.payment_terms[-1]._calendar_discount = d.discounted_amount or 0

		def allocate_outstanding_based_on_payment_terms(self, row):
			super().allocate_outstanding_based_on_payment_terms(row)
			# build_data sets the gate once; allocate can also run before it, and native() clones share it.
			fast = getattr(self, "_fast", None)
			if fast is None:
				fast = source_profile().major == "16"
			if fast:
				self.terms[_key(row)] = [frappe._dict(term) for term in row.payment_terms]
			else:
				self.terms[_key(row)] = deepcopy(row.payment_terms)

		def voucher_limit(self):
			"""The invoice count above which the read refuses `too_large`; None lifts the guard."""
			return MAX_VOUCHERS

		def has_multiple_invoice_accounts(self, counts):
			"""True when one invoice appears under more than one ledger key, which refuses the read.

			`counts` maps each (voucher_type, voucher_no) to the number of ledger keys (account, party)
			it has. A subclass may only widen this refusal, never lift it.
			"""
			return any(count > 1 for count in counts.values())

		def build_data(self):
			if not isinstance(self.voucher_balance, dict):
				raise CalendarSourceUnavailable()
			# The upstream accumulator also contains zero-invoiced Payment Entry
			# placeholders. Only actual positive invoice obligations need an overlay.
			fast = self._fast = source_profile().major == "16"
			if fast:
				# set_defaults (in run) already reset party_details; get_data fills voucher_balance, and
				# super().build_data() below is the first reader of party_details.
				self._seed_party_details()
			if fast and isinstance(getattr(self, "err_journals", None), list):
				# Only membership is used.
				self.err_journals = set(self.err_journals)
			# The entries hold scalars and dates only, so a flat copy is a full copy on v16.
			copy_row = frappe._dict if fast else _deep_dict
			self.pre = {
				k: copy_row(r)
				for k, r in self.voucher_balance.items()
				if self.is_invoice(r) and r.invoiced > 0
			}
			limit = self.voucher_limit()
			if limit is not None and len(self.pre) > limit:
				raise CalendarSourceUnavailable("too_large")
			if fast:
				# Per entry: upstream pops due_date from the source entries.
				self.metadata = {k: frappe._dict(v) for k, v in self.invoice_details.items()}
			else:
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
			if self.has_multiple_invoice_accounts(counts):
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
			replacements = {}
			for key, row in self.pre.items():
				expected = money(row.invoiced - row.paid - row.credit_note, self.currency_precision)
				if allocated.get(key, Decimal(0)) != expected:
					if reconciler.excluded_fx(row, key in allocated):
						self.excluded.extend(reconciler.native(row, force=True))
						suppressed.add(key)
						continue
					replacement = reconciler.repair(row, expected)
					if fast:
						replacements[key] = replacement
					else:
						self.data = [r for r in self.data if _key(r) != key] + [replacement]
					self.terms[key] = []
			if replacements:
				# Survivors in their order, then the replacements in pre order: today's result in one pass.
				self.data = [r for r in self.data if _key(r) not in replacements] + list(
					replacements.values()
				)
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
				row = copy_row(candidates[key])
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
				for offset in range(0, len(names), TERM_CHUNK):
					parents.update(
						frappe.get_all(
							"Payment Schedule",
							filters={
								"parent": ["in", names[offset : offset + TERM_CHUNK]],
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
			# In the order of `candidates`, not the set's: the order sets the overlay rows' positions, so their
			# ids. A set of text keys iterates by hash, which differs between two sets of the same keys built
			# in another order or size (the reader's candidates are fewer than today's) and between processes.
			return [key for key in candidates if key in selected]

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


def _ledger_rows_exceed(company, account_type, anchor, limit):
	"""True when more than `limit` live ledger rows exist; reads at most `limit + 1` of them."""
	ledger = frappe.qb.DocType("Payment Ledger Entry")
	rows = (
		frappe.qb.from_(ledger)
		.select(1)
		.where(
			(ledger.company == company)
			& (ledger.account_type == account_type)
			& (ledger.delinked == 0)
			& (ledger.posting_date <= anchor)
		)
		.limit(limit + 1)
	)
	(counted,) = frappe.qb.from_(rows.as_("ledger_rows")).select(Count("*")).run()[0]
	return counted > limit


def _read_obligations(company, account_type, as_of, window_start, window_end):
	# builtin_reader.read_obligations repeats this sequence (inputs, books, engine, assemble) with its own
	# reads and no size guards: a change to the gates or their order here needs the same change there.
	report, start, end, anchor, company_values = _require_inputs(
		company, account_type, as_of, window_start, window_end
	)
	fast = source_profile().major == "16"
	# On v16 a bounded probe goes first: a huge ledger must not pay for the full-history book scan.
	if fast and _ledger_rows_exceed(company, account_type, anchor, MAX_LEDGER_ROWS):
		raise CalendarSourceUnavailable("too_large")
	validate_obligation_books(company, account_type, anchor)
	if (
		not fast
		and frappe.db.count(
			"Payment Ledger Entry",
			{"company": company, "account_type": account_type, "delinked": 0, "posting_date": ["<=", anchor]},
		)
		> MAX_LEDGER_ROWS
	):
		raise CalendarSourceUnavailable("too_large")
	engine = _run_engine(_report_class(), _engine_filters(company, anchor), account_type, start, end)
	return _assemble(engine, report, account_type, start, end, company_values)


def _require_inputs(company, account_type, as_of, window_start, window_end):
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
	return report, start, end, anchor, company_values


def _engine_filters(company, anchor):
	return {
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
	}


def _run_engine(engine_factory, filters, account_type, start, end):
	"""Build the engine with `engine_factory(filters, start, end)` (a class, or a callable) and run it."""
	engine = engine_factory(filters, start, end)
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
	return engine


def _assemble(engine, report, account_type, start, end, company_values):
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


def _speed_fallback(name: str) -> None:
	"""A speed override met an unaudited upstream source and ran upstream instead; log once per name.

	Nothing here may raise into the read: upstream already ran, or is about to.
	"""
	global FAST_PATH
	FAST_PATH = False
	try:
		import erpnext

		version = erpnext.__version__
		# v15 has no pins by design: silent. Only an unknown v16 source needs an audit.
		if version.split(".")[0] != "16" or name in _warned_fallbacks:
			return
		_warned_fallbacks.add(name)
		read_log.speed_fallback(name, compatibility.installed_hash(name), version)
	except Exception:
		pass
