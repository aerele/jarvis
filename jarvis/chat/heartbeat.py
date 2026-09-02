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


def bench_liveness_vector() -> dict:
	"""The dead-man's-switch payload the control plane ingests + reasons over."""
	now = frappe.utils.now()
	return {
		"watchdog_last_completed_age_s": _watchdog_age_s(now),
		"oldest_nonterminal_turn_age_s": _oldest_nonterminal_turn_age_s(now),
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
