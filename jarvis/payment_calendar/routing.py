"""Reader routing: the switch in Jarvis Settings, the router, and the build identity of the reader code.

``reader_settings()`` is read fresh on every call and fails closed: a missing field, an unknown
value or any error gives ``off``. It reads the three fields of tabSingles with plain SELECTs, so it is safe
inside the read-only snapshot view.

``read_side`` is the router `snapshot.collect` calls for each side. Today's read decides: the reader is
tried only when today's read refused `too_large` (a served side, or any other refusal, never reaches the
settings or the reader). Every path that does not serve ends in a FRESH `too_large`, raised outside the
`except` blocks that caught the engine's errors, so no frame of an engine stays reachable from it.
Nothing here writes to the database: the keys below are Redis keys under the persistent `jarvis:pcal:`
prefix, all per company and side (never per user):

* slots (`READER_SLOTS` per site): how many readers run at once; held for `SLOT_TTL_S`;
* the in-flight marker: `SET NX` with the job's token; a second job that finds it answers `busy` and nothing is
  remembered, so two users opening the calendar together are both served by today's refusal only while one
  reader runs;
* the memo: set BEFORE the reader runs and owned by the job's token (the value is the in-flight token), so a
  job killed without a trace (OOM, SIGKILL) still leaves it. A job that finds a memo whose token is the live
  in-flight marker's answers `busy` (a read is running); a memo with no live marker is a bad-read memo and
  answers `memo`. A safe normal outcome (served, a refusal, an ineligible user, a deadline cut short by a late
  view) deletes it, only while it is still the job's own. A slow or too big read (`deadline`,
  `too_many_lines`, `assemble_budget`, an unexpected exception) turns it into a plain memo for `MEMO_TTL_S`; a
  deterministic fallback (`control_total`, `pin_drift`...) or a probe that says "too big" for
  `SHORT_MEMO_TTL_S`.

A DB error that may have rolled back the snapshot transaction (a deadlock, a lock wait, a lost connection)
is swallowed like any other reader error, but it also breaks the snapshot view (`poison_view`): with
autocommit off the next SELECT would open a new transaction, and the view's own exit check could not see
that the bank and the obligations were read in different views.
"""

import secrets
from dataclasses import dataclass
from functools import cache
from hashlib import sha256
from pathlib import Path
from time import monotonic

import frappe
from frappe.utils import nowdate

from jarvis.payment_calendar import builtin_reader, read_log, redis_slots, sources, store
from jarvis.payment_calendar.compatibility import source_profile
from jarvis.payment_calendar.errors import (
	TIMEOUTS,
	CalendarSourceUnavailable,
	ReaderFallback,
	safe_code,
)
from jarvis.payment_calendar.read_log import (
	CAUSE,
	DETAIL,
	EXCEPTION,
	FALLBACK,
	PROBE_ERROR,
	REFUSED,
	ROUTED,
	STEP,
)

SETTINGS_DOCTYPE = "Jarvis Settings"
MODE_FIELD = "payment_calendar_reader"
SERVED_FIELD = "payment_calendar_reader_companies"
SHADOW_FIELD = "payment_calendar_reader_shadow_companies"
MODES = ("off", "shadow", "on")
READER_LIST_FIELDS = (SERVED_FIELD, SHADOW_FIELD)
READER_FIELDS = (MODE_FIELD, *READER_LIST_FIELDS)

# Seconds of a job that may pass before the reader starts; a side that would start later is refused.
JOB_BUDGET_S = 90
# Seconds the whole snapshot view may last: the reader's own limit is cut to what is left.
VIEW_CAP_S = 120
# The reader's own limit per side.
READER_DEADLINE_S = 30
# Ledger rows above which a side is not sent to the reader: today's own guard, so the shadow (which reads
# today's lifted read under the same cap) can prove every company the reader may serve.
READER_LEDGER_CAP = sources.MAX_LEDGER_ROWS
# Sides the site may have in the reader at once.
READER_SLOTS = 2
# A slot and an in-flight marker outlive the longest job: the job is killed after RUNNING_TTL.
SLOT_TTL_S = store.RUNNING_TTL + 60
# Seconds a slow, too big or killed read keeps the side out of the reader.
MEMO_TTL_S = 1800
# Seconds a deterministic fallback, or a probe that says "too big", keeps the side out.
SHORT_MEMO_TTL_S = 300
# Fallbacks of a slow or too big read: the next read would be too.
_SLOW_FALLBACKS = frozenset({"deadline", "assemble_budget", "too_many_lines"})
# Fallbacks that repeat identically until something changes: not slow, but not worth a retry per job.
_DETERMINISTIC_FALLBACKS = frozenset(
	{
		"control_total",
		"rekey_unresolved",
		"reader_error",
		"pin_drift",
		"unsupported_major",
		"eligibility_unknown",
	}
)
# Driver errors of one failed statement: a refusal as a reader error.
_STATEMENT_ERRORS = ("OperationalError", "ProgrammingError", "InternalError", "DataError")
# MariaDB errors that roll back the transaction or end the connection: deadlock, lock wait, server gone
# away, connection lost, shutdown, killed, idle transaction timeout.
_ROLLBACK_ERRNOS = frozenset({1213, 1205, 2006, 2013, 2055, 1927, 4031})
# The release script lives in `redis_slots`; the name stays for the tests that run it.
_RELEASE = redis_slots.RELEASE

# The modules whose code decides a figure the reader returns (the reader, the rules it applies, the source it
# must equal, and today's reconciliation). A change to any of them is a new build for the shadow gate.
FIGURE_MODULES = (
	"batch_permission",
	"batch_permission_eval",
	"batch_permission_plan",
	"books",
	"builtin_reader",
	"compatibility",
	"eligibility",
	"errors",
	"permissions",
	"reconciliation",
	"scope",
	"sources",
)
# The modules whose code decides what a cache generation holds and when a user may be served from it (the
# build, the store, the eligibility rules, the serve and the nightly proof). With `FIGURE_MODULES` they make
# `cache_build`: a change to any of them is a new build for the cache shadow gate. Scheduling, the settings and
# the logs are not here.
CACHE_MODULES = (
	"cache_eligibility",
	"cache_refresh",
	"cache_shadow",
	"cache_store",
)
# Every other module of the package: routing, logs, the store, the page API, the cache's scheduling... A new
# file is in none of the three lists until someone decides, and a test fails until they do.
OTHER_MODULES = (
	"__init__",
	"access",
	"api",
	"balances",
	"bank_labels",
	"banks",
	"cache",
	"cache_alerts",
	"cache_state",
	"cache_status",
	"catalog",
	"detail",
	"guard",
	"jobs",
	"lifecycle",
	"preferences",
	"projection",
	"read_log",
	"reader_contract",
	"redis_slots",
	"routing",
	"settings",
	"shadow",
	"shadow_state",
	"snapshot",
	"store",
	"visibility",
)


@dataclass(frozen=True)
class ReaderSettings:
	mode: str
	served: tuple[str, ...]
	shadow: tuple[str, ...]


def reader_settings(*, lists_when_off: bool = True) -> ReaderSettings:
	"""The switch as saved now. Any problem reads as ``off`` with empty lists.

	The mode is read first. With `lists_when_off=False` a mode of `off` returns at once without reading the
	two lists (the router needs nothing else then); the default reads them, for the daily row of the shadow.
	A job deadline is not a problem with the switch: it propagates.
	"""
	try:
		meta = frappe.get_meta(SETTINGS_DOCTYPE)
		if not all(meta.has_field(field) for field in READER_FIELDS):
			return _OFF
		mode = _read(MODE_FIELD)
		if mode not in MODES:
			return _OFF
		if mode == "off" and not lists_when_off:
			return _OFF
		return ReaderSettings(
			mode, parse_company_lines(_read(SERVED_FIELD)), parse_company_lines(_read(SHADOW_FIELD))
		)
	except TIMEOUTS:
		raise
	except Exception as exc:
		if may_have_rolled_back(exc):
			poison_view()
		return _OFF


def read_side(
	today_read, company, side, as_of, window_start, window_end, *, started: float, outcomes: list[dict]
) -> dict:
	"""Today's result for a side, or the reader's when today refused `too_large` and the switch allows it.

	`started` is the `monotonic()` reading when the job began; `outcomes` receives one record per call (no
	names, no text from an error): the path taken, the step or fallback that refused, and the timings.
	"""
	from jarvis.payment_calendar.snapshot import view_is_broken

	record = {"side": side, "path": "today", "outcome": "refused", "company_digest": _digest(company)}
	outcomes.append(record)
	if view_is_broken():
		record.update(routed=True, step="view_broken", today_ms=0)
		raise CalendarSourceUnavailable("too_large") from None
	began = monotonic()
	try:
		result = today_read(company, side, as_of, window_start, window_end)
		record.update(outcome="served", today_ms=_ms(began))
		return result
	except CalendarSourceUnavailable as refusal:
		record["today_ms"] = _ms(began)
		if refusal.code != "too_large":
			record["refusal"] = refusal.code
			raise
	# Outside the `except`: the caught error, with its engine frames, is gone, and a raise below has no context.
	record[ROUTED] = True
	routed = _route(company, side, as_of, window_start, window_end, started, record)
	if routed is None:
		raise CalendarSourceUnavailable("too_large") from None
	record.update(path="reader", outcome="served")
	return routed


def _route(company, side, as_of, window_start, window_end, started, record):
	"""The reader's result, or None with `record` saying which step or fallback refused."""
	try:
		step = _early_step(company, side, started)
		if step:
			record[STEP] = step
			return None
		token = secrets.token_hex(16)
		held: list[bytes] = []
		try:
			step = _admit(company, side, as_of, token, held, record)
			if step:
				record[STEP] = step
				return None
			return _run_reader(company, side, as_of, window_start, window_end, started, record, token)
		finally:
			for key in held:
				_release(key, token)
	except TIMEOUTS as exc:
		# A killed or timed-out job still reports which side the reader was on.
		record[EXCEPTION] = type(exc).__name__
		raise


def _early_step(company, side, started) -> str | None:
	"""The first cheap step that keeps this side out of the reader, or None.

	The switch is read before anything else: in mode `off` nothing is counted, logged or probed.
	"""
	settings = reader_settings(lists_when_off=False)
	if settings.mode != "on" or company not in settings.served:
		return "settings"
	try:
		if source_profile().major != "16":
			return "v15"
	except CalendarSourceUnavailable:
		return "v15"
	if not frappe.conf.get("encryption_key"):
		return "no_key"
	if monotonic() - started > JOB_BUDGET_S:
		return "budget"
	try:
		return _memo_state(company, side)
	except TIMEOUTS:
		raise
	except Exception:
		return "cache"


def _memo_state(company, side) -> str | None:
	"""None without a memo, `busy` when the memo is the live in-flight marker's, else `memo` (a bad read)."""
	value = frappe.cache.execute_command("GET", memo_key(company, side))
	if value is None:
		return None
	live = frappe.cache.execute_command("GET", inflight_key(company, side))
	return "busy" if live is not None and live == value else "memo"


def _admit(company, side, as_of, token, held, record) -> str | None:
	"""Take the in-flight marker, pass the probe, take a slot; the step that refused, or None.

	Every key taken is appended to `held` for the caller to release.
	"""
	try:
		inflight = inflight_key(company, side)
		if not _claim(inflight, token):
			return "busy"
		held.append(inflight)
		# Job A may have failed and written its memo between our first check and this claim.
		if _memo_state(company, side):
			return "memo"
		try:
			exceeds = sources._ledger_rows_exceed(company, side, as_of, READER_LEDGER_CAP)
		except TIMEOUTS:
			raise
		except Exception as exc:
			# A failed statement (say a probe that times out) must not fail the job: today's refusal stands.
			record[PROBE_ERROR] = type(exc).__name__
			if may_have_rolled_back(exc):
				poison_view()
			return "probe"
		if exceeds:
			_remember(memo_key(company, side), SHORT_MEMO_TTL_S)
			return "probe"
		slot = _take_slot(token)
		if slot is None:
			return "busy"
		held.append(slot)
		return None
	except TIMEOUTS:
		raise
	except Exception:
		return "cache"


def _run_reader(company, side, as_of, window_start, window_end, started, record, token):
	"""One reader call under its slot and in-flight marker.

	The memo is set BEFORE the read and owned by `token`; a safe outcome deletes it (while it is still ours),
	a bad one replaces it with a plain memo for the right time.
	"""
	stats, began = {}, monotonic()
	limit = min(READER_DEADLINE_S, VIEW_CAP_S - (began - started))
	# A deadline cut below the reader's own limit by a late view says nothing about the reader.
	cut = limit < READER_DEADLINE_S
	memo = memo_key(company, side)
	_remember(memo, MEMO_TTL_S, token)
	memo_ttl, result = None, None
	try:
		result = builtin_reader.read_obligations(
			company, side, as_of, window_start, window_end, stats=stats, deadline=began + limit
		)
	except TIMEOUTS:
		# A killed read keeps the pessimistic memo; `_route` records the class on its way out.
		raise
	except ReaderFallback as fallback:
		record.update({FALLBACK: fallback.reason, DETAIL: list(fallback.detail)})
		if fallback.__cause__ is not None:
			record[CAUSE] = type(fallback.__cause__).__name__
			if may_have_rolled_back(fallback.__cause__):
				poison_view()
		memo_ttl = _memo_ttl(fallback.reason, cut)
	except CalendarSourceUnavailable as refusal:
		record[REFUSED] = safe_code(refusal, "unknown")
	except Exception as exc:
		record[EXCEPTION] = type(exc).__name__
		memo_ttl = MEMO_TTL_S
		if may_have_rolled_back(exc):
			poison_view()
		if isinstance(exc, _statement_errors()):
			record.update({FALLBACK: "reader_error", CAUSE: type(exc).__name__})
	finally:
		record.update(reader_ms=_ms(began), timings_ms=stats.get("timings_ms"), counts=stats.get("counts"))
	if memo_ttl:
		_remember(memo, memo_ttl)
	else:
		_release(memo, token)
	return result


def may_have_rolled_back(exc) -> bool:
	"""True when `exc` is a database error that may have rolled back the transaction or ended the connection.

	Driver-agnostic and safe on v15 (MariaDB has no `InterfaceError` there): the classes come from
	`frappe.db` when it has them. A statement error (an unknown column, a statement timeout) is not one.
	"""
	if exc is None:
		return False
	if isinstance(exc, (frappe.QueryDeadlockError, frappe.QueryTimeoutError)):
		return True
	interface = getattr(frappe.db, "InterfaceError", ())
	if interface and isinstance(exc, interface):
		return True
	driver = tuple(
		c for c in (getattr(frappe.db, n, None) for n in ("OperationalError", "InternalError")) if c
	)
	return bool(driver) and isinstance(exc, driver) and bool(exc.args) and exc.args[0] in _ROLLBACK_ERRNOS


def poison_view() -> None:
	"""Break the open snapshot view, so the job fails at its exit check instead of mixing two views.

	Inside `snapshot.consistent_read` (`snapshot.view_is_open`) `frappe.db.rollback` is replaced by a function
	that marks the view broken and raises `source_changed`; calling it is the view's own mechanism. Outside a
	view nothing is called and nothing is rolled back, whatever other code left on `frappe.db`.
	"""
	from jarvis.payment_calendar.snapshot import view_is_open

	if not view_is_open():
		return
	try:
		frappe.db.rollback()
	except CalendarSourceUnavailable:
		pass


# The names before they were public. The batch permission code and older tests still call (and patch) them.
_may_have_rolled_back = may_have_rolled_back


def _poison_view() -> None:
	poison_view()


def _today() -> str:
	return today()


def _memo_ttl(reason: str, cut: bool) -> int | None:
	if reason in _SLOW_FALLBACKS:
		return None if reason == "deadline" and cut else MEMO_TTL_S
	return SHORT_MEMO_TTL_S if reason in _DETERMINISTIC_FALLBACKS else None


def _statement_errors() -> tuple[type, ...]:
	return tuple(getattr(frappe.db, name) for name in _STATEMENT_ERRORS)


def slot_key(number: int) -> bytes:
	return frappe.cache.make_key(f"jarvis:pcal:reader:slot:{number}")


def memo_key(company: str, side: str) -> bytes:
	"""The failure memo of one company and side; the company appears only as its digest."""
	return frappe.cache.make_key(f"jarvis:pcal:reader:memo:{read_log.company_digest(company)}:{side}")


def inflight_key(company: str, side: str) -> bytes:
	"""The in-flight marker of one company and side."""
	return frappe.cache.make_key(f"jarvis:pcal:reader:inflight:{read_log.company_digest(company)}:{side}")


def _claim(key: bytes, token: str) -> bool:
	"""`SET NX` with the job's token for a slot's life; False when another job holds the key."""
	return redis_slots.claim(key, token, SLOT_TTL_S)


def _take_slot(token: str) -> bytes | None:
	"""The key of the first free slot, now held by `token`; None when all are held."""
	return redis_slots.take_slot(token, [slot_key(number) for number in range(READER_SLOTS)], SLOT_TTL_S)


# The key and memo primitives are the shared ones (`redis_slots`); `routing` adds only its key names and TTLs.
_release = redis_slots.release
_remember = redis_slots.remember


def listed_but_missing(current: ReaderSettings) -> tuple[str, ...]:
	"""Names in either list that no Company has (a renamed or deleted company): they are never served."""
	names = sorted({*current.served, *current.shadow})
	if not names:
		return ()
	found = set(frappe.get_all("Company", filters={"name": ("in", names)}, pluck="name"))
	return tuple(name for name in names if name not in found)


def today() -> str:
	"""The site date the day counters are keyed by; the one date seam of the reader's logs and the shadow."""
	return nowdate()


def _digest(company: str) -> str | None:
	"""The company's digest for the records, or None without an encryption key (nothing is keyed then)."""
	try:
		return read_log.company_digest(company) if frappe.conf.get("encryption_key") else None
	except TIMEOUTS:
		raise
	except Exception:
		return None


def _ms(started: float) -> int:
	return int((monotonic() - started) * 1000)


def parse_company_lines(text) -> tuple[str, ...]:
	"""One exact company name per line; lines are stripped and blanks dropped."""
	return tuple(line.strip() for line in (text or "").splitlines() if line.strip())


def reader_build() -> str:
	"""Identity of the figure-deciding code, computed once per process.

	It hashes `FIGURE_MODULES` (names and contents) and the Frappe and ERPNext versions, and is read once when
	first asked. A deploy that is not followed by a worker restart keeps the identity of the code the process
	loaded, which is the code that produced the shadow records.
	"""
	return _build_once()


@cache
def _build_once() -> str:
	import erpnext

	return build_identity(Path(__file__).parent, FIGURE_MODULES, frappe.__version__, erpnext.__version__)


def cache_build() -> str:
	"""Identity of the code that builds and serves the cache, computed once per process.

	It hashes `FIGURE_MODULES` and `CACHE_MODULES` (names and contents) and the Frappe and ERPNext versions.
	It is separate from `reader_build`, which hashes `FIGURE_MODULES` alone, so a cache-only change never
	restarts the S2 reader nights.
	"""
	return _cache_build_once()


@cache
def _cache_build_once() -> str:
	import erpnext

	return build_identity(
		Path(__file__).parent, (*FIGURE_MODULES, *CACHE_MODULES), frappe.__version__, erpnext.__version__
	)


def build_identity(directory: Path, modules, frappe_version: str, erpnext_version: str) -> str:
	"""sha256 over the named modules (`<name>.py`) of `directory` and the two versions."""
	digest = sha256()
	for name in sorted(modules):
		# The name goes in too, so moving text between modules changes the build.
		digest.update(name.encode() + b"\0" + (Path(directory) / f"{name}.py").read_bytes() + b"\0")
	digest.update(f"frappe={frappe_version};erpnext={erpnext_version}".encode())
	return digest.hexdigest()


def _read(field):
	return frappe.db.get_single_value(SETTINGS_DOCTYPE, field, cache=False)


_OFF = ReaderSettings("off", (), ())
