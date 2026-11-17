"""Hourly rollup of the user-activity summary into ``Jarvis Activity Daily``.

Reading raw rows per summary cannot be interactive at real volume (about 600 events per
heavy person per day, 100 users, 90 days kept), so closed days are folded into one row
per (person, day) and the summary reads those (plan
``.claude/workflow/plans/2026-11-06-user-activity-report.md``, rev 4, D13).

Invariants this module owns (each one a past or avoided incident):

- **One counting definition.** A day is rolled with ``activity_report.day_counters``,
  the very function the live summary uses for days not rolled yet, so the two cannot
  disagree (the equivalence test pins it).
- **Strictly in order, one transaction per day.** The days between the watermark and
  yesterday are rolled oldest first; each day's DELETE + INSERT and the watermark move
  commit together, so a crash leaves the watermark at the last COMPLETE day. The first
  failing day stops the run (watermark stays at the day before it; the next run retries
  it). The summary treats every day up to the watermark as final, so a gap would
  silently drop a day from the totals.
- **The watermark is a Single, read FOR UPDATE and written with ``set_single_value``.**
  Never ``set_global``/``set_default``: for ``__global`` that ends in
  ``frappe.clear_cache(user=None)``, a full site cache clear (see ``frappe.defaults.
  set_default``), the incident class ``hooks.py`` records under ``persistent_cache_keys``.
  ``set_single_value`` clears only this document's cache.
- **Bounded.** No new day starts after ``TIME_BUDGET_S``; a site-scoped Redis claim
  keeps two runs (the scheduler's and an on-demand one the summary enqueues when it is
  behind) from interleaving. The claim key is in ``persistent_cache_keys`` so the
  periodic full cache clears cannot drop it mid-run.
- **Late rows.** A row can commit long after its ``at`` (a long transaction, a clock
  skew), so yesterday stays open for the WHOLE following day: every run re-rolls it
  (delete + insert, watermark unchanged, never twice in one run) until midnight closes the
  window, then it is final. A shorter window (it was 2 h) left a closed day permanently
  undercounted by anything that committed after it.
- **Today is rolled too, through the last full hour** (rev 4.3, D14). After the closed
  days the job recomputes today's rows for ``[00:00, H)`` with ``H = floor_hour(now - 5
  min)``, DELETE + INSERT in one transaction, each row stamped ``through = H`` (closed
  days: the next midnight). The summary adds a live pass over everything after the rows'
  own ``through``, so it reads ~2 h of raw rows instead of a day. The 5-minute margin is
  what makes ``H`` safe: a row stamped before ``H`` was committed long before the job
  reads it (requests and the export publish commit within seconds). A row whose request
  or job commits MORE than 5 minutes after its ``at`` is undercounted until the next
  hourly roll, which recomputes ``[00:00, H)`` whole and heals it. No watermark move:
  the watermark stays "last CLOSED day", and midnight replaces yesterday's partial rows
  with the closed-day roll (same DELETE + INSERT), so nothing is double counted. Every
  run recomputes the whole ``[00:00, H)`` (no skip when ``H`` is unchanged), so a late row
  stamped before ``H`` is picked up by the next run.
- **Fail quiet, fail visible.** A failing day goes to the ``jarvis.activity_log`` file
  logger (class only) AND, at most once per site per day, to Error Log ("Jarvis activity
  rollup failed for <day>": exception class + the first 200 characters of its message, the
  day and the watermark; the rollup reads counts only, so the message carries no content).
  A run the time budget stops with days remaining logs a WARNING with the count. The
  summary shows ``stale_days`` and the health block ``rollup_lag_days`` (the report adds
  "Ask your system administrator to check the scheduler.").

Operations
----------

- **Job**: ``jarvis.activity_rollup.rollup_days``, ``scheduler_events["hourly_long"]``
  (the ``long`` queue), once an hour per site. The summary also enqueues it on demand
  (job id ``jarvis_activity_rollup``, deduplicated) whenever closed days are unrolled.
- **Lock**: Redis key ``jarvis:activity_rollup`` (site-prefixed, in ``persistent_cache_keys``),
  TTL ``LOCK_TTL_S`` = 600 s, released at the end of the run only if this run still owns it
  (token-checked). A run that finds it held returns ``[]`` at once.
- **Budget**: no new closed day starts after ``TIME_BUDGET_S`` = 240 s; the rest waits for
  the next run (about 2 to 3 s a day at 100 users x 600 events).
- **Check progress**: ``bench --site <site> execute frappe.db.get_single_value --args
  "['Jarvis Activity Rollup', 'rolled_through']"`` (the stored watermark may read empty
  before the first run); ``select day, count(*), max(through) from `tabJarvis Activity
  Daily` group by day order by day desc limit 5`` shows the days and today's cut-off.
  The report's Summary view states "daily totals are N days behind" above one day.
- **Force a re-roll**: clear the watermark (``frappe.db.set_single_value("Jarvis Activity
  Rollup", "rolled_through", "")``, then commit) and run ``bench --site <site> execute
  jarvis.activity_rollup.rollup_days``; it starts again at the horizon and rewrites every
  day (idempotent: a day is deleted and re-inserted in one transaction). To redo one day
  only, delete its ``Jarvis Activity Daily`` rows and set the watermark to the day before.
- **Retention**: 90 days. ``Jarvis Activity Event`` and ``Jarvis Activity Daily`` are in
  ``default_log_clearing_doctypes`` (Log Settings job); ``activity_report.HORIZON_DAYS``
  must equal both (a test pins it).
- **Log file**: ``jarvis.activity_log.log`` under the bench ``logs/`` and the site's ``logs/``
  (rollup failures, budget warnings, enqueue failures; also the activity logger's own
  failures).
- **Logging health**: the summary's ``logging_health`` block: ``warn`` is true when agents
  ran tools in the last 24 h but no AGENT event (read, export, refusal, rejected sign-in)
  was recorded (app ``action`` events do not count), ``rollup_lag_days`` is yesterday minus
  the watermark, ``scheduler_disabled`` is true when the scheduler is paused, in
  maintenance mode or switched off in System Settings.
"""

import datetime as _dt
import time

import frappe
from frappe.utils import getdate, now

from jarvis import _redis_lock
from jarvis import activity_report as ar

LOCK_KEY = "jarvis:activity_rollup"
LOCK_TTL_S = 600
# One Error Log per site per day for a failing rollup (a stuck day fails every hour).
FAILURE_KEY = "jarvis:activity_rollup_failed"
FAILURE_THROTTLE_S = 24 * 3600
TIME_BUDGET_S = 240
# Today's partial roll stops at the last full hour that ended at least this long ago.
PARTIAL_MARGIN = _dt.timedelta(minutes=5)

# The clock for the time budget. A module attribute so a test can drive it.
_clock = time.monotonic

_FIELDS = (
	"name",
	"creation",
	"modified",
	"modified_by",
	"owner",
	"docstatus",
	"idx",
	"day",
	"actor",
	"actor_name",
	*ar.COUNTER_KEYS,
	"through",
)

_ROLLED = "rolled"
_SKIPPED = "skipped"
_FAILED = "failed"


def cutoff_hour(now_dt):
	"""H: the start of the hour that ended at least ``PARTIAL_MARGIN`` ago."""
	return (now_dt - PARTIAL_MARGIN).replace(minute=0, second=0, microsecond=0)


def rollup_days():
	"""Scheduler entry (``hourly_long``) and the summary's on-demand enqueue. Returns
	the CLOSED days written this run, oldest first (re-rolled yesterday last); today's
	partial roll is not listed."""
	lock = _claim()
	if lock is None:
		return []
	try:
		return _run()
	finally:
		_release(lock)


def _claim():
	"""Take the site-scoped lock (``SET NX PX``) and KEEP the redis-py ``Lock`` object: it
	holds the token this run wrote, which ``_release`` needs. ``None`` when held."""
	cache = frappe.cache()
	lock = cache.lock(cache.make_key(LOCK_KEY), timeout=LOCK_TTL_S)
	return lock if lock.acquire(blocking=False) else None


def _release(lock):
	"""Free the claim early; it would otherwise block an on-demand run for its whole TTL
	after a run that finished in seconds. ``Lock.release`` deletes the key only while it
	still holds THIS run's token (a Lua compare-and-delete), so a run that outlived the TTL
	cannot free the claim another run took meanwhile (a plain ``cache.delete`` would). Best
	effort: ``LockNotOwnedError`` (expired, or taken over) and a Redis outage are ignored,
	the TTL is the backstop."""
	try:
		lock.release()
	except Exception:
		pass


def seed_watermark():
	"""Make sure the Single's ``rolled_through`` row exists (empty value). The
	``post_model_sync`` patch calls this on upgraded sites; a fresh install skips
	patches, so the first run seeds it itself. Idempotent."""
	if not frappe.db.sql(
		"select 1 from `tabSingles` where `doctype` = %s and `field` = %s", (ar.ROLLUP, "rolled_through")
	):
		frappe.qb.into("Singles").columns("doctype", "field", "value").insert(
			ar.ROLLUP, "rolled_through", ""
		).run()


def _read_watermark_locked():
	"""The watermark straight from ``tabSingles``, row-locked until this transaction
	ends (``Database.get_single_value`` has no ``for_update`` on v15). A lock on a row
	that does not exist is only a gap lock, so the row is seeded first."""
	rows = ar._read_watermark_raw(for_update=True)
	if not rows:
		seed_watermark()
		rows = ar._read_watermark_raw(for_update=True)
	return getdate(rows[0][0]) if rows and rows[0][0] else None


def _run():
	started = _clock()
	now_dt = ar.now()
	today = now_dt.date()
	yesterday = today - _dt.timedelta(days=1)
	horizon = ar.horizon_day(today)
	written: list = []

	rolled_through = ar.watermark()
	day = max(rolled_through + _dt.timedelta(days=1), horizon) if rolled_through else horizon
	while day <= yesterday:
		if _clock() - started >= TIME_BUDGET_S:
			remaining = (yesterday - day).days + 1
			frappe.logger("jarvis.activity_log").warning(
				f"activity rollup: time budget reached with {remaining} day(s) still to roll"
			)
			return written
		result = _roll_day(day, advance=True)
		if result == _FAILED:
			return written
		if result == _ROLLED:
			written.append(day)
		day += _dt.timedelta(days=1)

	# Late rows: yesterday's totals are final only once the day's stragglers are in, and a
	# straggler can commit hours late, so yesterday is re-rolled by EVERY run of the day.
	midnight = _dt.datetime.combine(today, _dt.time.min)
	if (
		yesterday >= horizon
		and yesterday not in written
		and _clock() - started < TIME_BUDGET_S
		and _roll_day(yesterday, advance=False) == _ROLLED
	):
		written.append(yesterday)

	# Today through the last full hour, only once every closed day is done (the loop
	# above returns early on a failure or the budget) and not before 01:05 (earlier,
	# H is still yesterday's last hour: nothing of today to roll).
	cutoff = cutoff_hour(now_dt)
	if cutoff > midnight and _clock() - started < TIME_BUDGET_S:
		_roll_day(today, advance=False, through=cutoff)
	return written


def _roll_day(day, *, advance, through=None):
	"""Replace ``day``'s rows with freshly counted ones and, when ``advance``, move the
	watermark to ``day``, all in one transaction. ``through`` (today's partial roll)
	counts only ``[day 00:00, through)`` and stamps the rows with it; a closed day is
	stamped with its end, the next midnight. The grouped queries run FIRST, before
	the watermark row is locked, so the ``tabSingles`` lock (a gap lock when the row is
	missing) is held for milliseconds rather than across three passes over the sources.
	A failure rolls back to the savepoint (not the whole transaction: the caller's
	earlier work is not ours to discard)."""
	frappe.db.savepoint("activity_rollup_day")
	try:
		start = _dt.datetime.combine(day, _dt.time.min)
		end = through or start + _dt.timedelta(days=1)
		counters, snapshots = ar.day_counters(day, day, until=through)
		actors = {a for a, _d in counters}
		unnamed = [a for a in actors if a and a not in snapshots]
		if unnamed:
			snapshots.update(ar.event_snapshots(unnamed, start, end))

		# Only a roll that moves the watermark needs the lock (and the seed): today's partial
		# roll and the late-row re-roll never write it.
		current = _read_watermark_locked() if advance else None
		if advance and current and current >= day:
			# Rolling back to a savepoint does not release the row lock the locking read
			# took: it lasts until the transaction ends (the job ends, or the next commit).
			frappe.db.rollback(save_point="activity_rollup_day")
			return _SKIPPED  # another run got here first; never move the watermark back

		stamp = now()
		frappe.db.delete(ar.DAILY, {"day": day})
		frappe.db.bulk_insert(
			ar.DAILY,
			list(_FIELDS),
			[
				(
					frappe.generate_hash(length=10),
					stamp,
					stamp,
					"Administrator",
					"Administrator",
					0,
					0,
					day,
					actor,
					snapshots.get(actor, ""),
					*[c[k] for k in ar.COUNTER_KEYS],
					end,
				)
				for (actor, _d), c in counters.items()
			],
		)
		if advance:
			frappe.db.set_single_value(ar.ROLLUP, "rolled_through", day, update_modified=False)
		frappe.db.commit()
		return _ROLLED
	except Exception as e:
		try:
			frappe.db.rollback(save_point="activity_rollup_day")
		except Exception:
			# A deadlock or lock-wait abort ends the whole transaction and the savepoint
			# with it; fall back to a full rollback. Either way the run stops.
			frappe.db.rollback()
		_report_failure(day, e)
		return _FAILED


def _report_failure(day, exc):
	"""Make a failing rollup visible to a system administrator. The file log line (class
	only) always; an Error Log entry at most once per site per day (``FAILURE_KEY``, a
	claim that expires after a day): a stuck day fails every hour and must not bury the
	Error Log. Called after the rollback, so the insert is not undone. Never raises."""
	frappe.logger("jarvis.activity_log").error(f"activity rollup: {day} failed: {type(exc).__name__}")
	try:
		if not _redis_lock.claim(f"{FAILURE_KEY}:{ar.today()}", FAILURE_THROTTLE_S):
			return
		try:
			rolled = ar.watermark()
		except Exception:
			rolled = "unknown"
		frappe.log_error(
			title=f"Jarvis activity rollup failed for {day}",
			message=f"{type(exc).__name__}: {str(exc)[:200]}\nday: {day}\nwatermark: {rolled}",
		)
	except Exception:
		pass
