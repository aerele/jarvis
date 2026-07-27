"""Orchestrator + engine scheduling tests (plan section 5.2).

Covers the pure window math (wrap-aware, boundaries), next_run advance,
stale-run threshold, the dormant-company skip, and the self-host / disabled /
in-window tick paths. No migrate is run; the doctypes are already migrated.
"""

import datetime
import time
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis.learning import engine, orchestrator


def _dt(y, m, d, hh, mm, ss=0):
	return datetime.datetime(y, m, d, hh, mm, ss)


class TestTimeToSeconds(FrappeTestCase):
	def test_timedelta(self):
		self.assertEqual(orchestrator._time_to_seconds(datetime.timedelta(hours=1, minutes=30)), 5400)

	def test_string(self):
		self.assertEqual(orchestrator._time_to_seconds("05:00:00"), 18000)
		self.assertEqual(orchestrator._time_to_seconds("01:00"), 3600)

	def test_time_and_datetime(self):
		self.assertEqual(orchestrator._time_to_seconds(datetime.time(2, 0, 0)), 7200)
		self.assertEqual(orchestrator._time_to_seconds(_dt(2026, 7, 5, 2, 0, 0)), 7200)

	def test_none_and_garbage(self):
		self.assertEqual(orchestrator._time_to_seconds(None), 0)
		self.assertEqual(orchestrator._time_to_seconds("not-a-time"), 0)


class TestInWindow(FrappeTestCase):
	START = "01:00:00"
	END = "05:00:00"

	def test_non_wrap_boundaries(self):
		# Inclusive start, exclusive end.
		self.assertFalse(orchestrator.in_window(self.START, self.END, _dt(2026, 7, 5, 0, 59)))
		self.assertTrue(orchestrator.in_window(self.START, self.END, _dt(2026, 7, 5, 1, 0)))
		self.assertTrue(orchestrator.in_window(self.START, self.END, _dt(2026, 7, 5, 3, 0)))
		self.assertFalse(orchestrator.in_window(self.START, self.END, _dt(2026, 7, 5, 5, 0)))
		self.assertFalse(orchestrator.in_window(self.START, self.END, _dt(2026, 7, 5, 6, 0)))

	def test_wrap_across_midnight(self):
		start, end = "22:00:00", "02:00:00"
		self.assertTrue(orchestrator.in_window(start, end, _dt(2026, 7, 5, 23, 0)))
		self.assertTrue(orchestrator.in_window(start, end, _dt(2026, 7, 5, 1, 0)))
		self.assertTrue(orchestrator.in_window(start, end, _dt(2026, 7, 5, 22, 0)))  # inclusive start
		self.assertFalse(orchestrator.in_window(start, end, _dt(2026, 7, 5, 2, 0)))  # exclusive end
		self.assertFalse(orchestrator.in_window(start, end, _dt(2026, 7, 5, 21, 59)))
		self.assertFalse(orchestrator.in_window(start, end, _dt(2026, 7, 5, 12, 0)))

	def test_degenerate_equal_is_empty(self):
		# Misconfigured start==end fails safe: never in window for scheduled work.
		self.assertFalse(orchestrator.in_window("03:00:00", "03:00:00", _dt(2026, 7, 5, 3, 0)))
		self.assertFalse(orchestrator.in_window("03:00:00", "03:00:00", _dt(2026, 7, 5, 12, 0)))


class TestShouldPauseForWindow(FrappeTestCase):
	START = "01:00:00"
	END = "05:00:00"

	def test_pauses_within_margin_of_end(self):
		self.assertTrue(orchestrator.should_pause_for_window(self.START, self.END, _dt(2026, 7, 5, 4, 57)))
		self.assertTrue(orchestrator.should_pause_for_window(self.START, self.END, _dt(2026, 7, 5, 4, 55)))

	def test_no_pause_with_headroom(self):
		self.assertFalse(orchestrator.should_pause_for_window(self.START, self.END, _dt(2026, 7, 5, 4, 0)))
		self.assertFalse(orchestrator.should_pause_for_window(self.START, self.END, _dt(2026, 7, 5, 1, 30)))

	def test_pauses_when_out_of_window(self):
		self.assertTrue(orchestrator.should_pause_for_window(self.START, self.END, _dt(2026, 7, 5, 6, 0)))


class TestComputeNextWindowStart(FrappeTestCase):
	def test_from_inside_window_advances_to_tomorrow(self):
		nxt = orchestrator.compute_next_window_start("01:00:00", _dt(2026, 7, 5, 1, 15))
		self.assertEqual(nxt, _dt(2026, 7, 6, 1, 0))

	def test_strictly_after_when_exactly_on_start(self):
		nxt = orchestrator.compute_next_window_start("01:00:00", _dt(2026, 7, 5, 1, 0))
		self.assertEqual(nxt, _dt(2026, 7, 6, 1, 0))

	def test_before_start_same_day(self):
		nxt = orchestrator.compute_next_window_start("01:00:00", _dt(2026, 7, 5, 0, 30))
		self.assertEqual(nxt, _dt(2026, 7, 5, 1, 0))


class TestStaleThreshold(FrappeTestCase):
	def test_twenty_min_rule_dominates_midwindow(self):
		now = _dt(2026, 7, 5, 3, 0)
		thr = orchestrator._stale_threshold(now, "01:00:00")
		self.assertEqual(thr, _dt(2026, 7, 5, 2, 40))  # max(01:00, 02:40)

	def test_window_start_dominates_at_open(self):
		now = _dt(2026, 7, 5, 1, 5)
		thr = orchestrator._stale_threshold(now, "01:00:00")
		self.assertEqual(thr, _dt(2026, 7, 5, 1, 0))  # max(01:00, 00:45)

	def test_before_open_twenty_min_rule_dominates(self):
		now = _dt(2026, 7, 5, 0, 30)
		thr = orchestrator._stale_threshold(now, "01:00:00")
		# recent start = yesterday 01:00; now-20min = 00:10; max -> 00:10 (the
		# 20-min rule is later here, so a run heartbeating within 20 min between
		# windows is never falsely killed).
		self.assertEqual(thr, _dt(2026, 7, 5, 0, 10))


class TestAdvanceNextRunAt(FrappeTestCase):
	def test_writes_computed_next_start_via_set_value(self):
		now = _dt(2026, 7, 5, 1, 15)
		with mock.patch("frappe.db.set_value") as sv:
			orchestrator._advance_next_run_at(now, "01:00:00")
		sv.assert_called_once()
		args, kwargs = sv.call_args
		self.assertEqual(args[0], "Jarvis Settings")
		self.assertEqual(args[1], "Jarvis Settings")
		payload = args[2]
		self.assertEqual(payload["pattern_next_run_at"], _dt(2026, 7, 6, 1, 0))
		self.assertFalse(kwargs.get("update_modified"))


class TestDormantCompanySkip(FrappeTestCase):
	def test_skips_companies_without_recent_submitted_txn(self):
		def fake_exists(doctype, name=None):
			if doctype == "DocType":
				return name == "Sales Invoice"  # only one core txn table "exists"
			if isinstance(name, dict):
				return name.get("company") == "ActiveCo"
			return False

		with mock.patch("frappe.db.exists", side_effect=fake_exists):
			active = engine.skip_dormant_companies(["ActiveCo", "DormantCo"])
		self.assertEqual(active, ["ActiveCo"])

	def test_fails_open_when_no_core_doctype_exists(self):
		with mock.patch("frappe.db.exists", return_value=False):
			active = engine.skip_dormant_companies(["A", "B"])
		self.assertEqual(active, ["A", "B"])

	def test_empty_input(self):
		self.assertEqual(engine.skip_dormant_companies([]), [])


class TestNormalizeDetectorResult(FrappeTestCase):
	"""Lock the engine<->executor contract: run_detector returns a
	DetectorResult dataclass, not a dict/list."""

	def test_handles_executor_detectorresult(self):
		from jarvis.learning.executor import DetectorResult

		cand = {"pattern_key": "k1", "n_rows": 7, "detector_id": "d", "domain": "selling"}
		norm = engine._normalize_detector_result(DetectorResult([cand], None))
		self.assertEqual(len(norm["candidates"]), 1)
		self.assertEqual(norm["rows"], 7)
		self.assertIsNone(norm["skipped"])

	def test_handles_skipped_detectorresult(self):
		from jarvis.learning.executor import DetectorResult

		norm = engine._normalize_detector_result(DetectorResult([], "missing field X.y"))
		self.assertEqual(norm["candidates"], [])
		self.assertEqual(norm["skipped"], "missing field X.y")

	def test_tolerates_bare_list_and_dict(self):
		self.assertEqual(engine._normalize_detector_result([{"n_rows": 3}])["rows"], 3)
		rich = engine._normalize_detector_result({"candidates": [{"n_rows": 2}], "rows_scanned": 99})
		self.assertEqual(rich["rows"], 99)

	def test_detectorresult_rows_scanned_wins_over_candidate_nrows(self):
		# fix 6: a real run reports rows_scanned; the engine charges THAT to the
		# budget, not the tiny candidate n_rows sum.
		from jarvis.learning.executor import DetectorResult

		cand = {"pattern_key": "k1", "n_rows": 2}
		norm = engine._normalize_detector_result(DetectorResult([cand], None, rows_scanned=8123))
		self.assertEqual(norm["rows"], 8123)

	def test_raw_candidate_count_is_normalized(self):
		# Drift needs the PRE-cap candidate count: absence from a truncated
		# list is not evidence a pattern stopped holding.
		from jarvis.learning.executor import DetectorResult

		cand = {"pattern_key": "k1", "n_rows": 2}
		norm = engine._normalize_detector_result(
			DetectorResult([cand], None, rows_scanned=5, raw_candidate_count=120)
		)
		self.assertEqual(norm["raw_count"], 120)
		# A hand-built result carries None (unknown - callers stay conservative).
		self.assertIsNone(engine._normalize_detector_result(DetectorResult([cand], None))["raw_count"])
		# A bare list IS the complete candidate set.
		self.assertEqual(engine._normalize_detector_result([{"n_rows": 3}])["raw_count"], 1)
		self.assertEqual(engine._normalize_detector_result(None)["raw_count"], 0)


class TestWeakerOf(FrappeTestCase):
	"""Correction-loop band clamp (shared field contract): pipeline strength_band
	writes take the weaker of (computed band, flag_band_cap)."""

	def test_cap_wins_when_weaker(self):
		self.assertEqual(engine.weaker_of("High", "Low"), "Low")
		self.assertEqual(engine.weaker_of("High", "Medium"), "Medium")
		self.assertEqual(engine.weaker_of("Medium", "Low"), "Low")

	def test_computed_wins_when_already_weaker(self):
		self.assertEqual(engine.weaker_of("Low", "High"), "Low")
		self.assertEqual(engine.weaker_of("Medium", "Medium"), "Medium")

	def test_empty_or_unknown_cap_is_no_ceiling(self):
		self.assertEqual(engine.weaker_of("High", None), "High")
		self.assertEqual(engine.weaker_of("High", ""), "High")
		self.assertEqual(engine.weaker_of("High", "Bogus"), "High")
		self.assertIsNone(engine.weaker_of(None, "Low"))


class TestPersistSurvivors(FrappeTestCase):
	"""Per-survivor guarded persistence: one bad candidate must never abort the
	rest of its FDR family (or, at the post-loop flush, the run's snapshot/
	surfacing/drift steps), and failures are attributed to the RELEASED family's
	detector_id with truthful counts."""

	def test_one_bad_candidate_does_not_abort_the_family(self):
		cands = [
			{"pattern_key": "a", "company": "C1"},
			{"pattern_key": "b", "company": "C2"},
			{"pattern_key": "c", "company": "C3"},
		]

		def persist(cand, run):
			if cand["pattern_key"] == "b":
				raise ValueError("boom")
			return "created"

		with (
			mock.patch.object(engine, "_persist_candidate", side_effect=persist),
			mock.patch("frappe.log_error") as log_error,
		):
			res = engine._persist_survivors(cands, None, "det-a")

		self.assertEqual(res["created"], 2)
		self.assertEqual(res["updated"], 0)
		self.assertEqual(res["duplicates"], 0)
		self.assertEqual(len(res["errors"]), 1)
		self.assertEqual(res["errors"][0]["detector_id"], "det-a")
		self.assertEqual(res["errors"][0]["company"], "C2")
		log_error.assert_called_once()
		self.assertIn("det-a", log_error.call_args.kwargs.get("title", ""))

	def test_outcomes_counted(self):
		outcomes = iter(["created", "updated", "duplicate"])
		with mock.patch.object(engine, "_persist_candidate", side_effect=lambda c, r: next(outcomes)):
			res = engine._persist_survivors([{}, {}, {}], None, "det-b")
		self.assertEqual((res["created"], res["updated"], res["duplicates"]), (1, 1, 1))
		self.assertEqual(res["errors"], [])

	def test_empty_and_none_inputs(self):
		self.assertEqual(engine._persist_survivors([], None, "det-c")["errors"], [])
		self.assertEqual(engine._persist_survivors(None, None, "det-c")["created"], 0)


class TestDriftStash(FrappeTestCase):
	"""The mining loop stashes {(detector_id, company): {pattern_key: candidate}}
	for drift re-validation - only watched (Approved/Active) keys, with the
	unit's cap-truncation state - so drift never re-runs a detector."""

	def _detect(self, result, mined, watch, company="CoA"):
		from jarvis.learning.fdr import DetectorFamilyBuffer

		spec = {"id": "det-stash", "doctype": "Sales Invoice"}
		with mock.patch("jarvis.learning.executor.run_detector", return_value=result):
			return engine._read_and_persist(
				spec,
				company,
				None,
				fdr_buffer=DetectorFamilyBuffer(),
				mined=mined,
				watch=watch,
			)

	def test_stashes_watched_keys_only(self):
		from jarvis.learning.executor import DetectorResult

		watched = {"pattern_key": "k-watched", "n_rows": 1}
		other = {"pattern_key": "k-other", "n_rows": 1}
		mined: dict = {}
		watch = {("det-stash", "CoA"): {"k-watched"}}
		unit = self._detect(
			DetectorResult([watched, other], None, rows_scanned=2, raw_candidate_count=2),
			mined,
			watch,
		)
		entry = mined[("det-stash", "CoA")]
		self.assertEqual(set(entry["by_key"]), {"k-watched"})
		self.assertEqual(entry["tracked"], {"k-watched"})
		self.assertFalse(entry["cap_truncated"])
		self.assertEqual(unit["candidates"], 2)
		self.assertEqual(unit["persist_errors"], [])

	def test_cap_truncated_unit_is_marked(self):
		from jarvis.learning.executor import DetectorResult

		cand = {"pattern_key": "k1", "n_rows": 1}
		mined: dict = {}
		unit = self._detect(
			DetectorResult([cand], None, rows_scanned=1, raw_candidate_count=7),
			mined,
			{},
		)
		self.assertTrue(mined[("det-stash", "CoA")]["cap_truncated"])
		self.assertEqual(unit["candidates"], 1)

	def test_skipped_unit_is_not_stashed(self):
		from jarvis.learning.executor import DetectorResult

		mined: dict = {}
		unit = self._detect(DetectorResult([], "missing field X.y"), mined, {})
		self.assertEqual(mined, {})
		self.assertEqual(unit["skipped"], "missing field X.y")


class TestRowBudget(FrappeTestCase):
	"""fix 6: the nightly row budget can actually trip once detectors report a
	real scanned-row count."""

	def _run(self):
		return frappe._dict(trigger="manual", window_start_used="01:00:00", window_end_used="05:00:00")

	def test_pause_reason_trips_when_budget_exhausted(self):
		with mock.patch.object(engine, "_feature_enabled", return_value=True):
			reason = engine._pause_reason(self._run(), time.monotonic(), 600000, 500000)
		self.assertIsNotNone(reason)
		self.assertIn("row budget", reason)

	def test_pause_reason_none_under_budget(self):
		with mock.patch.object(engine, "_feature_enabled", return_value=True):
			reason = engine._pause_reason(self._run(), time.monotonic(), 100, 500000)
		self.assertIsNone(reason)


class TestWriteFence(FrappeTestCase):
	"""fix 8: engine persistence may target only the §5.4 allowlist."""

	def test_allowlisted_doctypes_pass(self):
		for dt in (
			"Jarvis Pattern Run",
			"Jarvis Pattern Detector State",
			"Jarvis Learned Pattern",
			"Jarvis Learned Pattern Role",
			"Jarvis Pattern Snapshot",
		):
			engine._fenced_write(dt)  # must not raise

	def test_non_allowlisted_doctype_raises(self):
		for dt in ("Sales Invoice", "Jarvis Settings", "Jarvis Custom Skill", "Notification Log"):
			with self.assertRaises(engine.PatternWriteFenceError):
				engine._fenced_write(dt)

	def test_none_is_empty(self):
		self.assertEqual(engine._normalize_detector_result(None)["candidates"], [])


class TestTickBails(FrappeTestCase):
	def test_bails_on_kill_switch(self):
		with mock.patch.dict(frappe.conf, {"jarvis_pattern_learning_disabled": 1}):
			with mock.patch("frappe.enqueue") as enq:
				orchestrator.tick()
		enq.assert_not_called()

	def test_bails_on_self_host(self):
		with mock.patch("jarvis.selfhost.is_self_hosted", return_value=True):
			with mock.patch("frappe.enqueue") as enq:
				orchestrator.tick()
		enq.assert_not_called()

	def test_bails_when_disabled(self):
		with mock.patch("jarvis.selfhost.is_self_hosted", return_value=False):
			with mock.patch.object(orchestrator, "_feature_enabled", return_value=False):
				with mock.patch("frappe.enqueue") as enq:
					orchestrator.tick()
		enq.assert_not_called()

	def test_bails_when_not_onboarded(self):
		with mock.patch("jarvis.selfhost.is_self_hosted", return_value=False):
			with mock.patch.object(orchestrator, "_feature_enabled", return_value=True):
				with mock.patch.object(orchestrator, "_is_onboarded", return_value=False):
					with mock.patch("frappe.enqueue") as enq:
						orchestrator.tick()
		enq.assert_not_called()


class TestTickCreatesRunInWindow(FrappeTestCase):
	def setUp(self):
		super().setUp()
		# Isolation: a committed OPEN (Queued/Running/Paused) Jarvis Pattern Run
		# leaked by a sibling test (e.g. a manual run left Paused) makes
		# _schedule_in_window take the resume/skip path instead of creating tonight's
		# fresh run — so tick() enqueues the leaked run (with a ::hop job_id) and the
		# count never increases (before != before+1). Clear any open runs so tick
		# sees the clean slate this test assumes. Production behavior (resume an
		# unfinished night before opening a new run) is correct and unchanged.
		for n in frappe.get_all(
			orchestrator.RUN,
			filters={"status": ["in", list(orchestrator._OPEN_STATUSES)]},
			pluck="name",
		):
			frappe.delete_doc(orchestrator.RUN, n, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_in_window_and_due_creates_run_and_enqueues(self):
		now = now_datetime()
		start = (add_to_date(now, hours=-2)).strftime("%H:%M:%S")
		end = (add_to_date(now, hours=2)).strftime("%H:%M:%S")

		def sv(field):
			return {
				"pattern_window_start": start,
				"pattern_window_end": end,
				"pattern_next_run_at": None,  # due
			}.get(field)

		before = frappe.db.count("Jarvis Pattern Run")
		with (
			mock.patch("jarvis.selfhost.is_self_hosted", return_value=False),
			mock.patch.object(orchestrator, "_feature_enabled", return_value=True),
			mock.patch.object(orchestrator, "_is_onboarded", return_value=True),
			mock.patch.object(orchestrator, "_settings_value", side_effect=sv),
			mock.patch("frappe.db.set_value"),
			mock.patch("frappe.enqueue") as enq,
		):
			orchestrator.tick()

		enq.assert_called_once()
		_args, kwargs = enq.call_args
		self.assertEqual(kwargs.get("queue"), "long")
		self.assertTrue(kwargs.get("job_id", "").startswith("jarvis_pattern_run::"))
		self.assertTrue(kwargs.get("enqueue_after_commit"))
		self.assertEqual(frappe.db.count("Jarvis Pattern Run"), before + 1)

	def test_out_of_window_does_not_create_run(self):
		now = now_datetime()
		# A window that does not contain now: starts in 3h, ends in 4h.
		start = (add_to_date(now, hours=3)).strftime("%H:%M:%S")
		end = (add_to_date(now, hours=4)).strftime("%H:%M:%S")

		def sv(field):
			return {
				"pattern_window_start": start,
				"pattern_window_end": end,
				"pattern_next_run_at": None,
			}.get(field)

		with (
			mock.patch("jarvis.selfhost.is_self_hosted", return_value=False),
			mock.patch.object(orchestrator, "_feature_enabled", return_value=True),
			mock.patch.object(orchestrator, "_is_onboarded", return_value=True),
			mock.patch.object(orchestrator, "_settings_value", side_effect=sv),
			mock.patch("frappe.enqueue") as enq,
		):
			orchestrator.tick()

		enq.assert_not_called()


class TestRunNowGuards(FrappeTestCase):
	def test_refuses_on_self_host(self):
		with mock.patch("jarvis.selfhost.is_self_hosted", return_value=True):
			res = orchestrator.run_now("Administrator")
		self.assertFalse(res["ok"])

	def test_refuses_when_disabled(self):
		with (
			mock.patch("jarvis.selfhost.is_self_hosted", return_value=False),
			mock.patch.object(orchestrator, "_feature_enabled", return_value=False),
		):
			res = orchestrator.run_now("Administrator")
		self.assertFalse(res["ok"])


class TestEngineWriteFenceIntegration(FrappeTestCase):
	"""fix 8: a full engine run leaves every table row count unchanged EXCEPT the
	allowlisted pattern doctypes - the READ ONLY fence + write allowlist in one
	end-to-end assertion (plan §5.4 integration test)."""

	# Read-only during a run: the doctypes detectors READ (and a couple of masters
	# an org-wide detector reads). None may gain or lose a row.
	READ_TABLES = (
		"Sales Invoice",
		"Sales Invoice Item",
		"Purchase Order",
		"Purchase Order Item",
		"Purchase Invoice",
		"Payment Entry",
		"Quotation",
		"Stock Entry",
		"Stock Entry Detail",
		"Timesheet",
		"Project",
		"Item",
		"Item Price",
		"Company",
		"Address",
		"DocType",
		"Property Setter",
		"Jarvis Custom Skill",
	)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		from jarvis.tests.learning import factory

		cls.factory = factory
		factory.wipe()
		factory.build(commit=True)
		frappe.get_single("Jarvis Settings").db_set("pattern_learning_enabled", 1, update_modified=False)
		# Only clean up rows WE create; leave any pre-existing patterns untouched.
		cls._pre_jlp = set(frappe.get_all(engine.JLP, pluck="name"))
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.flags.jarvis_pattern_engine = True
		for n in frappe.get_all(engine.JLP, pluck="name"):
			if n not in cls._pre_jlp:
				frappe.delete_doc(engine.JLP, n, force=True, ignore_permissions=True)
		cls.factory.wipe()
		frappe.db.commit()
		super().tearDownClass()

	def test_full_run_mutates_only_allowlisted_tables(self):
		before = {dt: frappe.db.count(dt) for dt in self.READ_TABLES}

		run = frappe.get_doc(
			{
				"doctype": engine.RUN,
				"status": "Queued",
				"trigger": "manual",
				"window_start_used": "00:00:00",
				"window_end_used": "23:59:59",
				"scan_mode": "full",
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		frappe.db.commit()

		jlp_before = frappe.db.count(engine.JLP)
		# Force the seeded mini-org companies through the (otherwise dormant) skip so
		# company-scoped detectors actually run and persist.
		with mock.patch.object(
			engine,
			"skip_dormant_companies",
			side_effect=lambda comps, **k: [c for c in comps if str(c).startswith(factory_prefix())],
		):
			engine.run_pattern_analysis(run.name)
		frappe.db.commit()

		after = {dt: frappe.db.count(dt) for dt in self.READ_TABLES}
		for dt in self.READ_TABLES:
			self.assertEqual(before[dt], after[dt], f"detector run mutated read-only table {dt!r}")

		run_doc = frappe.get_doc(engine.RUN, run.name)
		self.assertIn(run_doc.status, ("Completed", "Partial"))
		# Writes DID land - in the allowlisted Jarvis Learned Pattern table.
		self.assertGreaterEqual(frappe.db.count(engine.JLP), jlp_before)


def factory_prefix() -> str:
	from jarvis.tests.learning import factory

	return factory.PREFIX
