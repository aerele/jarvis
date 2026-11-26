"""The cache's switch, its lane, its Redis admission slots and the timing numbers they share.

`cache_settings()` reads the three hidden fields of Jarvis Settings fresh on every call and fails closed: a
missing field, an unknown value or any error gives `UNKNOWN`, which acts as `off` for every path that serves or
queues work. It is not a positive `off`: it has `known=False`, and only a known `off` (or a company that a known
list leaves out) may purge. The modes are `off | shadow | on`; `on` is accepted so that the release that serves
can use it, and every path today treats it as `shadow` (`CacheSettings.effective_mode`).

The lane is the RQ queue `jarvis_pcal`. Generic workers also take every configured queue, so only a worker
whose queue list is exactly the lane counts (`lane_workers`). Without such a worker, daytime refresh is off and the
bench builds only in the night window, on its `long` workers (one unit at a time on a bench with one `long` worker).

The slots are Redis hints, never the fence: a bench-wide key per worker that may run a refresh at once
(`make_key(shared=True)`, so one count for every site of the bench). The durable claim on the cache head row
fences the writes; a lost slot may let one job too many start, and the claim then stops it.

The timing constants are shared: the claim, the slot and the loop all derive from them, so no module restates one.
"""

from dataclasses import dataclass

import frappe
from frappe.utils import cint

from jarvis.payment_calendar import guard, redis_slots, routing
from jarvis.payment_calendar.errors import TIMEOUTS

MODE_FIELD = "payment_calendar_cache"
COMPANIES_FIELD = "payment_calendar_cache_companies"
FULL_CADENCE_FIELD = "payment_calendar_cache_full_cadence"
CACHE_LIST_FIELDS = (COMPANIES_FIELD,)
CACHE_FIELDS = (MODE_FIELD, COMPANIES_FIELD, FULL_CADENCE_FIELD)
MODES = ("off", "shadow", "on")

# Keys that one `SCAN` step looks at.
SCAN_COUNT = 200

LANE_QUEUE = "jarvis_pcal"
# Every site Redis key of the cache starts with this prefix: the uninstall deletes them by it.
CACHE_KEY_PREFIX = "jarvis:pcal:cache:"
SLOT_PREFIX = f"{CACHE_KEY_PREFIX}slot:"

# No statement of a refresh view runs longer than this, and none starts with less than this left of the view.
STATEMENT_CAP_S = 60
# The read view: day, and the night pre-warm (which also runs the oracle).
VIEW_CAP_DAY_S = 240
VIEW_CAP_NIGHT_S = 720
# Write, read-back and swap share one deadline, checked before each chunk.
WRITE_DEADLINE_S = 120
# Added to the longest honest iteration to get the life of a claim and a slot.
CLAIM_MARGIN_S = 120
# The enforced bound of one day iteration: view cap + write deadline. Freshness is 2 x this + the hand-over.
D_MAX_S = VIEW_CAP_DAY_S + WRITE_DEADLINE_S
# A job loops again only with at least this much of its timeout left.
LOOP_MIN_LEFT_S = D_MAX_S + CLAIM_MARGIN_S
JOB_TIMEOUT_DAY_S = 1200
JOB_TIMEOUT_NIGHT_S = 1800


@dataclass(frozen=True)
class CacheSettings:
	mode: str
	companies: tuple[str, ...]
	full_cadence: bool
	known: bool = True

	@property
	def effective_mode(self) -> str:
		"""`off` or `shadow`: until the cache serves, `on` behaves as `shadow` on every path."""
		return "off" if self.mode == "off" else "shadow"

	@property
	def active(self) -> bool:
		return self.effective_mode != "off"


_OFF = CacheSettings("off", (), False)
# The switch could not be read. It acts as `off` for serving and queueing, but nothing may be purged on it.
UNKNOWN = CacheSettings("off", (), False, known=False)


def cache_settings(*, lists_when_off: bool = True) -> CacheSettings:
	"""The switch as saved now. Any problem reads as `UNKNOWN` (`off` for every path that serves or queues).

	The mode is read first. With `lists_when_off=False` a mode of `off` returns at once without reading the
	rest (the request path needs nothing else then, and costs one Singles read). A job deadline is not a
	problem with the switch: it propagates. So does a database error that may have rolled the transaction back;
	it first breaks the snapshot view, as `routing.reader_settings` does.
	"""
	try:
		meta = frappe.get_meta(routing.SETTINGS_DOCTYPE)
		if not all(meta.has_field(field) for field in CACHE_FIELDS):
			return UNKNOWN
		mode = _read(MODE_FIELD)
		if not mode:  # never saved (the read gives an empty value): the field's default is `off`
			return _OFF
		if mode not in MODES:
			return UNKNOWN
		if mode == "off" and not lists_when_off:
			return _OFF
		return CacheSettings(
			mode,
			routing.parse_company_lines(_read(COMPANIES_FIELD)),
			bool(cint(_read(FULL_CADENCE_FIELD))),
		)
	except Exception as exc:
		if routing.may_have_rolled_back(exc):
			routing.poison_view()
		if guard.propagates(exc):
			raise
		return UNKNOWN


def listed_but_missing(current: CacheSettings) -> tuple[str, ...]:
	"""Names in the list that no Company has (a renamed or deleted company): they are never refreshed."""
	names = sorted(set(current.companies))
	if not names:
		return ()
	found = set(frappe.get_all("Company", filters={"name": ("in", names)}, pluck="name"))
	return tuple(name for name in names if name not in found)


def lane_workers() -> int:
	"""Live workers whose queue list is exactly the lane; 0 when the lane is not configured or on any trouble.

	A lane that is not in `workers` of common_site_config has no queue to enqueue to. A generic worker that
	also listens on the lane is not counted: it would take the jobs of the other queues as well.
	"""
	try:
		if LANE_QUEUE not in (frappe.get_conf().get("workers") or {}):
			return 0
		from frappe.utils.background_jobs import generate_qname, get_workers

		lane = [generate_qname(LANE_QUEUE)]
		return sum(1 for worker in get_workers() if (worker.queue_names() or []) == lane)
	except TIMEOUTS:
		raise
	except Exception:
		return 0


def lane_live() -> bool:
	return lane_workers() >= 1


def long_workers() -> int:
	"""Live workers that listen on `long`, generic workers included; 0 on trouble."""
	try:
		from frappe.utils.background_jobs import generate_qname, get_workers

		qname = generate_qname("long")
		return sum(1 for worker in get_workers() if qname in (worker.queue_names() or []))
	except TIMEOUTS:
		raise
	except Exception:
		return 0


def bench_slots(*, night: bool) -> int:
	"""How many refreshes the bench may run at once.

	By day: the lane's workers, so a bench without a lane has none. At night: the `long` workers minus one, but
	at least one when there is any `long` worker, so a standard bench with one generic worker builds one unit at
	a time inside the window. With no `long` worker the night has no slot either.
	"""
	if not night:
		return lane_workers()
	workers = long_workers()
	return max(workers - 1, 1) if workers >= 1 else 0


def view_cap_s(*, night: bool) -> int:
	return VIEW_CAP_NIGHT_S if night else VIEW_CAP_DAY_S


def claim_ttl_s(*, night: bool) -> int:
	"""Life of a claim and of a slot: the view cap + the write deadline + the margin (480 s day, 960 s night)."""
	return view_cap_s(night=night) + WRITE_DEADLINE_S + CLAIM_MARGIN_S


def job_timeout_s(*, night: bool) -> int:
	return JOB_TIMEOUT_NIGHT_S if night else JOB_TIMEOUT_DAY_S


def cache_key(*parts: str) -> str:
	"""A site key under `CACHE_KEY_PREFIX`: `jarvis:pcal:cache:<part>:<part>`."""
	return frappe.cache.make_key(CACHE_KEY_PREFIX + ":".join(parts))


def scan_keys(prefix: str) -> list:
	"""The site keys that start with `prefix`, found with `SCAN` (the `KEYS` command blocks the whole Redis)."""
	return list(frappe.cache.scan_iter(match=frappe.cache.make_key(prefix + "*"), count=SCAN_COUNT))


def slot_key(number: int) -> str:
	"""A slot key. `shared=True` skips the site prefix, so the slots count once for the whole bench."""
	return frappe.cache.make_key(f"{SLOT_PREFIX}{number}", shared=True)


def take_slot(token: str, *, night: bool) -> str | None:
	"""The first free slot key, now held by `token` for a claim's life; None when all are held, none exist or Redis is down."""
	count = bench_slots(night=night)
	if count <= 0:
		return None
	try:
		return redis_slots.take_slot(
			token, [slot_key(number) for number in range(count)], claim_ttl_s(night=night)
		)
	except TIMEOUTS:
		raise
	except Exception:
		return None


def renew_slot(key: str, token: str, *, night: bool) -> bool:
	"""Give a slot a claim's life again, at each cooperative deadline check. False when it is no longer ours."""
	return redis_slots.renew(key, token, claim_ttl_s(night=night))


def release_slot(key: str, token: str) -> None:
	redis_slots.release(key, token)


def _read(field):
	return frappe.db.get_single_value(routing.SETTINGS_DOCTYPE, field, cache=False)
