"""Tier-1 + Tier-2 detector tests (plan section 11): each detector finds its
planted pattern at the right band and is SILENT on its trap.

Company-scoped detectors run against the seeded mini-org (isolated by the
company filter, so production data under other companies never leaks in).
Org-wide detectors (cfg-naming-series, cfg-default-vs-usage) are exercised with
a fake facade so their divergence logic is asserted deterministically without
depending on whatever else lives on the site.

Detectors run WITHOUT the READ ONLY fence here (a plain PatternDB in the test's
own transaction). The fence itself is covered by test_readonly_db and by the
engine's table-diff integration test (Wave B engine).
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.learning import registry, snapshots
from jarvis.learning.detectors import config as config_detectors
from jarvis.learning.executor import (
	_enforcement_conflict,
	_finalize,
	_state_predicts_template,
	reduce_units,
	run_detector,
)
from jarvis.learning.readonly_db import PatternDB
from jarvis.tests.learning import factory
from jarvis.tests.learning.test_snapshots import seed_print_log, wipe_print_state


def _po_rows(supplier, consequent, start_date, count, step_days=1, created_day=None):
	"""Synthetic unit-grain rows for reduce_units: one row per unit, distinct
	posting days; ``created_day`` (a single date) forces a one-session import
	burst, else creation days track posting days (organic)."""
	rows = []
	for i in range(count):
		day = frappe.utils.add_days(start_date, i * step_days)
		if created_day is not None:
			# multi-minute import cluster on one creation day (30s apart)
			created = f"{created_day} 03:{(i * 30) // 60:02d}:{(i * 30) % 60:02d}"
		else:
			created = f"{day} 10:00:00"
		rows.append(
			{
				"unit_id": f"{supplier}-{consequent}-{i}",
				"antecedent": supplier,
				"consequent": consequent,
				"company": factory.ALPHA,
				"day": day,
				"created": created,
			}
		)
	return rows


def _find(candidates, **kv):
	for c in candidates:
		if all(c.get(k) == v for k, v in kv.items()):
			return c
	return None


class TestTier1Detectors(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		factory.build(commit=True)

	@classmethod
	def tearDownClass(cls):
		factory.wipe(commit=True)
		super().tearDownClass()

	def _run(self, detector_id, company):
		return run_detector(registry.get_detector(detector_id), company, PatternDB())

	# --- selling ------------------------------------------------------------
	def test_customer_price_list_finds_dealer_and_silent_on_sole_value(self):
		res = self._run("sell-customer-price-list", factory.ALPHA)
		self.assertIsNone(res.skipped_reason)
		cand = _find(res.candidates, antecedent_value="_JPL-DealerD")
		self.assertIsNotNone(cand, "DealerD price-list pattern not found")
		self.assertEqual(cand["consequent_value"], "Dealer Pricing")
		self.assertIn(cand["strength_band"], ("High", "Medium"))
		self.assertEqual(cand["effective_sensitivity"], "B")  # party -> escalated
		self.assertGreaterEqual(cand["support_n"], 20)

		# sole-value trap: company Beta uses one price list -> variance suppressed
		beta = self._run("sell-customer-price-list", factory.BETA)
		self.assertIsNone(beta.skipped_reason)
		self.assertEqual(beta.candidates, [])

	def test_group_payment_terms_finds_dealer_with_exceptions(self):
		res = self._run("sell-group-payment-terms", factory.ALPHA)
		cand = _find(res.candidates, antecedent_value="Dealer")
		self.assertIsNotNone(cand)
		self.assertEqual(cand["consequent_value"], "30 Days")
		self.assertEqual(cand["exception_n"], 2)
		self.assertEqual(cand["sensitivity"], "A")
		# single-term company Beta -> variance suppressed
		self.assertEqual(self._run("sell-group-payment-terms", factory.BETA).candidates, [])

	def test_quotation_validity_diverges_and_silent_on_vanilla(self):
		res = self._run("sell-quotation-validity", factory.ALPHA)
		cand = _find(res.candidates, antecedent_value="org")
		self.assertIsNotNone(cand)
		self.assertIn("15", cand["pattern_statement"])
		# vanilla +1-month default in Beta -> suppressed
		self.assertEqual(self._run("sell-quotation-validity", factory.BETA).candidates, [])

	def test_tc_letterhead_diverges_from_company_default(self):
		res = self._run("sell-tc-letterhead", factory.ALPHA)
		cand = _find(res.candidates, consequent_value=factory.LETTER_HEAD)
		self.assertIsNotNone(cand)
		self.assertEqual(cand["sensitivity"], "A")

	def test_selective_item_pricing_existence(self):
		res = self._run("sell-selective-item-pricing", None)
		cand = _find(res.candidates, antecedent_value="org")
		self.assertIsNotNone(cand)
		self.assertGreaterEqual(cand["support_n"], factory.EXPECT["selective_customers"])
		self.assertEqual(cand["effective_sensitivity"], "B")

	# --- buying -------------------------------------------------------------
	def test_supplier_stockness_finds_acme(self):
		res = self._run("buy-supplier-stockness", factory.ALPHA)
		cand = _find(res.candidates, antecedent_value="_JPL-AcmeNonStock")
		self.assertIsNotNone(cand)
		self.assertEqual(cand["consequent_value"], "non_stock")
		self.assertEqual(cand["support_n"], 40)
		self.assertEqual(cand["strength_band"], "High")
		# a stock supplier is not proposed (target_consequents restricts to non_stock)
		self.assertIsNone(_find(res.candidates, antecedent_value="_JPL-BoltSupply"))
		# wording matches the gate: 40u tendency band must NOT over-claim "only"
		# (fix 3), and the "usually" gate never fires the enforcement cross-ref.
		self.assertIn("usually", cand["pattern_statement"])
		self.assertNotIn("only", cand["pattern_statement"])
		self.assertIsNone(cand["enforcement_conflict"])

	def test_supplier_itemgroup_finds_bolt_and_gates_child_row_inflation(self):
		res = self._run("buy-supplier-itemgroup", factory.ALPHA)
		bolt = _find(res.candidates, antecedent_value="_JPL-BoltSupply")
		self.assertIsNotNone(bolt)
		self.assertEqual(bolt["consequent_value"], "Fasteners")
		# child-row inflation trap: 6 POs x 10 lines -> 6 units < n_min -> silent
		self.assertIsNone(_find(res.candidates, antecedent_value="_JPL-InflateCo"))

	def test_supplier_tax_template_finds_taxhabit_with_one_exception(self):
		res = self._run("buy-supplier-tax-template", factory.ALPHA)
		cand = _find(res.candidates, antecedent_value="_JPL-TaxHabit")
		self.assertIsNotNone(cand)
		self.assertEqual(cand["consequent_value"], "Alpha GST 18%")
		self.assertEqual(cand["exception_n"], 1)
		# geography-confound caveat is always attached (fix 5): a tax template by
		# supplier can encode state-driven GST, not a per-supplier habit.
		self.assertIn("geography_caveat", cand["evidence"])
		self.assertIn("geography", cand["pattern_statement"].lower())

	def test_pi_update_stock_finds_habit_and_silent_on_burst(self):
		res = self._run("buy-pi-update-stock", factory.ALPHA)
		cand = _find(res.candidates, antecedent_value="org")
		self.assertIsNotNone(cand)
		self.assertEqual(cand["consequent_value"], "1")
		self.assertEqual(cand["sensitivity"], "B")  # process control
		# go-live burst trap in Beta (one day, one second) -> spread gate silent
		self.assertEqual(self._run("buy-pi-update-stock", factory.BETA).candidates, [])

	# --- stock --------------------------------------------------------------
	def test_itemgroup_warehouse_single_plant_and_multiplant_skip(self):
		res = self._run("stock-itemgroup-warehouse", factory.ALPHA)
		self.assertIsNone(res.skipped_reason)
		self.assertIsNotNone(_find(res.candidates, antecedent_value="Electronics"))
		self.assertIsNotNone(_find(res.candidates, antecedent_value="Furniture"))
		# multi-plant trap in Beta (5 warehouses) -> skipped with coverage note
		beta = self._run("stock-itemgroup-warehouse", factory.BETA)
		self.assertIsNotNone(beta.skipped_reason)
		self.assertIn("multi-plant", beta.skipped_reason)

	def test_stock_entry_purpose_mix(self):
		res = self._run("stock-entry-purpose-mix", factory.ALPHA)
		self.assertIsNotNone(_find(res.candidates, antecedent_value="Material Transfer"))
		self.assertIsNotNone(_find(res.candidates, antecedent_value="Material Receipt"))

	# --- accounts -----------------------------------------------------------
	def test_mode_of_payment(self):
		res = self._run("acct-mode-of-payment", factory.ALPHA)
		recv = _find(res.candidates, antecedent_value="Receive")
		pay = _find(res.candidates, antecedent_value="Pay")
		self.assertIsNotNone(recv)
		self.assertEqual(recv["consequent_value"], "Bank Draft")
		self.assertIsNotNone(pay)
		self.assertEqual(pay["consequent_value"], "Cash")

	# --- projects -----------------------------------------------------------
	def test_billing_method(self):
		res = self._run("proj-billing-method", factory.ALPHA)
		cand = _find(res.candidates, antecedent_value="External")
		self.assertIsNotNone(cand)
		self.assertIn("timesheet", cand["consequent_value"])

	# --- candidate contract sanity -----------------------------------------
	def test_candidate_contract_shape(self):
		res = self._run("sell-group-payment-terms", factory.ALPHA)
		cand = _find(res.candidates, antecedent_value="Dealer")
		required = {
			"detector_id",
			"detector_version",
			"registry_version",
			"domain",
			"company",
			"pattern_key",
			"roles",
			"pattern_statement",
			"skill_draft",
			"support_n",
			"n_rows",
			"exception_n",
			"confidence_pct",
			"wilson_low",
			"gap",
			"strength_band",
			"temporal_spread",
			"evidence",
			"exceptions",
			"exceptions_cluster",
			"sensitivity",
			"effective_sensitivity",
			"not_applicable",
		}
		self.assertTrue(required.issubset(set(cand)), required - set(cand))
		self.assertEqual(len(cand["pattern_key"]), 40)
		self.assertEqual(cand["detector_id"], "sell-group-payment-terms")
		self.assertIsInstance(cand["roles"], list)
		self.assertIsInstance(cand["evidence"], dict)
		self.assertTrue(cand["skill_draft"].startswith("- "))
		self.assertIn("Evidence:", cand["skill_draft"])


# ---------------------------------------------------------------------------
# org-wide config detectors: deterministic fake-facade tests
# ---------------------------------------------------------------------------
class _FakePatternDB:
	"""Maps a SQL substring to a canned result list; everything else is []."""

	def __init__(self, mapping):
		self._mapping = mapping

	def _rows(self, query):
		for needle, rows in self._mapping:
			if needle in query:
				return rows
		return []

	def timed_select(self, query, params=None, **kwargs):
		return self._rows(query)

	def sql_select(self, query, params=None, **kwargs):
		return self._rows(query)


def _usage_rows(consequent, n, start=0):
	import frappe

	rows = []
	for i in range(n):
		day = frappe.utils.add_days("2025-09-01", start + i)  # distinct calendar days
		rows.append(
			{
				"unit_id": f"row-{start + i}",
				"consequent": consequent,
				"day": day,
				"created": f"{day} 10:00:00",
			}
		)
	return rows


class TestConfigDetectorsFake(FrappeTestCase):
	def test_default_vs_usage_proposes_on_divergence(self):
		spec = registry.get_detector("cfg-default-vs-usage")
		# configured default = Standard Selling; realized usage = Custom PL
		db = _FakePatternDB(
			[
				("tabSingles", [{"value": "Standard Selling"}]),
				("tabSales Invoice", _usage_rows("Custom PL", 40)),
			]
		)
		out = config_detectors.postprocess_default_vs_usage(
			None, spec, None, db, {"window_start": "2025-01-01"}
		)
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["consequent_value"], "Custom PL")
		self.assertEqual(out[0]["evidence"]["configured_default"], "Standard Selling")

	def test_default_vs_usage_silent_when_usage_matches_default(self):
		spec = registry.get_detector("cfg-default-vs-usage")
		db = _FakePatternDB(
			[
				("tabSingles", [{"value": "Standard Selling"}]),
				("tabSales Invoice", _usage_rows("Standard Selling", 40)),
			]
		)
		out = config_detectors.postprocess_default_vs_usage(
			None, spec, None, db, {"window_start": "2025-01-01"}
		)
		self.assertEqual(out, [])

	def test_naming_series_merges_and_proposes(self):
		spec = registry.get_detector("cfg-naming-series")
		db = _FakePatternDB(
			[
				("naming_series` AS consequent", _usage_rows("SINV-.####", 30)),
				("tabProperty Setter", []),
				("tabSeries", [{"series": "SINV-", "current": 42}]),
			]
		)
		out = config_detectors.postprocess_naming_series(None, spec, None, db, {"window_start": "2025-01-01"})
		self.assertGreaterEqual(len(out), 1)
		first = out[0]
		self.assertEqual(first["consequent_value"], "SINV-.####")
		self.assertEqual(first["evidence"]["sql_shape"], "S1")


class TestEnforcementCrossRef(FrappeTestCase):
	def test_no_conflict_returns_none(self):
		self.assertIsNone(_enforcement_conflict("Purchase Invoice", "update_stock", PatternDB()))

	def test_active_server_script_badges(self):
		name = "_JPL-ES-updatestock"
		try:
			import frappe

			frappe.db.delete("Server Script", {"name": name})
			doc = frappe.new_doc("Server Script")
			doc.update(
				{
					"script_type": "DocType Event",
					"reference_doctype": "Purchase Invoice",
					"doctype_event": "Before Save",
					"disabled": 0,
					"script": "if doc.update_stock: pass",
				}
			)
			doc.name = name
			doc.flags.name_set = True
			doc.db_insert()
		except Exception as exc:  # environment without Server Script table shape
			self.skipTest(f"could not seed Server Script: {exc}")
		try:
			badge = _enforcement_conflict("Purchase Invoice", "update_stock", PatternDB())
			self.assertIsNotNone(badge)
			self.assertIn("Server Script", badge)
		finally:
			frappe.db.delete("Server Script", {"name": name})


# ---------------------------------------------------------------------------
# fix 1: recency guard (reduce_units, no fixtures needed)
# ---------------------------------------------------------------------------
class TestRecencyGuard(FrappeTestCase):
	"""A segment whose modal value flipped inside the last-90-day window must
	propose the RECENT behaviour (annotated) or withhold - never the stale
	full-window average."""

	def _reduce(self, rows):
		spec = registry.get_detector("buy-supplier-tax-template")
		return reduce_units(rows, spec, PatternDB())

	def test_flip_retargets_to_recent_and_annotates(self):
		rows = []
		# SupFlip: OldGST for 40 old POs, then NewGST for 25 recent POs -> flip.
		rows += _po_rows("SupFlip", "OldGST", "2025-01-01", 40, step_days=3)
		rows += _po_rows("SupFlip", "NewGST", "2025-09-01", 25)
		# SupFlip2: same flip but only 10 recent POs -> cannot stand alone.
		rows += _po_rows("SupFlip2", "OldGST", "2025-01-02", 40, step_days=3)
		rows += _po_rows("SupFlip2", "NewGST", "2025-08-20", 10)
		# SupSame: consistent, recent-only -> normal candidate, no recency note.
		rows += _po_rows("SupSame", "SameGST", "2025-07-01", 40)
		# SupB: variance ballast + a recent base rate.
		rows += _po_rows("SupB", "ConstGST", "2025-06-27", 40)
		cands = self._reduce(rows)

		flip = _find(cands, antecedent_value="SupFlip")
		self.assertIsNotNone(flip, "flipped supplier must still propose the RECENT habit")
		self.assertEqual(flip["consequent_value"], "NewGST")
		self.assertEqual(flip["n_units"], 25)  # recent subset only, not 65
		self.assertIn("recency", flip["evidence"])
		self.assertIn("behavior changed around", flip["evidence"]["recency"])

		# flip with too little recent evidence -> withheld entirely (not stale Old)
		self.assertIsNone(_find(cands, antecedent_value="SupFlip2"))

		same = _find(cands, antecedent_value="SupSame")
		self.assertIsNotNone(same)
		self.assertNotIn("recency", same["evidence"])


# ---------------------------------------------------------------------------
# fix 4: burst gate strengthened (multi-minute imports collapse)
# ---------------------------------------------------------------------------
class TestBurstGate(FrappeTestCase):
	def test_multi_minute_import_burst_is_collapsed(self):
		spec = registry.get_detector("buy-supplier-tax-template")
		rows = []
		# Organic: 25 POs, distinct posting AND creation days.
		rows += _po_rows("OrganicSup", "T-Org", "2025-09-01", 25)
		# Control: 30 organic POs -> proves the gate does not reject volume alone.
		rows += _po_rows("ControlSup", "T-Ctrl", "2025-07-01", 30)
		# Import: 30 POs across 30 distinct POSTING days but ONE creation session
		# (30s apart) -> must collapse, satisfying neither n_min nor creation-day
		# spread even though posting spread passes.
		rows += _po_rows("ImportSup", "T-Imp", "2025-08-01", 30, created_day="2025-10-01")
		cands = reduce_units(rows, spec, PatternDB())

		self.assertIsNotNone(_find(cands, antecedent_value="OrganicSup"))
		self.assertIsNotNone(_find(cands, antecedent_value="ControlSup"))
		self.assertIsNone(
			_find(cands, antecedent_value="ImportSup"),
			"a one-session import backfilling many posting dates must be collapsed",
		)


# ---------------------------------------------------------------------------
# fix 2: enforcement cross-ref fires for an "only"-band candidate
# ---------------------------------------------------------------------------
class TestEnforcementFires(FrappeTestCase):
	SCRIPT = "_JPL-ES-po-stockness"

	def setUp(self):
		frappe.db.delete("Server Script", {"name": self.SCRIPT})
		try:
			doc = frappe.new_doc("Server Script")
			doc.update(
				{
					"script_type": "DocType Event",
					"reference_doctype": "Purchase Order",
					"doctype_event": "Before Save",
					"disabled": 0,
					"script": "if doc.get('items'): pass  # touches is_stock_item",
				}
			)
			doc.name = self.SCRIPT
			doc.flags.name_set = True
			doc.db_insert()
		except Exception as exc:
			self.skipTest(f"could not seed Server Script: {exc}")

	def tearDown(self):
		frappe.db.delete("Server Script", {"name": self.SCRIPT})

	def test_only_band_fires_cross_ref_but_tendency_does_not(self):
		spec = registry.get_detector("buy-supplier-stockness")

		# 65 non-stock POs, 0 exceptions -> "only" (rule-of-three) band -> fires.
		rows = _po_rows("SupOnly", "non_stock", "2025-05-01", 65)
		rows += _po_rows("SupStock", "has_stock", "2025-05-01", 10)  # variance ballast
		cands = reduce_units(rows, spec, PatternDB())
		only = _find(cands, antecedent_value="SupOnly")
		self.assertIsNotNone(only)
		self.assertEqual(only["n_units"], 65)
		self.assertIsNotNone(only["enforcement_conflict"], "60u/0-exc claim must cross-ref")
		self.assertIn("Server Script", only["enforcement_conflict"])
		self.assertIn("enforcement_conflict", only["evidence"])

		# 40 non-stock POs -> tendency band (<60) -> cross-ref must NOT fire even
		# with the same active Server Script present.
		rows2 = _po_rows("Sup40", "non_stock", "2025-05-01", 40)
		rows2 += _po_rows("SupStockBig", "has_stock", "2025-05-01", 30)
		cands2 = reduce_units(rows2, spec, PatternDB())
		tend = _find(cands2, antecedent_value="Sup40")
		self.assertIsNotNone(tend)
		self.assertIsNone(tend["enforcement_conflict"])


# ---------------------------------------------------------------------------
# fix 3: strict "only" wording only when the evidence earns it
# ---------------------------------------------------------------------------
class TestStocknessStrictWording(FrappeTestCase):
	def test_strict_only_wording_at_rule_of_three_band(self):
		spec = registry.get_detector("buy-supplier-stockness")
		rows = _po_rows("SupOnly", "non_stock", "2025-05-01", 65)
		rows += _po_rows("SupStock", "has_stock", "2025-05-01", 10)
		raws = reduce_units(rows, spec, PatternDB())
		raw = _find(raws, antecedent_value="SupOnly")
		self.assertIsNotNone(raw)
		cand = _finalize(spec, factory.ALPHA, raw)
		self.assertIn("supplies only non-stock", cand["pattern_statement"])
		self.assertIn("only non-stock", cand["skill_draft"])


# ---------------------------------------------------------------------------
# fix 5: geography-confound helper
# ---------------------------------------------------------------------------
class TestGeographyConfound(FrappeTestCase):
	def test_state_predicts_template_true(self):
		states = {"p1": "MH", "p2": "MH", "p3": "KA", "p4": "KA"}
		templates = {"p1": "GST18", "p2": "GST18", "p3": "GST12", "p4": "GST12"}
		self.assertTrue(_state_predicts_template(states, templates))

	def test_state_does_not_predict_template(self):
		states = {"p1": "MH", "p2": "MH", "p3": "KA", "p4": "KA"}
		templates = {"p1": "GST18", "p2": "GST12", "p3": "GST18", "p4": "GST12"}
		self.assertFalse(_state_predicts_template(states, templates))

	def test_single_state_is_not_a_confound(self):
		self.assertFalse(_state_predicts_template({"p1": "MH", "p2": "MH"}, {"p1": "G", "p2": "G"}))

	def test_empty_is_not_a_confound(self):
		self.assertFalse(_state_predicts_template({}, {}))


# ---------------------------------------------------------------------------
# fix 6: run_detector reports an approximate scanned-row count
# ---------------------------------------------------------------------------
class _RowsDB:
	"""Facade stub: timed_select returns a fixed row list; sql_select empty."""

	def __init__(self, rows):
		self._rows = rows

	def timed_select(self, query, params=None, **kwargs):
		return list(self._rows)

	def sql_select(self, query, params=None, **kwargs):
		return []


class TestScannedRows(FrappeTestCase):
	def test_run_detector_charges_materialized_rows(self):
		spec = registry.get_detector("acct-mode-of-payment")
		rows = [
			{
				"unit_id": f"pe{i}",
				"antecedent": "Receive",
				"consequent": "Cash",
				"day": frappe.utils.add_days("2025-09-01", i),
				"created": "2025-09-01 10:00:00",
			}
			for i in range(7)
		]
		res = run_detector(spec, factory.ALPHA, _RowsDB(rows))
		self.assertEqual(res.rows_scanned, 7)


# ---------------------------------------------------------------------------
# Phase 2: sell-customer-print-format (S4 over Access Log + monthly snapshots)
# ---------------------------------------------------------------------------
class TestPrintFormatDetector(FrappeTestCase):
	"""Planted pattern (DealerD prints "Tax Invoice" 12x + 1 exception) proposes
	at B sensitivity with tendency wording; a 3-print trap stays silent; the
	evidence states the available log depth; snapshot history and the live
	Access-Log tail combine without double-counting (watermark-disjoint)."""

	DETECTOR_ID = "sell-customer-print-format"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		factory.build(commit=True)
		wipe_print_state()
		day = "2025-09-{:02d} 10:00:00"
		# planted: 12 DealerD invoices printed with "Tax Invoice" on 12 distinct
		# days (organic: no burst collapse) + 1 exception print with "Standard"
		for i in range(12):
			seed_print_log("Sales Invoice", f"_JPL-SI-A-{i:04d}", "Tax Invoice", day.format(i + 2))
		seed_print_log("Sales Invoice", "_JPL-SI-A-0012", "Standard", day.format(15))
		# variance ballast: RetailR (ordinals 60+) prints 11x "Retail Slip", so
		# the site is not a sole-format org (the variance gate stays open)
		for i in range(11):
			seed_print_log("Sales Invoice", f"_JPL-SI-A-{60 + i:04d}", "Retail Slip", day.format(i + 2))
		# trap: DealerE (ordinals 25+) prints only 3x -> below the 10-event gate
		for i in range(3):
			seed_print_log("Sales Invoice", f"_JPL-SI-A-{25 + i:04d}", "Rare Format", day.format(i + 2))
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		wipe_print_state()
		factory.wipe(commit=True)
		frappe.db.commit()
		super().tearDownClass()

	def _spec(self):
		"""Spec copy with the source gate lifted so detector logic is testable in
		isolation from the Detector State availability flag."""
		spec = dict(registry.get_detector(self.DETECTOR_ID))
		spec["requires_source"] = None
		return spec

	def test_requires_source_runs_unless_marked_not_applicable(self):
		# Availability semantics: with no not_applicable mark the detector RUNS...
		res = run_detector(registry.get_detector(self.DETECTOR_ID), factory.ALPHA, PatternDB())
		self.assertIsNone(res.skipped_reason)
		# ...and skips honestly once the preflight marks the source dead.
		# (wipe_print_state clears the state row; set_value on a missing row is
		# a silent no-op, so ensure it exists first.)
		if not frappe.db.exists("Jarvis Pattern Detector State", self.DETECTOR_ID):
			frappe.get_doc(
				{
					"doctype": "Jarvis Pattern Detector State",
					"detector_id": self.DETECTOR_ID,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Jarvis Pattern Detector State",
			self.DETECTOR_ID,
			"not_applicable",
			1,
			update_modified=False,
		)
		frappe.db.commit()
		try:
			res = run_detector(registry.get_detector(self.DETECTOR_ID), factory.ALPHA, PatternDB())
			self.assertIsNotNone(res.skipped_reason)
			self.assertIn("unavailable", res.skipped_reason)
		finally:
			frappe.db.set_value(
				"Jarvis Pattern Detector State",
				self.DETECTOR_ID,
				"not_applicable",
				0,
				update_modified=False,
			)
			frappe.db.commit()

	def test_planted_pattern_proposes_and_trap_is_silent(self):
		res = run_detector(self._spec(), factory.ALPHA, PatternDB())
		self.assertIsNone(res.skipped_reason)
		cand = _find(res.candidates, antecedent_value="_JPL-DealerD")
		self.assertIsNotNone(cand, "DealerD print-format habit not found")
		self.assertEqual(cand["consequent_value"], "Tax Invoice")
		self.assertEqual(cand["support_n"], 13)
		self.assertEqual(cand["exception_n"], 1)
		self.assertEqual(cand["sensitivity"], "B")
		self.assertEqual(cand["effective_sensitivity"], "B")  # names a customer
		# 12 effective events < 30 -> tendency wording, never "usually"
		self.assertIn("tend to be printed", cand["pattern_statement"])
		self.assertNotIn("usually", cand["pattern_statement"])
		# evidence states the available log depth (plan section 4.2)
		self.assertIn("days of print logs", cand["evidence"]["log_depth_note"])
		self.assertGreater(cand["evidence"]["log_depth_days"], 0)
		self.assertEqual(cand["evidence"]["sql_shape"], "S4")
		# trap: 3 prints is below the 10-event gate -> silent
		self.assertIsNone(_find(res.candidates, antecedent_value="_JPL-DealerE"))
		# company scoping: Beta printed nothing
		beta = run_detector(self._spec(), factory.BETA, PatternDB())
		self.assertIsNone(beta.skipped_reason)
		self.assertEqual(beta.candidates, [])

	def test_snapshot_history_and_live_tail_combine(self):
		# move everything seeded so far into monthly snapshots (engine-side,
		# post-fence step the engine one-liner performs after mining)...
		summary = snapshots.ingest_print_log()
		self.assertGreater(summary.get("snapshots", 0), 0)
		# ...then one NEW live print beyond the advanced watermark
		seed_print_log("Sales Invoice", "_JPL-SI-A-0013", "Tax Invoice", "2025-10-02 10:00:00")
		frappe.db.commit()

		res = run_detector(self._spec(), factory.ALPHA, PatternDB())
		cand = _find(res.candidates, antecedent_value="_JPL-DealerD")
		self.assertIsNotNone(cand)
		# 13 snapshot events + 1 live event; watermark keeps the two disjoint
		self.assertEqual(cand["support_n"], 14)
		self.assertEqual(cand["evidence"]["live_events"], 1)
		self.assertGreaterEqual(cand["evidence"]["snapshot_events"], 13)
		self.assertTrue(cand["evidence"]["snapshot_periods"])


# ---------------------------------------------------------------------------
# Tier-2 pack (plan sections 4.2 marked rows, 4.4)
# ---------------------------------------------------------------------------
class TestTier2Registry(FrappeTestCase):
	"""Registry metadata: tier keys, work-unit scheduling, and resolvable
	SQL/postprocess/template wiring for every Tier-2 spec."""

	EXPECTED_TIER2 = {
		"sell-customer-print-format",
		"acct-party-tax-template",
		"sell-customer-payment-terms",
		"buy-supplier-payment-terms-realized",
		"stock-itemgroup-warehouse-dimensioned",
		"stock-uom-conversion",
		"stock-batch-serial-usage",
		"acct-cost-center-dimension",
		"acct-je-usage",
		"acct-deferred-usage",
		"mfg-default-bom-usage",
		"proj-timesheet-rate-defaults",
		"cfg-custom-field-always-filled",
		"role-doctype-routing",
	}

	def test_tier_views(self):
		self.assertEqual({s["id"] for s in registry.TIER2_DETECTORS}, self.EXPECTED_TIER2)
		# every spec has a resolvable tier; anything unmarked defaults to 1
		for spec in registry.DETECTORS:
			self.assertIn(int(spec.get("tier", 1)), (1, 2), spec["id"])
		# bootstrap/engine seeding contract: the historical TIER1_DETECTORS name
		# stays an alias of ALL_DETECTORS so Tier-2 specs are seeded/rehydrated
		# with zero bootstrap or engine changes.
		self.assertIs(registry.TIER1_DETECTORS, registry.ALL_DETECTORS)
		self.assertEqual(len(registry.ALL_DETECTORS), len(registry.DETECTORS))

	def test_tier2_specs_are_wired(self):
		from jarvis.learning.executor import _resolve
		from jarvis.learning.skill_drafts import SKILL_TEMPLATES

		for spec in registry.TIER2_DETECTORS:
			if spec.get("sql"):
				self.assertTrue(callable(getattr(_resolve(spec["sql"]), "format", None)), spec["id"])
			if spec.get("postprocess"):
				self.assertTrue(callable(_resolve(spec["postprocess"])), spec["id"])
			self.assertIn(spec["skill_template"], SKILL_TEMPLATES, spec["id"])
			self.assertIn("n_min", spec.get("gates") or {}, spec["id"])
			self.assertIn(spec.get("sensitivity"), ("A", "B"), spec["id"])

	def test_iter_work_units_schedules_tier2(self):
		units = list(registry.iter_work_units(["C1"], registry.TIER2_DETECTORS))
		pairs = [(company, spec["id"]) for company, spec in units]
		self.assertIn(("C1", "acct-party-tax-template"), pairs)
		self.assertIn(("C1", "stock-itemgroup-warehouse-dimensioned"), pairs)
		self.assertIn((None, "cfg-custom-field-always-filled"), pairs)
		self.assertIn((None, "role-doctype-routing"), pairs)
		self.assertIn((None, "stock-batch-serial-usage"), pairs)


class TestTier2Detectors(FrappeTestCase):
	"""Company-scoped Tier-2 detectors against the seeded mini-org (planted
	pattern found, trap silent), mirroring the Tier-1 class."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		factory.build(commit=True)

	@classmethod
	def tearDownClass(cls):
		factory.wipe(commit=True)
		super().tearDownClass()

	def _run(self, detector_id, company):
		return run_detector(registry.get_detector(detector_id), company, PatternDB())

	# --- S5 realized-vs-master payment terms ---------------------------------
	def test_customer_payment_terms_divergence_and_master_match_trap(self):
		res = self._run("sell-customer-payment-terms", factory.ALPHA)
		self.assertIsNone(res.skipped_reason)
		dealer = _find(res.candidates, antecedent_value="_JPL-DealerD")
		self.assertIsNotNone(dealer, "DealerD realized-vs-master divergence not found")
		self.assertEqual(dealer["consequent_value"], "30 Days")
		self.assertEqual(dealer["evidence"]["master_payment_terms"], "45 Days")
		self.assertIn("45 Days", dealer["pattern_statement"])
		self.assertEqual(dealer["effective_sensitivity"], "B")  # names a customer
		# unset master proposes with the unset clause
		unset = _find(res.candidates, antecedent_value="_JPL-DealerE")
		self.assertIsNotNone(unset)
		self.assertIn("no default payment terms", unset["pattern_statement"])
		# trap: RetailR's master MATCHES its realized terms -> suppressed
		self.assertIsNone(_find(res.candidates, antecedent_value="_JPL-RetailR"))

	def test_supplier_payment_terms_divergence_and_master_match_trap(self):
		res = self._run("buy-supplier-payment-terms-realized", factory.ALPHA)
		self.assertIsNone(res.skipped_reason)
		bolt = _find(res.candidates, antecedent_value="_JPL-BoltSupply")
		self.assertIsNotNone(bolt)
		self.assertEqual(bolt["consequent_value"], "Net 15")
		self.assertEqual(bolt["evidence"]["master_payment_terms"], "Net 45")
		self.assertEqual(bolt["effective_sensitivity"], "B")
		# trap: Acme's master matches -> suppressed
		self.assertIsNone(_find(res.candidates, antecedent_value="_JPL-AcmeNonStock"))

	# --- multi-plant dimensioned warehouse variant ----------------------------
	def test_itemgroup_warehouse_dimensioned_multiplant(self):
		res = self._run("stock-itemgroup-warehouse-dimensioned", factory.GAMMA)
		self.assertIsNone(res.skipped_reason)
		north = _find(res.candidates, antecedent_value="Electronics :: _JPL-CC-North")
		self.assertIsNotNone(north, "dimensioned (group, cost center) pair not found")
		self.assertEqual(north["consequent_value"], "WH-Gamma-N1")
		self.assertIn("_JPL-CC-North", north["pattern_statement"])
		self.assertIn("Electronics", north["pattern_statement"])
		south = _find(res.candidates, antecedent_value="Furniture :: _JPL-CC-South")
		self.assertIsNotNone(south)
		self.assertEqual(south["consequent_value"], "WH-Gamma-S1")
		# strays (3 units) never clear n_min
		self.assertIsNone(_find(res.candidates, antecedent_value="Widgets :: _JPL-CC-East"))

	def test_dimensioned_variant_is_mirror_inverse_of_tier1_guard(self):
		# single-plant Alpha: Tier-2 variant skips (Tier-1 owns the company)...
		alpha = self._run("stock-itemgroup-warehouse-dimensioned", factory.ALPHA)
		self.assertIsNotNone(alpha.skipped_reason)
		self.assertIn("single-plant", alpha.skipped_reason)
		# ...and multi-plant Beta runs the Tier-2 variant (Tier-1 skips it) but
		# finds nothing because Beta rows carry no cost centers.
		beta = self._run("stock-itemgroup-warehouse-dimensioned", factory.BETA)
		self.assertIsNone(beta.skipped_reason)
		self.assertEqual(beta.candidates, [])

	# --- the Phase-2 headliner: party/segment -> tax template ------------------
	def test_party_tax_template_geography_demotes_and_tax_rule_suppresses(self):
		res = self._run("acct-party-tax-template", factory.GAMMA)
		self.assertIsNone(res.skipped_reason)
		north = _find(res.candidates, consequent_value=factory.EXPECT["north_tax_template"])
		self.assertIsNotNone(north, "discovered-segment tax-template pattern not found")
		self.assertEqual(north["antecedent_value"], "Customer.territory=_JPL-North")
		self.assertEqual(north["sensitivity"], "B")
		self.assertEqual(north["effective_sensitivity"], "B")
		# geography confound: state predicts the template as well as the
		# segment -> band demoted (32/32 is Medium on Wilson; demoted to Low)
		self.assertEqual(north["strength_band"], "Low")
		self.assertEqual(north["evidence"]["geography_confound"], "state predicts template")
		self.assertIn("geography_caveat", north["evidence"])
		# compiled text always carries the geography warning
		self.assertIn("geography", north["pattern_statement"].lower())
		self.assertIn("geography", north["skill_draft"].lower())
		# trap: an active Tax Rule already encodes the 12% template -> suppressed
		self.assertIsNone(_find(res.candidates, consequent_value=factory.EXPECT["south_tax_template"]))

	# --- cost-center dimension --------------------------------------------------
	def test_cost_center_dimension(self):
		res = self._run("acct-cost-center-dimension", factory.GAMMA)
		self.assertIsNone(res.skipped_reason)
		north = _find(res.candidates, antecedent_value="WH-Gamma-N1")
		self.assertIsNotNone(north)
		self.assertEqual(north["consequent_value"], "_JPL-CC-North")
		self.assertEqual(north["sensitivity"], "A")
		# stray warehouses (1 unit each) never clear n_min
		self.assertIsNone(_find(res.candidates, antecedent_value="WH-Gamma-X1"))
		# Alpha rows carry no cost centers: clean empty pass, not a skip
		alpha = self._run("acct-cost-center-dimension", factory.ALPHA)
		self.assertIsNone(alpha.skipped_reason)
		self.assertEqual(alpha.candidates, [])

	# --- UOM conversion ---------------------------------------------------------
	def test_uom_conversion_and_stock_uom_trap(self):
		res = self._run("stock-uom-conversion", factory.ALPHA)
		self.assertIsNone(res.skipped_reason)
		elec = _find(res.candidates, antecedent_value="Electronics")
		self.assertIsNotNone(elec, "transacted-UOM divergence not found")
		self.assertEqual(elec["consequent_value"], "Box")
		self.assertEqual(elec["strength_band"], "High")
		# trap: Furniture transacts in its stock UOM (the vanilla default)
		self.assertIsNone(_find(res.candidates, antecedent_value="Furniture"))

	# --- JE usage ----------------------------------------------------------------
	def test_je_usage_template_and_voucher_type_habits(self):
		res = self._run("acct-je-usage", factory.ALPHA)
		self.assertIsNone(res.skipped_reason)
		tmpl = _find(res.candidates, antecedent_value="Bank Entry")
		self.assertIsNotNone(tmpl, "JE from-template habit not found")
		self.assertEqual(tmpl["consequent_value"], factory.EXPECT["je_template"])
		self.assertIn(factory.EXPECT["je_template"], tmpl["pattern_statement"])
		org = _find(res.candidates, antecedent_value="org:voucher_type")
		self.assertIsNotNone(org, "org voucher-type habit not found")
		self.assertEqual(org["consequent_value"], "Bank Entry")
		self.assertEqual(org["evidence"]["voucher_type_mix"]["Bank Entry"], 35)
		# trap: the manual voucher type (4 units, mode __manual__) stays silent
		self.assertIsNone(_find(res.candidates, antecedent_value="Journal Entry"))

	# --- deferred usage -----------------------------------------------------------
	def test_deferred_usage_expense_and_revenue_sides(self):
		alpha = self._run("acct-deferred-usage", factory.ALPHA)
		self.assertIsNone(alpha.skipped_reason)
		services = _find(alpha.candidates, antecedent_value="Services :: expense")
		self.assertIsNotNone(services, "deferred-expense habit not found")
		self.assertEqual(services["consequent_value"], "deferred")
		self.assertIn("expense", services["pattern_statement"])
		# Alpha's revenue side is all-immediate -> variance-suppressed, silent
		self.assertIsNone(_find(alpha.candidates, antecedent_value="Electronics :: revenue"))
		gamma = self._run("acct-deferred-usage", factory.GAMMA)
		revenue = _find(gamma.candidates, antecedent_value="Electronics :: revenue")
		self.assertIsNotNone(revenue, "deferred-revenue habit not found")
		self.assertIn("revenue", revenue["pattern_statement"])
		# trap: Furniture books immediately -> silent
		self.assertIsNone(_find(gamma.candidates, antecedent_value="Furniture :: revenue"))

	# --- manufacturing: realized BOM vs flagged default ---------------------------
	def test_default_bom_usage_divergence_and_default_match_trap(self):
		res = self._run("mfg-default-bom-usage", factory.ALPHA)
		self.assertIsNone(res.skipped_reason)
		widget = _find(res.candidates, antecedent_value="_JPL-ItemWidget")
		self.assertIsNotNone(widget, "realized-vs-default BOM divergence not found")
		self.assertEqual(widget["consequent_value"], factory.EXPECT["widget_realized_bom"])
		self.assertEqual(widget["evidence"]["default_bom"], factory.EXPECT["widget_default_bom"])
		self.assertIn(factory.EXPECT["widget_default_bom"], widget["pattern_statement"])
		# trap: Elec WOs use the flagged default -> config working, suppressed
		self.assertIsNone(_find(res.candidates, antecedent_value="_JPL-ItemElec"))

	# --- timesheet rate defaults ---------------------------------------------------
	def test_timesheet_rate_defaults_and_master_match_trap(self):
		res = self._run("proj-timesheet-rate-defaults", factory.ALPHA)
		self.assertIsNone(res.skipped_reason)
		consulting = _find(res.candidates, antecedent_value="_JPL-Consulting")
		self.assertIsNotNone(consulting, "billing-rate habit not found")
		self.assertEqual(float(consulting["consequent_value"]), factory.EXPECT["consulting_rate"])
		self.assertIn("no default billing rate", consulting["pattern_statement"])
		# trap: Support's Activity Type master already carries 800 -> suppressed
		self.assertIsNone(_find(res.candidates, antecedent_value="_JPL-Support"))


# ---------------------------------------------------------------------------
# Tier-2 org-wide detectors: deterministic fake-facade tests (same idiom as
# the Tier-1 config detectors above)
# ---------------------------------------------------------------------------
def _unit_rows(antecedent, consequent, n, start=0, extra=None):
	rows = []
	for i in range(n):
		day = frappe.utils.add_days("2025-09-01", start + i)
		row = {
			"unit_id": f"{antecedent}-{consequent}-{start + i}",
			"antecedent": antecedent,
			"consequent": consequent,
			"day": day,
			"created": f"{day} 10:00:00",
		}
		row.update(extra or {})
		rows.append(row)
	return rows


class TestBatchSerialUsageFake(FrappeTestCase):
	def test_planted_batch_group_with_fill_rates_and_untracked_trap(self):
		from jarvis.learning import compat

		spec = registry.get_detector("stock-batch-serial-usage")
		items = _unit_rows("Chemicals", "batch-tracked", 35)
		items += _unit_rows("Widgets", "__untracked__", 30, start=40)
		db = _FakePatternDB(
			[
				(
					"tabStock Ledger Entry",
					[
						{"item_group": "Chemicals", "batch_filled": 120, "serial_filled": 0, "total": 130},
					],
				),
				("tabItem", items),
			]
		)
		res = run_detector(spec, None, db)
		self.assertIsNone(res.skipped_reason)
		cand = _find(res.candidates, antecedent_value="Chemicals")
		self.assertIsNotNone(cand, "batch-tracked group not found")
		self.assertEqual(cand["consequent_value"], "batch-tracked")
		self.assertEqual(cand["support_n"], 35)
		self.assertIn("batch tracked", cand["pattern_statement"])
		if compat.has_field("Stock Ledger Entry", "batch_no"):
			self.assertAlmostEqual(cand["evidence"]["batch_fill_rate"], 0.9231, places=3)
		# trap: untracked groups are not a proposal (target_consequents)
		self.assertIsNone(_find(res.candidates, antecedent_value="Widgets"))


class TestCustomFieldAlwaysFilledFake(FrappeTestCase):
	"""The discovered fieldname is regex + meta validated BEFORE interpolation:
	an injection-shaped fieldname and a ghost field (not in meta) must both be
	refused, while a real column with ~1 fill rate proposes."""

	def test_planted_fill_pattern_and_identifier_traps(self):
		spec = registry.get_detector("cfg-custom-field-always-filled")
		fill_rows = _usage_rows("__filled__", 70) + _usage_rows("__empty__", 1, start=70)
		db = _FakePatternDB(
			[
				(
					"tabCustom Field",
					[
						# po_no exists on Sales Invoice meta -> validation passes
						{
							"dt": "Sales Invoice",
							"fieldname": "po_no",
							"fieldtype": "Data",
							"label": "Customer PO",
						},
						# injection-shaped fieldname -> regex refuses before interpolation
						{
							"dt": "Sales Invoice",
							"fieldname": "po_no`; DROP TABLE x",
							"fieldtype": "Data",
							"label": "Evil",
						},
						# snake_case but not a real column -> meta probe refuses
						{
							"dt": "Sales Invoice",
							"fieldname": "ghost_field_xyz",
							"fieldtype": "Data",
							"label": "Ghost",
						},
						# child/non-submittable doctype -> out of scope
						{
							"dt": "Customer",
							"fieldname": "territory",
							"fieldtype": "Link",
							"label": "Territory",
						},
					],
				),
				("tabSales Invoice", fill_rows),
			]
		)
		res = run_detector(spec, None, db)
		self.assertIsNone(res.skipped_reason)
		self.assertEqual(len(res.candidates), 1, "only the validated field may propose")
		cand = res.candidates[0]
		self.assertEqual(cand["antecedent_value"], "Sales Invoice.po_no")
		self.assertEqual(cand["support_n"], 71)
		self.assertEqual(cand["exception_n"], 1)
		self.assertAlmostEqual(cand["evidence"]["fill_rate"], 70 / 71, places=3)
		self.assertIn("po_no", cand["pattern_statement"])

	def test_below_gate_fill_rate_is_silent(self):
		spec = registry.get_detector("cfg-custom-field-always-filled")
		# 90% fill rate is a habit, not "mandatory in practice" (0.98 gate)
		fill_rows = _usage_rows("__filled__", 63) + _usage_rows("__empty__", 7, start=70)
		db = _FakePatternDB(
			[
				(
					"tabCustom Field",
					[
						{
							"dt": "Sales Invoice",
							"fieldname": "po_no",
							"fieldtype": "Data",
							"label": "Customer PO",
						},
					],
				),
				("tabSales Invoice", fill_rows),
			]
		)
		res = run_detector(spec, None, db)
		self.assertEqual(res.candidates, [])

	def test_configured_default_is_refused(self):
		spec = registry.get_detector("cfg-custom-field-always-filled")
		# a field the framework auto-fills via its configured default would show
		# a ~100% fill rate that is configuration, not a user habit -> refused
		fill_rows = _usage_rows("__filled__", 70)
		db = _FakePatternDB(
			[
				(
					"tabCustom Field",
					[
						{
							"dt": "Sales Invoice",
							"fieldname": "po_no",
							"fieldtype": "Data",
							"label": "Customer PO",
							"field_default": "AUTO-PO",
						},
					],
				),
				("tabSales Invoice", fill_rows),
			]
		)
		res = run_detector(spec, None, db)
		self.assertEqual(res.candidates, [], "a defaulted field must never propose")
		# the SQL itself also excludes defaulted rows at source
		self.assertIn(
			"cf.`default` IS NULL OR cf.`default` = ''",
			config_detectors.CUSTOM_FIELD_CANDIDATES_SQL,
		)


class TestRoleDoctypeRoutingFake(FrappeTestCase):
	"""Fractional multi-role weighting over owner JOIN Has Role; org/role level
	output only - user names must never surface."""

	def _db(self):
		si_rows = [dict(r, owner="alice@x.com") for r in _unit_rows("d", "x", 33)]
		si_rows += [dict(r, owner="carol@x.com") for r in _unit_rows("d", "y", 4, start=40)]
		pe_rows = [dict(r, owner="bob@x.com") for r in _unit_rows("d", "z", 32)]
		return _FakePatternDB(
			[
				(
					"tabHas Role",
					[
						{"user": "alice@x.com", "role": "Sales User"},
						{"user": "carol@x.com", "role": "Sales User"},
						{"user": "carol@x.com", "role": "Purchase User"},
						{"user": "bob@x.com", "role": "Accounts User"},
						{"user": "bob@x.com", "role": "System User"},  # generic -> ignored
					],
				),
				("tabSales Invoice", si_rows),
				("tabPayment Entry", pe_rows),
			]
		)

	def test_fractional_weights_and_no_user_output(self):
		import json

		spec = registry.get_detector("role-doctype-routing")
		res = run_detector(spec, None, self._db())
		self.assertIsNone(res.skipped_reason)
		si = _find(res.candidates, antecedent_value="Sales Invoice")
		self.assertIsNotNone(si, "Sales Invoice routing not found")
		self.assertEqual(si["consequent_value"], "Sales User")
		# 33 whole units from the single-role owner + 4 x 0.5 from the
		# two-role owner -> k=35 of n=37 (fractional weights, then rounded)
		self.assertEqual(si["evidence"]["k"], 35)
		self.assertEqual(si["support_n"], 37)
		self.assertEqual(si["evidence"]["n_documents"], 37)
		self.assertIn("fractional", si["evidence"]["weighting"])
		pe = _find(res.candidates, antecedent_value="Payment Entry")
		self.assertIsNotNone(pe)
		self.assertEqual(pe["consequent_value"], "Accounts User")
		# org/role level ONLY: no user identifier anywhere in any candidate
		blob = json.dumps(res.candidates, default=str)
		for user in ("alice@x.com", "carol@x.com", "bob@x.com"):
			self.assertNotIn(user, blob)

	def test_single_qualifying_doctype_is_suppressed(self):
		# With one qualifying doctype the leave-segment-out base rate has no
		# "rest" and the gap gate would be skipped, so a role that dominates
		# the org's only active doctype (e.g. System Manager on a small site)
		# would propose as misleading routing -> the postprocess requires >= 2.
		spec = registry.get_detector("role-doctype-routing")
		si_rows = [dict(r, owner="admin.like@x.com") for r in _unit_rows("d", "x", 40)]
		db = _FakePatternDB(
			[
				("tabHas Role", [{"user": "admin.like@x.com", "role": "System Manager"}]),
				("tabSales Invoice", si_rows),
			]
		)
		res = run_detector(spec, None, db)
		self.assertEqual(res.candidates, [])


class TestPartyTaxTemplateSupplierFake(FrappeTestCase):
	"""The purchase-side pass of acct-party-tax-template, plus the
	no-confound path: without state data the band is NOT demoted but the
	compiled text still carries the geography warning."""

	def test_supplier_segment_without_confound_keeps_band(self):
		from jarvis.learning.detectors import accounts as accounts_detectors

		spec = registry.get_detector("acct-party-tax-template")
		rows = []
		for j in range(32):
			day = frappe.utils.add_days("2025-09-01", j)
			rows.append(
				{
					"unit_id": f"pi-imp-{j}",
					"antecedent": "Supplier.supplier_group=Importers",
					"consequent": "Import Duty 10%",
					"party": f"Sup{j % 8}",
					"day": day,
					"created": f"{day} 10:00:00",
				}
			)
		for j in range(32):
			day = frappe.utils.add_days("2025-10-15", j)
			rows.append(
				{
					"unit_id": f"pi-dom-{j}",
					"antecedent": "Supplier.supplier_group=Domestic",
					"consequent": "Alpha GST 18%",
					"party": f"Dom{j % 8}",
					"day": day,
					"created": f"{day} 10:00:00",
				}
			)
		db = _FakePatternDB(
			[
				(
					"tabCustom Field",
					[
						{
							"dt": "Supplier",
							"fieldname": "supplier_group",
							"fieldtype": "Link",
							"label": "Supplier Group",
						},
					],
				),
				("tabPurchase Invoice", rows),
				("tabTax Rule", []),
				("tabAddress", []),
			]
		)
		raws = accounts_detectors.postprocess_party_tax_template(
			None, spec, "SomeCo", db, {"window_start": "2025-01-01", "company": "SomeCo"}
		)
		imp = _find(raws, antecedent_value="Supplier.supplier_group=Importers")
		self.assertIsNotNone(imp, "supplier-segment tax pattern not found")
		self.assertEqual(imp["consequent_value"], "Import Duty 10%")
		# no state data -> no confound -> band NOT demoted (32/32 is Medium)
		self.assertEqual(imp["band"], "Medium")
		self.assertNotIn("geography_confound", imp["evidence"])
		self.assertIn("geography_caveat", imp["evidence"])
		# the compiled wording still carries the warning (template-borne)
		cand = _finalize(spec, "SomeCo", imp)
		self.assertIn("geography", cand["pattern_statement"].lower())
		self.assertEqual(cand["effective_sensitivity"], "B")


# ---------------------------------------------------------------------------
# acct-party-tax-template guards: discovery cardinality/ranking, the
# multi-party segment floor, and the geography-confound sample floors
# ---------------------------------------------------------------------------
class TestSegmentFieldDiscovery(FrappeTestCase):
	"""Custom-field pre-pass: identifier-shaped Data/Link columns are refused
	by the cardinality/selectivity gate, Select fields rank first, and fill
	rate (not alphabetical order) breaks ties within the Link/Data tier."""

	def _discover(self, mapping):
		from jarvis.learning.detectors.accounts import _discover_segment_fields

		return _discover_segment_fields(_FakePatternDB(mapping))

	def test_identifier_field_refused_and_ranking_by_usefulness(self):
		fields = self._discover(
			[
				(
					"tabCustom Field",
					[
						# alphabetical order would pick industry first; usefulness must not
						{"dt": "Customer", "fieldname": "industry", "fieldtype": "Link", "label": "Industry"},
						{
							"dt": "Customer",
							"fieldname": "market_segment",
							"fieldtype": "Link",
							"label": "Market Segment",
						},
						{
							"dt": "Customer",
							"fieldname": "customer_type",
							"fieldtype": "Select",
							"label": "Type",
						},
						# gstin/pan-shaped: effectively one distinct value per party
						{"dt": "Customer", "fieldname": "tax_id", "fieldtype": "Data", "label": "Tax ID"},
					],
				),
				("NULLIF(`industry`", [{"total": 100, "filled": 20, "distinct_values": 5}]),
				("NULLIF(`market_segment`", [{"total": 100, "filled": 90, "distinct_values": 4}]),
				("NULLIF(`customer_type`", [{"total": 100, "filled": 100, "distinct_values": 2}]),
				("NULLIF(`tax_id`", [{"total": 100, "filled": 90, "distinct_values": 85}]),
			]
		)
		names = [f["fieldname"] for f in fields]
		self.assertNotIn("tax_id", names)  # 85 distinct over 90 filled: identity
		# Select first, then Link tier by fill rate (market_segment > industry)
		self.assertEqual(names, ["customer_type", "market_segment", "industry"])

	def test_cardinality_and_selectivity_gates(self):
		from jarvis.learning.detectors.accounts import _segment_field_score

		# constant column partitions nothing
		self.assertIsNone(_segment_field_score("Data", {"total": 100, "filled": 100, "distinct": 1}))
		# unbounded cardinality is never a segment (any fieldtype)
		self.assertIsNone(_segment_field_score("Select", {"total": 100, "filled": 100, "distinct": 60}))
		# Link/Data: distinct must stay small relative to the filled party count
		self.assertIsNone(_segment_field_score("Link", {"total": 100, "filled": 40, "distinct": 30}))
		# bounded categorical fields score positive...
		self.assertGreater(_segment_field_score("Select", {"total": 100, "filled": 100, "distinct": 4}), 0.0)
		# ...and Select is exempt from the Link/Data selectivity ratio
		self.assertIsNotNone(_segment_field_score("Select", {"total": 100, "filled": 10, "distinct": 8}))
		# higher fill and lower cardinality rank higher
		low = _segment_field_score("Link", {"total": 100, "filled": 20, "distinct": 5})
		high = _segment_field_score("Link", {"total": 100, "filled": 90, "distinct": 4})
		self.assertGreater(high, low)

	def test_unavailable_profile_fails_open_with_zero_score(self):
		# no probe mapping -> profile None -> the field competes at score 0
		# (the multi-party guard downstream still blocks identity output)
		fields = self._discover(
			[
				(
					"tabCustom Field",
					[
						{
							"dt": "Supplier",
							"fieldname": "supplier_group",
							"fieldtype": "Link",
							"label": "Supplier Group",
						},
					],
				),
			]
		)
		self.assertEqual([f["fieldname"] for f in fields], ["supplier_group"])


class TestPartyTaxTemplateSegmentGuards(FrappeTestCase):
	"""A segment value covering a single party is party identity, not a
	segment habit: it must be dropped even when its unit count clears every
	statistical gate, while a genuinely shared value still proposes."""

	def test_single_party_segment_value_is_dropped(self):
		from jarvis.learning.detectors import accounts as accounts_detectors

		spec = registry.get_detector("acct-party-tax-template")
		rows = []
		# one high-volume customer: 32 invoices behind ONE identifier value
		for j in range(32):
			day = frappe.utils.add_days("2025-09-01", j)
			rows.append(
				{
					"unit_id": f"si-one-{j}",
					"antecedent": "Customer.tax_id=27AAAC0001X",
					"consequent": "GST18",
					"party": "CustBig",
					"day": day,
					"created": f"{day} 10:00:00",
				}
			)
		# a genuinely shared value: 32 invoices across 8 customers
		for j in range(32):
			day = frappe.utils.add_days("2025-10-15", j)
			rows.append(
				{
					"unit_id": f"si-shared-{j}",
					"antecedent": "Customer.tax_id=SHARED",
					"consequent": "GST12",
					"party": f"Cust{j % 8}",
					"day": day,
					"created": f"{day} 10:00:00",
				}
			)
		db = _FakePatternDB(
			[
				(
					"tabCustom Field",
					[
						{"dt": "Customer", "fieldname": "tax_id", "fieldtype": "Data", "label": "Tax ID"},
					],
				),
				# profile passes discovery so the postprocess-level guard is exercised
				("NULLIF(`tax_id`", [{"total": 40, "filled": 40, "distinct_values": 5}]),
				("tabSales Invoice", rows),
				("tabTax Rule", []),
				("tabAddress", []),
			]
		)
		raws = accounts_detectors.postprocess_party_tax_template(
			None, spec, "SomeCo", db, {"window_start": "2025-01-01", "company": "SomeCo"}
		)
		self.assertIsNone(
			_find(raws, antecedent_value="Customer.tax_id=27AAAC0001X"),
			"a one-party 'segment' must never surface (party identity leak)",
		)
		shared = _find(raws, antecedent_value="Customer.tax_id=SHARED")
		self.assertIsNotNone(shared, "a genuinely shared segment value still proposes")


class TestGeographyConfoundFloors(FrappeTestCase):
	"""Per-state sample floors + free-text state normalization: a one-party
	state bucket is 100% pure by construction and must never drive the
	confound verdict."""

	def test_single_party_per_state_is_not_a_confound(self):
		from jarvis.learning.detectors.accounts import _filter_confound_states

		# the degenerate case: two customers, one per state, both high-volume
		states = {"p1": "maharashtra", "p2": "karnataka"}
		templates = {"p1": "GST18-Composition", "p2": "GST12-Composition"}
		units = {"p1": 30, "p2": 30}
		filtered = _filter_confound_states(states, templates, units)
		self.assertEqual(filtered, {})
		self.assertFalse(_state_predicts_template(filtered, templates))

	def test_units_floor_drops_thin_buckets(self):
		from jarvis.learning.detectors.accounts import _filter_confound_states

		# 2 parties per state but only 2 units per state -> below the floor
		states = {f"p{i}": ("mh" if i < 2 else "ka") for i in range(4)}
		templates = {f"p{i}": ("A" if i < 2 else "B") for i in range(4)}
		units = {f"p{i}": 1 for i in range(4)}
		self.assertEqual(_filter_confound_states(states, templates, units), {})

	def test_qualifying_buckets_still_confound(self):
		from jarvis.learning.detectors.accounts import _filter_confound_states

		states = {f"m{i}": "mh" for i in range(3)}
		states.update({f"k{i}": "ka" for i in range(3)})
		templates = {f"m{i}": "GST18" for i in range(3)}
		templates.update({f"k{i}": "GST12" for i in range(3)})
		units = dict.fromkeys(states, 4)  # 12 units per state bucket
		filtered = _filter_confound_states(states, templates, units)
		self.assertEqual(len(filtered), 6)
		self.assertTrue(_state_predicts_template(filtered, templates))

	def test_state_normalization_merges_spelling_variants(self):
		from jarvis.learning.detectors.accounts import _normalize_state_map

		norm = _normalize_state_map(
			{
				"p1": "Karnataka",
				"p2": " karnataka ",
				"p3": "KARNATAKA",
				"p4": "Maharashtra",
				"p5": "",
				"p6": None,
			}
		)
		self.assertEqual(
			norm,
			{
				"p1": "karnataka",
				"p2": "karnataka",
				"p3": "karnataka",
				"p4": "maharashtra",
			},
		)


# ---------------------------------------------------------------------------
# acct-je-usage: org-level voucher-type tautology guard
# ---------------------------------------------------------------------------
class TestJeUsageTautologyGuard(FrappeTestCase):
	"""The org-level voucher-type card must not fire when the modal type is
	just the framework default every manual JE starts with."""

	def _post(self, rows):
		from jarvis.learning.detectors import accounts as accounts_detectors

		spec = registry.get_detector("acct-je-usage")
		return accounts_detectors.postprocess_je_usage(rows, spec, factory.ALPHA, PatternDB(), {})

	def test_default_mode_suppresses_org_card(self):
		rows = _unit_rows("Journal Entry", "__manual__", 35)
		rows += _unit_rows("Bank Entry", "__manual__", 5, start=40)
		out = self._post(rows)
		self.assertIsNone(
			_find(out, antecedent_value="org:voucher_type"),
			"'JEs are usually Journal Entry entries' is a tautology",
		)

	def test_divergent_mode_still_proposes_org_card(self):
		rows = _unit_rows("Bank Entry", "__manual__", 35)
		rows += _unit_rows("Journal Entry", "__manual__", 5, start=40)
		out = self._post(rows)
		org = _find(out, antecedent_value="org:voucher_type")
		self.assertIsNotNone(org, "a divergent modal voucher type must propose")
		self.assertEqual(org["consequent_value"], "Bank Entry")

	def test_default_reads_je_meta(self):
		from jarvis.learning.detectors.accounts import _je_voucher_type_default

		self.assertEqual(_je_voucher_type_default(), "Journal Entry")


# ---------------------------------------------------------------------------
# acct-party-tax-template: Tax Rule cross-ref honors BOTH date bounds
# ---------------------------------------------------------------------------
class TestTaxRuleCrossRefDates(FrappeTestCase):
	"""A future-dated Tax Rule is not yet enforcing anything, so it must not
	suppress a realized habit; an undated rule stays suppressing."""

	ACTIVE = "_JPL-TR-active"
	FUTURE = "_JPL-TR-future"

	def setUp(self):
		self._cleanup()
		today = frappe.utils.today()
		rows = (
			(self.ACTIVE, None),
			(self.FUTURE, frappe.utils.add_days(today, 30)),
		)
		try:
			for name, from_date in rows:
				doc = frappe.new_doc("Tax Rule")
				doc.update(
					{
						"tax_type": "Sales",
						"sales_tax_template": f"{name}-TMPL",
						"from_date": from_date,
					}
				)
				doc.name = name
				doc.flags.name_set = True
				doc.db_insert()
		except Exception as exc:  # environment without Tax Rule table shape
			self._cleanup()
			self.skipTest(f"could not seed Tax Rule: {exc}")

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		for name in (self.ACTIVE, self.FUTURE):
			frappe.db.delete("Tax Rule", {"name": name})

	def test_future_dated_rule_does_not_suppress(self):
		from jarvis.learning.detectors.accounts import _encoded_templates

		encoded = _encoded_templates(PatternDB(), None, "sales_tax_template")
		self.assertIn(f"{self.ACTIVE}-TMPL", encoded)
		self.assertNotIn(f"{self.FUTURE}-TMPL", encoded)


# ---------------------------------------------------------------------------
# executor geography guard (Tier-1 buy-supplier-tax-template path): per-state
# sample floors + free-text state normalization, mirroring the Tier-2
# acct-party-tax-template guard in detectors/accounts.py
# ---------------------------------------------------------------------------
class TestExecutorGeographyGuardFloors(FrappeTestCase):
	"""A one-party state bucket is 100% pure by construction: a single
	supplier per state must never declare a geography confound on the Tier-1
	supplier-tax-template path, while qualifying multi-party buckets still
	do - including when free-text case variants spell one state two ways."""

	def _run(self, po_rows, address_rows):
		spec = registry.get_detector("buy-supplier-tax-template")
		db = _FakePatternDB(
			[
				("tabPurchase Order", po_rows),
				("tabAddress", address_rows),
			]
		)
		return run_detector(spec, "SomeCo", db)

	def test_single_supplier_per_state_is_not_a_confound(self):
		# two suppliers, one per state, both high-volume (30/30 units): pre-floor
		# logic called this a confound and demoted; the bucket floors refuse it.
		rows = _po_rows("SupA", "GST18", "2025-09-01", 30)
		rows += _po_rows("SupB", "GST12", "2025-09-02", 30)
		res = self._run(
			rows,
			[
				{"party": "SupA", "state": "Maharashtra"},
				{"party": "SupB", "state": "Karnataka"},
			],
		)
		self.assertIsNone(res.skipped_reason)
		for supplier in ("SupA", "SupB"):
			cand = _find(res.candidates, antecedent_value=supplier)
			self.assertIsNotNone(cand)
			self.assertIn("geography_caveat", cand["evidence"])
			self.assertNotIn("geography_confound", cand["evidence"])
			# 30/30 is Medium on Wilson; NOT demoted to Low
			self.assertEqual(cand["strength_band"], "Medium")

	def test_qualifying_buckets_still_confound_despite_case_variants(self):
		# 2 suppliers per normalized state (>= _MIN_STATE_PARTIES, >= 5 units):
		# the confound fires and demotes even though free-text case/whitespace
		# variants spell each state two ways (normalization merges the buckets;
		# unnormalized they would be four one-party buckets and never confound).
		rows = _po_rows("SupA", "GST18", "2025-09-01", 30)
		rows += _po_rows("SupB", "GST18", "2025-09-02", 30)
		rows += _po_rows("SupC", "GST12", "2025-09-03", 30)
		rows += _po_rows("SupD", "GST12", "2025-09-04", 30)
		res = self._run(
			rows,
			[
				{"party": "SupA", "state": "Karnataka"},
				{"party": "SupB", "state": " karnataka "},
				{"party": "SupC", "state": "Maharashtra"},
				{"party": "SupD", "state": "MAHARASHTRA"},
			],
		)
		self.assertIsNone(res.skipped_reason)
		for supplier in ("SupA", "SupB", "SupC", "SupD"):
			cand = _find(res.candidates, antecedent_value=supplier)
			self.assertIsNotNone(cand)
			self.assertEqual(cand["evidence"]["geography_confound"], "state predicts template")
			self.assertEqual(cand["strength_band"], "Low")  # Medium demoted

	def test_party_state_map_dedupes_case_variants(self):
		from jarvis.learning.executor import _party_state_map

		db = _FakePatternDB(
			[
				(
					"tabAddress",
					[
						{"party": "SupA", "state": "Karnataka"},
						{"party": "SupA", "state": " karnataka "},
						{"party": "SupA", "state": "Karnataka"},
						{"party": "SupB", "state": "Tamil Nadu"},
						{"party": "SupB", "state": "Kerala"},  # genuinely multi-state
					],
				),
			]
		)
		states = _party_state_map(db, "Supplier", ["SupA", "SupB"])
		# SupA's variants are ONE state (display = most frequent original
		# casing); SupB really spans two states and stays dropped.
		self.assertEqual(states, {"SupA": "Karnataka"})


# ---------------------------------------------------------------------------
# recency guard: secondary grandfathered-transition condition (stats + the
# executor plumb-through of the detector's own c_min)
# ---------------------------------------------------------------------------
def _dated_rows(supplier, consequent, start_date, count, tag):
	"""Unit rows with collision-free ids (two batches of the same
	supplier+consequent need distinct unit_ids, which _po_rows cannot give)."""
	rows = []
	for i in range(count):
		day = frappe.utils.add_days(start_date, i)
		rows.append(
			{
				"unit_id": f"{supplier}-{tag}-{i}",
				"antecedent": supplier,
				"consequent": consequent,
				"company": factory.ALPHA,
				"day": day,
				"created": f"{day} 10:00:00",
			}
		)
	return rows


class TestRecencyGrandfatheredGuard(FrappeTestCase):
	"""'New terms for NEW deals only, legacy accounts grandfathered': legacy
	volume keeps the recent plurality on the old value and the share shift
	under the 0.2 threshold, but the recent window no longer clears the
	detector's own c_min - the candidate must carry the recency divergence
	(note + machine-readable changed-around date) so drift re-validation can
	stale the approved row."""

	def _reduce(self, rows):
		spec = registry.get_detector("buy-supplier-tax-template")  # c_min 0.95
		return reduce_units(rows, spec, PatternDB())

	def test_grandfathered_transition_stamps_recency_divergence(self):
		rows = []
		# SupGrand: 150 legacy OldGST POs, then a recent window of 27 OldGST
		# (legacy accounts still buying) + 3 NewGST (new deals): recent
		# confidence for OldGST is 27/30 = 0.90 < c_min 0.95, no plurality
		# flip, share shift 0.983 -> 0.90 stays under the 0.2 threshold, and
		# the full-window aggregate (177/180 = 98.3%) still clears every gate.
		rows += _dated_rows("SupGrand", "OldGST", "2025-01-01", 150, "old")
		rows += _dated_rows("SupGrand", "OldGST", "2025-11-01", 27, "legacy")
		rows += _dated_rows("SupGrand", "NewGST", "2025-11-28", 3, "new")
		# SupSteady: same volume shape, recent window still clears c_min.
		rows += _dated_rows("SupSteady", "SteadyGST", "2025-01-02", 150, "old")
		rows += _dated_rows("SupSteady", "SteadyGST", "2025-11-01", 30, "recent")
		cands = self._reduce(rows)

		grand = _find(cands, antecedent_value="SupGrand")
		self.assertIsNotNone(grand, "the full-window aggregate still proposes")
		self.assertEqual(grand["consequent_value"], "OldGST")  # no plurality flip
		self.assertEqual(grand["n_units"], 180)  # full window kept, not retargeted
		self.assertIn("recency", grand["evidence"])
		self.assertIn("behavior changed around", grand["evidence"]["recency"])
		# machine-readable onset for drift staling (lifecycle._recency_drift)
		self.assertIn("recency_changed_around", grand["evidence"])

		steady = _find(cands, antecedent_value="SupSteady")
		self.assertIsNotNone(steady)
		self.assertNotIn("recency", steady["evidence"])
