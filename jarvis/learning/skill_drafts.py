"""Deterministic skill-draft bullet templates (plan sections 4.3, 6.3).

Wave B owns the PROPOSAL draft text: one plain-English bullet per detected
pattern, rendered from a fixed template keyed by ``skill_template`` id plus
the executor's computed evidence. No LLM, no jinja, no interpolation of raw
free-text values beyond backtick-safe entity names.

Wave C's compiler consumes APPROVED patterns to build the pushed
``learned-<domain>`` skills; it may reword, but every proposal still needs
readable draft text on the review board, which is what this module produces.

Bullet grammar (plan section 6.3):
    - <rule> Evidence: <conf>% of <n_units> <unit-doctype> since <YYYY-MM>.
      [K known exceptions (see board).]

No statistical jargon in the bullet (no lift, no Wilson, no rule-of-three
parentheticals) - those live in the drill-down evidence JSON only.
"""

from __future__ import annotations

# Each entry: {"rule": <imperative default sentence>, "statement": <card sentence>}.
# Placeholders are plain str.format keys populated from skill_bullet_vars.
# Keep both sentences ASCII and free of statistical jargon.
SKILL_TEMPLATES: dict[str, dict[str, str]] = {
	"customer-price-list": {
		"rule": 'Invoice customer "{customer}" on price list "{price_list}" by default.',
		"statement": 'Customer "{customer}" is usually invoiced on price list "{price_list}".',
	},
	"group-payment-terms": {
		"rule": 'Default payment terms for Customer Group "{group}" is "{terms}".',
		"statement": 'Customer Group "{group}" is usually billed on payment terms "{terms}".',
	},
	"quotation-validity": {
		"rule": "Set new quotations to be valid for {days} days by default.",
		"statement": "Quotations are usually valid for {days} days (the system default is {default_days}).",
	},
	"selective-item-pricing": {
		"rule": "This org maintains customer-specific negotiated prices; honour a customer's own price list before the group or org default.",
		"statement": "Customer-specific negotiated prices exist for {n_customers} customers.",
	},
	"tc-letterhead": {
		"rule": 'Use {field_label} "{value}" on selling documents by default.',
		"statement": 'Selling documents usually use {field_label} "{value}" (differs from the company default).',
	},
	"supplier-itemgroup": {
		"rule": 'Supplier "{supplier}" is used mainly for item group "{item_group}"; flag other groups.',
		"statement": 'Supplier "{supplier}" supplies mostly item group "{item_group}".',
	},
	# Tendency band (20/0.90): admits exceptions, so the wording must NOT say
	# "only". The strict "only" variant below is selected by the executor when the
	# evidence clears the rule-of-three bar (n>=60, 0 exceptions).
	"supplier-stockness": {
		"rule": 'Supplier "{supplier}" mostly supplies non-stock items: default is_stock_item to 0 and flag if a stock item appears.',
		"statement": 'Supplier "{supplier}" usually supplies non-stock items.',
	},
	"supplier-stockness-only": {
		"rule": 'Supplier "{supplier}" supplies only non-stock items: default is_stock_item to 0 and flag if any stock item appears.',
		"statement": 'Supplier "{supplier}" supplies only non-stock items.',
	},
	"supplier-tax-template": {
		"rule": 'Apply purchase tax template "{tax_template}" for supplier "{supplier}" by default.',
		"statement": 'Supplier "{supplier}" usually uses purchase tax template "{tax_template}".',
	},
	"pi-update-stock": {
		"rule": "Purchase invoices are usually booked with Update Stock enabled; confirm before disabling it.",
		"statement": "Purchase invoices are usually booked with Update Stock enabled.",
	},
	"itemgroup-warehouse": {
		"rule": 'Ship item group "{item_group}" from warehouse "{warehouse}" by default.',
		"statement": 'Item group "{item_group}" usually ships from warehouse "{warehouse}".',
	},
	"stock-entry-route": {
		"rule": "{purpose} stock entries usually run the route {route}.",
		"statement": "{purpose} stock entries usually move stock {route}.",
	},
	"mode-of-payment": {
		"rule": '{payment_type} entries usually use mode of payment "{mode}".',
		"statement": '{payment_type} entries usually use mode of payment "{mode}".',
	},
	"billing-method": {
		"rule": 'Projects of type "{project_type}" are usually billed {billing_method}.',
		"statement": 'Projects of type "{project_type}" are usually billed {billing_method}.',
	},
	"naming-series": {
		"rule": 'New {doctype} documents use naming series "{series}".',
		"statement": 'New {doctype} documents use naming series "{series}".',
	},
	"default-vs-usage": {
		"rule": 'Documents usually use {field_label} "{used}" though the configured default is "{configured}".',
		"statement": 'The configured {field_label} default is "{configured}", but documents usually use "{used}".',
	},
	# Phase 2 (sell-customer-print-format, S4). The tendency variant is picked
	# by the detector postprocess below 30 effective print events (plan
	# section 4.2): enough signal to propose, not enough to say "usually".
	"customer-print-format": {
		"rule": 'Print documents for customer "{customer}" with print format "{print_format}" by default.',
		"statement": 'Customer "{customer}" documents are usually printed with format "{print_format}".',
	},
	"customer-print-format-tendency": {
		"rule": 'Prefer print format "{print_format}" when printing documents for customer "{customer}".',
		"statement": 'Customer "{customer}" documents tend to be printed with format "{print_format}".',
	},
	# --- Tier-2 pack (plan sections 4.2 marked rows, 4.4) ---------------------
	# The geography warning is part of the template text on purpose: compiled
	# skill text must ALWAYS carry it for this detector (plan section 4.2).
	"party-tax-template": {
		"rule": 'Apply {tax_kind} tax template "{tax_template}" when a {party_doctype} has {field_label} "{segment_value}". Verify the geography caveat first: this may be state-driven tax routing, not a segment habit.',
		"statement": '{party_doctype}s with {field_label} "{segment_value}" usually get {tax_kind} tax template "{tax_template}". Caveat: may reflect party geography (state), not a segment habit; review before approval.',
	},
	"customer-payment-terms": {
		"rule": 'Bill customer "{customer}" on payment terms "{terms}" by default ({master_clause}).',
		"statement": 'Customer "{customer}" is usually billed on payment terms "{terms}" ({master_clause}).',
	},
	"supplier-payment-terms": {
		"rule": 'Book invoices from supplier "{supplier}" on payment terms "{terms}" by default ({master_clause}).',
		"statement": 'Supplier "{supplier}" is usually billed on payment terms "{terms}" ({master_clause}).',
	},
	"itemgroup-warehouse-dimensioned": {
		"rule": 'For cost center "{cost_center}", ship item group "{item_group}" from warehouse "{warehouse}" by default.',
		"statement": 'Item group "{item_group}" under cost center "{cost_center}" usually ships from warehouse "{warehouse}".',
	},
	"uom-conversion": {
		"rule": 'Items in group "{item_group}" usually transact in UOM "{uom}" rather than their stock UOM; default the line UOM to "{uom}".',
		"statement": 'Item group "{item_group}" usually transacts in UOM "{uom}", not the stock UOM.',
	},
	"batch-serial-usage": {
		"rule": 'Items in group "{item_group}" are {tracking} in practice; set the matching tracking flags when creating items in this group.',
		"statement": 'Items in group "{item_group}" are usually {tracking}.',
	},
	"cost-center-dimension": {
		"rule": 'Book lines from warehouse "{warehouse}" to cost center "{cost_center}" by default.',
		"statement": 'Lines from warehouse "{warehouse}" usually book to cost center "{cost_center}".',
	},
	"je-from-template": {
		"rule": '{voucher_type} journal entries are usually created from template "{je_template}"; start from it.',
		"statement": '{voucher_type} journal entries are usually created from template "{je_template}".',
	},
	"je-voucher-type": {
		"rule": 'Journal entries here are usually "{voucher_type}" entries; default the voucher type accordingly.',
		"statement": 'Journal entries are usually "{voucher_type}" entries.',
	},
	"deferred-usage": {
		"rule": 'Items in group "{item_group}" usually book deferred {kind}; enable deferred {kind} when invoicing this group.',
		"statement": 'Item group "{item_group}" usually books deferred {kind}.',
	},
	"default-bom-usage": {
		"rule": 'Use BOM "{bom}" for work orders on item "{item}" by default ({master_clause}).',
		"statement": 'Work Orders for item "{item}" usually use BOM "{bom}" ({master_clause}).',
	},
	"timesheet-rate-defaults": {
		"rule": 'Bill activity "{activity_type}" at rate {rate} on timesheets by default ({master_clause}).',
		"statement": 'Timesheet lines for activity "{activity_type}" are usually billed at rate {rate} ({master_clause}).',
	},
	"custom-field-always-filled": {
		"rule": 'Fill the custom field "{fieldname}" ({label}) when drafting {doctype} documents; it is filled on nearly every submitted one despite not being mandatory.',
		"statement": 'Custom field "{fieldname}" on {doctype} is filled on nearly every submitted document (mandatory in practice).',
	},
	"role-doctype-routing": {
		"rule": '{doctype} documents are usually created by users holding the "{role}" role; route {doctype} work to them.',
		"statement": '{doctype} documents are usually created by the "{role}" role.',
	},
}


# Tendency templates that have a strict "only/always" sibling. The executor
# (``_effective_template``) swaps to the strict variant when a candidate's
# evidence supports an always/only claim (n>=60, 0 exceptions); keeping both
# entries side by side keeps the wording contract in one place.
STRICT_TEMPLATE_VARIANTS: dict[str, str] = {
	"supplier-stockness": "supplier-stockness-only",
}


def _safe_format(template: str, variables: dict) -> str:
	"""str.format that never raises on a missing key (renders the raw
	placeholder instead) and stringifies values. Deterministic; no eval."""

	class _Default(dict):
		def __missing__(self, key):
			return "{" + key + "}"

	safe_vars = {k: ("" if v is None else str(v)) for k, v in (variables or {}).items()}
	try:
		return template.format_map(_Default(safe_vars))
	except Exception:
		return template


def render_rule(template_id: str, variables: dict) -> str:
	"""The imperative default sentence for the skill bullet."""
	tmpl = SKILL_TEMPLATES.get(template_id, {})
	return _safe_format(tmpl.get("rule", ""), variables)


def render_statement(template_id: str, variables: dict) -> str:
	"""The plain-English card sentence (pattern_statement)."""
	tmpl = SKILL_TEMPLATES.get(template_id, {})
	return _safe_format(tmpl.get("statement", ""), variables)


def render_skill_draft(rule: str, meta: dict) -> str:
	"""Assemble one review-board bullet from a rule sentence + evidence meta.

	``meta`` keys: conf_pct (float), n_units (int), unit_doctype (str),
	since_date (str YYYY-MM), exception_n (int). The JLP ref is appended by
	the engine/compiler after the row is named, not here.
	"""
	conf = meta.get("conf_pct")
	conf_str = f"{conf:g}" if isinstance(conf, (int, float)) else str(conf)
	n_units = meta.get("n_units", 0)
	unit_doctype = meta.get("unit_doctype", "documents")
	since = meta.get("since_date", "")
	exc_n = int(meta.get("exception_n") or 0)
	exc_clause = f" {exc_n} known exceptions (see board)." if exc_n else ""
	rule = (rule or "").strip()
	sep = "" if rule.endswith((".", "!", "?")) else "."
	return f"- {rule}{sep} Evidence: {conf_str}% of {n_units} {unit_doctype} since {since}.{exc_clause}"
