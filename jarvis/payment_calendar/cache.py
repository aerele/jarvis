"""When the cache refreshes: the web hook, the 5-minute tick, the night pre-warm and the refresh chains.

`note_opened(company)` is the one call of the request path. In `off` it costs one Singles read. Otherwise it
marks the company's heads as opened (at most one write per head in `NOTE_EVERY_S`) and queues a refresh for each
side that is stale. It never raises (a job deadline passes through), so it can never change a reply.

`tick()` runs every 5 minutes and only enqueues, plus the janitor. A side is due when its company was opened in
the last `OPEN_WINDOW_S`, it holds no live claim or memo, and its generation is older than `start_age_s`. The
start age comes from the measured duration `d`: a refresh must end before the age reaches `MAX_AGE_S`, so it
starts `2*d + START_MARGIN_S` before that (never below 0). At 0 the chain is continuous: the job loops while it
holds its claim (`cache_refresh`), and hands over to a hop job with a new id when its time budget is spent.
Without `payment_calendar_cache_full_cadence` a day refresh is an hourly sample and never loops.

Day refreshes run only on the dedicated lane (`cache_state.lane_live`). The night pre-warm runs on `long`, once
per (company, side) per night, from `PREWARM_FROM` + a jitter of the company digest to `PREWARM_UNTIL`; the job
checks the window again when it starts. RQ arguments and Redis keys hold the company digest only.

The tick also purges a company once after a Transaction Deletion Record completed (`purge_after_deletion`, a backstop
for the doc event of `lifecycle`) and writes the Error Log rows of `cache_alerts`. A web request remembers the
lane probe for `LANE_MEMO_S` seconds, so it does not probe the lane for each stale side.

Everything here reads `cache_state.effective_mode` / `active`, never the raw mode: `on` acts as `shadow` until the cache serves.
"""

import contextlib
from dataclasses import dataclass
from datetime import datetime, time
from time import monotonic
from time import time as epoch_now

import frappe
from frappe.utils.background_jobs import get_job
from rq.timeouts import JobTimeoutException

from jarvis.payment_calendar import cache_state, cache_store, guard, read_log, redis_slots, routing
from jarvis.payment_calendar.errors import TIMEOUTS
from jarvis.permissions import is_system_manager

JOB = "jarvis.payment_calendar.cache_refresh.run"
SIDES = cache_store.SIDES
HEAD = cache_store.HEAD
GENERATION = cache_store.GENERATION

# --- Numbers T0 must confirm. Each is a default until the measurement sets it. ---
# A company counts as in use for this long after the last open.
OPEN_WINDOW_S = 30 * 60
# `last_opened` is written at most this often per head.
NOTE_EVERY_S = 300
# The age no served generation may pass, and the gap kept before it when a refresh starts.
MAX_AGE_S = 14 * 60
START_MARGIN_S = 5 * 60
# A day refresh of a company that is not on full cadence (the hourly sample on customer hosts).
HOURLY_SAMPLE_S = 3600
# The night pre-warm window (checked at job start) and its jitter.
PREWARM_FROM = time(0, 15)
PREWARM_UNTIL = time(1, 15)
PREWARM_JITTER_S = 600
# Per-night marker of a pre-warm unit that has started (a Redis key under the persistent prefix).
NIGHT_MARKER_TTL_S = 2 * 86_400
# `note_opened` remembers the lane probe this long, so a request does not probe it per stale side.
LANE_MEMO_S = 30
# A job that skips for a lasting reason that has no marker or memo of its own says so for this long, per
# reason, so that the tick and the requests do not queue a job for the same skip again and again.
SKIP_MEMO_S = {
	"no_slot": 60,
	"no_disk": 600,
	"deletion_running": 600,
	"scheduler_inactive": 600,
	"unsupported_major": 3600,
}
# A tick that has not run for this long is late: three missed runs of the 5-minute cron.
TICK_LATE_S = 15 * 60
# How long the stamp of the last tick stays in Redis.
TICK_STAMP_TTL_S = 7 * 86_400
# Heads whose building generation has outlived its job: recorded per tick.
EXPIRED_PER_TICK = 20
# The failure class of a generation whose job left no trace.
ORPHAN_CLASS = "worker_lost"
# --- end of the T0-dependent numbers ---

_now = frappe.utils.now_datetime
_monotonic = monotonic
_lane_memo: dict = {}


@dataclass(frozen=True)
class Head:
	"""What the scheduler reads of a head and of its current generation."""

	company: str
	side: str
	hop_seq: int
	current_generation: int | None
	opened_age_s: int | None
	claim_live: bool
	memo_live: bool
	last_failure: str | None
	duration_ms: int | None
	age_s: int | None


# ------------------------------------------------------------------------------------------ request path


def note_opened(company: str) -> None:
	"""Mark `company` as in use and queue a refresh for each stale side. Never raises; best effort.

	An RQ job deadline passes through (a web request has none). Any other error, a socket `TimeoutError`
	included, is logged by class and the caller carries on.
	"""
	try:
		settings = cache_state.cache_settings(lists_when_off=False)
		if not settings.active or company not in settings.companies:
			return
		_check_tick_alive()
		heads = heads_of(company)
		for side in SIDES:
			head = heads.get(side)
			if head and (head.opened_age_s is None or head.opened_age_s >= NOTE_EVERY_S):
				mark_opened(company, side)
			if stale(head, settings):
				enqueue_side(company, side, opened=True)
	except JobTimeoutException:
		raise
	except Exception as exc:
		frappe.logger("payment_calendar").warning("cache_note_opened failed class=%s", type(exc).__name__)


def mark_opened(company: str, side: str) -> bool:
	"""Set `last_opened` to now unless it was set in the last `NOTE_EVERY_S`; True when it wrote. Never commits."""
	frappe.db.sql(
		f"UPDATE `tab{HEAD}` SET last_opened=NOW(6) WHERE name=%s AND "
		"(last_opened IS NULL OR last_opened < NOW(6) - INTERVAL %s SECOND)",
		(cache_store.head_name(company, side), NOTE_EVERY_S),
	)
	return bool(frappe.db.sql("SELECT ROW_COUNT()")[0][0])


# --------------------------------------------------------------------------------------------- the tick


def tick() -> None:
	"""The cron entry: janitor, then enqueue what is due. Only enqueues; the refresh runs in its own job."""
	_stamp_tick()
	settings = cache_state.cache_settings()
	if not settings.known:
		# An unreadable switch is not `off`: nothing is purged, bumped or delisted on it.
		count_skip("settings_unknown")
		return
	listed = settings.companies if settings.active else ()
	if not settings.active and not _any_head():
		return
	_isolated(_record_expired_builds, "expired_builds")
	_isolated(lambda: _run_janitor(listed), "janitor")
	if not settings.active:
		return
	_isolated(purge_after_deletion, "deletions")
	heads = heads_of()
	for company in settings.companies:
		for side in SIDES:
			if stale(heads.get((company, side)), settings) and opened(heads.get((company, side))):
				enqueue_side(company, side)
	_prewarm(settings, _now())
	_isolated(lambda: _raise_alerts(settings, heads), "alerts")


def _run_janitor(listed) -> None:
	deleted = cache_store.janitor(listed=listed, cache_build=routing.cache_build())
	frappe.logger("payment_calendar").info("cache_janitor rows_deleted=%s", deleted)


def _stamp_tick() -> None:
	"""Remember when the tick ran (Redis), so that a request and the status can tell a tick that has stopped."""
	try:
		frappe.cache.execute_command(
			"SET", cache_state.cache_key("last_tick"), int(epoch_now()), "EX", TICK_STAMP_TTL_S
		)
	except TIMEOUTS:
		raise
	except Exception:
		pass


def last_tick_age_s() -> int | None:
	"""Seconds since the last tick, or None when no tick was seen (or Redis does not answer)."""
	try:
		stamp = frappe.cache.execute_command("GET", cache_state.cache_key("last_tick"))
		return None if stamp is None else max(int(epoch_now()) - int(stamp), 0)
	except TIMEOUTS:
		raise
	except Exception:
		return None


def tick_age_s() -> int | None:
	"""Seconds since the last tick; when none was seen, since the first time this was asked. None on a Redis error.

	A site whose tick never ran (the scheduler is off) has no stamp: the age then counts from the first request that
	looked, so the dead-man check still fires `TICK_LATE_S` later.
	"""
	try:
		age = last_tick_age_s()
		if age is not None:
			return age
		watch = cache_state.cache_key("tick_watch")
		frappe.cache.execute_command("SET", watch, int(epoch_now()), "NX", "EX", TICK_STAMP_TTL_S)
		return max(int(epoch_now()) - int(frappe.cache.execute_command("GET", watch)), 0)
	except TIMEOUTS:
		raise
	except Exception:
		return None


def _check_tick_alive() -> None:
	"""A request raises the alert of a tick that is late: a scheduler that is down cannot raise its own."""
	age = tick_age_s()
	if age is not None and age > TICK_LATE_S:
		from jarvis.payment_calendar import cache_alerts

		cache_alerts.tick_late(age)


def _raise_alerts(settings: cache_state.CacheSettings, heads: dict) -> None:
	"""The Error Log rows of the cache. Imported here: `cache_alerts` reads this module's heads."""
	from jarvis.payment_calendar import cache_alerts

	cache_alerts.alerts(settings, heads)


def purge_after_deletion() -> None:
	"""Backstop: purge a company once when a Transaction Deletion Record completed after its current generation began.

	The main path is the doc event: ERPNext sets the record's status with `db_set`, which runs `on_change`, and
	`lifecycle.on_deletion_record` purges on Queued, Running and Completed. This tick catches what that path
	misses: a purge job lost after the commit, a record completed while the hooks were not installed or the cache
	was off, or a build that finished after the event. The refresh preflight refuses while one is Queued or
	Running (`deletion_running`). A purge clears the current generation, so the join finds the company only once.
	Commits (it is the tick); a database error rolls back and propagates.
	"""
	# `t.modified` is site time and `g.as_of` is database time: `g.as_of` is moved to site time first.
	companies = [
		row[0]
		for row in frappe.db.sql(
			f"SELECT DISTINCT h.company FROM `tab{HEAD}` h JOIN `tab{GENERATION}` g ON g.name=h.current_generation "
			"JOIN `tabTransaction Deletion Record` t ON t.company=h.company AND t.status='Completed' "
			"AND t.docstatus=1 AND t.modified > g.as_of + INTERVAL %s SECOND",
			(cache_store.site_clock_offset_s(),),
		)
	]
	for company in companies:
		try:
			cache_store.purge_company(company)
			frappe.db.commit()
		except BaseException:
			with contextlib.suppress(Exception):
				frappe.db.rollback()
			raise


def enqueue_side(company: str, side: str, *, night: bool = False, opened: bool = False) -> str:
	"""Queue one refresh of (company, side): `queued`, `backoff`, `memo` or `busy`.

	`busy` also covers every reason that nothing can be queued now (switch off, company not listed, no lane,
	no queue); the reason is counted per day, name-free. `opened` tells the job to mark the head as opened.
	"""
	settings = cache_state.cache_settings()
	if not settings.active or company not in settings.companies:
		return _busy("not_active")
	if not night and not _lane_live(remember=opened):
		return _busy("no_lane")
	if night and cache_state.bench_slots(night=True) <= 0:
		return _busy("no_slot")
	digest = read_log.company_digest_or(company)
	if digest and skip_memo_set(digest, side):
		return _busy("skip_memo")
	head = heads_of(company).get(side)
	if head and head.memo_live and cache_store.memo_blocks(company, side, routing.cache_build()):
		return "backoff" if head.last_failure == "history_too_large" else "memo"
	if head and head.claim_live:
		return "busy"
	hop_seq = head.hop_seq if head else 0
	return "queued" if enqueue_job(company, side, hop_seq, night=night, opened=opened) else "busy"


def remember_skip(digest: str, side: str, reason: str) -> None:
	"""Remember that a job skipped (company digest and side) for `reason`, for `SKIP_MEMO_S[reason]` seconds."""
	seconds = SKIP_MEMO_S.get(reason)
	if not seconds:
		return
	try:
		frappe.cache.execute_command("SET", _skip_key(digest, side), "1", "EX", seconds)
	except TIMEOUTS:
		raise
	except Exception:
		pass  # a Redis that is down remembers nothing: the next enqueue goes through


def skip_memo_set(digest: str, side: str) -> bool:
	try:
		return bool(frappe.cache.execute_command("EXISTS", _skip_key(digest, side)))
	except TIMEOUTS:
		raise
	except Exception:
		return False


def _skip_key(digest: str, side: str) -> str:
	return cache_state.cache_key("skip", digest, side)


def _lane_live(*, remember: bool) -> bool:
	"""The lane probe. A web request (`remember`) reuses the answer for `LANE_MEMO_S` seconds in this process."""
	if not remember:
		return cache_state.lane_live()
	now = _monotonic()
	if _lane_memo.get("until", 0) > now:
		return _lane_memo["live"]
	live = cache_state.lane_live()
	_lane_memo.update(live=live, until=now + LANE_MEMO_S)
	return live


def reset_lane_memo() -> None:
	_lane_memo.clear()


def enqueue_job(company: str, side: str, hop_seq: int, *, night: bool = False, opened: bool = False) -> bool:
	"""Put the refresh job on its queue under `job_id`; False when dedupe dropped it or the queue refused.

	The timeout is explicit: the `long` queue's default (1,500 s) is shorter than a night job (1,800 s).
	"""
	digest = read_log.company_digest_or(company)
	if digest is None:
		return False
	try:
		queued = frappe.enqueue(
			JOB,
			queue="long" if night else cache_state.LANE_QUEUE,
			timeout=cache_state.job_timeout_s(night=night),
			job_id=job_id(digest, side, hop_seq),
			deduplicate=True,
			enqueue_after_commit=False,
			digest=digest,
			side=side,
			night=night,
			opened=opened,
		)
	except TIMEOUTS:
		raise
	except Exception as exc:
		frappe.logger("payment_calendar").warning("cache_enqueue failed class=%s", type(exc).__name__)
		return False
	return queued is not None


def job_id(digest: str, side: str, hop_seq: int) -> str:
	"""`pcal-cache:<digest>:<side>:<hop_seq>`: a hop never has the id of the job that enqueues it."""
	return f"pcal-cache:{digest}:{side}:{hop_seq}"


def hop_queued(digest: str, side: str) -> bool:
	"""True when a job with the head's current hop id waits in a queue."""
	hop = _hop_seq_of(digest, side)
	if hop is None:
		return False
	try:
		job = get_job(job_id(digest, side, hop))
		return bool(job and job.get_status(refresh=True) in ("queued", "scheduled", "deferred"))
	except TIMEOUTS:
		raise
	except Exception:
		return False


# ----------------------------------------------------------------------------------------- chain policy


def start_age_s(duration_ms: int | None, *, full_cadence: bool) -> int:
	"""The age of a generation at which the next refresh starts. 0 means a continuous chain."""
	if not full_cadence:
		return HOURLY_SAMPLE_S
	d = duration_ms / 1000 if duration_ms else cache_state.D_MAX_S
	return max(0, int(MAX_AGE_S - 2 * d - START_MARGIN_S))


def continuous(duration_ms: int | None, *, full_cadence: bool) -> bool:
	return start_age_s(duration_ms, full_cadence=full_cadence) == 0


def stale(head: Head | None, settings: cache_state.CacheSettings) -> bool:
	"""True when a refresh should start now: no generation yet, or older than the start age; no claim, no memo."""
	if head and (head.claim_live or head.memo_live):
		return False
	if head is None or head.current_generation is None or head.age_s is None:
		return True
	return head.age_s >= start_age_s(head.duration_ms, full_cadence=settings.full_cadence)


def opened(head: Head | None) -> bool:
	"""True when the company was opened in the last `OPEN_WINDOW_S`."""
	return bool(head and head.opened_age_s is not None and head.opened_age_s < OPEN_WINDOW_S)


def opened_recently(company: str, side: str) -> bool:
	return opened(heads_of(company).get(side))


def enqueue_hop(company: str, side: str, hop_seq: int) -> bool:
	"""The hand-over of a chain whose job spent its time budget: one job under the new hop id (day, lane)."""
	return cache_state.lane_live() and enqueue_job(company, side, hop_seq)


# ------------------------------------------------------------------------------------------ night pre-warm


def in_prewarm_window(now: datetime | None = None) -> bool:
	"""True from `PREWARM_FROM` up to (not including) `PREWARM_UNTIL`, site time."""
	return PREWARM_FROM <= (now or _now()).time() < PREWARM_UNTIL


def take_night_marker(digest: str, side: str) -> bool:
	"""True the first time per night; the unit that gets False has nothing to do (already started)."""
	return redis_slots.claim(_night_key(digest, side), "1", NIGHT_MARKER_TTL_S)


def _prewarm(settings: cache_state.CacheSettings, now: datetime) -> None:
	if not in_prewarm_window(now):
		return
	slots = cache_state.bench_slots(night=True)
	if slots <= 0:
		count_skip("no_slot")
		return
	start = datetime.combine(now.date(), PREWARM_FROM)
	for company in settings.companies:
		digest = read_log.company_digest_or(company)
		if digest is None or (now - start).total_seconds() < int(digest[:8], 16) % PREWARM_JITTER_S:
			continue
		for side in SIDES:
			if not night_marker_set(digest, side) and enqueue_side(company, side, night=True) == "queued":
				slots -= 1
				if slots <= 0:
					return


def _night_key(digest: str, side: str) -> str:
	return cache_state.cache_key("night", digest, side, routing.today())


def night_marker_set(digest: str, side: str) -> bool:
	try:
		return bool(frappe.cache.execute_command("EXISTS", _night_key(digest, side)))
	except TIMEOUTS:
		raise
	except Exception:
		return True  # Redis down: no pre-warm queued, the same as at admission


# --------------------------------------------------------------------------------------------- the heads


def heads_of(company: str | None = None) -> dict:
	"""The heads with their current generation's age: `{side: Head}` for one company, else `{(company, side): Head}`."""
	where, values = ("WHERE h.company=%s", (company,)) if company else ("", ())
	rows = frappe.db.sql(
		"SELECT h.company, h.side, h.hop_seq, h.current_generation, "
		"TIMESTAMPDIFF(SECOND, h.last_opened, NOW(6)), "
		"(h.claim_token IS NOT NULL AND h.claim_expires >= NOW(6)), "
		# A memo holds for the build that failed (`cache_store.memo_blocks`); a new build lifts it.
		"(h.memo_until IS NOT NULL AND h.memo_until > NOW(6) AND COALESCE((SELECT f.cache_build "
		f"FROM `tab{GENERATION}` f WHERE f.company=h.company AND f.side=h.side AND f.state='failed' "
		"ORDER BY f.name DESC LIMIT 1) = %s, 1)), "
		"h.last_failure, h.last_duration_ms, TIMESTAMPDIFF(SECOND, g.as_of, NOW(6)) "
		f"FROM `tab{HEAD}` h LEFT JOIN `tab{GENERATION}` g ON g.name=h.current_generation AND g.state='current' "
		f"{where}",
		(routing.cache_build(), *values),
	)
	heads = {
		(row[0], row[1]): Head(
			row[0], row[1], row[2] or 0, row[3], row[4], bool(row[5]), bool(row[6]), row[7], row[8], row[9]
		)
		for row in rows
	}
	return {side: head for (_, side), head in heads.items()} if company else heads


@frappe.whitelist(methods=["POST"])
def reset(company: str, side: str | None = None) -> dict:
	"""Operator: clear the memo, the backoff and the failure counters of a company (one side, or all three).

	For a System Manager. It keeps the claim, the current generation and the purge epoch, so a running build and
	the served generation are untouched; the next tick or request queues the side again. From the bench:
	`bench --site <site> execute jarvis.payment_calendar.cache.reset --kwargs "{'company': '<company>'}"`.
	Returns the number of heads that were reset.
	"""
	if not is_system_manager():
		raise frappe.PermissionError
	if side is not None and side not in SIDES:
		raise ValueError("Unknown cache side.")
	where, values = ("company=%s AND side=%s", (company, side)) if side else ("company=%s", (company,))
	# nosemgrep: frappe-sql-format-injection -- `where` is one of two literal clauses; company and side are bound
	heads = frappe.db.sql(f"SELECT COUNT(*) FROM `tab{HEAD}` WHERE {where} FOR UPDATE", values)[0][0]
	# nosemgrep: frappe-sql-format-injection -- `where` is one of two literal clauses; company and side are bound
	frappe.db.sql(
		f"UPDATE `tab{HEAD}` SET memo_until=NULL, backoff_level=0, failures=0, last_failure=NULL WHERE {where}",
		values,
	)
	frappe.logger("payment_calendar").info(
		"cache_reset company=%s heads=%s", read_log.company_digest_or(company), heads
	)
	return {"heads": heads}


def _any_head() -> bool:
	# nosemgrep: frappe-sql-format-injection -- constant table name; no value
	return bool(frappe.db.sql(f"SELECT 1 FROM `tab{HEAD}` LIMIT 1"))


def _hop_seq_of(digest: str, side: str) -> int | None:
	for (company, head_side), head in heads_of().items():
		if head_side == side and read_log.company_digest_or(company) == digest:
			return head.hop_seq
	return None


def _record_expired_builds() -> None:
	"""A `building` generation past its deadline with no failure: its job died (a kill, a reboot, a failed
	reconnect after a `MemoryError`). The store records it as `worker_lost`, one plain failure and never the
	"too large" backoff, and only when nothing newer decides the head. It does not need the claim token: the
	job's `finally` may have released the claim.
	"""
	rows = frappe.db.sql(
		f"SELECT name, company, side FROM `tab{GENERATION}` WHERE state='building' AND deadline < NOW(6) "
		"ORDER BY name LIMIT %s",
		(EXPIRED_PER_TICK,),
	)
	for name, company, side in rows:
		cache_store.fail_orphan(company, side, name, ORPHAN_CLASS)


# ------------------------------------------------------------------------------------------ small tools


def _isolated(step, name: str) -> None:
	"""Run a housekeeping step so that its error does not stop the enqueues of the tick (`guard.guarded`).

	A step that fails is named in the log and counted for the day (`tick_step_failed:<name>`).
	"""
	guard.guarded(step, f"cache_tick {name}", on_swallow=lambda exc: count_skip(f"tick_step_failed:{name}"))


def count_skip(reason: str) -> None:
	"""Count one skipped or refused refresh by a fixed reason (a site day counter, no names). Never raises."""
	try:
		read_log.site_count(f"cache_skip:{reason}", routing.today())
	except TIMEOUTS:
		raise
	except Exception:
		pass


def _busy(reason: str) -> str:
	"""Count why nothing was queued and answer `busy`."""
	count_skip(reason)
	return "busy"
