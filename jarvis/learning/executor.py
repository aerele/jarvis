"""Detector executor: turns a declarative spec + a company into candidate
proposals (plan sections 4.1, 4.3).

Deterministic, no LLM. The executor is the ONLY place §4.1's gate suite is
applied to real rows: it counts INDEPENDENT UNITS (never child rows), runs
leave-segment-out base rate + Wilson bands + gap / variance / spread / burst
gates, and only then emits a candidate. It receives a :class:`PatternDB`
(SELECT-only facade); every read - the detector SQL, master/default lookups,
and the enforcement cross-ref - goes through that facade so the engine's
READ ONLY transaction contains the whole pass.

Contract (documented for the engine's ``_persist_candidate``):

``run_detector(spec, company, patterndb) -> DetectorResult`` where
``DetectorResult(candidates: list[dict], skipped_reason: str | None)``. A
non-None ``skipped_reason`` means the detector could NOT run (missing app /
field, unavailable source, single-plant guard) - the engine records it as a
skip so a partial run never reads as a clean pass. An empty ``candidates``
list with ``skipped_reason=None`` means the detector ran and found nothing
that survived the gates (a legitimate clean pass).

Each candidate dict (see :func:`_finalize`) carries every ``Jarvis Learned
Pattern`` field the engine writes plus ``antecedent_value`` /
``consequent_value`` bookkeeping and stamped detector/registry versions.

Phase 2 statistics upgrade: candidates from the standard reduce also carry a
one-sided enrichment ``p_value`` (Fisher's exact when any expected 2x2 cell
is < 5 per Cochran, else G-test; also mirrored into ``evidence`` with its
``p_value_method``). The per-detector-family BH-FDR pass over these p-values
lives in ``jarvis.learning.fdr`` - ADDITIVE after every gate here, never a
replacement. ``p_value`` is None when there is no comparison population.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib
from collections import defaultdict
from dataclasses import dataclass, field

import frappe

from jarvis.learning import compat, stats
from jarvis.learning.readonly_db import DEFAULT_TIMEOUT_S
from jarvis.learning.skill_drafts import render_rule, render_skill_draft, render_statement

DETECTOR_SQL_TIMEOUT_S = DEFAULT_TIMEOUT_S
PER_DETECTOR_CANDIDATE_CAP = 50  # plan §4.1
_SENS_ORDER = {"A": 0, "B": 1, "C": 2}

# Recency guard (plan §4.1): compare a segment's last-N-day behaviour to its
# full window; on a material divergence prefer the RECENT behaviour, never the
# stale average. The window is anchored to the segment's OWN latest day so a
# change near the end of any dataset is caught (not only when "today" happens to
# sit inside the data).
RECENCY_WINDOW_DAYS = 90
RECENCY_SHARE_THRESHOLD = 0.2  # matches stats.recency_divergence default

# Burst gate (plan §4.1): a go-live import creates a run of rows in ONE session
# (seconds-to-minutes apart) even when it backfills many historical posting
# dates. Collapse creation clusters up to this gap AND require the evidence to
# span >=5 distinct CREATION days, so an import can never satisfy n_min on
# posting-date spread alone.
BURST_MAX_GAP_S = 120

# Detectors whose party->value pattern can encode GEOGRAPHY (state-driven tax
# templates) rather than a per-party habit (plan §4.2). Keyed by skill_template
# so the guard needs no registry change; acct-party-tax-template (Tier-2) joins
# this set when it ships.
GEOGRAPHY_CONFOUND_TEMPLATES = {"supplier-tax-template"}

SINGLES_SQL = "SELECT value FROM `tabSingles` WHERE `doctype` = %(dt)s AND field = %(f)s LIMIT 1"


class DetectorSkip(Exception):
	"""Raised by a guard / postprocess to mark the detector un-runnable this
	pass (recorded as a skip with a reason, never a silent clean pass)."""

	def __init__(self, reason: str):
		self.reason = reason
		super().__init__(reason)


@dataclass
class DetectorResult:
	candidates: list = field(default_factory=list)
	skipped_reason: str | None = None
	# Approximate scanned-row count charged against pattern_row_budget_per_night
	# (plan §5.6). ``None`` (a hand-built result) tells the engine to fall back to
	# the candidates' n_rows; a real run always sets an int.
	rows_scanned: int | None = None
	# Candidate count BEFORE the PER_DETECTOR_CANDIDATE_CAP truncation. Drift
	# re-validation needs it: when the unit was cap-truncated, a pattern_key
	# missing from ``candidates`` is NOT evidence the pattern stopped holding.
	# ``None`` (a hand-built result) means unknown - callers must be
	# conservative. A real run always sets an int.
	raw_candidate_count: int | None = None


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def run_detector(spec: dict, company: str | None, patterndb) -> DetectorResult:
	"""Run one detector spec for one company (or None for org-wide specs)."""
	installed = set(frappe.get_installed_apps())
	missing_apps = [a for a in (spec.get("requires_app") or []) if a not in installed]
	if missing_apps:
		return DetectorResult([], f"requires app(s) not installed: {', '.join(missing_apps)}")

	source = spec.get("requires_source")
	if source and not _source_available(spec):
		# Availability check, not a blanket gate: the preflight marks the
		# Detector State not_applicable when the source has no signal (e.g. a
		# custom print engine writes no Access Log Print rows). Honest skip.
		return DetectorResult([], f"source '{source}' unavailable for this site")

	for dt, fname in spec.get("field_guards") or []:
		if not compat.has_field(dt, fname):
			return DetectorResult([], f"missing field {dt}.{fname}")

	params = {"window_start": window_start(int(spec.get("window_months", 18)))}
	if spec.get("company_scoped"):
		params["company"] = company

	try:
		rows = None
		if spec.get("sql"):
			rows = patterndb.timed_select(_resolve(spec["sql"]), params, timeout_s=DETECTOR_SQL_TIMEOUT_S)
		if spec.get("postprocess"):
			raws = _resolve(spec["postprocess"])(rows, spec, company, patterndb, params)
		else:
			raws = reduce_units(rows, spec, patterndb)
	except DetectorSkip as skip:
		return DetectorResult([], skip.reason)

	candidates = [_finalize(spec, company, r) for r in (raws or []) if r]
	candidates = _apply_geography_guard(spec, candidates, patterndb)
	scanned = _scanned_rows(rows, raws)
	return DetectorResult(
		candidates[:PER_DETECTOR_CANDIDATE_CAP],
		None,
		rows_scanned=scanned,
		raw_candidate_count=len(candidates),
	)


def _source_available(spec: dict) -> bool:
	"""A stream-source detector runs unless the preflight/engine marked its
	Detector State not_applicable (structural no-signal, e.g. printing bypasses
	frappe's print system entirely). Missing state row = available (first run)."""
	state = frappe.db.get_value("Jarvis Pattern Detector State", spec.get("id"), "not_applicable")
	return not state


def _scanned_rows(rows, raws) -> int:
	"""Approximate scanned-row count for the nightly row budget (plan §5.6).

	When the detector ran a SQL we charge the number of rows it MATERIALIZED
	(bounded by each detector's hard LIMIT) - a deterministic figure that, for
	the aggregate SQL shapes, under-estimates the underlying scan but never lies
	upward. Postprocess-only detectors (no top-level SQL) fall back to the units
	they produced. A true EXPLAIN-based scan estimate is a Phase 2 upgrade."""
	if rows is not None:
		return len(rows)
	total = 0
	for r in raws or []:
		if not r:
			continue
		total += int(r.get("n_rows") or r.get("n_units") or 0)
	return total


# ---------------------------------------------------------------------------
# generic S1/S2 reduce (conditional-mode / flag-share)
# ---------------------------------------------------------------------------
def reduce_units(rows, spec: dict, patterndb=None) -> list:
	"""Standard reduce for detectors whose SQL returns unit-grain rows with
	columns unit_id / antecedent / consequent / day / created.

	One row per independent unit (the SQL must already collapse child rows to
	unit grain). Applies the site-wide variance gate across antecedents, then
	the per-antecedent gate suite via :func:`evaluate_segment`.
	"""
	idx: dict = defaultdict(dict)  # antecedent -> unit_id -> (consequent, day, created)
	for r in rows or []:
		ant, uid = r.get("antecedent"), r.get("unit_id")
		if ant is None or uid is None:
			continue
		idx[ant][uid] = (r.get("consequent"), r.get("day"), r.get("created"))

	counts: dict = {}
	site: dict = defaultdict(int)
	for ant, units in idx.items():
		per = defaultdict(int)
		for cons, _d, _c in units.values():
			per[cons] += 1
			site[cons] += 1
		counts[ant] = dict(per)

	multi = len(idx) > 1
	# Variance gate for PER-SEGMENT detectors (antecedent_kind != 'org'): a
	# near-constant consequent site-wide is an org default, not a per-segment
	# habit - this kills the sole-value trap even when only one segment exists
	# (a company that uses a single price list / a single term). Org-level
	# detectors (antecedent_kind == 'org') are the finding itself and are never
	# variance-suppressed here (they defer to org_default_consequent / S5).
	if spec.get("antecedent_kind") != "org" and stats.variance_gate(dict(site)):
		return []

	org_default = spec.get("org_default_consequent")
	targets = spec.get("target_consequents")
	# The detector's own admission gate, fed to the recency guard's secondary
	# (grandfathered-transition) condition so a recent window that no longer
	# clears c_min diverges even while legacy volume holds the plurality.
	c_min = float((spec.get("gates") or {}).get("c_min", 0.90))

	# Recency-guard scaffolding (plan §4.1): recent_cutoff is anchored to the
	# latest day across ALL segments; recent_counts is the leave-segment-out base
	# for a re-targeted recent proposal when a segment's modal value has flipped.
	all_days = [d for units in idx.values() for (_c, d, _cr) in units.values()]
	recent_cutoff = _recency_cutoff(all_days)
	recent_counts = _recent_counts(idx, recent_cutoff) if recent_cutoff else {}

	out = []
	for ant, units in idx.items():
		lsob = stats.leave_segment_out_base_rate(counts, ant)
		mode = lsob["consequent"]
		if mode is None or mode == "__mixed__":
			continue

		decision = _recency_decision(units, mode, recent_cutoff, c_min=c_min)
		if decision and decision["recent"]:
			# The modal VALUE flipped inside the recent window: never propose the
			# stale full-window average. Prefer the recent behaviour if it can
			# stand on its own gates, else withhold this segment entirely.
			retarget = _retarget_recent(
				spec,
				ant,
				units,
				recent_cutoff,
				recent_counts,
				multi=multi,
				targets=targets,
				org_default=org_default,
				patterndb=patterndb,
				decision=decision,
			)
			if retarget:
				out.append(retarget)
			continue

		if targets is not None and mode not in targets:
			continue
		if org_default is not None and str(mode) == str(org_default):
			continue
		days = [d for _c, d, _cr in units.values()]
		created = [cr for _c, _d, cr in units.values()]
		exceptions = _exception_rows(spec, ant, units, mode)
		raw = evaluate_segment(
			spec,
			antecedent_value=ant,
			consequent_value=mode,
			k=lsob["k"],
			n_units=lsob["n_units"],
			base_rate=lsob["base_rate"],
			rest_k=lsob["rest_k"],
			rest_n=lsob["rest_n"],
			days=days,
			created=created,
			exceptions=exceptions,
			single_antecedent=not multi,
			vars=_default_vars(spec, ant, mode),
			template=spec.get("skill_template"),
			recency_note=(decision["note"] if decision else None),
			recency_changed_around=(decision["changed_around"] if decision else None),
			n_candidate_values=_selection_breadth(targets, counts.get(ant)),
			patterndb=patterndb,
		)
		if raw:
			out.append(raw)
	return out


def evaluate_segment(
	spec: dict,
	*,
	antecedent_value,
	consequent_value,
	k: int,
	n_units: int,
	base_rate: float,
	days,
	created,
	n_rows: int | None = None,
	rest_k: int | None = None,
	rest_n: int | None = None,
	exceptions=None,
	single_antecedent: bool = False,
	names_party: bool | None = None,
	vars: dict | None = None,
	statement: str | None = None,
	rule: str | None = None,
	template: str | None = None,
	extra_evidence: dict | None = None,
	recency_note: str | None = None,
	recency_changed_around: str | None = None,
	n_candidate_values: int | None = None,
	patterndb=None,
) -> dict | None:
	"""Apply the full §4.1 gate suite to one antecedent segment and return a
	raw candidate dict, or None if any gate rejects it.

	Gates, in order: n_min on units, spread (>=5 distinct days), burst
	collapse (go-live imports cannot satisfy n_min), precision (Wilson
	half-width), phrasing (only/always => 0 exceptions and n>=60), confidence
	>= c_min, and gap >= 0.15 vs the leave-segment-out base rate (skipped for
	single-antecedent org-level detectors, where a base rate is meaningless).

	Phase 2 statistics upgrade: when the caller supplies the leave-segment-out
	counts (``rest_k``/``rest_n``), a survivor also gets a one-sided
	enrichment ``p_value`` (Fisher's exact when any expected cell < 5, else
	G-test) - ADDITIVE evidence for the per-family BH-FDR pass in
	``jarvis.learning.fdr``, never a replacement for the gates above.
	Candidates without a comparison population (single-antecedent detectors,
	postprocess callers that pass no rest counts) carry ``p_value=None`` and
	pass through FDR untested.

	Selection correction: when the consequent was chosen POST HOC as the
	segment's argmax (the standard reduce), the single-value tail is optimistic
	by up to the number of candidate values it was the max over (union bound).
	Callers pass that count as ``n_candidate_values`` and the p-value gets a
	within-segment Bonferroni ``p_adj = min(1.0, p * n_candidate_values)``
	before it reaches the FDR buffer (recorded in the evidence as
	``p_value_correction``). Pre-specified single-target detectors (and
	postprocess callers that pass nothing) stay exact.
	"""
	gates = spec.get("gates") or {}
	n_min = int(gates.get("n_min", stats.N_MIN_USUALLY))
	c_min = float(gates.get("c_min", 0.90))
	phrasing = gates.get("phrasing", "usually")
	exceptions = list(exceptions or [])
	exception_n = max(n_units - k, 0)

	if n_units < n_min:
		return None
	day_list = [d for d in days if d is not None]
	if not stats.spread_ok(day_list):
		return None
	created_list = [c for c in created if c is not None]
	# Collapse creation clusters up to a multi-minute gap (not just the sub-second
	# default): a real go-live import runs over minutes/hours, not one second.
	effective_units = (
		stats.collapse_bursts(created_list, max_gap_s=BURST_MAX_GAP_S) if created_list else n_units
	)
	if effective_units < n_min:
		return None
	# An import backfills many historical POSTING dates in ONE creation session,
	# so posting-date spread alone can be satisfied by a migration. Require the
	# evidence to also span >=5 distinct CREATION days (plan §4.1 burst gate).
	if created_list and not stats.spread_ok(created_list):
		return None
	if not stats.precision_ok(k, n_units):
		return None  # data_starved: interval too wide to phrase as a habit

	confidence = (k / n_units) if n_units else 0.0
	if phrasing in ("only", "always"):
		if exception_n != 0 or n_units < max(n_min, stats.N_MIN_ALWAYS):
			return None
	elif confidence < c_min:
		return None

	gap = confidence - (base_rate or 0.0)
	if not single_antecedent and gap < stats.MIN_GAP:
		return None

	wl = stats.wilson_lower_bound(k, n_units)
	band = stats.band(wl)
	tspread = _temporal_spread(day_list)
	cluster = stats.cluster_exceptions(exceptions) if exceptions else None

	# Phase 2 significance test (Cochran-dispatched Fisher exact / G-test)
	# against the leave-segment-out rest. rest_n == 0 means there is no
	# comparison population, so no test - p_value stays None (FDR pass-through).
	p_value = None
	p_value_method = None
	p_value_correction = None
	if rest_k is not None and rest_n is not None and int(rest_n) > 0 and n_units > 0:
		p_value, p_value_method = stats.enrichment_p_value(k, n_units, k + int(rest_k), n_units + int(rest_n))
		# Within-segment Bonferroni for a post-hoc argmax consequent: testing
		# the WINNER of V candidate values is up to V times as likely to show
		# a small tail under the null (union bound), so correct before BH.
		if p_value is not None and n_candidate_values and int(n_candidate_values) > 1:
			v = int(n_candidate_values)
			p_value = min(1.0, p_value * v)
			p_value_correction = f"bonferroni x{v} (post-hoc consequent selection)"

	# Enforcement cross-ref (plan §4.1): fires for any proposal strong enough to be
	# phrased "always/only" - the declared phrasing OR the strict rule-of-three
	# claim the compiler reads off the evidence (n>=60, 0 exceptions). Tier-1
	# "only" detectors declare a "usually" gate, so keying purely off the declared
	# phrasing (the prior bug) meant the cross-ref never ran for them.
	only_claim = exception_n == 0 and n_units >= stats.N_MIN_ALWAYS
	enforcement = None
	if (
		patterndb is not None
		and spec.get("consequent_field")
		and (phrasing in ("only", "always") or only_claim)
	):
		enforcement = _enforcement_conflict(spec["doctype"], spec["consequent_field"], patterndb)

	evidence = {
		"antecedent": antecedent_value,
		"consequent": consequent_value,
		"k": k,
		"n_units": n_units,
		"n_rows": n_rows if n_rows is not None else n_units,
		"exception_n": exception_n,
		"confidence": round(confidence, 4),
		"base_rate": round(base_rate or 0.0, 4),
		"gap": round(gap, 4),
		"wilson_low": round(wl, 4),
		"band": band,
		"sql_shape": spec.get("sql_shape"),
	}
	if p_value is not None:
		# Full float, not display-rounded: BH-FDR ranks on it and tiny tails
		# (1e-12 vs 1e-3) must stay distinguishable in the persisted evidence.
		evidence["p_value"] = p_value
		evidence["p_value_method"] = p_value_method
		if p_value_correction:
			evidence["p_value_correction"] = p_value_correction
	if extra_evidence:
		evidence.update(extra_evidence)
	if recency_note:
		evidence["recency"] = recency_note
		if recency_changed_around:
			# Machine-readable divergence onset for drift re-validation
			# (lifecycle compares it against the row's reviewed_at).
			evidence["recency_changed_around"] = recency_changed_around
	if enforcement:
		evidence["enforcement_conflict"] = enforcement

	return {
		"antecedent_value": antecedent_value,
		"consequent_value": consequent_value,
		"k": k,
		"n_units": n_units,
		"n_rows": n_rows if n_rows is not None else n_units,
		"exception_n": exception_n,
		"confidence": confidence,
		"wilson_low": wl,
		"gap": gap,
		"band": band,
		"p_value": p_value,
		"p_value_method": p_value_method,
		"temporal_spread": tspread,
		"evidence": evidence,
		"exceptions": exceptions[:20],
		"exceptions_cluster": (cluster or {}).get("note") if cluster else None,
		"names_party": names_party if names_party is not None else (spec.get("antecedent_kind") == "party"),
		"skill_template": template,
		"skill_bullet_vars": vars,
		"statement": statement,
		"rule": rule,
		"since_date": _since(day_list),
		"unit_doctype": spec.get("unit_doctype", "documents"),
		"enforcement_conflict": enforcement,
	}


# ---------------------------------------------------------------------------
# finalize: raw candidate -> engine candidate dict
# ---------------------------------------------------------------------------
def _finalize(spec: dict, company: str | None, raw: dict) -> dict:
	template_id = _effective_template(raw.get("skill_template") or spec.get("skill_template"), raw)
	variables = raw.get("skill_bullet_vars")
	if variables is None:
		variables = _default_vars(spec, raw["antecedent_value"], raw["consequent_value"])
	rule = raw.get("rule") or render_rule(template_id, variables)
	statement = raw.get("statement") or render_statement(template_id, variables)
	conf_pct = round(float(raw.get("confidence", 0.0)) * 100, 1)
	meta = {
		"conf_pct": conf_pct,
		"n_units": raw["n_units"],
		"unit_doctype": raw.get("unit_doctype", spec.get("unit_doctype", "documents")),
		"since_date": raw.get("since_date", ""),
		"exception_n": raw.get("exception_n", 0),
	}
	skill_draft = raw.get("skill_draft") or render_skill_draft(rule, meta)

	names_party = raw.get("names_party", spec.get("antecedent_kind") == "party")
	effective = _escalate(spec.get("sensitivity", "A"), names_party)

	evidence = dict(raw.get("evidence") or {})
	evidence.setdefault("detector_version", spec.get("version"))
	evidence.setdefault("registry_version", _registry_version())

	return {
		"detector_id": spec["id"],
		"detector_version": spec.get("version"),
		"registry_version": _registry_version(),
		"domain": spec["domain"],
		"company": company,
		"pattern_key": _pattern_key(spec["id"], raw["antecedent_value"], company),
		"roles": _roles_for(spec),
		"pattern_statement": statement,
		"skill_draft": skill_draft,
		"support_n": raw["n_units"],
		"n_rows": raw.get("n_rows", raw["n_units"]),
		"exception_n": raw.get("exception_n", 0),
		"confidence_pct": conf_pct,
		"wilson_low": round(float(raw.get("wilson_low", 0.0)), 4),
		"gap": round(float(raw.get("gap", 0.0)), 4),
		# Top-level p_value (full float; also in evidence) is what the
		# per-family BH-FDR pass keys on. None = untested (no comparison
		# population) - fdr passes those through.
		"p_value": raw.get("p_value"),
		"strength_band": raw.get("band"),
		"temporal_spread": raw.get("temporal_spread", {}),
		"evidence": evidence,
		"exceptions": (raw.get("exceptions") or [])[:20],
		"exceptions_cluster": raw.get("exceptions_cluster"),
		"sensitivity": spec.get("sensitivity", "A"),
		"effective_sensitivity": effective,
		"not_applicable": bool(raw.get("not_applicable", False)),
		"antecedent_value": raw["antecedent_value"],
		"consequent_value": raw["consequent_value"],
		"enforcement_conflict": raw.get("enforcement_conflict"),
	}


# ---------------------------------------------------------------------------
# shared readers usable by postprocess functions (all via the facade)
# ---------------------------------------------------------------------------
def single_value(patterndb, sql: str, params: dict | None = None):
	rows = patterndb.timed_select(sql, params or {})
	if not rows:
		return None
	row = rows[0]
	if isinstance(row, dict):
		return next(iter(row.values()), None)
	return row[0]


def singles_value(patterndb, doctype: str, field_name: str):
	"""Read a Single doctype field from `tabSingles` (the row-per-field store)."""
	return single_value(patterndb, SINGLES_SQL, {"dt": doctype, "f": field_name})


def window_start(months: int) -> str:
	return frappe.utils.add_months(frappe.utils.today(), -int(months))


def month_key(day) -> str | None:
	d = as_date(day)
	return d.strftime("%Y-%m") if d else None


def as_date(value):
	if value in (None, ""):
		return None
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	try:
		return datetime.datetime.fromisoformat(str(value)[:19]).date()
	except Exception:
		try:
			return frappe.utils.getdate(value)
		except Exception:
			return None


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _resolve(dotted: str):
	"""Resolve 'module.attr' to the attribute in jarvis.learning.detectors."""
	module_name, attr = dotted.rsplit(".", 1)
	module = importlib.import_module(f"jarvis.learning.detectors.{module_name}")
	return getattr(module, attr)


def _default_vars(spec: dict, antecedent, consequent) -> dict:
	vmap = spec.get("vars_map") or {"antecedent": "antecedent", "consequent": "consequent"}
	return {ph: (antecedent if src == "antecedent" else consequent) for ph, src in vmap.items()}


def _selection_breadth(targets, segment_counts) -> int:
	"""How many candidate consequent values the segment's winner was selected
	over - the within-segment Bonferroni factor for the post-hoc argmax (plan
	§4.1 significance test). Target-constrained detectors were only ever going
	to propose one of ``targets`` (|targets|; a single pre-specified target is
	exact, factor 1); open detectors select over the segment's distinct
	observed values."""
	if targets is not None:
		return max(len(targets), 1)
	return max(len(segment_counts or {}), 1)


def _effective_template(template_id, raw: dict):
	"""Pick the strict '...-only' variant when the evidence supports an
	always/only claim (n>=60, 0 exceptions), so the drafted wording matches the
	actual gate instead of over-claiming 'only' on a 90% tendency (plan §6.3)."""
	if not template_id:
		return template_id
	from jarvis.learning.skill_drafts import STRICT_TEMPLATE_VARIANTS

	only_claim = int(raw.get("exception_n") or 0) == 0 and int(raw.get("n_units") or 0) >= stats.N_MIN_ALWAYS
	if only_claim:
		return STRICT_TEMPLATE_VARIANTS.get(template_id, template_id)
	return template_id


# ---------------------------------------------------------------------------
# recency guard (plan §4.1): last-90-day behaviour vs the full window
# ---------------------------------------------------------------------------
def _recency_cutoff(days):
	"""Anchor date for the recency window: latest observed day minus
	RECENCY_WINDOW_DAYS. None when there are no dated units."""
	dates = [d for d in (as_date(x) for x in days) if d]
	if not dates:
		return None
	return max(dates) - datetime.timedelta(days=RECENCY_WINDOW_DAYS)


def _recent_counts(idx: dict, cutoff) -> dict:
	"""{antecedent: {consequent: recent_unit_count}} over units on/after the
	cutoff - the leave-segment-out base for a re-targeted recent proposal."""
	out: dict = {}
	for ant, units in idx.items():
		per: dict = defaultdict(int)
		for cons, day, _cr in units.values():
			d = as_date(day)
			if d is not None and d >= cutoff:
				per[cons] += 1
		out[ant] = dict(per)
	return out


def _recent_subset(units: dict, cutoff) -> dict:
	"""``units`` restricted to entries on/after the recency cutoff."""
	if cutoff is None:
		return dict(units)
	out = {}
	for uid, (cons, day, created) in units.items():
		d = as_date(day)
		if d is not None and d >= cutoff:
			out[uid] = (cons, day, created)
	return out


def _segment_recency(units: dict, full_mode, cutoff, c_min: float | None = None) -> dict | None:
	"""Detect a last-window divergence for one antecedent segment. Returns
	{divergence, recent_mode, recent_n, note, changed_around} or None when the
	segment is recency-consistent (or has no recent signal). ``c_min`` (the
	detector's own admission gate) arms the secondary grandfathered-transition
	condition: a recent window whose confidence for the ESTABLISHED consequent
	falls below c_min diverges even without a plurality flip or a >=0.2 share
	shift (see stats.recency_divergence)."""
	if cutoff is None:
		return None
	recent: dict = defaultdict(int)
	recent_n = 0
	full_mode_k = 0
	n = 0
	for cons, day, _cr in units.values():
		n += 1
		if cons == full_mode:
			full_mode_k += 1
		d = as_date(day)
		if d is not None and d >= cutoff:
			recent[cons] += 1
			recent_n += 1
	if recent_n == 0 or n == 0:
		return None
	full_share = full_mode_k / n
	recent_mode = max(recent.items(), key=lambda kv: (kv[1], str(kv[0])))[0]
	recent_share = recent[recent_mode] / recent_n
	div = stats.recency_divergence(
		full_share,
		recent_share,
		threshold=RECENCY_SHARE_THRESHOLD,
		full_mode=full_mode,
		recent_mode=recent_mode,
		recent_n=recent_n,
		recent_established_share=recent.get(full_mode, 0) / recent_n,
		c_min=c_min,
	)
	if not div:
		return None
	return {
		"divergence": div,
		"recent_mode": recent_mode,
		"recent_n": recent_n,
		"recent_share": round(recent_share, 4),
		"full_share": round(full_share, 4),
		"changed_around": cutoff.isoformat(),
		"note": f"behavior changed around {cutoff.isoformat()}",
	}


def _recency_decision(units: dict, full_mode, cutoff, c_min: float | None = None) -> dict | None:
	"""Resolve the recency guard for a segment. None => consistent (use the full
	window). Otherwise {mode, recent, note, changed_around}: ``recent=True`` means
	the modal VALUE flipped (prefer the recent subset); ``recent=False`` means the
	same value but a materially shifted share OR a recent window that fell under
	the detector's own c_min (grandfathered transition) - keep the full candidate,
	annotate; the stamped ``recency_changed_around`` then drives drift staling."""
	rec = _segment_recency(units, full_mode, cutoff, c_min=c_min)
	if not rec:
		return None
	flipped = rec["recent_mode"] != full_mode
	return {
		"mode": rec["recent_mode"] if flipped else full_mode,
		"recent": flipped,
		"note": rec["note"],
		"changed_around": rec["changed_around"],
	}


def _retarget_recent(
	spec, ant, units, cutoff, recent_counts, *, multi, targets, org_default, patterndb, decision
):
	"""Re-evaluate a segment on its recent subset for the flipped mode, so a
	recent policy change is proposed (annotated) instead of the stale average.
	Returns a raw candidate or None if the recent evidence fails its gates or a
	target / org-default filter."""
	mode = decision["mode"]
	if targets is not None and mode not in targets:
		return None
	if org_default is not None and str(mode) == str(org_default):
		return None
	recent_units = _recent_subset(units, cutoff)
	if not recent_units:
		return None
	lsob = stats.leave_segment_out_base_rate(recent_counts, ant, consequent=mode)
	days = [d for _c, d, _cr in recent_units.values()]
	created = [cr for _c, _d, cr in recent_units.values()]
	exceptions = _exception_rows(spec, ant, recent_units, mode)
	recent_values = {c for c, _d, _cr in recent_units.values()}
	return evaluate_segment(
		spec,
		antecedent_value=ant,
		consequent_value=mode,
		k=lsob["k"],
		n_units=lsob["n_units"],
		base_rate=lsob["base_rate"],
		rest_k=lsob["rest_k"],
		rest_n=lsob["rest_n"],
		days=days,
		created=created,
		exceptions=exceptions,
		single_antecedent=not multi,
		vars=_default_vars(spec, ant, mode),
		template=spec.get("skill_template"),
		recency_note=decision["note"],
		recency_changed_around=decision["changed_around"],
		n_candidate_values=_selection_breadth(targets, {v: 1 for v in recent_values}),
		patterndb=patterndb,
	)


# ---------------------------------------------------------------------------
# geography-confound guard (plan §4.2): tax-template-by-party may be geography
# ---------------------------------------------------------------------------
_PARTY_STATE_SQL = (
	"SELECT dl.link_name AS party, addr.state AS state "
	"FROM `tabAddress` addr "
	"JOIN `tabDynamic Link` dl ON dl.parent = addr.name AND dl.parenttype = 'Address' "
	"WHERE dl.link_doctype = %(dt)s "
	"AND addr.state IS NOT NULL AND addr.state != '' "
	"LIMIT 200000"
)


def _apply_geography_guard(spec: dict, candidates: list, patterndb) -> list:
	"""Tax-template-by-party patterns can encode GEOGRAPHY (state-driven GST),
	not a per-party habit (plan §4.2, mirrors acct-party-tax-template). Annotate
	every candidate with a geography caveat and, when party state predicts the
	template, demote the band. B-sensitivity already excludes these from batch
	approve.

	State normalization + per-state sample floors are SHARED with the Tier-2
	acct-party-tax-template guard (detectors/accounts.py): free-text states are
	strip+casefold normalized, and a state bucket only counts toward the
	confound verdict with >= _MIN_STATE_PARTIES distinct parties AND
	>= _MIN_STATE_UNITS units - a one-party state is 100% "pure" by
	construction, so a single supplier per state must never declare a
	confound."""
	if not candidates or spec.get("skill_template") not in GEOGRAPHY_CONFOUND_TEMPLATES:
		return candidates
	from jarvis.learning.detectors.accounts import (
		_filter_confound_states,
		_normalize_state_map,
	)

	template_by_party = {c.get("antecedent_value"): c.get("consequent_value") for c in candidates}
	units_by_party = {c.get("antecedent_value"): int(c.get("support_n") or 0) for c in candidates}
	states = _normalize_state_map(_party_state_map(patterndb, "Supplier", list(template_by_party)))
	states = _filter_confound_states(states, template_by_party, units_by_party)
	confound = _state_predicts_template(states, template_by_party)
	caveat = "may reflect supplier geography (state), not a per-supplier habit"
	for c in candidates:
		ev = c.get("evidence")
		if not isinstance(ev, dict):
			ev = {}
			c["evidence"] = ev
		ev["geography_caveat"] = caveat
		stmt = (c.get("pattern_statement") or "").rstrip()
		c["pattern_statement"] = f"{stmt} Caveat: {caveat}; review before approval."
		if confound:
			ev["geography_confound"] = "state predicts template"
			c["strength_band"] = _demote_band(c.get("strength_band"))
	return candidates


def _party_state_map(patterndb, link_doctype: str, parties) -> dict:
	"""Best-effort party -> state from the linked Address. Address.state is
	free text, so the multi-state dedup runs on NORMALIZED (strip + casefold)
	values: a party with 'Karnataka' and 'karnataka ' addresses is ONE state,
	not dropped as multi-state. The returned display value is the most
	frequent original-cased variant (ties broken deterministically). Only
	unambiguous single-state parties are returned; any failure (missing table
	/ version drift) yields an empty map so the caller falls back to
	annotate-only."""
	wanted = {p for p in (parties or []) if p}
	if not wanted or patterndb is None:
		return {}
	try:
		rows = patterndb.sql_select(_PARTY_STATE_SQL, {"dt": link_doctype})
	except Exception:
		return {}
	# party -> {normalized state -> {original-cased variant -> count}}
	acc: dict = {}
	for r in rows or []:
		party, state = r.get("party"), r.get("state")
		if not party or party not in wanted or not state:
			continue
		display = str(state).strip()
		norm = display.casefold()
		if not norm:
			continue
		variants = acc.setdefault(party, {}).setdefault(norm, defaultdict(int))
		variants[display] += 1
	out: dict = {}
	for party, by_norm in acc.items():
		if len(by_norm) != 1:
			continue  # genuinely multi-state party: ambiguous, drop
		variants = next(iter(by_norm.values()))
		out[party] = max(variants.items(), key=lambda kv: (kv[1], str(kv[0])))[0]
	return out


def _state_predicts_template(
	state_by_party: dict, template_by_party: dict, min_states: int = 2, purity: float = 0.8
) -> bool:
	"""True when party STATE predicts the tax template about as well as the party
	does: group the known-state parties by state; if the dominant template within
	each state accounts for >= ``purity`` of that state's parties across >=
	``min_states`` distinct states, the by-party pattern is a geography confound.
	Pure + deterministic (unit-tested)."""
	by_state: dict = {}
	for party, state in (state_by_party or {}).items():
		tmpl = (template_by_party or {}).get(party)
		if not state or not tmpl:
			continue
		by_state.setdefault(state, []).append(tmpl)
	usable = {s: t for s, t in by_state.items() if t}
	if len(usable) < min_states:
		return False
	for tmpls in usable.values():
		top = max(set(tmpls), key=lambda t: (tmpls.count(t), str(t)))
		if tmpls.count(top) / len(tmpls) < purity:
			return False
	return True


def _demote_band(band):
	return {"High": "Medium", "Medium": "Low"}.get(band, band or "Low")


def _exception_rows(spec: dict, antecedent, units: dict, mode) -> list:
	rows = []
	kind = spec.get("antecedent_kind", "antecedent")
	for uid, (cons, day, _created) in units.items():
		if cons == mode:
			continue
		rows.append({"unit": uid, "value": cons, "month": month_key(day), kind: antecedent})
	return rows


def _temporal_spread(days) -> dict:
	dates = sorted(d for d in (as_date(x) for x in days) if d)
	n = len(dates)
	if not n:
		return {"distinct_days": 0, "largest_span_share": 0.0, "single_period": False}
	distinct = len({d for d in dates})
	best, left = 0, 0
	for right in range(n):
		while (dates[right] - dates[left]).days > 183:
			left += 1
		best = max(best, right - left + 1)
	share = round(best / n, 3)
	return {
		"distinct_days": distinct,
		"largest_span_share": share,
		"single_period": share > 0.7,
		"first_day": dates[0].isoformat(),
		"last_day": dates[-1].isoformat(),
	}


def _since(days) -> str:
	dates = [d for d in (as_date(x) for x in days) if d]
	return min(dates).strftime("%Y-%m") if dates else ""


def _escalate(declared: str, names_party: bool) -> str:
	eff = declared or "A"
	if names_party and _SENS_ORDER.get(eff, 0) < _SENS_ORDER["B"]:
		eff = "B"
	return eff


def _pattern_key(detector_id: str, antecedent_value, company: str | None) -> str:
	raw = f"{detector_id}|{antecedent_value}|{company or ''}"
	return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:40]


def _registry_version() -> int:
	try:
		from jarvis.learning.registry import REGISTRY_VERSION

		return REGISTRY_VERSION
	except Exception:
		return 0


def _roles_for(spec: dict) -> list:
	"""Computed role attachment (plan §4.1). Wave C ships
	jarvis.learning.roles.roles_for_doctype; until then fall back to the
	spec's declared role_priors."""
	try:
		from jarvis.learning import roles as roles_mod

		computed = roles_mod.roles_for_doctype(spec.get("doctype"))
		if computed:
			return list(computed)
	except Exception:
		pass
	return list(spec.get("role_priors") or [])


def _enforcement_conflict(doctype: str, field_name: str, patterndb) -> str | None:
	"""§4.1 enforcement cross-ref: an active Server Script / Client Script /
	Workflow referencing the consequent field means the value is ENFORCED,
	not a habit. Badge it so the reviewer sees it (the plan allows suppress or
	badge; we badge and let review decide)."""
	like = f"%{field_name}%"
	found = []
	probes = [
		(
			"Server Script",
			"SELECT name FROM `tabServer Script` WHERE reference_doctype = %(dt)s AND disabled = 0 AND script LIKE %(like)s LIMIT 1",
			{"dt": doctype, "like": like},
		),
		(
			"Client Script",
			"SELECT name FROM `tabClient Script` WHERE dt = %(dt)s AND enabled = 1 AND script LIKE %(like)s LIMIT 1",
			{"dt": doctype, "like": like},
		),
		(
			"Workflow",
			"SELECT name FROM `tabWorkflow` WHERE document_type = %(dt)s AND is_active = 1 LIMIT 1",
			{"dt": doctype},
		),
	]
	for label, sql, params in probes:
		try:
			if patterndb.sql_select(sql, params):
				found.append(label)
		except Exception:
			continue
	if found:
		return "matches existing automation (" + ", ".join(found) + ") - enforced, not a habit"
	return None
