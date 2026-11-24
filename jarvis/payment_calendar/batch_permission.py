"""An exact batch replacement for the per-document `frappe.has_permission` loop of `permissions.visible_names`.

This is the entry module: the live gate, the freeze, the stop, the stats, the sample oracle, the trip and the
breaker, `status()`, `reset_breaker()` and `differential()`. The metadata plan is in `batch_permission_plan`, the
evaluator in `batch_permission_eval`; their names are re-exported here, and they read the names that tests or
operators rebind (`_table_columns`, `_lazy_doc`, `CHUNK`...) through this module at call time.

`batch_visible(doctype, names, ptype)` returns `{n for n in names if frappe.has_permission(doctype, ptype, doc=n)}`
for the session user. It reads the parent rows, the child rows that a User Permission can deny, the shares and
the hook inputs in chunks, then runs Frappe's own `get_role_permissions`, `has_user_permission` and the known
hooks on a read-only stand-in for the document. Anything it cannot reproduce with certainty goes to the
per-document loop: the whole doctype (`batch_plan` reasons) or one name (`ShimMiss`, a name that the database
spells differently). Every Frappe callable is resolved through its module binding at call time, never imported
by name, so the identity checks of `compatibility` see the same objects that the per-document path uses.
"""

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from hashlib import sha256
from re import fullmatch
from time import monotonic
from typing import NoReturn

import frappe
import frappe.installer
import frappe.model.document
import frappe.permissions
import frappe.utils

from jarvis.payment_calendar import read_log
from jarvis.payment_calendar.batch_permission_eval import (
	EvalResult,
	ShimMiss,
	_PermDoc,
	_PermRow,
	_Run,
)
from jarvis.payment_calendar.batch_permission_eval import evaluate_batch as _evaluate_batch
from jarvis.payment_calendar.batch_permission_plan import (
	_INHERITED_HOOK,
	_PARTY_HOOK,
	BatchPlan,
	_compat_failures,
	_hook_chain,
	_Layout,
	_master_links,
	_mro_reason,
	_null_keys,
	_quoted,
	_Table,
	_table_columns,
	build_plan,
)
from jarvis.payment_calendar.errors import TIMEOUTS, CalendarSourceUnavailable

SWITCH = "payment_calendar_batch_permission"
TRIPPED = "pcal_permission_tripped"
PLAN_MEMO = "pcal_permission_plans"
FREEZE = "pcal_permission_freeze"
EVENTS = "pcal_permission_events"
# Set on `frappe.local` while `differential` runs: events are only recorded, never written (it writes nothing).
QUIET = "pcal_permission_quiet"
STATS = "pcal_permission_stats"
BREAKER_KEY = "jarvis:pcal:permission_breaker:"
# A user without any User Permission and this many names or fewer: the loop is cheaper than the first batch.
SMALL_INPUT = 4
CHUNK = 500
# The sample oracle (jobs only): names per doctype and call, and the wall clock of all samples of one job.
SAMPLE_PER_DOCTYPE = 3
SAMPLE_BUDGET_S = 0.5
SAMPLE_SPENT = "pcal_permission_sample_spent"
SAMPLE_RUNS = "pcal_permission_sample_runs"
# A transient mismatch (the re-check does not repeat it) that happens this often for one doctype in one site
# day trips the breaker.
TRANSIENT_LIMIT = 2
# The Redis flag `status()` shows when a trip could not write the site config; beside the breaker keys.
LATCH_FAILED = "_latch_write_failed"
EVENT_MISMATCH = "pcal_permission_batch_mismatch"
EVENT_BREAKER = "pcal_permission_breaker"
EVENT_PLAN = "pcal_permission_plan"
# Events about a state that lasts (a drifted pin, a patched callable, a doctype that is always per document): one
# Error Log row per day, not one per five minutes.
PERSISTENT_EVENTS = frozenset(
	{"pcal_permission_pin_drift", "pcal_permission_identity", "pcal_permission_plan_error", EVENT_PLAN}
)
PERSISTENT_EVENT_WINDOW_S = 24 * 3600
# Fixed words that the failure record of a job refused by the batch carries.
# Kept on `frappe.local` too: the snapshot collector turns the error into a plain `source_changed` of its own.
REFUSAL = "pcal_permission_refusal"
DETAIL_MISMATCH = "permission_mismatch"
DETAIL_MODE_CHANGED = "permission_mode_changed"
# A trip inside a job with less than this many seconds before the job stop defers the site-config write (it takes a
# file lock and cannot be cut) to the end of the job; the Redis key, the latch key and the flag stop the batch at once.
CONFIG_WRITE_MARGIN_S = 35
# Set on `frappe.local` by a trip that deferred its site-config write; `write_pending_config` does the write.
CONFIG_PENDING = "pcal_permission_config_pending"
# Plan reasons that need no event of their own: the switch and the user decide them, or the plan builder
# already queued one (pin drift, identity, plan error).
SILENT_PLAN_REASONS = frozenset({"scope_users", "pin_drift", "entry_patched", "plan_error"})
# The Error Log title and exception class of every event; `error_log_once` dedupes by operation and class.
EVENT_TITLE = "Payment Calendar batch permission"
EVENT_CLASS = "batch_permission"
# The differential selects documents by SQL; each selector returns at most this many names per user and doctype.
DIFFERENTIAL_NAME_LIMIT = 50_000
# The doctypes the differential must run in batch mode, and that `status()` checks the identity of.
EXPECT_BATCH = ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry", "Customer", "Supplier")
DIFFERENTIAL_BOX_S = 4 * 3600

# The stop of the permission work in this context: (monotonic limit, error factory) or None.
_STOP: ContextVar[tuple[float, Callable[[], BaseException]] | None] = ContextVar(
	"pcal_permission_stop", default=None
)


def batch_visible(doctype: str, names: set[str], ptype: str) -> set[str]:
	"""Exactly `{n for n in names if frappe.has_permission(doctype, ptype, doc=n)}` for the session user.

	Order: live gate (never memoised) -> small input -> metadata plan -> job freeze. Empty input gives
	`set()` with no statement.
	"""
	gate = _live_gate(doctype)
	started = monotonic()
	if gate == "switch_off":
		return _switch_off(doctype, names, ptype, started)
	if gate is None and frappe.session.user == "Administrator":
		return set(names)
	if gate is None and not names:
		return set()
	if gate is None and _is_small(names):
		return _per_doc_recorded(doctype, names, ptype, "small_input", started)
	plan = batch_plan(doctype) if gate is None else None
	try:
		_check_frozen(doctype, gate, plan)
	except CalendarSourceUnavailable:
		_stats(doctype, "per_doc", gate or "plan_changed", names, started, refusal="mode_changed")
		raise
	if plan is None or plan.mode != "batch":
		_note_plan_reason(doctype, plan)
		return _per_doc_recorded(doctype, names, ptype, gate or plan.reason, started)
	result = _evaluate_batch(doctype, names, ptype, plan, count_branches=False)
	record = _stats(doctype, "batch", None, names, started, result)
	if _in_job():
		_sample_oracle(doctype, names, ptype, plan, result, record)
	return result.visible


def _note_plan_reason(doctype: str, plan: BatchPlan | None) -> None:
	"""A doctype that stays per document for a plan reason is made visible once per request (and once a day)."""
	if plan is None or plan.reason in SILENT_PLAN_REASONS:
		return
	seen = _local("pcal_permission_plan_noted", set)
	if (doctype, plan.reason) not in seen:
		seen.add((doctype, plan.reason))
		queue_event(EVENT_PLAN, f"permission plan runs per document: doctype={doctype} reason={plan.reason}")


def _switch_off(doctype: str, names: set[str], ptype: str, started: float) -> set[str]:
	"""The per-document loop while the switch is off, or after a trip in this job or request.

	Only a trip is recorded (it reports a fault on a site that turned the batch on). With the plain switch off
	nothing is written, ever: no stat, no day counter, no event. A doctype that the job froze in another
	mode still refuses the job, in silence when nothing tripped.
	"""
	tripped = bool(getattr(frappe.local, TRIPPED, False))
	if doctype in _freeze_map():
		try:
			_check_frozen(doctype, "switch_off", None, quiet=not tripped)
		except CalendarSourceUnavailable:
			if tripped:
				_stats(doctype, "per_doc", "tripped", names, started, refusal="mode_changed")
			raise
	if tripped:
		return _per_doc_recorded(doctype, names, ptype, "tripped", started)
	return _per_doc(doctype, names, ptype)


def batch_plan(doctype: str) -> BatchPlan:
	"""'batch', or 'per_doc' with one fixed reason. Metadata only; memo per user and doctype on `frappe.local`.

	The memo never holds the switch or the breaker state, and nothing here lives across requests.
	"""
	memo = _local(PLAN_MEMO, dict)
	key = (frappe.session.user, doctype)
	if key not in memo:
		memo[key] = build_plan(doctype)
	return memo[key]


def switch_enabled() -> bool:
	"""The site switch is on and no trip happened in this job or request."""
	return _switch_value() and not getattr(frappe.local, TRIPPED, False)


def _switch_value() -> bool:
	"""The site config switch as Frappe reads a flag: `1`, `"1"` are on; `0`, `"0"`, `"false"`, `"off"`, empty are off."""
	return bool(frappe.utils.cint(frappe.conf.get(SWITCH)))


def begin_mode_freeze() -> None:
	"""Start the one-mode-per-job record on `frappe.local`. A job calls it once at its start."""
	setattr(frappe.local, FREEZE, {})
	setattr(frappe.local, STATS, [])
	setattr(frappe.local, SAMPLE_SPENT, 0.0)
	setattr(frappe.local, REFUSAL, None)
	setattr(frappe.local, CONFIG_PENDING, False)


def check_permission_stop() -> None:
	"""Raise the active stop's error once `monotonic()` passes it; a no-op without a stop."""
	stop = _STOP.get()
	if stop is not None and monotonic() > stop[0]:
		raise stop[1]()


@contextmanager
def permission_stop(stop: float, error_factory) -> Iterator[None]:
	"""Stop the permission work at the `monotonic()` reading `stop` with the error `error_factory()` makes.

	Nested (the reader inside a job): the earlier stop is the active one, with its own error; leaving the
	inner block restores the outer one.
	"""
	current = _STOP.get()
	active = current if current is not None and current[0] <= stop else (stop, error_factory)
	token = _STOP.set(active)
	try:
		yield
	finally:
		_STOP.reset(token)


def queue_event(operation: str, message: str) -> None:
	"""Record a name-free event. Outside a snapshot view it is written at once; inside one it is only queued.

	An insert inside the snapshot view would break it, so there `jobs.run_snapshot` writes the queue after the
	view. An event that was written at once is marked and skipped by that flush. A write error never reaches
	the caller (a timeout excepted).
	"""
	event = {"operation": operation, "message": message}
	_local(EVENTS, list).append(event)
	# The test of an open view is `snapshot.view_is_open`, as in `routing._poison_view`; `differential` writes
	# nothing at all.
	from jarvis.payment_calendar.snapshot import view_is_open

	if not view_is_open() and not getattr(frappe.local, QUIET, False):
		try:
			write_event(event)
			event["written"] = True
		except TIMEOUTS:
			raise
		except Exception as error:
			log_event_failure(event, error)


def write_event(event: dict) -> None:
	"""One Error Log row per distinct event (operation and message) in the dedupe window, not per operation.

	The operation is part of the title (the Error Log keeps the title in its `method` column, not the
	dedupe key), so one kind of event is found by title.
	"""
	operation = f"{event['operation']}:{sha256(event['message'].encode()).hexdigest()[:12]}"
	window = (
		PERSISTENT_EVENT_WINDOW_S if event["operation"] in PERSISTENT_EVENTS else read_log.ERROR_LOG_WINDOW
	)
	read_log.error_log_once(
		f"{EVENT_TITLE} {event['operation']}", event["message"], operation, EVENT_CLASS, window=window
	)


def log_event_failure(event: dict, error: Exception) -> None:
	"""The log file line of an event that could not be written: ERROR for a trip or a mismatch, else WARNING."""
	loud = event["operation"] in (EVENT_MISMATCH, EVENT_BREAKER)
	line = f"permission_event_unavailable operation={event['operation']} error={type(error).__name__}"
	try:
		logger = frappe.logger("payment_calendar")
		(logger.error if loud else logger.warning)(line)
	except Exception:
		pass


def _live_gate(doctype: str) -> str | None:
	"""Why this call must take the per-document loop right now, or None. Reads the live state on every call."""
	if not switch_enabled():
		return "switch_off"
	try:
		# One call for both keys: the doctype's breaker, and the flag of a trip whose config write failed (the
		# config still reads 1, so that trip must stop every doctype).
		keys = (
			frappe.cache.make_key(BREAKER_KEY + doctype),
			frappe.cache.make_key(BREAKER_KEY + LATCH_FAILED),
		)
		return "breaker" if frappe.cache.execute_command("EXISTS", *keys) else None
	except TIMEOUTS:
		raise
	except Exception as error:
		_log_gate_error(error)
		return "breaker_unknown"


def _log_gate_error(error: Exception) -> None:
	"""The class of the error behind `breaker_unknown` goes to the log file (no document name in a class name)."""
	try:
		frappe.logger("payment_calendar").warning(
			f"permission_breaker_unreadable error={type(error).__name__}"
		)
	except Exception:
		pass


def _dispatch_hook(method: str, **kwargs):
	"""Run one `has_permission` hook the way `has_controller_permissions` does: through `frappe.call`, debug off."""
	return frappe.call(method, debug=False, **kwargs)


def _is_small(names) -> bool:
	return len(names) <= SMALL_INPUT and not frappe.permissions.get_user_permissions(frappe.session.user)


def _per_doc(doctype: str, names, ptype: str) -> set[str]:
	from jarvis.payment_calendar import permissions

	return permissions._visible_names_per_doc(doctype, names, ptype)


def _per_doc_recorded(doctype: str, names, ptype: str, reason: str, started: float) -> set[str]:
	visible = _per_doc(doctype, names, ptype)
	_stats(doctype, "per_doc", reason, names, started)
	_count_day("permission_batch:" + reason)
	return visible


def _stats(
	doctype, mode, reason, names, started, result: EvalResult | None = None, *, refusal: str | None = None
) -> dict:
	"""Append one name-free record of this call to `frappe.local.pcal_permission_stats` and return it."""
	branches = result.branches if result else {}
	record = {
		"doctype": doctype,
		"permission_mode": mode,
		"reason": reason,
		"names": len(names),
		"ms": int((monotonic() - started) * 1000),
		"real_hook_calls": branches.get("real_hook_call", 0),
		"candidates": branches.get("candidate_allow", 0) + branches.get("candidate_deny", 0),
		"shim_miss": result.shim_miss if result else 0,
		"samples": 0,
		"mismatches": 0,
		"permission_refusal": refusal,
	}
	_local(STATS, list).append(record)
	return record


def stats_mark() -> int:
	"""The number of stats records so far: `stats_summary(mark)` then covers the calls made after it."""
	return len(getattr(frappe.local, STATS, None) or [])


def stats_summary(since: int = 0) -> dict[str, int]:
	"""Name-free integer counts of the permission calls after `since`; `{}` when there was none.

	With the switch off no call is recorded, so the caller's record and log line stay as they were.
	"""
	records = (getattr(frappe.local, STATS, None) or [])[since:]
	if not records:
		return {}
	out: Counter = Counter()
	for record in records:
		out["permission_calls"] += 1
		out["permission_mode_" + record["permission_mode"]] += 1
		if record["reason"]:
			out["permission_reason_" + record["reason"]] += 1
		if record["permission_refusal"]:
			out["permission_refusal_" + record["permission_refusal"]] += 1
		for name in ("names", "ms", "real_hook_calls", "candidates", "shim_miss", "samples", "mismatches"):
			out["permission_" + name] += record[name]
	return dict(out)


def stats_line(since: int = 0) -> str | None:
	"""One log line of `stats_summary(since)` (fixed words and numbers), or None without a call."""
	summary = stats_summary(since)
	if not summary:
		return None
	return "permission_batch " + " ".join(f"{name}={value}" for name, value in sorted(summary.items()))


def _count_day(klass: str) -> int | None:
	"""Count one site-level event of the day; a Redis error loses the count, never the reply."""
	try:
		from jarvis.payment_calendar import routing

		return read_log.site_count(klass, routing._today())
	except TIMEOUTS:
		raise
	except Exception:
		return None


def _in_job() -> bool:
	"""A job started its one-mode record (`begin_mode_freeze`); a read request never does."""
	return getattr(frappe.local, FREEZE, None) is not None


def _local(name: str, factory):
	"""The value on `frappe.local` under `name`, created by `factory` on first use."""
	value = getattr(frappe.local, name, None)
	if value is None:
		value = factory()
		setattr(frappe.local, name, value)
	return value


def _freeze_map() -> dict:
	return getattr(frappe.local, FREEZE, None) or {}


def _check_frozen(doctype: str, gate: str | None, plan: BatchPlan | None, *, quiet: bool = False) -> None:
	"""In a job the first call of a doctype fixes its mode; a later call that differs refuses the job.

	`quiet` (the plain switch off) refuses without an event.
	"""
	frozen = getattr(frappe.local, FREEZE, None)
	if frozen is None:
		return
	state = (gate is None, plan)
	if frozen.setdefault(doctype, state) == state:
		return
	from jarvis.payment_calendar import routing

	if not quiet:
		queue_event(EVENT_BREAKER, f"{DETAIL_MODE_CHANGED}: doctype={doctype} {_trace()}")
	routing._poison_view()
	raise _source_changed(DETAIL_MODE_CHANGED)


def _source_changed(detail: str) -> CalendarSourceUnavailable:
	"""The refusal of a job whose permission mode changed or whose sample disagreed; `detail` is a fixed word."""
	error = CalendarSourceUnavailable("source_changed")
	error.permission_detail = detail
	setattr(frappe.local, REFUSAL, detail)
	return error


def _trace() -> str:
	"""Name-free fields that tie an event to its user (a keyed digest) and its job."""
	return f"user={_user_digest()} job={_job_id()}"


def _user_digest() -> str:
	"""The keyed digest of the session user (the pattern of the company digest), `-` without an encryption key."""
	try:
		return read_log.ops_digest(frappe.session.user)
	except TIMEOUTS:
		raise
	except Exception:
		return "-"


def _job_id() -> str:
	"""The id of the running RQ job when its characters are fixed-name safe, else `-`."""
	try:
		from rq import get_current_job

		job = get_current_job()
		return job.id if job and fullmatch(r"[A-Za-z0-9_.:-]{1,120}", job.id or "") else "-"
	except TIMEOUTS:
		raise
	except Exception:
		return "-"


# --- metadata plan -------------------------------------------------------------------------------


# --- evaluation ----------------------------------------------------------------------------------


# --- small helpers -------------------------------------------------------------------------------


def _lazy_doc(doctype: str, name: str):
	"""A real lazy document for a hook that the stand-in cannot serve."""
	return frappe.get_lazy_doc(doctype, name)


# --- sample oracle, trip and breaker ---------------------------------------------------------------


def _sample_oracle(
	doctype: str, names: set[str], ptype: str, plan: BatchPlan, result: EvalResult, record=None
) -> None:
	"""Compare the batch verdict of a few names with Frappe's own per-document check (jobs only).

	A mismatch is re-checked on the SAME input and chunks with fresh memos, against a fresh per-document call.
	It never returns a per-document result in place of the batch result: a mismatch ends in an error.
	"""
	checked = 0
	try:
		for name in _sample_names(doctype, names, result):
			began = monotonic()
			if _local(SAMPLE_SPENT, float) >= SAMPLE_BUDGET_S:
				break
			agrees = (name in result.visible) == bool(_per_doc(doctype, {name}, ptype))
			checked += 1
			setattr(frappe.local, SAMPLE_SPENT, _local(SAMPLE_SPENT, float) + (monotonic() - began))
			if not agrees:
				_resolve_mismatch(doctype, names, ptype, plan, result, name, record)
	finally:
		if record is not None:
			record["samples"] = checked


def _sample_names(doctype: str, names: set[str], result: EvalResult) -> list[str]:
	"""Up to `SAMPLE_PER_DOCTYPE` names: first those of a non-default branch, then a stride that turns per run."""
	chosen = [name for name in dict.fromkeys(result.flagged) if name in names][:SAMPLE_PER_DOCTYPE]
	rest = [name for name in sorted(names) if name not in set(chosen)]
	need = SAMPLE_PER_DOCTYPE - len(chosen)
	if need > 0 and rest:
		runs = _local(SAMPLE_RUNS, dict)
		turn = runs.get(doctype, 0)
		runs[doctype] = turn + 1
		turn += int(sha256(_job_id().encode()).hexdigest(), 16) % 9973  # the start differs from job to job
		step = max(1, len(rest) // need)
		picks = sorted({(turn + position * step) % len(rest) for position in range(need)})
		chosen += [rest[index] for index in picks]
	return chosen


def _resolve_mismatch(doctype, names, ptype, plan, result, name, record) -> NoReturn:
	"""Re-check one mismatch; always raises: confirmed (trip) or transient (counted), never a result."""
	if record is not None:
		record["mismatches"] += 1
	# Name-free facts of the sampled document: its branch class, and which side allowed it.
	branch = "special" if name in result.flagged else "plain"
	direction = "batch_only" if name in result.visible else "per_doc_only"
	trace = f"branch={branch} direction={direction} {_trace()}"
	try:
		again = _evaluate_batch(
			doctype, names, ptype, plan, chunks=result.chunks, fresh=True, count_branches=False
		)
		per_doc = bool(_per_doc(doctype, {name}, ptype))
	except Exception as error:
		if _is_stop_error(error):
			_confirm(doctype, "mismatch_unresolved", record, trace, error)
		raise
	if (name in again.visible) != per_doc:
		_confirm(doctype, "mismatch", record, trace)
	_transient(doctype, record, trace)


def _is_stop_error(error: Exception) -> bool:
	"""True only for the error that the active stop makes, once its time has passed.

	Any other error (a database error that comes just after the stop time) is not a stop: it propagates as it is.
	"""
	stop = _STOP.get()
	return stop is not None and monotonic() > stop[0] and isinstance(error, type(stop[1]()))


def _transient(doctype: str, record, trace: str) -> NoReturn:
	"""A mismatch that the re-check does not repeat: count it; the limit per doctype and site day trips."""
	try:
		from jarvis.payment_calendar import routing

		count = read_log.site_count("permission_mismatch_transient:" + doctype, routing._today())
	except TIMEOUTS:
		raise
	except Exception:
		_confirm(doctype, "transient_unknown", record, trace)
	if count >= TRANSIENT_LIMIT:
		_confirm(doctype, "transient_limit", record, trace)
	queue_event(EVENT_MISMATCH, f"permission_mismatch_transient: doctype={doctype} {trace}")
	_refuse(record, "mismatch_transient", None)


def _confirm(doctype: str, reason: str, record, trace: str, stop_error: Exception | None = None) -> NoReturn:
	"""A confirmed mismatch: trip the breaker, queue the event, count it, then refuse (or re-raise the stop's error)."""
	_trip(doctype, reason)
	queue_event(
		EVENT_MISMATCH, f"permission batch mismatch confirmed: doctype={doctype} reason={reason} {trace}"
	)
	_count_day("permission_mismatch")
	_count_day("breaker_trip")
	_refuse(record, reason, stop_error)


def _refuse(record, refusal: str, stop_error: Exception | None) -> NoReturn:
	from jarvis.payment_calendar import routing

	if record is not None:
		record["permission_refusal"] = refusal
	routing._poison_view()
	if stop_error is not None:
		raise stop_error
	raise _source_changed(DETAIL_MISMATCH)


def _trip(doctype: str, reason: str) -> None:
	"""The latch, in this order: the Redis key (no TTL), the site config at 0, the trip flag.

	A failed write is queued as the event `breaker_write_failed:<step>`; the other steps still run. The trip
	turns the batch off for the whole site; the flag lasts for this job or request only. The flag is set even
	when a timeout cuts the writes short. One ERROR line goes to the log file at once, before any write.
	"""
	try:
		frappe.logger("payment_calendar").error(
			f"permission_batch_trip doctype={doctype} reason={reason} {_trace()}"
		)
	except Exception:
		pass
	try:
		_trip_writes(doctype, reason)
	finally:
		setattr(frappe.local, TRIPPED, True)


def _trip_writes(doctype: str, reason: str) -> None:
	# The config write is pending from the first step: a timeout in any step below still leaves it to the job's
	# `finally` (`write_pending_config`). It is cleared only once the write was tried.
	setattr(frappe.local, CONFIG_PENDING, True)
	try:
		_set_breaker_key(doctype, reason)
	except TIMEOUTS:
		raise
	except Exception:
		queue_event(EVENT_BREAKER, "breaker_write_failed:breaker_key")
	if _in_job() and _stop_is_near():
		# The write may not finish before the stop: the latch key stops every doctype meanwhile, and the job
		# writes the config in its `finally`, after the view, with no stop pressure.
		queue_event(EVENT_BREAKER, "breaker_write_deferred:site_config")
		_remember_failed_latch()
		return
	_write_config()
	setattr(frappe.local, CONFIG_PENDING, False)


def _write_config() -> bool:
	"""The site-config write of a trip: True when it is done. A failure leaves the latch key and queues an event."""
	try:
		frappe.installer.update_site_config(SWITCH, 0)
		return True
	except TIMEOUTS:
		raise
	except Exception:
		queue_event(EVENT_BREAKER, "breaker_write_failed:site_config")
		_remember_failed_latch()
		return False


def write_pending_config() -> None:
	"""Do the site-config write that a trip near the job stop deferred. `jobs.run_snapshot` calls it last.

	Done: the latch key is deleted (the config now stops the batch for good). Failed: the latch key stays and
	the event `breaker_write_failed:site_config` is queued (written at once: the view has ended).
	"""
	if not getattr(frappe.local, CONFIG_PENDING, False):
		return
	setattr(frappe.local, CONFIG_PENDING, False)
	if _write_config():
		try:
			frappe.cache.execute_command("DEL", frappe.cache.make_key(BREAKER_KEY + LATCH_FAILED))
		except TIMEOUTS:
			raise
		except Exception:
			pass


def _stop_is_near() -> bool:
	"""Less than `CONFIG_WRITE_MARGIN_S` seconds before the active stop: the site-config write may not finish."""
	stop = _STOP.get()
	return stop is not None and stop[0] - monotonic() < CONFIG_WRITE_MARGIN_S


def _set_breaker_key(doctype: str, reason: str) -> None:
	stamp = datetime.now(timezone.utc).isoformat()
	frappe.cache.execute_command("SET", frappe.cache.make_key(BREAKER_KEY + doctype), f"{stamp}|{reason}")


def _remember_failed_latch() -> None:
	try:
		frappe.cache.execute_command("SET", frappe.cache.make_key(BREAKER_KEY + LATCH_FAILED), "1")
	except TIMEOUTS:
		raise
	except Exception:
		pass


def reset_breaker(doctype: str | None = None) -> dict:
	"""Delete the breaker key of `doctype`, or every breaker key, and the transient counters of those doctypes.

	With no `doctype` the failed-latch flag goes too. It does NOT turn the switch on, and it does not touch
	the site config. Returns what it deleted (doctype names and counter classes, no document or user names)
	and writes one audit line to the log file with the doctype and the keyed digest of the session user.
	"""
	if doctype is not None:
		keys = [frappe.cache.make_key(BREAKER_KEY + doctype)]
	else:
		keys = _breaker_keys()
	prefix = frappe.cache.make_key(BREAKER_KEY).decode()
	deleted = {"breakers": [], "latch_write_failed": False, "transient_counters": []}
	for key in keys:
		if frappe.cache.execute_command("DEL", key):
			name = key.decode()[len(prefix) :]
			if name == LATCH_FAILED:
				deleted["latch_write_failed"] = True
			else:
				deleted["breakers"].append(name)
	deleted["transient_counters"] = _delete_transient_counters(doctype)
	deleted["breakers"].sort()
	try:
		frappe.logger("payment_calendar").info(
			f"permission_batch_reset doctype={doctype or '-'} user={_user_digest()} "
			f"breakers={len(deleted['breakers'])} counters={len(deleted['transient_counters'])}"
		)
	except Exception:
		pass
	return deleted


def _delete_transient_counters(doctype: str | None) -> list[str]:
	"""The day counters of transient mismatches (every day still kept) of one doctype or of all: deleted."""
	pattern = frappe.cache.make_key(
		"jarvis:pcal:site:day:*:permission_mismatch_transient:" + (doctype or "*")
	)
	deleted = set()
	for key in _scan(pattern):
		if frappe.cache.execute_command("DEL", key):
			deleted.add(_counter_class(key))
	return sorted(deleted)


def _counter_class(key: bytes) -> str:
	"""`permission_mismatch_transient:<doctype>` of a site day counter key."""
	text = key.decode()
	return text[text.index("permission_mismatch_transient:") :]


def _scan(pattern: bytes) -> list[bytes]:
	"""Every key that matches `pattern`, found with SCAN (never the blocking KEYS)."""
	cursor, found = b"0", []
	while True:
		cursor, keys = frappe.cache.execute_command("SCAN", cursor, "MATCH", pattern, "COUNT", 500)
		found += keys
		if int(cursor) == 0:
			return found


def _breaker_keys() -> list[bytes]:
	"""Every key under the breaker prefix."""
	return _scan(frappe.cache.make_key(BREAKER_KEY + "*"))


def status(user: str | None = None) -> dict:
	"""Operator view of this process: the switch, the breakers, the failed latch, drift, identity, counters, plans.

	Run with `bench --site <site> execute jarvis.payment_calendar.batch_permission.status`. It reads only:
	`counters` are today's site counters (a GET each, nothing is counted), and `plans` is the plan mode and
	reason of each doctype of `EXPECT_BATCH` for `user` (default: the session user), which shows a doctype that
	stays per document for a reason that nothing else reports. Names of documents or users never appear.
	"""
	prefix = frappe.cache.make_key(BREAKER_KEY).decode()
	breakers, latch = [], False
	for key in sorted(_breaker_keys()):
		doctype = key.decode()[len(prefix) :]
		if doctype == LATCH_FAILED:
			latch = True
			continue
		stamp, _, reason = _text(frappe.cache.execute_command("GET", key)).partition("|")
		breakers.append({"doctype": doctype, "reason": reason, "set_at": stamp})
	return {
		"switch": _switch_value(),
		"breakers": breakers,
		"latch_write_failed": latch,
		"pin_drift": _compat_failures("permission_pin_drift"),
		"identity_failures": _identity_failures(),
		"counters": _today_counters(),
		"plans": _plans_for(user or frappe.session.user),
	}


def _today_counters() -> dict[str, int]:
	"""Today's site counters (`permission_batch:<reason>`, `permission_mismatch`...), read without counting."""
	from jarvis.payment_calendar import routing

	head = frappe.cache.make_key(f"jarvis:pcal:site:day:{routing._today()}:").decode()
	counters = {}
	for key in sorted(_scan(frappe.cache.make_key(f"jarvis:pcal:site:day:{routing._today()}:*"))):
		klass = key.decode()[len(head) :]
		if klass.startswith(("permission_", "breaker_")):
			counters[klass] = int(_text(frappe.cache.execute_command("GET", key)) or 0)
	return counters


def _plans_for(user: str) -> dict[str, dict]:
	"""The plan of each `EXPECT_BATCH` doctype for `user`; the plan's own events are dropped, not written."""
	original = frappe.session.user
	plans = {}
	with _quiet_events():
		try:
			frappe.set_user(user)
			setattr(frappe.local, PLAN_MEMO, None)
			for doctype in EXPECT_BATCH:
				plan = batch_plan(
					doctype
				)  # a doctype that cannot be planned is the per-document plan `plan_error`
				plans[doctype] = {"mode": plan.mode, "reason": plan.reason}
		finally:
			frappe.set_user(original)
			setattr(frappe.local, PLAN_MEMO, None)
	return plans


@contextmanager
def _quiet_events() -> Iterator[list]:
	"""Events are only recorded in this block, never written; the ones it recorded are removed and yielded."""
	events = _local(EVENTS, list)
	kept, previous, dropped = len(events), getattr(frappe.local, QUIET, False), []
	setattr(frappe.local, QUIET, True)
	try:
		yield dropped
	finally:
		setattr(frappe.local, QUIET, previous)
		dropped.extend(events[kept:])
		del events[kept:]


def _text(value) -> str:
	return value.decode() if isinstance(value, bytes) else str(value or "")


def _identity_failures() -> list[str]:
	failures: set[str] = set()
	try:
		for doctype in EXPECT_BATCH:
			lazy = frappe.model.document.get_lazy_controller(doctype)
			children = [
				frappe.model.document.get_controller(df.options)
				for df in frappe.get_meta(doctype).get_table_fields(include_computed=True)
			]
			failures.update(_compat_failures("permission_identity_failures", lazy, children))
	except TIMEOUTS:
		raise
	except Exception as error:
		failures.add(f"identity check failed: {type(error).__name__}")
	return sorted(failures)


# --- pilot differential -------------------------------------------------------------------


def differential(
	users=None,
	*,
	names: dict | None = None,
	expect_batch=EXPECT_BATCH,
	time_box_s: int = DIFFERENTIAL_BOX_S,
	show_users: bool = False,
) -> dict:
	"""Compare the batch with Frappe's per-document check for real users, read-only.

	Run it in its own `bench execute`, in a process where no other app has wrapped a Frappe permission
	function (the identity check fails otherwise). `users` is a list (a string is a `TypeError`).

	For each user (`frappe.set_user` in a try/finally) and each doctype in `expect_batch`: the metadata plan,
	then `_evaluate_batch` directly, never `batch_visible`, `_live_gate`, the sample or the trip. So the switch
	value and the breaker keys do not change what it runs, and it writes no breaker key, site config, day
	counter or Error Log row. It rolls back at the end.

	The compared set per user and doctype is `names[user][doctype]` (the names the calendar passes to
	`visible_names`, captured by the pilot script) plus the documents that `_selected_names` finds by SQL:
	owned by the user, shared with the user or everyone, a relevant child row outside the user's allowed values
	or empty, a link to a master restricted to companies. A selector that hits `DIFFERENTIAL_NAME_LIMIT` is
	listed in `names_cut` (the gate does not pass then).

	Output: `users` (per doctype: mode, reason, batch_evaluated, compared, supplied, selected, differences,
	branches), `gate_failed` (a doctype that is missing, did not run as `batch`, had names and
	`batch_evaluated == 0`, or was compared for no user: `not_exercised`), `not_compared` (Administrator, Guest
	and users with the reason `scope_users`), `not_done` (users the time box did not reach, checked between
	users and between doctypes), `names_cut`, `differences`, `elapsed_s`, `gate_passed` (no failure, no
	difference, no cut, every user done, at least one user compared) and `plan_events` (the pin-drift and
	identity events the plans queued: reported here, never written). Users are shown by a keyed digest; with
	`show_users=True` by their id.
	"""
	if users is not None and not isinstance(users, (list, tuple)):
		raise TypeError("users must be a list of user ids")
	began, original = monotonic(), frappe.session.user
	out = {
		"users": {},
		"gate_failed": [],
		"not_compared": [],
		"not_done": [],
		"names_cut": [],
		"differences": 0,
	}
	supplied = names or {}
	listed = list(users) if users is not None else _differential_users()
	try:
		with _quiet_events() as plan_events:
			for position, user in enumerate(listed):
				label = _user_label(user, position, show_users)
				if user in ("Administrator", "Guest"):
					out["not_compared"].append(label)
				elif monotonic() - began > time_box_s or not _differential_user(
					user,
					label,
					supplied.get(user) or {},
					expect_batch,
					out,
					lambda: monotonic() - began > time_box_s,
				):
					out["not_done"].append(label)
	finally:
		frappe.set_user(original)
		setattr(frappe.local, PLAN_MEMO, None)
		_rollback_quietly()
	out["plan_events"] = plan_events
	_gate_not_exercised(out, expect_batch)
	out["elapsed_s"] = round(monotonic() - began, 3)
	out["gate_passed"] = not (out["gate_failed"] or out["differences"] or out["names_cut"] or out["not_done"])
	return out


def _user_label(user: str, position: int, show_users: bool) -> str:
	"""How a user appears in the output: the id when asked for, else a keyed digest (`user-<n>` without a key)."""
	if show_users:
		return user
	try:
		return read_log.ops_digest(user)
	except TIMEOUTS:
		raise
	except Exception:
		return f"user-{position}"


def _rollback_quietly() -> None:
	"""End the read-only transaction; an error here must not hide the error that is already on its way."""
	try:
		frappe.db.rollback()
	except TIMEOUTS:
		raise
	except Exception as error:
		frappe.logger("payment_calendar").error(
			f"permission_differential_rollback_failed error={type(error).__name__}"
		)


def _gate_not_exercised(out: dict, expect_batch) -> None:
	"""A pass that compared nothing is no pass: a doctype that no user had names for, or no user at all."""
	failed = {row["doctype"] for row in out["gate_failed"]}
	rows = [row for per_user in out["users"].values() for row in per_user.values()]
	if not rows:
		out["gate_failed"].append({"user": "-", "doctype": "-", "reason": "not_exercised"})
		return
	for doctype in expect_batch:
		compared = [
			per_user[doctype]["compared"] for per_user in out["users"].values() if doctype in per_user
		]
		if doctype not in failed and not any(compared):
			out["gate_failed"].append({"user": "-", "doctype": doctype, "reason": "not_exercised"})


def _differential_user(user: str, label: str, supplied: dict, expect_batch, out: dict, over_the_box) -> bool:
	"""Run the doctypes of one user; False when the time box ended before the last one."""
	frappe.set_user(user)
	setattr(frappe.local, PLAN_MEMO, None)
	rows = out["users"][label] = {}
	for position, doctype in enumerate(expect_batch):
		if position and over_the_box():
			return False
		plan = batch_plan(doctype)  # never raises for a missing doctype: its plan is `per_doc` `plan_error`
		if plan.reason == "scope_users":
			if label not in out["not_compared"]:
				out["not_compared"].append(label)
			continue
		selected, cut = _selected_names(user, doctype) if plan.mode == "batch" else (set(), [])
		given = set(supplied.get(doctype) or ())
		row = rows[doctype] = _differential_row(doctype, selected | given, plan)
		row.update(supplied=len(given), selected=len(selected))
		out["differences"] += row["differences"]
		out["names_cut"] += [{"user": label, "doctype": doctype, "selector": selector} for selector in cut]
		failure = _gate_failure(plan, row, len(selected | given))
		if failure:
			out["gate_failed"].append({"user": label, "doctype": doctype, "reason": failure})
	return True


def _differential_row(doctype: str, names: set[str], plan: BatchPlan) -> dict:
	row = {
		"mode": plan.mode,
		"reason": plan.reason,
		"batch_evaluated": 0,
		"compared": 0,
		"differences": 0,
		"branches": {},
	}
	if plan.mode != "batch":
		return row
	ptype = "select" if frappe.only_has_select_perm(doctype) else "read"
	result = _evaluate_batch(doctype, names, ptype, plan)
	expected = _per_doc(doctype, names, ptype)
	row.update(
		batch_evaluated=result.batch_evaluated,
		compared=len(names),
		differences=len(result.visible ^ expected),
		branches=dict(result.branches),
	)
	return row


def _gate_failure(plan: BatchPlan, row: dict, names: int) -> str | None:
	if plan.mode != "batch":
		return plan.reason or "per_doc"
	if names and row["batch_evaluated"] == 0:
		return "not_evaluated"
	return None


def _differential_users() -> list[str]:
	"""Every user with a User Permission (last login first) and five without one."""
	with_permissions = frappe.get_all("User Permission", pluck="user", distinct=True)
	enabled = {"enabled": 1, "user_type": "System User"}
	skipped = ["Administrator", "Guest"]
	restricted = frappe.get_all(
		"User",
		filters={**enabled, "name": ["in", with_permissions or [""]]},
		pluck="name",
		order_by="last_login desc",
	)
	free = frappe.get_all(
		"User",
		filters={**enabled, "name": ["not in", [*with_permissions, *skipped]]},
		pluck="name",
		order_by="last_login desc",
		limit=5,
	)
	return [user for user in dict.fromkeys([*restricted, *free]) if user not in skipped]


def _selected_names(user: str, doctype: str) -> tuple[set[str], list[str]]:
	"""The special documents for one user and doctype (the differential's compared set), by SELECT with parameters only.

	Selectors: `owner`; `shared` (a read share for the user or for everyone); `child_row` (a child row of a
	table that a User Permission can deny, whose value is outside the user's allowed values or empty);
	`restricted_master` (a link to a master with `restrict_to_companies = 1`, or such a master itself).
	Returns the names and the selectors that hit `DIFFERENTIAL_NAME_LIMIT` (their names are cut).
	"""
	permissions = frappe.permissions.get_user_permissions(user)
	layout = _Layout(doctype, set(permissions))
	allowed = tuple(
		{
			entry["doc"]
			for entry in permissions.get("Company", [])
			if not entry.get("applicable_for") or entry.get("applicable_for") == doctype
		}
	)
	found: set[str] = set()
	cut: list[str] = []
	for selector, query, values in _selector_queries(user, doctype, layout, allowed):
		rows = frappe.db.sql(query + " LIMIT %(limit)s", {**values, "limit": DIFFERENTIAL_NAME_LIMIT + 1})
		if len(rows) > DIFFERENTIAL_NAME_LIMIT:
			rows = rows[:DIFFERENTIAL_NAME_LIMIT]
			cut.append(selector)
		found.update(row[0] for row in rows)
	return found, cut


def _selector_queries(user: str, doctype: str, layout, allowed: tuple) -> Iterator[tuple[str, str, dict]]:
	parent = layout.quoted
	yield "owner", f"SELECT `name` FROM {parent} WHERE `owner` = %(user)s", {"user": user}
	yield (
		"shared",
		f"SELECT DISTINCT s.`share_name` FROM {_quoted('DocShare')} s WHERE s.`share_doctype` = %(doctype)s "
		"AND s.`read` = 1 AND (s.`user` = %(user)s OR s.`everyone` = 1)",
		{"doctype": doctype, "user": user},
	)
	if allowed:
		for table in layout.relevant:
			for field in table.up_links:
				yield (
					"child_row",
					f"SELECT DISTINCT c.`parent` FROM {table.quoted} c WHERE c.`parenttype` = %(doctype)s "
					f"AND c.`parentfield` = %(field)s AND (c.`{field}` IS NULL OR c.`{field}` = '' "
					f"OR c.`{field}` NOT IN %(allowed)s)",
					{"doctype": doctype, "field": table.fieldname, "allowed": allowed},
				)
	yield from _restricted_queries(doctype, layout)


def _restricted_queries(doctype: str, layout) -> Iterator[tuple[str, str, dict]]:
	chain = _hook_chain(doctype)
	if _PARTY_HOOK in chain and "restrict_to_companies" in layout.columns:
		yield "restricted_master", f"SELECT `name` FROM {layout.quoted} WHERE `restrict_to_companies` = 1", {}
	if _INHERITED_HOOK not in chain:
		return
	for link in _master_links(layout):
		master = _quoted(link.doctype)
		if "restrict_to_companies" not in _table_columns(master.strip("`")):
			continue
		kind = f" AND p.`{link.doctype_fieldname}` = %(master)s" if link.doctype_fieldname else ""
		yield (
			"restricted_master",
			f"SELECT DISTINCT p.`name` FROM {layout.quoted} p JOIN {master} m ON p.`{link.fieldname}` = m.`name` "
			f"WHERE m.`restrict_to_companies` = 1{kind}",
			{"master": link.doctype} if kind else {},
		)
