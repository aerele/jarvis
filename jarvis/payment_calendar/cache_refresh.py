"""The refresh job of the Payment Calendar cache: preflight, guards, the read, the publish, the chain.

`run(digest, side)` is the RQ job. It runs as Administrator and holds the durable claim of (company, side) while it
works (`cache_store`). Call order, fixed by the store: `claim` -> `publish(keep_claim=...)` -> `next_generation` ->
... -> `release_claim` in a `finally` (which also runs after a job deadline, through a fresh connection). `fail` is
called only for a reader failure BEFORE `publish`; `publish` fails the generation itself, so it is never called
again after `publish` raised. Slots are Redis hints (`cache_state.take_slot/renew_slot/release_slot`); the claim
is the fence. No company, party or voucher name reaches a log, an RQ argument or a Redis key: failures are logged by
class and the job carries the company digest.

What a generation holds. The reader runs with the constant `cache_store.OPEN_SET_WINDOW`, so no settled candidate may enter
`engine.terms` (asserted, else `reader_error`). The rows are every result row (`remaining != 0`), the excluded FX
rows (`settled_fx`), and the RESIDUE rows (`remaining = 0.00`): one for each row the reader's overlay
would show. After the open-set `_assemble`, the settled candidates are replayed as `build_data` treats a selected
one (`replay_candidates`), the terms stay in `engine.terms`, and `_assemble` runs once more over every date; the
zero rows that only this second result has are stored, each with its own `due_date` (`residue_rows`). A refused
candidate (`repair(settled=True)` raises) is one marker row with `window_refusal = 1`. `window_due_dates` is the
row's own date, or for a settled candidate its Payment Schedule dates and header due date (the dates that make a
window select it). `hide_in_window` is always 0. The payload holds the doctype sets before visibility and the
permission columns (the contract of `cache_eligibility`) plus the result-level fields.

Guards: the read runs in one `consistent_read()` with `max_statement_time` = `STATEMENT_CAP_S`; a clock check
on `frappe.db.sql` refuses to START a statement when less than `STATEMENT_CAP_S` of the view cap remain; the line
caps and the row cap are raised to refresh values; an estimate of the lines to fetch (`estimate_bytes`) and
`RLIMIT_AS` bound the memory at `RSS_CAP_MB` above the job's baseline; hooks are checked before the view and again
inside the swap (`verify`). `builtin_reader.py` and `sources.py` are not edited.

Night units (`night=True`) run once per night inside the pre-warm window, do not loop, and call the optional
`cache_shadow.night_oracle(company, side, anchor, rows, payload)` inside the same view . The operator's night build
(`rebuild(use_night_slot=True)`) is a night unit that runs at any time: no window, no night marker. Its oracle
record is marked `source: operator` and the status does not count it as a night.
"""

import contextlib
import functools
import hashlib
import json
import os
import resource
import secrets
import shutil
import time
import traceback
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import frappe
from frappe.core.doctype.server_script.server_script_utils import get_server_script_map
from frappe.utils import getdate, nowdate

from jarvis import _failure_kind
from jarvis.payment_calendar import (
	builtin_reader,
	cache,
	cache_eligibility,
	cache_shadow,
	cache_state,
	cache_store,
	read_log,
	routing,
	snapshot,
	sources,
)
from jarvis.payment_calendar.compatibility import bank_balance_arguments, source_profile
from jarvis.payment_calendar.errors import TIMEOUTS, CalendarSourceUnavailable, ReaderFallback
from jarvis.payment_calendar.permissions import require_company
from jarvis.payment_calendar.reconciliation import InvoiceReconciler
from jarvis.permission_hooks import effective_hooks

SIDES = cache_store.SIDES

# --- Numbers T0 must confirm. Each is a default (or a bound) until the measurement sets it. ---
# Extra address space a refresh may use above the job's baseline.
RSS_CAP_MB = 1024
# Bytes of one ledger line held in Python.
# T0 (partial, playground data, 2026-11-26): the traced peak is 2.55-2.6 KB per line; 2048 was 16 % too low.
BYTES_PER_LINE = 3072
# Ledger lines per discovered invoice, for the estimate before the fetch (T0: estimate accuracy).
# T0 (partial, playground data, 2026-11-26): 5.7 (Receivable) and 8.8 (Payable) lines per invoice; 8 was too low.
LINES_PER_INVOICE = 10
# The reader's caps for a refresh. The line caps count the whole side's history; memory is bounded by the
# estimate and RLIMIT_AS, not by them (T0 sets them, and decides whether buckets are needed).
REFRESH_TOTAL_LINE_CAP = 20_000_000
REFRESH_GENERIC_LINE_CAP = RSS_CAP_MB * 1024 * 1024 // BYTES_PER_LINE
REFRESH_RANK_PARTY_CAP = 2_000_000
# T0 (partial, playground data, 2026-11-26): the rows list takes 3.1-4.6 KB per row; memory binds near 150k rows.
REFRESH_MAX_ROWS = 150_000
# Free disk needed before a refresh: the larger of the floor and 3 x the last generation's bytes.
FREE_DISK_FLOOR_BYTES = 5 * 1024**3
FREE_DISK_FACTOR = 3
# The slot is renewed at a cooperative check at most this often.
SLOT_RENEW_EVERY_S = 30
# A refresh slower than this share of the 15-minute window is logged as a WARNING.
SLOW_REFRESH_S = 450
# --- end of the T0-dependent numbers ---

# Reader fallbacks that mean "this side is too big for one read here": backoff, not a counted failure.
_TOO_BIG = frozenset({"deadline", "too_many_lines", "assemble_budget"})
# The doctypes whose query hooks can shrink an Administrator read, besides every Party Type.
HOOKED_DOCTYPES = (
	"Sales Invoice",
	"Purchase Invoice",
	"Payment Entry",
	"Journal Entry",
	"Payment Ledger Entry",
	"GL Entry",
	"Account",
)
_BANK_DOCTYPES = ("Account", "GL Entry")
_INVOICE = {"Receivable": "Sales Invoice", "Payable": "Purchase Invoice"}
_PARTY = {"Receivable": "Customer", "Payable": "Supplier"}

_clock = time.monotonic
# The reader's own engine class, taken before `refresh_caps` swaps the factory.
_READER_CLASS = builtin_reader._builtin_class


class LeaseLost(Exception):
	"""The admission slot is no longer ours (expired or Redis down): the job stops, uncounted."""


@dataclass(frozen=True)
class RunResult:
	"""`outcome`: `swapped`, `unchanged`, `skipped:<reason>`, `failed:<class>` or `fenced`."""

	outcome: str
	iterations: int = 0
	oracle: dict | None = None


@dataclass(frozen=True)
class Preflight:
	skip: str | None = None
	fingerprint: str = ""
	checked: frozenset = frozenset()
	pairs: frozenset = frozenset()


@dataclass
class Build:
	"""What the view hands over: compact row tuples, the payload and the build's numbers (no engine state)."""

	rows: list
	payload: dict
	stats: cache_store.BuildStats
	doctypes: frozenset
	oracle: dict | None = None


@dataclass
class Extract:
	"""Facts taken from the engine before `_assemble`: DocType sets before visibility and the residue rows."""

	vouchers: set = field(default_factory=set)
	parties: set = field(default_factory=set)
	residue: list = field(default_factory=list)


# ------------------------------------------------------------------------------------------------ the job


def run(
	digest: str, side: str, *, night: bool = False, opened: bool = False, any_time: bool = False
) -> RunResult:
	"""The refresh job. Catches every failure that is not a job deadline and logs its class only.

	`any_time` is the operator's night build (`rebuild(use_night_slot=True)`): a night unit without the pre-warm
	window and without the night marker. It takes the night slot and runs under the night caps.
	"""
	if side not in SIDES:
		return RunResult("skipped:bad_side")
	previous = frappe.session.user
	frappe.set_user("Administrator")
	try:
		return _Chain(digest, side, night, opened, any_time).run()
	except TIMEOUTS:
		raise
	except Exception as exc:
		_log("failed", digest, side, error=type(exc).__name__)
		return RunResult(f"failed:error:{type(exc).__name__}")
	finally:
		frappe.set_user(previous)


def rebuild(company: str, side: str, night: bool = False, use_night_slot: bool = False) -> dict:
	"""Operator: refresh one side now, in this process. Returns `{"outcome", "iterations"}` (printable by `bench execute`).

	The outcome is `swapped`, `unchanged`, `fenced`, `failed:<class>` or `skipped:<reason>`. `skipped:off` and
	`skipped:not_listed` mean that the switch is off or the company is not on the list; it does not clear a memo or
	a backoff (`cache.reset` does). By default the day rule admits the build: it needs a live lane worker, so a bench
	without a lane returns `skipped:no_slot`. With `use_night_slot=True` the build is admitted at any time under the
	night slot rule: it takes the night slot (one `long` worker is enough), runs under the night caps and the
	oracle, and ignores the pre-warm window and the night marker. It holds that worker for the whole run, so run
	it on purpose. The switch and the company list still apply. Run it as
	`bench --site <site> execute jarvis.payment_calendar.cache_refresh.rebuild --kwargs "{'company': '<company>', 'side': 'Receivable', 'use_night_slot': True}"`.
	"""
	digest = read_log.company_digest_or(company)
	if digest is None:
		return {"outcome": "skipped:no_encryption_key", "iterations": 0}
	result = run(digest, side, night=night or use_night_slot, opened=False, any_time=use_night_slot)
	return {"outcome": result.outcome, "iterations": result.iterations}


class _Chain:
	"""One job: admission, the iterations while the claim is held, the release, the hand-over."""

	def __init__(self, digest: str, side: str, night: bool, opened: bool, any_time: bool = False):
		self.digest, self.side, self.night, self.opened = digest, side, night, opened
		# A pre-warm unit runs in the window, once a night (the marker); an operator's night build does neither.
		self.pre_warm = night and not any_time
		self.any_time = any_time
		self.token = secrets.token_hex(16)
		self.started = _clock()
		self.company = None
		self.settings = None
		self.slot = None
		self.held = None
		self.pre = Preflight()
		self.hop = None
		self.kept = False
		self.handover = False
		self.iterations = 0
		self.oracle = None

	def run(self) -> RunResult:
		try:
			try:
				result = self._guarded()
			except TIMEOUTS:
				raise
			except Exception as exc:
				result = self._unexpected(exc)
		finally:
			self._release()
		self._hand_over(result)
		return RunResult(result.outcome, result.iterations, result.oracle)

	def _guarded(self) -> RunResult:
		skip = self._admit()
		if skip:
			cache.count_skip(skip)
			cache.remember_skip(self.digest, self.side, skip)
			_log("skipped", self.digest, self.side, reason=skip)
			return RunResult(f"skipped:{skip}")
		outcome = self._loop()
		return RunResult(outcome, self.iterations, self.oracle)

	def _admit(self) -> str | None:
		self.settings = cache_state.cache_settings()
		self.company = company_for(self.digest, self.settings)
		if not self.settings.active or self.company is None:
			return "off" if not self.settings.active else "not_listed"
		if self.pre_warm and not cache.in_prewarm_window():
			return "outside_window"
		self.slot = cache_state.take_slot(self.token, night=self.night)
		if self.slot is None:
			return "no_slot"
		if self.pre_warm and cache.night_marker_set(self.digest, self.side):
			return "night_done"
		try:
			self.pre = preflight(self.company, self.side, renew=self._renew)
		except Exception as exc:
			if not _failure_kind.too_slow(exc):
				raise
			return self._too_large_before_the_read()
		if self.pre.skip:
			return self.pre.skip
		return self._claim()

	def _renew(self) -> bool:
		return cache_state.renew_slot(self.slot, self.token, night=self.night)

	def _too_large_before_the_read(self) -> str | None:
		"""The preflight aggregate hit the statement cap: record `history_too_large` as a failed generation."""
		skip = self._claim(marker=False)
		if skip:
			return skip
		self._record(self.held, "history_too_large")
		return "history_too_large"

	def _claim(self, *, marker: bool = True) -> str | None:
		try:
			self.held = cache_store.claim(
				self.company,
				self.side,
				anchor=getdate(nowdate()),
				cache_build=routing.cache_build(),
				night=self.night,
				token=self.token,
			)
		except cache_store.NotClaimed as exc:
			return exc.reason
		if marker and self.pre_warm and not cache.take_night_marker(self.digest, self.side):
			# Lost a race for the marker: the generation the claim inserted is closed at once.
			self._record(self.held, "fenced")
			return "night_done"
		if self.opened:
			cache.mark_opened(self.company, self.side)
			frappe.db.commit()
		return None

	def _loop(self) -> str:
		while True:
			outcome = self._iterate()
			self.iterations += 1
			if outcome not in ("swapped", "unchanged") or not self._continues():
				return outcome
			self.settings = cache_state.cache_settings()
			if not self.settings.active or self.company not in self.settings.companies:
				cache.count_skip("off" if not self.settings.active else "not_listed")
				return "skipped:off" if not self.settings.active else "skipped:not_listed"
			again = preflight(self.company, self.side, known=self.pre)
			if again.skip:
				cache.count_skip(again.skip)
				return f"skipped:{again.skip}"
			try:
				self.held = cache_store.next_generation(
					self.held, anchor=getdate(nowdate()), cache_build=routing.cache_build()
				)
			except cache_store.Fenced:
				return "fenced"

	def _continues(self) -> bool:
		"""True when the loop runs again: a continuous chain of an opened company with time left in the job."""
		if self.night or not self.kept:
			return False
		head = cache.heads_of(self.company).get(self.side)
		if not (self.settings.full_cadence and cache.opened(head)):
			return False
		if not cache.continuous(head.duration_ms, full_cadence=True):
			return False
		if self._time_left() < cache_state.LOOP_MIN_LEFT_S:
			self.handover = True
			return False
		return True

	def _time_left(self) -> float:
		return cache_state.job_timeout_s(night=self.night) - (_clock() - self.started)

	def _iterate(self) -> str:
		held, began = self.held, _clock()
		as_of = _db_now()
		_log("start", self.digest, self.side, night=int(self.night))
		try:
			build = self._build()
		except TIMEOUTS:
			raise
		except Exception as exc:
			return self._read_failed(held, exc)
		if self.any_time and build.oracle:
			build.oracle = {**build.oracle, "source": cache_store.OPERATOR_SOURCE}
		self.oracle = build.oracle
		return self._publish(held, build, as_of, began)

	def _build(self) -> Build:
		build = build_generation(
			self.company,
			self.side,
			self.held.anchor,
			night=self.night,
			slot=self.slot,
			token=self.token,
			oracle=cache_shadow.night_oracle if self.night else None,
		)
		build.stats.hook_fingerprint = self.pre.fingerprint
		return build

	def _read_failed(self, held, exc: BaseException) -> str:
		klass = failure_class(exc)
		if isinstance(exc, MemoryError):
			_reopen_connection()
		self._record(held, klass, exc)
		_log("failed", self.digest, self.side, klass=klass)
		return f"failed:{klass}"

	def _unexpected(self, exc: BaseException) -> RunResult:
		"""An error outside the reader's own failure path: counted for the day, failed on the generation, logged.

		The generation (when a claim was taken) is failed with the trace, so that the failure counts on the head,
		starts the memo after three in a row and shows in the status. Without a claim there is no head to count on:
		the day counter and the log line (class and frames, no name) are what is left.
		"""
		klass = failure_class(exc)
		cache.count_skip(f"failed:{klass}")
		if self.held is not None:
			self._record(self.held, klass, exc)
		_log("failed", self.digest, self.side, klass=klass, trace=_frames(exc))
		return RunResult(f"failed:{klass}", self.iterations, self.oracle)

	def _record(self, held, klass: str, exc: BaseException | None = None) -> None:
		try:
			cache_store.fail(held, klass, cache_store.trace_of(exc) if exc else None)
		except TIMEOUTS:
			raise
		except Exception as exc:
			_log("failed", self.digest, self.side, error=type(exc).__name__, record="failed")

	def _publish(self, held, build: Build, as_of, began) -> str:
		keep = not self.night and self.settings.full_cadence
		try:
			published = cache_store.publish(
				held,
				rows=build.rows,
				payload=build.payload,
				as_of=as_of,
				stats=build.stats,
				verify=self._verify(build, began),
				keep_claim=keep,
			)
		except TIMEOUTS:
			raise
		except cache_store.Fenced:
			_log("failed", self.digest, self.side, klass="fenced")
			return "fenced"
		except cache_store.StoreFailure as exc:
			_log("failed", self.digest, self.side, klass=exc.klass)
			return f"failed:{exc.klass}"
		self.kept = published.kept_claim
		self.hop = published.hop_seq
		_log(
			published.outcome if published.outcome == "unchanged" else "done",
			self.digest,
			self.side,
			ms=build.stats.duration_ms,
			rows=published.rows,
			rss=build.stats.peak_rss_mb,
			bytes=published.bytes,
		)
		self._persist(published.generation, build.oracle)
		return published.outcome

	def _persist(self, generation: int, record: dict | None) -> None:
		"""Store a night record on the generation after the view, and commit it.

		A failure never fails the refresh: it is logged by class. `False` means the generation is gone.
		"""
		if not record:
			return
		try:
			stored = cache_shadow.persist(generation, record)
			frappe.db.commit()
		except TIMEOUTS:
			raise
		except Exception as exc:
			with contextlib.suppress(Exception):
				frappe.db.rollback()
			_log("failed", self.digest, self.side, klass=f"persist:{type(exc).__name__}")
			return
		if not stored:
			_log("failed", self.digest, self.side, klass="persist_generation_gone")

	def _verify(self, build: Build, began: float) -> Callable[[], str | None]:
		"""The swap check: the hooks are as at preflight and the DocTypes read were checked.

		It runs last before the head is updated, so it also stamps the iteration's full duration.
		"""

		def verify():
			build.stats.duration_ms = int((_clock() - began) * 1000)
			if hook_fingerprint(self.pre.checked) != self.pre.fingerprint:
				return "not_buildable:query_hook"
			if not build.doctypes <= self.pre.checked:
				return "not_buildable:query_hook"
			return None

		return verify

	def _release(self) -> None:
		if self.held is not None:
			try:
				hop = cache_store.release_claim(self.held)
				self.hop = hop if hop is not None else self.hop
			except TIMEOUTS:
				raise
			except Exception as exc:
				_log("failed", self.digest, self.side, error=type(exc).__name__, record="release")
		if self.slot is not None:
			cache_state.release_slot(self.slot, self.token)

	def _hand_over(self, result: RunResult) -> None:
		"""One hop job under the new hop id when the loop stopped only because the job's time ran out."""
		if self.handover and self.hop is not None and result.outcome in ("swapped", "unchanged"):
			cache.enqueue_hop(self.company, self.side, self.hop)


def company_for(digest: str, settings: cache_state.CacheSettings) -> str | None:
	"""The listed company whose digest is `digest`; None when none is, or the site has no encryption key."""
	for name in settings.companies:
		try:
			if read_log.company_digest(name) == digest:
				return name
		except RuntimeError:
			return None
	return None


def failure_class(exc: BaseException) -> str:
	"""The failure class of a reader-side error (class names only, never a message)."""
	if isinstance(exc, LeaseLost):
		return "lease_lost"
	if isinstance(exc, MemoryError) or _failure_kind.too_slow(exc):
		return "history_too_large"
	if isinstance(exc, ReaderFallback):
		return "history_too_large" if exc.reason in _TOO_BIG else f"reader_fallback:{exc.reason}"
	if isinstance(exc, CalendarSourceUnavailable):
		return f"refused:{exc.code}"
	return f"error:{type(exc).__name__}"


# -------------------------------------------------------------------------------------------- preflight


def preflight(
	company: str, side: str, *, known: Preflight | None = None, renew: Callable[[], bool] | None = None
) -> Preflight:
	"""The skip reason (no generation row is made), or the hook fingerprint and the DocTypes checked.

	`known` is an earlier result of the same job: its control pairs are reused, so a loop makes no new aggregate.
	The memo and the generation bound belong to `cache_store.claim`. `renew` renews the admission slot around the
	control aggregate (`lease_lost` when it says no); that aggregate runs under the statement cap, and a timeout
	there raises MariaDB error 1969 (`_failure_kind.too_slow`); the job records `history_too_large`. A lock
	wait (`frappe.QueryTimeoutError`, error 1205) is not a statement timeout and is no sign of a big history.
	"""
	reason = _static_skip(company)
	if reason:
		return Preflight(reason)
	if _free_disk_bytes() < _needed_disk_bytes(company, side):
		return Preflight("no_disk")
	if known:
		pairs = known.pairs
	else:
		if renew and not renew():
			return Preflight("lease_lost")
		pairs = _control_types(company, side)
		if renew and not renew():
			return Preflight("lease_lost")
	checked = frozenset(_checked_doctypes(side) | pairs)
	if query_hook_offenders(checked):
		return Preflight("not_buildable:query_hook")
	return Preflight(None, hook_fingerprint(checked), checked, pairs)


def _static_skip(company: str) -> str | None:
	try:
		if source_profile().major != "16":
			return "unsupported_major"
	except CalendarSourceUnavailable:
		return "unsupported_major"
	if not frappe.conf.get("encryption_key"):
		return "no_encryption_key"
	if cache_store.company_creation(company) is None:
		return "company_gone"
	if _scheduler_inactive() or frappe.flags.in_migrate:
		return "scheduler_inactive"
	return "deletion_running" if _tdr_running() else None


def _scheduler_inactive() -> bool:
	from frappe.utils.scheduler import is_scheduler_inactive

	return bool(is_scheduler_inactive(verbose=False))


def _tdr_running() -> bool:
	"""A Transaction Deletion Record is queued or running (site-wide). An unreadable answer counts as running."""
	try:
		return bool(
			frappe.db.exists("Transaction Deletion Record", {"status": ["in", ["Queued", "Running"]]})
		)
	except TIMEOUTS:
		raise
	except Exception:
		return True


def _free_disk_bytes() -> int:
	return shutil.disk_usage(frappe.get_site_path()).free


def _needed_disk_bytes(company: str, side: str) -> int:
	row = frappe.db.sql(
		f"SELECT g.bytes FROM `tab{cache_store.HEAD}` h JOIN `tab{cache_store.GENERATION}` g "
		"ON g.name=h.current_generation WHERE h.name=%s",
		(cache_store.head_name(company, side),),
	)
	return max(FREE_DISK_FLOOR_BYTES, FREE_DISK_FACTOR * int((row[0][0] if row else 0) or 0))


def _checked_doctypes(side: str) -> set[str]:
	if side == "Bank":
		return set(_BANK_DOCTYPES)
	return {*HOOKED_DOCTYPES, *frappe.get_all("Party Type", pluck="name")}


def _control_types(company: str, side: str) -> frozenset[str]:
	"""Every voucher and against-voucher type in the side's ledger aggregate, one read before the view."""
	if side == "Bank":
		return frozenset()
	accounts = frappe.get_all("Account", filters={"account_type": side, "company": company}, pluck="name")
	if not accounts:
		return frozenset()
	with frappe.db.execution_timeout(cache_state.STATEMENT_CAP_S):
		control = builtin_reader.ledger_control(company, side, getdate(nowdate()), accounts)
	return frozenset(name for pair in control.pairs for name in pair if name)


def query_hook_offenders(doctypes: Iterable[str]) -> list[str]:
	"""Doctypes (or `*`) that carry a query hook an Administrator read would still apply.

	An unaudited `"*"` hook, any `permission_query_conditions` hook on a listed doctype whose paths are not all
	audited, and any Server Script permission query on one. A hook whose every path is in
	`eligibility.AUDITED_HOOKS` (ERPNext's Company Restriction on Customer and Supplier) is inert for a build that
	has no Company User Permission, so it does not refuse, while the audited sources still hash as audited. An
	audited `"*"` hook is dropped by `effective_hooks`. The hook paths are part of `hook_fingerprint`.
	"""
	hooks = effective_hooks("permission_query_conditions", user="Administrator")
	scripts = (get_server_script_map() or {}).get("permission_query") or {}
	found = ["*"] if isinstance(hooks, dict) and hooks.get("*") else []
	found += [name for name in sorted(doctypes) if name in scripts or _unaudited((hooks or {}).get(name))]
	return found


def _unaudited(paths) -> bool:
	"""True when a doctype's query hooks include a path that is not audited (or the audit no longer holds)."""
	if not paths:
		return False
	if isinstance(paths, str):
		paths = [paths]
	audited = cache_eligibility.AUDITED_HOOKS["permission_query_conditions"]
	if not isinstance(paths, (list, tuple)) or not all(path in audited for path in paths):
		return True
	return not cache_eligibility.audited_sources_ok()


def hook_fingerprint(doctypes: Iterable[str]) -> str:
	"""A digest of the hook values and the Server Script permission queries of `doctypes` and `*` (no names logged)."""
	names = sorted({*doctypes, "*"})
	query = effective_hooks("permission_query_conditions", user="Administrator") or {}
	has = frappe.get_hooks("has_permission", {}) or {}
	scripts = (get_server_script_map() or {}).get("permission_query") or {}
	state = {
		"query": {name: query.get(name) for name in names},
		"has": {name: has.get(name) for name in names},
		"scripts": {name: scripts.get(name) for name in names},
	}
	return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------------------------------------------------- the view


def build_generation(
	company: str,
	side: str,
	anchor: date,
	*,
	night: bool,
	slot,
	token: str,
	oracle: Callable | None = None,
) -> Build:
	"""Read the side in one consistent view under the guards; the rows and payload leave, the engine does not."""
	clock = ViewClock(cache_state.view_cap_s(night=night), slot, token, night)
	started = _clock()
	with address_space_cap(RSS_CAP_MB), frappe.db.execution_timeout(cache_state.STATEMENT_CAP_S):
		with snapshot.consistent_read() as hits, refresh_caps(clock):
			build = (
				_read_bank(company, anchor, hits)
				if side == "Bank"
				else _read_side(company, side, anchor, hits, clock)
			)
			build.stats.read_view_ms = int((_clock() - started) * 1000)
			build.stats.duration_ms = build.stats.read_view_ms
			build.stats.peak_rss_mb = int(_rss_mb())
			if oracle:
				build.oracle = oracle(company, side, anchor, build.rows, build.payload)
	return build


class ViewClock:
	"""The view's clock: no statement starts with less than `STATEMENT_CAP_S` left; the slot renews as it goes."""

	def __init__(self, cap_s: int, slot, token: str, night: bool):
		self.cap_s, self.slot, self.token, self.night = cap_s, slot, token, night
		self.started = self.renewed = _clock()

	def deadline(self) -> float:
		"""The reader's own cooperative deadline, a `time.monotonic()` value."""
		return time.monotonic() + self.cap_s

	def check(self) -> None:
		now = _clock()
		if self.cap_s - (now - self.started) < cache_state.STATEMENT_CAP_S:
			raise ReaderFallback("deadline")
		if self.slot is not None and now - self.renewed >= SLOT_RENEW_EVERY_S:
			self.renewed = now
			if not cache_state.renew_slot(self.slot, self.token, night=self.night):
				raise LeaseLost


@contextmanager
def refresh_caps(clock: ViewClock):
	"""The reader's caps raised to refresh values and the view's clock check on `frappe.db.sql`."""
	caps = {
		"GENERIC_LINE_CAP": REFRESH_GENERIC_LINE_CAP,
		"TOTAL_LINE_CAP": REFRESH_TOTAL_LINE_CAP,
		"RANK_PARTY_CAP": REFRESH_RANK_PARTY_CAP,
		"READER_MAX_ROWS": REFRESH_MAX_ROWS,
	}
	saved = {name: getattr(builtin_reader, name) for name in caps}
	original = frappe.db.sql

	def guarded(*args, **kwargs):
		clock.check()
		return original(*args, **kwargs)

	with contextlib.ExitStack() as stack:
		for name, value in caps.items():
			setattr(builtin_reader, name, value)
		stack.callback(lambda: [setattr(builtin_reader, name, value) for name, value in saved.items()])
		stack.enter_context(_set_attr(builtin_reader, "_builtin_class", _engine_class))
		stack.enter_context(_set_attr(frappe.db, "sql", guarded))
		yield clock


@contextmanager
def _set_attr(target, name: str, value):
	owned = name in vars(target)
	original = getattr(target, name)
	setattr(target, name, value)
	try:
		yield
	finally:
		if owned:
			setattr(target, name, original)
		else:
			delattr(target, name)


@contextmanager
def address_space_cap(extra_mb: int):
	"""`RLIMIT_AS` = the job's present address space + `extra_mb`, restored on exit (fork and no-fork workers)."""
	soft, hard = resource.getrlimit(resource.RLIMIT_AS)
	limit = _address_space_bytes() + extra_mb * 1024 * 1024
	if hard != resource.RLIM_INFINITY:
		limit = min(limit, hard)
	resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
	try:
		yield
	finally:
		resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


def _address_space_bytes() -> int:
	with open("/proc/self/statm") as statm:
		return int(statm.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")


def _rss_mb() -> float:
	with open("/proc/self/statm") as statm:
		return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)


def estimate_bytes(generic_lines: int, invoices: int) -> int:
	"""Bytes the lines of a read would take: the generic types are read whole, the invoices by `LINES_PER_INVOICE`."""
	return (generic_lines + invoices * LINES_PER_INVOICE) * BYTES_PER_LINE


@functools.cache
def _engine_class():
	"""The reader's engine with the estimate check between discovery and the line fetch (before each fetch)."""

	class RefreshEngine(_READER_CLASS()):
		def _discover(self, control):
			discovery = super()._discover(control)
			generic = self.reader_stats["counts"].get("generic_lines", 0)
			if estimate_bytes(generic, len(discovery.invoices())) > RSS_CAP_MB * 1024 * 1024:
				raise ReaderFallback("too_many_lines")
			return discovery

	return RefreshEngine


def _reopen_connection() -> None:
	"""After a `MemoryError` the connection may hold unread packets: close it and open a new one."""
	with contextlib.suppress(Exception):
		frappe.db.close()
	frappe.db.connect()


def _db_now() -> datetime:
	"""The server time, read BEFORE the view starts: the generation's `as_of`."""
	return frappe.db.sql("SELECT NOW(6)")[0][0]


# ------------------------------------------------------------------------------------- receivable, payable


def _read_side(company: str, side: str, anchor: date, hits, clock: ViewClock) -> Build:
	holder: dict = {}
	stats: dict = {}
	with _capturing_assemble(holder):
		result = builtin_reader.read_obligations(
			company,
			side,
			anchor.isoformat(),
			cache_store.OPEN_SET_WINDOW,
			cache_store.OPEN_SET_WINDOW,
			stats=stats,
			deadline=clock.deadline(),
		)
	profile = source_profile()
	snapshot.require_defaults_cache_hit(hits, {side: result}, profile)
	snapshot.check_source_contract(
		hits, {side: result}, None, result["currency"], result["precision"], profile
	)
	extract: Extract = holder["extract"]
	rows = [_row_tuple(row) for row in result["rows"]]
	rows += [_row_tuple(row) for row in result["excluded_rows"]]
	rows += [cache_store.row_tuple(fields) for fields in extract.residue]
	doctypes = frozenset(extract.vouchers | extract.parties)
	payload = _payload(company, side, result, extract)
	return Build(rows, payload, cache_store.BuildStats(), doctypes)


@contextmanager
def _capturing_assemble(holder: dict):
	"""Take the engine's facts around `_assemble`: the DocType sets before visibility and the residue rows."""
	original = sources._assemble

	def assemble(engine, report, account_type, start, end, company_values):
		extract = extract_engine(engine, account_type)
		first = original(engine, report, account_type, start, end, company_values)
		replay = replay_candidates(engine, account_type)
		second = original(engine, report, account_type, date.min, date.max, company_values)
		extract.residue = [*residue_rows(first, second, replay.dates), *replay.markers]
		holder["extract"] = extract
		return first

	with _set_attr(sources, "_assemble", assemble):
		yield


def extract_engine(engine, side: str) -> Extract:
	"""The DocType sets before visibility of an engine that has run with `cache_store.OPEN_SET_WINDOW`.

	Asserts that no settled candidate entered `engine.terms` (else `reader_error`): the window of one day in
	1900 selects none, so a term of a zero-residual key means the window logic changed.
	"""
	for key, terms in engine.terms.items():
		if terms and key in engine.pre and engine.ledger_residuals.get(key) == 0:
			raise ReaderFallback("reader_error", ("settled_candidate_in_terms",))
	return Extract(
		vouchers={_INVOICE[side]} | {r.voucher_type for r in (*engine.data, *engine.excluded)},
		parties={_PARTY[side]} | {r.party_type for r in (*engine.data, *engine.excluded) if r.party_type},
	)


@dataclass
class Replay:
	"""What replaying the settled candidates leaves: their selection dates and a marker for each refusal."""

	dates: dict = field(default_factory=dict)
	markers: list = field(default_factory=list)


def replay_candidates(engine, side: str) -> Replay:
	"""Do for every settled candidate what `build_data` does for one that a window selects.

	A candidate is an invoice with a zero residual that is not in `engine.terms`. Its terms are allocated; a
	non-zero allocated term goes through `repair(settled=True)`, whose refusal (`settlement_schedule_mismatch`)
	becomes a marker row; an invoice with no payment terms is its own single term with outstanding 0. The terms
	STAY in `engine.terms`, so a second `_assemble` over every date shows the overlay rows of the settled
	invoices. `dates` maps (voucher type, voucher) to the dates that make a window select the invoice: its
	Payment Schedule dates and its header due date.
	"""
	precision = engine.currency_precision
	suppressed = {sources._key(r) for r in engine.excluded}
	candidates = {
		key: row
		for key, row in engine.pre.items()
		if engine.is_invoice(row)
		and row.invoiced > 0
		and key not in engine.terms
		and key not in suppressed
		and sources.money(row.invoiced - row.paid - row.credit_note, precision) == 0
	}
	replay, reconciler = Replay(), None
	for key, source in candidates.items():
		row = frappe._dict(source)
		row.invoice_grand_total = row.invoiced
		schedule = engine._schedule_rows(row.voucher_type, row.voucher_no)
		header = engine.metadata.get(row.voucher_no, {}).get("due_date")
		dates = sorted({str(getdate(d)) for d in [*(detail.due_date for detail in schedule), header] if d})
		engine.allocate_outstanding_based_on_payment_terms(row)
		if any(sources.money(term.outstanding, precision) for term in row.payment_terms):
			reconciler = reconciler or InvoiceReconciler(engine, sources.money, sources._key)
			try:
				engine.terms[key] = [reconciler.repair(source, Decimal(0), settled=True)]
			except CalendarSourceUnavailable:
				engine.terms.pop(key, None)
				engine.set_party_details(row)
				replay.markers.append(_marker_fields(engine, side, row, dates))
				continue
		if not row.payment_terms:
			row.outstanding = 0
			engine.terms[key] = [row]
		replay.dates[(row.voucher_type, row.voucher_no)] = dates
	return replay


def residue_rows(first: dict, second: dict, dates: dict) -> list[dict]:
	"""The zero-money rows that the all-dates read has and the open-set read has not, as store fields.

	Each is one reader row (one term) with its own `due_date`. `window_due_dates` is that date for a plain row,
	and the selection dates of the invoice for a row of a settled candidate. `hide_in_window` is 0: the reader
	shows such a row whenever its due date is inside the window.
	"""
	seen = Counter(_body(row) for row in first["rows"])
	found = []
	for row in second["rows"]:
		body = _body(row)
		if seen[body] > 0:
			seen[body] -= 1
		elif Decimal(row["remaining"]) == 0:
			selected = dates.get((row["voucher_type"], row["voucher_no"])) or [row["due_date"]]
			found.append({**_row_fields(row), "residue": 1, "window_due_dates": selected})
	return found


def _body(row: dict) -> str:
	return json.dumps({key: value for key, value in row.items() if key != "id"}, sort_keys=True, default=str)


def _marker_fields(engine, side, row, dates) -> dict:
	"""The row of a refused settled candidate: it stands for the refusal, never shows (no `due_date`)."""
	precision = engine.currency_precision
	money = sources.money
	return {
		"kind": "obligation",
		"voucher_type": row.voucher_type,
		"voucher_no": row.voucher_no,
		"party_type": row.party_type or _PARTY[side],
		"party": row.party,
		"party_name": row.get("supplier_name") or row.get("customer_name") or row.party,
		"account": row.party_account,
		"posting_date": getdate(row.posting_date),
		"due_date": None,
		"term": "",
		"original": money(row.invoiced, precision),
		"paid": money(row.paid, precision),
		"credit": money(row.credit_note, precision),
		"discount": Decimal(0).quantize(Decimal(1).scaleb(-precision)),
		"remaining": Decimal(0).quantize(Decimal(1).scaleb(-precision)),
		"settlement": "Settled",
		"permission": _jsonable(sources._permission_row(row, engine.columns)),
		"residue": 1,
		"window_refusal": 1,
		"window_due_dates": dates,
	}


def _row_fields(row: dict) -> dict:
	"""A reader result row (or an excluded FX row) in the store's field names. Ids are dropped."""
	fields = {key: value for key, value in row.items() if key not in ("id", "_permission")}
	fields["permission"] = _jsonable(row["_permission"])
	return fields


def _row_tuple(row: dict) -> tuple:
	"""A result row (or an excluded FX row) as the store's tuple."""
	return cache_store.row_tuple(_row_fields(row))


def _jsonable(value):
	if isinstance(value, dict):
		return {key: _jsonable(item) for key, item in value.items()}
	if isinstance(value, (list, tuple)):
		return [_jsonable(item) for item in value]
	if isinstance(value, (date, datetime)):
		return value.isoformat()
	if isinstance(value, Decimal):
		return str(value)
	return value if value is None or isinstance(value, (str, int, bool)) else str(value)


def _payload(company: str, side: str, result: dict, extract: Extract) -> dict:
	values = require_company(company)
	return {
		"voucher_doctypes": sorted(extract.vouchers),
		"party_doctypes": sorted(extract.parties),
		"permission_columns": result["permission_columns"],
		"canonical_total": result["canonical_total"],
		"currency": result["currency"],
		"precision": result["precision"],
		"complete_for_company": result["complete_for_company"],
		"scope_reasons": result["scope_reasons"],
		"notes": result["notes"],
		"default_finance_book": values.default_finance_book,
		"open_set_window": cache_store.OPEN_SET_WINDOW,
		"residue_rows": len(extract.residue),
		"window_refusals": sum(fields.get("window_refusal", 0) for fields in extract.residue),
	}


# -------------------------------------------------------------------------------------------------- bank


def _read_bank(company: str, anchor: date, hits) -> Build:
	"""Every enabled bank or cash ledger account with its balance, plus the multibook facts."""
	from erpnext.accounts.utils import (
		FiscalYearError,
		get_balance_on,
		get_currency_precision,
		get_fiscal_year,
	)

	values = require_company(company)
	profile = source_profile()
	try:
		get_fiscal_year(anchor, company=company, verbose=0)
	except FiscalYearError as exc:
		raise CalendarSourceUnavailable("fiscal_year_unavailable") from exc
	names = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": ["in", ["Bank", "Cash"]], "is_group": 0, "disabled": 0},
		pluck="name",
		order_by="name",
	)
	arguments = bank_balance_arguments(get_balance_on)
	precision = get_currency_precision() or 2
	previous = frappe.local.form_dict
	try:
		# ERPNext falls back to request fields even for explicit None arguments.
		frappe.local.form_dict = frappe._dict()
		balances = {
			name: sources.money(
				get_balance_on(
					account=name,
					date=anchor,
					company=company,
					in_account_currency=False,
					party_type=None,
					party=None,
					cost_center=None,
					ignore_account_permission=False,
					**arguments,
				),
				precision,
			)
			for name in names
		}
	finally:
		frappe.local.form_dict = previous
	snapshot.check_source_contract(
		hits, {}, {"precision": precision}, values.default_currency, precision, profile
	)
	payload = {
		"currency": values.default_currency,
		"precision": precision,
		"default_finance_book": values.default_finance_book,
		"ple_book_flag": bool(
			_foreign_books("Payment Ledger Entry", company, anchor, values, {"delinked": 0})
		),
		"multibook_accounts": sorted(_multibook_accounts(company, anchor, names, values)),
	}
	rows = [
		cache_store.row_tuple({"kind": cache_store.KIND_BANK, "account": name, "remaining": balances[name]})
		for name in names
	]
	return Build(rows, payload, cache_store.BuildStats(), frozenset(_BANK_DOCTYPES))


def _foreign_books(doctype: str, company: str, anchor: date, values, filters: dict) -> list:
	"""Finance books other than none or the company default (the rule of `books._validate_books`)."""
	allowed = {None, "", values.default_finance_book}
	books = frappe.get_all(
		doctype,
		filters={"company": company, "posting_date": ["<=", anchor], **filters},
		pluck="finance_book",
		distinct=True,
		limit_page_length=0,
	)
	return [book for book in books if book not in allowed]


def _multibook_accounts(company: str, anchor: date, names: list[str], values) -> set[str]:
	"""Accounts whose GL rows sit in a finance book that is not the default (today's `multibook_unsupported`)."""
	if not names:
		return set()
	allowed = {None, "", values.default_finance_book}
	rows = frappe.get_all(
		"GL Entry",
		filters={
			"company": company,
			"posting_date": ["<=", anchor],
			"account": ["in", names],
			"is_cancelled": 0,
		},
		fields=["account", "finance_book"],
		distinct=True,
		limit_page_length=0,
	)
	return {row.account for row in rows if row.finance_book not in allowed}


# ------------------------------------------------------------------------------------------------ small tools


def _frames(exc: BaseException) -> str:
	"""The function names of the frames of `exc`, innermost last: where it came from, with no message."""
	return ">".join(frame.name for frame in traceback.extract_tb(exc.__traceback__))


def _log(event: str, digest: str, side: str, **fields) -> None:
	"""One INFO line `cache_refresh <event> digest=... side=...`: digests, counts and classes only."""
	text = " ".join(f"{key}={value}" for key, value in fields.items())
	logger = frappe.logger("payment_calendar")
	logger.info(f"cache_refresh {event} digest={digest} side={side} {text}".rstrip())
	if event == "done" and fields.get("ms", 0) > SLOW_REFRESH_S * 1000:
		logger.warning(f"cache_refresh slow digest={digest} side={side} ms={fields['ms']}")
