"""Tests for the Phase 2 per-family BH-FDR pass (jarvis.learning.fdr) and
its integration with the executor's p-value emission.

Three layers, per plan section 11 (deferred clustered-null fixtures):

1. Exact-value tests: benjamini_hochberg against the worked example from
   Benjamini & Hochberg (1995) - 15 p-values, q=0.05, exactly 4 discoveries.
2. Statistical-level control: synthetic null 2x2 tables through
   stats.enrichment_p_value + BH must yield ~zero survivors across repeated
   seeds, while planted true positives survive and nulls die.
3. Pipeline-level clustered-null fixtures: rows correlated within parent
   docs (child-row clustering, the i.i.d. trap) through the executor's
   reduce_units (unit counting + full gate suite + Fisher/G-test) and the
   DetectorFamilyBuffer must yield ~zero proposals on pure null data and
   exactly the planted patterns on mixed data.

All fixtures are seeded (random.Random(seed)), so every count asserted here
is deterministic; the bounds still leave headroom so a libm-level float
difference cannot flap the suite. No DB access anywhere (patterndb=None).
"""

import datetime
import random
from types import SimpleNamespace

from frappe.tests.utils import FrappeTestCase

from jarvis.learning import stats
from jarvis.learning.executor import reduce_units
from jarvis.learning.fdr import (
	FDR_Q,
	DetectorFamilyBuffer,
	apply_family_fdr,
	benjamini_hochberg,
	default_p_value_key,
)


def _cand(p, **extra):
	d = {"p_value": p}
	d.update(extra)
	return d


class TestBenjaminiHochberg(FrappeTestCase):
	# The worked example from Benjamini & Hochberg (1995), q=0.05, m=15:
	# the largest rank i with p_(i) <= (i/15)*0.05 is i=4 (0.0095 <= 0.01333;
	# 0.0201 > 0.01667), so exactly the 4 smallest p-values are discoveries.
	BH_1995 = [
		0.0001,
		0.0004,
		0.0019,
		0.0095,
		0.0201,
		0.0278,
		0.0298,
		0.0344,
		0.0459,
		0.3240,
		0.4262,
		0.5719,
		0.6528,
		0.7590,
		1.000,
	]

	def test_bh_1995_worked_example(self):
		items = [_cand(p, i=i) for i, p in enumerate(self.BH_1995)]
		survivors = benjamini_hochberg(items, q=0.05)
		self.assertEqual([s["i"] for s in survivors], [0, 1, 2, 3])

	def test_bh_1995_input_order_is_preserved(self):
		shuffled = [_cand(p, i=i) for i, p in enumerate(self.BH_1995)]
		random.Random(7).shuffle(shuffled)
		survivors = benjamini_hochberg(shuffled, q=0.05)
		self.assertEqual({s["i"] for s in survivors}, {0, 1, 2, 3})
		# Survivors come back in INPUT order, not p-value order.
		positions = [shuffled.index(s) for s in survivors]
		self.assertEqual(positions, sorted(positions))

	def test_step_up_rescues_smaller_ranks(self):
		# The defining step-up property: rank 3 (0.049 <= 3/3 * 0.05)
		# qualifies, so ranks 1-2 survive too even though 0.04 > 1/3 * 0.05.
		items = [_cand(0.04), _cand(0.045), _cand(0.049)]
		self.assertEqual(len(benjamini_hochberg(items, q=0.05)), 3)

	def test_no_qualifying_rank_rejects_all_tested(self):
		items = [_cand(0.2), _cand(0.5), _cand(0.9)]
		self.assertEqual(benjamini_hochberg(items, q=0.05), [])

	def test_untested_pass_through_and_do_not_count_in_m(self):
		# With m=1 the tested 0.04 survives (0.04 <= 0.05); if the untested
		# item were wrongly counted (m=2), rank 1's cutoff would be 0.025
		# and 0.04 would die.
		items = [_cand(None), _cand(0.04)]
		survivors = benjamini_hochberg(items, q=0.05)
		self.assertEqual(len(survivors), 2)

	def test_all_untested_kept(self):
		items = [_cand(None), _cand(None)]
		self.assertEqual(benjamini_hochberg(items), items)

	def test_empty(self):
		self.assertEqual(benjamini_hochberg([]), [])

	def test_key_supports_attribute_objects(self):
		good = SimpleNamespace(p_value=0.001)
		bad = SimpleNamespace(p_value=0.9)
		self.assertEqual(benjamini_hochberg([good, bad], q=0.05), [good])
		self.assertEqual(default_p_value_key(good), 0.001)
		self.assertIsNone(default_p_value_key(SimpleNamespace()))


class TestApplyFamilyFdr(FrappeTestCase):
	def test_families_filtered_independently(self):
		# Family = detector_id: det-a's strong p-values must not rescue
		# det-b's nulls (and det-b's nulls must not drag det-a down).
		results = apply_family_fdr(
			{
				"det-a": [_cand(1e-8, who="a1"), _cand(1e-6, who="a2")],
				"det-b": [_cand(0.2, who="b1"), _cand(0.4, who="b2")],
			}
		)
		self.assertEqual([c["who"] for c in results["det-a"].survivors], ["a1", "a2"])
		self.assertEqual(results["det-a"].rejected, [])
		self.assertEqual(results["det-b"].survivors, [])
		self.assertEqual([c["who"] for c in results["det-b"].rejected], ["b1", "b2"])

	def test_counts_exposed_nothing_silently_vanishes(self):
		res = apply_family_fdr({"det-a": [_cand(1e-8), _cand(0.5), _cand(None)]})["det-a"]
		self.assertEqual(res.tested_n, 2)
		self.assertEqual(res.untested_n, 1)
		self.assertEqual(res.rejected_n, 1)
		self.assertEqual(len(res.survivors), 2)  # the strong one + the untested one
		self.assertEqual(res.rejected[0]["p_value"], 0.5)

	def test_empty_input(self):
		self.assertEqual(apply_family_fdr({}), {})
		res = apply_family_fdr({"det-a": []})["det-a"]
		self.assertEqual((res.survivors, res.rejected, res.tested_n, res.untested_n), ([], [], 0, 0))


class TestDetectorFamilyBuffer(FrappeTestCase):
	def test_releases_previous_family_on_detector_change(self):
		buf = DetectorFamilyBuffer()
		# det-a runs across two companies (detector-major loop), then det-b
		# starts: det-a's family is complete and comes back filtered.
		self.assertIsNone(buf.add("det-a", [_cand(1e-9, co="C1")]))
		self.assertIsNone(buf.add("det-a", [_cand(0.6, co="C2")]))
		released = buf.add("det-b", [_cand(None, co="C1")])
		self.assertEqual(released.detector_id, "det-a")
		self.assertEqual([c["co"] for c in released.survivors], ["C1"])
		self.assertEqual([c["co"] for c in released.rejected], ["C2"])
		# det-b is still buffering.
		self.assertEqual(buf.pending_detector_id, "det-b")
		self.assertEqual(buf.pending_n, 1)

	def test_flush_releases_final_family(self):
		buf = DetectorFamilyBuffer()
		buf.add("det-z", [_cand(None), _cand(1e-6)])
		final = buf.flush()
		self.assertEqual(final.detector_id, "det-z")
		self.assertEqual(len(final.survivors), 2)
		self.assertIsNone(buf.flush())  # nothing left
		self.assertIsNone(buf.pending_detector_id)

	def test_flush_on_empty_buffer_is_none(self):
		self.assertIsNone(DetectorFamilyBuffer().flush())

	def test_counts_accumulate_across_families(self):
		buf = DetectorFamilyBuffer()
		buf.add("det-a", [_cand(1e-9), _cand(0.7)])
		buf.add("det-b", [_cand(None)])
		buf.add("det-b", [_cand(0.9)])
		buf.flush()
		self.assertEqual(
			buf.counts(),
			{
				"families": 2,
				"tested": 3,
				"untested": 1,
				"fdr_rejected": 2,
				"early_releases": 0,
				"peak_buffered": 2,
			},
		)

	def test_soft_cap_releases_early_and_is_counted(self):
		# A pathological many-company family must not become an unobservable
		# memory high-water mark: crossing the soft cap releases the buffered
		# chunk (a narrowed but valid per-chunk FDR grain) and counts it.
		buf = DetectorFamilyBuffer(soft_cap=3)
		self.assertIsNone(buf.add("det-a", [_cand(1e-9, co="C1"), _cand(1e-8, co="C2")]))
		released = buf.add("det-a", [_cand(0.9, co="C3")])  # 3rd candidate hits the cap
		self.assertIsNotNone(released)
		self.assertEqual(released.detector_id, "det-a")
		self.assertEqual(len(released.survivors) + released.rejected_n, 3)
		# The family keeps buffering under the SAME id after the early release.
		self.assertIsNone(buf.add("det-a", [_cand(0.5, co="C4")]))
		self.assertEqual(buf.pending_detector_id, "det-a")
		final = buf.flush()
		self.assertEqual(final.detector_id, "det-a")
		counts = buf.counts()
		self.assertEqual(counts["early_releases"], 1)
		self.assertEqual(counts["families"], 2)  # the chunk + the remainder
		self.assertEqual(counts["peak_buffered"], 3)

	def test_default_soft_cap_never_trips_on_normal_volumes(self):
		from jarvis.learning.fdr import FAMILY_SOFT_CAP

		buf = DetectorFamilyBuffer()
		self.assertEqual(buf.soft_cap, FAMILY_SOFT_CAP)
		self.assertIsNone(buf.add("det-a", [_cand(0.01) for _ in range(100)]))
		self.assertEqual(buf.counts()["early_releases"], 0)

	def test_partial_family_flush_pause_semantics(self):
		# A run pausing mid-family flushes what was tested this night; the
		# deferred companies rerun (and re-test) next night.
		buf = DetectorFamilyBuffer()
		buf.add("det-a", [_cand(0.001, co="C1")])
		final = buf.flush()
		self.assertEqual(final.detector_id, "det-a")
		self.assertEqual([c["co"] for c in final.survivors], ["C1"])

	def test_empty_units_still_complete_a_family(self):
		buf = DetectorFamilyBuffer()
		buf.add("det-a", [])
		released = buf.add("det-b", [])
		self.assertEqual(released.detector_id, "det-a")
		self.assertEqual((released.survivors, released.rejected), ([], []))


# ---------------------------------------------------------------------------
# statistical-level control: enrichment_p_value + BH on synthetic 2x2 tables
# ---------------------------------------------------------------------------
def _binom(rng, n, p):
	return sum(rng.random() < p for _ in range(n))


def _null_table_p(rng, base_rate=0.3, n=50, rest_n=450):
	"""One null table: segment and rest drawn from the SAME base rate."""
	k = _binom(rng, n, base_rate)
	rest_k = _binom(rng, rest_n, base_rate)
	p, _method = stats.enrichment_p_value(k, n, k + rest_k, n + rest_n)
	return p


def _planted_table_p(rng, segment_rate=0.85, base_rate=0.3, n=50, rest_n=450):
	"""One planted table: the segment is genuinely enriched vs the rest."""
	k = _binom(rng, n, segment_rate)
	rest_k = _binom(rng, rest_n, base_rate)
	p, _method = stats.enrichment_p_value(k, n, k + rest_k, n + rest_n)
	return p


class TestFdrNullControl(FrappeTestCase):
	def test_null_tables_yield_no_survivors_across_seeds(self):
		# 10 seeds x 200 null hypotheses per family: BH at q=0.05 holds the
		# complete null. Measured: 0 survivors on these seeds; <= 2 leaves
		# headroom without weakening the claim ("~zero").
		total = 0
		for seed in range(10):
			rng = random.Random(seed)
			family = [_cand(_null_table_p(rng)) for _ in range(200)]
			total += len(benjamini_hochberg(family, q=FDR_Q))
		self.assertLessEqual(total, 2)

	def test_planted_positives_survive_while_nulls_die(self):
		# Mixed family per seed: 3 planted (p ~ 1e-13) + 20 nulls. EVERY
		# planted positive must survive in EVERY seed. A few null riders are
		# FDR working as specified (q allows a small false share of the
		# discoveries): measured 4 across these 10 seeds against 30 true
		# discoveries; assert riders stay a small minority.
		planted_survived = 0
		riders = 0
		for seed in range(10):
			rng = random.Random(seed)
			family = [_cand(_planted_table_p(rng), planted=True) for _ in range(3)]
			family += [_cand(_null_table_p(rng), planted=False) for _ in range(20)]
			survivors = benjamini_hochberg(family, q=FDR_Q)
			got = sum(1 for s in survivors if s["planted"])
			self.assertEqual(got, 3, f"planted positive died under BH (seed {seed})")
			planted_survived += got
			riders += sum(1 for s in survivors if not s["planted"])
		self.assertEqual(planted_survived, 30)
		self.assertLessEqual(riders, 6)


# ---------------------------------------------------------------------------
# pipeline-level clustered-null fixtures (plan section 11, deferred item):
# unit counting + the full gate suite + Fisher/G-test + BH, end to end.
# ---------------------------------------------------------------------------
_VALUES = ["Standard", "Wholesale", "Retail", "Export", "Projects"]
_WEIGHTS = [0.55, 0.2, 0.1, 0.1, 0.05]
_BASE_DAY = datetime.date(2025, 9, 1)
_PLANTED = ("PLANTED-A", "PLANTED-B", "PLANTED-C")

_SPEC = {
	"id": "test-clustered-null",
	"domain": "selling",
	"gates": {"n_min": 20, "c_min": 0.80, "phrasing": "usually"},
	"antecedent_kind": "party",
	"unit_doctype": "documents",
	"skill_template": None,
}


def _unit_rows(rng, uid, party, value):
	"""1-3 child rows per parent unit, all carrying the parent's value:
	rows are CORRELATED within the parent doc (the i.i.d. trap) - unit
	counting must collapse them to one unit."""
	day = _BASE_DAY + datetime.timedelta(days=rng.randint(0, 299))
	created = datetime.datetime.combine(
		day, datetime.time(rng.randint(8, 18), rng.randint(0, 59), rng.randint(0, 59))
	)
	return [
		{"unit_id": f"U-{uid:05d}", "antecedent": party, "consequent": value, "day": day, "created": created}
		for _ in range(rng.randint(1, 3))
	]


def _clustered_null_rows(rng, parties=10, uid0=0):
	"""No true pattern: every unit's consequent is an i.i.d. draw from the
	site-wide marginal, whatever the party."""
	rows, uid = [], uid0
	for p in range(parties):
		party = f"PARTY-{p:02d}"
		for _ in range(rng.randint(80, 140)):
			uid += 1
			rows.extend(_unit_rows(rng, uid, party, rng.choices(_VALUES, weights=_WEIGHTS, k=1)[0]))
	return rows, uid


def _planted_pattern_rows(rng, uid0=100000):
	"""Three parties with a REAL habit: ~95% Wholesale over 60 units each."""
	rows, uid = [], uid0
	for party in _PLANTED:
		for _ in range(60):
			uid += 1
			value = "Wholesale" if rng.random() < 0.95 else rng.choices(_VALUES, weights=_WEIGHTS, k=1)[0]
			rows.extend(_unit_rows(rng, uid, party, value))
	return rows, uid


class TestClusteredNullPipeline(FrappeTestCase):
	def test_clustered_null_yields_no_proposals_across_seeds(self):
		# 10 seeds x 10 parties of pure noise, child-row clustered: the
		# pipeline (unit counting + gates + significance + BH) must propose
		# ~nothing. Measured: 0 candidates even reach the FDR pass on these
		# seeds; <= 1 total keeps the assertion honest, not flaky.
		total_proposals = 0
		for seed in range(10):
			rng = random.Random(seed)
			rows, _uid = _clustered_null_rows(rng)
			raws = reduce_units(rows, _SPEC, None)
			total_proposals += len(benjamini_hochberg(raws, q=FDR_Q))
		self.assertLessEqual(total_proposals, 1)

	def test_planted_patterns_survive_pipeline_nulls_die(self):
		# Mixed fixture through the DetectorFamilyBuffer, simulating the
		# engine's detector-major loop over two companies: survivors must be
		# EXACTLY the planted parties, in every seed, and each must carry a
		# p_value the FDR pass ranked on.
		for seed in range(10):
			rng = random.Random(1000 + seed)
			rows_a, uid = _clustered_null_rows(rng)
			planted, _uid = _planted_pattern_rows(rng)
			rows_b, _uid = _clustered_null_rows(rng, uid0=uid)

			buf = DetectorFamilyBuffer()
			self.assertIsNone(buf.add("det-mixed", reduce_units(rows_a + planted, _SPEC, None)))
			self.assertIsNone(buf.add("det-mixed", reduce_units(rows_b, _SPEC, None)))
			released = buf.add("det-next", [])
			self.assertEqual(released.detector_id, "det-mixed")

			survivors = sorted(c["antecedent_value"] for c in released.survivors)
			self.assertEqual(survivors, sorted(_PLANTED), f"seed {seed}")
			for cand in released.survivors:
				self.assertIsNotNone(cand["p_value"])
				self.assertLess(cand["p_value"], 1e-6)
				self.assertIn(cand["evidence"]["p_value_method"], ("fisher_exact", "g_test"))
			# Run accounting: the buffer reports what was tested; nothing
			# vanished silently.
			self.assertEqual(released.tested_n, len(released.survivors) + released.rejected_n)

	def test_finalize_copies_p_value_to_engine_candidate(self):
		# The engine/lifecycle candidate contract: _finalize must carry the
		# raw p_value to the top level (what the FDR buffer keys on) and the
		# evidence copy must match it.
		from jarvis.learning.executor import _finalize

		rng = random.Random(42)
		null_rows, uid = _clustered_null_rows(rng, parties=4)
		planted, _uid = _planted_pattern_rows(rng, uid0=uid)
		spec = dict(_SPEC, version=1, sensitivity="B", role_priors=[])
		raws = reduce_units(null_rows + planted, spec, None)
		self.assertTrue(raws)
		cand = _finalize(spec, "Acme Co", raws[0])
		self.assertIsNotNone(cand["p_value"])
		self.assertEqual(cand["p_value"], raws[0]["p_value"])
		self.assertEqual(cand["evidence"]["p_value"], cand["p_value"])
		self.assertEqual(default_p_value_key(cand), cand["p_value"])

	def test_executor_emits_p_value_and_method_in_evidence(self):
		# One deterministic planted segment: reduce_units must emit a tiny
		# one-sided p and mirror it (with its method) into evidence.
		rng = random.Random(42)
		null_rows, uid = _clustered_null_rows(rng, parties=4)
		planted, _uid = _planted_pattern_rows(rng, uid0=uid)
		raws = reduce_units(null_rows + planted, _SPEC, None)
		by_party = {r["antecedent_value"]: r for r in raws}
		for party in _PLANTED:
			self.assertIn(party, by_party)
			raw = by_party[party]
			self.assertLess(raw["p_value"], 1e-6)
			self.assertEqual(raw["evidence"]["p_value"], raw["p_value"])
			self.assertIn(raw["evidence"]["p_value_method"], ("fisher_exact", "g_test"))
			self.assertEqual(raw["p_value_method"], raw["evidence"]["p_value_method"])


# ---------------------------------------------------------------------------
# post-hoc consequent selection correction (within-segment Bonferroni): the
# argmax winner's single-value tail is optimistic by up to the number of
# candidate values, so the corrected p is what reaches the FDR buffer.
# ---------------------------------------------------------------------------
class TestSelectionCorrection(FrappeTestCase):
	SPEC = {
		"id": "test-selection",
		"domain": "selling",
		"gates": {"n_min": 20, "c_min": 0.80, "phrasing": "usually"},
		"antecedent_kind": "party",
		"unit_doctype": "documents",
		"skill_template": None,
	}

	@staticmethod
	def _segment_rows(party, values, uid0):
		"""One unit per (value) entry, spread over distinct days within a 60-day
		range so the spread/burst gates pass and no recency window divides it."""
		rows = []
		for i, value in enumerate(values):
			day = _BASE_DAY + datetime.timedelta(days=i % 60)
			created = datetime.datetime.combine(day, datetime.time(9 + (i % 8), i % 60))
			rows.append(
				{
					"unit_id": f"S-{uid0 + i:05d}",
					"antecedent": party,
					"consequent": value,
					"day": day,
					"created": created,
				}
			)
		return rows

	def _mixed_rows(self):
		# PARTY-A: 36x "X" + 4x "Y" interleaved (mode X, 2 candidate values).
		a_values = ["X"] * 36 + ["Y"] * 4
		a_values = [a_values[(i * 7) % 40] for i in range(40)]  # interleave
		rows = self._segment_rows("PARTY-A", a_values, 0)
		# PARTY-B: 10x "X" + 30x "Y" (rest population; own mode Y at 75% < c_min).
		b_values = ["X"] * 10 + ["Y"] * 30
		b_values = [b_values[(i * 3) % 40] for i in range(40)]
		rows += self._segment_rows("PARTY-B", b_values, 1000)
		return rows

	def test_reduce_units_applies_bonferroni_for_argmax_consequent(self):
		raws = reduce_units(self._mixed_rows(), self.SPEC, None)
		by_party = {r["antecedent_value"]: r for r in raws}
		self.assertIn("PARTY-A", by_party)
		raw = by_party["PARTY-A"]
		# The uncorrected one-sided tail for 36/40 vs the pooled 46/80, times
		# the segment's 2 candidate values, capped at 1.
		p_raw, _method = stats.enrichment_p_value(36, 40, 36 + 10, 40 + 40)
		self.assertAlmostEqual(raw["p_value"], min(1.0, p_raw * 2), places=12)
		self.assertIn("bonferroni x2", raw["evidence"]["p_value_correction"])

	def test_single_target_detector_stays_exact(self):
		spec = dict(self.SPEC, target_consequents=["X"])
		raws = reduce_units(self._mixed_rows(), spec, None)
		by_party = {r["antecedent_value"]: r for r in raws}
		self.assertIn("PARTY-A", by_party)
		raw = by_party["PARTY-A"]
		p_raw, _method = stats.enrichment_p_value(36, 40, 36 + 10, 40 + 40)
		self.assertAlmostEqual(raw["p_value"], p_raw, places=12)
		self.assertNotIn("p_value_correction", raw["evidence"])

	def test_correction_caps_at_one(self):
		from jarvis.learning.executor import evaluate_segment

		days = [_BASE_DAY + datetime.timedelta(days=i) for i in range(30)]
		created = [datetime.datetime.combine(d, datetime.time(10, 0)) for d in days]
		raw = evaluate_segment(
			self.SPEC,
			antecedent_value="P",
			consequent_value="X",
			k=25,
			n_units=30,
			base_rate=0.8,
			rest_k=240,
			rest_n=300,
			days=days,
			created=created,
			single_antecedent=True,
			n_candidate_values=500,
		)
		self.assertIsNotNone(raw)
		self.assertEqual(raw["p_value"], 1.0)
