"""Prepare source-backed LCV allocations through the installed ERP, without saving."""

from __future__ import annotations

import copy
import math
from collections import Counter
from decimal import Decimal

import frappe
from frappe.utils import flt, getdate, nowdate

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError, PreviewSandboxLost, RetryLaterError
from jarvis.tools._lcv_source_tax import _DENIED, LCVSourceTaxNeedsInput, _guard_fields, gate_lcv
from jarvis.tools._preview_sandbox import preview_sandbox
from jarvis.tools._result_guard import MAX_RESULT_CHARS, _size
from jarvis.tools.preview_doc import _error_text

LCV = "Landed Cost Voucher"
RECEIPTS = {"Purchase Receipt", "Purchase Invoice"}
MAX_ROWS = 500
MAX_SOURCES = 20
MAX_SOURCE_ROWS = 2000
_ITEM_FIELDS = (
	"item_code",
	"description",
	"qty",
	"rate",
	"amount",
	"is_fixed_asset",
	"cost_center",
	"receipt_document_type",
	"receipt_document",
	"purchase_receipt_item",
	"stock_entry_item",
)


class _Gap(Exception):
	def __init__(self, code, message, **details):
		self.issue = {"code": code, "message": message, **details}


def preview_landed_cost_voucher(
	company: str,
	receipts: list,
	charges: list | None = None,
	bills_of_entry: list | None = None,
	posting_date: str | None = None,
	existing_draft: str | None = None,
) -> dict:
	"""Read-only calculation; returns ready draft values or explicit unresolved inputs.

	Native draft validation and allocation reconciliation are distinct from submit
	readiness. This call neither saves a voucher nor reserves source invoice costs.
	"""
	_name(company)
	_list(receipts, "receipts", MAX_SOURCES, required=True)
	_list(charges, "charges", MAX_SOURCES)
	_list(bills_of_entry, "bills_of_entry", MAX_SOURCES)
	for ref in receipts:
		if (
			not isinstance(ref, dict)
			or not isinstance(ref.get("doctype"), str)
			or ref.get("doctype") not in RECEIPTS
		):
			raise InvalidArgumentError("receipts must identify Purchase Receipt or Purchase Invoice records")
		_name(ref.get("name"))
	for name in bills_of_entry or []:
		_name(name)
	if existing_draft is not None:
		_name(existing_draft)
		if charges or bills_of_entry or posting_date is not None:
			raise InvalidArgumentError(
				"existing_draft reconciles saved values; omit charges, bills_of_entry and posting_date"
			)
	if posting_date is not None:
		_name(posting_date)
		try:
			getdate(posting_date)
		except Exception as exc:
			raise InvalidArgumentError("posting_date must be a valid date") from exc
	if not frappe.db.exists("DocType", LCV):
		return _unavailable("ERPNext landed-cost support is not installed.")
	if not frappe.has_permission(LCV, "create"):
		raise PermissionDeniedError("No permission to prepare a Landed Cost Voucher")
	from erpnext.stock.doctype.landed_cost_voucher import landed_cost_voucher as controller

	if not callable(getattr(controller, "get_lcv_dimension_fields", None)):
		return _unavailable(
			"This ERP version does not provide the supported landed-cost calculation interface."
		)
	try:
		with preview_sandbox():
			prep = _Preparation(company, posting_date or nowdate())
			prep.receipts(receipts)
			if existing_draft:
				return _bounded(prep.saved_draft(existing_draft))
			prep.assessments(bills_of_entry or [])
			prep.common_costs(charges or [])
			if not prep.doc.taxes:
				raise _Gap("no_costs", "Identify the assessment or booked service costs to allocate.")
			prep.doc.distribute_charges_based_on = "Distribute Manually"
			prep.doc.insert()
			prep.validate_allocations(prep.doc)
			return _bounded(prep.result())
	except LCVSourceTaxNeedsInput as exc:
		return exc.assessment
	except _Gap as gap:
		return {"status": "needs_input", "issues": [gap.issue]}
	except (PermissionDeniedError, frappe.PermissionError, PreviewSandboxLost):
		raise
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError) as exc:
		raise RetryLaterError(
			"The source records are busy. Retry this read-only preview after the other transaction finishes."
		) from exc
	except frappe.ValidationError as exc:
		frappe.clear_messages()
		return {
			"status": "needs_input",
			"issues": [{"code": "native_validation", "message": _error_text(exc)}],
		}


def _bounded(result):
	if _size(result) > MAX_RESULT_CHARS - 1000:
		raise _Gap(
			"result_too_large",
			"The complete draft exceeds the review size limit. Narrow the source scope or use the ERP form.",
		)
	return result


def _unavailable(message):
	return {"status": "needs_input", "issues": [{"code": "capability_unavailable", "message": message}]}


def _name(value):
	if not isinstance(value, str) or not value.strip() or len(value) > 140:
		raise InvalidArgumentError("Source references must be non-empty names, not filters")
	return value


def _list(value, label, maximum, required=False):
	if value is None and not required:
		return
	if not isinstance(value, list) or len(value) > maximum or (required and not value):
		raise InvalidArgumentError(
			f"{label} must be a list with {'1' if required else '0'} to {maximum} entries"
		)


def _number(value):
	try:
		if isinstance(value, bool):
			raise ValueError
		number = float(value)
		if not math.isfinite(number):
			raise ValueError
	except (TypeError, ValueError, OverflowError) as exc:
		raise InvalidArgumentError("Amounts must be finite numbers") from exc
	return number


def _key(row):
	return (row.receipt_document_type, row.receipt_document, row.purchase_receipt_item)


class _Preparation:
	def __init__(self, company, posting_date):
		self.cache = {}
		self.sources = []
		self.charges = []
		self.components = {}
		self.warnings = []
		self.source_rows = 0
		from erpnext.stock.doctype.landed_cost_voucher.landed_cost_voucher import get_lcv_dimension_fields

		self.dimensions = get_lcv_dimension_fields()
		self.company = self.read("Company", company)
		self.doc = frappe.new_doc(LCV)
		self.doc.company = company
		self.doc.posting_date = str(getdate(posting_date))
		self.doc.distribute_charges_based_on = "Distribute Manually"
		self.currency = self.company.default_currency
		self.receipt_docs = {}

	def read(self, doctype, name):
		_name(name)
		key = (doctype, name)
		if key in self.cache:
			return self.cache[key]
		if len(self.cache) > MAX_SOURCE_ROWS:
			raise _Gap("source_too_large", "Too many distinct source records for one preview.")
		try:
			if not frappe.has_permission(doctype, "read", doc=name):
				raise PermissionDeniedError(_DENIED)
			doc = frappe.get_doc(doctype, name)
		except (frappe.DoesNotExistError, frappe.PermissionError) as exc:
			frappe.clear_messages()
			raise PermissionDeniedError(_DENIED) from exc
		_guard_fields(doc.meta, dimensions=self.dimensions)
		for field in doc.meta.get_table_fields():
			if field.fieldname not in {"items", "taxes", "vendor_invoices", "purchase_receipts"}:
				continue
			if len(doc.get(field.fieldname) or []) > MAX_ROWS:
				raise _Gap("source_too_large", "This source exceeds the bounded landed-cost preview size.")
			self.source_rows += len(doc.get(field.fieldname) or [])
			if self.source_rows > MAX_SOURCE_ROWS:
				raise _Gap("source_too_large", "The combined source rows exceed this bounded preview.")
			_guard_fields(frappe.get_meta(field.options), doctype, self.dimensions)
		self.cache[key] = doc
		if doctype in RECEIPTS | {"Bill of Entry", LCV}:
			if len(self.sources) >= MAX_SOURCES * 3:
				raise _Gap("source_too_large", "Too many linked business documents for one preview.")
			self.sources.append({"doctype": doctype, "name": name, "modified": str(doc.modified)})
		return doc

	def submitted(self, doctype, name):
		doc = self.read(doctype, name)
		if doc.company != self.doc.company or doc.docstatus != 1:
			raise _Gap("source_state", "Sources must be submitted and belong to the selected company.")
		return doc

	def receipts(self, refs):
		for ref in refs:
			key = (ref["doctype"], ref["name"])
			if key in self.receipt_docs:
				raise _Gap("duplicate_receipt", "The same receipt was selected more than once.")
			doc = self.submitted(*key)
			if doc.doctype == "Purchase Invoice" and not doc.update_stock:
				raise _Gap("not_stock_receipt", "This Purchase Invoice does not update stock.")
			self.receipt_docs[key] = doc
			for row in doc.items:
				self.read("Item", row.item_code)
			self.doc.append(
				"purchase_receipts", {"receipt_document_type": key[0], "receipt_document": key[1]}
			)
		self.doc.get_items_from_purchase_receipts()
		if not self.doc.items or len(self.doc.items) > MAX_ROWS:
			raise _Gap("receipt_items", "No supported stock/asset lines, or too many lines, were found.")
		self.items = {_key(row): row for row in self.doc.items}
		if len(self.items) != len(self.doc.items):
			raise _Gap("ambiguous_rows", "Receipt lines could not be uniquely identified.")
		for row in self.doc.items:
			row.applicable_charges = 0
			source = self.source_row(_key(row))
			for dimension in self.dimensions:
				if row.meta.has_field(dimension):
					row.set(
						dimension,
						source.get(dimension)
						or self.receipt_docs[_key(row)[:2]].get(dimension)
						or row.get(dimension),
					)

	def assessments(self, names):
		if len(set(names)) != len(names):
			raise _Gap("duplicate_assessment", "An assessment was selected more than once.")
		if not names:
			return
		if "india_compliance" not in frappe.get_installed_apps():
			raise _Gap("capability_unavailable", "India Compliance is required to map these assessments.")
		from india_compliance.gst_india.doctype.bill_of_entry.bill_of_entry import (
			get_items_for_landed_cost_voucher,
		)
		from india_compliance.gst_india.overrides.ineligible_itc import (
			update_landed_cost_voucher_for_gst_expense,
		)

		covered_receipts = set()
		for name in names:
			boe = self.submitted("Bill of Entry", name)
			self._assessment_sources(boe)
			try:
				mapped = get_items_for_landed_cost_voucher(boe)
			except (KeyError, TypeError, IndexError) as exc:
				raise _Gap(
					"unsupported_assessment_shape",
					"The installed mapper cannot resolve this partial receipt topology.",
				) from exc
			keys = {(r.parenttype, r.parent, r.name): r for r in mapped.values()}
			if not keys or set(keys) - set(self.items):
				raise _Gap(
					"assessment_scope",
					"The assessment covers missing or different receipt lines; confirm the receipt scope.",
				)
			covered_receipts.update((k[0], k[1]) for k in keys)
			scratch = self.scratch(list(keys))
			for item in scratch.items:
				source = keys[_key(item)]
				if source.customs_duty is None:
					raise _Gap("assessment_scope", "An assessment duty line has no exact receipt match.")
				item.applicable_charges = source.customs_duty
				item.boe_detail = source.boe_detail
			if abs(sum(r.applicable_charges for r in scratch.items) - boe.total_customs_duty) > 0.01:
				raise _Gap("assessment_balance", "The mapped receipts do not cover the full assessed duty.")
			scratch.append(
				"taxes",
				{
					"expense_account": boe.customs_expense_account,
					"description": f"Customs duty: {boe.name}",
					"amount": boe.total_customs_duty,
				},
			)
			before = {_key(r): r.applicable_charges for r in scratch.items}
			update_landed_cost_voucher_for_gst_expense(boe, scratch)
			repeats = Counter(r.boe_detail for r in scratch.items)
			if any(
				repeats[r.boe_detail] > 1 and r.applicable_charges != before[_key(r)] for r in scratch.items
			):
				raise _Gap(
					"split_ineligible_tax",
					"Ineligible GST on this split assessment needs accountant reconciliation; the native hook repeats the tax.",
				)
			for tax in scratch.taxes:
				if not tax.expense_account:
					raise _Gap("missing_account", "An assessment cost has no expense account configured.")
				self.read("Account", tax.expense_account)
				# Assessed amounts are company currency. A foreign account needs an
				# explicit supported conversion, not a posting-date rate lookup.
				if self.account_currency(tax.expense_account) != self.currency:
					raise _Gap(
						"assessment_account_currency",
						"Confirm the recorded assessment conversion for this foreign-currency expense account.",
					)
				tax.exchange_rate = 1
			self._prior_duty(keys, {r.expense_account for r in scratch.taxes})
			self.reconcile(scratch)
			for row in scratch.items:
				target = self.items[_key(row)]
				target.applicable_charges = flt(
					target.applicable_charges + row.applicable_charges, target.precision("applicable_charges")
				)
				self.component(row, name, "assessment")
			for tax in scratch.taxes:
				self.doc.append("taxes", self.tax_values(tax))
				self.charges.append(
					{
						"source": boe.name,
						"kind": "assessment",
						"amount": tax.base_amount,
						"description": tax.description,
					}
				)
		if covered_receipts != set(self.receipt_docs):
			raise _Gap(
				"assessment_scope",
				"Selected receipts and assessed receipts differ; confirm whether the extra receipt belongs in this voucher.",
			)

	def _assessment_sources(self, boe):
		invoices = {r.purchase_invoice for r in boe.items}
		if len(invoices) > MAX_SOURCES:
			raise _Gap("source_too_large", "Too many goods invoices for this bounded assessment preview.")
		for name in invoices:
			invoice = self.submitted("Purchase Invoice", name)
			for item in invoice.items:
				if item.purchase_receipt:
					self.submitted("Purchase Receipt", item.purchase_receipt)
			parents = frappe.get_all(
				"Purchase Receipt Item",
				filters={"purchase_invoice": name, "docstatus": 1},
				pluck="parent",
				limit_page_length=MAX_ROWS + 1,
			)
			if len(parents) > MAX_ROWS:
				raise _Gap("source_too_large", "Too many receipt links for this assessment.")
			for parent in set(parents):
				self.submitted("Purchase Receipt", parent)

	def _prior_duty(self, keys, accounts):
		matches = frappe.get_all(
			"Landed Cost Item",
			filters={
				"receipt_document_type": ["in", sorted({k[0] for k in keys})],
				"receipt_document": ["in", sorted({k[1] for k in keys})],
				"purchase_receipt_item": ["in", sorted({k[2] for k in keys})],
				"docstatus": ["in", [0, 1]],
			},
			fields=["parent", "receipt_document_type", "receipt_document", "purchase_receipt_item"],
			limit_page_length=MAX_ROWS + 1,
		)
		if len(matches) > MAX_ROWS:
			raise _Gap("source_too_large", "Too many existing landed-cost links to reconcile.")
		parents = {row.parent for row in matches if _key(row) in keys}
		for name in sorted(parents):
			doc = self.read(LCV, name)
			if any(r.expense_account in accounts for r in doc.taxes):
				raise _Gap(
					"possible_prior_duty",
					"An existing voucher may already allocate this assessment; reconcile it first.",
					vouchers=[name],
				)

	def common_costs(self, charges):
		seen_rows = set()
		claims = Counter()
		for charge in charges:
			if not isinstance(charge, dict):
				raise InvalidArgumentError("Each charge must identify a booked Purchase Invoice")
			name = charge.get("purchase_invoice")
			if not name:
				raise _Gap(
					"unbooked_cost",
					"Identify the booked service invoice, or prepare its payable in a separate flow.",
				)
			_name(name)
			unknown = set(charge) - {"purchase_invoice", "basis", "items", "receipt_items", "allocations"}
			if unknown:
				raise InvalidArgumentError(
					"Unknown charge options; use the tool's declared eligibility and allocation inputs"
				)
			invoice = self.submitted("Purchase Invoice", name)
			if invoice.update_stock or invoice.is_return or invoice.is_subcontracted:
				raise _Gap(
					"ineligible_vendor_invoice",
					"Select a submitted non-stock service invoice for these costs.",
				)
			basis = charge.get("basis")
			if not isinstance(basis, str) or basis not in {"Amount", "Qty", "Weight", "Manual"}:
				raise _Gap(
					"allocation_policy",
					"Supply Amount, comparable Qty, recorded Weight in one mass unit, or explicit Manual allocations.",
				)
			selected = charge.get("items")
			_list(selected, "charge items", MAX_ROWS)
			selections = {r.name: r.net_amount for r in invoice.items} if selected is None else {}
			for entry in selected or []:
				if not isinstance(entry, dict):
					raise InvalidArgumentError("Charge items must identify invoice row names")
				row_name = _name(entry.get("name"))
				if row_name in selections:
					raise _Gap("duplicate_invoice_row", "An invoice row is selected more than once.")
				row = next((r for r in invoice.items if r.name == row_name), None)
				if not row:
					raise _Gap("invoice_row", "A selected row does not belong to this invoice.")
				selections[row_name] = _number(entry.get("amount", row.net_amount))
			if not selections:
				raise _Gap("no_costs", "Select the attributable pre-tax invoice rows.")
			for row_name in selections:
				if (name, row_name) in seen_rows:
					raise _Gap(
						"duplicate_invoice_row", "The same invoice cost row was allocated more than once."
					)
				seen_rows.add((name, row_name))
			scope = self.scope(charge.get("receipt_items"))
			scratch = self.scratch(scope)
			weight_evidence = self.weight_basis(scratch, scope) if basis == "Weight" else None
			if basis == "Qty":
				units = {self.source_row(key).uom for key in scope}
				if len(units) != 1:
					raise _Gap("incomparable_units", "Quantity allocation needs comparable receipt units.")
			for row in invoice.items:
				if row.name not in selections:
					continue
				if not row.item_code:
					raise _Gap(
						"itemless_service",
						"This service row has no Item classification; reconcile its eligibility in the ERP form.",
					)
				item = self.read("Item", row.item_code)
				amount = _number(selections[row.name])
				rounded = flt(amount, row.precision("net_amount"))
				if rounded != amount:
					self.warnings.append(
						"A selected cost was rounded to the source invoice currency precision."
					)
				amount = rounded
				if item.is_stock_item or item.is_fixed_asset or amount <= 0 or amount > row.net_amount:
					raise _Gap(
						"cost_eligibility",
						"Select positive attributable non-stock costs within the source row's net amount.",
					)
				account = row.expense_account
				if flt(invoice.conversion_rate) <= 0 or not math.isfinite(float(invoice.conversion_rate)):
					raise _Gap("recorded_fx", "The source invoice needs a positive recorded conversion rate.")
				account_currency = self.account_currency(account)
				if account_currency == self.currency:
					amount, rate = amount * invoice.conversion_rate, 1
				elif account_currency == invoice.currency and invoice.conversion_rate:
					rate = invoice.conversion_rate
				else:
					raise _Gap(
						"recorded_fx",
						"The source does not provide a recorded conversion into this expense account's currency.",
					)
				values = {
					"expense_account": account,
					"account_currency": account_currency,
					"exchange_rate": rate,
					"amount": amount,
					"description": f"{invoice.name}: {row.description or row.item_code}",
				}
				values.update(
					{
						f: row.get(f) or invoice.get(f)
						for f in self.dimensions
						if scratch.meta.get_field("taxes")
						and frappe.get_meta(scratch.meta.get_field("taxes").options).has_field(f)
					}
				)
				scratch.append("taxes", values)
			self.initialize_taxes(scratch)
			if scratch.total_taxes_and_charges <= 0:
				raise _Gap(
					"cost_precision", "The selected cost rounds to zero at the account currency precision."
				)
			if basis == "Manual":
				self.manual(scratch, charge.get("allocations"))
			else:
				scratch.distribute_charges_based_on = "Amount" if basis == "Weight" else basis
				scratch.set_applicable_charges_on_item()
				scratch.validate_applicable_charges_for_item()
			for row in scratch.items:
				target = self.items[_key(row)]
				target.applicable_charges = flt(
					target.applicable_charges + row.applicable_charges, target.precision("applicable_charges")
				)
				self.component(row, name, "service", basis)
			if weight_evidence:
				for evidence, row in zip(weight_evidence, scratch.items, strict=True):
					evidence["allocated_amount"] = row.applicable_charges
			for tax in scratch.taxes:
				self.doc.append("taxes", self.tax_values(tax))
				if len(self.doc.taxes) > MAX_ROWS:
					raise _Gap("source_too_large", "Too many charge rows for one preview.")
			claims[name] += scratch.total_taxes_and_charges
			self.charges.append(
				{
					"source": name,
					"kind": "service",
					"basis": basis,
					"amount": scratch.total_taxes_and_charges,
					"receipt_items": [k[2] for k in scope],
					**(
						{"weight_uom": weight_evidence[0]["weight_uom"], "weight_evidence": weight_evidence}
						if weight_evidence
						else {}
					),
				}
			)
		for name, amount in claims.items():
			self.doc.append("vendor_invoices", {"vendor_invoice": name, "amount": amount})

		self.source_tax = gate_lcv(self.doc)

	def weight_basis(self, scratch, scope):
		"""Use the native allocator on exact proportional integers in scratch only."""
		evidence = []
		for key in scope:
			row = self.source_row(key)
			_guard_fields(
				row.meta,
				key[0],
				extra_fields=("weight_uom", "stock_uom", "total_weight", "weight_per_unit", "stock_qty"),
			)
			try:
				weight, per_unit, quantity = (
					_number(row.get(field)) for field in ("total_weight", "weight_per_unit", "stock_qty")
				)
			except InvalidArgumentError as exc:
				raise _Gap(
					"weight_basis",
					"The receipt needs finite recorded item weights.",
					receipt_item=key[2],
					check="finite_weight_and_stock_quantity",
				) from exc
			expected = quantity * per_unit
			if (
				min(weight, per_unit, quantity) <= 0
				or not math.isfinite(expected)
				or not row.weight_uom
				or not row.stock_uom
				or not (
					math.isclose(weight, expected, rel_tol=1e-12, abs_tol=0)
					or weight == flt(expected, row.precision("total_weight"))
				)
			):
				raise _Gap(
					"weight_basis",
					"Use positive, consistent posted receipt weights, or provide Manual allocations. Item defaults and quantities cannot substitute for missing weights.",
					receipt_item=key[2],
					check="positive_consistent_recorded_weight_and_units",
				)
			unit = self.read("UOM", row.weight_uom)
			if not unit.enabled or unit.get("category") != "Mass":
				raise _Gap(
					"weight_basis",
					"Recorded weights need an enabled mass-category UOM.",
					receipt_item=key[2],
					weight_uom=unit.name,
				)
			evidence.append(
				{
					"receipt_item": key[2],
					"item_code": row.item_code,
					"stock_qty": quantity,
					"stock_uom": row.stock_uom,
					"weight_per_unit": per_unit,
					"total_weight": weight,
					"weight_uom": unit.name,
				}
			)
		if len({row["weight_uom"] for row in evidence}) != 1:
			raise _Gap(
				"weight_basis",
				"Weight allocation requires one common mass UOM; provide Manual allocations for mixed units.",
				weight_evidence=evidence[:20],
				total_weight_rows=len(evidence),
			)
		# Currency precision rounds the native basis total. Uniform decimal scaling
		# makes even 0.004/0.004 exact integers, preserving the intended 50/50 split.
		weights = [Decimal(str(row["total_weight"])) for row in evidence]
		digits = max(0, *(-weight.as_tuple().exponent for weight in weights))
		scaled = [int(weight.scaleb(digits)) for weight in weights]
		precision = scratch.items[0].precision("amount")
		if sum(scaled) * 10**precision > 2**53 - 1:
			raise _Gap(
				"weight_basis",
				"The weight precision/range exceeds exact native allocation. Provide Manual allocations.",
			)
		for row, weight in zip(scratch.items, scaled, strict=True):
			row.amount = weight
		return evidence

	def account_currency(self, name):
		account = self.read("Account", name)
		if account.company != self.doc.company or account.is_group or account.account_type == "Stock":
			raise _Gap(
				"expense_account", "Use a non-group, non-Stock source account belonging to this company."
			)
		return account.account_currency or self.currency

	def component(self, row, source, kind, basis=None):
		self.components.setdefault(_key(row), []).append(
			{"source": source, "kind": kind, "basis": basis, "amount": row.applicable_charges}
		)

	def scope(self, names):
		if names is None:
			return list(self.items)
		_list(names, "receipt_items", MAX_ROWS, required=True)
		for name in names:
			_name(name)
		if len(set(names)) != len(names):
			raise _Gap("duplicate_scope", "An allocation row was selected more than once.")
		keys = [k for k in self.items if k[2] in names]
		if len(keys) != len(names):
			raise _Gap("allocation_scope", "An allocation row does not belong to the selected receipts.")
		return keys

	def source_row(self, key):
		return next(r for r in self.receipt_docs[key[:2]].items if r.name == key[2])

	def scratch(self, keys):
		doc = frappe.new_doc(LCV)
		doc.company = self.doc.company
		doc.posting_date = self.doc.posting_date
		doc.distribute_charges_based_on = "Distribute Manually"
		for key in keys:
			values = self.item_values(self.items[key])
			values["applicable_charges"] = 0
			doc.append("items", values)
		return doc

	@staticmethod
	def initialize_taxes(doc):
		from erpnext.controllers.taxes_and_totals import init_landed_taxes_and_totals

		init_landed_taxes_and_totals(doc)
		doc.set_total_taxes_and_charges()

	def reconcile(self, doc):
		self.initialize_taxes(doc)
		for row in doc.items:
			row.applicable_charges = flt(row.applicable_charges, row.precision("applicable_charges"))
		from erpnext.stock.doctype.landed_cost_voucher import landed_cost_voucher as controller

		difference = flt(
			doc.total_taxes_and_charges - sum(r.applicable_charges for r in doc.items),
			doc.items[0].precision("applicable_charges"),
		)
		if difference and hasattr(controller, "absorb_rounding_difference"):
			controller.absorb_rounding_difference(
				doc.get_rows_following_the_basis_total(),
				difference,
				doc.items[0].precision("applicable_charges"),
				doc.total_taxes_and_charges,
			)
			self.warnings.append(f"Native assessment rounding adjustment: {difference} {self.currency}.")
		self.validate_allocations(doc)

	def validate_allocations(self, doc):
		before = [r.applicable_charges for r in doc.items]
		doc.validate_applicable_charges_for_item()
		if before != [r.applicable_charges for r in doc.items]:
			self.warnings.append(
				"The native allocation validator applied a precision adjustment; the returned rows include it."
			)

	def manual(self, doc, allocations):
		_list(allocations, "allocations", MAX_ROWS, required=True)
		by_name = {row.purchase_receipt_item: row for row in doc.items}
		seen = set()
		for allocation in allocations:
			if not isinstance(allocation, dict):
				raise InvalidArgumentError("Manual allocations must identify receipt_item and amount")
			name = _name(allocation.get("receipt_item"))
			if name not in by_name or name in seen:
				raise _Gap(
					"manual_scope", "Manual allocations must identify each scoped receipt row exactly once."
				)
			seen.add(name)
			by_name[name].applicable_charges = _number(allocation.get("amount"))
		if seen != set(by_name):
			raise _Gap("manual_scope", "Supply an explicit allocation for every selected receipt row.")
		self.validate_allocations(doc)

	def saved_draft(self, name):
		doc = self.read(LCV, name)
		if doc.company != self.doc.company or doc.docstatus != 0:
			raise _Gap("draft_state", "Select an existing draft in this company.")
		if not {_key(r) for r in doc.items}.issubset(self.items):
			raise _Gap("draft_scope", "This draft belongs to different receipt lines.")
		for row in doc.vendor_invoices:
			self.submitted("Purchase Invoice", row.vendor_invoice)
		for row in doc.taxes:
			self.account_currency(row.expense_account)
		self.source_tax = gate_lcv(doc)
		self.doc = copy.deepcopy(doc)
		self.charges = [
			{"source": name, "kind": "saved_charge", "description": r.description, "amount": r.base_amount}
			for r in doc.taxes
		]
		self.doc.run_method("validate")
		self.validate_allocations(self.doc)
		return self.result(existing=name)

	def item_values(self, row):
		fields = (*_ITEM_FIELDS, "applicable_charges", *self.dimensions)
		return {f: row.get(f) for f in fields if row.meta.has_field(f) and row.get(f) is not None}

	def tax_values(self, row):
		return {
			f: row.get(f)
			for f in (
				"description",
				"expense_account",
				"amount",
				"account_currency",
				"exchange_rate",
				*self.dimensions,
			)
			if row.meta.has_field(f) and row.get(f) is not None
		}

	def valuation_estimates(self):
		"""Use the native receipt calculation on copies, never simulate downstream stock."""
		estimates = {}
		for key, receipt in self.receipt_docs.items():
			if receipt.get("is_old_subcontracting_flow") or receipt.get("is_internal_supplier"):
				self.warnings.append("This receipt's valuation requires its specialized ERP flow.")
				continue
			working = copy.deepcopy(receipt)
			for row in working.items:
				item_key = (*key, row.name)
				if item_key in self.items:
					row.landed_cost_voucher_amount = (
						flt(row.landed_cost_voucher_amount) + self.items[item_key].applicable_charges
					)
			try:
				working.update_valuation_rate(reset_outgoing_rate=False)
			except (frappe.PermissionError, PermissionDeniedError):
				raise
			except (frappe.ValidationError, AttributeError):
				frappe.clear_messages()
				self.warnings.append(
					"Receipt valuation could not be estimated; allocation reconciliation is unchanged. The posted receipt rate is authoritative."
				)
				continue
			for row in working.items:
				item_key = (*key, row.name)
				if item_key in self.items:
					estimates[item_key] = row.valuation_rate
		return estimates

	def result(self, existing=None):
		doc = self.doc
		items = [self.item_values(row) for row in doc.items]
		self.items = {_key(row): row for row in doc.items}
		estimates = self.valuation_estimates()
		review_items = [
			{
				**values,
				"cost_components": self.components.get(_key(row), []),
				"estimated_receipt_valuation_rate": estimates.get(_key(row)),
			}
			for values, row in zip(items, doc.items, strict=True)
		]
		for row in review_items:
			if row["cost_components"]:
				row["native_rounding_adjustment"] = flt(
					row["applicable_charges"] - sum(c["amount"] for c in row["cost_components"]),
					doc.items[0].precision("applicable_charges"),
				)
		out = {
			"status": "ready",
			"currency": self.currency,
			"sources": self.sources,
			"charges": self.charges,
			"items": review_items,
			"totals": {
				"charges": doc.total_taxes_and_charges,
				"allocated": flt(
					sum(r.applicable_charges for r in doc.items), doc.items[0].precision("applicable_charges")
				),
				"vendor_invoices": doc.total_vendor_invoices_cost,
			},
			"warnings": [
				*self.warnings,
				"Preview does not reserve invoice costs or customs duty; independently confirmed vouchers can compete.",
			],
			"source_tax": self.source_tax,
			"native_checks": ["draft_validation", "item_allocation_reconciliation", "linked_invoice_tax"],
			"submission_checks_pending": [
				"current permissions and source state",
				"closed/frozen periods",
				"asset prerequisites",
				"valuation and downstream reposts",
			],
		}
		if existing:
			out.update({"existing_draft": existing, "suggested_action": "reuse"})
		else:
			out["draft_values"] = {
				"company": doc.company,
				"posting_date": str(doc.posting_date),
				"distribute_charges_based_on": doc.distribute_charges_based_on,
				"purchase_receipts": [
					{"receipt_document_type": r.receipt_document_type, "receipt_document": r.receipt_document}
					for r in doc.purchase_receipts
				],
				"items": items,
				"taxes": [self.tax_values(row) for row in doc.taxes],
				"vendor_invoices": [
					{"vendor_invoice": row.vendor_invoice, "amount": row.amount}
					for row in doc.vendor_invoices
				],
			}
		return out
