"""Batch permission: sample oracle, trip, transient limit, breaker, stats and the job stop (S2b-batch, T3).

Covers the sample oracle and its re-check, the transient limit, the stop in the re-check, the job stop, the
mode change in a job, reads (no sample), the stats, the breaker and its durability, and the trip flag.

Names these tests rely on in `batch_permission`:
- `_sample_oracle(doctype, names, ptype, plan, result)`: the sample entry point (jobs only, never on a read).
- `_trip(doctype, reason)`: the trip: step 1 `_set_breaker_key(doctype, reason)` (Redis SET
  `<BREAKER_KEY><doctype>` = `<iso time>|<reason>`, no TTL), step 2 `frappe.installer.update_site_config(SWITCH,
  0)` (called through the module attribute), step 3 the flag `frappe.local.pcal_permission_tripped`. A failure of
  step 1 or 2 queues an event `{"operation": "pcal_permission_breaker", "message": "breaker_write_failed:<step>"}`
  with `<step>` = `breaker_key` or `site_config`; the other steps still run.
- A job is a process that called `begin_mode_freeze()` (the freeze map on `frappe.local` is not None).
- The sample and the re-check call `_evaluate_batch` through the module global, and the counters call
  `read_log.site_count(klass, date)` through the module attribute (the tests rewrite the date).
- Confirmed mismatch: event `{"operation": "pcal_permission_batch_mismatch", "message": "<text with the reason>"}`;
  transient: the same operation with the text `permission_mismatch_transient`.
- Stats: one dict per `batch_visible` call in `frappe.local.pcal_permission_stats` with the keys
  `permission_mode`, `reason`, `names`, `samples`, `mismatches`, `permission_refusal` (None or the refusal).
- Day counter classes: `permission_batch:<reason>`, `permission_mismatch`, `permission_mismatch_transient:<doctype>`,
  `breaker_trip`.
- `status()` returns `{"switch", "breakers": [{"doctype", "reason", "set_at"}], "latch_write_failed",
  "pin_drift", "identity_failures"}`; `reset_breaker(doctype=None)` deletes the key(s) and writes no config.

Faults are forced by wrapping `batch_permission._evaluate_batch` (an internal helper of this app); no pinned Frappe
callable is patched. `frappe.installer.update_site_config` is replaced by a recorder, so the real site config is
never written. The breaker keys use a private prefix and the day counters a private date.
"""

import json
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
import frappe.installer

from jarvis.payment_calendar import (
	batch_permission,
	builtin_reader,
	compatibility,
	jobs,
	permissions,
	read_log,
	routing,
	snapshot,
	sources,
	store,
)
from jarvis.payment_calendar.errors import CalendarSourceUnavailable
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.tests.batch_permission_fixtures import (
	HOOK_PATH,
	SI,
	BatchCase,
	clear_plan_memo,
	forget_local,
	hooks,
	oracle,
	session_as,
)
from jarvis.tests.payment_calendar_results import schema2_result

SWITCH = batch_permission.SWITCH
REAL_BREAKER_KEY = batch_permission.BREAKER_KEY
REAL_READ_OBLIGATIONS = builtin_reader.read_obligations  # `JobCase` replaces the attribute with a spy
PRIVATE_BREAKER = "jarvis:pcal:test-limits:permission_breaker:"
PRIVATE_DATE = "2031-07-01"
CUSTOMER = "Customer"
KEY = "k" * 44
SIDES = ("Payable", "Receivable")
DOCS = Path(__file__).resolve().parents[2] / "docs" / "architecture" / "payment-calendar.md"


def redis(*command):
	return frappe.cache.execute_command(*command)


class Recorder:
	"""Stands in for `frappe.installer.update_site_config`: records the call, keeps the process config in step."""

	def __init__(self, fail: bool = False):
		self.calls: list[tuple] = []
		self.fail = fail

	def __call__(self, key, value, *args, **kwargs):
		self.calls.append((key, value))
		if self.fail:
			raise OSError("site config is read-only")
		frappe.local.conf[key] = value


class Fault:
	"""`_evaluate_batch` with a forced wrong answer: it drops one visible name from the result.

	kind: persistent (every run), transient (the first run only), chunk (a run whose chunks hold 2+ names).
	`on_recheck` runs before the second call (the re-check), for the stop test.
	"""

	def __init__(self, kind: str, on_recheck=None):
		self.kind = kind
		self.real = batch_permission._evaluate_batch
		self.calls: list[dict] = []
		self.on_recheck = on_recheck

	def __call__(self, doctype, names, ptype, plan, *, chunks=None, fresh=False, **options):
		self.calls.append({"names": set(names), "chunks": chunks, "fresh": fresh, **options})
		if len(self.calls) == 2 and self.on_recheck:
			self.on_recheck()
		result = self.real(doctype, names, ptype, plan, chunks=chunks, fresh=fresh, **options)
		self.calls[-1]["result_chunks"] = result.chunks
		if self.kind == "add":  # the batch shows one name that the loop hides
			hidden = set(names) - result.visible
			return replace(result, visible=result.visible | {min(hidden)}) if hidden else result
		if result.visible and self.wrong(len(self.calls), result):
			return replace(result, visible=result.visible - {min(result.visible)})
		return result

	def wrong(self, number, result) -> bool:
		if self.kind == "transient":
			return number == 1
		if self.kind == "chunk":
			return any(len(chunk) >= 2 for chunk in result.chunks)
		return True

	def active(self):
		return patch.object(batch_permission, "_evaluate_batch", self)


@contextmanager
def fake_view():
	"""The marker of `snapshot.consistent_read` without its rollback (the fixtures are not committed).

	Inside, `snapshot.view_is_open` is true and `frappe.db.rollback` is the view's refusal; on a clean exit a
	broken view fails the job. Both markers are reset in `finally`, also when the body raises.
	"""
	token = snapshot._VIEW_BROKEN.set(False)
	open_token = snapshot._VIEW_OPEN.set(True)

	def refuse(*args, **kwargs):
		snapshot._VIEW_BROKEN.set(True)
		raise CalendarSourceUnavailable("source_changed")

	try:
		with patch.object(frappe.db, "rollback", refuse):
			yield
			if snapshot.view_is_broken():
				refuse()
	finally:
		snapshot._VIEW_OPEN.reset(open_token)
		snapshot._VIEW_BROKEN.reset(token)


class LimitsCase(BatchCase):
	def setUp(self):
		super().setUp()
		stack = ExitStack()
		self.addCleanup(stack.close)
		self.recorder = Recorder()
		self.counted: list[str] = []
		real_count = read_log.site_count

		def count(klass, date):
			self.counted.append(klass)
			return real_count(klass, PRIVATE_DATE)

		for patcher in (
			patch.object(batch_permission, "BREAKER_KEY", PRIVATE_BREAKER),
			patch.object(frappe.installer, "update_site_config", self.recorder),
			patch.object(read_log, "site_count", count),
			patch.dict(frappe.local.conf, {SWITCH: 1}),
		):
			stack.enter_context(patcher)
		# Outside a view an event is written at once: no test writes a real Error Log row.
		self.written = stack.enter_context(patch.object(read_log, "error_log_once"))
		forget_local()
		self.clean_keys()
		self.addCleanup(self.clean_keys)
		self.addCleanup(forget_local)

	def clean_keys(self):
		for pattern in (PRIVATE_BREAKER + "*", f"jarvis:pcal:site:day:{PRIVATE_DATE}:*"):
			for key in redis("KEYS", frappe.cache.make_key(pattern)):
				redis("DEL", key)

	# --- helpers ------------------------------------------------------------------------------
	def names(self, doctype=SI):
		f = self.f
		chosen = (
			[f.si_plain, f.si_twe_a, f.si_represents_b]
			if doctype == SI
			else [f.cust_plain, f.fx.customer, f.cust_party_account_b]
		)
		return set(chosen)

	def latch_key(self):
		return frappe.cache.make_key(PRIVATE_BREAKER + batch_permission.LATCH_FAILED)

	def key(self, doctype=SI):
		return frappe.cache.make_key(PRIVATE_BREAKER + doctype)

	def breaker_reason(self, doctype=SI):
		value = redis("GET", self.key(doctype))
		return None if value is None else value.decode().split("|", 1)[1]

	def events(self):
		return list(getattr(frappe.local, batch_permission.EVENTS, None) or [])

	def in_job(self, doctype, names=None, user=None):
		"""One `batch_visible` call of the user as a job makes it (the freeze map is the job's marker)."""
		if getattr(frappe.local, batch_permission.FREEZE, None) is None:
			batch_permission.begin_mode_freeze()
		with session_as(user or self.f.u1):
			return batch_permission.batch_visible(doctype, set(names or self.names(doctype)), "read")

	def refused(self, doctype=SI, names=None):
		with self.assertRaises(CalendarSourceUnavailable) as raised:
			self.in_job(doctype, names)
		self.assertEqual(raised.exception.code, "source_changed")

	def assert_no_names_in(self, value):
		text = repr(value)
		for name in (*self.f.names(SI), *self.f.names(CUSTOMER), self.f.u1, self.f.u3):
			self.assertNotIn(name, text)

	def assert_tripped(self, reason, doctype=SI, *, config=True):
		"""The breaker key (no TTL) and the flag; then the site config at 0 (`config=True`, the latch key gone) or
		its write deferred to the end of the job (`config="deferred"`: nothing written yet, the latch key set)."""
		self.assertEqual(redis("TTL", self.key(doctype)), -1, "the breaker key has no TTL")
		self.assertEqual(self.breaker_reason(doctype), reason)
		if config == "deferred":
			self.assertEqual(self.recorder.calls, [])
			self.assertEqual(redis("EXISTS", self.latch_key()), 1)
			self.assertTrue(getattr(frappe.local, batch_permission.CONFIG_PENDING, False))
		else:
			self.assertEqual(self.recorder.calls, [(SWITCH, 0)])
			self.assertEqual(redis("EXISTS", self.latch_key()), 0)
		self.assertTrue(getattr(frappe.local, batch_permission.TRIPPED, False))

	def assert_oracle_names(self, doctype=SI):
		with session_as(self.f.u1):
			self.assertEqual(oracle(doctype, self.names(doctype), "read"), self.names(doctype))


class TestBreakerNaming(LimitsCase):
	def test_the_key_and_the_switch_have_the_planned_names(self):
		self.assertEqual(REAL_BREAKER_KEY, "jarvis:pcal:permission_breaker:")
		self.assertEqual(SWITCH, "payment_calendar_batch_permission")
		self.assertEqual(batch_permission.TRIPPED, "pcal_permission_tripped")
		self.assertEqual(batch_permission.EVENTS, "pcal_permission_events")

	def test_a_trip_writes_the_key_without_a_ttl_and_with_the_reason(self):
		batch_permission._trip(SI, "mismatch")
		self.assert_tripped("mismatch")
		self.assertTrue(redis("GET", self.key()).decode().count("|") == 1)


class TestConfirmedMismatch(LimitsCase):
	"""A confirmed mismatch at the call level: the sample finds a mismatch that the same-input re-check repeats."""

	def test_a_persistent_mismatch_trips_poisons_and_refuses_without_a_transient_count(self):
		self.assert_oracle_names()
		fault = Fault("persistent")
		with fault.active():
			self.refused()
		self.assert_tripped("mismatch")
		self.assertEqual(len(fault.calls), 2, "the run and one re-check")
		self.assertFalse(
			[klass for klass in self.counted if klass.startswith("permission_mismatch_transient")]
		)
		self.assertIn("permission_mismatch", self.counted)
		self.assertIn("breaker_trip", self.counted)

	def test_it_queues_one_event_without_names(self):
		with Fault("persistent").active():
			self.refused()
		events = self.events()
		self.assertEqual([event["operation"] for event in events], ["pcal_permission_batch_mismatch"])
		self.assertIn("mismatch", events[0]["message"])
		self.assert_no_names_in(events)

	def test_the_trip_turns_the_batch_off_for_the_rest_of_the_job_for_other_doctypes(self):
		with Fault("persistent").active():
			self.refused()
		self.assert_oracle_names(CUSTOMER)
		with (
			patch.object(batch_permission, "_evaluate_batch") as batch,
			patch.object(
				permissions, "_visible_names_per_doc", wraps=permissions._visible_names_per_doc
			) as loop,
		):
			got = self.in_job(CUSTOMER)
		batch.assert_not_called()
		self.assertEqual(loop.call_count, 1)
		self.assertEqual(got, self.names(CUSTOMER))

	def test_the_tripped_doctype_is_refused_by_the_job_freeze(self):
		with Fault("persistent").active():
			self.refused()
		self.refused()


class TestTransient(LimitsCase):
	"""A transient mismatch (the re-check does not repeat it) and the limit per doctype and site day."""

	def test_a_mismatch_that_the_re_check_does_not_repeat_is_transient(self):
		self.assert_oracle_names()
		with Fault("transient").active():
			self.refused()
		self.assertIsNone(redis("GET", self.key()), "no breaker key")
		self.assertEqual(self.recorder.calls, [], "no site-config write")
		self.assertFalse(getattr(frappe.local, batch_permission.TRIPPED, False))
		self.assertEqual(self.counted.count("permission_mismatch_transient:" + SI), 1)
		self.assertEqual(
			redis(
				"GET",
				frappe.cache.make_key(
					f"jarvis:pcal:site:day:{PRIVATE_DATE}:permission_mismatch_transient:{SI}"
				),
			),
			b"1",
		)
		events = self.events()
		self.assertEqual([event["operation"] for event in events], ["pcal_permission_batch_mismatch"])
		self.assertIn("permission_mismatch_transient", events[0]["message"])
		self.assert_no_names_in(events)

	def test_the_second_transient_of_a_doctype_in_one_day_trips(self):
		with Fault("transient").active():
			self.refused()
		self.assertIsNone(redis("GET", self.key()))
		with Fault("transient").active():
			self.refused()
		self.assert_tripped("transient_limit")

	def test_a_transient_of_another_doctype_does_not_count_against_the_first(self):
		self.assert_oracle_names(CUSTOMER)
		with Fault("transient").active():
			self.refused()
		with Fault("transient").active():
			self.refused(CUSTOMER)
		self.assertIsNone(redis("GET", self.key(SI)))
		self.assertIsNone(redis("GET", self.key(CUSTOMER)))
		self.assertEqual(self.recorder.calls, [])

	def test_a_redis_error_on_the_transient_counter_trips(self):
		def broken(klass, date):
			raise ConnectionError("down")

		with Fault("transient").active(), patch.object(read_log, "site_count", broken):
			self.refused()
		self.assert_tripped("transient_unknown")


class TestChunkOnlyFault(LimitsCase):
	"""A chunk-only fault: a fault that needs two names in one chunk repeats in the re-check over the same chunks."""

	def test_the_re_check_repeats_it_with_the_same_chunks_and_fresh_memos(self):
		self.assert_oracle_names()
		fault = Fault("chunk")
		with patch.object(batch_permission, "CHUNK", 2), fault.active():
			self.refused()
		self.assert_tripped("mismatch")
		self.assertEqual([c["fresh"] for c in fault.calls], [False, True])
		first, again = fault.calls
		self.assertEqual(again["names"], first["names"])
		self.assertEqual(again["chunks"], first["result_chunks"])
		self.assertTrue(any(len(chunk) >= 2 for chunk in again["chunks"]))
		self.assertFalse(
			[klass for klass in self.counted if klass.startswith("permission_mismatch_transient")]
		)

	def test_one_name_alone_would_pass(self):
		fault = Fault("chunk")
		one = {min(self.names())}
		with session_as(self.f.u1), patch.object(batch_permission, "CHUNK", 2):
			plan = batch_permission.batch_plan(SI)
			alone = fault(SI, set(one), "read", plan)
		self.assertEqual(alone.visible, one)


class TestStopDuringTheRecheck(LimitsCase):
	"""A stop during the re-check at the call level: the stop fires in the re-check, so the mismatch is confirmed unresolved."""

	def test_the_stop_error_propagates_after_a_trip_with_the_unresolved_reason(self):
		offset = [0.0]
		stop = monotonic() + 100

		def late():
			offset[0] = 1000.0

		fault = Fault("persistent", on_recheck=late)
		with (
			patch.object(batch_permission, "monotonic", lambda: monotonic() + offset[0]),
			batch_permission.permission_stop(stop, TimeoutError),
			fault.active(),
			self.assertRaises(TimeoutError),
		):
			self.in_job(SI)
		self.assert_tripped("mismatch_unresolved", config="deferred")
		batch_permission.write_pending_config()  # what the job does last
		self.assert_tripped("mismatch_unresolved")
		self.assertFalse(
			[klass for klass in self.counted if klass.startswith("permission_mismatch_transient")]
		)
		# The stop had passed, so the config write was skipped (an event of its own) before the mismatch event.
		self.assertEqual(
			[event["operation"] for event in self.events()],
			["pcal_permission_breaker", "pcal_permission_batch_mismatch"],
		)


class TestReadsHaveNoSample(LimitsCase):
	"""A read request makes no sample call."""

	def test_a_read_request_runs_the_batch_once_and_never_the_sample(self):
		self.assert_oracle_names()
		with (
			patch.object(batch_permission, "_sample_oracle") as sample,
			Fault("persistent").active(),
			session_as(self.f.u1),
		):
			batch_permission.batch_visible(SI, set(self.names()), "read")
		sample.assert_not_called()
		self.assertIsNone(redis("GET", self.key()))
		self.assertEqual(self.recorder.calls, [])

	def test_a_job_does_call_the_sample(self):
		with patch.object(batch_permission, "_sample_oracle") as sample:
			self.in_job(SI)
		self.assertEqual(sample.call_count, 1)


class TestBreakerDurability(LimitsCase):
	"""The breaker survives a Redis eviction and fails closed on a Redis error."""

	def tripped_in_another_process(self):
		batch_permission._trip(SI, "mismatch")
		delattr(frappe.local, batch_permission.TRIPPED)  # the flag lives in the tripping process only
		forget_local()

	def test_a_deleted_redis_key_leaves_the_batch_off_until_the_switch_is_set(self):
		self.assert_oracle_names()
		self.tripped_in_another_process()
		redis("DEL", self.key())
		self.assertEqual(batch_permission._live_gate(SI), "switch_off")
		with patch.object(batch_permission, "_evaluate_batch") as batch, session_as(self.f.u1):
			got = batch_permission.batch_visible(SI, set(self.names()), "read")
		batch.assert_not_called()
		self.assertEqual(got, self.names())
		frappe.local.conf[SWITCH] = 1
		self.assertIsNone(batch_permission._live_gate(SI))

	def test_a_redis_error_on_the_breaker_read_takes_the_loop(self):
		self.assert_oracle_names()
		real = frappe.cache.execute_command

		def command(*args, **kwargs):
			if args and args[0] == "EXISTS":
				raise ConnectionError("down")
			return real(*args, **kwargs)

		with (
			patch.object(frappe.cache, "execute_command", command),
			patch.object(batch_permission, "_evaluate_batch") as batch,
			session_as(self.f.u1),
		):
			got = batch_permission.batch_visible(SI, set(self.names()), "read")
		batch.assert_not_called()
		self.assertEqual(got, self.names())
		self.assertEqual(frappe.local.pcal_permission_stats[-1]["reason"], "breaker_unknown")

	def test_a_failed_site_config_write_queues_an_event_and_keeps_the_redis_key(self):
		self.recorder.fail = True
		batch_permission._trip(SI, "mismatch")
		self.assertEqual(self.breaker_reason(), "mismatch")
		self.assertTrue(getattr(frappe.local, batch_permission.TRIPPED, False))
		events = self.events()
		self.assertEqual([event["operation"] for event in events], ["pcal_permission_breaker"])
		self.assertIn("breaker_write_failed:site_config", events[0]["message"])
		self.assertTrue(batch_permission.status()["latch_write_failed"])

	def test_a_failed_redis_write_queues_an_event_and_the_other_steps_run(self):
		with patch.object(batch_permission, "_set_breaker_key", side_effect=ConnectionError("down")):
			batch_permission._trip(SI, "mismatch")
		self.assertEqual(self.recorder.calls, [(SWITCH, 0)])
		self.assertTrue(getattr(frappe.local, batch_permission.TRIPPED, False))
		self.assertIn("breaker_write_failed:breaker_key", self.events()[0]["message"])

	def test_the_steps_run_in_the_planned_order(self):
		order = []
		real = batch_permission._set_breaker_key
		flag = lambda: getattr(frappe.local, batch_permission.TRIPPED, False)  # noqa: E731

		def key_step(*args):
			order.append(("key", flag()))
			return real(*args)

		def config_step(key, value, *args, **kwargs):
			order.append(("config", flag()))

		with (
			patch.object(batch_permission, "_set_breaker_key", key_step),
			patch.object(frappe.installer, "update_site_config", config_step),
		):
			batch_permission._trip(SI, "mismatch")
		self.assertEqual(order, [("key", False), ("config", False)])
		self.assertTrue(flag())

	def test_status_shows_each_state_without_names(self):
		clean = batch_permission.status()
		self.assertEqual(
			set(clean),
			{
				"switch",
				"breakers",
				"latch_write_failed",
				"pin_drift",
				"identity_failures",
				"counters",
				"plans",
			},
		)
		self.assertTrue(clean["switch"])
		self.assertEqual((clean["breakers"], clean["latch_write_failed"]), ([], False))
		batch_permission._trip(SI, "transient_limit")
		tripped = batch_permission.status()
		self.assertFalse(tripped["switch"])
		self.assertEqual(
			[(b["doctype"], b["reason"]) for b in tripped["breakers"]], [(SI, "transient_limit")]
		)
		self.assertTrue(tripped["breakers"][0]["set_at"])
		with patch.object(compatibility, "permission_pin_drift", lambda: ["Some.pin"]):
			self.assertEqual(batch_permission.status()["pin_drift"], ["Some.pin"])
		self.assert_no_names_in(tripped)

	def test_reset_deletes_the_key_and_never_turns_the_switch_on(self):
		batch_permission._trip(SI, "mismatch")
		batch_permission._trip(CUSTOMER, "mismatch")
		batch_permission.reset_breaker(SI)
		self.assertIsNone(redis("GET", self.key(SI)))
		self.assertIsNotNone(redis("GET", self.key(CUSTOMER)))
		batch_permission.reset_breaker()
		self.assertIsNone(redis("GET", self.key(CUSTOMER)))
		self.assertEqual(self.recorder.calls, [(SWITCH, 0)] * 2)
		self.assertEqual(batch_permission.status()["breakers"], [])
		self.assertFalse(frappe.local.conf.get(SWITCH))


class TestStats(LimitsCase):
	"""The stats of a call (the reader record and the log line carry these counts, tested below)."""

	def stats(self):
		return list(getattr(frappe.local, batch_permission.STATS, None) or [])

	def test_a_batch_call_records_the_mode_and_reason_counts_without_names(self):
		self.in_job(SI)
		(entry,) = self.stats()
		self.assertEqual((entry["permission_mode"], entry["reason"], entry["names"]), ("batch", None, 3))
		self.assertGreaterEqual(entry["samples"], 1)
		self.assertEqual(entry["mismatches"], 0)
		self.assert_no_names_in(entry)

	def test_a_mismatch_records_the_refusal_and_writes_the_day_counters_and_one_event(self):
		with Fault("persistent").active():
			self.refused()
		entry = self.stats()[-1]
		self.assertEqual((entry["permission_refusal"], entry["mismatches"] >= 1), ("mismatch", True))
		self.assertTrue({"permission_mismatch", "breaker_trip"} <= set(self.counted))
		self.assertEqual(len(self.events()), 1)

	def test_a_pin_drift_writes_a_day_counter_and_one_event_for_many_calls(self):
		batch_permission.begin_mode_freeze()
		with (
			patch.object(compatibility, "permission_pin_drift", lambda: ["Some.pin"]),
			session_as(self.f.u1),
		):
			for _ in range(2):
				batch_permission.batch_visible(SI, set(self.names()), "read")
		self.assertEqual(self.counted.count("permission_batch:pin_drift"), 2)
		drift = [event for event in self.events() if event["operation"] == "pcal_permission_pin_drift"]
		self.assertEqual(len(drift), 1, "one event for the plan, not one per call")
		self.assert_no_names_in(drift)

	def test_the_runbook_rows_exist(self):
		text = DOCS.read_text()
		for needle in (
			SWITCH,
			"reset_breaker",
			"batch_permission.status",
			"differential",
			"pin_drift",
			"breaker",
		):
			self.assertIn(needle, text)


class JobCase(LimitsCase):
	"""`jobs.run_snapshot` as the Company-UP user, with the router's reader eligible for both sides."""

	def setUp(self):
		super().setUp()
		stack = ExitStack()
		self.addCleanup(stack.close)
		frappe.set_user(self.f.u1)
		self.addCleanup(frappe.set_user, "Administrator")
		self.log_error = stack.enter_context(patch.object(frappe, "log_error"))
		self.error_log = stack.enter_context(patch.object(read_log, "error_log_once"))
		stack.enter_context(patch.object(jobs, "get_job", return_value=None))
		stack.enter_context(patch.object(frappe, "enqueue", return_value=object()))
		stack.enter_context(patch.object(jobs, "authorize_scope", return_value={"fingerprint": "fp"}))
		stack.enter_context(patch.dict(frappe.local.conf, {"encryption_key": KEY}))
		settings = routing.ReaderSettings("on", (self.f.a,), ())
		stack.enter_context(patch.object(routing, "reader_settings", return_value=settings))
		self.reader = stack.enter_context(
			patch.object(routing.builtin_reader, "read_obligations", return_value=None)
		)
		self.clean_routing()
		self.addCleanup(self.clean_routing)
		self.addCleanup(store.purge_actor, self.f.u1)
		self.outcomes = []
		self.view_open_at_event = []
		self.error_log.side_effect = lambda *a, **k: self.view_open_at_event.append(snapshot.view_is_open())

	def clean_routing(self):
		for number in range(routing.READER_SLOTS):
			redis("DEL", routing.slot_key(number))
		for side in SIDES:
			redis("DEL", routing.memo_key(self.f.a, side))
			redis("DEL", routing.inflight_key(self.f.a, side))

	def run_job(self, today_read):
		"""Run the job; collect is the router over both sides inside a view, with `today_read` for today's read."""
		frappe.set_user(self.f.u1)  # a helper such as `session_as` leaves the session as Administrator
		store.purge_actor(self.f.u1)
		sid = jobs.request_snapshot(normalize_scope(self.f.a, []))["snapshot_id"]

		def collect(scope, fingerprint):
			with fake_view():
				for side in SIDES:
					try:
						routing.read_side(
							today_read,
							scope["company"],
							side,
							scope["anchor"],
							scope["window_start"],
							scope["window_end"],
							started=monotonic(),
							outcomes=self.outcomes,
						)
					except CalendarSourceUnavailable:
						pass  # as `snapshot._collect`: the side is recorded unavailable and the next side runs
			return schema2_result()

		with patch.object(jobs, "collect", side_effect=collect):
			jobs.run_snapshot(actor=self.f.u1, snapshot_id=sid)
		return store.load_status(self.f.u1, sid)

	def too_large_after(self, *steps):
		"""Today's read: the batch calls of `steps`, then a `too_large` refusal (which routes to the reader)."""

		def today_read(company, side, as_of, window_start, window_end):
			for step in steps:
				step(side)
			raise CalendarSourceUnavailable("too_large")

		return today_read

	def batch_call(self, doctype=SI):
		return lambda side: batch_permission.batch_visible(doctype, set(self.names(doctype)), "read")

	def steps(self):
		return [record.get("step") for record in self.outcomes]


class TestStatsWiring(JobCase):
	"""The reader's `counts` and the job's log line carry the permission counts."""

	def read_with_stats(self, deadline=None):
		"""`builtin_reader.read_obligations` with the permission filter standing in for `sources._assemble`."""
		counts = {}
		engine = SimpleNamespace(reader_stats={"counts": counts}, data=[], excluded=[], terms={})
		stats = {"counts": counts}
		names = self.names()
		with (
			patch.object(sources, "_require_inputs", return_value=(None, "Payable", "s", "a", {})),
			patch.object(builtin_reader, "validate_obligation_books"),
			patch.object(sources, "_run_engine", return_value=engine),
			patch.object(
				sources,
				"_assemble",
				side_effect=lambda *a: batch_permission.batch_visible(SI, set(names), "read"),
			),
		):
			batch_permission.begin_mode_freeze()
			with session_as(self.f.u1):
				REAL_READ_OBLIGATIONS("c", "Payable", "a", "s", "e", stats=stats, deadline=deadline)
		return counts

	def test_the_reader_counts_hold_the_mode_and_reason_counts_without_names(self):
		counts = self.read_with_stats()
		self.assertEqual(counts["permission_mode_batch"], 1)
		self.assertEqual(counts["permission_names"], 3)
		self.assertGreaterEqual(counts["permission_samples"], 1)
		self.assert_no_names_in(counts)

	def test_a_per_document_reason_is_counted_by_name(self):
		with patch.object(compatibility, "permission_pin_drift", lambda: ["Some.pin"]):
			counts = self.read_with_stats()
		self.assertEqual(counts["permission_mode_per_doc"], 1)
		self.assertEqual(counts["permission_reason_pin_drift"], 1)

	def test_the_counts_cover_only_the_reads_own_calls(self):
		batch_permission.begin_mode_freeze()
		self.in_job(SI)
		counts = self.read_with_stats()
		self.assertEqual(counts["permission_calls"], 1)

	def test_the_job_writes_one_info_line_with_the_counts_and_no_names(self):
		lines = []
		logger = MagicMock()
		logger.info.side_effect = lines.append
		with (
			patch.object(compatibility, "permission_pin_drift", lambda: ["Some.pin"]),
			patch.object(frappe, "logger", return_value=logger),
		):
			self.run_job(self.too_large_after(self.batch_call()))
		(line,) = [text for text in lines if str(text).startswith("permission_batch ")]
		self.assertIn("permission_mode_per_doc=", line)
		self.assertIn("permission_reason_pin_drift=", line)
		self.assert_no_names_in(line)


class TestJobConfirmedMismatch(JobCase):
	"""A confirmed mismatch in a job."""

	def test_without_a_fault_the_reader_is_eligible_for_both_sides(self):
		self.assert_oracle_names()
		status = self.run_job(self.too_large_after(self.batch_call()))
		self.assertEqual(self.reader.call_count, 2)
		self.assertEqual(status["state"], "complete")

	def test_a_persistent_mismatch_in_collect_never_reaches_the_reader(self):
		self.assert_oracle_names()
		with Fault("persistent").active():
			status = self.run_job(self.too_large_after(self.batch_call()))
		self.assertEqual((status["state"], status["code"]), ("failed", "source_changed"))
		self.reader.assert_not_called()
		self.assertEqual(self.outcomes[0].get("refusal"), "source_changed")
		self.assertEqual(self.steps()[1:], ["view_broken"])
		self.assertNotIn("budget", self.steps())
		self.assertEqual(frappe.local.pcal_permission_stats[-1]["permission_refusal"], "mismatch")
		self.assert_tripped("mismatch")

	def test_one_event_is_written_after_the_view_without_names(self):
		with Fault("persistent").active():
			self.run_job(self.too_large_after(self.batch_call()))
		self.assertEqual(self.error_log.call_count, 1)
		args = self.error_log.call_args.args
		self.assertTrue(args[2].startswith("pcal_permission_batch_mismatch"))
		self.assert_no_names_in(args)
		self.assertEqual(self.view_open_at_event, [False])
		self.assertEqual(self.events(), [])

	def test_a_mismatch_inside_the_reader_returns_none_and_refuses_the_other_side(self):
		self.reader.side_effect = lambda *a, **k: batch_permission.batch_visible(SI, self.names(), "read")
		with Fault("persistent").active():
			status = self.run_job(self.too_large_after())
		self.assertEqual((status["state"], status["code"]), ("failed", "source_changed"))
		self.assertEqual(self.reader.call_count, 1, "the other side is not read")
		self.assertEqual(self.outcomes[0].get("reader_refused"), "source_changed")
		self.assertEqual(self.steps()[1], "view_broken")
		self.assert_tripped("mismatch")


class TestJobStopInTheRecheck(JobCase):
	"""A stop during the re-check in a job."""

	def test_the_job_fails_as_a_timeout_and_the_breaker_says_unresolved(self):
		offset = [0.0]

		def late():
			offset[0] = jobs.JOB_PERMISSION_STOP_S + 100.0

		with (
			patch.object(batch_permission, "monotonic", lambda: monotonic() + offset[0]),
			Fault("persistent", on_recheck=late).active(),
		):
			status = self.run_job(self.too_large_after(self.batch_call()))
		self.assertEqual((status["state"], status["code"]), ("failed", "timeout"))
		self.assert_tripped("mismatch_unresolved")  # the deferred write was done by the job's `finally`
		self.reader.assert_not_called()
		self.assertEqual(self.error_log.call_count, 2, "the deferred config write and the mismatch")

	def test_a_failed_deferred_write_keeps_the_latch_and_writes_the_event(self):
		self.recorder.fail = True
		offset = [0.0]

		def late():
			offset[0] = jobs.JOB_PERMISSION_STOP_S + 100.0

		with (
			patch.object(batch_permission, "monotonic", lambda: monotonic() + offset[0]),
			Fault("persistent", on_recheck=late).active(),
		):
			self.run_job(self.too_large_after(self.batch_call()))
		self.assertEqual(self.recorder.calls, [(SWITCH, 0)], "the write was tried once")
		self.assertEqual(redis("EXISTS", self.latch_key()), 1, "the latch stays: the config still reads 1")
		messages = [call.args[1] for call in self.error_log.call_args_list]
		self.assertIn("breaker_write_deferred:site_config", messages)
		self.assertIn("breaker_write_failed:site_config", messages)
		forget_local()  # a later process: only the latch key stops the other doctypes
		self.assertEqual(batch_permission._live_gate(CUSTOMER), "breaker")


class FakeClock:
	"""A monotonic clock that only moves when the test moves it: no real seconds pass."""

	def __init__(self, now: float = 5000.0):
		self.now = now

	def __call__(self) -> float:
		return self.now


class TestJobStop(JobCase):
	"""A forced per-document path in collect is stopped by the job stop, on a patched clock."""

	NAMES = 20_000
	CHECK_S = 0.030
	RQ_TIMEOUT_S = 300

	def setUp(self):
		super().setUp()
		self.clock = FakeClock()
		self.checks = 0
		stack = ExitStack()
		self.addCleanup(stack.close)
		stack.enter_context(patch.object(jobs, "monotonic", self.clock))
		stack.enter_context(patch.object(batch_permission, "monotonic", self.clock))

	def per_document_collect(self, names=NAMES):
		"""Today's read on the per-document path: each of `names` costs 30 ms of the patched clock and one check."""

		def today_read(company, side, as_of, window_start, window_end):
			for _ in range(names):
				self.clock.now += self.CHECK_S
				self.checks += 1
				batch_permission.check_permission_stop()
			raise CalendarSourceUnavailable("too_large")

		return today_read

	def test_the_job_fails_as_a_timeout_within_the_stop_and_the_reader_is_not_called(self):
		started = self.clock.now
		status = self.run_job(self.per_document_collect())
		elapsed = self.clock.now - started
		self.assertEqual((status["state"], status["code"]), ("failed", "timeout"))
		self.assertGreaterEqual(elapsed, jobs.JOB_PERMISSION_STOP_S)
		self.assertLessEqual(elapsed, jobs.JOB_PERMISSION_STOP_S + 5)
		self.assertLess(self.checks, self.NAMES, "the stop ended the loop")
		self.reader.assert_not_called()

	def test_the_job_ends_itself_before_the_rq_timeout_so_the_worker_is_not_killed(self):
		started = self.clock.now
		status = self.run_job(self.per_document_collect())  # returns: no exception reached the worker
		self.assertLess(self.clock.now - started, self.RQ_TIMEOUT_S)
		self.assertEqual(status["code"], "timeout")
		self.assertNotEqual(status["code"], "internal_error")

	def test_the_next_job_in_the_process_has_no_stop(self):
		self.run_job(self.per_document_collect())
		batch_permission.check_permission_stop()
		self.clock.now += 10 * jobs.JOB_PERMISSION_STOP_S  # long past the first job's stop
		batch_permission.check_permission_stop()
		self.checks = 0
		second = self.run_job(self.per_document_collect(100))
		self.assertEqual(second["state"], "complete")
		self.assertEqual(self.checks, 200, "100 names on each of the two sides")

	def test_the_stop_is_measured_from_the_job_start_not_from_the_first_permission_call(self):
		started = self.clock.now

		def late_start(company, side, as_of, window_start, window_end):
			self.clock.now = started + jobs.JOB_PERMISSION_STOP_S - 1
			batch_permission.check_permission_stop()  # still inside
			self.clock.now = started + jobs.JOB_PERMISSION_STOP_S + 1
			batch_permission.check_permission_stop()  # past it

		status = self.run_job(late_start)
		self.assertEqual((status["state"], status["code"]), ("failed", "timeout"))


class TestJobModeChange(JobCase):
	"""A mode change in a job."""

	def test_a_breaker_key_set_by_another_process_refuses_the_job_at_the_next_call(self):
		self.assert_oracle_names()

		def other_process(side):
			redis("SET", self.key(), "now|mismatch")

		with patch.object(
			batch_permission, "_evaluate_batch", wraps=batch_permission._evaluate_batch
		) as batch:
			status = self.run_job(self.too_large_after(self.batch_call(), other_process, self.batch_call()))
		self.assertEqual((status["state"], status["code"]), ("failed", "source_changed"))
		self.assertEqual(batch.call_count, 1, "only the call before the change ran the batch")
		self.reader.assert_not_called()
		self.assertEqual(self.steps()[1:], ["view_broken"])
		self.assertEqual(self.recorder.calls, [], "a mode change is not a trip")

	def test_a_redis_error_on_the_breaker_read_after_a_batch_call_refuses_the_job(self):
		real = frappe.cache.execute_command
		failing = {"on": False}

		def command(*args, **kwargs):
			if failing["on"] and args and args[0] == "EXISTS":
				raise ConnectionError("down")
			return real(*args, **kwargs)

		def go_down(side):
			failing["on"] = True

		with patch.object(frappe.cache, "execute_command", command):
			status = self.run_job(self.too_large_after(self.batch_call(), go_down, self.batch_call()))
		self.assertEqual((status["state"], status["code"]), ("failed", "source_changed"))
		self.reader.assert_not_called()


class TestTripFlagLifetime(JobCase):
	"""The trip flag lives for one job. `frappe.destroy` is stood in for by `forget_local()` (it would drop the test connection)."""

	def test_after_the_trip_another_doctype_takes_the_loop_in_the_same_job(self):
		self.assert_oracle_names(CUSTOMER)
		with Fault("persistent").active():
			self.refused()
		with patch.object(batch_permission, "_evaluate_batch") as batch:
			got = self.in_job(CUSTOMER)
		batch.assert_not_called()
		self.assertEqual(got, self.names(CUSTOMER))
		self.assertEqual(frappe.local.pcal_permission_stats[-1]["permission_mode"], "per_doc")

	def test_the_next_job_in_the_same_process_runs_in_batch_mode_after_the_reset(self):
		self.assert_oracle_names()
		with Fault("persistent").active():
			first = self.run_job(self.too_large_after(self.batch_call()))
		self.assertEqual(first["code"], "source_changed")
		self.assertTrue(frappe.local.pcal_permission_tripped)
		batch_permission.reset_breaker()
		frappe.local.conf[SWITCH] = 1  # `bench set-config payment_calendar_batch_permission 1`
		forget_local()  # the end of job 1: `frappe.destroy` clears `frappe.local`
		with patch.object(
			batch_permission, "_evaluate_batch", wraps=batch_permission._evaluate_batch
		) as batch:
			second = self.run_job(self.too_large_after(self.batch_call()))
		self.assertEqual(second["state"], "complete")
		self.assertGreaterEqual(batch.call_count, 1)
		self.assertEqual(frappe.local.pcal_permission_stats[-1]["permission_mode"], "batch")
		self.assertFalse(getattr(frappe.local, batch_permission.TRIPPED, False))


class TestShimContract(LimitsCase):
	"""Plan 5.2: the stand-in answers exactly or raises `ShimMiss`; `None` only where a real document holds none."""

	def doc(self):
		layout = batch_permission._Layout(SI, {"Company"})
		values = {"name": "X", "owner": "o", "company": "C"}
		return batch_permission._PermDoc(layout, values, [])

	def assert_shim_miss(self, *args, **kwargs):
		with self.assertRaises(batch_permission.ShimMiss):
			self.doc().get(*args, **kwargs)

	def test_a_loaded_column_the_doctype_and_the_name_are_answered(self):
		doc = self.doc()
		self.assertEqual((doc.get("name"), doc.get("owner"), doc.get("company")), ("X", "o", "C"))
		self.assertEqual(doc.get("doctype"), SI)

	def test_none_is_answered_for_islocal_and_the_parentfield_of_a_parent(self):
		doc = self.doc()
		self.assertIsNone(doc.get("__islocal"))
		self.assertIsNone(doc.get("parentfield"))

	def test_a_table_a_column_that_was_not_read_and_any_other_key_raise(self):
		for key in ("items", "taxes", "grand_total", "parent", "no_such_key_zz", "docstatus"):
			with self.subTest(key=key):
				self.assert_shim_miss(key)

	def test_extra_arguments_and_a_non_string_key_raise(self):
		self.assert_shim_miss("company", default=0)
		self.assert_shim_miss("company", filters={})
		self.assert_shim_miss(None)

	def test_a_link_field_without_a_column_is_none_and_one_with_a_column_is_not(self):
		meta = frappe.get_meta(SI)
		self.assertIn("customer", batch_permission._null_keys(meta, {}, set()))
		self.assertNotIn(
			"customer", batch_permission._null_keys(meta, {"customer": ("varchar", None)}, set())
		)

	def test_a_child_row_answers_its_parentfield_as_a_value_and_raises_for_unread_columns(self):
		table = next(t for t in batch_permission._Layout(SI, {"Company"}).tables if t.relevant)
		row = batch_permission._PermRow(table, {"idx": 1, "parentfield": table.fieldname, "company": "B"})
		self.assertEqual(row.get("parentfield"), table.fieldname)
		self.assertIsNone(row.get("__islocal"))
		with self.assertRaises(batch_permission.ShimMiss):
			row.get("parent")


class TestPlanFailsClosed(LimitsCase):
	"""A broken table link or a missing `restrict_to_companies` column goes per document."""

	def plan(self, doctype=SI):
		with session_as(self.f.u1):
			return batch_permission.batch_plan(doctype)

	def test_a_table_link_to_a_missing_doctype_is_no_table_not_an_error(self):
		with patch.object(batch_permission, "_Table", side_effect=frappe.DoesNotExistError("gone")):
			plan = self.plan()
		self.assertEqual((plan.mode, plan.reason), ("per_doc", "no_table"))

	def test_a_missing_restrict_to_companies_column_is_no_column(self):
		real = batch_permission._table_columns

		def without(table):
			return {name: value for name, value in real(table).items() if name != "restrict_to_companies"}

		with patch.object(batch_permission, "_table_columns", without):
			plans = {doctype: self.plan(doctype) for doctype in (SI, CUSTOMER)}
		for doctype, plan in plans.items():
			with self.subTest(doctype=doctype):
				self.assertEqual((plan.mode, plan.reason), ("per_doc", "no_column"))

	def test_the_result_is_still_the_oracle(self):
		with patch.object(batch_permission, "_Table", side_effect=frappe.DoesNotExistError("gone")):
			with session_as(self.f.u1):
				got = batch_permission.batch_visible(SI, set(self.f.names(SI)), "read")
				self.assertEqual(got, oracle(SI, self.f.names(SI), "read"))


class TestLazyClassRows(LimitsCase):
	"""The rows of the lazy class come from `compatibility`, not from a string list here."""

	def reason(self, failure):
		with (
			patch.object(compatibility, "permission_identity_failures", lambda *a: [failure]),
			session_as(self.f.u1),
		):
			return batch_permission.batch_plan(SI).reason

	def test_the_constant_is_derived_from_the_lookup_rows(self):
		rows = {r.row for r in compatibility.PERMISSION_LOOKUP if r.binding == "<descriptors>"}
		self.assertTrue(rows <= compatibility.LAZY_CLASS_ROWS)
		self.assertEqual(compatibility.LAZY_CLASS_ROWS, {"36", "37"})

	def test_a_failure_of_a_listed_row_is_a_controller_override_and_another_is_entry_patched(self):
		self.assertEqual(self.reason("37:get"), "controller_override")
		self.assertEqual(self.reason("5"), "entry_patched")

	def test_the_planner_follows_the_constant(self):
		with patch.object(compatibility, "LAZY_CLASS_ROWS", frozenset({"99"})):
			self.assertEqual(self.reason("99:get"), "controller_override")
			self.assertEqual(self.reason("37:get"), "entry_patched")


class TestPlanHooks(LimitsCase):
	def test_real_hooks_lists_the_hooks_that_run_on_a_lazy_document(self):
		path = HOOK_PATH + "deny_listed"
		clear_plan_memo()
		with hooks({SI: [path]}), session_as(self.f.u1):
			plan = batch_permission.batch_plan(SI)
		self.assertEqual((plan.mode, plan.real_hooks), ("batch", (path,)))

	def test_a_known_hook_is_not_a_real_hook(self):
		with session_as(self.f.u1):
			plan = batch_permission.batch_plan(SI)
		self.assertNotIn(batch_permission._INHERITED_HOOK, plan.real_hooks)


class TestHotPathCost(LimitsCase):
	"""The branch counting calls (extra `has_user_permission` calls) stay off the hot path."""

	def test_batch_visible_asks_for_no_branch_counting_and_the_result_is_the_same(self):
		fault = []
		real = batch_permission._evaluate_batch

		def spy(*args, **kwargs):
			fault.append(kwargs)
			return real(*args, **kwargs)

		counting = patch.object(
			batch_permission._Run,
			"_count_user_permission_denial",
			autospec=True,
			side_effect=batch_permission._Run._count_user_permission_denial,
		)
		names = set(self.f.names(SI))
		with counting as count, patch.object(batch_permission, "_evaluate_batch", spy), session_as(self.f.u1):
			got = batch_permission.batch_visible(SI, set(names), "read")
		with session_as(self.f.u1):
			self.assertEqual(got, oracle(SI, names, "read"))
		self.assertEqual(count.call_count, 0)
		self.assertEqual(fault[0].get("count_branches"), False)

	def test_the_default_keeps_the_branch_counts_for_the_tests_and_the_differential(self):
		names = {self.f.si_twe_b, self.f.si_plain}
		with session_as(self.f.u1):
			plan = batch_permission.batch_plan(SI)
			result = batch_permission._evaluate_batch(SI, names, "read", plan)
			lean = batch_permission._evaluate_batch(SI, names, "read", plan, count_branches=False)
		self.assertGreaterEqual(result.branches["child_deny"], 1)
		self.assertEqual(lean.branches.get("child_deny", 0), 0)
		self.assertEqual(lean.visible, result.visible)
		self.assertIn(self.f.si_twe_b, lean.flagged, "a denial with child rows is still a sample candidate")


MODULES = ("batch_permission", "batch_permission_plan", "batch_permission_eval")


class TestModuleSplit(LimitsCase):
	"""The batch lives in three modules; each one is a figure module and none imports a Frappe name."""

	def sources(self):
		import importlib
		import inspect

		return {
			name: inspect.getsource(importlib.import_module("jarvis.payment_calendar." + name))
			for name in MODULES
		}

	def test_every_module_is_a_figure_module(self):
		for name in MODULES:
			with self.subTest(module=name):
				self.assertIn(name, routing.FIGURE_MODULES)
				self.assertNotIn(name, routing.OTHER_MODULES)

	def test_no_module_imports_a_frappe_callable_by_name(self):
		import ast

		for name, source in self.sources().items():
			imported = [
				node.module
				for node in ast.walk(ast.parse(source))
				if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("frappe")
			]
			with self.subTest(module=name):
				self.assertEqual(imported, [])

	def test_hooks_are_dispatched_only_through_frappe_call(self):
		joined = "\n".join(self.sources().values())
		self.assertEqual(joined.count("frappe.call("), 1)
		self.assertNotIn("get_attr(method", joined)

	def test_the_public_names_stay_importable_from_the_entry_module(self):
		for name in (
			"batch_visible", "batch_plan", "status", "reset_breaker", "differential", "check_permission_stop",
			"permission_stop", "switch_enabled", "begin_mode_freeze", "queue_event", "BatchPlan", "EvalResult",
			"stats_mark", "stats_summary", "stats_line",
		):  # fmt: skip
			with self.subTest(name=name):
				self.assertTrue(hasattr(batch_permission, name))

	def test_a_name_rebound_on_the_entry_module_reaches_the_evaluator_and_the_plan(self):
		names = {self.f.si_twe_b, self.f.si_plain}
		with session_as(self.f.u1):
			plan = batch_permission.batch_plan(SI)
			with patch.object(batch_permission, "CHUNK", 1):
				result = batch_permission._evaluate_batch(SI, names, "read", plan)
			self.assertEqual(sorted(len(chunk) for chunk in result.chunks), [1, 1])
			clear_plan_memo()
			with patch.object(batch_permission, "_mro_reason", return_value="controller_override"):
				self.assertEqual(batch_permission.batch_plan(SI).reason, "controller_override")
			clear_plan_memo()
			with patch.object(batch_permission, "_table_columns", return_value={}):
				self.assertEqual(batch_permission.batch_plan(SI).reason, "no_table")


class TestReaderOutcomeLine(LimitsCase):
	"""The `reader_outcome` line of a side carries `permission_mode` and `permission_reason` (and only then)."""

	RECORD = {"side": "Payable", "path": "reader", "today_ms": 5, "reader_ms": 7, "view_ms": 9}
	OFF = "reader_outcome side=Payable path=reader class=served company=abc today_ms=5 reader_ms=7 view_ms=9"

	def line(self, counts):
		lines = []
		logger = MagicMock()
		logger.info.side_effect = lines.append
		record = {**self.RECORD, **({} if counts is None else {"counts": counts})}
		with patch.object(frappe, "logger", return_value=logger):
			read_log._info(record, "served", "abc", False)
		(text,) = lines
		return text

	def test_without_permission_counts_the_line_is_the_one_of_today(self):
		self.assertEqual(self.line(None), self.OFF)
		self.assertEqual(self.line({"assemble_rows": 4}), self.OFF)

	def test_a_batch_side_shows_the_mode_and_no_reason(self):
		counts = {"permission_calls": 2, "permission_mode_batch": 2}
		self.assertEqual(self.line(counts), self.OFF + " permission_mode=batch permission_reason=-")

	def test_a_per_document_side_shows_its_reasons(self):
		counts = {
			"permission_calls": 2,
			"permission_mode_per_doc": 2,
			"permission_reason_pin_drift": 1,
			"permission_reason_breaker": 1,
		}
		self.assertEqual(
			self.line(counts), self.OFF + " permission_mode=per_doc permission_reason=breaker,pin_drift"
		)

	def test_both_modes_are_mixed(self):
		counts = {"permission_calls": 2, "permission_mode_batch": 1, "permission_mode_per_doc": 1}
		self.assertIn("permission_mode=mixed", self.line(counts))


class TestRequestPathEvents(LimitsCase):
	"""Outside a snapshot view an event is written at once; inside one it is only queued."""

	def event(self, operation="pcal_permission_pin_drift", message="permission source changed: Some.pin"):
		return {"operation": operation, "message": message}

	def test_outside_a_view_the_event_is_written_at_once_and_marked(self):
		batch_permission.queue_event(**self.event())
		self.assertEqual(self.written.call_count, 1)
		title, message, operation, klass = self.written.call_args.args
		self.assertEqual(title, f"{batch_permission.EVENT_TITLE} pcal_permission_pin_drift")
		self.assertEqual(klass, batch_permission.EVENT_CLASS)
		self.assertTrue(operation.startswith("pcal_permission_pin_drift:"))
		self.assertTrue(self.events()[0]["written"])

	def test_inside_a_view_it_is_only_queued(self):
		with fake_view():
			batch_permission.queue_event(**self.event())
		self.written.assert_not_called()
		self.assertNotIn("written", self.events()[0])

	def test_a_write_error_does_not_reach_the_caller_and_the_event_stays_queued(self):
		self.written.side_effect = RuntimeError("insert failed")
		batch_permission.queue_event(**self.event())
		self.assertNotIn("written", self.events()[0])

	def test_a_timeout_is_not_swallowed(self):
		self.written.side_effect = TimeoutError
		with self.assertRaises(TimeoutError):
			batch_permission.queue_event(**self.event())

	def test_the_job_flush_skips_an_event_that_was_written_at_once(self):
		frappe.local.pcal_permission_events = [
			{**self.event("a", "one"), "written": True},
			self.event("b", "two"),
		]
		jobs._write_permission_events()
		self.assertEqual(self.written.call_count, 1)
		self.assertEqual(self.written.call_args.args[1], "two")

	def test_a_pin_drift_first_seen_on_a_read_gives_one_error_log_row_at_once(self):
		with (
			patch.object(compatibility, "permission_pin_drift", lambda: ["Some.pin"]),
			session_as(self.f.u1),
		):
			for _ in range(2):
				batch_permission.batch_visible(SI, set(self.names()), "read")
		self.assertEqual(self.written.call_count, 1, "the plan is built once for the request")


class TestMismatchLabel(LimitsCase):
	"""Only the stop's own error, after the stop time, is `mismatch_unresolved`."""

	def run_recheck_failing_with(self, error):
		offset = [0.0]
		stop = monotonic() + 100

		def late():
			offset[0] = 1000.0
			raise error

		with (
			patch.object(batch_permission, "monotonic", lambda: monotonic() + offset[0]),
			batch_permission.permission_stop(stop, TimeoutError),
			Fault("persistent", on_recheck=late).active(),
			self.assertRaises(type(error)),
		):
			self.in_job(SI)

	def test_another_error_past_the_stop_time_propagates_without_a_trip(self):
		self.run_recheck_failing_with(RuntimeError("database error"))
		self.assertIsNone(redis("GET", self.key()))
		self.assertEqual(self.recorder.calls, [])
		self.assertFalse(getattr(frappe.local, batch_permission.TRIPPED, False))

	def test_the_stop_error_past_the_stop_time_is_unresolved(self):
		self.run_recheck_failing_with(TimeoutError())
		self.assert_tripped("mismatch_unresolved", config="deferred")

	def test_a_stop_type_error_before_the_stop_time_is_not_a_stop(self):
		stop_error = TimeoutError("slow lock")
		with (
			batch_permission.permission_stop(monotonic() + 1000, TimeoutError),
			Fault("persistent", on_recheck=lambda: (_ for _ in ()).throw(stop_error)).active(),
			self.assertRaises(TimeoutError),
		):
			self.in_job(SI)
		self.assertIsNone(redis("GET", self.key()))


class TestSwitchIsFixedForAJob(LimitsCase):
	"""`frappe.conf` is read at `frappe.init` and changes in a job only through this process's own write.

	A switch that was off at job start therefore stays off for the job: no code path re-reads the site config.
	(Checked, not assumed: a changed `site_config.json` does not reach the running process.)
	"""

	def test_the_config_is_assigned_once_by_init_and_the_attribute_is_the_object_the_code_reads(self):
		import inspect

		self.assertIn("local.conf = get_site_config(", inspect.getsource(frappe.init))
		frappe.local.conf[SWITCH] = 0
		self.assertFalse(batch_permission.switch_enabled())
		frappe.local.conf[SWITCH] = 1
		self.assertTrue(batch_permission.switch_enabled(), "the code reads the process config object")

	def test_a_changed_config_file_does_not_change_a_running_process(self):
		import json
		import tempfile

		frappe.local.conf[SWITCH] = 0
		with tempfile.TemporaryDirectory() as folder:
			path = Path(folder) / "site_config.json"
			path.write_text(json.dumps({SWITCH: 1}))
			self.assertFalse(batch_permission.switch_enabled())
			frappe.get_site_config(site_path=folder)  # reading a file is not assigning it
			self.assertFalse(batch_permission.switch_enabled())

	def test_the_batch_code_never_reads_the_site_config_itself(self):
		import importlib
		import inspect

		for name in MODULES:
			source = inspect.getsource(importlib.import_module("jarvis.payment_calendar." + name))
			with self.subTest(module=name):
				self.assertNotIn("get_site_config", source)
				self.assertNotIn("site_config.json", source)


class TestTripFlagOnTimeout(LimitsCase):
	def test_a_timeout_in_a_write_still_sets_the_trip_flag(self):
		with (
			patch.object(batch_permission, "_set_breaker_key", side_effect=TimeoutError),
			self.assertRaises(TimeoutError),
		):
			batch_permission._trip(SI, "mismatch")
		self.assertTrue(getattr(frappe.local, batch_permission.TRIPPED, False))
		self.assertEqual(self.recorder.calls, [], "the later write did not run: the timeout ended the trip")


class TestLatchStopsEveryDoctype(LimitsCase):
	"""A trip whose site-config write failed (or was skipped) leaves the config at 1: the latch key stops all."""

	def test_the_failed_latch_key_makes_every_doctype_breaker(self):
		redis("SET", self.latch_key(), "1")
		for doctype in (SI, CUSTOMER, "Journal Entry"):
			with self.subTest(doctype=doctype):
				self.assertEqual(batch_permission._live_gate(doctype), "breaker")

	def test_a_trip_with_a_failed_config_write_stops_another_doctype_in_a_later_process(self):
		self.recorder.fail = True
		batch_permission._trip(SI, "mismatch")
		forget_local()
		self.assertEqual(batch_permission._live_gate(CUSTOMER), "breaker")
		with patch.object(batch_permission, "_evaluate_batch") as batch, session_as(self.f.u1):
			got = batch_permission.batch_visible(CUSTOMER, set(self.names(CUSTOMER)), "read")
		batch.assert_not_called()
		with session_as(self.f.u1):
			self.assertEqual(got, oracle(CUSTOMER, self.names(CUSTOMER), "read"))

	def test_without_the_latch_key_only_the_tripped_doctype_is_stopped(self):
		batch_permission._trip(SI, "mismatch")  # the config write worked: it reads 0 and no latch key is set
		forget_local()
		self.assertEqual(redis("EXISTS", self.latch_key()), 0)
		frappe.local.conf[SWITCH] = 1  # the operator turned the batch on again without a reset
		self.assertEqual(batch_permission._live_gate(SI), "breaker")
		self.assertIsNone(batch_permission._live_gate(CUSTOMER))

	def test_a_redis_error_on_the_gate_is_logged_with_its_class_only(self):
		logger = MagicMock()
		real = frappe.cache.execute_command

		def command(*args, **kwargs):
			if args and args[0] == "EXISTS":
				raise ConnectionError("secret-host")
			return real(*args, **kwargs)

		with (
			patch.object(frappe.cache, "execute_command", command),
			patch.object(frappe, "logger", return_value=logger),
		):
			self.assertEqual(batch_permission._live_gate(SI), "breaker_unknown")
		line = logger.warning.call_args.args[0]
		self.assertIn("ConnectionError", line)
		self.assertNotIn("secret-host", line)


class TestSwitchValue(LimitsCase):
	def test_the_switch_reads_as_a_frappe_flag(self):
		cases = (
			(1, True),
			("1", True),
			(0, False),
			("0", False),
			("false", False),
			("off", False),
			("", False),
			(None, False),
		)
		for value, expected in cases:
			with self.subTest(value=value):
				frappe.local.conf[SWITCH] = value
				self.assertEqual(batch_permission.switch_enabled(), expected)

	def test_status_shows_the_same_reading(self):
		frappe.local.conf[SWITCH] = "0"
		self.assertFalse(batch_permission.status()["switch"])


class TestEventShape(LimitsCase):
	"""The Error Log title carries the operation; the text carries the user digest, branch, direction and job."""

	def test_the_title_ends_with_the_operation_and_persistent_events_dedupe_for_a_day(self):
		for operation in (
			"pcal_permission_pin_drift",
			"pcal_permission_identity",
			"pcal_permission_plan",
			"pcal_permission_plan_error",
		):
			batch_permission.queue_event(operation, "m " + operation)
			self.assertTrue(self.written.call_args.args[0].endswith(operation))
			self.assertEqual(self.written.call_args.kwargs["window"], 24 * 3600)
		batch_permission.queue_event("pcal_permission_batch_mismatch", "mm")
		self.assertEqual(self.written.call_args.kwargs["window"], read_log.ERROR_LOG_WINDOW)

	def mismatch(self, kind, names, user=None):
		with (
			patch.dict(frappe.local.conf, {"encryption_key": KEY}),
			patch("rq.get_current_job", return_value=SimpleNamespace(id="pcal:abc:def")),
			Fault(kind).active(),
		):
			with self.assertRaises(CalendarSourceUnavailable):
				self.in_job(SI, names)
		events = [e for e in self.events() if e["operation"] == "pcal_permission_batch_mismatch"]
		return events[-1]["message"]

	def test_a_confirmed_mismatch_names_the_digest_the_branch_the_direction_and_the_job(self):
		with patch.dict(frappe.local.conf, {"encryption_key": KEY}):
			digest = read_log.ops_digest(self.f.u1)
		message = self.mismatch("persistent", self.names())
		self.assertIn(f"user={digest}", message)
		self.assertIn("job=pcal:abc:def", message)
		self.assertRegex(message, r"branch=(special|plain) direction=per_doc_only")
		self.assert_no_names_in(message)

	def test_a_name_that_only_the_batch_shows_is_batch_only_and_special(self):
		f = self.f
		message = self.mismatch("add", {f.si_plain, f.si_twe_a, f.si_twe_b})
		self.assertIn("direction=batch_only", message)
		self.assertIn("branch=special", message)

	def test_a_transient_mismatch_carries_the_same_fields(self):
		message = self.mismatch("transient", self.names())
		self.assertTrue(message.startswith("permission_mismatch_transient"))
		self.assertIn("direction=", message)
		self.assertIn("job=pcal:abc:def", message)

	def test_a_mode_change_event_carries_the_digest_and_the_job(self):
		batch_permission.begin_mode_freeze()
		with patch.object(batch_permission, "_live_gate", return_value=None), session_as(self.f.u1):
			batch_permission.batch_visible(SI, set(self.names()), "read")
		with patch.object(batch_permission, "_live_gate", return_value="breaker"), session_as(self.f.u1):
			with self.assertRaises(CalendarSourceUnavailable):
				batch_permission.batch_visible(SI, set(self.names()), "read")
		message = self.events()[-1]["message"]
		self.assertTrue(message.startswith("permission_mode_changed: doctype=Sales Invoice"))
		self.assertIn("user=", message)
		self.assertIn("job=", message)

	def test_the_refusal_carries_a_fixed_detail_word(self):
		with Fault("persistent").active(), self.assertRaises(CalendarSourceUnavailable) as raised:
			self.in_job(SI)
		self.assertEqual(raised.exception.permission_detail, "permission_mismatch")


class TestPlanReasonEvent(LimitsCase):
	"""A doctype that stays per document for a plan reason is made visible, once per request."""

	def reasons(self):
		return [e["message"] for e in self.events() if e["operation"] == "pcal_permission_plan"]

	def run_calls(self, reason, calls=2):
		plan = batch_permission.BatchPlan("per_doc", reason, ())
		with patch.object(batch_permission, "batch_plan", return_value=plan), session_as(self.f.u1):
			for _ in range(calls):
				batch_permission.batch_visible(SI, set(self.names()), "read")

	def test_a_plan_reason_gives_one_event_for_many_calls(self):
		self.run_calls("controller_override")
		self.assertEqual(len(self.reasons()), 1)
		self.assertIn("doctype=Sales Invoice reason=controller_override", self.reasons()[0])

	def test_the_reasons_that_have_their_own_report_give_none(self):
		for reason in ("scope_users", "pin_drift", "entry_patched"):
			with self.subTest(reason=reason):
				self.run_calls(reason, 1)
				self.assertEqual(self.reasons(), [])


class TestStatusReadsAndPlans(LimitsCase):
	def test_the_counters_are_todays_and_reading_them_counts_nothing(self):
		redis(
			"INCR",
			frappe.cache.make_key(f"jarvis:pcal:site:day:{routing._today()}:permission_batch:wave_test"),
		)
		self.addCleanup(
			redis,
			"DEL",
			frappe.cache.make_key(f"jarvis:pcal:site:day:{routing._today()}:permission_batch:wave_test"),
		)
		first = batch_permission.status()["counters"]
		second = batch_permission.status()["counters"]
		self.assertEqual(first["permission_batch:wave_test"], 1)
		self.assertEqual(first, second)

	def test_the_plan_of_each_expected_doctype_is_shown_for_the_user_and_writes_nothing(self):
		before = list(self.events())
		status = batch_permission.status(self.f.u1)
		self.assertEqual(set(status["plans"]), set(batch_permission.EXPECT_BATCH))
		self.assertEqual(status["plans"][SI], {"mode": "batch", "reason": None})
		self.assertEqual(self.events(), before)
		self.written.assert_not_called()
		self.assertEqual(frappe.session.user, "Administrator")

	def test_a_per_document_reason_shows_in_the_plans(self):
		real = batch_permission._mro_reason
		with patch.object(
			batch_permission,
			"_mro_reason",
			lambda d, layout: "controller_override" if d == CUSTOMER else real(d, layout),
		):
			plans = batch_permission.status(self.f.u1)["plans"]
		self.assertEqual(plans[CUSTOMER], {"mode": "per_doc", "reason": "controller_override"})


class TestResetBreaker(LimitsCase):
	def counter_key(self, doctype):
		return frappe.cache.make_key(
			f"jarvis:pcal:site:day:{PRIVATE_DATE}:permission_mismatch_transient:{doctype}"
		)

	def test_it_returns_what_it_deleted_and_clears_the_transient_counters_of_the_doctype(self):
		batch_permission._trip(SI, "mismatch")
		batch_permission._trip(CUSTOMER, "mismatch")
		for doctype in (SI, CUSTOMER):
			read_log.site_count("permission_mismatch_transient:" + doctype, "x")
		deleted = batch_permission.reset_breaker(SI)
		self.assertEqual(deleted["breakers"], [SI])
		self.assertEqual(deleted["transient_counters"], ["permission_mismatch_transient:" + SI])
		self.assertEqual(redis("EXISTS", self.counter_key(SI)), 0)
		self.assertEqual(redis("EXISTS", self.counter_key(CUSTOMER)), 1)

	def test_without_a_doctype_it_clears_all_counters_and_the_latch_and_says_so(self):
		self.recorder.fail = True
		batch_permission._trip(SI, "mismatch")
		read_log.site_count("permission_mismatch_transient:" + CUSTOMER, "x")
		deleted = batch_permission.reset_breaker()
		self.assertEqual(
			(deleted["breakers"], deleted["latch_write_failed"], deleted["transient_counters"]),
			([SI], True, ["permission_mismatch_transient:" + CUSTOMER]),
		)
		self.assertEqual(batch_permission.status()["breakers"], [])
		self.assertFalse(batch_permission.status()["latch_write_failed"])

	def test_one_audit_line_names_the_doctype_and_the_user_digest_only(self):
		logger = MagicMock()
		with (
			patch.dict(frappe.local.conf, {"encryption_key": KEY}),
			patch.object(frappe, "logger", return_value=logger),
		):
			batch_permission.reset_breaker(SI)
			digest = read_log.ops_digest(frappe.session.user)
		(line,) = [
			c.args[0] for c in logger.info.call_args_list if c.args[0].startswith("permission_batch_reset")
		]
		self.assertIn("doctype=Sales Invoice", line)
		self.assertIn(f"user={digest}", line)
		self.assertNotIn("Administrator", line)


class TestTripNearTheStop(LimitsCase):
	"""Inside a job, a trip with little time left defers the config write to the end of the job."""

	def near(self):
		batch_permission.begin_mode_freeze()
		with batch_permission.permission_stop(monotonic() + 10, TimeoutError):
			batch_permission._trip(SI, "mismatch")

	def test_with_little_time_left_the_config_write_is_deferred_and_the_latch_stops_the_site(self):
		self.near()
		self.assert_tripped("mismatch", config="deferred")
		self.assertIn("breaker_write_deferred:site_config", [e["message"] for e in self.events()])
		forget_local()  # a later process: the trip flag is gone, the latch key still stops the site
		self.assertEqual(batch_permission._live_gate(CUSTOMER), "breaker")

	def test_the_end_of_the_job_writes_the_config_and_drops_the_latch(self):
		self.near()
		batch_permission.write_pending_config()
		self.assert_tripped("mismatch")
		batch_permission.write_pending_config()  # a second call writes nothing more
		self.assertEqual(self.recorder.calls, [(SWITCH, 0)])

	def test_a_failed_deferred_write_leaves_the_latch_and_queues_the_event(self):
		self.near()
		self.recorder.fail = True
		batch_permission.write_pending_config()
		self.assertEqual(redis("EXISTS", self.latch_key()), 1)
		self.assertIn("breaker_write_failed:site_config", [e["message"] for e in self.events()])
		self.assertEqual(self.recorder.calls, [(SWITCH, 0)])

	def test_with_time_to_spare_the_config_is_written_at_once(self):
		batch_permission.begin_mode_freeze()
		with batch_permission.permission_stop(monotonic() + 100, TimeoutError):
			batch_permission._trip(SI, "mismatch")
		self.assert_tripped("mismatch")

	def test_without_a_stop_the_config_is_written_at_once(self):
		batch_permission._trip(SI, "mismatch")
		self.assert_tripped("mismatch")

	def test_outside_a_job_the_config_is_written_at_once_even_near_a_stop(self):
		with batch_permission.permission_stop(
			monotonic() + 10, TimeoutError
		):  # a read's stop: no job `finally`
			batch_permission._trip(SI, "mismatch")
		self.assert_tripped("mismatch")


class TestTripLogs(LimitsCase):
	def test_a_trip_writes_an_error_line_at_once_without_names(self):
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger):
			batch_permission._trip(SI, "mismatch")
		(line,) = [
			c.args[0] for c in logger.error.call_args_list if c.args[0].startswith("permission_batch_trip")
		]
		self.assertIn("doctype=Sales Invoice reason=mismatch", line)
		self.assert_no_names_in(line)

	def test_a_failed_event_write_is_an_error_line_for_a_trip_and_a_warning_otherwise(self):
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger):
			batch_permission.log_event_failure(
				{"operation": "pcal_permission_batch_mismatch", "message": "m"}, KeyError("x")
			)
			batch_permission.log_event_failure(
				{"operation": "pcal_permission_breaker", "message": "m"}, KeyError("x")
			)
			batch_permission.log_event_failure(
				{"operation": "pcal_permission_pin_drift", "message": "m"}, KeyError("x")
			)
		self.assertEqual(logger.error.call_count, 2)
		self.assertEqual(logger.warning.call_count, 1)
		self.assertIn("KeyError", logger.warning.call_args.args[0])


class TestSampleStartPerJob(LimitsCase):
	def test_the_stride_starts_at_another_name_in_another_job(self):
		names = {f"NAME-{n:03d}" for n in range(60)}
		result = batch_permission.EvalResult(set(names), [], {}, 0, 0)
		picks = set()
		for job in ("pcal:a:1", "pcal:b:2", "pcal:c:3", "pcal:d:4", "pcal:e:5"):
			forget_local()
			with patch("rq.get_current_job", return_value=SimpleNamespace(id=job)):
				picks.add(tuple(batch_permission._sample_names(SI, names, result)))
		self.assertGreater(len(picks), 1)

	def test_the_same_job_starts_at_the_same_name(self):
		names = {f"NAME-{n:03d}" for n in range(60)}
		result = batch_permission.EvalResult(set(names), [], {}, 0, 0)
		first = []
		for _ in range(2):
			forget_local()
			with patch("rq.get_current_job", return_value=SimpleNamespace(id="pcal:a:1")):
				first.append(batch_permission._sample_names(SI, names, result))
		self.assertEqual(first[0], first[1])


class TestJobFailureRecord(JobCase):
	def body(self):
		(call,) = [c for c in self.log_error.call_args_list if "Payment Calendar snapshot" in str(c)]
		return json.loads(call.kwargs["message"])

	def test_a_mismatch_refusal_carries_the_fixed_detail_word(self):
		with Fault("persistent").active():
			self.run_job(self.too_large_after(self.batch_call()))
		self.assertEqual(self.body()["permission_detail"], "permission_mismatch")

	def test_a_mode_change_refusal_carries_the_other_word(self):
		def other_process(side):
			redis("SET", self.key(), "now|mismatch")

		self.run_job(self.too_large_after(self.batch_call(), other_process, self.batch_call()))
		self.assertEqual(self.body()["permission_detail"], "permission_mode_changed")

	def test_a_job_without_a_batch_refusal_has_no_such_key(self):
		with patch.object(jobs, "collect", side_effect=ZeroDivisionError):
			store.purge_actor(self.f.u1)
			sid = jobs.request_snapshot(normalize_scope(self.f.a, []))["snapshot_id"]
			frappe.set_user(self.f.u1)
			jobs.run_snapshot(actor=self.f.u1, snapshot_id=sid)
		self.assertNotIn("permission_detail", self.body())


class TestTimeoutInTheTrip(LimitsCase):
	"""A timeout in the Redis step still leaves the durable config write pending."""

	def test_a_timeout_in_the_breaker_key_write_leaves_the_config_write_pending(self):
		with (
			patch.object(batch_permission, "_set_breaker_key", side_effect=TimeoutError),
			self.assertRaises(TimeoutError),
		):
			batch_permission._trip(SI, "mismatch")
		self.assertTrue(getattr(frappe.local, batch_permission.CONFIG_PENDING, False))
		self.assertEqual(self.recorder.calls, [])
		batch_permission.write_pending_config()  # what the job's `finally` does
		self.assertEqual(self.recorder.calls, [(SWITCH, 0)])

	def test_a_trip_that_wrote_the_config_leaves_nothing_pending(self):
		batch_permission._trip(SI, "mismatch")
		self.assertFalse(getattr(frappe.local, batch_permission.CONFIG_PENDING, False))

	def test_a_timeout_in_the_config_write_itself_leaves_it_pending(self):
		self.recorder.fail = False
		with (
			patch.object(frappe.installer, "update_site_config", side_effect=TimeoutError),
			self.assertRaises(TimeoutError),
		):
			batch_permission._trip(SI, "mismatch")
		self.assertTrue(getattr(frappe.local, batch_permission.CONFIG_PENDING, False))


class TestTimeoutInTheTripInAJob(JobCase):
	def test_the_job_ends_with_the_config_at_zero_although_the_redis_step_timed_out(self):
		with (
			Fault("persistent").active(),
			patch.object(batch_permission, "_set_breaker_key", side_effect=TimeoutError),
		):
			status = self.run_job(self.too_large_after(self.batch_call()))
		self.assertEqual((status["state"], status["code"]), ("failed", "timeout"))
		self.assertEqual(self.recorder.calls, [(SWITCH, 0)])
		self.assertFalse(getattr(frappe.local, batch_permission.CONFIG_PENDING, False))
