"""GAP 1 (Track B) — the bench heartbeat emitter: the bench-side half of the
scheduler dead-man's-switch.

An UNCONDITIONAL ``*/5`` cron POSTs a small liveness VECTOR to the backend on
every tick — even with zero turns and zero errors. That unconditional cadence IS the
switch: if the site scheduler dies, the POSTs stop and the backend (Track C) alarms on the
tenant's silence. The vector also carries symptom signals so a scheduler that is alive
but whose recovery machinery is wedged is still visible:

  * ``watchdog_last_completed_age_s`` — grows if the ``pump.watchdog`` cron fails to RUN
    to completion (a top-level failure, or not scheduled) while other crons tick.
  * ``oldest_nonterminal_turn_age_s`` — grows if the ``long`` queue is wedged (hops
    cannot drain) even with a live scheduler — the symptom a bare ping would miss. This
    also catches per-shard recovery failing while the watchdog cron body still reaches
    its end (so the watchdog age stays fresh).
"""

from __future__ import annotations

import frappe

from jarvis.chat import turn_state as ts
from jarvis.chat.pump import WATCHDOG_LAST_COMPLETED_KEY
from jarvis.chat.usage_push import _admin_configured
from jarvis.exceptions import (
	AdminAuthError,
	AdminRateLimitedError,
	AdminUnreachableError,
	AdminValidationError,
)

_LOG_THROTTLE_KEY = "jarvis_bench_heartbeat_last_log_hour"
# Separate marker (under a ``persistent_cache_keys`` prefix in hooks.py) so a fault in the macro-health
# queries is logged at most once an hour without sharing the push-failure throttle.
_MACRO_HEALTH_LOG_KEY = "jarvis:heartbeat_macro_health_log_hour"

# A failed scheduled slot is retried 55 minutes later (macro_scheduler._RETRY_AFTER_S), so a
# next_run_at older than 2 hours has missed its slot AND its retry.
_MACRO_OVERDUE_AFTER_S = 2 * 3600
# Bounds the Jarvis Macro Run lookup: ``trigger`` has no index, so without a window the query
# walks every completed run. A tenant whose newest scheduled success is older than this simply
# reports no last-ok age.
_LAST_OK_WINDOW_DAYS = 30


def _watchdog_age_s(now: str | None = None) -> int | None:
	"""Seconds since ``pump.watchdog`` last completed, or None if it has never run
	(a brand-new / never-scheduled bench). None reads as 'unknown', not 'stale'."""
	last = frappe.db.get_default(WATCHDOG_LAST_COMPLETED_KEY)
	if not last:
		return None
	delta = frappe.utils.get_datetime(now or frappe.utils.now()) - frappe.utils.get_datetime(last)
	return max(0, int(delta.total_seconds()))


def _oldest_nonterminal_turn_age_s(now: str | None = None) -> int:
	"""Age (s) of the oldest still-nonterminal chat turn — a proxy for 'turns are
	stuck', which is what a wedged ``long`` queue produces. 0 when nothing is in flight.
	The ``enqueued_at is set`` filter is load-bearing: enqueued_at is nullable and
	ORDER BY sorts NULLs FIRST, so without it one NULL-enqueued nonterminal turn would
	return NULL -> age 0 -> a wedged bench reporting healthy (a blinded switch)."""
	oldest = frappe.db.get_value(
		ts.TURN,
		{"state": ["in", list(ts.NONTERMINAL_STATES)], "enqueued_at": ["is", "set"]},
		"enqueued_at",
		order_by="enqueued_at asc",
	)
	if not oldest:
		return 0
	delta = frappe.utils.get_datetime(now or frappe.utils.now()) - frappe.utils.get_datetime(oldest)
	return max(0, int(delta.total_seconds()))


def _scheduled_macro_health() -> dict:
	"""Three integers about scheduled macros: how many are armed, how many of those missed
	their slot by more than 2 hours, and seconds since the newest scheduled run completed.
	Counts only; no owner, macro name or prompt text leaves the bench.

	Clock: ``now_datetime()`` is the site clock, the one ``macro_scheduler.run_due_macros``
	compares ``next_run_at`` against. A NULL ``next_run_at`` is not overdue (``<`` never
	matches NULL). The hold field exists only after migrate, so it is probed via the
	DocType meta (``has_column`` caches and can lag a migrate). ``Jarvis Macro`` has no
	index on the schedule fields; the table is one row per saved macro, so the count is a
	small scan. ``Jarvis Macro Run.status`` is indexed and the run lookup is windowed."""
	now = frappe.utils.now_datetime()
	armed = {"enabled": 1, "schedule_enabled": 1}
	if frappe.get_meta("Jarvis Macro").has_field("admin_hold"):
		armed["admin_hold"] = 0
	health = {
		"sched_macros_enabled": frappe.db.count("Jarvis Macro", armed),
		"sched_macros_overdue": frappe.db.count(
			"Jarvis Macro",
			{**armed, "next_run_at": ["<", frappe.utils.add_to_date(now, seconds=-_MACRO_OVERDUE_AFTER_S)]},
		),
	}
	last = frappe.db.get_value(
		"Jarvis Macro Run",
		{
			"trigger": "scheduled",
			"status": "completed",
			"creation": [">=", frappe.utils.add_to_date(now, days=-_LAST_OK_WINDOW_DAYS)],
		},
		["finished_at", "modified"],
		order_by="creation desc",
	)
	if last:
		stamp = last[0] or last[1]
		health["sched_last_ok_age_s"] = max(0, int((now - frappe.utils.get_datetime(stamp)).total_seconds()))
	return health


def _scheduled_macro_health_safe() -> dict:
	"""``_scheduled_macro_health`` that never raises: on any fault the three keys are
	omitted (liveness keys must still go out) and the fault is logged at most hourly."""
	try:
		return _scheduled_macro_health()
	except Exception:
		try:
			hour = frappe.utils.now()[:13]
			if frappe.cache().get_value(_MACRO_HEALTH_LOG_KEY, expires=True) != hour:
				frappe.cache().set_value(_MACRO_HEALTH_LOG_KEY, hour, expires_in_sec=7200)
				frappe.log_error(
					title="jarvis.chat.heartbeat macro health failed", message=frappe.get_traceback()
				)
		except Exception:
			pass
		return {}


def bench_liveness_vector() -> dict:
	"""The dead-man's-switch payload the control plane ingests + reasons over."""
	now = frappe.utils.now()
	return {
		"watchdog_last_completed_age_s": _watchdog_age_s(now),
		"oldest_nonterminal_turn_age_s": _oldest_nonterminal_turn_age_s(now),
		**_scheduled_macro_health_safe(),
	}


def _log_failure_throttled() -> None:
	"""Log a push-path bug at most once per hour, so a persistent fault cannot flood the
	local Error Log every 5 minutes. NEVER raises (mirrors error_push): a Redis/DB hiccup
	in the logging path itself must not escape into the scheduler."""
	try:
		hour = frappe.utils.now()[:13]
		if frappe.cache().get_value(_LOG_THROTTLE_KEY) == hour:
			return
		frappe.cache().set_value(_LOG_THROTTLE_KEY, hour)
		frappe.log_error(title="jarvis.chat.heartbeat push failed", message=frappe.get_traceback())
	except Exception:
		pass


def push_bench_heartbeat() -> None:
	"""``*/5`` scheduler entry. Self-gating + best-effort; NEVER raises. UNCONDITIONAL:
	posts every tick whenever admin is configured (that is the point — silence means the
	scheduler is dead)."""
	try:
		if not _admin_configured():
			return
		from jarvis import admin_client

		admin_client.push_bench_heartbeat(bench_liveness_vector())
	except (AdminAuthError, AdminUnreachableError, AdminRateLimitedError, AdminValidationError):
		# Best-effort telemetry: ANY push the backend does not accept — not onboarded, admin
		# down, throttled, or a 4xx (INCLUDING the ingest endpoint not existing yet, before
		# Track C ships, and any rejected payload) — is not a bench-side actionable bug.
		# Stay silent: the backend's dead-man's-switch alarms on the ABSENCE of heartbeats, so a
		# rejected one is harmless, and logging it would spam the Error Log fleet-wide.
		return
	except Exception:
		# A genuine bug in the push path — log it, at most once an hour.
		_log_failure_throttled()
