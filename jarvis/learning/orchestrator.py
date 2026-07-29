"""Pattern-learning orchestrator: the */15 cron tick (plan section 5.2).

``tick()`` is the app-static cron entry (per-site cron rows are impossible;
they are reset on migrate). It is intentionally cheap and self-enforcing:

1. Bail on the site_config kill switch, the disabled flag, an un-onboarded
   account, or a time outside the analysis window (wrap-aware, site-tz).
2. Stale-run watch: a Running run whose heartbeat predates the current
   window (or is >20 min old), and a Queued run that never got a worker
   (queued >20 min), are marked Failed with a coverage note. This is the
   simple Phase 1 recovery: aggregates recompute nightly, so there is no
   resume-cursor machinery (plan section 5.6).
3. Continue an unfinished night: if a scheduled run is Paused (window-end,
   worker-timeout or row-budget stop), re-enqueue it under a DISTINCT per-hop
   job_id and advance ``pattern_next_run_at`` (the resume path).
4. New run: if ``pattern_next_run_at`` is due and no run is open, create a
   ``Jarvis Pattern Run`` and enqueue the engine on queue ``long`` under a
   deterministic job_id, then advance ``pattern_next_run_at`` to the next
   window start strictly after now.

The driver is ONE job per night that loops internally. The tick NEVER
re-enqueues a STARTED job under the same id (frappe's job-id dedup drops it,
plan section 5.2); the engine loops over work units itself and pauses
gracefully. A genuine continuation (paused run) uses a fresh ``::hop<ts>``
id so it is not deduped away.
"""

from __future__ import annotations

import datetime

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

RUN = "Jarvis Pattern Run"
SETTINGS = "Jarvis Settings"

ENGINE_METHOD = "jarvis.learning.engine.run_pattern_analysis"
QUEUE = "long"

# Matches the engine's redis-lock TTL and the internal soft budget. The RQ
# job timeout is set to this so a wedged run self-releases (plan section 5.2:
# redis_lock timeout_s=1500).
WORKER_TIMEOUT_S = 1500

# The engine pauses this many minutes before window end so it never straddles
# the boundary mid-write. Exposed for the engine's per-unit check.
WINDOW_SAFETY_MARGIN_MIN = 5

# Stale thresholds for the recovery watch.
STALE_RUN_MINUTES = 20  # Running with no heartbeat this long -> Failed
QUEUE_STALE_MINUTES = 20  # Queued with no worker this long -> Failed (no worker time)

_OPEN_STATUSES = ("Queued", "Running", "Paused")


# --------------------------------------------------------------------------- #
# cron entry
# --------------------------------------------------------------------------- #
def tick() -> None:
	"""*/15 cron entry. No-op unless pattern learning is enabled AND in-window
	for this site. Runs as Administrator (the scheduler user), default queue."""
	if frappe.conf.get("jarvis_pattern_learning_disabled"):
		return
	if not _feature_enabled():
		return
	if not _is_onboarded():
		return

	now = now_datetime()
	window_start = _settings_value("pattern_window_start")
	window_end = _settings_value("pattern_window_end")

	# Stale-run watch runs whenever the feature is enabled (cleanup happens
	# even outside the window). Never let a recovery hiccup block scheduling.
	try:
		_fail_stale_runs(now, window_start)
	except Exception:
		frappe.log_error(
			title="jarvis pattern learning: stale-run watch failed",
			message=frappe.get_traceback(),
		)

	# Scheduled work only starts inside the window (manual runs use run_now,
	# which bypasses this gate).
	if not in_window(window_start, window_end, now):
		return

	try:
		_schedule_in_window(now, window_start, window_end)
	except Exception:
		frappe.log_error(
			title="jarvis pattern learning: tick scheduling failed",
			message=frappe.get_traceback(),
		)


def _schedule_in_window(now, window_start, window_end) -> None:
	open_runs = frappe.get_all(
		RUN,
		filters={"status": ["in", list(_OPEN_STATUSES)]},
		fields=["name", "status", "trigger", "creation"],
		order_by="creation asc",
	)
	# Something is already queued/running: the engine loops internally, so we
	# never re-hop it (frappe would dedup a STARTED job anyway).
	if any(r.status in ("Queued", "Running") for r in open_runs):
		return

	# Continue an unfinished night before opening a fresh run.
	paused = [r for r in open_runs if r.status == "Paused"]
	if paused:
		_resume_run(paused[0].name, now, window_start)
		return

	# No open run: start tonight's run if the schedule is due.
	next_run_at = _settings_value("pattern_next_run_at")
	if next_run_at is None or get_datetime(next_run_at) <= now:
		_create_and_enqueue_run(now, window_start, window_end)


# --------------------------------------------------------------------------- #
# manual "Run now" (Wave C wires the SM endpoint to this)
# --------------------------------------------------------------------------- #
def run_now(requested_by: str) -> dict:
	"""Create a manual ``Jarvis Pattern Run`` and enqueue the engine. Manual
	runs BYPASS the window (plan section 5.2, trigger-aware) but keep the row
	budget + statement timeouts. Refuses when disabled.

	Returns ``{"ok": bool, "run": <name>|None, "reason": <str>|None}``."""
	if frappe.conf.get("jarvis_pattern_learning_disabled"):
		return {"ok": False, "run": None, "reason": "pattern learning is disabled for this site"}
	if not _feature_enabled():
		return {"ok": False, "run": None, "reason": "pattern learning is not enabled"}

	# Coalesce with an in-flight run: the engine single-flights on a redis lock
	# anyway, but avoid stacking redundant manual run rows.
	open_active = frappe.get_all(
		RUN, filters={"status": ["in", ["Queued", "Running"]]}, fields=["name"], limit=1
	)
	if open_active:
		return {"ok": False, "run": open_active[0].name, "reason": "a run is already in progress"}

	window_start = _settings_value("pattern_window_start")
	window_end = _settings_value("pattern_window_end")
	run = frappe.get_doc(
		{
			"doctype": RUN,
			"status": "Queued",
			"trigger": "manual",
			"requested_by": requested_by,
			"window_start_used": window_start,
			"window_end_used": window_end,
			"scan_mode": "full",
		}
	)
	run.flags.ignore_permissions = True
	run.insert()

	job_id = f"jarvis_pattern_run::{run.name}"
	frappe.enqueue(
		ENGINE_METHOD,
		queue=QUEUE,
		timeout=WORKER_TIMEOUT_S,
		enqueue_after_commit=True,
		job_id=job_id,
		deduplicate=True,
		run_name=run.name,
	)
	return {"ok": True, "run": run.name, "reason": None}


# --------------------------------------------------------------------------- #
# run creation / resume
# --------------------------------------------------------------------------- #
def _create_and_enqueue_run(now, window_start, window_end) -> str:
	run = frappe.get_doc(
		{
			"doctype": RUN,
			"status": "Queued",
			"trigger": "scheduled",
			"window_start_used": window_start,
			"window_end_used": window_end,
			"scan_mode": "full",
		}
	)
	run.flags.ignore_permissions = True
	run.insert()

	job_id = f"jarvis_pattern_run::{run.name}"
	frappe.enqueue(
		ENGINE_METHOD,
		queue=QUEUE,
		timeout=WORKER_TIMEOUT_S,
		enqueue_after_commit=True,
		job_id=job_id,
		deduplicate=True,
		run_name=run.name,
	)
	# Advance ONLY after a successful enqueue registration; the enqueue and
	# both set_values commit together at the end of the tick, firing the
	# after-commit push. A crash before that commit rolls everything back
	# (no orphan run, no lost pointer).
	_advance_next_run_at(now, window_start)
	return run.name


def _resume_run(run_name: str, now, window_start) -> None:
	"""Re-enqueue a Paused run under a DISTINCT per-hop job_id (the same id
	would be deduped). Flip it to Queued so the next tick sees an in-flight run
	and does not double-enqueue; advance next_run_at (the resume path, so a run
	spanning nights never triggers a duplicate same-night run)."""
	frappe.db.set_value(RUN, run_name, {"status": "Queued"}, update_modified=False)
	job_id = f"jarvis_pattern_run::{run_name}::hop{int(now.timestamp())}"
	frappe.enqueue(
		ENGINE_METHOD,
		queue=QUEUE,
		timeout=WORKER_TIMEOUT_S,
		enqueue_after_commit=True,
		job_id=job_id,
		deduplicate=True,
		run_name=run_name,
	)
	_advance_next_run_at(now, window_start)


def _advance_next_run_at(now, window_start) -> None:
	frappe.db.set_value(
		SETTINGS,
		SETTINGS,
		{"pattern_next_run_at": compute_next_window_start(window_start, now)},
		update_modified=False,
	)


# --------------------------------------------------------------------------- #
# stale-run recovery (plan section 5.6)
# --------------------------------------------------------------------------- #
def _fail_stale_runs(now, window_start) -> None:
	threshold = _stale_threshold(now, window_start)
	queue_cutoff = add_to_date(now, minutes=-QUEUE_STALE_MINUTES)

	for r in frappe.get_all(
		RUN,
		filters={"status": "Running"},
		fields=["name", "heartbeat_at", "started_at"],
	):
		beat = r.heartbeat_at or r.started_at
		stale = beat is None or get_datetime(beat) < threshold
		if stale:
			last = str(beat) if beat else "never"
			_mark_run_failed(
				r.name,
				now,
				f"Marked Failed by the stale-run watch: no heartbeat since {last} "
				f"(before the current window start or older than {STALE_RUN_MINUTES} min). "
				f"Aggregates recompute on the next nightly run.",
			)

	# Queued with no worker time: distinct from "no patterns found" (plan
	# section 5.3). Fresh runs (< QUEUE_STALE_MINUTES old) are left alone.
	# A run RESUMED after a window pause is re-queued but keeps its original
	# creation and already carries started_at; keying the stale check off
	# creation would kill it seconds after re-enqueue under long-queue
	# contention. Only fail runs that have never worked (started_at IS NULL).
	for r in frappe.get_all(
		RUN,
		filters={"status": "Queued", "started_at": ["is", "not set"]},
		fields=["name", "creation"],
	):
		if r.creation and get_datetime(r.creation) < get_datetime(queue_cutoff):
			_mark_run_failed(
				r.name,
				now,
				f"Marked Failed: queued for over {QUEUE_STALE_MINUTES} min with no worker time "
				f"(long-queue contention or a lost enqueue). This is not 'no patterns found'.",
			)


def _mark_run_failed(run_name: str, now, note: str) -> None:
	frappe.db.set_value(
		RUN,
		run_name,
		{"status": "Failed", "ended_at": now, "coverage_note": note[:1000]},
		update_modified=False,
	)


def _stale_threshold(now, window_start) -> datetime.datetime:
	"""A Running run's heartbeat older than this is stale: the later of the
	most recent past window start and (now - STALE_RUN_MINUTES)."""
	start_secs = _time_to_seconds(window_start)
	recent_start = _at_time(now, start_secs)
	if recent_start > now:
		recent_start = add_to_date(recent_start, days=-1)
	minus = add_to_date(now, minutes=-STALE_RUN_MINUTES)
	return max(recent_start, minus)


# --------------------------------------------------------------------------- #
# window math (pure, wrap-aware) - unit tested
# --------------------------------------------------------------------------- #
def in_window(window_start, window_end, now) -> bool:
	"""True iff ``now`` is within [start, end) for the site. Wrap-aware: when
	start > end the window crosses midnight. Start is inclusive, end exclusive.
	A degenerate start == end reads as an empty window (scheduled work never
	fires; misconfiguration fails safe, manual runs still work)."""
	start_s = _time_to_seconds(window_start)
	end_s = _time_to_seconds(window_end)
	now_s = _time_to_seconds(now)
	if start_s <= end_s:
		return start_s <= now_s < end_s
	# crosses midnight: in window from start..24h and 0..end
	return now_s >= start_s or now_s < end_s


def should_pause_for_window(
	window_start, window_end, now, margin_min: int = WINDOW_SAFETY_MARGIN_MIN
) -> bool:
	"""Engine per-unit check: stop when out of window OR within ``margin_min``
	of window end, so a unit's writes never straddle the boundary."""
	if not in_window(window_start, window_end, now):
		return True
	secs_left = _seconds_until_window_end(window_end, now)
	return secs_left <= margin_min * 60


def compute_next_window_start(window_start, from_dt) -> datetime.datetime:
	"""The next datetime at ``window_start`` strictly after ``from_dt``."""
	base = get_datetime(from_dt)
	cand = _at_time(base, _time_to_seconds(window_start))
	while cand <= base:
		cand = add_to_date(cand, days=1)
	return cand


def _seconds_until_window_end(window_end, now) -> float:
	"""Seconds from ``now`` until the next occurrence of the window-end time
	(strictly after now)."""
	base = get_datetime(now)
	end_dt = _at_time(base, _time_to_seconds(window_end))
	if end_dt <= base:
		end_dt = add_to_date(end_dt, days=1)
	return (end_dt - base).total_seconds()


def _at_time(dt: datetime.datetime, secs: int) -> datetime.datetime:
	secs = int(secs) % 86400
	return dt.replace(hour=secs // 3600, minute=(secs % 3600) // 60, second=secs % 60, microsecond=0)


def _time_to_seconds(value) -> int:
	"""Seconds since midnight for a Time (timedelta from the DB), a
	datetime/time, or an 'HH:MM:SS' string. None -> 0."""
	if value is None:
		return 0
	if isinstance(value, datetime.timedelta):
		return int(value.total_seconds()) % 86400
	if isinstance(value, datetime.datetime):
		return value.hour * 3600 + value.minute * 60 + value.second
	if isinstance(value, datetime.time):
		return value.hour * 3600 + value.minute * 60 + value.second
	parts = str(value).split(":")
	try:
		h = int(parts[0])
		m = int(parts[1]) if len(parts) > 1 else 0
		s = int(float(parts[2])) if len(parts) > 2 else 0
		return (h * 3600 + m * 60 + s) % 86400
	except (ValueError, IndexError):
		return 0


# --------------------------------------------------------------------------- #
# settings helpers
# --------------------------------------------------------------------------- #
def _settings_value(field: str):
	return frappe.db.get_single_value(SETTINGS, field)


def _feature_enabled() -> bool:
	try:
		return bool(_settings_value("pattern_learning_enabled"))
	except Exception:
		return False


def _is_onboarded() -> bool:
	"""Gate on account.is_onboarded() when present; skip the gate (allow) if the
	helper is missing so a partial deploy does not silently disable learning."""
	try:
		from jarvis.account import is_onboarded
	except Exception:
		return True
	try:
		res = is_onboarded()
		if isinstance(res, dict):
			return bool(res.get("onboarded"))
		return bool(res)
	except Exception:
		return True
