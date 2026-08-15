"""Accounts-domain detector SQL + postprocess (plan section 4.2, Accounts).

Tier-1 accounts detector: mode of payment by payment direction. Antecedent is
the direction (Receive / Pay), an org-level A-class aggregate - no party names,
no amounts.

Tier-2 pack (plan section 4.4):
  * acct-party-tax-template - the Phase-2 headliner. Segment antecedents are
    DISCOVERED at run time from `tabCustom Field` on the party doctypes
    (Select/Link/Data; e.g. india_compliance's gst_category - GST names are
    never hardcoded). Each discovered fieldname is regex + meta validated
    before it is interpolated into the otherwise-static segment SQL. Active
    Tax Rules are cross-referenced (propose only what no rule encodes) and the
    geography confound is guarded via the executor's Address/Dynamic-Link
    state map: when party state predicts the template as well as the segment
    does, the band is demoted; the compiled text ALWAYS carries the geography
    warning either way. Sensitivity B keeps it out of batch approve. The
    guard runs here (not via executor.GEOGRAPHY_CONFOUND_TEMPLATES) because
    the executor set is Supplier-worded and this detector spans Customer and
    Supplier passes with segment-shaped antecedents.
  * acct-cost-center-dimension, acct-je-usage, acct-deferred-usage - A-class
    aggregates over SI/PI/JE rows.

acct-rounding-habit and acct-payment-terms-timing remain future Tier-2 rows.
"""

from __future__ import annotations

import re
from collections import defaultdict

# Hard per-detector row cap (plan §4.3 fix 7): a curated OOM backstop for the
# unit-grain header SQL; the nightly row budget pauses across detectors.
HARD_ROW_LIMIT = 200000

# --- S1: payment direction -> mode of payment (unit = distinct Payment Entry) -
MODE_OF_PAYMENT_SQL = f"""
SELECT pe.name AS unit_id,
       pe.payment_type AS antecedent,
       pe.mode_of_payment AS consequent,
       pe.company AS company,
       pe.posting_date AS day,
       pe.creation AS created
FROM `tabPayment Entry` pe
WHERE pe.docstatus = 1
  AND pe.company = %(company)s
  AND pe.posting_date >= %(window_start)s
  AND pe.mode_of_payment IS NOT NULL AND pe.mode_of_payment != ''
  AND pe.payment_type IS NOT NULL AND pe.payment_type != ''
LIMIT {HARD_ROW_LIMIT}
"""

# --- S1: warehouse dimension -> cost center (unit = (invoice, warehouse)) -----
COST_CENTER_DIMENSION_SQL = f"""
SELECT CONCAT(sii.parent, '::', sii.warehouse) AS unit_id,
       sii.warehouse AS antecedent,
       CASE WHEN COUNT(DISTINCT sii.cost_center) = 1
            THEN MAX(sii.cost_center) ELSE '__mixed__' END AS consequent,
       si.company AS company,
       MIN(si.posting_date) AS day,
       MIN(si.creation) AS created
FROM `tabSales Invoice Item` sii
JOIN `tabSales Invoice` si ON si.name = sii.parent
WHERE si.docstatus = 1
  AND si.company = %(company)s
  AND si.posting_date >= %(window_start)s
  AND sii.warehouse IS NOT NULL AND sii.warehouse != ''
  AND sii.cost_center IS NOT NULL AND sii.cost_center != ''
GROUP BY sii.parent, sii.warehouse, si.company
LIMIT {HARD_ROW_LIMIT}
"""

# --- S1: JE voucher type -> creation-from-template habit (unit = distinct JE) -
# '__manual__' (no template) is the vanilla default, suppressed via
# org_default_consequent on the spec; the postprocess adds the org-level
# voucher-type habit from the same scan.
JE_USAGE_SQL = f"""
SELECT je.name AS unit_id,
       je.voucher_type AS antecedent,
       CASE WHEN je.from_template IS NOT NULL AND je.from_template != ''
            THEN je.from_template ELSE '__manual__' END AS consequent,
       je.company AS company,
       je.posting_date AS day,
       je.creation AS created
FROM `tabJournal Entry` je
WHERE je.docstatus = 1
  AND je.company = %(company)s
  AND je.posting_date >= %(window_start)s
  AND je.voucher_type IS NOT NULL AND je.voucher_type != ''
LIMIT {HARD_ROW_LIMIT}
"""

# --- S2: item group -> deferred revenue/expense realized share ----------------
# Unit = (invoice, item group); a unit is 'deferred' only when EVERY line of
# that group on the invoice books deferred. The kind is baked into the
# antecedent so revenue and expense dedupe under distinct pattern keys.
DEFERRED_REVENUE_SQL = f"""
SELECT CONCAT(sii.parent, '::', sii.item_group) AS unit_id,
       CONCAT(sii.item_group, ' :: revenue') AS antecedent,
       CASE WHEN MIN(sii.enable_deferred_revenue) = 1 THEN 'deferred'
            ELSE '__immediate__' END AS consequent,
       si.company AS company,
       MIN(si.posting_date) AS day,
       MIN(si.creation) AS created
FROM `tabSales Invoice Item` sii
JOIN `tabSales Invoice` si ON si.name = sii.parent
WHERE si.docstatus = 1
  AND si.company = %(company)s
  AND si.posting_date >= %(window_start)s
  AND sii.item_group IS NOT NULL AND sii.item_group != ''
GROUP BY sii.parent, sii.item_group, si.company
LIMIT {HARD_ROW_LIMIT}
"""

DEFERRED_EXPENSE_SQL = f"""
SELECT CONCAT(pii.parent, '::', pii.item_group) AS unit_id,
       CONCAT(pii.item_group, ' :: expense') AS antecedent,
       CASE WHEN MIN(pii.enable_deferred_expense) = 1 THEN 'deferred'
            ELSE '__immediate__' END AS consequent,
       pi.company AS company,
       MIN(pi.posting_date) AS day,
       MIN(pi.creation) AS created
FROM `tabPurchase Invoice Item` pii
JOIN `tabPurchase Invoice` pi ON pi.name = pii.parent
WHERE pi.docstatus = 1
  AND pi.company = %(company)s
  AND pi.posting_date >= %(window_start)s
  AND pii.item_group IS NOT NULL AND pii.item_group != ''
GROUP BY pii.parent, pii.item_group, pi.company
LIMIT {HARD_ROW_LIMIT}
"""

# --- acct-party-tax-template: custom-field pre-pass + segment SQL --------------
# Discovery is a static SELECT over `tabCustom Field`; candidate fieldnames are
# validated (regex + meta probe) BEFORE any interpolation, so the segment SQL
# below stays effectively static (the only interpolated token is a validated
# column identifier; everything else is %(param)s).
CUSTOM_SEGMENT_FIELDS_SQL = """
SELECT cf.dt AS dt, cf.fieldname AS fieldname, cf.fieldtype AS fieldtype, cf.label AS label
FROM `tabCustom Field` cf
WHERE cf.dt IN ('Customer', 'Supplier')
  AND cf.fieldtype IN ('Select', 'Link', 'Data')
ORDER BY cf.dt, cf.fieldname
LIMIT 50
"""

# Frappe fieldnames are lowercase snake_case; anything else is refused before
# interpolation (static-SQL guarantee).
_FIELDNAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,139}$")
_MAX_SEGMENT_FIELDS_PER_DOCTYPE = 5

# Cardinality/selectivity gates for discovered segment fields: a segment must
# partition parties into GROUPS. A near-unique Data/Link column (gstin/pan/
# legacy-code-shaped identifiers) is party identity, not a segment, and its
# raw value must never surface in a pattern.
_MIN_SEGMENT_DISTINCT = 2
_MAX_SEGMENT_DISTINCT = 50
_MAX_SEGMENT_SELECTIVITY = 0.5  # Link/Data: distinct values per filled party

# A segment value backed by a single party is that party's identity, not a
# segment habit; require at least this many distinct parties per value.
_MIN_SEGMENT_PARTIES = 2

# Geography-confound sample floors: a state bucket holding one party is 100%
# "pure" by construction and carries no confound signal, so tiny buckets must
# not count toward the confound verdict.
_MIN_STATE_PARTIES = 2
_MIN_STATE_UNITS = 5

# Active Tax Rules already encode template routing; anything they map is
# enforced configuration, not a habit (plan section 4.2 cross-ref). Both date
# bounds are honored: a future-dated rule is not yet enforcing anything.
TAX_RULE_SQL = """
SELECT tr.name AS name,
       tr.company AS company,
       tr.sales_tax_template AS sales_tax_template,
       tr.purchase_tax_template AS purchase_tax_template
FROM `tabTax Rule` tr
WHERE (tr.from_date IS NULL OR tr.from_date <= %(today)s)
  AND (tr.to_date IS NULL OR tr.to_date >= %(today)s)
LIMIT 500
"""

_GEOGRAPHY_CAVEAT = "may reflect party geography (state), not a segment habit"


def _customer_segment_sql(fieldname: str) -> str:
	"""Segment SQL for a VALIDATED Customer custom field (see module docstring).
	The %(seg_prefix)s param bakes 'Customer.<field>=' into the antecedent so
	segments from different discovered fields dedupe under distinct keys."""
	return f"""
SELECT si.name AS unit_id,
       CONCAT(%(seg_prefix)s, cust.`{fieldname}`) AS antecedent,
       si.taxes_and_charges AS consequent,
       cust.name AS party,
       si.company AS company,
       si.posting_date AS day,
       si.creation AS created
FROM `tabSales Invoice` si
JOIN `tabCustomer` cust ON cust.name = si.customer
WHERE si.docstatus = 1
  AND si.company = %(company)s
  AND si.posting_date >= %(window_start)s
  AND si.taxes_and_charges IS NOT NULL AND si.taxes_and_charges != ''
  AND cust.`{fieldname}` IS NOT NULL AND cust.`{fieldname}` != ''
LIMIT {HARD_ROW_LIMIT}
"""


def _supplier_segment_sql(fieldname: str) -> str:
	"""Segment SQL for a VALIDATED Supplier custom field (realized purchase
	side; Purchase Invoice headers)."""
	return f"""
SELECT pi.name AS unit_id,
       CONCAT(%(seg_prefix)s, sup.`{fieldname}`) AS antecedent,
       pi.taxes_and_charges AS consequent,
       sup.name AS party,
       pi.company AS company,
       pi.posting_date AS day,
       pi.creation AS created
FROM `tabPurchase Invoice` pi
JOIN `tabSupplier` sup ON sup.name = pi.supplier
WHERE pi.docstatus = 1
  AND pi.company = %(company)s
  AND pi.posting_date >= %(window_start)s
  AND pi.taxes_and_charges IS NOT NULL AND pi.taxes_and_charges != ''
  AND sup.`{fieldname}` IS NOT NULL AND sup.`{fieldname}` != ''
LIMIT {HARD_ROW_LIMIT}
"""


_PARTY_PASSES = {
	"Customer": {"sql": _customer_segment_sql, "tax_kind": "sales", "rule_field": "sales_tax_template"},
	"Supplier": {"sql": _supplier_segment_sql, "tax_kind": "purchase", "rule_field": "purchase_tax_template"},
}


def _segment_field_profile_sql(dt: str, fieldname: str) -> str:
	"""Cardinality probe for a VALIDATED (whitelisted doctype, regex + meta
	checked fieldname) pair - same interpolation contract as the segment SQL."""
	return (
		"SELECT COUNT(*) AS total, "
		f"COUNT(NULLIF(`{fieldname}`, '')) AS filled, "
		f"COUNT(DISTINCT NULLIF(`{fieldname}`, '')) AS distinct_values "
		f"FROM `tab{dt}`"
	)


def _profile_segment_field(patterndb, dt: str, fieldname: str) -> dict | None:
	"""total/filled/distinct counts for a candidate field, or None when the
	probe is unavailable (the field then competes with a zero score)."""
	try:
		rows = patterndb.sql_select(_segment_field_profile_sql(dt, fieldname), {})
	except Exception:
		return None
	if not rows:
		return None
	r = rows[0]
	return {
		"total": int(r.get("total") or 0),
		"filled": int(r.get("filled") or 0),
		"distinct": int(r.get("distinct_values") or 0),
	}


def _segment_field_score(fieldtype: str, profile: dict | None) -> float | None:
	"""Usefulness score (fill rate x bounded cardinality) for ranking, or None
	when the profile disqualifies the field. Select fields carry a bounded
	option list by construction; Link/Data must additionally be non-selective
	(distinct small relative to the filled party count) so identifier-shaped
	columns never become 'segments'. An unavailable profile keeps the field at
	a zero score (ranked last) - the multi-party guard downstream still blocks
	identity-shaped output."""
	if profile is None:
		return 0.0
	distinct, filled, total = profile["distinct"], profile["filled"], profile["total"]
	if distinct < _MIN_SEGMENT_DISTINCT or distinct > _MAX_SEGMENT_DISTINCT:
		return None  # constant column or unbounded cardinality: no grouping
	if fieldtype != "Select" and distinct > _MAX_SEGMENT_SELECTIVITY * filled:
		return None  # near-unique per party: identity, not a segment
	fill_rate = (filled / total) if total else 0.0
	return fill_rate * (1.0 - distinct / (_MAX_SEGMENT_DISTINCT + 1.0))


def _discover_segment_fields(patterndb) -> list[dict]:
	"""Custom-field pre-pass: candidate segment columns on the party doctypes,
	regex + meta validated, cardinality/selectivity gated, then ranked by
	usefulness (Select first, fill rate x bounded cardinality) before the
	per-doctype cap. Never hardcodes GST names."""
	from jarvis.learning import compat

	try:
		rows = patterndb.sql_select(CUSTOM_SEGMENT_FIELDS_SQL, {})
	except Exception:
		return []
	ranked: dict = defaultdict(list)
	for r in rows or []:
		dt, fieldname, fieldtype = r.get("dt"), r.get("fieldname"), r.get("fieldtype")
		if dt not in _PARTY_PASSES:
			continue
		if not fieldname or not _FIELDNAME_RE.fullmatch(str(fieldname)):
			continue  # refuse anything that is not a plain snake_case identifier
		if not compat.has_field(dt, fieldname):
			continue  # meta/schema probe: the row must describe a real column
		score = _segment_field_score(
			str(fieldtype or ""), _profile_segment_field(patterndb, dt, str(fieldname))
		)
		if score is None:
			continue
		ranked[dt].append(
			(
				(0 if fieldtype == "Select" else 1, -score, str(fieldname)),
				{
					"dt": dt,
					"fieldname": str(fieldname),
					"label": (r.get("label") or str(fieldname)),
				},
			)
		)
	out: list[dict] = []
	for dt in sorted(ranked):
		best = sorted(ranked[dt], key=lambda kv: kv[0])
		out.extend(item for _key, item in best[:_MAX_SEGMENT_FIELDS_PER_DOCTYPE])
	return out


def _normalize_state_map(states: dict) -> dict:
	"""Address.state is free text; trim + casefold so spelling variants of one
	real state neither fragment the buckets nor inflate the state count."""
	out: dict = {}
	for party, state in (states or {}).items():
		norm = str(state or "").strip().casefold()
		if norm:
			out[party] = norm
	return out


def _filter_confound_states(states: dict, template_by_party: dict, units_by_party: dict) -> dict:
	"""Keep only parties whose state bucket covers >= _MIN_STATE_PARTIES
	distinct parties AND >= _MIN_STATE_UNITS units, so the purity predicate
	only sees buckets that actually carry signal (a one-party state is pure by
	construction and must never contribute to the confound verdict)."""
	by_state: dict = defaultdict(list)
	for party, state in (states or {}).items():
		if (template_by_party or {}).get(party):
			by_state[state].append(party)
	keep = set()
	for state, parties in by_state.items():
		units = sum(int(units_by_party.get(p) or 0) for p in parties)
		if len(parties) >= _MIN_STATE_PARTIES and units >= _MIN_STATE_UNITS:
			keep.add(state)
	return {p: s for p, s in (states or {}).items() if s in keep}


def _encoded_templates(patterndb, company, rule_field: str) -> set:
	"""Templates an ACTIVE Tax Rule (org-wide or this company) already routes
	to; candidates proposing one of these are suppressed."""
	import frappe

	try:
		rules = patterndb.sql_select(TAX_RULE_SQL, {"today": frappe.utils.today()})
	except Exception:
		return set()
	out = set()
	for r in rules or []:
		template = r.get(rule_field)
		if not template:
			continue
		rule_company = r.get("company")
		if rule_company and company and rule_company != company:
			continue
		out.add(template)
	return out


def postprocess_party_tax_template(rows, spec, company, patterndb, params):
	"""acct-party-tax-template (plan section 4.2): discovered party segments ->
	Sales/Purchase tax template, Tax-Rule cross-referenced and geography
	guarded. See the module docstring for the full contract."""
	from jarvis.learning.executor import (
		_demote_band,
		_party_state_map,
		_state_predicts_template,
		reduce_units,
	)

	fields = _discover_segment_fields(patterndb)
	if not fields:
		return []

	out = []
	for field in fields:
		dt, fieldname, label = field["dt"], field["fieldname"], field["label"]
		pass_cfg = _PARTY_PASSES[dt]
		seg_prefix = f"{dt}.{fieldname}="
		pass_params = dict(params or {})
		pass_params["seg_prefix"] = seg_prefix
		try:
			pass_rows = patterndb.timed_select(pass_cfg["sql"](fieldname), pass_params) or []
		except Exception:
			continue
		if not pass_rows:
			continue

		raws = reduce_units(pass_rows, spec, patterndb)
		if not raws:
			continue

		# Multi-party guard: a segment value backed by fewer than
		# _MIN_SEGMENT_PARTIES distinct parties is party identity dressed as a
		# segment (and its raw value - a gstin/pan/name-shaped identifier -
		# must never surface in a pattern).
		seg_parties: dict = defaultdict(set)
		for r in pass_rows:
			if r.get("antecedent") and r.get("party"):
				seg_parties[r["antecedent"]].add(r["party"])
		raws = [
			r for r in raws if len(seg_parties.get(r.get("antecedent_value"), ())) >= _MIN_SEGMENT_PARTIES
		]
		if not raws:
			continue

		# Tax Rule cross-ref: propose only what no active rule encodes.
		encoded = _encoded_templates(patterndb, company, pass_cfg["rule_field"])
		raws = [r for r in raws if r.get("consequent_value") not in encoded]
		if not raws:
			continue

		# Geography-confound guard (reuses the executor's state helpers): the
		# party -> modal-template map feeds _state_predicts_template; a
		# confound demotes the band, and the caveat is stamped regardless
		# (the compiled text always carries the warning via the template).
		# States are normalized (free text) and buckets below the per-state
		# party/unit floors are dropped before the purity predicate runs.
		per_party: dict = defaultdict(lambda: defaultdict(int))
		for r in pass_rows:
			party, cons = r.get("party"), r.get("consequent")
			if party and cons:
				per_party[party][cons] += 1
		template_by_party = {
			p: max(c.items(), key=lambda kv: (kv[1], str(kv[0])))[0] for p, c in per_party.items()
		}
		units_by_party = {p: sum(c.values()) for p, c in per_party.items()}
		states = _normalize_state_map(_party_state_map(patterndb, dt, list(template_by_party)))
		states = _filter_confound_states(states, template_by_party, units_by_party)
		confound = _state_predicts_template(states, template_by_party)

		for raw in raws:
			segment_value = str(raw.get("antecedent_value") or "")
			if segment_value.startswith(seg_prefix):
				segment_value = segment_value[len(seg_prefix) :]
			raw["skill_bullet_vars"] = {
				"party_doctype": dt,
				"field_label": label,
				"segment_value": segment_value,
				"tax_template": raw.get("consequent_value"),
				"tax_kind": pass_cfg["tax_kind"],
			}
			evidence = raw.get("evidence")
			if not isinstance(evidence, dict):
				evidence = {}
				raw["evidence"] = evidence
			evidence["segment_field"] = f"{dt}.{fieldname}"
			evidence["geography_caveat"] = _GEOGRAPHY_CAVEAT
			if confound:
				evidence["geography_confound"] = "state predicts template"
				raw["band"] = _demote_band(raw.get("band"))
			out.append(raw)
	return out


def _je_voucher_type_default() -> str:
	"""Framework default for Journal Entry.voucher_type: the org-level card is
	a tautology when the modal type merely equals the default every manual JE
	starts with. Meta is the truth source, with ERPNext's shipped 'Journal
	Entry' as the fallback."""
	try:
		import frappe

		field = frappe.get_meta("Journal Entry").get_field("voucher_type")
		default = getattr(field, "default", None) if field else None
		if default:
			return str(default)
	except Exception:
		pass
	return "Journal Entry"


def postprocess_je_usage(rows, spec, company, patterndb, params):
	"""acct-je-usage (plan section 4.2): per-voucher-type template habits via
	the generic reduce, plus ONE org-level voucher-type habit from the same
	scan (both A-class, no parties, no amounts). The org card is suppressed
	when the modal voucher type is just the framework default (tautology
	guard, mirroring org_default_consequent on the per-type cards)."""
	from jarvis.learning.executor import evaluate_segment, month_key, reduce_units

	rows = rows or []
	out = reduce_units(rows, spec, patterndb)

	units = {}
	for r in rows:
		uid, voucher_type = r.get("unit_id"), r.get("antecedent")
		if uid and voucher_type:
			units[uid] = (voucher_type, r.get("day"), r.get("created"))
	if not units:
		return out

	counts: dict = defaultdict(int)
	for voucher_type, _d, _c in units.values():
		counts[voucher_type] += 1
	mode = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))[0]
	if str(mode) == _je_voucher_type_default():
		return out  # "Journal entries are usually 'Journal Entry' entries"
	exceptions = [
		{"unit": uid, "value": vt, "month": month_key(d)} for uid, (vt, d, _c) in units.items() if vt != mode
	][:20]
	raw = evaluate_segment(
		spec,
		antecedent_value="org:voucher_type",
		consequent_value=mode,
		k=counts[mode],
		n_units=len(units),
		base_rate=0.0,
		days=[d for _v, d, _c in units.values()],
		created=[c for _v, _d, c in units.values()],
		exceptions=exceptions,
		single_antecedent=True,
		names_party=False,
		template="je-voucher-type",
		vars={"voucher_type": mode},
		extra_evidence={"voucher_type_mix": dict(counts)},
		patterndb=patterndb,
	)
	if raw:
		out.append(raw)
	return out


def postprocess_deferred_usage(rows, spec, company, patterndb, params):
	"""acct-deferred-usage (plan section 4.2): realized deferred share by item
	group, one reduce pass per side (revenue on SI, expense on PI) so each
	side's variance gate and base rate stay clean."""
	from jarvis.learning.executor import reduce_units

	out = []
	for sql, kind in ((DEFERRED_REVENUE_SQL, "revenue"), (DEFERRED_EXPENSE_SQL, "expense")):
		try:
			pass_rows = patterndb.timed_select(sql, params)
		except Exception:
			continue
		raws = reduce_units(pass_rows, spec, patterndb)
		for raw in raws:
			group = str(raw.get("antecedent_value") or "").split(" :: ", 1)[0]
			raw["skill_bullet_vars"] = {"item_group": group, "kind": kind}
			evidence = raw.get("evidence")
			if isinstance(evidence, dict):
				evidence["deferred_kind"] = kind
			out.append(raw)
	return out
