"""Tax readiness for linked LCV service invoices, shared by previews and writes.

This deliberately stops unsupported nonrecoverable tax; it does not capitalize it.
Charge rows retain invoice provenance, but not invoice-item provenance. Checks therefore cover the
whole linked invoice, including partial selections. Unlinked/manual costs and
native Desk operations are outside this contract.
"""

import copy
import math
from collections import defaultdict

import frappe
from frappe.utils import flt

from jarvis.exceptions import PermissionDeniedError
from jarvis.lcv import SOURCE_INVOICE, TAX_DOCTYPE, source_link_available

_DENIED = "Cannot read the source records and fields required to prepare landed costs."
MAX_SOURCES = 20
MAX_ROWS = 500
GST_TYPES = ("cgst", "sgst", "igst", "cess", "cess_non_advol")


def _guard_fields(meta, parenttype=None, dimensions=(), extra_fields=()):
	"""Native helpers read raw rows: fence amounts, ratios and dimensions first."""
	if frappe.session.user == "Administrator":
		return
	access = set(meta.get_permlevel_access("read", parenttype=parenttype))
	unreadable = {df.fieldname for df in meta.fields if df.permlevel and df.permlevel not in access}
	mask_reader = getattr(meta, "get_masked_fields", None)
	masked = {df.fieldname for df in mask_reader(parenttype=parenttype)} if mask_reader else set()
	master_fields = {
		"Company": {
			"default_currency",
			"cost_center",
			"default_gst_expense_account",
			"country",
			"gst_category",
			"enable_perpetual_inventory",
		},
		"Item": {"is_stock_item", "is_fixed_asset", "stock_uom"},
		"Account": {"company", "is_group", "account_type", "account_currency", "root_type"},
		"UOM": {"category", "enabled"},
	}
	required = master_fields.get(meta.name)
	if required is not None:
		required = {f for f in required if meta.has_field(f)}
	else:
		required = {
			df.fieldname
			for df in meta.fields
			if df.fieldtype in {"Currency", "Float", "Percent", "Int"}
			or df.fieldname
			in {
				"items",
				"taxes",
				"vendor_invoices",
				"purchase_receipts",
				"company",
				"cost_center",
				"is_ineligible_for_itc",
				"is_stock_item",
				"is_fixed_asset",
				"update_stock",
				"is_return",
				"is_subcontracted",
			}
		}
		required.update(
			f
			for f in (
				"item_code",
				"description",
				"expense_account",
				"account_currency",
				"currency",
				"receipt_document_type",
				"receipt_document",
				"purchase_receipt_item",
				"stock_entry_item",
				"purchase_invoice",
				"purchase_invoice_item",
				"purchase_receipt",
				"pr_detail",
				"pi_detail",
				*dimensions,
			)
			if meta.has_field(f)
		)
	required.update(f for f in extra_fields if meta.has_field(f))
	if required & (unreadable | masked):
		raise PermissionDeniedError(_DENIED)


class LCVSourceTaxNeedsInput(frappe.ValidationError):
	def __init__(self, issues, invoices=()):
		self.assessment = {
			"status": "needs_input",
			"scope": "linked_vendor_invoices",
			"issues": issues,
			"invoices": list(invoices),
		}
		super().__init__("LCV source tax needs input: " + "; ".join(i["message"] for i in issues))


def _issue(name, code, message, **values):
	return {"code": code, "purchase_invoice": name, "message": f"{name}: {message}", **values}


def _read(doctype, name):
	if not isinstance(name, str) or not name.strip() or len(name) > 140:
		raise PermissionDeniedError(_DENIED)
	try:
		doc = frappe.get_doc(doctype, name)
		if not frappe.has_permission(doctype, "read", doc=doc):
			raise PermissionDeniedError(_DENIED)
	except (frappe.DoesNotExistError, frappe.PermissionError) as exc:
		frappe.clear_messages()
		raise PermissionDeniedError(_DENIED) from exc
	return doc


def _amount(value):
	value = float(value or 0)
	if not math.isfinite(value):
		raise ValueError("Non-finite source amount")
	return value


def _tds_accounts(pi):
	"""Only an explicit withholding category establishes a TDS deduction."""
	categories = set()
	if pi.get("apply_tds") and pi.get("tax_withholding_category"):
		categories.add(pi.tax_withholding_category)
	# ERPNext 16 stores the applicable categories on the invoice items.
	for item in pi.items:
		if item.get("apply_tds") and item.get("tax_withholding_category"):
			categories.add(item.tax_withholding_category)
	if len(categories) > MAX_SOURCES:
		raise LCVSourceTaxNeedsInput(
			[_issue(pi.name, "source_too_large", "Narrow the withholding categories before reconciling tax.")]
		)
	accounts = set()
	for name in sorted(categories):
		category = _read("Tax Withholding Category", name)
		_guard_fields(category.meta, extra_fields=("accounts",))
		if len(category.accounts) > MAX_ROWS:
			raise LCVSourceTaxNeedsInput(
				[_issue(pi.name, "source_too_large", "Narrow the withholding account mapping.")]
			)
		for row in category.accounts:
			_guard_fields(row.meta, category.doctype, extra_fields=("company", "account"))
			if row.company != pi.company:
				continue
			account = _read("Account", row.account)
			_guard_fields(account.meta)
			if account.company == pi.company and account.root_type == "Liability" and not account.is_group:
				accounts.add(account.name)
	return accounts


def _posted_input_matches(pi, expected, precision):
	"""Do not let a permission-filtered subset prove a complete input-tax balance."""
	meta = frappe.get_meta("GL Entry")
	_guard_fields(meta, extra_fields=("account", "voucher_type", "voucher_no", "is_cancelled"))
	rows = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Purchase Invoice", "voucher_no": pi.name, "is_cancelled": 0},
		fields=["*"],
		limit_page_length=MAX_ROWS + 1,
	)
	if len(rows) > MAX_ROWS:
		return False
	actual = defaultdict(float)
	for values in rows:
		row = frappe.get_doc({"doctype": "GL Entry", **values})
		if not frappe.has_permission("GL Entry", "read", doc=row):
			raise PermissionDeniedError(_DENIED)
		if row.account in expected:
			actual[row.account] += _amount(row.debit) - _amount(row.credit)
	return all(
		flt(actual[account], precision) == flt(amount, precision) for account, amount in expected.items()
	)


def _classify(pi):
	_guard_fields(
		pi.meta,
		extra_fields=(
			"ineligibility_reason",
			"place_of_supply",
			"company_gstin",
			"is_opening",
			"is_reverse_charge",
			"apply_tds",
			"tax_withholding_category",
		),
	)
	for field in ("items", "taxes"):
		if len(pi.get(field) or []) > MAX_ROWS:
			return _issue(pi.name, "source_too_large", "Narrow the source invoice before reconciling tax.")
		if pi.get(field):
			_guard_fields(
				pi.get(field)[0].meta,
				pi.doctype,
				extra_fields=(
					"gst_tax_type",
					"account_head",
					"add_deduct_tax",
					"category",
					"apply_tds",
					"tax_withholding_category",
				),
			)
	precision = pi.precision("base_total")
	recorded_tax = sum(
		_amount(r.base_tax_amount_after_discount_amount) * (-1 if r.add_deduct_tax == "Deduct" else 1)
		for r in pi.taxes
		if r.category in {"Total", "Valuation and Total"}
	)
	if flt(recorded_tax, precision) != flt(_amount(pi.base_total_taxes_and_charges), precision):
		return _issue(
			pi.name,
			"tax_split_inconsistent",
			"Invoice tax totals do not reconcile with the recorded tax rows.",
		)
	material = [r for r in pi.taxes if flt(_amount(r.base_tax_amount_after_discount_amount), precision)]
	item_gst = any(_amount(r.get(f"{kind}_amount")) for r in pi.items for kind in GST_TYPES)
	if material and any(r.add_deduct_tax == "Deduct" for r in material):
		tds = _tds_accounts(pi)
		material = [
			r
			for r in material
			if not (r.add_deduct_tax == "Deduct" and r.account_head in tds and not r.get("gst_tax_type"))
		]
	if not material and not item_gst:
		return {"purchase_invoice": pi.name, "treatment": "no_tax"}

	def unresolved(code, message, **values):
		return _issue(pi.name, code, message, **values)

	if "india_compliance" not in frappe.get_installed_apps():
		return unresolved(
			"tax_classifier_unavailable",
			"Source tax treatment is unclassified. Reconcile it in the ERP form.",
		)
	try:
		from india_compliance.gst_india.constants import GST_TAX_TYPES
		from india_compliance.gst_india.overrides.ineligible_itc import PurchaseInvoice
		from india_compliance.gst_india.overrides.purchase_invoice import set_ineligibility_reason
	except ImportError:
		return unresolved("tax_classifier_unavailable", "The installed tax classifier is unavailable.")
	if not callable(getattr(PurchaseInvoice, "update_item_ineligibility", None)) or set(GST_TAX_TYPES) != set(
		GST_TYPES
	):
		return unresolved("tax_classifier_unavailable", "The installed tax classifier is unsupported.")
	if any(not pi.meta.has_field(f) or not pi.get(f) for f in ("company_gstin", "place_of_supply")):
		return unresolved("tax_identity_unverified", "Source GST identity or place of supply is missing.")
	if not pi.meta.has_field("ineligibility_reason") or any(
		not r.meta.has_field(f"{kind}_amount") for r in pi.items for kind in GST_TYPES
	):
		return unresolved("tax_classifier_unavailable", "Source tax detail is incomplete.")
	if any(
		r.add_deduct_tax != "Add" or r.get("gst_tax_type") not in GST_TYPES or r.category != "Total"
		for r in material
	):
		return unresolved(
			"source_tax_unsupported", "Additional source charges or deductions need separate reconciliation."
		)
	if pi.get("is_reverse_charge") or pi.get("is_opening") == "Yes":
		return unresolved(
			"source_tax_unsupported", "Reverse-charge or opening tax treatment needs separate reconciliation."
		)
	company = _read("Company", pi.company)
	_guard_fields(company.meta)
	for name in {r.item_code for r in pi.items if r.item_code}:
		_guard_fields(_read("Item", name).meta)
	working = copy.deepcopy(pi)
	set_ineligibility_reason(working, show_alert=False)
	if (working.ineligibility_reason or "") != (pi.ineligibility_reason or ""):
		return unresolved(
			"tax_treatment_changed",
			"Current GST facts differ from the recorded treatment. Reconcile the invoice first.",
		)
	PurchaseInvoice(working).update_item_ineligibility()
	ineligible = sum(_amount(r.get("_ineligible_tax_amount")) for r in working.items)
	if ineligible:
		return unresolved(
			"nonrecoverable_gst_unsupported",
			"The invoice records nonrecoverable GST. Jarvis cannot reconcile this tax with the selected costs. Review the invoice and LCV in the ERP form. Do not increase the vendor claim.",
			ineligible_gst_amount=flt(ineligible, precision),
			amount_scope="invoice",
			currency=company.default_currency,
		)
	expected, by_kind = defaultdict(float), defaultdict(float)
	for row in material:
		amount = _amount(row.base_tax_amount_after_discount_amount)
		if amount < 0:
			return unresolved("tax_split_inconsistent", "Source tax amounts need reconciliation.")
		account = _read("Account", row.account_head)
		_guard_fields(account.meta)
		if (
			account.company != pi.company
			or account.root_type not in {"Asset", "Liability"}
			or account.account_type != "Tax"
		):
			return unresolved(
				"posted_treatment_unverified",
				"The recorded GST account is not a supported input-tax account.",
			)
		expected[row.account_head] += amount
		by_kind[row.gst_tax_type] += amount
	for kind in GST_TYPES:
		total = sum(_amount(r.get(f"{kind}_amount")) for r in pi.items)
		if flt(total, precision) != flt(by_kind[kind], precision):
			return unresolved("tax_split_inconsistent", "Item GST and invoice GST do not reconcile.")
	if not expected:
		return unresolved(
			"tax_split_inconsistent", "The source does not have a complete GST account mapping."
		)
	try:
		matches = _posted_input_matches(pi, expected, precision)
	except PermissionDeniedError:
		return unresolved(
			"posted_treatment_unreadable",
			"Cannot verify the recorded input-tax balance with the current read permissions.",
		)
	if not matches:
		return unresolved(
			"posted_treatment_unverified",
			"The recorded input-tax balance differs from the source GST. Reconcile the invoice first.",
		)
	return {
		"purchase_invoice": pi.name,
		"treatment": "excluded_per_recorded_treatment",
		"gst_amount": flt(sum(expected.values()), precision),
		"currency": company.default_currency,
		"amount_scope": "invoice",
	}


def source_names(doc):
	"""Read persisted source links only after the child fields are permission-fenced."""
	_guard_fields(frappe.get_meta(TAX_DOCTYPE), doc.doctype, extra_fields=(SOURCE_INVOICE,))
	names = [row.get(SOURCE_INVOICE) for row in doc.get("taxes") or [] if row.get(SOURCE_INVOICE)]
	if any(not isinstance(n, str) or not n.strip() or len(n) > 140 for n in names):
		raise PermissionDeniedError(_DENIED)
	return sorted(set(names))


def _prior_claims(invoice, voucher, lock):
	# A locking read sees a competing commit even if this transaction already
	# established an older REPEATABLE READ snapshot before waiting for the PI.
	rows = frappe.db.sql(
		f"""select t.expense_account, t.base_amount
		from `tabLanded Cost Taxes and Charges` t
		inner join `tabLanded Cost Voucher` p on p.name = t.parent
		where t.parenttype = 'Landed Cost Voucher' and t.parentfield = 'taxes'
		and p.docstatus = 1 and p.name != %(voucher)s
		and t.`{SOURCE_INVOICE}` = %(invoice)s
		limit 2001 {"for update" if lock else ""}""",
		{"voucher": voucher or "", "invoice": invoice},
		as_dict=True,
	)
	if len(rows) > 2000:
		return None
	amounts = defaultdict(float)
	for row in rows:
		amount = _amount(row.base_amount)
		if amount < 0:
			return None
		amounts[row.expense_account] += amount
	return amounts


def _check_claim(doc, pi, lock):
	"""Tie every linked cost to a service account and the source's recorded FX."""
	company = _read("Company", pi.company)
	_guard_fields(company.meta)
	eligible = defaultdict(float)
	for item in pi.items:
		if not item.item_code:
			continue
		master = _read("Item", item.item_code)
		_guard_fields(master.meta)
		if not master.is_stock_item and not master.is_fixed_asset:
			eligible[item.expense_account] += _amount(item.base_net_amount)
	working = copy.deepcopy(doc)
	for row in working.taxes:
		if row.get(SOURCE_INVOICE) != pi.name:
			continue
		account = _read("Account", row.expense_account)
		_guard_fields(account.meta)
		currency = account.account_currency or company.default_currency
		rate = 1 if currency == company.default_currency else _amount(pi.conversion_rate)
		if (
			account.company != pi.company
			or account.is_group
			or row.expense_account not in eligible
			or currency not in {company.default_currency, pi.currency}
			or (row.account_currency and row.account_currency != currency)
			or (currency != company.default_currency and not row.exchange_rate)
			or (row.exchange_rate and flt(_amount(row.exchange_rate), 9) != flt(rate, 9))
			or rate <= 0
			or _amount(row.amount) <= 0
			or any(row.get(field) for field in ("has_corrective_cost", "has_operating_cost", "operation_id"))
		):
			return _issue(
				pi.name,
				"linked_charge_unsupported",
				"Keep linked charges on the source service account and recorded currency conversion.",
			)
		row.account_currency, row.exchange_rate = currency, rate
	from erpnext.controllers.taxes_and_totals import init_landed_taxes_and_totals

	# Initialize only linked rows: unrelated manual costs retain their native flow.
	working.set("taxes", [r for r in working.taxes if r.get(SOURCE_INVOICE) == pi.name])
	init_landed_taxes_and_totals(working)
	claims = defaultdict(float)
	for row in working.taxes:
		claims[row.expense_account] += _amount(row.base_amount)
	prior = _prior_claims(pi.name, doc.name, lock)
	if prior is None:
		return _issue(
			pi.name,
			"prior_claims_unresolved",
			"Review existing landed-cost claims in the ERP form before continuing.",
		)
	precision = pi.precision("base_net_total")
	if flt(sum(claims.values()) + sum(prior.values()), precision) > flt(pi.base_net_total, precision) or any(
		flt(claims.get(account, 0) + prior.get(account, 0), precision)
		> flt(eligible.get(account, 0), precision)
		for account in set(claims) | set(prior)
	):
		return _issue(
			pi.name,
			"source_net_claim_exceeded",
			"The source service cost is already allocated or this claim exceeds its recorded net cost. Review the source and LCV in the ERP form.",
		)
	return None


def gate_lcv(doc, *, lock=False, previous_links=None, previous_rows=None):
	"""Check linked v15 sources; actual writes retain sorted PI locks until commit."""
	if doc.doctype != "Landed Cost Voucher":
		return None
	if doc.meta.has_field("vendor_invoices"):
		raise LCVSourceTaxNeedsInput(
			[
				{
					"code": "capability_unavailable",
					"message": "Use the matching Jarvis version for this ERPNext landed-cost schema before continuing.",
				}
			]
		)
	_guard_fields(doc.meta)
	names = source_names(doc)
	if len(doc.get("taxes") or []) > MAX_ROWS or len(names) > MAX_SOURCES:
		raise LCVSourceTaxNeedsInput(
			[{"code": "source_too_large", "message": "Use at most 20 source invoices and 500 cost rows."}]
		)
	if (names or previous_links) and not source_link_available():
		raise LCVSourceTaxNeedsInput(
			[
				{
					"code": "source_link_unavailable",
					"message": "Source service invoice links need setup. Ask your ERP administrator to run the Jarvis migration and check the Source Service Invoice field.",
				}
			]
		)
	if previous_links and previous_links - set(names) and doc.get("taxes"):
		raise LCVSourceTaxNeedsInput(
			[
				{
					"code": "vendor_link_removed",
					"message": "Keep existing service invoice links while charges remain. Reconcile source removal in the ERP form.",
				}
			]
		)
	if previous_rows and doc.get("taxes"):
		current = {row.name: row.get(SOURCE_INVOICE) for row in doc.taxes}
		if any(current.get(name) != invoice for name, invoice in previous_rows.items()):
			raise LCVSourceTaxNeedsInput(
				[
					{
						"code": "vendor_link_removed",
						"message": "Keep each existing service invoice link while charges remain. Reconcile cost row removal in the ERP form.",
					}
				]
			)
	locked, initial = {}, {}
	if lock and names:
		initial = {name: str(_read("Purchase Invoice", name).modified) for name in names}
		rows = frappe.db.sql(
			"select name, modified, docstatus, company from `tabPurchase Invoice` where name in %s order by name for update",
			(names,),
			as_dict=True,
		)
		locked = {row.name: row for row in rows}
	issues, invoices = [], []
	for name in names:
		pi = _read("Purchase Invoice", name)
		_guard_fields(pi.meta, extra_fields=("ineligibility_reason", "place_of_supply", "company_gstin"))
		if lock and (
			name not in locked
			or str(locked[name].modified) != str(pi.modified)
			or str(locked[name].modified) != initial[name]
		):
			issues.append(
				_issue(
					name,
					"source_changed",
					"The source changed during this transaction. Reload and review it again.",
				)
			)
			continue
		if (
			pi.docstatus != 1
			or pi.company != doc.company
			or pi.update_stock
			or pi.is_return
			or pi.is_subcontracted
		):
			issues.append(
				_issue(name, "source_state", "Use a submitted non-stock service invoice in this company.")
			)
			continue
		try:
			result = _classify(pi)
			if not result.get("code"):
				claim_issue = _check_claim(doc, pi, lock)
				if claim_issue:
					result = claim_issue
		except (ValueError, TypeError, AttributeError) as exc:
			frappe.logger("lcv_source_tax").warning(
				"Source tax classifier unavailable (%s)", type(exc).__name__
			)
			result = _issue(
				name, "tax_classifier_unavailable", "The installed source tax detail cannot be reconciled."
			)
		if result.get("code"):
			issues.append(result)
		else:
			invoices.append(result)
	if issues:
		raise LCVSourceTaxNeedsInput(issues, invoices)
	return {
		"status": "ready" if names else "not_applicable",
		"scope": "linked_vendor_invoices",
		"invoices": invoices,
	}
