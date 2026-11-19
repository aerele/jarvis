"""Snapshot infra + print-log ingest tests (plan sections 4.4, 5.3, 6.1, Phase 2).

Covers the ``Jarvis Pattern Snapshot`` upsert/merge/uniqueness contract, the
engine-side ``ingest_print_log`` stream (party resolution, monthly grouping,
watermark advance, idempotency), the ingest budget gates (paused run skips,
closed scheduled window halts between chunks, manual bypasses) plus the
per-chunk transaction shape, and the preflight print-signal wiring that
marks ``sell-customer-print-format`` not_applicable when printing bypasses
Frappe's print system.

Run on the test site only:
    bench --site patterntest.localhost run-tests --module jarvis.tests.learning.test_snapshots
"""

from __future__ import annotations

import datetime
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.learning import bootstrap, snapshots
from jarvis.tests.learning import factory

DETECTOR_ID = snapshots.PRINT_FORMAT_DETECTOR_ID
SNAPSHOT = snapshots.SNAPSHOT
STATE = snapshots.DETECTOR_STATE

_TEST_DETECTOR = "_jpl-test-snapshots"


def seed_print_log(ref_doctype: str, ref_name: str, print_format: str, created: str) -> str:
	"""One Access Log row exactly as frappe printview writes it (method='Print',
	page='Print Format: <name>'), with a controlled creation timestamp."""
	doc = frappe.get_doc(
		{
			"doctype": "Access Log",
			"user": "Administrator",
			"export_from": ref_doctype,
			"reference_document": ref_name,
			"file_type": "PDF",
			"method": "Print",
			"page": f"Print Format: {print_format}",
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(
		"Access Log", doc.name, {"creation": created, "modified": created}, update_modified=False
	)
	return doc.name


def wipe_print_state(detector_ids=(DETECTOR_ID, _TEST_DETECTOR)) -> None:
	"""Deterministic slate for print-detector tests on the test site: no Print
	log rows, no snapshots, no detector-state stream position."""
	frappe.db.delete("Access Log", {"method": "Print"})
	for detector_id in detector_ids:
		frappe.db.delete(SNAPSHOT, {"detector_id": detector_id})
		frappe.db.delete(STATE, {"name": detector_id})


def _payload(party: str, fmt: str, day_counts: dict, first_day: str, last_day: str) -> dict:
	n = sum(day_counts.values())
	return {
		"party_type": "Customer",
		"counts": {party: {fmt: {"n": n, "eff": n, "days": dict(day_counts)}}},
		"first_day": first_day,
		"last_day": last_day,
		"rows_ingested": n,
	}


class TestSnapshotUpsert(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.delete(SNAPSHOT, {"detector_id": _TEST_DETECTOR})

	def tearDown(self):
		frappe.db.delete(SNAPSHOT, {"detector_id": _TEST_DETECTOR})
		super().tearDown()

	def test_upsert_creates_then_merges_additively(self):
		name1 = snapshots.upsert_monthly(
			_TEST_DETECTOR,
			"2026-05",
			None,
			_payload("CustA", "FmtX", {"2026-05-01": 3, "2026-05-02": 2}, "2026-05-01", "2026-05-02"),
		)
		name2 = snapshots.upsert_monthly(
			_TEST_DETECTOR,
			"2026-05",
			None,
			_payload("CustA", "FmtX", {"2026-05-02": 1, "2026-05-09": 1}, "2026-05-02", "2026-05-09"),
		)
		self.assertEqual(name1, name2, "same (detector, period, company) must merge, not duplicate")
		self.assertEqual(frappe.db.count(SNAPSHOT, {"detector_id": _TEST_DETECTOR}), 1)
		merged = snapshots.read_all(_TEST_DETECTOR)
		agg = merged["counts"]["CustA"]["FmtX"]
		self.assertEqual(agg["n"], 7)
		self.assertEqual(agg["eff"], 7)
		self.assertEqual(agg["days"], {"2026-05-01": 3, "2026-05-02": 3, "2026-05-09": 1})
		self.assertEqual(merged["rows_ingested"], 7)
		self.assertEqual(merged["first_day"], "2026-05-01")
		self.assertEqual(merged["last_day"], "2026-05-09")

	def test_read_all_merges_periods_and_filters_company(self):
		snapshots.upsert_monthly(
			_TEST_DETECTOR,
			"2026-04",
			None,
			_payload("CustA", "FmtX", {"2026-04-10": 4}, "2026-04-10", "2026-04-10"),
		)
		snapshots.upsert_monthly(
			_TEST_DETECTOR,
			"2026-05",
			None,
			_payload("CustA", "FmtX", {"2026-05-11": 6}, "2026-05-11", "2026-05-11"),
		)
		merged = snapshots.read_all(_TEST_DETECTOR)
		self.assertEqual(merged["counts"]["CustA"]["FmtX"]["n"], 10)
		self.assertEqual(merged["periods"], ["2026-04", "2026-05"])
		# window filter drops months before the window start
		windowed = snapshots.read_all(_TEST_DETECTOR, window_start="2026-05-01")
		self.assertEqual(windowed["counts"]["CustA"]["FmtX"]["n"], 6)
		self.assertEqual(windowed["periods"], ["2026-05"])
		# company filter: these rows are org-wide (company None), so a company
		# selection reads none of them
		scoped = snapshots.read_all(_TEST_DETECTOR, company="_JPL-Nowhere Ltd")
		self.assertEqual(scoped.get("counts") or {}, {})

	def test_snapshot_key_unique_constraint(self):
		snapshots.upsert_monthly(
			_TEST_DETECTOR,
			"2026-06",
			None,
			_payload("CustA", "FmtX", {"2026-06-01": 1}, "2026-06-01", "2026-06-01"),
		)
		duplicate = frappe.get_doc(
			{
				"doctype": SNAPSHOT,
				"detector_id": _TEST_DETECTOR,
				"period": "2026-06",
				"company": None,
				"payload": "{}",
			}
		)
		with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
			duplicate.insert(ignore_permissions=True)

	def test_period_must_be_year_month(self):
		with self.assertRaises(frappe.ValidationError):
			snapshots.upsert_monthly(_TEST_DETECTOR, "2026-13", None, {"counts": {}})

	def test_engine_fence_covers_snapshot_writes(self):
		from jarvis.learning.engine import ALLOWED_WRITE_DOCTYPES

		self.assertIn(SNAPSHOT, ALLOWED_WRITE_DOCTYPES)


class TestPrintLogIngest(FrappeTestCase):
	"""Stream -> resolve -> monthly snapshot -> watermark, engine-side."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		factory.build(commit=True)
		wipe_print_state()
		# DealerD invoice printed 3x "Tax Invoice" on distinct days (>120s apart
		# so nothing burst-collapses), a Beta-company print, an unresolvable ref
		# (counted as a row, never an event) and a same-day reprint burst pair
		# (2 raw rows -> 1 effective event).
		seed_print_log("Sales Invoice", "_JPL-SI-A-0000", "Tax Invoice", "2025-09-02 10:00:00")
		seed_print_log("Sales Invoice", "_JPL-SI-A-0001", "Tax Invoice", "2025-09-03 10:00:00")
		seed_print_log("Sales Invoice", "_JPL-SI-A-0002", "Tax Invoice", "2025-09-04 10:00:00")
		seed_print_log("Sales Invoice", "_JPL-SI-B-0000", "Beta Slip", "2025-09-05 10:00:00")
		seed_print_log("Sales Invoice", "SINV-NOPE", "Ghost Format", "2025-09-06 10:00:00")
		seed_print_log("Sales Invoice", "_JPL-SI-A-0003", "Tax Invoice", "2025-10-01 10:00:00")
		seed_print_log("Sales Invoice", "_JPL-SI-A-0003", "Tax Invoice", "2025-10-01 10:00:30")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		wipe_print_state()
		factory.wipe(commit=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_ingest_then_idempotent_then_incremental(self):
		summary = snapshots.ingest_print_log()
		self.assertEqual(summary.get("rows"), 7)
		self.assertEqual(summary.get("events"), 6, "the unresolvable ref must drop")
		# monthly + company grain: 2025-09 Alpha, 2025-09 Beta, 2025-10 Alpha
		self.assertEqual(summary.get("snapshots"), 3)

		sep_alpha = snapshots.read_all(DETECTOR_ID, company=factory.ALPHA, window_start="2025-09-01")
		dealer = sep_alpha["counts"]["_JPL-DealerD"]["Tax Invoice"]
		self.assertEqual(dealer["n"], 5)
		# the 10:00:00/10:00:30 reprint pair collapses to one effective event
		self.assertEqual(dealer["eff"], 4)
		self.assertEqual(
			dealer["days"],
			{"2025-09-02": 1, "2025-09-03": 1, "2025-09-04": 1, "2025-10-01": 2},
		)
		beta = snapshots.read_all(DETECTOR_ID, company=factory.BETA)
		self.assertEqual(beta["counts"]["_JPL-BetaCust0"]["Beta Slip"]["n"], 1)

		# watermark advanced to the last RAW row read (incl. the unresolvable)
		watermark = frappe.db.get_value(STATE, DETECTOR_ID, "last_watermark")
		self.assertEqual(str(watermark), "2025-10-01 10:00:30")

		# idempotency: nothing beyond the watermark -> no rows, payload unchanged
		again = snapshots.ingest_print_log()
		self.assertEqual(again.get("rows"), 0)
		unchanged = snapshots.read_all(DETECTOR_ID, company=factory.ALPHA)
		self.assertEqual(unchanged["counts"]["_JPL-DealerD"]["Tax Invoice"]["n"], 5)

		# incremental: one new print -> only it is ingested, counts merge
		seed_print_log("Sales Invoice", "_JPL-SI-A-0004", "Tax Invoice", "2025-10-05 10:00:00")
		frappe.db.commit()
		third = snapshots.ingest_print_log()
		self.assertEqual(third.get("rows"), 1)
		merged = snapshots.read_all(DETECTOR_ID, company=factory.ALPHA)
		self.assertEqual(merged["counts"]["_JPL-DealerD"]["Tax Invoice"]["n"], 6)
		self.assertEqual(
			str(frappe.db.get_value(STATE, DETECTOR_ID, "last_watermark")),
			"2025-10-05 10:00:00",
		)


class TestIngestGating(FrappeTestCase):
	"""Budget gates on the engine-side ingest (plan section 5.3): a paused
	mining run skips it entirely, a SCHEDULED run whose analysis window has
	closed halts it, manual runs bypass the window - and every skip/halt is
	lossless because the watermark defers the tail to the next call."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		factory.build(commit=True)

	@classmethod
	def tearDownClass(cls):
		wipe_print_state()
		factory.wipe(commit=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		wipe_print_state()
		seed_print_log("Sales Invoice", "_JPL-SI-A-0000", "Tax Invoice", "2025-09-02 10:00:00")
		frappe.db.commit()

	def _closed_window_run(self, trigger="scheduled"):
		# start == end is a degenerate (empty) window: ``in_window`` is always
		# False, so a scheduled run reads as out-of-window at any wall clock.
		return frappe._dict(trigger=trigger, window_start_used="02:00:00", window_end_used="02:00:00")

	def test_paused_run_skips_ingest_entirely(self):
		summary = snapshots.ingest_print_log(paused=True)
		self.assertEqual(summary.get("skipped"), "mining run paused")
		self.assertEqual(summary.get("rows"), 0)
		self.assertEqual(frappe.db.count(SNAPSHOT, {"detector_id": DETECTOR_ID}), 0)
		self.assertIsNone(frappe.db.get_value(STATE, DETECTOR_ID, "last_watermark"))
		# lossless: the next unpaused call picks up the deferred tail
		resumed = snapshots.ingest_print_log()
		self.assertEqual(resumed.get("rows"), 1)

	def test_scheduled_run_out_of_window_halts_before_any_chunk(self):
		summary = snapshots.ingest_print_log(run=self._closed_window_run())
		self.assertEqual(summary.get("halted"), "window")
		self.assertEqual(summary.get("rows"), 0)
		self.assertEqual(frappe.db.count(SNAPSHOT, {"detector_id": DETECTOR_ID}), 0)
		self.assertIsNone(frappe.db.get_value(STATE, DETECTOR_ID, "last_watermark"))

	def test_manual_run_bypasses_the_window(self):
		summary = snapshots.ingest_print_log(run=self._closed_window_run(trigger="manual"))
		self.assertNotIn("halted", summary)
		self.assertEqual(summary.get("rows"), 1)
		self.assertEqual(
			str(frappe.db.get_value(STATE, DETECTOR_ID, "last_watermark")),
			"2025-09-02 10:00:00",
		)


class TestIngestChunkCommits(FrappeTestCase):
	"""Per-chunk transaction shape (plan section 5.3): each chunk's upserts
	and watermark move commit together in their OWN small transaction - never
	one read-write transaction across the whole stream - and a window close
	between chunks keeps every already-committed chunk. Uses a fake runner
	that serves two Access Log chunks (a full PRINT_LOG_CHUNK, then a short
	tail) so no 5000 real log rows are seeded; snapshot/state writes hit the
	real test-site DB."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		wipe_print_state()
		frappe.db.commit()

	def tearDown(self):
		wipe_print_state()
		frappe.db.commit()
		super().tearDown()

	def _chunks(self):
		from jarvis.learning.detectors.selling import PRINT_LOG_CHUNK

		base = datetime.datetime(2025, 9, 1, 10, 0, 0)

		def row(i):
			created = (base + datetime.timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S")
			return {
				"name": f"AL-FAKE-{i}",
				"created": created,
				"page": "Print Format: Fake Invoice",
				"ref_doctype": "Sales Invoice",
				"ref_name": "SI-FAKE-1",
			}

		chunk1 = [row(i) for i in range(PRINT_LOG_CHUNK)]
		chunk2 = [row(10000 + i) for i in range(3)]
		return chunk1, chunk2

	def _fake_runner(self, chunk1, chunk2):
		last1 = chunk1[-1]["created"]
		last2 = chunk2[-1]["created"]

		def runner(query, params=None):
			if "tabJarvis Pattern Detector State" in query:
				return []  # missing state row -> epoch watermark
			if "tabAccess Log" in query:
				wm = str((params or {}).get("watermark"))
				if wm < last1:
					return list(chunk1)
				if wm < last2:
					return list(chunk2)
				return []
			if "tabSales Invoice" in query:
				return [{"name": "SI-FAKE-1", "party": "_JPL-FakeCust", "company": None}]
			return []

		return runner

	def test_each_chunk_commits_upserts_and_watermark_together(self):
		chunk1, chunk2 = self._chunks()
		committed_watermarks = []
		real_commit = frappe.db.commit

		def counting_commit(*args, **kwargs):
			# the in-transaction watermark at commit time = what this chunk's
			# transaction is about to make durable
			committed_watermarks.append(str(frappe.db.get_value(STATE, _TEST_DETECTOR, "last_watermark")))
			return real_commit(*args, **kwargs)

		with mock.patch.object(snapshots, "_db_runner", self._fake_runner(chunk1, chunk2)):
			with mock.patch.object(frappe.db, "commit", counting_commit):
				summary = snapshots.ingest_print_log(detector_id=_TEST_DETECTOR)

		self.assertEqual(summary.get("rows"), len(chunk1) + 3)
		self.assertEqual(summary.get("events"), len(chunk1) + 3)
		self.assertEqual(summary.get("snapshots"), 1)  # same (period, company)
		# TWO small transactions, one per chunk, each carrying its own
		# watermark move - never a single commit after the whole stream
		self.assertEqual(
			committed_watermarks,
			[chunk1[-1]["created"], chunk2[-1]["created"]],
		)
		self.assertEqual(
			str(frappe.db.get_value(STATE, _TEST_DETECTOR, "last_watermark")),
			chunk2[-1]["created"],
		)
		self.assertEqual(
			int(frappe.db.get_value(STATE, _TEST_DETECTOR, "rows_scanned_total")),
			len(chunk1) + 3,
		)
		merged = snapshots.read_all(_TEST_DETECTOR)
		self.assertEqual(merged["counts"]["_JPL-FakeCust"]["Fake Invoice"]["n"], len(chunk1) + 3)

	def test_window_close_between_chunks_keeps_committed_chunk(self):
		chunk1, chunk2 = self._chunks()
		closes = iter([False, True])

		with mock.patch.object(snapshots, "_db_runner", self._fake_runner(chunk1, chunk2)):
			with mock.patch.object(snapshots, "_window_closed", lambda run: next(closes)):
				summary = snapshots.ingest_print_log(detector_id=_TEST_DETECTOR)

		self.assertEqual(summary.get("halted"), "window")
		self.assertEqual(summary.get("rows"), len(chunk1))
		# chunk 1 is durable (its upserts + watermark committed before the
		# halt); the tail resumes from the watermark on the next run
		self.assertEqual(
			str(frappe.db.get_value(STATE, _TEST_DETECTOR, "last_watermark")),
			chunk1[-1]["created"],
		)
		merged = snapshots.read_all(_TEST_DETECTOR)
		self.assertEqual(merged["counts"]["_JPL-FakeCust"]["Fake Invoice"]["n"], len(chunk1))


class TestPreflightPrintSignal(FrappeTestCase):
	"""Plan section 3: zero Print rows while submitted selling documents exist
	=> the print-format detector is not_applicable (custom print engines never
	write Access Log; more data will not fix it), distinct from data_starved."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		factory.build(commit=True)  # submitted selling documents exist
		wipe_print_state()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		wipe_print_state()
		factory.wipe(commit=True)
		frappe.db.commit()
		super().tearDownClass()

	def _state(self):
		return frappe.db.get_value(STATE, DETECTOR_ID, ["not_applicable", "last_error"], as_dict=True)

	def test_zero_print_signal_marks_not_applicable_with_reason(self):
		outcome = bootstrap._apply_print_signal({"supported": True, "access_log_rows": 40, "print_rows": 0})
		self.assertTrue(outcome["applied"])
		self.assertTrue(outcome["not_applicable"])
		state = self._state()
		self.assertEqual(int(state.not_applicable), 1)
		self.assertTrue(str(state.last_error).startswith("not_applicable:"))
		self.assertIn("bypasses Frappe's print system", state.last_error)

	def test_print_signal_clears_a_stale_flag(self):
		bootstrap._apply_print_signal({"supported": True, "access_log_rows": 40, "print_rows": 0})
		outcome = bootstrap._apply_print_signal({"supported": True, "access_log_rows": 41, "print_rows": 1})
		self.assertTrue(outcome["applied"])
		self.assertFalse(outcome["not_applicable"])
		state = self._state()
		self.assertEqual(int(state.not_applicable), 0)
		self.assertFalse(state.last_error, "preflight-written reason must be cleared")

	def test_unsupported_signal_is_a_noop(self):
		outcome = bootstrap._apply_print_signal({"supported": False})
		self.assertFalse(outcome["applied"])

	def test_enablement_preflight_wires_the_signal(self):
		# the test site has factory submissions and zero Print rows here
		result = bootstrap.enablement_preflight()
		self.assertIn("print_signal", result)
		self.assertTrue(result["print_signal"].get("applied"))
		self.assertTrue(result["print_signal"].get("not_applicable"))
		self.assertEqual(int(self._state().not_applicable), 1)
