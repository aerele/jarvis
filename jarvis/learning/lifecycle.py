"""Learned-pattern lifecycle: dedupe, suppression, expiry, retention (plan 6.5).

``upsert_candidate`` REPLACES the engine's minimal inline ``_persist_candidate``
(the engine imports it lazily and delegates when present; its inline version is
the fallback). The candidate-dict contract is the stable boundary - see the
engine module docstring.

Rules (plan section 6.5):
  * dedupe on ``pattern_key`` (unique); non-terminal rows refresh evidence in
    place and bump ``last_seen_run`` - an SM-edited draft (``draft_edited=1``) is
    never overwritten (enforced by ``engine._apply_evidence``);
  * Rejected stays suppressed UNLESS the band rises or ``support_n`` grows >=50%,
    which auto re-proposes it (pattern_key is unique, so the SAME row flips
    Rejected -> Proposed carrying a prior-rejection note, rather than a duplicate
    row);
  * Snoozed / Stale refresh evidence only (status untouched);
  * Superseded / Archived are skipped (last_seen_run stamped as suppression
    memory);
  * ``snooze_expiry`` un-snoozes elapsed rows; ``retention`` archives Rejected
    >180d / Superseded >90d (evidence trimmed, pattern_key kept);
  * ``overlap_warning`` is a warn-only lexical check vs the customer's own
    enabled skills;
  * ``revalidate_active`` (drift -> Stale, Phase 2 A1) compares each Approved/
    Active pattern against the mining pass's OWN fresh candidates (the
    engine's ``mined`` stash - no second detector scan; a legacy checker
    re-run path remains for direct callers), refreshes the stats in place,
    and moves undetectable / below-threshold / recency-diverged rows to Stale
    (+ ONE summary Notification Log to System Managers). Never a silent edit:
    Stale rows drop out of the next compile (the compiler filters Approved/
    Active) and the board shows the reason;
  * REDRAFT drift (#482): the evidence refresh above rewrites ``skill_draft``
    in place, and the detector's own wording can flip meaning while every
    statistical axis IMPROVES (the strict "-only" template variant swaps in at
    n>=60 with zero exceptions, so "mostly supplies" becomes "supplies only"),
    which ``revalidate_active`` can never catch. An approved row therefore
    carries an ``approved_draft`` snapshot (the compiler ships that, not the
    live draft), and ``upsert_candidate`` moves the row to Stale the moment
    the freshly detected RULE sentence stops matching the approved one.
"""

from __future__ import annotations

import contextlib
import re

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime, today

JLP = "Jarvis Learned Pattern"
SKILL = "Jarvis Custom Skill"

TERMINAL_SKIP_STATUSES = frozenset({"Superseded", "Archived"})
_REFRESH_STATUSES = frozenset({"Proposed", "Approved", "Active", "Snoozed", "Stale"})

_BAND_RANK = {"High": 0, "Medium": 1, "Low": 2}

REJECTED_TTL_DAYS = 180
SUPERSEDED_TTL_DAYS = 90

_MAX_NOTE_LEN = 500

# Redraft drift (#482). Statuses whose text a human signed off on, and the
# stale_reason prefix stamped when re-detection stops matching that text
# (learned_api.approve_learned_pattern clears reasons with this prefix, since
# re-approving IS the resolution).
_APPROVED_STATUSES = frozenset({"Approved", "Active"})
REDRAFT_STALE_PREFIX = "the detected rule changed after approval"
# skill_drafts.render_skill_draft joins the rule sentence to its evidence
# sentence with this marker. Everything after it is measured numbers that move
# on every run, so only the head is compared.
_EVIDENCE_MARKER = " Evidence: "

# Overlap check: ignore short/common words so a warning means real shared wording.
_OVERLAP_MIN_SHARED = 3
_STOPWORDS = frozenset(
	{
		"this",
		"that",
		"with",
		"from",
		"into",
		"your",
		"will",
		"when",
		"then",
		"they",
		"them",
		"have",
		"which",
		"usually",
		"default",
		"defaults",
		"skill",
		"skills",
		"learned",
		"these",
		"those",
		"about",
		"over",
		"under",
		"each",
		"document",
		"documents",
		"always",
		"never",
		"should",
		"there",
		"their",
	}
)


# --------------------------------------------------------------------------- #
# surfacing order (shared helper; plan section 6.4)
# --------------------------------------------------------------------------- #
def surfacing_sort_key(row) -> tuple:
	"""Ranking for proposal surfacing: **party-specific personalization ranked
	ahead of config-cleanup** (plan section 6.4 - debt-heavy sites must not bury
	the marquee wins), THEN strength band, THEN support_n descending.

	A named-party bullet is escalated to effective sensitivity B/C (A-class never
	names a party), so ``effective_sensitivity in (B, C)`` is the party-specific
	signal and A is config-cleanup. ``row`` is a dict / ``frappe._dict`` carrying
	``effective_sensitivity``, ``strength_band`` and ``support_n``.

	Cross-file coordination: ``engine._promote_surfaced`` (group 2) currently
	orders on band+support only and does NOT fetch ``effective_sensitivity``; it
	should add that field to its ``get_all`` and sort with this helper so the
	party-before-config priority takes effect. Exposed here (a file this fixer
	owns) rather than edited into engine.py to keep the assignments disjoint."""
	get = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))
	eff = (get("effective_sensitivity") or "").strip().upper()
	party_rank = 0 if eff in ("B", "C") else 1
	band_rank = _BAND_RANK.get(get("strength_band"), 3)
	return (party_rank, band_rank, -(get("support_n") or 0))


@contextlib.contextmanager
def _engine_flag():
	"""Bypass the JLP transition guard for engine-driven writes. Restores the
	prior value so nested use (engine already inside a run) is a no-op."""
	prev = frappe.flags.jarvis_pattern_engine
	frappe.flags.jarvis_pattern_engine = True
	try:
		yield
	finally:
		frappe.flags.jarvis_pattern_engine = prev


# --------------------------------------------------------------------------- #
# upsert (engine delegates here)
# --------------------------------------------------------------------------- #
def upsert_candidate(candidate: dict, run) -> str:
	"""Dedupe + persist one candidate. Returns 'created' | 'updated' |
	'duplicate' (the engine's per-unit counters key off these)."""
	from jarvis.learning import engine as eng

	pattern_key = candidate.get("pattern_key")
	if not pattern_key:
		# Unkeyed candidates cannot be deduped safely; drop (suppressed).
		return "duplicate"

	existing = frappe.db.exists(JLP, {"pattern_key": pattern_key})
	if not existing:
		result = _insert_new(candidate, run, eng)
	else:
		status = frappe.db.get_value(JLP, existing, "status")
		if status in TERMINAL_SKIP_STATUSES:
			frappe.db.set_value(JLP, existing, {"last_seen_run": run.name}, update_modified=False)
			result = "duplicate"
		elif status == "Rejected":
			result = _handle_rejected(existing, candidate, run, eng)
		else:
			# Proposed / Approved / Active / Snoozed / Stale: refresh evidence in place.
			doc = frappe.get_doc(JLP, existing)
			# Read BEFORE the refresh: _apply_evidence overwrites the statement
			# and clears draft_polished, so the pre-refresh row is the only
			# place the approved-vs-detected comparison can be made.
			redraft = _redraft_reason(doc, candidate)
			with _engine_flag():
				eng._apply_evidence(doc, candidate, run, is_new=False)
				_set_overlap(doc)
				if redraft:
					doc.status = "Stale"
					doc.stale_reason = redraft[:_MAX_NOTE_LEN]
					doc.last_validated_at = now_datetime()
				doc.save(ignore_permissions=True)
			if redraft:
				_record_redraft_stale(f"{doc.name}: {redraft}")
			result = "updated"

	# Skills-area rework (DESIGN.md section 3): the moment a learned-pattern row
	# is freshly created (fresh insert OR a Rejected row re-proposed), route the
	# finding to the identifiable user(s) as a Personalise question. Best-effort
	# and cap/dedupe-guarded inside the router; it never breaks the engine.
	if result == "created":
		_maybe_materialize_question(pattern_key)
	return result


# --------------------------------------------------------------------------- #
# redraft drift (#482): the approved wording vs what is detected now
# --------------------------------------------------------------------------- #
def rule_sentence(draft) -> str:
	"""The directive half of a skill-draft bullet: the leading "- " and the
	trailing "Evidence: 96% of 58 Purchase Receipt since 2024-09." tail removed.
	That tail is measured numbers which legitimately move on every run, so only
	the head decides whether the org is being told something different."""
	text = (draft or "").strip()
	if text.startswith("- "):
		text = text[2:]
	return " ".join(text.split(_EVIDENCE_MARKER, 1)[0].split()).strip()


def _redraft_reason(doc, candidate: dict) -> str | None:
	"""Why this approved row must go back to review, or None to leave it alone.

	Applies to rows whose approved text is the DETECTOR's own wording, which is
	exactly the set the nightly refresh rewrites (``engine._apply_evidence``
	only overwrites ``skill_draft`` when neither ``draft_edited`` nor
	``draft_polished`` is set). An SM-edited or LLM-polished draft is already
	frozen against that refresh and is not a template render, so comparing it
	against a fresh template render would flag every night; those rows stay
	governed by the statistical axes in ``revalidate_active``."""
	if (doc.get("status") or "") not in _APPROVED_STATUSES:
		return None
	if doc.get("draft_edited") or doc.get("draft_polished"):
		return None
	approved_rule = rule_sentence(doc.get("approved_draft"))
	if not approved_rule:
		# Never approved through the snapshotting path (a row approved before
		# #482 shipped and not yet backfilled): nothing trustworthy to compare.
		return None
	fresh_rule = rule_sentence(candidate.get("skill_draft"))
	if not fresh_rule or fresh_rule.casefold() == approved_rule.casefold():
		return None
	return f'{REDRAFT_STALE_PREFIX}: approved "{approved_rule}" but now detects "{fresh_rule}"'


def _record_redraft_stale(line: str) -> None:
	"""Stash one line for the run's single summary notification. Per-request
	state, drained by ``revalidate_active`` at the end of the same run - the
	mining loop touches one candidate at a time and must never send one
	Notification Log per pattern."""
	lines = getattr(frappe.local, "jarvis_redraft_stale", None)
	if lines is None:
		lines = []
		frappe.local.jarvis_redraft_stale = lines
	lines.append(line)


def _drain_redraft_stale() -> list:
	lines = list(getattr(frappe.local, "jarvis_redraft_stale", None) or [])
	frappe.local.jarvis_redraft_stale = []
	return lines


def _maybe_materialize_question(pattern_key: str) -> None:
	"""Best-effort hook into the Personalise question router (Wave B1). Resolves
	the freshly-touched JLP row by its unique pattern_key and hands it off; any
	failure is logged and swallowed so the learning engine never breaks."""
	try:
		name = frappe.db.get_value(JLP, {"pattern_key": pattern_key}, "name")
		if not name:
			return
		from jarvis.learning import questions

		questions.maybe_materialize_for_pattern(name)
	except Exception:
		frappe.log_error(
			title="jarvis personalise: pattern question hook failed",
			message=frappe.get_traceback(),
		)


def _insert_new(candidate: dict, run, eng, *, status: str = "Proposed", note: str | None = None) -> str:
	doc = frappe.get_doc({"doctype": JLP, "pattern_key": candidate["pattern_key"], "status": status})
	with _engine_flag():
		eng._apply_evidence(doc, candidate, run, is_new=True)
		_set_overlap(doc)
		if note:
			doc.review_note = note[:_MAX_NOTE_LEN]
		doc.insert(ignore_permissions=True)
	return "created"


def _handle_rejected(existing: str, candidate: dict, run, eng) -> str:
	"""Durable suppression, reversible on strengthening (plan section 6.5)."""
	prev = (
		frappe.db.get_value(JLP, existing, ["strength_band", "support_n", "review_note"], as_dict=True)
		or frappe._dict()
	)
	new_rank = _BAND_RANK.get(candidate.get("strength_band"), 3)
	old_rank = _BAND_RANK.get(prev.strength_band, 3)
	band_rose = new_rank < old_rank
	old_n = prev.support_n or 0
	new_n = candidate.get("support_n") or 0
	n_grew = old_n > 0 and new_n >= old_n * 1.5

	if not (band_rose or n_grew):
		# Still weak: remember we saw it again, stay Rejected (suppression memory).
		frappe.db.set_value(JLP, existing, {"last_seen_run": run.name}, update_modified=False)
		return "duplicate"

	# Re-propose. pattern_key is UNIQUE, so we reuse THIS row (Rejected ->
	# Proposed) rather than a literally-new row, preserving the prior note.
	reason = "band rose" if band_rose else f"support grew to {new_n}"
	note = f"Auto re-proposed ({reason} since rejection)."
	prior = (prev.review_note or "").strip()
	if prior:
		note = f"{note} Prior note: {prior}"

	doc = frappe.get_doc(JLP, existing)
	with _engine_flag():
		doc.status = "Proposed"
		eng._apply_evidence(doc, candidate, run, is_new=False)
		doc.surfaced = 0
		doc.surfaced_at = None
		doc.reviewed_by = None
		doc.reviewed_at = None
		doc.review_note = note[:_MAX_NOTE_LEN]
		_set_overlap(doc)
		doc.save(ignore_permissions=True)
	return "created"


# --------------------------------------------------------------------------- #
# snooze expiry + retention (cheap; implemented per assignment)
# --------------------------------------------------------------------------- #
def snooze_expiry() -> dict:
	"""Snoozed rows whose ``snoozed_until`` has passed return to Proposed and
	re-enter surfacing. Null ``snoozed_until`` is excluded by the filter."""
	names = frappe.get_all(JLP, filters={"status": "Snoozed", "snoozed_until": ["<=", today()]}, pluck="name")
	moved = 0
	for name in names:
		try:
			doc = frappe.get_doc(JLP, name)
			with _engine_flag():
				doc.status = "Proposed"
				doc.snoozed_until = None
				doc.surfaced = 0
				doc.surfaced_at = None
				doc.save(ignore_permissions=True)
			moved += 1
		except Exception:
			frappe.log_error(
				title=f"jarvis pattern learning: un-snooze failed for {name}",
				message=frappe.get_traceback(),
			)
	if moved:
		frappe.db.commit()
	return {"unsnoozed": moved}


def retention() -> dict:
	"""Archive Rejected >180d / Superseded >90d: trim evidence, keep pattern_key
	(suppression memory persists). Age is measured from ``modified`` - a
	re-detection stamps last_seen_run with update_modified=False, so a
	still-seen Rejected row ages from its rejection, not its last sighting."""
	now = now_datetime()
	archived = 0
	for status, ttl in (("Rejected", REJECTED_TTL_DAYS), ("Superseded", SUPERSEDED_TTL_DAYS)):
		cutoff = str(get_datetime(add_to_date(now, days=-ttl)))
		names = frappe.get_all(JLP, filters={"status": status, "modified": ["<", cutoff]}, pluck="name")
		for name in names:
			try:
				doc = frappe.get_doc(JLP, name)
				with _engine_flag():
					doc.status = "Archived"
					doc.evidence = None  # trimmed; pattern_key stays for suppression
					doc.save(ignore_permissions=True)
				archived += 1
			except Exception:
				frappe.log_error(
					title=f"jarvis pattern learning: archive failed for {name}",
					message=frappe.get_traceback(),
				)
	if archived:
		frappe.db.commit()
	return {"archived": archived}


# --------------------------------------------------------------------------- #
# re-validation / drift (plan 6.5, Phase 2 A1)
# --------------------------------------------------------------------------- #
# A drifted pattern goes Stale when its fresh Wilson lower bound falls under
# this hard floor, OR when its fresh confidence no longer meets the detector's
# own c_min gate (the quantity that gate originally tested), OR when the
# checker no longer emits its pattern_key at all (undetectable).
STALE_WILSON_FLOOR = 0.80


def revalidate_active(run=None, patterndb=None, mined=None) -> dict:
	"""Nightly drift re-validation (plan 6.5): compare each Approved/Active
	pattern against the CURRENT run's mining candidates by ``pattern_key``.

	``mined`` is the engine's per-unit stash - ``{(detector_id, company):
	{"by_key": {pattern_key: candidate}, "tracked": set(pattern_key),
	"cap_truncated": bool}}`` - built from the same candidates mining just
	computed, so drift costs no second detector scan, needs no extra row
	budget, and covers exactly the units mined tonight (paused runs included).
	Units the run did not mine (skipped, errored, deferred) are left unchecked,
	as are pattern_keys the stash was not tracking (e.g. approved mid-run).
	Without ``mined`` (direct/console callers) the legacy path re-runs the
	checker once per group through ``executor.run_detector`` inside a READ ONLY
	fence (or the caller's ``patterndb``); only that slow path re-checks the
	analysis window per group.

	  * matched: refresh the stat fields + ``last_validated_at`` in place
	    (``skill_draft`` untouched, so an SM edit is preserved by construction;
	    ``strength_band`` clamps to ``flag_band_cap``);
	  * matched but carrying a recency divergence (mining stamped
	    ``evidence.recency`` - the segment's last-90-day behaviour moved away
	    from the full window, including a partial "new deals only" adoption
	    with a >=0.2 recent-share shift, OR a grandfathered partial adoption
	    where the recent window has >=10 units and its confidence for the
	    established consequent falls below the detector's c_min while the
	    full-window aggregate still passes - see stats.recency_divergence):
	    -> Stale with the "behavior changed around <date>" reason, unless
	    the row was reviewed after the divergence window opened;
	  * matched but below threshold: -> Stale. Admission-consistent: stale only
	    when fresh confidence < the detector's own c_min, or the fresh Wilson
	    bound falls under min(STALE_WILSON_FLOOR, the row's stored bound) - the
	    drift floor is never stricter than what admission/approval allowed, so
	    an approved Low-band pattern is not staled on day one;
	  * unmatched with the unit NOT cap-truncated: -> Stale, "no longer
	    detectable";
	  * unmatched but the unit hit the per-detector candidate cap: absence
	    proves nothing - the row is left untouched and the run's coverage note
	    reports the deferral (``cap_deferred``);
	  * checker-version mismatch (registry spec bumped since detection): the
	    comparison would be apples-to-oranges - leave the status, annotate the
	    evidence, and let the nightly mining re-propose under the new version;
	  * detector skipped/errored (missing field/app): NOT evidence of drift -
	    the group is left unchecked.

	Returns ``{revalidated, staled, version_skipped, unchecked, cap_deferred}``
	- the engine copies revalidated/staled onto the run row counters and
	surfaces cap_deferred in the coverage note."""
	from jarvis.learning import registry

	rows = frappe.get_all(
		JLP,
		filters={"status": ["in", ["Approved", "Active"]]},
		fields=[
			"name",
			"status",
			"detector_id",
			"company",
			"pattern_key",
			"confidence_pct",
			"wilson_low",
			"draft_edited",
			"evidence",
			"flag_band_cap",
			"reviewed_at",
		],
	)
	out = {"revalidated": 0, "staled": 0, "version_skipped": 0, "unchecked": 0, "cap_deferred": 0}
	if not rows:
		return out

	groups: dict = {}
	for r in rows:
		groups.setdefault((r.detector_id, r.company), []).append(r)

	now = now_datetime()
	staled_lines: list[str] = []
	for (detector_id, company), patterns in groups.items():
		if mined is None and _revalidation_window_closed(run):
			# Respect the analysis window on the SLOW (checker-re-run) path
			# only: the mined path does no detector SQL, so remaining groups
			# cost row writes at most.
			out["unchecked"] += len(patterns)
			continue

		spec = registry.get_detector(detector_id)
		if spec is None:
			# Detector left the registry: no checker to compare against. Never
			# Stale on our own blindness - annotate and leave the status.
			for row in patterns:
				_annotate_revalidation(
					row.name,
					f"detector '{detector_id}' is no longer in the registry; drift compare skipped",
					now,
				)
			out["unchecked"] += len(patterns)
			continue

		compare: list = []
		for row in patterns:
			stored = _stored_checker_version(row)
			spec_version = spec.get("version")
			if stored is not None and spec_version is not None and int(stored) != int(spec_version):
				# Version guard: the checker changed since this pattern was
				# detected - a drift comparison would be meaningless. Leave the
				# status; the nightly mining re-proposes under the new version.
				_annotate_revalidation(
					row.name,
					f"checker version changed {stored} -> {spec_version}; "
					"awaiting re-propose by the nightly mining",
					now,
				)
				out["version_skipped"] += 1
			else:
				compare.append(row)
		if not compare:
			continue

		tracked = None
		if mined is not None:
			entry = mined.get((detector_id, company))
			if entry is None:
				# The run did not mine this unit (skipped / errored / deferred
				# on a pause): proves nothing about the patterns.
				out["unchecked"] += len(compare)
				continue
			by_key = entry.get("by_key") or {}
			tracked = entry.get("tracked")
			cap_truncated = bool(entry.get("cap_truncated"))
		else:
			candidates = _run_checker_once(spec, company, patterndb)
			if candidates is None:
				# Skip / error: the checker could not run, which proves nothing
				# about the patterns. Leave them for the next pass.
				out["unchecked"] += len(compare)
				continue
			by_key = {c.get("pattern_key"): c for c in candidates if c.get("pattern_key")}
			from jarvis.learning.executor import PER_DETECTOR_CANDIDATE_CAP

			cap_truncated = len(candidates) >= PER_DETECTOR_CANDIDATE_CAP

		window_months = spec.get("window_months", 18)
		for row in compare:
			if tracked is not None and row.pattern_key not in tracked:
				# The stash was not watching this key when the unit was mined
				# (e.g. the row was approved mid-run): no evidence either way.
				out["unchecked"] += 1
				continue
			cand = by_key.get(row.pattern_key)
			if cand is None:
				if cap_truncated:
					# The unit's candidate list was cut at the per-detector
					# cap: absence is NOT evidence. Leave the row untouched;
					# the engine surfaces the deferral in the coverage note.
					out["cap_deferred"] += 1
					continue
				reason = f"no longer detectable (window {window_months} months)"
				_mark_stale(row.name, reason, now)
				out["revalidated"] += 1
				out["staled"] += 1
				staled_lines.append(f"{row.name}: {reason}")
				continue
			_refresh_drift_stats(row.name, cand, now, band_cap=row.get("flag_band_cap"))
			out["revalidated"] += 1
			reason = _stale_reason(spec, row, cand, window_months)
			if reason:
				_mark_stale(row.name, reason, now)
				out["staled"] += 1
				staled_lines.append(f"{row.name}: {reason}")

	# Rows this run's mining already staled for redraft drift ride the SAME
	# summary notification (#482) - the mining loop stashes them precisely so
	# they do not become one Notification Log per pattern.
	redraft_lines = _drain_redraft_stale()
	if redraft_lines:
		staled_lines.extend(redraft_lines)
		out["staled"] += len(redraft_lines)
	if staled_lines:
		_notify_stale(staled_lines)
	if out["revalidated"] or out["version_skipped"]:
		frappe.db.commit()
	return out


def _run_checker_once(spec, company, patterndb) -> list | None:
	"""One fenced checker pass for a (detector, company) group. Returns the
	candidate list, or None when the detector skipped or errored (which is NOT
	evidence of drift). When no ``patterndb`` is supplied the READ ONLY fence is
	opened here (commit first - the fence refuses pending writes)."""
	from jarvis.learning.executor import run_detector

	try:
		if patterndb is not None:
			result = run_detector(spec, company, patterndb)
		else:
			from jarvis.learning.readonly_db import read_only_transaction

			frappe.db.commit()
			with read_only_transaction() as pdb:
				result = run_detector(spec, company, pdb)
	except Exception:
		frappe.log_error(
			title=f"jarvis pattern re-validation failed: {spec.get('id')} / {company}",
			message=frappe.get_traceback(),
		)
		return None
	if getattr(result, "skipped_reason", None):
		return None
	return list(getattr(result, "candidates", None) or [])


def _stale_reason(spec, row, cand: dict, window_months) -> str | None:
	"""Threshold check on the FRESH numbers; None means the pattern still holds.

	Admission-consistent (never stricter than what approval allowed): a matched
	pattern stales only when

	  * mining stamped a recency divergence on the candidate (the segment's
	    recent behaviour moved away from the approved pattern - a mode flip OR
	    a >=0.2 recent-share shift such as "new terms for new deals only") and
	    the row was not reviewed after the divergence window opened; or
	  * fresh confidence fell under the detector's own c_min gate (the quantity
	    admission originally tested); or
	  * the fresh Wilson bound fell under min(STALE_WILSON_FLOOR, the row's
	    stored bound) - an approved Low-band pattern (admitted with wilson_low
	    below the 0.80 floor) is NOT staled while its numbers hold; it stales
	    only on a real regression below what it was approved with.
	"""
	recency = _recency_drift(row, cand)
	if recency:
		return f"{recency} (recent behavior diverged from the approved pattern)"

	gates = spec.get("gates") or {}
	c_min = float(gates.get("c_min", 0.90))
	wilson = float(cand.get("wilson_low") or 0)
	confidence = float(cand.get("confidence_pct") or 0) / 100.0
	old_wilson = float(row.get("wilson_low") or 0)
	if confidence < c_min:
		old_pct = round(float(row.get("confidence_pct") or 0))
		new_pct = round(float(cand.get("confidence_pct") or 0))
		return f"confidence dropped {old_pct}% -> {new_pct}% (window {window_months} months)"
	if wilson < min(STALE_WILSON_FLOOR, old_wilson):
		return (
			f"wilson lower bound dropped {round(old_wilson, 2)} -> {round(wilson, 2)} "
			f"(window {window_months} months)"
		)
	return None


def _recency_drift(row, cand: dict) -> str | None:
	"""The mining candidate's recency-divergence note ("behavior changed around
	<date>"), unless the row was reviewed AFTER the divergence window opened -
	an SM who re-approved with the divergence already visible has accepted it,
	and re-staling nightly would loop them."""
	ev = cand.get("evidence")
	if not isinstance(ev, dict):
		ev = _evidence_dict(ev)
	note = ev.get("recency")
	if not note:
		return None
	changed_around = ev.get("recency_changed_around")
	reviewed_at = row.get("reviewed_at")
	if changed_around and reviewed_at:
		try:
			if get_datetime(reviewed_at) >= get_datetime(str(changed_around)):
				return None
		except Exception:
			pass
	return str(note)


def _refresh_drift_stats(name: str, cand: dict, now, band_cap=None) -> None:
	"""Refresh the measured stats in place. Deliberately NOT the mining upsert:
	no skill_draft, no evidence rewrite, no status change - the drill-down shows
	the fresh truth while the reviewed text stays exactly as approved (an
	SM-edited draft, draft_edited=1, is preserved by construction). The band
	write clamps to the correction loop's ``flag_band_cap`` (shared contract)."""
	from jarvis.learning import engine as eng

	update = {
		"support_n": _coerce_int(cand.get("support_n")),
		"n_rows": _coerce_int(cand.get("n_rows")),
		"exception_n": _coerce_int(cand.get("exception_n")),
		"confidence_pct": _coerce_float(cand.get("confidence_pct")),
		"wilson_low": _coerce_float(cand.get("wilson_low")),
		"gap": _coerce_float(cand.get("gap")),
		"strength_band": eng.weaker_of(cand.get("strength_band"), band_cap),
		"last_validated_at": now,
	}
	frappe.db.set_value(JLP, name, update, update_modified=False)


def _coerce_int(value) -> int:
	try:
		return int(value) if value is not None else 0
	except (TypeError, ValueError):
		return 0


def _coerce_float(value) -> float:
	try:
		return float(value) if value is not None else 0.0
	except (TypeError, ValueError):
		return 0.0


def _mark_stale(name: str, reason: str, now) -> None:
	doc = frappe.get_doc(JLP, name)
	with _engine_flag():
		doc.status = "Stale"
		doc.stale_reason = reason[:_MAX_NOTE_LEN]
		doc.last_validated_at = now
		doc.save(ignore_permissions=True)


def _stored_checker_version(row):
	"""The checker version stamped into the pattern's evidence at detection
	(executor writes ``detector_version``; ``checker_version`` accepted for
	forward-compat). None when the evidence carries no stamp (legacy row) - the
	comparison then proceeds against the current spec."""
	ev = _evidence_dict(row.get("evidence"))
	version = ev.get("detector_version")
	if version is None:
		version = ev.get("checker_version")
	try:
		return int(version) if version is not None else None
	except (TypeError, ValueError):
		return None


def _annotate_revalidation(name: str, note: str, now) -> None:
	ev = _evidence_dict(frappe.db.get_value(JLP, name, "evidence"))
	ev["revalidation"] = {"note": note, "at": str(now)}
	frappe.db.set_value(JLP, name, {"evidence": frappe.as_json(ev)}, update_modified=False)


def _evidence_dict(raw) -> dict:
	if isinstance(raw, dict):
		return raw
	if not raw:
		return {}
	try:
		parsed = frappe.parse_json(raw)
	except Exception:
		return {}
	return parsed if isinstance(parsed, dict) else {}


def _notify_stale(staled_lines: list[str]) -> None:
	"""ONE summary Notification Log per pass (never one per pattern - the
	morning-1 single-notification idiom)."""
	n = len(staled_lines)
	subject = f"Jarvis: {n} learned pattern{'s' if n != 1 else ''} went stale"
	shown = staled_lines[:5]
	if n > len(shown):
		shown.append(f"...and {n - len(shown)} more.")
	message = (
		"Nightly re-validation found learned patterns that no longer hold:\n"
		+ "\n".join(f"- {line}" for line in shown)
		+ "\n\nStale patterns are excluded from the next Apply (never silently "
		"edited); re-approve or reject them on the Learning board."
	)
	notify_system_managers(subject, message)


def notify_system_managers(subject: str, message: str) -> None:
	"""Best-effort Notification Log to every enabled System Manager (the
	agent_scheduler ``_notify_owner`` shape; ``get_users_with_role`` already
	filters to enabled users and excludes Administrator). Shared by drift
	re-validation and the correction loop (``learned_api.flag_learned_default``);
	never raises."""
	try:
		from frappe.utils.user import get_users_with_role

		recipients = get_users_with_role("System Manager")
	except Exception:
		return
	for user in recipients:
		if not user or user in ("Administrator", "Guest"):
			continue
		try:
			frappe.get_doc(
				{
					"doctype": "Notification Log",
					"for_user": user,
					"type": "Alert",
					"subject": subject,
					"email_content": message,
				}
			).insert(ignore_permissions=True)
		except Exception:
			pass
	try:
		frappe.db.commit()
	except Exception:
		pass


def _revalidation_window_closed(run) -> bool:
	"""True when a SCHEDULED run's analysis window has ended (manual runs bypass
	the window, mirroring ``engine._pause_reason``)."""
	if run is None or getattr(run, "trigger", None) == "manual":
		return False
	try:
		from jarvis.learning.orchestrator import should_pause_for_window

		return bool(should_pause_for_window(run.window_start_used, run.window_end_used, now_datetime()))
	except Exception:
		return False


# --------------------------------------------------------------------------- #
# overlap warning (warn-only, lexical, vs the customer's own enabled skills)
# --------------------------------------------------------------------------- #
def compute_overlap_warning(pattern_statement, skill_draft) -> str | None:
	"""Return a warn-only note if this pattern's wording lexically overlaps an
	enabled customer-authored (non-managed) skill, else None."""
	index = _skill_token_index()
	if not index:
		return None
	pat = _tokens(f"{pattern_statement or ''} {skill_draft or ''}")
	if not pat:
		return None
	best_name = None
	best_score = 0
	for sname, toks in index:
		score = len(pat & toks)
		if score >= _OVERLAP_MIN_SHARED and score > best_score:
			best_name, best_score = sname, score
	if best_name:
		return f"May overlap with your custom skill '{best_name}' (shared wording); review before applying."
	return None


def _set_overlap(doc) -> None:
	warning = compute_overlap_warning(doc.get("pattern_statement"), doc.get("skill_draft"))
	# Only clear an existing warning to None if we recomputed (avoids churn).
	doc.overlap_warning = warning


def _skill_token_index():
	"""Job-scoped token index of enabled non-managed skills (avoids an N+1 across
	a run's many candidates). Stashed on frappe.local for the job's lifetime."""
	cached = getattr(frappe.local, "_jarvis_overlap_index", None)
	if cached is not None:
		return cached
	index: list = []
	try:
		rows = frappe.get_all(
			SKILL,
			filters={"enabled": 1, "managed_by_learning": 0},
			fields=["skill_name", "description"],
		)
	except Exception:
		rows = []
	for r in rows:
		toks = _tokens(f"{(r.get('skill_name') or '').replace('-', ' ')} {r.get('description') or ''}")
		if toks:
			index.append((r.get("skill_name"), toks))
	try:
		frappe.local._jarvis_overlap_index = index
	except Exception:
		pass
	return index


def _tokens(text: str) -> set:
	return {w for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower()) if w not in _STOPWORDS}
