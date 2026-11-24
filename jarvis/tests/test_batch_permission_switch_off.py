"""With the switch off the batch changes nothing: results, statements, stats, counters, events, stops.

Names these tests rely on in `batch_permission`: none beyond the public ones (`batch_plan`, `_evaluate_batch`, `permission_stop`,
`begin_mode_freeze`, `queue_event`, the `frappe.local` names `STATS`, `EVENTS`, `FREEZE`, `TRIPPED`).

With the switch at 0 and a registered `has_permission` hook (so `visible_names` takes its per-document branch):
`batch_plan` and `_evaluate_batch` are never called, `read_log.site_count` is never called, `ast.parse` is never
called by `compatibility`, nothing is read from Redis for a breaker, no stat, event or freeze record exists, no
stop is set in the job, and the result equals a copy of today's `visible_names` written in this file. The reader's
and the job's outcome records and log lines carry no `permission_*` word. `compatibility.reader_pin_drift()` is
the same with a forced permission-pin drift.

Reader-level parts that need no job (`_permission_deadline` rebinding only with a hook, no stop) are in
`test_batch_permission_wiring`; this module does not repeat them.
"""

import ast
import inspect
import sys
from contextlib import ExitStack
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

import frappe

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
from jarvis.permission_hooks import effective_hooks
from jarvis.tests.batch_permission_fixtures import (
	HOOK_PATH,
	HOOK_STATE,
	SI,
	BatchCase,
	hooks,
	oracle,
	session_as,
	strict,
)
from jarvis.tests.payment_calendar_results import schema2_result
from jarvis.tests.test_batch_permission_limits import KEY, SIDES, fake_view, forget_local, redis

SWITCH = batch_permission.SWITCH
CUSTOMER = "Customer"
PRIVATE_DATE = "2031-07-02"


def todays_visible_names(doctype, names, filters=None):
	"""`permissions.visible_names` as it was at 4452fb395, written out so the new code is not its own judge.

	Returns the visible names and whether the per-document loop ran."""
	names = sorted(set(names))
	allowed = set()
	try:
		for offset in range(0, len(names), 500):
			allowed.update(
				frappe.get_list(
					doctype,
					filters={**(filters or {}), "name": ["in", names[offset : offset + 500]]},
					pluck="name",
					limit_page_length=0,
				)
			)
	except (frappe.PermissionError, frappe.DoesNotExistError):
		return set(), False
	registered = effective_hooks("has_permission")
	if registered.get(doctype) or registered.get("*"):
		ptype = "select" if frappe.only_has_select_perm(doctype) else "read"
		visible = set()
		for name in allowed:
			try:
				if frappe.has_permission(doctype, ptype, doc=name):
					visible.add(name)
			except (frappe.DoesNotExistError, frappe.PermissionError):
				continue
		return visible, True
	return allowed, False


class SwitchOffCase(BatchCase):
	def setUp(self):
		super().setUp()
		stack = ExitStack()
		self.addCleanup(stack.close)
		stack.enter_context(patch.dict(frappe.local.conf, {SWITCH: 0}))
		forget_local()
		self.addCleanup(forget_local)
		self.addCleanup(frappe.set_user, "Administrator")
		self.parse_callers: list[str] = []
		real_parse = ast.parse

		def parse(*args, **kwargs):
			caller = sys._getframe(1).f_globals.get("__name__")
			self.parse_callers.append(caller)
			return real_parse(*args, **kwargs)

		self.commands: list[str] = []
		real_command = frappe.cache.execute_command

		def command(*args, **kwargs):
			self.commands.append(str(args[0]).upper() if args else "")
			return real_command(*args, **kwargs)

		self.plan = stack.enter_context(patch.object(batch_permission, "batch_plan"))
		self.evaluate = stack.enter_context(patch.object(batch_permission, "_evaluate_batch"))
		self.site_count = stack.enter_context(patch.object(read_log, "site_count"))
		self.stop = stack.enter_context(patch.object(batch_permission, "permission_stop"))
		stack.enter_context(patch.object(ast, "parse", parse))
		stack.enter_context(patch.object(frappe.cache, "execute_command", command))
		self.loop = stack.enter_context(
			patch.object(permissions, "_visible_names_per_doc", wraps=permissions._visible_names_per_doc)
		)

	def assert_nothing_of_the_batch_ran(self):
		self.plan.assert_not_called()
		self.evaluate.assert_not_called()
		self.site_count.assert_not_called()
		self.stop.assert_not_called()
		self.assertNotIn("jarvis.payment_calendar.compatibility", self.parse_callers)
		self.assertNotIn("EXISTS", self.commands, "no breaker key is read with the switch off")
		for name in (batch_permission.STATS, batch_permission.EVENTS, batch_permission.FREEZE):
			self.assertFalse(getattr(frappe.local, name, None), name)
		self.assertFalse(getattr(frappe.local, batch_permission.TRIPPED, False))


class TestVisibleNames(SwitchOffCase):
	def run_cases(self, extra):
		f = self.f
		ran_a_loop = []
		deny = {f.si_hook_deny, f.si_plain}
		for strict_on in (True, False):
			for user in (f.u1, f.u3, f.u4, f.u5):
				for doctype in (SI, CUSTOMER):
					with self.subTest(strict=strict_on, user=user, doctype=doctype):
						names = set(f.names(doctype))
						HOOK_STATE["deny"] = deny
						with strict(strict_on), hooks(extra), session_as(user):
							expected, looped = todays_visible_names(doctype, names)
							self.loop.reset_mock()
							got = permissions.visible_names(doctype, set(names))
						self.assertEqual(got, expected)
						self.assertEqual(self.loop.call_count, int(looped))
						ran_a_loop.append(looped)
						self.assert_nothing_of_the_batch_ran()
						self.commands.clear()
		HOOK_STATE.clear()
		self.assertTrue(any(ran_a_loop), "some case must take the per-document branch")

	def test_the_per_document_branch_gives_todays_result_and_runs_nothing_of_the_batch(self):
		self.run_cases({SI: [HOOK_PATH + "deny_listed"], CUSTOMER: [HOOK_PATH + "deny_listed"]})

	def test_a_wildcard_hook_takes_the_same_branch(self):
		self.run_cases({"*": [HOOK_PATH + "deny_listed"]})

	def test_the_entry_points_keep_their_signatures(self):
		self.assertEqual(
			str(inspect.signature(permissions.visible_names)),
			"(doctype: str, names, filters: dict | None = None) -> set[str]",
		)
		self.assertEqual(
			str(inspect.signature(self.loop._mock_wraps)),
			"(doctype: str, allowed: set[str], ptype: str) -> set[str]",
		)

	def test_a_forced_permission_pin_drift_does_not_change_the_reader_pin_check(self):
		before = compatibility.reader_pin_drift()
		with patch.object(compatibility, "permission_pin_drift", return_value=["Some.pin"]) as drift:
			self.assertEqual(compatibility.reader_pin_drift(), before)
		drift.assert_not_called()


class TestReaderEntry(SwitchOffCase):
	"""`builtin_reader.read_obligations` with a deadline: no stop is set and no permission key is added."""

	def read(self):
		counts = {"assemble_rows": 0}
		engine = SimpleNamespace(reader_stats={"counts": counts}, data=[], excluded=[], terms={})
		names = set(self.f.names(SI))
		with (
			patch.object(sources, "_require_inputs", return_value=(None, "Payable", "s", "a", {})),
			patch.object(builtin_reader, "validate_obligation_books"),
			patch.object(sources, "_run_engine", return_value=engine),
			patch.object(
				sources, "_assemble", side_effect=lambda *a: permissions.visible_names(SI, set(names))
			),
			hooks({SI: [HOOK_PATH + "deny_listed"]}),
			session_as(self.f.u1),
		):
			builtin_reader.read_obligations(
				"c", "Payable", "a", "s", "e", stats={"counts": counts}, deadline=monotonic() + 60
			)
		return counts

	def test_the_reader_counts_have_no_permission_key_and_no_stop_is_set(self):
		counts = self.read()
		self.assertEqual(counts, {"assemble_rows": 0})
		self.stop.assert_not_called()
		self.assert_nothing_of_the_batch_ran()


class TestJobAndRecords(SwitchOffCase):
	"""A job with the switch off: no stop, no freeze, no event, and records and log lines as today's."""

	def setUp(self):
		super().setUp()
		stack = ExitStack()
		self.addCleanup(stack.close)
		frappe.set_user(self.f.u1)
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
		self.addCleanup(store.purge_actor, self.f.u1)
		self.addCleanup(self.clean_routing)
		self.clean_routing()
		self.outcomes: list[dict] = []

	def clean_routing(self):
		for number in range(routing.READER_SLOTS):
			redis("DEL", routing.slot_key(number))
		for side in SIDES:
			redis("DEL", routing.memo_key(self.f.a, side))
			redis("DEL", routing.inflight_key(self.f.a, side))

	def run_job(self):
		frappe.set_user(self.f.u1)  # a helper such as `session_as` leaves the session as Administrator
		store.purge_actor(self.f.u1)
		sid = jobs.request_snapshot(normalize_scope(self.f.a, []))["snapshot_id"]
		names = set(self.f.names(SI))

		def today_read(company, side, as_of, window_start, window_end):
			permissions.visible_names(SI, set(names))
			raise CalendarSourceUnavailable("too_large")

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

		with hooks({SI: [HOOK_PATH + "deny_listed"]}), patch.object(jobs, "collect", side_effect=collect):
			jobs.run_snapshot(actor=self.f.u1, snapshot_id=sid)
		return store.load_status(self.f.u1, sid)

	def test_the_job_completes_with_no_stop_no_freeze_no_event_and_no_batch_call(self):
		status = self.run_job()
		self.assertEqual(status["state"], "complete")
		self.assertGreaterEqual(self.loop.call_count, 2, "one per-document pass for each side")
		self.assert_nothing_of_the_batch_ran()
		self.error_log.assert_not_called()

	def test_the_outcome_records_and_the_log_lines_have_no_permission_key(self):
		self.run_job()
		self.assertEqual(len(self.outcomes), 2)
		for record in self.outcomes:
			self.assertFalse([key for key in record if "permission" in str(key)], record)
			self.assertNotIn("permission", repr(record))
		lines: list[str] = []
		with (
			patch.object(
				read_log, "_info", side_effect=lambda record, *a, **k: lines.append(repr((record, a)))
			),
			patch.object(read_log, "_warn", side_effect=lines.append),
			patch.object(read_log, "_count"),
		):
			read_log.reader_outcomes(self.outcomes, PRIVATE_DATE)
		self.assertFalse([line for line in lines if "permission_" in line], lines)

	def test_the_view_still_ends_cleanly(self):
		self.run_job()
		self.assertFalse(snapshot.view_is_broken())


class TestFrozenDoctypeWithTheSwitchOff(BatchCase):
	"""Once a freeze entry exists, the switch off still writes no stat, counter or event."""

	def setUp(self):
		super().setUp()
		stack = ExitStack()
		self.addCleanup(stack.close)
		stack.enter_context(patch.dict(frappe.local.conf, {SWITCH: 1}))
		self.site_count = stack.enter_context(patch.object(read_log, "site_count"))
		forget_local()
		self.addCleanup(forget_local)
		self.addCleanup(frappe.set_user, "Administrator")
		self.names = set(self.f.names(SI))
		batch_permission.begin_mode_freeze()

	def stats(self):
		return list(getattr(frappe.local, batch_permission.STATS, None) or [])

	def events(self):
		return list(getattr(frappe.local, batch_permission.EVENTS, None) or [])

	def freeze_as(self, gate):
		"""The first call of the job for the doctype, with the live gate `gate` (None: the batch runs)."""
		with patch.object(batch_permission, "_live_gate", return_value=gate), session_as(self.f.u1):
			batch_permission.batch_visible(SI, set(self.names), "read")

	def call_with_the_switch_off(self):
		frappe.local.conf[SWITCH] = 0
		with session_as(self.f.u1):
			return batch_permission.batch_visible(SI, set(self.names), "read")

	def test_a_doctype_frozen_as_per_document_stays_silent_and_gives_todays_result(self):
		self.freeze_as("breaker")
		before = (len(self.stats()), len(self.events()), self.site_count.call_count)
		got = self.call_with_the_switch_off()
		with session_as(self.f.u1):
			self.assertEqual(got, oracle(SI, self.names, "read"))
		self.assertEqual((len(self.stats()), len(self.events()), self.site_count.call_count), before)

	def test_a_doctype_frozen_as_batch_refuses_the_job_in_silence(self):
		self.freeze_as(None)
		before = (len(self.stats()), len(self.events()), self.site_count.call_count)
		with (
			patch.object(routing, "_poison_view") as poison,
			self.assertRaises(CalendarSourceUnavailable) as raised,
		):
			self.call_with_the_switch_off()
		self.assertEqual(raised.exception.code, "source_changed")
		poison.assert_called_once()
		self.assertEqual((len(self.stats()), len(self.events()), self.site_count.call_count), before)

	def test_after_a_trip_the_same_call_is_recorded(self):
		self.freeze_as("breaker")
		frappe.local.conf[SWITCH] = 0
		setattr(frappe.local, batch_permission.TRIPPED, True)
		before = len(self.stats())
		with session_as(self.f.u1):
			batch_permission.batch_visible(SI, set(self.names), "read")
		self.assertEqual(len(self.stats()), before + 1)
		self.assertEqual(self.stats()[-1]["reason"], "tripped")
