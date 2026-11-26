"""Payment Calendar S4b B2: the cache switch in Jarvis Settings and `cache_state`.

Covers the three hidden settings fields and their save path, `cache_settings()`, the lane probe, the
bench-wide slots and the timing constants that the claim and the slots share. Nothing here commits a mode:
values live in the test transaction and roll back. The fields reach `test_jarvis` through `reload_doc`
only (never a migrate of a shared site). Redis keys are private to each test.

Run ONLY on the test site:
    bench --site test_jarvis run-tests --module jarvis.tests.test_payment_calendar_cache_state
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.background_jobs import generate_qname
from rq.timeouts import JobTimeoutException

from jarvis.jarvis.doctype.jarvis_settings import jarvis_settings
from jarvis.payment_calendar import cache_state, redis_slots
from jarvis.tests.test_payment_calendar_routing import (
	SETTINGS,
	SYSMAN,
	RoutingSettingsCase,
	_as,
	_companies,
	_raw,
	_save_as_administrator,
	_stored,
	_version_count,
)

MODE = "payment_calendar_cache"
COMPANIES = "payment_calendar_cache_companies"
FULL = "payment_calendar_cache_full_cadence"
FIELDS = (MODE, COMPANIES, FULL)
OFF = cache_state.CacheSettings("off", (), False)
UNKNOWN = cache_state.UNKNOWN

_start_settings = []


def setUpModule():
	# The switch as the site has it now: the last test checks that the run left it as it was.
	_start_settings.append(cache_state.cache_settings())


class TestCacheSettingsFields(RoutingSettingsCase):
	def test_fields_are_hidden_permlevel_two_and_admin_only(self):
		meta = frappe.get_meta(SETTINGS)
		for fieldname in FIELDS:
			df = meta.get_field(fieldname)
			self.assertEqual((df.hidden, df.permlevel), (1, 2), fieldname)
			self.assertIn(fieldname, jarvis_settings.JarvisSettings._ADMIN_ONLY_FIELDS)

	def test_field_types_and_defaults(self):
		meta = frappe.get_meta(SETTINGS)
		self.assertEqual(meta.get_field(MODE).fieldtype, "Select")
		# `on` is accepted and acts as `shadow` until S5 (S5 plan 6a, item 9).
		self.assertEqual(meta.get_field(MODE).options.split("\n"), ["off", "shadow", "on"])
		self.assertEqual(meta.get_field(MODE).default, "off")
		self.assertEqual(meta.get_field(COMPANIES).fieldtype, "Small Text")
		self.assertEqual(meta.get_field(FULL).fieldtype, "Check")
		self.assertEqual(str(meta.get_field(FULL).default), "0")

	def test_a_fresh_settings_doc_defaults_to_off_and_not_full_cadence(self):
		doc = frappe.new_doc(SETTINGS)
		self.assertEqual(doc.get(MODE), "off")
		self.assertEqual(frappe.utils.cint(doc.get(FULL)), 0)

	def test_the_fields_are_in_the_form_order(self):
		meta = frappe.get_meta(SETTINGS)
		names = [df.fieldname for df in meta.fields]
		for fieldname in FIELDS:
			self.assertEqual(names.count(fieldname), 1, fieldname)


class TestCacheSettingsRead(RoutingSettingsCase):
	def test_defaults_read_as_off_with_no_companies_and_no_full_cadence(self):
		for field in FIELDS:
			frappe.db.sql("DELETE FROM `tabSingles` WHERE doctype=%s AND field=%s", (SETTINGS, field))
		self.assertEqual(cache_state.cache_settings(), OFF)

	def test_lines_are_stripped_and_blanks_dropped_and_names_stay_exact(self):
		_raw(MODE, "shadow")
		_raw(COMPANIES, "  Alpha Co \n\n\t\nbeta co\r\n   ")
		_raw(FULL, "1")
		self.assertEqual(
			cache_state.cache_settings(), cache_state.CacheSettings("shadow", ("Alpha Co", "beta co"), True)
		)

	def test_an_unknown_mode_value_reads_as_unknown_which_acts_as_off_but_is_not_a_positive_off(self):
		for value in ("ON", "enabled", "1", "shadow "):
			_raw(MODE, value)
			_raw(COMPANIES, "Alpha Co")
			read = cache_state.cache_settings()
			self.assertEqual(read, UNKNOWN, repr(value))
			self.assertFalse(read.active)
			self.assertFalse(read.known)

	def test_a_saved_off_is_known_and_a_never_saved_mode_is_a_known_off(self):
		_raw(MODE, "off")
		self.assertTrue(cache_state.cache_settings().known)
		self.assertTrue(cache_state.cache_settings(lists_when_off=False).known)
		for value in ("", None):
			_raw(MODE, value)
			self.assertEqual(cache_state.cache_settings(), OFF, repr(value))

	def test_each_known_mode_reads_back_and_on_acts_as_shadow(self):
		for value, effective in (("off", "off"), ("shadow", "shadow"), ("on", "shadow")):
			_raw(MODE, value)
			read = cache_state.cache_settings()
			self.assertEqual(read.mode, value)
			self.assertEqual(read.effective_mode, effective)
			self.assertEqual(read.active, effective != "off")

	def test_a_missing_field_reads_as_unknown_without_a_read(self):
		meta = MagicMock()
		meta.has_field.return_value = False
		with (
			patch.object(frappe, "get_meta", return_value=meta),
			patch.object(frappe.db, "get_single_value") as read,
		):
			self.assertEqual(cache_state.cache_settings(), UNKNOWN)
		read.assert_not_called()

	def test_a_read_error_that_keeps_the_transaction_reads_as_unknown_not_off(self):
		_raw(MODE, "shadow")
		for exc in (RuntimeError("boom"), frappe.db.OperationalError(1054, "x")):
			with patch.object(frappe.db, "get_single_value", side_effect=exc):
				read = cache_state.cache_settings()
			self.assertEqual(read, UNKNOWN, repr(exc))
			self.assertFalse(read.active)
			self.assertFalse(read.known)

	def test_a_read_error_that_may_have_rolled_back_propagates(self):
		_raw(MODE, "shadow")
		for exc in (
			frappe.QueryDeadlockError("x"),
			frappe.db.OperationalError(2013, "x"),
			frappe.db.OperationalError(1213, "x"),
		):
			with patch.object(frappe.db, "get_single_value", side_effect=exc):
				with self.assertRaises(type(exc)) as raised:
					cache_state.cache_settings()
			self.assertIs(raised.exception, exc)

	def test_a_job_deadline_is_not_swallowed(self):
		_raw(MODE, "shadow")
		for exc in (TimeoutError(), JobTimeoutException()):
			with patch.object(frappe.db, "get_single_value", side_effect=exc):
				with self.assertRaises(type(exc)) as raised:
					cache_state.cache_settings()
			self.assertIs(raised.exception, exc)

	def test_off_costs_one_singles_read_when_the_lists_are_not_wanted(self):
		_raw(MODE, "off")
		_raw(COMPANIES, "Alpha Co")
		with patch.object(frappe.db, "get_single_value", wraps=frappe.db.get_single_value) as read:
			self.assertEqual(cache_state.cache_settings(lists_when_off=False), OFF)
		self.assertEqual([call.args[1] for call in read.call_args_list], [MODE])
		# The default reads them (the janitor must see which companies are still listed).
		self.assertEqual(cache_state.cache_settings().companies, ("Alpha Co",))

	def test_values_are_fresh_on_every_call(self):
		_raw(MODE, "shadow")
		self.assertEqual(cache_state.cache_settings().mode, "shadow")
		self.assertEqual(frappe.db.get_single_value(SETTINGS, MODE), "shadow")
		_raw(MODE, "off")
		self.assertEqual(cache_state.cache_settings().mode, "off")

	def test_a_deadlock_inside_a_snapshot_view_breaks_the_view(self):
		from jarvis.payment_calendar.errors import CalendarSourceUnavailable
		from jarvis.payment_calendar.snapshot import consistent_read

		with self.assertRaises(CalendarSourceUnavailable) as raised:
			with consistent_read():
				with patch.object(frappe.db, "get_single_value", side_effect=frappe.QueryDeadlockError("x")):
					with self.assertRaises(frappe.QueryDeadlockError):
						cache_state.cache_settings()
		self.assertEqual(raised.exception.code, "source_changed")

	def test_a_listed_name_that_no_company_has_is_listed_as_missing(self):
		(company,) = _companies(1)
		current = cache_state.CacheSettings("shadow", (company, "Ghost Co"), False)
		self.assertEqual(cache_state.listed_but_missing(current), ("Ghost Co",))
		self.assertEqual(cache_state.listed_but_missing(OFF), ())


class TestScanKeys(TestCase):
	def test_it_finds_the_site_keys_under_a_prefix_without_the_keys_command(self):
		prefix = f"jarvis:pcal:test:scan:{frappe.generate_hash(length=8)}:"
		keys = [frappe.cache.make_key(f"{prefix}{n}") for n in range(3)]
		other = frappe.cache.make_key(f"jarvis:pcal:test:scan_other:{frappe.generate_hash(length=8)}")
		for key in (*keys, other):
			frappe.cache.execute_command("SET", key, "1", "EX", 60)
		self.addCleanup(frappe.cache.execute_command, "DEL", other)
		self.addCleanup(lambda: [frappe.cache.execute_command("DEL", key) for key in keys])
		with patch.object(frappe.cache, "keys", side_effect=AssertionError("KEYS")):
			found = cache_state.scan_keys(prefix)
		self.assertEqual(sorted(found), sorted(keys))

	def test_no_key_is_an_empty_list(self):
		self.assertEqual(cache_state.scan_keys("jarvis:pcal:test:scan:nothing:here:"), [])


class TestCacheSettingsSave(RoutingSettingsCase):
	def test_administrator_save_is_accepted_with_one_version_row(self):
		(company,) = _companies(1)
		before = _version_count()
		_save_as_administrator(**{MODE: "shadow", COMPANIES: company, FULL: 1})
		self.assertEqual(_version_count(), before + 1)
		self.assertEqual(cache_state.cache_settings(), cache_state.CacheSettings("shadow", (company,), True))

	def test_a_save_normalizes_the_list(self):
		first, second = _companies(2)
		_save_as_administrator(**{COMPANIES: f"  {first}  \n\n{second}\n  \n"})
		self.assertEqual(_stored(COMPANIES), f"{first}\n{second}")

	def test_a_system_manager_save_cannot_change_any_field(self):
		(company,) = _companies(1)
		before = cache_state.cache_settings()
		for field, value in ((MODE, "shadow"), (COMPANIES, company), (FULL, 1)):
			with _as(SYSMAN), patch("frappe.enqueue"):
				settings = frappe.get_single(SETTINGS)
				settings.set(field, value)
				settings.save()
			self.assertNotEqual(_stored(field), str(value), field)
		self.assertEqual(cache_state.cache_settings(), before)

	def test_a_self_granted_permlevel_write_is_still_refused(self):
		from frappe import permissions as frappe_permissions
		from frappe.core.page.permission_manager import permission_manager

		(company,) = _companies(1)
		with _as(SYSMAN):
			permission_manager.update(SETTINGS, "System Manager", 2, "write", 1)
		frappe.db.commit()
		try:
			for field, value in ((MODE, "shadow"), (COMPANIES, company), (FULL, 1)):
				with _as(SYSMAN), patch("frappe.enqueue"), self.assertRaises(frappe.PermissionError):
					settings = frappe.get_single(SETTINGS)
					settings.set(field, value)
					settings.save()
		finally:
			frappe.db.rollback()
			frappe_permissions.reset_perms(SETTINGS)
			frappe.clear_cache(doctype=SETTINGS)
			frappe.db.commit()

	def test_an_added_unknown_company_is_refused_and_the_refusal_escapes_the_name(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			_save_as_administrator(**{COMPANIES: "<img src=x onerror=alert(1)>"})
		self.assertNotIn("<img", str(raised.exception))
		self.assertIn("&lt;img", str(raised.exception))
		with self.assertRaises(frappe.ValidationError):
			_save_as_administrator(**{COMPANIES: "No Such Company Ltd"})

	def test_a_company_name_must_match_exactly(self):
		(company,) = _companies(1)
		with self.assertRaises(frappe.ValidationError):
			_save_as_administrator(**{COMPANIES: company.swapcase()})

	def test_a_stale_line_does_not_block_a_save_of_another_field(self):
		_raw(COMPANIES, "Deleted Company Ltd")
		_raw(MODE, "shadow")
		with patch.object(jarvis_settings.JarvisSettings, "_on_update_single_model_legacy"):
			settings = _save_as_administrator(payment_calendar_chat_handoff=1)
		self.assertEqual(settings.get(COMPANIES), "Deleted Company Ltd")
		self.assertEqual(cache_state.cache_settings().mode, "shadow")

	def test_a_save_of_only_the_cache_fields_runs_no_llm_sync(self):
		(company,) = _companies(1)
		failed = "failed: pc cache state test"
		frappe.db.set_single_value(SETTINGS, "last_sync_status", failed)
		with (
			patch("frappe.enqueue") as enqueue,
			patch.object(jarvis_settings, "_switch_or_enqueue") as switch_or_enqueue,
			patch.object(jarvis_settings, "_write_settings_fields") as write_fields,
			patch("jarvis.chat.wiki_mirror.enqueue_scrub") as scrub,
		):
			_save_as_administrator(**{MODE: "shadow", COMPANIES: company, FULL: 1})
		enqueue.assert_not_called()
		switch_or_enqueue.assert_not_called()
		write_fields.assert_not_called()
		scrub.assert_not_called()
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "last_sync_status", cache=False), failed)
		self.assertEqual(cache_state.cache_settings().mode, "shadow")


def _worker(*queues):
	return SimpleNamespace(queue_names=lambda: [generate_qname(name) for name in queues])


def _conf(*queues):
	return {"workers": {name: {"timeout": 1200} for name in queues}}


class TestLaneProbe(TestCase):
	"""A lane worker listens on `jarvis_pcal` and nothing else (S4 D-5, C2-13)."""

	def probe(self, workers, conf):
		with (
			patch.object(frappe, "get_conf", return_value=conf),
			patch("frappe.utils.background_jobs.get_workers", return_value=workers),
		):
			return cache_state.lane_workers()

	def test_it_counts_only_workers_whose_queue_list_is_exactly_the_lane(self):
		workers = [
			_worker("jarvis_pcal"),
			_worker("jarvis_pcal"),
			_worker("short", "default", "long", "jarvis_pcal"),  # a generic worker also takes the lane
			_worker("long"),
			_worker("jarvis_pcal", "long"),
		]
		self.assertEqual(self.probe(workers, _conf("jarvis_pcal")), 2)

	def test_a_lane_that_is_not_in_the_workers_config_has_no_workers(self):
		self.assertEqual(self.probe([_worker("jarvis_pcal")], _conf("jarvis_chat")), 0)
		self.assertEqual(self.probe([_worker("jarvis_pcal")], {}), 0)
		self.assertEqual(self.probe([_worker("jarvis_pcal")], {"workers": None}), 0)

	def test_a_lane_without_workers_is_not_live(self):
		with (
			patch.object(frappe, "get_conf", return_value=_conf("jarvis_pcal")),
			patch("frappe.utils.background_jobs.get_workers", return_value=[_worker("long")]),
		):
			self.assertEqual(cache_state.lane_workers(), 0)
			self.assertFalse(cache_state.lane_live())

	def test_a_live_lane_with_one_worker(self):
		with (
			patch.object(frappe, "get_conf", return_value=_conf("jarvis_pcal")),
			patch("frappe.utils.background_jobs.get_workers", return_value=[_worker("jarvis_pcal")]),
		):
			self.assertTrue(cache_state.lane_live())

	def test_a_probe_error_is_no_lane_and_a_job_deadline_is_not_swallowed(self):
		with (
			patch.object(frappe, "get_conf", return_value=_conf("jarvis_pcal")),
			patch("frappe.utils.background_jobs.get_workers", side_effect=RuntimeError("redis down")),
		):
			self.assertEqual(cache_state.lane_workers(), 0)
		with (
			patch.object(frappe, "get_conf", return_value=_conf("jarvis_pcal")),
			patch("frappe.utils.background_jobs.get_workers", side_effect=JobTimeoutException()),
		):
			with self.assertRaises(JobTimeoutException):
				cache_state.lane_workers()

	def test_a_worker_without_queue_names_is_not_counted(self):
		self.assertEqual(
			self.probe(
				[SimpleNamespace(queue_names=lambda: None), _worker("jarvis_pcal")], _conf("jarvis_pcal")
			),
			1,
		)


class TestBenchSlots(TestCase):
	"""Bench-wide slots: day = the lane's workers, night = `long` workers minus one, at least one (S4 D-4 step 2, D-5, E8)."""

	def setUp(self):
		suffix = frappe.generate_hash(length=10)
		patcher = patch.object(cache_state, "SLOT_PREFIX", f"jarvis:pcal:test:cache_state:{suffix}:slot:")
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(
			lambda: [frappe.cache.execute_command("DEL", cache_state.slot_key(n)) for n in range(5)]
		)

	def count(self, night, lane=0, long=0):
		with (
			patch.object(cache_state, "lane_workers", return_value=lane),
			patch.object(cache_state, "long_workers", return_value=long),
		):
			return cache_state.bench_slots(night=night)

	def test_the_day_slots_are_the_lane_workers(self):
		self.assertEqual(self.count(False, lane=3, long=9), 3)
		self.assertEqual(self.count(False, lane=0, long=9), 0)

	def test_the_night_slots_are_the_long_workers_minus_one_and_one_for_a_single_worker(self):
		# E8-1..E8-3: a bench with one `long` worker builds one unit a night; none at all with no `long` worker.
		for lane in (0, 3):
			for long, slots in ((0, 0), (1, 1), (2, 1), (3, 2), (4, 3)):
				with self.subTest(lane=lane, long=long):
					self.assertEqual(self.count(True, lane=lane, long=long), slots)

	def test_a_bench_without_a_lane_has_night_slots_and_no_day_slots(self):
		self.assertEqual((self.count(False, lane=0, long=1), self.count(True, lane=0, long=1)), (0, 1))

	def test_the_slot_key_is_shared_across_sites(self):
		self.assertIsInstance(cache_state.slot_key(0), str)
		self.assertTrue(cache_state.slot_key(2).endswith("slot:2"))
		self.assertTrue(cache_state.SLOT_PREFIX.startswith("jarvis:pcal:"))

	def test_the_default_slot_keys_live_under_the_cache_prefix(self):
		prefix = "jarvis:pcal:cache:slot:"
		with patch.object(cache_state, "SLOT_PREFIX", prefix):
			self.assertEqual(cache_state.slot_key(1), prefix + "1")

	def test_slots_are_taken_up_to_the_count_and_released_by_token(self):
		with patch.object(cache_state, "bench_slots", return_value=2):
			first = cache_state.take_slot("a", night=False)
			second = cache_state.take_slot("b", night=False)
			self.assertEqual((first, second), (cache_state.slot_key(0), cache_state.slot_key(1)))
			self.assertIsNone(cache_state.take_slot("c", night=False))
			cache_state.release_slot(first, "wrong")
			self.assertIsNone(cache_state.take_slot("c", night=False))
			cache_state.release_slot(first, "a")
			self.assertEqual(cache_state.take_slot("c", night=False), first)

	def test_one_long_worker_gives_one_night_slot_and_a_second_unit_is_refused(self):
		with (
			patch.object(cache_state, "lane_workers", return_value=0),
			patch.object(cache_state, "long_workers", return_value=1),
		):
			first = cache_state.take_slot("a", night=True)
			self.assertEqual(first, cache_state.slot_key(0))
			self.assertIsNone(cache_state.take_slot("b", night=True))
			self.assertIsNone(cache_state.take_slot("c", night=False))

	def test_no_slots_means_none_and_touches_no_key(self):
		with (
			patch.object(cache_state, "bench_slots", return_value=0),
			patch.object(redis_slots, "claim") as claim,
		):
			self.assertIsNone(cache_state.take_slot("a", night=False))
		claim.assert_not_called()

	def test_a_slot_lives_as_long_as_a_claim_and_renews_to_that_length(self):
		with patch.object(cache_state, "bench_slots", return_value=1):
			key = cache_state.take_slot("a", night=True)
		ttl = frappe.cache.execute_command("TTL", key)
		self.assertTrue(cache_state.claim_ttl_s(night=True) - 5 <= ttl <= cache_state.claim_ttl_s(night=True))
		frappe.cache.execute_command("EXPIRE", key, 10)
		self.assertTrue(cache_state.renew_slot(key, "a", night=False))
		self.assertTrue(cache_state.claim_ttl_s(night=False) - 5 <= frappe.cache.execute_command("TTL", key))
		self.assertFalse(cache_state.renew_slot(key, "other", night=False))

	def test_redis_down_at_admission_is_no_slot_and_a_job_deadline_is_not_swallowed(self):
		with (
			patch.object(cache_state, "bench_slots", return_value=2),
			patch.object(redis_slots, "take_slot", side_effect=RuntimeError("redis down")),
		):
			self.assertIsNone(cache_state.take_slot("a", night=False))
		with (
			patch.object(cache_state, "bench_slots", return_value=2),
			patch.object(redis_slots, "take_slot", side_effect=JobTimeoutException()),
		):
			with self.assertRaises(JobTimeoutException):
				cache_state.take_slot("a", night=False)


class TestTiming(TestCase):
	"""The numbers of S4 §14 that the claim, the slots and the loop all derive from."""

	def test_the_claim_lives_view_plus_write_plus_margin(self):
		self.assertEqual(cache_state.claim_ttl_s(night=False), 240 + 120 + 120)
		self.assertEqual(cache_state.claim_ttl_s(night=True), 720 + 120 + 120)

	def test_the_enforced_per_iteration_bound(self):
		self.assertEqual(cache_state.STATEMENT_CAP_S, 60)
		self.assertEqual(cache_state.D_MAX_S, 6 * 60)
		# The loop continues only with this much job time left.
		self.assertEqual(cache_state.LOOP_MIN_LEFT_S, cache_state.D_MAX_S + 120)

	def test_a_loop_iteration_fits_in_the_job_timeout(self):
		self.assertEqual(cache_state.job_timeout_s(night=False), 1200)
		self.assertEqual(cache_state.job_timeout_s(night=True), 1800)
		self.assertGreater(cache_state.job_timeout_s(night=False), cache_state.LOOP_MIN_LEFT_S)

	def test_the_lane_queue_name(self):
		self.assertEqual(cache_state.LANE_QUEUE, "jarvis_pcal")


class TestZZNoModeLeftBehind(FrappeTestCase):
	"""Runs last in the module (alphabetical): the site must read as it did after the suite."""

	def test_the_mode_is_as_the_site_had_it(self):
		self.assertEqual(cache_state.cache_settings(), _start_settings[-1])
