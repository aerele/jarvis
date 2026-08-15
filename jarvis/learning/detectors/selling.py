"""Selling-domain detector SQL + postprocess (plan section 4.2, Selling).

Tier-1 selling detectors read the realized document (Sales Invoice) at unit =
distinct parent invoice. Quotation validity and TC/letterhead are S5
(master-vs-realized) and live in postprocess functions. Item-Price existence
is a row-count existence check, also a postprocess.
"""

from __future__ import annotations

from collections import defaultdict

# Hard per-detector row cap (plan §4.3 fix 7): a curated OOM backstop for the
# unit-grain header SQL shapes; the nightly row budget pauses across detectors.
HARD_ROW_LIMIT = 200000

# --- S1: customer -> selling price list (unit = distinct Sales Invoice) -------
CUSTOMER_PRICE_LIST_SQL = f"""
SELECT si.name AS unit_id,
       si.customer AS antecedent,
       si.selling_price_list AS consequent,
       si.company AS company,
       si.posting_date AS day,
       si.creation AS created
FROM `tabSales Invoice` si
WHERE si.docstatus = 1
  AND si.company = %(company)s
  AND si.posting_date >= %(window_start)s
  AND si.customer IS NOT NULL AND si.customer != ''
  AND si.selling_price_list IS NOT NULL AND si.selling_price_list != ''
LIMIT {HARD_ROW_LIMIT}
"""

# --- S1: customer group -> payment terms template (unit = distinct SI) --------
GROUP_PAYMENT_TERMS_SQL = f"""
SELECT si.name AS unit_id,
       si.customer_group AS antecedent,
       si.payment_terms_template AS consequent,
       si.company AS company,
       si.posting_date AS day,
       si.creation AS created
FROM `tabSales Invoice` si
WHERE si.docstatus = 1
  AND si.company = %(company)s
  AND si.posting_date >= %(window_start)s
  AND si.customer_group IS NOT NULL AND si.customer_group != ''
  AND si.payment_terms_template IS NOT NULL AND si.payment_terms_template != ''
LIMIT {HARD_ROW_LIMIT}
"""

# --- S5 (Tier-2): realized customer terms vs the Customer master --------------
# Unit = distinct Sales Invoice; the postprocess compares each customer's
# realized mode against Customer.payment_terms and proposes only on an unset
# or divergent master (a matching master is config working as intended).
CUSTOMER_PAYMENT_TERMS_SQL = f"""
SELECT si.name AS unit_id,
       si.customer AS antecedent,
       si.payment_terms_template AS consequent,
       si.company AS company,
       si.posting_date AS day,
       si.creation AS created
FROM `tabSales Invoice` si
WHERE si.docstatus = 1
  AND si.company = %(company)s
  AND si.posting_date >= %(window_start)s
  AND si.customer IS NOT NULL AND si.customer != ''
  AND si.payment_terms_template IS NOT NULL AND si.payment_terms_template != ''
LIMIT {HARD_ROW_LIMIT}
"""

CUSTOMER_TERMS_MASTER_SQL = (
	"SELECT c.name AS name, c.payment_terms AS payment_terms FROM `tabCustomer` c WHERE c.name IN %(names)s"
)

# --- S5 baseline for quotation validity (unit = distinct Quotation) -----------
# consequent = realized validity gap in whole days; postprocess compares it to
# the system default (CRM Settings.default_valid_till, else the +1-month client
# default) and suppresses vanilla usage.
QUOTATION_VALIDITY_SQL = f"""
SELECT q.name AS unit_id,
       'org' AS antecedent,
       CAST(DATEDIFF(q.valid_till, q.transaction_date) AS CHAR) AS consequent,
       q.company AS company,
       q.transaction_date AS day,
       q.creation AS created
FROM `tabQuotation` q
WHERE q.docstatus = 1
  AND q.company = %(company)s
  AND q.transaction_date >= %(window_start)s
  AND q.valid_till IS NOT NULL
  AND q.valid_till >= q.transaction_date
LIMIT {HARD_ROW_LIMIT}
"""

# --- S2 existence: customer-specific selling Item Prices (org-wide) ------------
SELECTIVE_ITEM_PRICING_SQL = f"""
SELECT ip.customer AS customer,
       ip.item_code AS item_code
FROM `tabItem Price` ip
WHERE ip.selling = 1
  AND ip.customer IS NOT NULL AND ip.customer != ''
LIMIT {HARD_ROW_LIMIT}
"""

# --- S1+S5 raw rows for TC / letterhead vs company defaults --------------------
TC_LETTERHEAD_SQL = f"""
SELECT si.name AS unit_id,
       si.letter_head AS letter_head,
       si.tc_name AS tc_name,
       si.company AS company,
       si.posting_date AS day,
       si.creation AS created
FROM `tabSales Invoice` si
WHERE si.docstatus = 1
  AND si.company = %(company)s
  AND si.posting_date >= %(window_start)s
LIMIT {HARD_ROW_LIMIT}
"""

_COMPANY_DEFAULTS_SQL = """
SELECT default_letter_head AS default_letter_head,
       default_selling_terms AS default_selling_terms
FROM `tabCompany`
WHERE name = %(company)s
LIMIT 1
"""


# --- S4 log-mode: customer -> print format (Access Log, Phase 2) --------------
# Stream chunk over `tabAccess Log` by indexed `creation` (plan section 5.3:
# `modified` is NOT indexed on log tables). The statement is static; the caller
# advances the %(watermark)s parameter between chunks. The LIKE literal uses %%
# because the driver always receives params with this query.
PRINT_LOG_CHUNK = 5000

PRINT_LOG_SQL = f"""
SELECT al.name AS name,
       al.creation AS created,
       al.page AS page,
       al.export_from AS ref_doctype,
       al.reference_document AS ref_name
FROM `tabAccess Log` al
WHERE al.method = 'Print'
  AND al.page LIKE 'Print Format: %%'
  AND al.creation > %(watermark)s
ORDER BY al.creation
LIMIT {PRINT_LOG_CHUNK}
"""

# Bounded IN-list party resolution: one static statement per referenced selling
# doctype; the caller chunks the name lists (plan section 5.3). Quotation keeps
# its customer in party_name (no customer column).
PRINT_PARTY_SQL = {
	"Sales Invoice": (
		"SELECT name AS name, customer AS party, company AS company "
		"FROM `tabSales Invoice` WHERE docstatus = 1 AND name IN %(names)s"
	),
	"Sales Order": (
		"SELECT name AS name, customer AS party, company AS company "
		"FROM `tabSales Order` WHERE docstatus = 1 AND name IN %(names)s"
	),
	"Delivery Note": (
		"SELECT name AS name, customer AS party, company AS company "
		"FROM `tabDelivery Note` WHERE docstatus = 1 AND name IN %(names)s"
	),
	"Quotation": (
		"SELECT name AS name, party_name AS party, company AS company "
		"FROM `tabQuotation` WHERE docstatus = 1 AND quotation_to = 'Customer' "
		"AND name IN %(names)s"
	),
}


def postprocess_quotation_validity(rows, spec, company, patterndb, params):
	"""S5: propose a validity habit ONLY when it diverges from the system
	default (never restate the vanilla +1-month / configured default)."""
	from jarvis.learning.executor import evaluate_segment, month_key, singles_value

	units = {}
	for r in rows or []:
		try:
			gap = int(r.get("consequent"))
		except (TypeError, ValueError):
			continue
		units[r.get("unit_id")] = (str(gap), r.get("day"), r.get("created"))
	if not units:
		return []

	default_raw = singles_value(patterndb, "CRM Settings", "default_valid_till")
	try:
		default_days = int(default_raw) if default_raw not in (None, "", "0", 0) else None
	except (TypeError, ValueError):
		default_days = None

	counts: dict = defaultdict(int)
	for gap, _d, _c in units.values():
		counts[gap] += 1
	mode = max(counts.items(), key=lambda kv: (kv[1], -abs(int(kv[0]))))[0]
	mode_days = int(mode)

	if default_days:
		if abs(mode_days - default_days) <= 2:
			return []
		default_label = f"{default_days} days"
	else:
		if mode_days in (28, 29, 30, 31):  # untouched +1-month client default
			return []
		default_label = "1 month"

	days = [d for _g, d, _c in units.values()]
	created = [c for _g, _d, c in units.values()]
	exceptions = [
		{"unit": uid, "value": g, "month": month_key(d)} for uid, (g, d, _c) in units.items() if g != mode
	][:20]
	raw = evaluate_segment(
		spec,
		antecedent_value="org",
		consequent_value=mode,
		k=counts[mode],
		n_units=len(units),
		base_rate=0.0,
		days=days,
		created=created,
		exceptions=exceptions,
		single_antecedent=True,
		names_party=False,
		template="quotation-validity",
		vars={"days": mode_days, "default_days": default_label},
		extra_evidence={"system_default_days": default_days, "system_default_label": default_label},
		patterndb=patterndb,
	)
	return [raw] if raw else []


def postprocess_customer_payment_terms(rows, spec, company, patterndb, params):
	"""S5 (Tier-2, plan section 4.2): a customer's realized terms habit is
	proposed only when the Customer master is unset or says something else
	(divergence is the finding; a matching master is suppressed)."""
	from jarvis.learning.executor import reduce_units

	raws = reduce_units(rows, spec, patterndb)
	if not raws:
		return []

	names = sorted({r.get("antecedent_value") for r in raws if r.get("antecedent_value")})
	masters: dict = {}
	if names:
		try:
			for m in patterndb.sql_select(CUSTOMER_TERMS_MASTER_SQL, {"names": names}) or []:
				masters[m.get("name")] = m.get("payment_terms")
		except Exception:
			masters = {}

	out = []
	for raw in raws:
		master = masters.get(raw.get("antecedent_value"))
		if master and str(master) == str(raw.get("consequent_value")):
			continue  # master already encodes the habit - nothing to learn
		clause = (
			f'the Customer master default is "{master}"'
			if master
			else "the Customer master has no default payment terms"
		)
		raw["skill_bullet_vars"] = {
			"customer": raw.get("antecedent_value"),
			"terms": raw.get("consequent_value"),
			"master_clause": clause,
		}
		evidence = raw.get("evidence")
		if isinstance(evidence, dict):
			evidence["master_payment_terms"] = master
		out.append(raw)
	return out


def postprocess_selective_item_pricing(rows, spec, company, patterndb, params):
	"""S2 existence: customer-specific negotiated prices exist. Names customers
	in the evidence -> content-escalated to B."""
	rows = rows or []
	customers = sorted({r.get("customer") for r in rows if r.get("customer")})
	n_rows = len(rows)
	n_customers = len(customers)
	min_rows = int((spec.get("gates") or {}).get("n_min", 10))
	if n_rows < min_rows or n_customers < 1:
		return []
	band = "High" if n_customers >= 3 else "Medium"
	draft = (
		f"- This org maintains customer-specific negotiated prices for {n_customers} "
		"customers; honour a customer's own price list before the group or org default."
	)
	return [
		{
			"antecedent_value": "org",
			"consequent_value": "customer-specific-pricing",
			"k": n_customers,
			"n_units": n_customers,
			"n_rows": n_rows,
			"exception_n": 0,
			"confidence": 1.0,
			"wilson_low": 1.0,
			"gap": 1.0,
			"band": band,
			"temporal_spread": {},
			"evidence": {
				"n_customers": n_customers,
				"n_item_prices": n_rows,
				"customers": customers[:10],
				"sql_shape": "S2",
			},
			"exceptions": [],
			"exceptions_cluster": None,
			"names_party": True,
			"skill_template": "selective-item-pricing",
			"skill_bullet_vars": {"n_customers": n_customers},
			"statement": f"Customer-specific negotiated prices exist for {n_customers} customers.",
			"rule": None,
			"skill_draft": draft,
			"since_date": "",
			"unit_doctype": "customers",
		}
	]


def postprocess_tc_letterhead(rows, spec, company, patterndb, params):
	"""S1+S5: habitual letter head / TC that diverges from the Company default."""
	from jarvis.learning.executor import evaluate_segment, month_key

	row = patterndb.timed_select(_COMPANY_DEFAULTS_SQL, {"company": company})
	comp = row[0] if row else {}

	out = []
	for field_name, comp_default, label in (
		("letter_head", (comp or {}).get("default_letter_head"), "letter head"),
		("tc_name", (comp or {}).get("default_selling_terms"), "terms and conditions"),
	):
		units = {}
		for r in rows or []:
			value = r.get(field_name)
			if not value:
				continue
			units[r.get("unit_id")] = (value, r.get("day"), r.get("created"))
		if not units:
			continue
		counts: dict = defaultdict(int)
		for value, _d, _c in units.values():
			counts[value] += 1
		mode = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))[0]
		if comp_default and str(mode) == str(comp_default):
			continue  # matches the company default -> nothing to learn
		days = [d for _v, d, _c in units.values()]
		created = [c for _v, _d, c in units.values()]
		exceptions = [
			{"unit": uid, "value": v, "month": month_key(d)} for uid, (v, d, _c) in units.items() if v != mode
		][:20]
		raw = evaluate_segment(
			spec,
			antecedent_value=f"org:{field_name}",
			consequent_value=mode,
			k=counts[mode],
			n_units=len(units),
			base_rate=0.0,
			days=days,
			created=created,
			exceptions=exceptions,
			single_antecedent=True,
			names_party=False,
			template="tc-letterhead",
			vars={"field_label": label, "value": mode},
			extra_evidence={"company_default": comp_default, "field": field_name},
			patterndb=patterndb,
		)
		if raw:
			out.append(raw)
	return out


def postprocess_customer_print_format(rows, spec, company, patterndb, params):
	"""S4 log-mode (plan section 4.2, Phase 2): customer C prints with format F.

	Pure-read: combines the detector's monthly snapshot history with the live
	Access Log tail beyond the Detector State watermark, BOTH read through the
	PatternDB facade, so the engine's read fence contains the whole pass.
	Persistence of the tail into snapshots is a separate engine-side step
	(``snapshots.ingest_print_log``, called after the fence closes) - this
	function writes nothing.

	Unit = print event (the natural unit of a printing habit; there is no
	parent document grain because the same invoice printed on ten days is ten
	observations of the habit). Import/reprint bursts are collapsed via
	``stats.collapse_bursts`` on creation timestamps at aggregation time, so
	``eff`` counts - not raw rows - feed every gate. Gates (plan section 4.2):
	n_min=10 effective print events for the (customer, format) pair,
	c_min=0.90, spread over >=5 distinct days; below 30 events the wording
	drops to the tendency template. B sensitivity (names a customer) =>
	insight-only compile in Phase 1.
	"""
	from jarvis.learning import snapshots, stats
	from jarvis.learning.executor import _temporal_spread

	gates = spec.get("gates") or {}
	n_min = int(gates.get("n_min", 10))
	c_min = float(gates.get("c_min", 0.90))
	tendency_below = int(gates.get("tendency_below", 30))
	window_start = str((params or {}).get("window_start") or "")
	detector_id = spec["id"]

	# 1. snapshot history (window-bounded, company-scoped), via the facade.
	combined = snapshots.read_all(
		detector_id, company=company, runner=patterndb.sql_select, window_start=window_start
	)
	snapshot_events = int(combined.get("rows_ingested") or 0)
	snapshot_periods = list(combined.get("periods") or [])

	# 2. live tail beyond the watermark, via the facade (not yet in snapshots;
	# the watermark keeps the two sources disjoint, so nothing double-counts).
	watermark = snapshots.read_watermark(detector_id, runner=patterndb.sql_select)
	events, _last, _rows_read = snapshots.stream_print_events(patterndb.sql_select, watermark)
	live = [
		e
		for e in events
		if (company is None or e.get("company") == company) and (not window_start or e["day"] >= window_start)
	]
	for payload in snapshots.aggregate_events(live).values():
		combined = snapshots.merge_payloads(combined, payload)

	counts = combined.get("counts") or {}
	if not counts:
		return []

	# Site-wide variance gate (mirrors reduce_units): one format across the
	# whole company is the org default, not a per-customer habit.
	site: dict = defaultdict(int)
	for formats in counts.values():
		for fmt, agg in formats.items():
			site[fmt] += int((agg or {}).get("eff") or 0)
	if stats.variance_gate(dict(site)):
		return []
	site_total = sum(site.values())

	first_day = combined.get("first_day")
	last_day = combined.get("last_day")
	depth_days = 0
	if first_day and last_day:
		from jarvis.learning.executor import as_date

		start_d, end_d = as_date(first_day), as_date(last_day)
		if start_d and end_d:
			depth_days = (end_d - start_d).days + 1
	depth_note = f"based on {depth_days} days of print logs"

	out = []
	for party, formats in counts.items():
		n_party = sum(int((a or {}).get("eff") or 0) for a in formats.values())
		if not n_party:
			continue
		fmt, agg = max(formats.items(), key=lambda kv: (int((kv[1] or {}).get("eff") or 0), kv[0]))
		k = int((agg or {}).get("eff") or 0)
		if k < n_min:
			continue
		confidence = k / n_party
		if confidence < c_min:
			continue
		days = {d: c for d, c in ((agg or {}).get("days") or {}).items()}
		if not stats.spread_ok(days):
			continue

		rest_k = site[fmt] - k
		rest_n = site_total - n_party
		base_rate = (rest_k / rest_n) if rest_n > 0 else 0.0
		wl = stats.wilson_lower_bound(k, n_party)
		band = stats.band(wl)
		n_raw = sum(int((a or {}).get("n") or 0) for a in formats.values())
		template = "customer-print-format-tendency" if k < tendency_below else "customer-print-format"

		day_list: list = []
		for day, count in sorted(days.items()):
			day_list.extend([day] * max(int(count or 0), 1))
		exceptions = [
			{"value": other, "count": int((a or {}).get("eff") or 0)}
			for other, a in sorted(formats.items())
			if other != fmt
		][:20]

		out.append(
			{
				"antecedent_value": party,
				"consequent_value": fmt,
				"k": k,
				"n_units": n_party,
				"n_rows": n_raw,
				"exception_n": n_party - k,
				"confidence": confidence,
				"wilson_low": wl,
				"gap": confidence - base_rate,
				"band": band,
				"temporal_spread": _temporal_spread(day_list),
				"evidence": {
					"antecedent": party,
					"consequent": fmt,
					"k": k,
					"n_units": n_party,
					"n_rows": n_raw,
					"exception_n": n_party - k,
					"confidence": round(confidence, 4),
					"base_rate": round(base_rate, 4),
					"gap": round(confidence - base_rate, 4),
					"wilson_low": round(wl, 4),
					"band": band,
					"sql_shape": "S4",
					"log_depth_days": depth_days,
					"log_depth_note": depth_note,
					"snapshot_periods": snapshot_periods,
					"snapshot_events": snapshot_events,
					"live_events": len(live),
					"formats": {f: int((a or {}).get("eff") or 0) for f, a in formats.items()},
				},
				"exceptions": exceptions,
				"exceptions_cluster": None,
				"names_party": True,
				"skill_template": template,
				"skill_bullet_vars": {"customer": party, "print_format": fmt},
				"statement": None,
				"rule": None,
				"since_date": str(first_day or "")[:7],
				"unit_doctype": "print events",
			}
		)
	return out
