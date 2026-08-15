"""Shared platform enums for the agent result grammar (PP-1 / PP-2 / PP-3).

This module is the SINGLE source of truth for the three closed enums the
platform prerequisites introduce, so the enum can never drift between the
doctype schema (the ``Select`` ``options`` strings), the writeback validators,
the dashboard render layer, the support UI, and telemetry — all of which read
from here:

  * PP-1 — ``RESULT_CLASSES``: the four-class value grammar every value-bearing
    result carries (``00-MASTER-PLAN.md`` §6). ``confirmed_outcome`` is reserved
    for the PP-5 provenance ledger and may never be emitted by an evaluator.
  * PP-2 — ``RUN_STATES``: the coverage-verdict axis (distinct from the
    execution-lifecycle ``status``) that decides whether a clean conclusion is
    warranted.
  * PP-3 — ``REASON_CODES``: the closed, typed ``not_evaluable`` reason-code
    registry, each code carrying customer remediation text, a retryability flag,
    and support-routing metadata.

Every ``Select`` in the corresponding doctype JSON derives its ``options`` from
the tuples below (kept byte-for-byte in sync via ``select_options``); the
runtime enforcement (set-once validation, writeback drops, strong-verb gating,
the render layer) is layered on top of these definitions in later phases.
"""

import re

# --------------------------------------------------------------------------- #
# PP-1 — result classes (the four-class value grammar)
# --------------------------------------------------------------------------- #
RESULT_CLASSES = (
	"observed_fact",
	"derived_candidate",
	"legal_scenario",
	"confirmed_outcome",
)

#: The class an evaluator may NOT emit. It is written ONLY by the PP-5
#: provenance ledger, from an accepted recovery / avoided-payment / completed
#: remediation with a resolving ``outcome_provenance`` link. The finding
#: writeback drops any evaluator row carrying it.
RESERVED_RESULT_CLASS = "confirmed_outcome"

#: The only ungated class — a direct read from declared records; no extra
#: metadata required. Used as the conservative backfill for legacy fact rows.
DEFAULT_RESULT_CLASS = "observed_fact"

#: Per-class REQUIRED metadata fieldnames. A writeback row whose class is
#: ``derived_candidate`` / ``legal_scenario`` and that is missing ANY of these
#: is dropped (forces the run partial, names the rejected ref). ``observed_fact``
#: needs nothing; ``confirmed_outcome`` is never evaluator-emitted (it needs an
#: ``outcome_provenance`` link, supplied only by the ledger).
RESULT_CLASS_REQUIRED_FIELDS = {
	"observed_fact": (),
	"derived_candidate": ("confidence", "match_basis", "false_positive_path"),
	"legal_scenario": ("rule_version", "source", "reviewer"),
	"confirmed_outcome": ("outcome_provenance",),
}

#: Confirmation lifecycle for ``derived_candidate`` findings. Only the PP-5
#: ledger may move a finding off ``unconfirmed``.
CONFIRMATION_STATUSES = (
	"unconfirmed",
	"confirmed",
	"rejected",
)

#: The strong verbs that may render ONLY for a ``confirmed_outcome`` row with a
#: resolving ``outcome_provenance``. For every other class the shared render
#: helper (later phase) refuses the token rather than trusting author discipline.
STRONG_VERBS = (
	"saved",
	"recovered",
	"prevented",
	"actually paid",
	"replaces",
)


# --------------------------------------------------------------------------- #
# PP-2 — coverage-verdict run states
# --------------------------------------------------------------------------- #
RUN_STATES = (
	# COVERAGE verdict, NOT a licence to say "clean": every required rule evaluated
	# to completion. Findings MAY still be present under evaluated_clean (a completed
	# close that surfaced exceptions) — the verdict is a coverage axis, so
	# ``evaluated_clean`` does NOT imply zero findings (R5-J1). The old "AND no
	# findings" gloss conflated the coverage verdict with the render gate below.
	"evaluated_clean",  # coverage verdict: every required rule evaluated to completion
	"partial",  # some required rules evaluated, result incomplete
	"not_evaluable",  # no conclusion possible for the required checks
	"failed",  # the run produced no result (exec failure)
)

#: The coverage verdict that is a PRECONDITION for the "no exceptions were found"
#: sentence (and any equivalent clean/compliant attestation) — but NOT on its own
#: sufficient. R5-J1 TWO-CONDITION RENDER RULE: that sentence may render ONLY when
#: ``run_state == evaluated_clean`` AND the run persisted ZERO findings. A run that
#: evaluated every required check but DID persist findings is still
#: ``evaluated_clean`` (the coverage verdict) yet must NEVER read "no exceptions" —
#: the render layer (``agent_runs._clean_attestation_allowed``) enforces the second
#: condition, so the coverage verdict and the absence-of-exceptions claim can never
#: be conflated.
CLEAN_RUN_STATE = "evaluated_clean"


# --------------------------------------------------------------------------- #
# PP-3 — typed not_evaluable reason codes
# --------------------------------------------------------------------------- #
#: The closed reason-code registry. Each entry carries:
#:   meaning     — internal description
#:   retryable   — whether re-running (after the stated remediation) can succeed
#:   remediation — customer-facing text; may contain ``{}`` placeholders
#:                 (e.g. ``{app}``, ``{setting}``) filled at render time
#:   routing     — where a persistent occurrence is routed
REASON_CODES = {
	"app_absent_or_ineligible": {
		"meaning": "a min_apps dependency is not installed / edition ineligible",
		"retryable": False,
		"remediation": "This capability needs {app}; it is not installed on your site.",
		"routing": "install/onboarding",
	},
	"permission_slice": {
		"meaning": "the run-as identity is record-sliced (A12 scoped_visibility)",
		"retryable": True,
		"remediation": "The scheduled user only sees part of the ledger; findings are limited to that slice.",
		"routing": "admin/permissions",
	},
	"configuration_missing": {
		"meaning": "a required setting/master is unconfigured",
		"retryable": True,
		"remediation": "Configure {setting} to evaluate this check.",
		"routing": "config/support",
	},
	"record_coverage_insufficient": {
		"meaning": "too few in-scope records to conclude",
		"retryable": True,
		"remediation": "Not enough {records} in scope this period to evaluate.",
		"routing": "none/informational",
	},
	"source_stale": {
		"meaning": "an input source is behind its freshness watermark",
		"retryable": True,
		"remediation": "The {source} data is stale; refresh and re-run.",
		"routing": "data/support",
	},
	"rule_expired": {
		"meaning": "a statutory rule set is past review/expiry (unknown applicability)",
		"retryable": False,
		"remediation": "This statutory rule is pending review; the check is paused.",
		"routing": "legal-rule owner",
	},
	"external_evidence_absent": {
		"meaning": "required external evidence (import artifact) not supplied",
		"retryable": True,
		"remediation": "Upload the {evidence} to evaluate this check.",
		"routing": "reviewer/File-Box",
	},
	"run_truncated_watermark": {
		"meaning": "fetch hit the turn budget / GL drifted mid-scan (A17)",
		"retryable": True,
		"remediation": "The run was truncated; re-run to complete coverage.",
		"routing": "none/auto-retry",
	},
	"unsupported_customisation": {
		"meaning": "a customer customisation the evaluator cannot safely read",
		"retryable": False,
		"remediation": "A customisation on {doctype} is not supported by this check.",
		"routing": "product/support",
	},
}

#: The fail-safe code an unknown/unmapped reason string is coerced to (never
#: dropped silently — the raw string is preserved in the token ``detail``).
FALLBACK_REASON_CODE = "unsupported_customisation"

#: Per-token coverage states in the typed coverage manifest (PP-3).
COVERAGE_STATES = (
	"evaluated",
	"not_evaluable",
	"truncated",
)


# --------------------------------------------------------------------------- #
# Helpers — the enforcement layers (writeback, render, telemetry) call these
# --------------------------------------------------------------------------- #
def select_options(seq) -> str:
	"""Render an enum tuple as a Frappe ``Select`` ``options`` string (the exact
	value stored in the doctype JSON), so schema and code share one definition."""
	return "\n".join(seq)


def is_result_class(value) -> bool:
	return value in RESULT_CLASSES


def is_run_state(value) -> bool:
	return value in RUN_STATES


def is_reason_code(value) -> bool:
	return value in REASON_CODES


def required_metadata_for(result_class) -> tuple:
	"""The metadata fieldnames a writeback row of this class must carry."""
	return RESULT_CLASS_REQUIRED_FIELDS.get(result_class, ())


def missing_metadata(result_class, row) -> list:
	"""The subset of required metadata for ``result_class`` that ``row`` (a dict)
	is missing or has empty. Empty list == the class contract is satisfied."""
	out = []
	for field in required_metadata_for(result_class):
		val = row.get(field)
		if val is None or (isinstance(val, str) and not val.strip()):
			out.append(field)
	return out


def coerce_reason_code(raw) -> tuple:
	"""Map a raw coverage reason to ``(reason_code, detail)`` (PP-3 fail-safe):
	a known code passes through with empty detail; anything else becomes
	``unsupported_customisation`` with the raw string preserved in ``detail`` —
	never dropped."""
	if raw in REASON_CODES:
		return raw, ""
	return FALLBACK_REASON_CODE, ("" if raw is None else str(raw))


def remediation_for(reason_code, **fmt) -> str:
	"""Customer-facing remediation text for a reason code, with any ``{...}``
	placeholders filled from ``fmt``. Unknown placeholders are left literal so a
	missing substitution never raises in the render path."""
	entry = REASON_CODES.get(reason_code) or REASON_CODES[FALLBACK_REASON_CODE]
	text = entry["remediation"]
	if not fmt:
		return text
	try:
		return text.format(**fmt)
	except (KeyError, IndexError):
		return text


def is_retryable(reason_code) -> bool:
	entry = REASON_CODES.get(reason_code) or REASON_CODES[FALLBACK_REASON_CODE]
	return bool(entry["retryable"])


def routing_for(reason_code) -> str:
	entry = REASON_CODES.get(reason_code) or REASON_CODES[FALLBACK_REASON_CODE]
	return entry["routing"]


# --------------------------------------------------------------------------- #
# PP-2 — coverage-verdict resolution (the ONE run_state a writeback resolves)
# --------------------------------------------------------------------------- #
def resolve_run_state(*, required_tokens, evaluated_tokens, partial: bool, failed: bool = False) -> str:
	"""Resolve EXACTLY one PP-2 coverage verdict from the per-check coverage
	payload (never derived from an empty-findings heuristic):

	  * ``failed``          — the run produced no result (exec failure);
	  * ``not_evaluable``   — required checks were named but NONE were evaluated
	    (every required token is not_evaluable/truncated);
	  * ``partial``         — some required checks evaluated, result incomplete
	    (a partial gate fired, or some — not all — required tokens not_evaluable);
	  * ``evaluated_clean`` — every required check evaluated and no partial gate
	    fired (the ONLY state under which "no exceptions" may render; the render
	    layer additionally requires an empty findings list).

	``required_tokens`` is the set of checks this run was asked to cover (the keys
	of the coverage manifest); ``evaluated_tokens`` is the subset that came back
	``evaluated``. An empty manifest (an evaluator that reports no required tokens)
	has no required coverage to fail, so — absent any partial gate — it resolves
	``evaluated_clean`` exactly as the pre-PP-2 behaviour did."""
	if failed:
		return "failed"
	req = set(required_tokens or ())
	ev = set(evaluated_tokens or ())
	if req and not (req & ev):
		return "not_evaluable"
	if partial:
		return "partial"
	return CLEAN_RUN_STATE


# --------------------------------------------------------------------------- #
# PP-1 — strong-verb gating (a shared helper refuses the token, not authors)
# --------------------------------------------------------------------------- #
#: ``\b`` word boundaries so a substring ("un-saved") never trips; the phrase
#: verbs ("actually paid") match with their internal space intact.
_STRONG_VERB_RE = re.compile(r"\b(" + "|".join(re.escape(v) for v in STRONG_VERBS) + r")\b", re.IGNORECASE)

#: The neutral marker a stripped strong verb is replaced with in non-strict
#: render paths, so the row still renders but makes no outcome claim.
STRONG_VERB_REPLACEMENT = "[unverified]"


def contains_strong_verb(text) -> bool:
	"""True iff ``text`` carries any outcome-claim strong verb (case-insensitive)."""
	return bool(text) and bool(_STRONG_VERB_RE.search(str(text)))


def strong_verb_allowed(result_class, outcome_provenance) -> bool:
	"""A strong verb ("saved/recovered/prevented/actually paid/replaces") may
	render ONLY for a ``confirmed_outcome`` row whose ``outcome_provenance`` link
	resolves — every other class is a scenario/candidate/fact, never a measured
	outcome."""
	return result_class == RESERVED_RESULT_CLASS and bool(outcome_provenance)


def render_value_text(text, result_class, *, outcome_provenance=None, strict: bool = False) -> str:
	"""Return ``text`` made safe to render (PP-1 strong-verb gate).

	If the text carries no strong verb it passes through unchanged. If it does,
	the verb is permitted only for a ``confirmed_outcome`` row with a resolving
	``outcome_provenance``; for every other class the helper — not author
	discipline — neutralises it: ``strict=True`` raises ``ValueError`` (the export
	/ collateral template path, which must fail a build rather than emit the
	claim); otherwise the verb tokens are replaced with :data:`STRONG_VERB_REPLACEMENT`
	so the row still renders without the unearned claim."""
	if text is None:
		return ""
	text = str(text)
	if not contains_strong_verb(text):
		return text
	if strong_verb_allowed(result_class, outcome_provenance):
		return text
	if strict:
		raise ValueError(
			f"strong verb not permitted for result_class={result_class!r} without outcome provenance"
		)
	return _STRONG_VERB_RE.sub(STRONG_VERB_REPLACEMENT, text)
