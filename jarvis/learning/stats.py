"""Pure statistical gates for pattern learning (plan section 4.1).

Deliberately frappe-free so the whole gate suite is unit-testable without
a site. Every count fed in here is an INDEPENDENT UNIT count (distinct
parent documents, parties, or months as declared per detector) - never
child-table rows; the executor enforces that upstream.

Phase 2 statistics upgrade (plan sections 2, 4.1): formal significance
testing on the segment-vs-rest 2x2 table. :func:`fisher_exact_greater` is
the exact one-sided enrichment p-value (hypergeometric tail, lgamma-based,
no scipy) for small expected cells; :func:`g_test_greater` is the
large-sample likelihood-ratio complement; :func:`enrichment_p_value`
dispatches on Cochran's condition (any expected cell < 5 => exact test).
The per-family BH-FDR pass over these p-values lives in
``jarvis.learning.fdr`` and is ADDITIVE after the hard gates below.
"""

from __future__ import annotations

import datetime
import math
from itertools import pairwise

# Strength bands on the Wilson LOWER bound, not raw k/n (plan section 4.1).
BAND_HIGH = 0.90
BAND_MEDIUM = 0.80

# Hard n-gates on units (plan section 4.1): "usually" phrasing needs 20,
# "always/only" needs 60 with 0 exceptions (rule of three).
N_MIN_USUALLY = 20
N_MIN_ALWAYS = 60

MAX_HALF_WIDTH = 0.15
MIN_GAP = 0.15
VARIANCE_THRESHOLD = 0.95
MIN_SPREAD_DAYS = 5

# Cochran's condition: the large-sample G-test is trustworthy only when every
# expected cell of the 2x2 table is at least this big; below it the exact
# hypergeometric tail (Fisher) is used instead (plan Phase 2 statistics).
COCHRAN_MIN_EXPECTED = 5.0

# Grandfathered-transition guard (drift finding): a partial adoption ("new
# terms for NEW deals only, legacy accounts grandfathered") keeps the recent
# PLURALITY on the legacy value and the recent-share shift under the 0.2
# threshold, so the primary recency conditions never fire while legacy volume
# dominates. The secondary condition below needs at least this many recent
# units before it may report divergence (noise floor).
RECENCY_MIN_RECENT_UNITS = 10


def wilson_lower_bound(k: int, n: int, z: float = 1.96) -> float:
	"""95% Wilson score lower bound: unlike raw k/n it does not reward tiny
	samples, so 5/5 never reads as strong as 205/214."""
	if n <= 0:
		return 0.0
	k = min(max(k, 0), n)
	phat = k / n
	z2 = z * z
	denom = 1 + z2 / n
	center = phat + z2 / (2 * n)
	margin = z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))
	return max(0.0, (center - margin) / denom)


def wilson_half_width(k: int, n: int, z: float = 1.96) -> float:
	"""Half the Wilson interval width; the precision gate rejects estimates
	too fuzzy to phrase as a habit."""
	if n <= 0:
		return 1.0
	k = min(max(k, 0), n)
	phat = k / n
	z2 = z * z
	denom = 1 + z2 / n
	margin = z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))
	return margin / denom


def band(wilson_low: float) -> str:
	"""Strength band on the Wilson lower bound: High >= 0.90, Medium
	0.80-0.90, Low < 0.80 (plan: strength_band is banded on wilson_low)."""
	if wilson_low >= BAND_HIGH:
		return "High"
	if wilson_low >= BAND_MEDIUM:
		return "Medium"
	return "Low"


def precision_ok(k: int, n: int, max_half_width: float = MAX_HALF_WIDTH) -> bool:
	"""Precision gate: Wilson half-width <= 0.15, else the candidate is
	data_starved rather than proposed (plan section 4.1)."""
	return wilson_half_width(k, n) <= max_half_width


def leave_segment_out_base_rate(counts: dict, antecedent, consequent=None) -> dict:
	"""Confidence vs the base rate computed EXCLUDING the target segment; a
	naive lift ratio passes trivially on skewed marginals (plan gap gate).

	``counts`` is {antecedent_value: {consequent_value: unit_count}}. When
	``consequent`` is None the target segment's mode value is used (ties
	broken by count desc, then value string asc, deterministically).
	Returns {consequent, k, n_units, confidence, base_rate, gap, rest_k,
	rest_n}; ``rest_k``/``rest_n`` are the raw leave-segment-out counts the
	Phase 2 significance test (:func:`enrichment_p_value`) needs.
	``base_rate`` is 0.0 when no OTHER segment has units - sole-segment
	sites are the variance gate's job, not a free gap win.
	"""
	segment = counts.get(antecedent) or {}
	n_units = sum(segment.values())
	if not n_units:
		return {
			"consequent": consequent,
			"k": 0,
			"n_units": 0,
			"confidence": 0.0,
			"base_rate": 0.0,
			"gap": 0.0,
			"rest_k": 0,
			"rest_n": 0,
		}
	if consequent is None:
		consequent = sorted(segment.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]
	k = segment.get(consequent, 0)
	confidence = k / n_units

	rest_k = 0
	rest_n = 0
	for other, dist in counts.items():
		if other == antecedent:
			continue
		rest_n += sum(dist.values())
		rest_k += dist.get(consequent, 0)
	base_rate = (rest_k / rest_n) if rest_n else 0.0

	return {
		"consequent": consequent,
		"k": k,
		"n_units": n_units,
		"confidence": confidence,
		"base_rate": base_rate,
		"gap": confidence - base_rate,
		"rest_k": rest_k,
		"rest_n": rest_n,
	}


# ---------------------------------------------------------------------------
# Phase 2 significance testing (plan sections 2, 4.1: Fisher's exact on small
# cells, G-test otherwise; consumed by the executor, then BH-FDR in fdr.py).
#
# The 2x2 enrichment table, in unit counts (never child rows):
#
#                     consequent   other      margin
#   target segment        k         n - k       n
#   rest of site        K - k     (N-K)-(n-k)  N - n
#   margin                K         N - K       N
# ---------------------------------------------------------------------------
def fisher_exact_greater(k: int, n: int, K: int, N: int) -> float:
	"""One-sided (enrichment) Fisher's exact p-value: the probability that the
	segment's consequent share is at least this extreme under the null that
	segment membership and the consequent are independent, i.e. the upper
	hypergeometric tail P(X >= k) for X ~ Hypergeometric(N, K, n).

	Pure math.lgamma log-binomials (no scipy); each tail term is computed in
	log space and the tail is summed exactly with math.fsum, so a 10^-40 term
	neither overflows nor drowns. Degenerate margins (K=0, K=N, n=0, n=N with
	k at the floor) all resolve to 1.0: a table with no free cell carries no
	evidence of enrichment.
	"""
	n, N, K, k = int(n), int(N), int(K), int(k)
	if N <= 0 or n <= 0:
		return 1.0
	n = min(n, N)
	K = min(max(K, 0), N)
	hi = min(n, K)  # largest achievable k
	k = min(max(k, 0), hi)
	lo = max(0, n - (N - K))  # smallest achievable k
	if k <= lo:
		return 1.0  # the tail is the whole support
	log_denom = _log_binom(N, n)
	terms = [math.exp(_log_binom(K, x) + _log_binom(N - K, n - x) - log_denom) for x in range(k, hi + 1)]
	return min(max(math.fsum(terms), 0.0), 1.0)


def g_test_greater(k: int, n: int, K: int, N: int) -> float:
	"""One-sided (enrichment) G-test p-value on the same 2x2 table: the
	large-sample likelihood-ratio complement to :func:`fisher_exact_greater`
	when Cochran's condition holds (all expected cells >= 5).

	G = 2 * sum(O * ln(O/E)) with df=1; the two-sided chi-square tail
	(erfc(sqrt(G/2)) for df=1) is halved and directed: enrichment gets
	p_two/2, depletion gets 1 - p_two/2, and a null table gives 0.5.
	"""
	n, N, K, k = int(n), int(N), int(K), int(k)
	rest_n = N - n
	if n <= 0 or rest_n <= 0:
		return 1.0
	a = min(max(k, 0), min(n, K))
	b = n - a
	c = K - a
	d = rest_n - c
	if min(b, c, d) < 0 or K <= 0 or K >= N:
		return 1.0  # inconsistent counts or a zero-variance consequent

	g = 0.0
	for observed, row_margin, col_margin in (
		(a, n, K),
		(b, n, N - K),
		(c, rest_n, K),
		(d, rest_n, N - K),
	):
		expected = row_margin * col_margin / N
		if observed > 0:
			g += observed * math.log(observed / expected)
	g = max(2.0 * g, 0.0)

	p_two = math.erfc(math.sqrt(g / 2.0))  # chi-square df=1 survival
	enriched = (a / n) > (c / rest_n)
	return p_two / 2.0 if enriched else 1.0 - p_two / 2.0


def cochran_ok(n: int, K: int, N: int, min_expected: float = COCHRAN_MIN_EXPECTED) -> bool:
	"""Cochran's condition on the 2x2 table's margins: every expected cell
	count under independence must be >= min_expected for the large-sample
	G-test; otherwise the exact test is required. (Expected cells depend only
	on the margins n, K, N - never on the observed k.)"""
	n, K, N = int(n), int(K), int(N)
	if N <= 0 or n <= 0 or n >= N or K <= 0 or K >= N:
		return False
	cells = (
		n * K / N,
		n * (N - K) / N,
		(N - n) * K / N,
		(N - n) * (N - K) / N,
	)
	return min(cells) >= min_expected


def enrichment_p_value(k: int, n: int, K: int, N: int) -> tuple[float, str]:
	"""Dispatch for the Phase 2 significance test: Fisher's exact when ANY
	expected cell is < 5 (Cochran), else the G-test. Returns
	``(p_value, method)`` with method in {'fisher_exact', 'g_test'}."""
	if cochran_ok(n, K, N):
		return (g_test_greater(k, n, K, N), "g_test")
	return (fisher_exact_greater(k, n, K, N), "fisher_exact")


def _log_binom(n: int, k: int) -> float:
	"""log C(n, k) via lgamma; 0 for the empty/full choices, -inf never
	(callers keep k within [0, n])."""
	if k < 0 or k > n:
		return float("-inf")
	return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def variance_gate(site_wide_counts: dict, threshold: float = VARIANCE_THRESHOLD) -> bool:
	"""True = suppress: the consequent is near-constant site-wide (one value
	holds >= threshold of all units), so per-antecedent proposals would just
	restate the org default (kills sole-price-list / all-non-stock /
	vanilla-default restatements; emit at most ONE org-level card instead).
	No data also suppresses."""
	total = sum(site_wide_counts.values())
	if not total:
		return True
	return max(site_wide_counts.values()) / total >= threshold


def collapse_bursts(timestamps, max_gap_s: int = 1) -> int:
	"""Effective unit count with creation-burst runs collapsed to one unit:
	rows created in consecutive-second runs (imports, go-live backfills) are
	one event, so a migration batch cannot satisfy n_min (plan spread gate).
	"""
	ts = sorted(_to_datetime(t) for t in timestamps)
	if not ts:
		return 0
	units = 1
	for prev, cur in pairwise(ts):
		if (cur - prev).total_seconds() > max_gap_s:
			units += 1
	return units


def spread_ok(days_or_timestamps, min_days: int = MIN_SPREAD_DAYS) -> bool:
	"""Spread gate: evidence must span >= min_days distinct calendar days;
	one busy afternoon (or one import batch) is not an organizational habit.

	Accepts either {day: count} (SQL-aggregated) or an iterable of
	timestamps/dates.
	"""
	if isinstance(days_or_timestamps, dict):
		days = {d for d, c in days_or_timestamps.items() if c}
	else:
		days = {_to_date(t) for t in days_or_timestamps}
	return len(days) >= min_days


def recency_divergence(
	full_window_mode_share: float,
	last90_mode_share: float | None,
	threshold: float = 0.2,
	full_mode=None,
	recent_mode=None,
	recent_n: int | None = None,
	recent_established_share: float | None = None,
	c_min: float | None = None,
) -> str | None:
	"""Recency guard: 'recent_differs' when last-90-day behavior materially
	diverges from the full window - propose the RECENT habit or withhold,
	never average over a policy change (plan section 4.1).

	A changed mode VALUE (pass full_mode/recent_mode) always diverges; else
	the mode shares must differ by >= threshold. None when consistent or
	when the recent window has no signal (last90_mode_share is None).

	Secondary (grandfathered-transition) condition: even with the SAME mode
	and a sub-threshold share shift, the recent window contradicts the
	established habit when its confidence for the ESTABLISHED consequent
	(``recent_established_share``) falls below the detector's own admission
	gate (``c_min``) while the full window still passes - the "new terms for
	new deals only, legacy accounts grandfathered" trap, where legacy volume
	keeps the 18-month aggregate high indefinitely. Noise-safe: it needs
	>= RECENCY_MIN_RECENT_UNITS units in the recent window (``recent_n``) and
	all three kwargs; callers that pass none of them keep the exact original
	behaviour.
	"""
	if full_mode is not None and recent_mode is not None and full_mode != recent_mode:
		return "recent_differs"
	if (
		recent_n is not None
		and int(recent_n) >= RECENCY_MIN_RECENT_UNITS
		and recent_established_share is not None
		and c_min is not None
		and float(recent_established_share) < float(c_min) <= float(full_window_mode_share)
	):
		return "recent_differs"
	if last90_mode_share is None:
		return None
	if abs(full_window_mode_share - last90_mode_share) >= threshold:
		return "recent_differs"
	return None


def cluster_exceptions(
	exception_rows,
	keys=("customer", "user", "month", "cost_center", "warehouse"),
	min_count: int = 5,
	dominance: float = 0.6,
) -> dict | None:
	"""Exceptions concentrating in one entity are a sub-rule candidate, not
	noise: > dominance share on any axis, over >= min_count total exceptions
	(plan: exception_cluster, >60% in one entity => sub-rule candidate).

	``exception_rows`` is a list of dicts keyed by the cluster axes; missing
	or empty axis values are ignored per axis. Returns the strongest cluster
	as {axis, value, count, total, share, note}, or None.
	"""
	rows = [r for r in exception_rows if r]
	if len(rows) < min_count:
		return None
	best = None
	for axis in keys:
		tally: dict = {}
		for row in rows:
			value = row.get(axis)
			if value in (None, ""):
				continue
			tally[value] = tally.get(value, 0) + 1
		if not tally:
			continue
		value, count = max(tally.items(), key=lambda kv: (kv[1], str(kv[0])))
		share = count / len(rows)
		if share > dominance and (best is None or share > best["share"]):
			best = {
				"axis": axis,
				"value": value,
				"count": count,
				"total": len(rows),
				"share": share,
				"note": (
					f"{count} of {len(rows)} exceptions concentrate in {axis} {value} - sub-rule candidate"
				),
			}
	return best


def rule_of_three_note(n_units: int) -> str | None:
	"""Rule of three on units: with 0 exceptions in n units the 95% upper
	bound on the true exception rate is 3/n - the basis for requiring
	n >= 60 before 'always/only' phrasing. Drill-down only: the plan bans
	this parenthetical from compiled skill text."""
	if not n_units or n_units <= 0:
		return None
	pct = 100.0 * 3.0 / n_units
	return (
		f"0 exceptions in {n_units} units: true exception rate below "
		f"{pct:.1f}% at 95% confidence (rule of three)"
	)


def _to_datetime(value) -> datetime.datetime:
	if isinstance(value, datetime.datetime):
		return value
	if isinstance(value, datetime.date):
		return datetime.datetime(value.year, value.month, value.day)
	return datetime.datetime.fromisoformat(str(value))


def _to_date(value) -> datetime.date:
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	return datetime.datetime.fromisoformat(str(value)).date()
