"""Mini-org fixture factory for pattern-learning detector tests (plan section 11).

Fabricates a two-company org with PLANTED patterns AND planted TRAPS, then lets
the detectors run against it. Rows are written with low-level ``db_insert`` (no
ERPNext controllers / no submit validation) directly into the tab tables the
detectors read; the detectors' SQL reads those same columns, so the fixtures do
not need to be fully valid ERPNext documents. Every row is named ``_JPL-*`` so
:func:`wipe` is a single idempotent ``LIKE`` sweep (reused by the E2E harness).

Planted patterns (company _JPL-Alpha):
  * customer DealerD -> price list "Dealer Pricing" (sell-customer-price-list)
  * Customer Group "Dealer" -> "30 Days" terms 58/60 (sell-group-payment-terms)
  * supplier AcmeNonStock -> non-stock only 40/40 (buy-supplier-stockness)
  * supplier BoltSupply -> item group "Fasteners" (buy-supplier-itemgroup)
  * supplier TaxHabit -> tax template "Alpha GST 18%" 24/25 (buy-supplier-tax-template)
  * quotations valid 15 days vs +1-month default (sell-quotation-validity)
  * habitual letter head vs company default (sell-tc-letterhead)
  * customer-specific Item Prices for 12 customers (sell-selective-item-pricing)
  * Purchase Invoices booked with update_stock (buy-pi-update-stock)
  * item group -> warehouse, 2 balanced warehouses (stock-itemgroup-warehouse)
  * stock-entry purpose -> route (stock-entry-purpose-mix)
  * payment direction -> mode of payment (acct-mode-of-payment)
  * project type "External" -> timesheet-billed (proj-billing-method)

Planted traps:
  * child-row inflation: supplier InflateCo, 6 POs x 10 lines (unit counting)
  * go-live burst: company Beta PIs, all one second (spread gate)
  * sole-value: company Beta single price list / single term (variance gate)
  * vanilla-default: company Beta quotations valid +1 month (S5 system-default gate)
  * multi-plant: company Beta ships from 5 warehouses (single-plant guard)

Tier-2 planted patterns (plan section 4.2 marked rows):
  * customer DealerD realized "30 Days" vs Customer master "45 Days"; DealerE
    has no master (sell-customer-payment-terms) - RetailR's master MATCHES its
    realized terms, the suppression trap
  * supplier BoltSupply realized "Net 15" vs master "Net 45"
    (buy-supplier-payment-terms-realized) - AcmeNonStock's master matches (trap)
  * Alpha SI Electronics lines transact in UOM "Box" vs stock UOM "Nos"
    (stock-uom-conversion) - Furniture lines transact in the stock UOM (trap)
  * 35 Alpha Bank-Entry JEs, 32 from template "_JPL-Monthly Accrual"
    (acct-je-usage) + 4 plain Journal Entry ballast
  * Alpha Work Orders: item Widget usually uses a NON-default BOM
    (mfg-default-bom-usage) - item Elec uses its flagged default (trap)
  * Alpha Timesheets: activity Consulting billed 1500 with no configured rate
    (proj-timesheet-rate-defaults) - Support's Activity Type master matches (trap)
  * Alpha Purchase Invoice items: group Services books deferred expense
    (acct-deferred-usage expense side) - Fasteners immediate (ballast)
  * company Gamma (MULTI-PLANT, 5 warehouses): (item_group, cost_center)
    pairs cluster to warehouses (stock-itemgroup-warehouse-dimensioned +
    acct-cost-center-dimension); Electronics lines book deferred revenue
  * company Gamma tax-segment trap: a Custom Field row marks Customer.territory
    as a discovered segment; North/South segments map to GST 18%/12% BUT the
    party states (Maharashtra/Karnataka) predict the template just as well -
    the geography confound that MUST demote; an active Tax Rule already
    encodes the 12% template (cross-ref suppression trap)
"""

from __future__ import annotations

import frappe

ALPHA = "_JPL-Alpha Ltd"
BETA = "_JPL-Beta Ltd"
GAMMA = "_JPL-Gamma Ltd"
PREFIX = "_JPL-"
LETTER_HEAD = "_JPL-Alpha Header"

# Doctypes wiped by the _JPL- name sweep (children before parents is harmless
# without FKs, but we list children first anyway).
_WIPE_DOCTYPES = (
	"Sales Invoice Item",
	"Purchase Order Item",
	"Purchase Invoice Item",
	"Stock Entry Detail",
	"Timesheet Detail",
	"Dynamic Link",
	"Sales Invoice",
	"Purchase Order",
	"Purchase Invoice",
	"Payment Entry",
	"Quotation",
	"Stock Entry",
	"Journal Entry",
	"Work Order",
	"BOM",
	"Timesheet",
	"Project",
	"Item Price",
	"Item",
	"Address",
	"Customer",
	"Supplier",
	"Activity Type",
	"Tax Rule",
	"Custom Field",
	"Company",
)

# What the tests assert. company-scoped detectors are isolated by the company
# filter; org-wide ones (naming-series, default-vs-usage) are unit-tested with a
# fake facade instead, so they are not listed here.
EXPECT = {
	"companies": {"alpha": ALPHA, "beta": BETA},
	"dealer_customer": "_JPL-DealerD",
	"dealer_price_list": "Dealer Pricing",
	"dealer_group": "Dealer",
	"dealer_terms": "30 Days",
	"acme_supplier": "_JPL-AcmeNonStock",
	"bolt_supplier": "_JPL-BoltSupply",
	"bolt_item_group": "Fasteners",
	"taxhabit_supplier": "_JPL-TaxHabit",
	"taxhabit_template": "Alpha GST 18%",
	"inflate_supplier": "_JPL-InflateCo",
	"quotation_days": 15,
	"selective_customers": 12,
	# Tier-2 expectations
	"dealer_master_terms": "45 Days",
	"bolt_realized_terms": "Net 15",
	"bolt_master_terms": "Net 45",
	"je_template": "_JPL-Monthly Accrual",
	"widget_realized_bom": "_JPL-BOM-ALT",
	"widget_default_bom": "_JPL-BOM-DEF",
	"consulting_rate": 1500.0,
	"north_tax_template": "Alpha GST 18%",
	"south_tax_template": "Alpha GST 12%",
}


# ---------------------------------------------------------------------------
# low-level insert
# ---------------------------------------------------------------------------
def _insert(doctype, name, fields, docstatus=1, creation=None, children=None):
	doc = frappe.new_doc(doctype)
	doc.update(fields)
	doc.name = name
	doc.flags.name_set = True
	doc.docstatus = docstatus
	doc.db_insert()
	ts = creation or (
		str(fields.get("posting_date") or fields.get("transaction_date") or "2025-09-01") + " 10:00:00"
	)
	frappe.db.set_value(doctype, name, {"creation": ts, "modified": ts}, update_modified=False)
	for childfield, (child_dt, rows) in (children or {}).items():
		for i, row in enumerate(rows):
			cname = f"{name}-{childfield}-{i + 1}"
			cd = frappe.new_doc(child_dt)
			cd.update(row)
			cd.parent = name
			cd.parenttype = doctype
			cd.parentfield = childfield
			cd.idx = i + 1
			cd.name = cname
			cd.flags.name_set = True
			cd.docstatus = docstatus
			cd.db_insert()
			frappe.db.set_value(child_dt, cname, {"creation": ts, "modified": ts}, update_modified=False)
	return name


def _day(base_ordinal: int) -> str:
	"""A distinct calendar day per index, inside the 18-month window (today is
	well after 2025-09). Distinct days => spread gate passes and consecutive
	rows never collapse as a creation burst."""
	return frappe.utils.add_days("2025-09-01", base_ordinal)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def build(commit: bool = False) -> None:
	"""Idempotent: wipe any prior fixtures, then seed the mini-org. Detectors
	can then run in the same transaction (tests, no commit) or the caller can
	commit for the E2E harness."""
	wipe(commit=False)
	_masters()
	_selling_alpha()
	_buying_alpha()
	_accounts_alpha()
	_stock_alpha()
	_projects_alpha()
	_selective_pricing()
	_beta_traps()
	_journal_entries_alpha()
	_manufacturing_alpha()
	_timesheets_alpha()
	_gamma_multiplant()
	_gamma_tax_segments()
	if commit:
		frappe.db.commit()


def wipe(commit: bool = False) -> None:
	for dt in _WIPE_DOCTYPES:
		try:
			frappe.db.delete(dt, {"name": ("like", f"{PREFIX}%")})
		except Exception:
			pass
	if commit:
		frappe.db.commit()


# ---------------------------------------------------------------------------
# masters
# ---------------------------------------------------------------------------
def _masters() -> None:
	for name, cur, lh in ((ALPHA, "INR", ""), (BETA, "INR", ""), (GAMMA, "INR", "")):
		_insert(
			"Company",
			name,
			{
				"company_name": name,
				"abbr": name.replace("_JPL-", "").replace(" ", "")[:8],
				"default_currency": cur,
				"country": "India",
				"default_letter_head": lh,
				"default_selling_terms": "",
			},
			docstatus=0,
		)
	items = (
		("_JPL-ItemNonStock", "Services", 0),
		("_JPL-ItemStock", "Fasteners", 1),
		("_JPL-ItemWidget", "Widgets", 1),
		("_JPL-ItemElec", "Electronics", 1),
		("_JPL-ItemFurn", "Furniture", 1),
	)
	for code, group, is_stock in items:
		_insert(
			"Item",
			code,
			{
				"item_code": code,
				"item_name": code,
				"item_group": group,
				"is_stock_item": is_stock,
				"stock_uom": "Nos",
			},
			docstatus=0,
		)
	# Party masters for the Tier-2 realized-vs-master detectors. DealerD's
	# master DIVERGES from its realized terms, RetailR's MATCHES (suppression
	# trap), DealerE deliberately has NO Customer master (unset case).
	for cust, terms in (("_JPL-DealerD", "45 Days"), ("_JPL-RetailR", "15 Days")):
		_insert(
			"Customer",
			cust,
			{"customer_name": cust, "customer_group": "Dealer", "payment_terms": terms},
			docstatus=0,
		)
	for sup, terms in (("_JPL-BoltSupply", "Net 45"), ("_JPL-AcmeNonStock", "Net 30")):
		_insert(
			"Supplier",
			sup,
			{"supplier_name": sup, "payment_terms": terms},
			docstatus=0,
		)


# ---------------------------------------------------------------------------
# selling (Sales Invoices): price list, payment terms, tc/letterhead, warehouse
# ---------------------------------------------------------------------------
def _selling_alpha() -> None:
	# (customer, group, price_list, terms, count)
	blocks = [
		("_JPL-DealerD", "Dealer", "Dealer Pricing", ["30 Days"] * 25),
		("_JPL-DealerE", "Dealer", "Dealer Pricing", ["30 Days"] * 20),
		("_JPL-DealerF", "Dealer", "Standard Selling", ["30 Days"] * 13 + ["15 Days"] * 2),
		("_JPL-RetailR", "Retail", "Standard Selling", ["15 Days"] * 22),
	]
	ordinal = 0
	for customer, group, price_list, terms_list in blocks:
		for terms in terms_list:
			# first 40 SIs -> Electronics/Main, rest -> Furniture/Bulk (balanced,
			# single-plant: exactly two warehouses). Electronics lines transact
			# in UOM "Box" (differs from the item's stock UOM "Nos" - the
			# stock-uom-conversion pattern); Furniture lines use the stock UOM
			# (the vanilla trap the org-default gate must suppress).
			if ordinal < 40:
				item_group, warehouse, uom = "Electronics", "WH-Alpha-Main", "Box"
			else:
				item_group, warehouse, uom = "Furniture", "WH-Alpha-Bulk", "Nos"
			name = f"{PREFIX}SI-A-{ordinal:04d}"
			_insert(
				"Sales Invoice",
				name,
				{
					"customer": customer,
					"customer_group": group,
					"selling_price_list": price_list,
					"payment_terms_template": terms,
					"letter_head": LETTER_HEAD,
					"tc_name": "",
					"company": ALPHA,
					"posting_date": _day(ordinal),
				},
				children={
					"items": (
						"Sales Invoice Item",
						[
							{
								"item_code": "_JPL-ItemElec",
								"item_name": "_JPL-ItemElec",
								"item_group": item_group,
								"warehouse": warehouse,
								"uom": uom,
								"qty": 1,
								"rate": 100,
							}
						],
					)
				},
			)
			ordinal += 1


# ---------------------------------------------------------------------------
# buying (Purchase Orders + Purchase Invoices)
# ---------------------------------------------------------------------------
def _buying_alpha() -> None:
	def po(idx, supplier, item_code, item_group, taxes, lines=1):
		name = f"{PREFIX}PO-A-{idx:04d}"
		rows = [
			{
				"item_code": item_code,
				"item_group": item_group,
				"qty": 1,
				"rate": 10,
				"schedule_date": _day(idx),
			}
			for _ in range(lines)
		]
		_insert(
			"Purchase Order",
			name,
			{
				"supplier": supplier,
				"company": ALPHA,
				"transaction_date": _day(idx),
				"taxes_and_charges": taxes,
			},
			children={"items": ("Purchase Order Item", rows)},
		)

	idx = 0
	for _ in range(40):  # Acme: non-stock only
		po(idx, "_JPL-AcmeNonStock", "_JPL-ItemNonStock", "Services", "Alpha GST 5%")
		idx += 1
	for _ in range(35):  # Bolt: Fasteners, stock
		po(idx, "_JPL-BoltSupply", "_JPL-ItemStock", "Fasteners", "Alpha GST 12%")
		idx += 1
	for j in range(25):  # TaxHabit: 24x 18% + 1x 5%
		taxes = "Alpha GST 5%" if j == 0 else "Alpha GST 18%"
		po(idx, "_JPL-TaxHabit", "_JPL-ItemStock", "Fasteners", taxes)
		idx += 1
	for _ in range(6):  # InflateCo child-row trap: 6 POs x 10 lines
		po(idx, "_JPL-InflateCo", "_JPL-ItemWidget", "Widgets", "Alpha GST 12%", lines=10)
		idx += 1

	# Purchase Invoices with update_stock=1, spread across days (legit habit).
	# Tier-2 additions ride the same rows: BoltSupply's realized terms
	# ("Net 15") DIVERGE from its Supplier master ("Net 45"); its item lines
	# (Fasteners, immediate) are the deferred-expense variance ballast.
	for k in range(34):
		name = f"{PREFIX}PI-A-{k:04d}"
		_insert(
			"Purchase Invoice",
			name,
			{
				"supplier": "_JPL-BoltSupply",
				"company": ALPHA,
				"posting_date": _day(k),
				"update_stock": 1,
				"payment_terms_template": "Net 15",
			},
			children={
				"items": (
					"Purchase Invoice Item",
					[
						{
							"item_code": "_JPL-ItemStock",
							"item_name": "_JPL-ItemStock",
							"item_group": "Fasteners",
							"enable_deferred_expense": 0,
							"qty": 1,
							"rate": 10,
						}
					],
				)
			},
		)
	# AcmeNonStock's realized terms MATCH its Supplier master ("Net 30") - the
	# suppression trap; its Services lines book deferred expense (the
	# acct-deferred-usage expense-side pattern). update_stock stays 1 so the
	# Tier-1 buy-pi-update-stock habit keeps its 100% confidence.
	for k in range(22):
		name = f"{PREFIX}PI-A-{34 + k:04d}"
		_insert(
			"Purchase Invoice",
			name,
			{
				"supplier": "_JPL-AcmeNonStock",
				"company": ALPHA,
				"posting_date": _day(34 + k),
				"update_stock": 1,
				"payment_terms_template": "Net 30",
			},
			children={
				"items": (
					"Purchase Invoice Item",
					[
						{
							"item_code": "_JPL-ItemNonStock",
							"item_name": "_JPL-ItemNonStock",
							"item_group": "Services",
							"enable_deferred_expense": 1,
							"qty": 1,
							"rate": 10,
						}
					],
				)
			},
		)


# ---------------------------------------------------------------------------
# accounts (Payment Entries)
# ---------------------------------------------------------------------------
def _accounts_alpha() -> None:
	# Receive: 30 Bank Draft + 3 Cash ; Pay: 30 Cash + 2 Bank Draft
	plan = (
		["Bank Draft"] * 30 + ["Cash"] * 3,  # Receive
		["Cash"] * 30 + ["Bank Draft"] * 2,  # Pay
	)
	idx = 0
	for ptype, modes in zip(("Receive", "Pay"), plan, strict=True):
		for mode in modes:
			name = f"{PREFIX}PE-A-{idx:04d}"
			_insert(
				"Payment Entry",
				name,
				{
					"payment_type": ptype,
					"mode_of_payment": mode,
					"company": ALPHA,
					"posting_date": _day(idx),
					"party_type": "Customer",
					"party": "_JPL-DealerD",
					"paid_amount": 100,
					"received_amount": 100,
				},
			)
			idx += 1

	# Quotations valid 15 days (diverges from the +1-month default).
	for q in range(32):
		td = _day(q)
		name = f"{PREFIX}QTN-A-{q:04d}"
		_insert(
			"Quotation",
			name,
			{
				"company": ALPHA,
				"transaction_date": td,
				"valid_till": frappe.utils.add_days(td, 15),
				"quotation_to": "Customer",
				"party_name": "_JPL-DealerD",
			},
		)


# ---------------------------------------------------------------------------
# stock (Stock Entries)
# ---------------------------------------------------------------------------
def _stock_alpha() -> None:
	# 32 Material Transfer (Main -> Bulk) + 30 Material Receipt (-> Main)
	idx = 0
	for _ in range(32):
		name = f"{PREFIX}SE-A-{idx:04d}"
		_insert(
			"Stock Entry",
			name,
			{
				"company": ALPHA,
				"posting_date": _day(idx),
				"purpose": "Material Transfer",
				"stock_entry_type": "Material Transfer",
			},
			children={
				"items": (
					"Stock Entry Detail",
					[
						{
							"s_warehouse": "WH-Alpha-Main",
							"t_warehouse": "WH-Alpha-Bulk",
							"item_code": "_JPL-ItemStock",
							"qty": 1,
						}
					],
				)
			},
		)
		idx += 1
	for _ in range(30):
		name = f"{PREFIX}SE-A-{idx:04d}"
		_insert(
			"Stock Entry",
			name,
			{
				"company": ALPHA,
				"posting_date": _day(idx),
				"purpose": "Material Receipt",
				"stock_entry_type": "Material Receipt",
			},
			children={
				"items": (
					"Stock Entry Detail",
					[
						{
							"s_warehouse": "",
							"t_warehouse": "WH-Alpha-Main",
							"item_code": "_JPL-ItemStock",
							"qty": 1,
						}
					],
				)
			},
		)
		idx += 1


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------
def _projects_alpha() -> None:
	# 22 External (20 timesheet-billed) + 10 Internal (fixed)
	idx = 0
	for j in range(22):
		name = f"{PREFIX}PRJ-A-{idx:04d}"
		_insert(
			"Project",
			name,
			{"project_name": name, "project_type": "External", "company": ALPHA},
			docstatus=0,
			creation=f"{_day(idx)} 10:00:00",
		)
		if j < 20:
			ts_name = f"{PREFIX}TS-A-{idx:04d}"
			_insert(
				"Timesheet",
				ts_name,
				{"company": ALPHA, "parent_project": name},
				creation=f"{_day(idx)} 11:00:00",
			)
		idx += 1
	for _ in range(10):
		name = f"{PREFIX}PRJ-A-{idx:04d}"
		_insert(
			"Project",
			name,
			{"project_name": name, "project_type": "Internal", "company": ALPHA},
			docstatus=0,
			creation=f"{_day(idx)} 10:00:00",
		)
		idx += 1


# ---------------------------------------------------------------------------
# selective item pricing (org-wide existence)
# ---------------------------------------------------------------------------
def _selective_pricing() -> None:
	for j in range(EXPECT["selective_customers"]):
		name = f"{PREFIX}IP-{j:04d}"
		_insert(
			"Item Price",
			name,
			{
				"customer": f"{PREFIX}IPCust{j}",
				"price_list": "Dealer Pricing",
				"item_code": "_JPL-ItemStock",
				"selling": 1,
				"price_list_rate": 100 + j,
			},
			docstatus=0,
		)


# ---------------------------------------------------------------------------
# company Beta: all traps
# ---------------------------------------------------------------------------
def _beta_traps() -> None:
	# sole-value (single price list + single term) + multi-plant (5 warehouses)
	for i in range(30):
		name = f"{PREFIX}SI-B-{i:04d}"
		warehouse = f"WH-Beta-{(i % 5) + 1}"
		_insert(
			"Sales Invoice",
			name,
			{
				"customer": f"{PREFIX}BetaCust{i % 6}",
				"customer_group": "Wholesale",
				"selling_price_list": "Standard Selling",
				"payment_terms_template": "Net 30",
				"letter_head": "",
				"tc_name": "",
				"company": BETA,
				"posting_date": _day(i),
			},
			children={
				"items": (
					"Sales Invoice Item",
					[
						{
							"item_code": "_JPL-ItemElec",
							"item_name": "_JPL-ItemElec",
							"item_group": "Electronics",
							"warehouse": warehouse,
							"qty": 1,
							"rate": 100,
						}
					],
				)
			},
		)
	# vanilla-default quotations (valid +1 month, spread over days so only the
	# system-default gate suppresses them, not the spread gate).
	for i in range(30):
		td = _day(i)
		name = f"{PREFIX}QTN-B-{i:04d}"
		_insert(
			"Quotation",
			name,
			{
				"company": BETA,
				"transaction_date": td,
				"valid_till": frappe.utils.add_days(td, 30),
				"quotation_to": "Customer",
				"party_name": f"{PREFIX}BetaCust{i % 6}",
			},
		)
	# go-live burst: 35 PIs, all one day and one creation second -> spread gate.
	burst_ts = "2025-10-01 03:00:00"
	for i in range(35):
		name = f"{PREFIX}PI-B-{i:04d}"
		_insert(
			"Purchase Invoice",
			name,
			{
				"supplier": f"{PREFIX}BetaSup",
				"company": BETA,
				"posting_date": "2025-10-01",
				"update_stock": 1,
			},
			creation=burst_ts,
		)


# ---------------------------------------------------------------------------
# Tier-2: journal entries (acct-je-usage)
# ---------------------------------------------------------------------------
def _journal_entries_alpha() -> None:
	# 35 Bank Entry JEs (32 from the template, 3 manual, interleaved) + 4 plain
	# Journal Entry ballast -> template habit 32/35 and an org voucher-type
	# habit 35/39, both over distinct days.
	idx = 0
	for j in range(35):
		manual = j % 12 == 5  # 3 of 35
		name = f"{PREFIX}JE-A-{idx:04d}"
		_insert(
			"Journal Entry",
			name,
			{
				"voucher_type": "Bank Entry",
				"company": ALPHA,
				"posting_date": _day(idx),
				"from_template": "" if manual else EXPECT["je_template"],
			},
		)
		idx += 1
	for _ in range(4):
		name = f"{PREFIX}JE-A-{idx:04d}"
		_insert(
			"Journal Entry",
			name,
			{
				"voucher_type": "Journal Entry",
				"company": ALPHA,
				"posting_date": _day(idx),
				"from_template": "",
			},
		)
		idx += 1


# ---------------------------------------------------------------------------
# Tier-2: manufacturing (mfg-default-bom-usage)
# ---------------------------------------------------------------------------
def _manufacturing_alpha() -> None:
	# BOM masters: Widget's flagged default is BOM-DEF but 22/24 Work Orders
	# use BOM-ALT (the divergence to learn); Elec's WOs all use the flagged
	# default (the suppression trap).
	for bom, item in (("_JPL-BOM-DEF", "_JPL-ItemWidget"), ("_JPL-BOM-ELEC", "_JPL-ItemElec")):
		_insert(
			"BOM",
			bom,
			{"item": item, "is_default": 1, "is_active": 1, "company": ALPHA, "quantity": 1},
		)
	idx = 0
	for j in range(24):
		bom = "_JPL-BOM-DEF" if j % 12 == 7 else "_JPL-BOM-ALT"  # 2 of 24 on default
		name = f"{PREFIX}WO-A-{idx:04d}"
		_insert(
			"Work Order",
			name,
			{"production_item": "_JPL-ItemWidget", "bom_no": bom, "company": ALPHA, "qty": 1},
			creation=f"{_day(idx)} 10:00:00",
		)
		idx += 1
	for _ in range(21):
		name = f"{PREFIX}WO-A-{idx:04d}"
		_insert(
			"Work Order",
			name,
			{"production_item": "_JPL-ItemElec", "bom_no": "_JPL-BOM-ELEC", "company": ALPHA, "qty": 1},
			creation=f"{_day(idx)} 10:00:00",
		)
		idx += 1


# ---------------------------------------------------------------------------
# Tier-2: timesheets (proj-timesheet-rate-defaults)
# ---------------------------------------------------------------------------
def _timesheets_alpha() -> None:
	# Consulting billed at 1500 with NO configured rate (proposes); Support
	# billed at 800 with a MATCHING Activity Type master (suppression trap).
	_insert(
		"Activity Type",
		"_JPL-Consulting",
		{"activity_type": "_JPL-Consulting", "billing_rate": 0},
		docstatus=0,
	)
	_insert(
		"Activity Type", "_JPL-Support", {"activity_type": "_JPL-Support", "billing_rate": 800}, docstatus=0
	)
	idx = 0
	for activity, rate, count in (("_JPL-Consulting", 1500, 32), ("_JPL-Support", 800, 30)):
		for _ in range(count):
			name = f"{PREFIX}TSR-A-{idx:04d}"
			_insert(
				"Timesheet",
				name,
				{"company": ALPHA},
				creation=f"{_day(idx)} 09:00:00",
				children={
					"time_logs": (
						"Timesheet Detail",
						[{"activity_type": activity, "billing_rate": rate, "is_billable": 1, "hours": 1}],
					)
				},
			)
			idx += 1


# ---------------------------------------------------------------------------
# Tier-2: company Gamma - MULTI-PLANT (5 warehouses), cost-center-clustered
# ---------------------------------------------------------------------------
def _gamma_multiplant() -> None:
	"""(item_group, cost_center) -> warehouse pairs for the dimensioned stock
	variant + warehouse -> cost_center for acct-cost-center-dimension.
	Electronics lines also book deferred revenue (acct-deferred-usage). The 3
	stray warehouses push the count to 5 (> the single-plant max of 3) without
	feeding any gate (3 units < n_min)."""
	blocks = [
		# (count, item_group, cost_center, warehouse, deferred_revenue)
		(32, "Electronics", "_JPL-CC-North", "WH-Gamma-N1", 1),
		(32, "Furniture", "_JPL-CC-South", "WH-Gamma-S1", 0),
		(1, "Widgets", "_JPL-CC-East", "WH-Gamma-X1", 0),
		(1, "Widgets", "_JPL-CC-East", "WH-Gamma-X2", 0),
		(1, "Widgets", "_JPL-CC-East", "WH-Gamma-X3", 0),
	]
	ordinal = 0
	for count, item_group, cost_center, warehouse, deferred in blocks:
		for _ in range(count):
			name = f"{PREFIX}SI-G-{ordinal:04d}"
			_insert(
				"Sales Invoice",
				name,
				{
					"customer": f"{PREFIX}GDim{ordinal % 4}",
					"customer_group": "Wholesale",
					"company": GAMMA,
					"posting_date": _day(ordinal),
				},
				children={
					"items": (
						"Sales Invoice Item",
						[
							{
								"item_code": "_JPL-ItemElec",
								"item_name": "_JPL-ItemElec",
								"item_group": item_group,
								"warehouse": warehouse,
								"cost_center": cost_center,
								"enable_deferred_revenue": deferred,
								"qty": 1,
								"rate": 100,
							}
						],
					)
				},
			)
			ordinal += 1


# ---------------------------------------------------------------------------
# Tier-2: company Gamma - geography-confounded tax segments (MUST demote)
# ---------------------------------------------------------------------------
def _gamma_tax_segments() -> None:
	"""acct-party-tax-template fixtures: a Custom Field row marks
	Customer.territory as a discoverable segment column (an EXISTING standard
	column, so the regex+meta validation passes without any DDL). North/South
	segments map cleanly to GST 18%/12%, but every North customer sits in
	Maharashtra and every South one in Karnataka - party STATE predicts the
	template exactly as well as the segment (the geography confound; the
	candidate must demote and carry the caveat). An active Tax Rule already
	encodes the 12% template, so the South candidate must be suppressed
	entirely (cross-ref)."""
	_insert(
		"Custom Field",
		f"{PREFIX}CF-Customer-territory",
		{
			"dt": "Customer",
			"fieldname": "territory",
			"fieldtype": "Link",
			"label": "Territory",
			"reqd": 0,
			"hidden": 0,
		},
		docstatus=0,
	)
	_insert(
		"Tax Rule",
		f"{PREFIX}TR-South",
		{"tax_type": "Sales", "sales_tax_template": EXPECT["south_tax_template"], "company": GAMMA},
		docstatus=0,
	)

	segments = (
		("N", "_JPL-North", "Maharashtra", EXPECT["north_tax_template"]),
		("S", "_JPL-South", "Karnataka", EXPECT["south_tax_template"]),
	)
	ordinal = 0
	for tag, territory, state, template in segments:
		customers = [f"{PREFIX}GCust-{tag}{j}" for j in range(8)]
		for cust in customers:
			_insert(
				"Customer",
				cust,
				{"customer_name": cust, "customer_group": "Wholesale", "territory": territory},
				docstatus=0,
			)
			addr = f"{PREFIX}ADDR-{tag}-{cust[-1]}"
			_insert(
				"Address",
				addr,
				{
					"address_title": cust,
					"address_type": "Billing",
					"address_line1": "1 Main Rd",
					"city": "City",
					"state": state,
					"country": "India",
				},
				docstatus=0,
				children={"links": ("Dynamic Link", [{"link_doctype": "Customer", "link_name": cust}])},
			)
		for j in range(32):
			name = f"{PREFIX}SI-GT-{tag}-{j:04d}"
			_insert(
				"Sales Invoice",
				name,
				{
					"customer": customers[j % 8],
					"customer_group": "Wholesale",
					"taxes_and_charges": template,
					"company": GAMMA,
					"posting_date": _day(ordinal),
				},
			)
			ordinal += 1
