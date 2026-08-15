"""Masters/Config detector postprocess (plan section 4.2, Masters/Config).

The Tier-1 config detectors are org-wide (company=None) and multi-source, so
they live entirely in postprocess functions rather than a single SQL constant:

  * cfg-naming-series merges three sources per doctype - realized usage (the
    naming_series column on recent documents), the configured options
    (DocType meta + Property Setter), and tabSeries current counters.
  * cfg-default-vs-usage compares a configured default (per plan section 4.2,
    Price List comes from the Selling Settings single, which has no is_default
    field) against realized usage, and proposes only on divergence.

Tier-2 org detectors (plan section 4.4):

  * cfg-custom-field-always-filled - a non-mandatory custom field that is
    filled on nearly every submitted row is mandatory in practice. Discovered
    identifiers (doctype + fieldname) are regex + meta validated BEFORE any
    interpolation, so the fill SQL keeps the static-SQL guarantee.
  * role-doctype-routing - which role actually creates/submits each core
    doctype (owner JOIN `tabHas Role`, fractional multi-role weights). Org and
    role level ONLY: user names never reach a candidate.
"""

from __future__ import annotations

import re
from collections import defaultdict

# Hard per-detector row cap (plan §4.3 fix 7): a curated OOM backstop for the
# unit-grain usage SQL; the nightly row budget pauses across detectors.
HARD_ROW_LIMIT = 200000

# Current-era doctypes whose naming series is worth learning. Fixed whitelist:
# the table name is interpolated, so it must never come from user input.
NAMING_SERIES_DOCTYPES = (
	"Sales Invoice",
	"Sales Order",
	"Purchase Order",
	"Purchase Invoice",
	"Quotation",
	"Delivery Note",
	"Payment Entry",
	"Journal Entry",
)

_PROPERTY_SETTER_SQL = """
SELECT value AS options
FROM `tabProperty Setter`
WHERE doc_type = %(dt)s AND field_name = 'naming_series' AND property = 'options'
LIMIT 1
"""

_SERIES_CURRENT_SQL = """
SELECT name AS series, current AS current
FROM `tabSeries`
WHERE name = %(prefix)s
LIMIT 1
"""

# Realized price-list usage, org-wide (unit = distinct Sales Invoice).
DEFAULT_PRICE_LIST_USAGE_SQL = f"""
SELECT si.name AS unit_id,
       si.selling_price_list AS consequent,
       si.posting_date AS day,
       si.creation AS created
FROM `tabSales Invoice` si
WHERE si.docstatus = 1
  AND si.posting_date >= %(window_start)s
  AND si.selling_price_list IS NOT NULL AND si.selling_price_list != ''
LIMIT {HARD_ROW_LIMIT}
"""


def _usage_sql(table: str) -> str:
	return (
		"SELECT `naming_series` AS consequent, name AS unit_id, "
		"DATE(creation) AS day, creation AS created "
		f"FROM `tab{table}` "
		"WHERE naming_series IS NOT NULL AND naming_series != '' "
		"AND creation >= %(window_start)s AND docstatus <= 1 "
		f"LIMIT {HARD_ROW_LIMIT}"
	)


def _series_prefix(series: str) -> str:
	"""tabSeries stores the counter under the literal prefix (the part before
	the hash placeholder), e.g. 'ACC-SINV-.YYYY.-' -> 'ACC-SINV-.YYYY.-' with
	the '#' run stripped. We take everything up to the first '#'."""
	return (series or "").split("#", 1)[0]


def postprocess_naming_series(rows, spec, company, patterndb, params):
	"""Merge realized naming-series usage with configured options + tabSeries
	current counters, one candidate per doctype with a dominant series."""
	from jarvis.learning import compat
	from jarvis.learning.executor import evaluate_segment

	out = []
	for doctype in NAMING_SERIES_DOCTYPES:
		if not compat.has_field(doctype, "naming_series"):
			continue
		try:
			usage = patterndb.timed_select(_usage_sql(doctype), params)
		except Exception:
			continue
		units = {}
		for r in usage or []:
			series = r.get("consequent")
			if not series:
				continue
			units[r.get("unit_id")] = (series, r.get("day"), r.get("created"))
		if not units:
			continue
		counts: dict = defaultdict(int)
		for series, _d, _c in units.values():
			counts[series] += 1
		mode = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))[0]

		configured = _configured_options(doctype, patterndb)
		current = _series_current(patterndb, mode)
		raw = evaluate_segment(
			spec,
			antecedent_value=doctype,
			consequent_value=mode,
			k=counts[mode],
			n_units=len(units),
			base_rate=0.0,
			days=[d for _s, d, _c in units.values()],
			created=[c for _s, _d, c in units.values()],
			single_antecedent=True,
			names_party=False,
			template="naming-series",
			vars={"doctype": doctype, "series": mode},
			extra_evidence={
				"configured_options": configured,
				"series_current": current,
				"used_series_share": round(counts[mode] / len(units), 4),
			},
			patterndb=patterndb,
		)
		if raw:
			out.append(raw)
	return out


def postprocess_default_vs_usage(rows, spec, company, patterndb, params):
	"""cfg-default-vs-usage (Price List baseline): the configured default lives
	on the Selling Settings single; propose only when realized usage diverges
	from it."""
	from jarvis.learning.executor import evaluate_segment, month_key, singles_value

	configured = singles_value(patterndb, "Selling Settings", "selling_price_list")
	if not configured:
		return []
	usage = patterndb.timed_select(DEFAULT_PRICE_LIST_USAGE_SQL, params)
	units = {}
	for r in usage or []:
		value = r.get("consequent")
		if not value:
			continue
		units[r.get("unit_id")] = (value, r.get("day"), r.get("created"))
	if not units:
		return []
	counts: dict = defaultdict(int)
	for value, _d, _c in units.values():
		counts[value] += 1
	mode = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))[0]
	if str(mode) == str(configured):
		return []  # realized usage matches config -> nothing to reconcile

	exceptions = [
		{"unit": uid, "value": v, "month": month_key(d)} for uid, (v, d, _c) in units.items() if v != mode
	][:20]
	raw = evaluate_segment(
		spec,
		antecedent_value="selling_price_list",
		consequent_value=mode,
		k=counts[mode],
		n_units=len(units),
		base_rate=0.0,
		days=[d for _v, d, _c in units.values()],
		created=[c for _v, _d, c in units.values()],
		exceptions=exceptions,
		single_antecedent=True,
		names_party=False,
		template="default-vs-usage",
		vars={"field_label": "price list", "used": mode, "configured": configured},
		extra_evidence={"configured_default": configured},
		patterndb=patterndb,
	)
	return [raw] if raw else []


def _configured_options(doctype: str, patterndb) -> list:
	"""Configured naming_series options: Property Setter override wins, else the
	shipped DocType meta options."""
	try:
		ps = patterndb.sql_select(_PROPERTY_SETTER_SQL, {"dt": doctype})
		if ps and ps[0].get("options"):
			return [o for o in str(ps[0]["options"]).splitlines() if o.strip()]
	except Exception:
		pass
	try:
		import frappe

		field = frappe.get_meta(doctype).get_field("naming_series")
		if field and field.options:
			return [o for o in str(field.options).splitlines() if o.strip()]
	except Exception:
		pass
	return []


def _series_current(patterndb, mode_series: str):
	prefix = _series_prefix(mode_series)
	if not prefix:
		return None
	try:
		row = patterndb.sql_select(_SERIES_CURRENT_SQL, {"prefix": prefix})
		return row[0].get("current") if row else None
	except Exception:
		return None


# ---------------------------------------------------------------------------
# Tier-2: cfg-custom-field-always-filled
# ---------------------------------------------------------------------------
# Textual fieldtypes only: their columns are varchar/text, so the emptiness
# CASE below never trips MariaDB strict-mode date/number coercion. Fields with
# a configured default are excluded: the framework auto-fills them, so a ~100%
# fill rate reflects configuration, not a user habit.
CUSTOM_FIELD_CANDIDATES_SQL = """
SELECT cf.dt AS dt, cf.fieldname AS fieldname, cf.fieldtype AS fieldtype, cf.label AS label,
       cf.`default` AS field_default
FROM `tabCustom Field` cf
WHERE cf.reqd = 0
  AND cf.hidden = 0
  AND (cf.`default` IS NULL OR cf.`default` = '')
  AND cf.fieldtype IN ('Data', 'Select', 'Link', 'Small Text', 'Text', 'Long Text', 'Phone', 'Autocomplete')
ORDER BY cf.dt, cf.fieldname
LIMIT 200
"""

# Frappe fieldnames are lowercase snake_case and doctype names are plain words;
# anything else is refused BEFORE interpolation (static-SQL guarantee).
_CF_FIELDNAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,139}$")
_CF_DOCTYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 \-]{0,138}$")
_MAX_CUSTOM_FIELDS_PROBED = 40


def _fill_rate_sql(doctype: str, fieldname: str) -> str:
	"""Fill-rate SQL for a VALIDATED (doctype, fieldname) pair - the caller
	MUST have passed both through the regex + meta checks first."""
	return (
		"SELECT name AS unit_id, "
		f"CASE WHEN `{fieldname}` IS NULL OR `{fieldname}` = '' THEN '__empty__' "
		"ELSE '__filled__' END AS consequent, "
		"DATE(creation) AS day, creation AS created "
		f"FROM `tab{doctype}` "
		"WHERE docstatus = 1 AND creation >= %(window_start)s "
		f"LIMIT {HARD_ROW_LIMIT}"
	)


def _validated_custom_field(row) -> tuple[str, str] | None:
	"""(doctype, fieldname) iff the discovered Custom Field row survives the
	regex + meta validation contract (plan section 4.2): snake_case fieldname,
	plain doctype name, a real submittable non-child doctype, and the column
	actually present per meta/schema probe. None otherwise."""
	import frappe

	from jarvis.learning import compat

	doctype, fieldname = row.get("dt"), row.get("fieldname")
	if not doctype or not fieldname:
		return None
	if not _CF_DOCTYPE_RE.fullmatch(str(doctype)):
		return None
	if not _CF_FIELDNAME_RE.fullmatch(str(fieldname)):
		return None
	try:
		meta = frappe.get_meta(doctype)
	except Exception:
		return None
	# "submitted rows" (plan section 4.2): fill rate is measured on docstatus=1
	# parents, so child tables and non-submittable masters are out of scope.
	if meta.istable or not meta.is_submittable:
		return None
	if not compat.has_field(doctype, fieldname):
		return None
	return (str(doctype), str(fieldname))


def postprocess_custom_field_always_filled(rows, spec, company, patterndb, params):
	"""cfg-custom-field-always-filled (plan section 4.2): reqd=0 custom fields
	whose fill rate on submitted rows clears the 60/0.98 gate are mandatory in
	practice. One candidate per surviving field."""
	from jarvis.learning.executor import evaluate_segment, month_key

	try:
		candidates = patterndb.sql_select(CUSTOM_FIELD_CANDIDATES_SQL, {})
	except Exception:
		return []

	out = []
	probed = 0
	for row in candidates or []:
		if probed >= _MAX_CUSTOM_FIELDS_PROBED:
			break
		if str(row.get("field_default") or "").strip():
			continue  # configured default: the framework fills it, not users
		validated = _validated_custom_field(row)
		if not validated:
			continue
		doctype, fieldname = validated
		probed += 1
		try:
			fill_rows = patterndb.timed_select(_fill_rate_sql(doctype, fieldname), params)
		except Exception:
			continue
		units = {}
		for r in fill_rows or []:
			if r.get("unit_id"):
				units[r["unit_id"]] = (r.get("consequent"), r.get("day"), r.get("created"))
		if not units:
			continue
		k = sum(1 for cons, _d, _c in units.values() if cons == "__filled__")
		if not k:
			continue
		exceptions = [
			{"unit": uid, "value": "empty", "month": month_key(d)}
			for uid, (cons, d, _c) in units.items()
			if cons != "__filled__"
		][:20]
		raw = evaluate_segment(
			spec,
			antecedent_value=f"{doctype}.{fieldname}",
			consequent_value="filled",
			k=k,
			n_units=len(units),
			base_rate=0.0,
			days=[d for _v, d, _c in units.values()],
			created=[c for _v, _d, c in units.values()],
			exceptions=exceptions,
			single_antecedent=True,
			names_party=False,
			template="custom-field-always-filled",
			vars={
				"doctype": doctype,
				"fieldname": fieldname,
				"label": row.get("label") or fieldname,
			},
			extra_evidence={
				"fill_rate": round(k / len(units), 4),
				"fieldtype": row.get("fieldtype"),
				"reqd": 0,
			},
			patterndb=patterndb,
		)
		if raw:
			out.append(raw)
	return out


# ---------------------------------------------------------------------------
# Tier-2: role-doctype-routing
# ---------------------------------------------------------------------------
# Fixed doctype whitelist (interpolation-safe, same contract as
# NAMING_SERIES_DOCTYPES; extend deliberately, never from user input).
ROUTING_DOCTYPES = NAMING_SERIES_DOCTYPES

# Blanket roles every desk user tends to hold carry no routing signal; real
# functional roles (incl. System Manager) stay in and the leave-segment-out
# gap gate kills any role that dominates EVERY doctype equally. That defense
# only exists with a "rest" to compare against, so the postprocess requires
# >= 2 doctypes with qualifying data before proposing anything.
_GENERIC_ROLES = frozenset({"All", "Guest", "Desk User", "System User"})
_MIN_ROUTING_DOCTYPES = 2

_HAS_ROLE_SQL = """
SELECT hr.parent AS user, hr.role AS role
FROM `tabHas Role` hr
JOIN `tabRole` r ON r.name = hr.role
WHERE hr.parenttype = 'User'
  AND r.disabled = 0
  AND r.desk_access = 1
  AND hr.parent IN %(users)s
LIMIT 100000
"""

_HAS_ROLE_CHUNK = 1000


def _owner_usage_sql(table: str) -> str:
	"""Owner-per-document rows for one WHITELISTED doctype (see
	ROUTING_DOCTYPES; the table name is never user input)."""
	return (
		"SELECT name AS unit_id, owner AS owner, DATE(creation) AS day, creation AS created "
		f"FROM `tab{table}` "
		"WHERE docstatus = 1 AND creation >= %(window_start)s "
		"AND owner IS NOT NULL AND owner NOT IN ('Administrator', 'Guest') "
		f"LIMIT {HARD_ROW_LIMIT}"
	)


def postprocess_role_doctype_routing(rows, spec, company, patterndb, params):
	"""role-doctype-routing (plan section 4.2): which role ACTUALLY creates
	each core doctype. A document by a multi-role owner contributes 1/k weight
	to each of the owner's k functional roles (fractional weights), so a
	blended-role user never double-counts. Output is org/role level only -
	no user ever appears in a candidate."""
	from jarvis.learning import compat, stats
	from jarvis.learning.executor import evaluate_segment

	per_doctype: dict = {}
	all_users: set = set()
	for doctype in ROUTING_DOCTYPES:
		if not compat.has_field(doctype, "owner"):
			continue
		try:
			doc_rows = patterndb.timed_select(_owner_usage_sql(doctype), params)
		except Exception:
			continue
		if doc_rows:
			per_doctype[doctype] = doc_rows
			all_users.update(r.get("owner") for r in doc_rows if r.get("owner"))
	if not per_doctype:
		return []

	role_map: dict = defaultdict(list)
	users = sorted(all_users)
	for i in range(0, len(users), _HAS_ROLE_CHUNK):
		chunk = users[i : i + _HAS_ROLE_CHUNK]
		try:
			role_rows = patterndb.sql_select(_HAS_ROLE_SQL, {"users": chunk}) or []
		except Exception:
			continue
		for r in role_rows:
			if r.get("user") and r.get("role") and r["role"] not in _GENERIC_ROLES:
				role_map[r["user"]].append(r["role"])

	counts: dict = {}
	meta: dict = {}
	for doctype, doc_rows in per_doctype.items():
		weights: dict = defaultdict(float)
		days, created = [], []
		n_docs = 0
		for r in doc_rows:
			roles = role_map.get(r.get("owner")) or []
			if not roles:
				continue  # unattributable owner: excluded from the unit count
			w = 1.0 / len(roles)
			for role in roles:
				weights[role] += w
			n_docs += 1
			days.append(r.get("day"))
			created.append(r.get("created"))
		rounded = {role: int(round(v)) for role, v in weights.items() if int(round(v)) > 0}
		if not rounded or not n_docs:
			continue
		counts[doctype] = rounded
		meta[doctype] = (days, created, n_docs)

	if len(counts) < _MIN_ROUTING_DOCTYPES:
		# With a single qualifying doctype there is no "rest" for the
		# leave-segment-out base rate and the gap gate would be skipped, so a
		# role that dominates the org's only active doctype (often System
		# Manager on a small site) would propose as misleading routing.
		return []

	out = []
	for doctype in counts:
		lsob = stats.leave_segment_out_base_rate(counts, doctype)
		mode = lsob["consequent"]
		if not mode:
			continue
		days, created, n_docs = meta[doctype]
		raw = evaluate_segment(
			spec,
			antecedent_value=doctype,
			consequent_value=mode,
			k=lsob["k"],
			n_units=lsob["n_units"],
			base_rate=lsob["base_rate"],
			rest_k=lsob["rest_k"],
			rest_n=lsob["rest_n"],
			days=days,
			created=created,
			exceptions=[],
			single_antecedent=False,
			names_party=False,
			template="role-doctype-routing",
			vars={"doctype": doctype, "role": mode},
			extra_evidence={
				"weighting": "fractional multi-role (owner JOIN Has Role)",
				"n_documents": n_docs,
			},
			patterndb=patterndb,
		)
		if raw:
			out.append(raw)
	return out
