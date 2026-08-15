"""Per-family Benjamini-Hochberg FDR control (plan sections 2, 4.1 - the
Phase 2 statistics upgrade's multiple-testing pass).

A nightly run evaluates one hypothesis per surviving antecedent segment; a
detector that scans 400 suppliers gets ~400 shots at a fluke, so raw p-values
over-propose exactly on the biggest sites. This module applies the standard
BH step-up procedure PER FAMILY, where a family = one ``detector_id`` within
a run, ACROSS ALL of that detector's companies (plan: "BH-FDR per detector
family").

Design constraints honoured here:

* ADDITIVE after the hard gates: only candidates that already passed every
  section 4.1 gate (units / Wilson / gap / variance / spread / burst /
  recency) reach this pass. FDR removes, never admits.
* Candidates without a ``p_value`` (single-antecedent org detectors and
  postprocess candidates with no comparison population) carry NO test, so
  they can never be FDR-rejected: they pass through untouched and do not
  count toward the family size m.
* Nothing silently vanishes: every filtered family reports its rejected
  candidates and counts (:class:`FamilyFdrResult`), and
  :class:`DetectorFamilyBuffer` accumulates run-level totals for the run's
  coverage accounting.

Frappe-free and lifecycle-agnostic: callers hand in candidate dicts (the
executor contract) or any objects exposing ``p_value``; nothing here reads
or writes the database.

Application point: the engine walks work units detector-major (all companies
of detector D are consecutive - ``registry.iter_work_units``), but persists
per unit. :class:`DetectorFamilyBuffer` closes that gap: the engine adds each
unit's candidates and persists only what the buffer releases when a family
completes (id change) or the run ends/pauses (``flush``). The engine wiring
is a separate, deliberately tiny patch owned by engine.py's owner; this
module carries the whole mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Family-wise false-discovery-rate budget: at most this expected share of a
# family's surviving proposals are flukes.
FDR_Q = 0.05

# jarvis#483: the ONLY way ``engine._read_and_persist`` may skip this module's BH
# pass and persist a unit's raw candidates uncorrected. ``fdr_buffer`` there has no
# default and is not optional, precisely so a caller cannot skip correction by
# omitting the argument or by passing ``None`` out of habit (Python's usual "nothing
# here" value) - both used to silently persist every raw candidate with zero
# multiple-testing correction, letting a detector that tests hundreds of suppliers
# take hundreds of shots at a fluke. A caller that genuinely wants that (there is
# currently none) must say so with this exact sentinel.
NO_FDR = object()

# Defensive soft cap on a family's buffered candidates: a many-company tenant
# can concentrate one detector's whole family in RAM before the first persist
# (the run's memory high-water mark). Crossing the cap releases the buffered
# chunk early - the FDR grain narrows to that chunk for the outlier family,
# still a valid BH pass - and the release is counted (``counts()``) so a
# pathological family is observable in the run's coverage note before it
# pressures a long-queue worker. Per-unit candidates are already capped at 50
# (executor), so ~2000 means 40+ buffered company-units of one detector.
FAMILY_SOFT_CAP = 2000


def default_p_value_key(item):
	"""p-value accessor for BH: dict candidates (the executor contract) or
	any object with a ``p_value`` attribute. None = untested."""
	if isinstance(item, dict):
		return item.get("p_value")
	return getattr(item, "p_value", None)


def benjamini_hochberg(items, q: float = FDR_Q, key=default_p_value_key) -> list:
	"""Standard BH step-up over one family; returns the SURVIVING subset in
	the original input order.

	With the m tested items sorted by p ascending, find the largest rank i
	with p_(i) <= (i/m) * q and keep every tested item with p <= p_(i); if no
	rank qualifies, every tested item is rejected. Items whose ``key`` is
	None carry no test: they are always kept and are NOT counted in m (an
	untested candidate must neither die by FDR nor dilute the correction).
	"""
	items = list(items)
	tested: list[tuple[int, float]] = []
	for idx, item in enumerate(items):
		p = key(item)
		if p is None:
			continue
		tested.append((idx, float(p)))
	if not tested:
		return items

	m = len(tested)
	threshold = None
	for rank, (_idx, p) in enumerate(sorted(tested, key=lambda t: t[1]), start=1):
		if p <= (rank / m) * q:
			threshold = p
	if threshold is None:
		rejected = {idx for idx, _p in tested}
	else:
		rejected = {idx for idx, p in tested if p > threshold}
	return [item for idx, item in enumerate(items) if idx not in rejected]


@dataclass
class FamilyFdrResult:
	"""One filtered detector family: what survived, what was removed, and the
	counts the run's coverage accounting needs (nothing silently vanishes)."""

	detector_id: str | None
	survivors: list = field(default_factory=list)
	rejected: list = field(default_factory=list)
	tested_n: int = 0
	untested_n: int = 0

	@property
	def rejected_n(self) -> int:
		return len(self.rejected)


def apply_family_fdr(candidates_by_detector: dict, q: float = FDR_Q) -> dict:
	"""Filter each detector family independently (family = detector_id across
	ALL its companies). ``candidates_by_detector`` maps detector_id -> list of
	candidate dicts; returns {detector_id: FamilyFdrResult}. Lifecycle-agnostic:
	the caller decides what persisting a survivor or recording a rejection
	means."""
	out: dict = {}
	for detector_id, candidates in (candidates_by_detector or {}).items():
		out[detector_id] = _filter_family(detector_id, list(candidates or []), q)
	return out


def _filter_family(detector_id, candidates: list, q: float) -> FamilyFdrResult:
	survivors = benjamini_hochberg(candidates, q=q)
	survivor_ids = {id(c) for c in survivors}
	rejected = [c for c in candidates if id(c) not in survivor_ids]
	tested_n = sum(1 for c in candidates if default_p_value_key(c) is not None)
	return FamilyFdrResult(
		detector_id=detector_id,
		survivors=survivors,
		rejected=rejected,
		tested_n=tested_n,
		untested_n=len(candidates) - tested_n,
	)


class DetectorFamilyBuffer:
	"""Buffers candidates across the engine's detector-major unit loop so the
	BH pass sees a WHOLE family (one detector, all companies) before anything
	is persisted - filtering per (company, detector) unit would void the
	family guarantee.

	Contract with the unit loop:

	* ``add(detector_id, candidates)`` per work unit, in loop order. Returns
	  the PREVIOUS family's :class:`FamilyFdrResult` when ``detector_id``
	  starts a new family (persist its survivors, account its rejections),
	  else None while the current family is still accumulating.
	* ``flush()`` after the loop AND on a pause-break: releases the final
	  buffered family. On a pause the family may be partial (its remaining
	  companies deferred to the next run); BH over the tests actually run
	  this night is still a valid FDR pass, and next night's run re-evaluates
	  the deferred units afresh.
	* ``counts()`` exposes run-level totals for coverage accounting - FDR
	  rejections are recorded like gate failures, never silently dropped.
	* ``soft_cap``: when one family's buffered candidates reach the cap, the
	  chunk is released early (BH over the chunk - a narrowed but valid FDR
	  grain for that outlier detector) and ``early_releases`` counts it, so a
	  pathological family cannot become an unobservable memory high-water mark.
	"""

	def __init__(self, q: float = FDR_Q, soft_cap: int = FAMILY_SOFT_CAP):
		self.q = q
		self.soft_cap = soft_cap
		self._detector_id: str | None = None
		self._candidates: list = []
		self.families_flushed = 0
		self.total_tested = 0
		self.total_rejected = 0
		self.total_untested = 0
		self.early_releases = 0
		self.peak_buffered = 0

	@property
	def pending_detector_id(self) -> str | None:
		return self._detector_id

	@property
	def pending_n(self) -> int:
		return len(self._candidates)

	def add(self, detector_id: str, candidates) -> FamilyFdrResult | None:
		released = None
		if self._detector_id is not None and detector_id != self._detector_id:
			released = self._release()
		self._detector_id = detector_id
		self._candidates.extend(candidates or [])
		if len(self._candidates) > self.peak_buffered:
			self.peak_buffered = len(self._candidates)
		if released is None and self.soft_cap and len(self._candidates) >= self.soft_cap:
			# Early release at the soft cap: the family's FDR grain narrows to
			# this buffered chunk (the remainder keeps buffering under the same
			# detector_id and releases on the next family change / flush).
			self.early_releases += 1
			released = self._release()
			self._detector_id = detector_id
		return released

	def flush(self) -> FamilyFdrResult | None:
		if self._detector_id is None:
			return None
		return self._release()

	def counts(self) -> dict:
		return {
			"families": self.families_flushed,
			"tested": self.total_tested,
			"untested": self.total_untested,
			"fdr_rejected": self.total_rejected,
			"early_releases": self.early_releases,
			"peak_buffered": self.peak_buffered,
		}

	def _release(self) -> FamilyFdrResult:
		result = _filter_family(self._detector_id, self._candidates, self.q)
		self._detector_id = None
		self._candidates = []
		self.families_flushed += 1
		self.total_tested += result.tested_n
		self.total_untested += result.untested_n
		self.total_rejected += result.rejected_n
		return result
