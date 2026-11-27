"""The Error Log rows of the cache: one row per company, class and day.

The tick calls `alerts(settings, heads)`. It reads the state (heads and generation records from the database,
the skip counters from Redis, Transaction Deletion Records, the reader pins), `evaluate` turns it into alerts,
and `write` stores each alert. The rows do not depend on which job saw a failure, and a Redis restart at worst
writes one more row.

`write` commits each row and only then sets the once-a-day Redis key, so a row that was rolled back never hides
its alert for the rest of the day. A database error that may have rolled the transaction back propagates (the
tick isolates it); any other error is logged by class and the next alert goes on. (`frappe.log_error` rows are not
rolled back by `frappe.db.rollback()` on this Frappe; the order still decides whether a failed commit hides the alert.)

Recovery: an alert that fired and no longer does gives one INFO line (`cache_alert recovered`). A Redis marker
per (class, digest) remembers what fired; it expires after `RECOVERY_TTL_S`.

A title is a fixed phrase that can be searched for. It starts with `jarvis.`, the rule of `api_errors.is_jarvis_error`:
only such a row is forwarded to the Jarvis error feed, where a person reads it. A row without it stays on the site. A message holds the company digest, the sides and numbers
or fixed words only, never a name. `docs/architecture/payment-calendar.md` maps each class to its cause and fix.
"""

from collections.abc import Iterable
from dataclasses import dataclass

import frappe

from jarvis.payment_calendar import (
	cache,
	cache_state,
	cache_status,
	cache_store,
	compatibility,
	guard,
	read_log,
	routing,
)
from jarvis.payment_calendar.errors import TIMEOUTS

TITLES = {
	"diff": "jarvis.payment_calendar.cache: shadow diff",
	"history_too_large": "jarvis.payment_calendar.cache: history too large",
	"readback": "jarvis.payment_calendar.cache: read-back mismatch",
	"failures": "jarvis.payment_calendar.cache: repeated refresh failures",
	"stale_risk": "jarvis.payment_calendar.cache: stale generation risk",
	"lane": "jarvis.payment_calendar.cache: lane not live",
	"deletion": "jarvis.payment_calendar.cache: deletion record blocks refresh",
	"not_buildable": "jarvis.payment_calendar.cache: query hook blocks build",
	"pin_drift": "jarvis.payment_calendar.cache: reader pin drift",
	"tick": "jarvis.payment_calendar.cache: tick is late (scheduler or worker down)",
	"janitor_lag": "jarvis.payment_calendar.cache: janitor is behind",
}
# A second `history_too_large` in a row (backoff level 2 or more) is a repeat; the first is expected.
REPEAT_LEVEL = 2
# A Transaction Deletion Record that is Queued or Running and was created this long ago blocks every refresh of
# the site. Its age is its `creation`: ERPNext `db_set` bumps `modified` at every step, so a long deletion that
# keeps progressing would never look stale by `modified`.
DELETION_STALE_S = 3600
# The janitor is behind when this many generations wait for it, or the oldest has waited this long.
JANITOR_LAG_COUNT = 200
JANITOR_LAG_S = 3600
# A diff record counts for the alert this long after its build.
DIFF_LOOKBACK_S = 86_400
NOT_BUILDABLE_SKIP = "not_buildable:query_hook"
# One digest stands for "no company": site-level alerts.
SITE = read_log.SITE_DIGEST
# The operation class of the once-a-day dedupe key.
WINDOW_CLASS = "cache"
# What fired, per (class, digest), so that a recovery is told once. Under the cache prefix that uninstall clears.
RECOVERY_PREFIX = f"{cache_state.CACHE_KEY_PREFIX}alerting:"
RECOVERY_TTL_S = 3 * 86_400


@dataclass(frozen=True)
class Alert:
	klass: str
	digest: str
	sides: tuple[str, ...]
	detail: str

	@property
	def title(self) -> str:
		return TITLES[self.klass]

	@property
	def message(self) -> str:
		return f"company={self.digest} class={self.klass} sides={','.join(self.sides) or '-'} {self.detail}".strip()


def alerts(settings: cache_state.CacheSettings, heads: dict, *, today: str | None = None) -> list[Alert]:
	"""Evaluate the state and write the rows; returns the alerts found. The tick isolates a failure."""
	today = today or routing.today()
	in_use = any(cache.opened(head) for head in heads.values())
	configured = lane_configured()
	found = evaluate(
		heads,
		cache_status.head_metrics(),
		full_cadence=settings.full_cadence,
		diffs=recent_diffs(),
		lane_live=cache_state.lane_live() if in_use and configured else None,
		lane_configured=configured,
		tdr_stuck=stuck_deletions(),
		pin_drift=len(compatibility.reader_pin_drift()),
		skips=cache_status.skip_counts(today),
		janitor=cache_store.janitor_backlog(),
	)
	write(found, today)
	track_recovery(found)
	return found


def lane_configured() -> bool:
	"""True when `jarvis_pcal` is in `workers` of the site config: the site meant to have a lane.

	A site without it runs the night pre-warm only, which is a valid setup. A configured lane with no live
	worker is the fault.
	"""
	try:
		return cache_state.LANE_QUEUE in (frappe.get_conf().get("workers") or {})
	except TIMEOUTS:
		raise
	except Exception:
		return False


def evaluate(
	heads: dict,
	metrics: dict,
	*,
	full_cadence: bool,
	diffs: Iterable[dict],
	lane_live: bool | None,
	lane_configured: bool,
	tdr_stuck: int,
	pin_drift: int,
	skips: dict,
	janitor: dict | None = None,
) -> list[Alert]:
	"""The alerts for this state. Pure: no database, no Redis. `lane_live` None means it was not probed.

	`lane_configured` False is a bench without a `jarvis_pcal` lane: it builds by night and its generation ages by
	day, so it raises no `stale_risk`, whatever the full cadence. Without a configured lane `lane_live` is None.
	"""
	per_company: dict = {}

	def add(klass: str, company: str, side: str, detail: str) -> None:
		digest = cache_status.digest_of(company) or SITE
		per_company.setdefault((klass, digest), (set(), detail))[0].add(side)

	for (company, side), head in heads.items():
		metric = metrics.get((company, side), {})
		if metric.get("backoff_level", 0) >= REPEAT_LEVEL:
			add("history_too_large", company, side, f"level={metric['backoff_level']}")
		if head.last_failure == "readback":
			add("readback", company, side, "class=readback")
		elif metric.get("failures", 0) >= cache_store.FAILURES_BEFORE_MEMO:
			add(
				"failures",
				company,
				side,
				f"failures={metric['failures']} class={read_log._safe(head.last_failure)}",
			)
		if (
			full_cadence
			and lane_configured
			and cache.opened(head)
			and head.age_s is not None
			and head.age_s > cache.MAX_AGE_S
		):
			add("stale_risk", company, side, f"age_s={head.age_s}")
	for record in diffs:
		add("diff", record["company"], record["side"], _numbers(record.get("counts")))
	found = [
		Alert(klass, digest, tuple(sorted(sides)), detail)
		for (klass, digest), (sides, detail) in per_company.items()
	]
	if lane_live is False:
		found.append(Alert("lane", SITE, (), f"companies_in_use={_in_use(heads)}"))
	if tdr_stuck:
		found.append(Alert("deletion", SITE, (), f"records={tdr_stuck}"))
	if pin_drift:
		found.append(Alert("pin_drift", SITE, (), f"pins={pin_drift}"))
	if janitor and (
		janitor["purged"] + janitor["retired_with_rows"] >= JANITOR_LAG_COUNT
		or (janitor["purged"] + janitor["retired_with_rows"] and janitor["oldest_s"] > JANITOR_LAG_S)
	):
		found.append(
			Alert(
				"janitor_lag",
				SITE,
				(),
				f"purged={janitor['purged']} retired_with_rows={janitor['retired_with_rows']} oldest_s={janitor['oldest_s']}",
			)
		)
	if skips.get(NOT_BUILDABLE_SKIP):
		found.append(Alert("not_buildable", SITE, (), f"count={skips[NOT_BUILDABLE_SKIP]}"))
	return sorted(found, key=lambda alert: (alert.klass, alert.digest))


def write(found: Iterable[Alert], today: str, *, commit: bool = True) -> None:
	"""One Error Log row per (class, company) and day: commit the row, then close the day's window.

	A cache that is down writes no row. A job deadline and a database error that may have rolled the transaction
	back propagate; any other error is logged by class and lets the next alert go on (`guard.guarded`).
	`commit=False` is for a caller that runs inside someone else's transaction (a web request): the row is
	written without a commit.
	"""
	for alert in found:
		guard.guarded(lambda alert=alert: _write_one(alert, today, commit), f"cache_alert {alert.klass}")


def tick_late(age_s: int) -> None:
	"""The alert of a tick that has not run for `age_s` seconds. Called by a request: it never commits."""
	write([Alert("tick", SITE, (), f"last_tick_age_s={age_s}")], routing.today(), commit=False)


def _write_one(alert: Alert, today: str, commit: bool) -> None:
	operation = f"cache:{alert.klass}:{alert.digest}:{today}"
	if read_log.window_open(operation, WINDOW_CLASS) is not True:
		return
	frappe.log_error(title=alert.title, message=alert.message)
	if commit:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- the scheduler tick only; a request passes commit=False
	read_log.close_window(operation, WINDOW_CLASS, read_log.OUTCOME_ROW_WINDOW)


def track_recovery(found: Iterable[Alert]) -> None:
	"""Mark what fires now; write one INFO line for each marker whose alert no longer fires, and drop it.

	Redis only. A cache that is down marks and recovers nothing; a marker that expires gives no line.
	"""
	try:
		firing = {f"{alert.klass}:{alert.digest}" for alert in found}
		for key in cache_state.scan_keys(RECOVERY_PREFIX):
			name = (key.decode() if isinstance(key, bytes) else str(key)).split(RECOVERY_PREFIX, 1)[1]
			if name not in firing:
				frappe.cache.delete_value(key, make_keys=False)
				klass, _, digest = name.partition(":")
				frappe.logger("payment_calendar").info(
					"cache_alert recovered class=%s company=%s", klass, digest
				)
		for name in sorted(firing):
			frappe.cache.execute_command(
				"SET", frappe.cache.make_key(f"{RECOVERY_PREFIX}{name}"), "1", "EX", RECOVERY_TTL_S
			)
	except TIMEOUTS:
		raise
	except Exception as exc:
		frappe.logger("payment_calendar").warning("cache_alert_recovery failed class=%s", type(exc).__name__)


def recent_diffs() -> list[dict]:
	"""The night records with outcome `diff` that are younger than `DIFF_LOOKBACK_S`."""
	return [
		record
		for record in cache_status.oracle_records(days=2)
		if record["outcome"] == "diff" and record["age_s"] <= DIFF_LOOKBACK_S
	]


def stuck_deletions() -> int:
	"""Transaction Deletion Records of the site that are Queued or Running and were created over `DELETION_STALE_S` ago.

	The age is `creation`, never `modified` (see `DELETION_STALE_S`). A record that was drafted long before it
	was submitted counts at once; that is rare and the alert only asks someone to look. `creation` is site
	time, so the database `NOW(6)` is moved to site time first.
	"""
	return frappe.db.sql(
		"SELECT COUNT(*) FROM `tabTransaction Deletion Record` WHERE status IN ('Queued', 'Running') "
		"AND creation < NOW(6) + INTERVAL %s SECOND - INTERVAL %s SECOND",
		(cache_store.site_clock_offset_s(), DELETION_STALE_S),
	)[0][0]


def _in_use(heads: dict) -> int:
	return len({company for (company, _), head in heads.items() if cache.opened(head)})


def _numbers(counts) -> str:
	"""`name=n` for each numeric count, sorted; lists and words in a record are dropped."""
	if not isinstance(counts, dict):
		return ""
	return " ".join(
		f"{name}={value}"
		for name, value in sorted(counts.items())
		if isinstance(value, int) and not isinstance(value, bool) and read_log._SAFE_TEXT.fullmatch(str(name))
	)
