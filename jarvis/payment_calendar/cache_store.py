"""The store of the Payment Calendar cache: claim, chunk write, read-back, swap, janitor and purge.

This module is the only writer of the three cache tables (Head, Generation, Row). It knows nothing about the
reader: `cache_refresh` builds rows and a payload and hands them to `publish`.

The fence. The durable claim and the purge epoch live on the Head row and every write goes through them.
Redis (`redis_slots`) only admits work; a lost Redis key never lets a second writer through, because every chunk,
the swap and `next_generation` lock the Head row `FOR UPDATE` and compare the claim token and the epoch. A
mismatch raises `Fenced`; the caller never retries it. Time in every fence comparison is the database's
(`NOW(6)`), never the worker's clock, so a worker with a wrong clock cannot widen a claim.

What a generation is. Rows are tuples in `ROW_FIELDS` order (`row_tuple`). Dates become ISO text and amounts become
exact Decimal text BEFORE any checksum (`encode_rows`); a float amount is refused. The row name is
`<generation:012d><seq:08d>`; `row_id` = sha256(side, canonical body, occurrence n). Amounts are text: sum them in
Python with Decimal, never in SQL. The checksum (`identity_checksum`) is order-free (a sum of per-row digests) and
covers rows + payload + anchor + `cache_build` + `company_creation` + side, so equal checksums mean equal rows.
`publish` writes the rows in chunks (one transaction each), reads them back through SQL, compares the checksum, and
swaps in one short transaction. When the checksum equals the current generation's, the swap only advances the
current `as_of` and writes no rows (the no-change skip).

The payload (JSON) of Receivable and Payable holds exactly the keys that `cache_eligibility.read_generation` reads:
`voucher_doctypes`, `party_doctypes` (lists of doctype names before visibility) and `permission_columns`; it may hold
more JSON-native keys (currency, precision, flags). A Decimal or a date in it is refused. A Bank payload is any object.

Transactions. Everything here commits (it is job code), EXCEPT `bump_epoch` and `purge_company`, which run inside a
doc event of someone else's transaction: they never commit. No company or party name goes to a log, a Redis key or an
RQ argument; the purge job takes a digest only.

Janitor rule: a Generation row carries no claim token, so the janitor cannot test
"the head claim has another token" per generation. It uses the equivalent rule: under a live claim, the NEWEST
`building` generation of the side is the claimant's (it is inserted in the claim's own transaction) and is spared;
every other dead one goes once its deadline has passed.
"""

import contextlib
import hashlib
import itertools
import json
import re
import secrets
import time
import traceback
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import frappe

from jarvis.payment_calendar import cache_state, guard, read_log, routing
from jarvis.payment_calendar.cache_eligibility import UnknownGeneration, read_generation
from jarvis.payment_calendar.errors import TIMEOUTS

HEAD = "Jarvis Payment Calendar Cache Head"
GENERATION = "Jarvis Payment Calendar Cache Generation"
ROW = "Jarvis Payment Calendar Cache Row"
SIDES = ("Receivable", "Payable", "Bank")
# `source` of an oracle record that an operator build stored (`rebuild(use_night_slot=True)`): not night evidence.
OPERATOR_SOURCE = "operator"
PURGE_METHOD = "jarvis.payment_calendar.cache_store.run_purge"
PURGE_HOOK = "pcal_cache_purge"
# The `kind` of a stored row that is not an open row: an FX row the reader excludes, and a bank account.
KIND_EXCLUDED = "settled_fx"
KIND_BANK = "bank"
# The window of every generation: wide enough that one copy serves every month.
OPEN_SET_WINDOW = "1900-01-01"

# --- Numbers T0 must confirm. Each is a default or a bound until the measurement sets it. ---
# Rows per write transaction (T0: write transaction time, row bytes).
CHUNK_ROWS = 2_000
# Rows per read-back SELECT (T0: memory and time of the read-back).
READBACK_BATCH_ROWS = 5_000
# Unfinished (`building`, `ready`) generations of a side before a new claim is refused.
GENERATION_BOUND = 3
# A retired generation keeps its rows this long for readers of the old one.
GRACE_S = 900
# Generation metadata (and so the evidence of the shadow gate) lives this long.
METADATA_DAYS = 30
# Rows the janitor deletes per run, and per delete statement.
JANITOR_BUDGET_ROWS = 20_000
JANITOR_CHUNK_ROWS = 2_000
# One janitor run stops after this many seconds, so it cannot hold a job past its deadline (T0: delete speed).
# T0 (partial, playground data, 2026-11-26): 400k rows at 20k rows per s take 20 s; 40 s allows a factor 2 for load.
JANITOR_TIME_BUDGET_S = 40
# Purged generations that one query of the janitor reads (the next page follows until the budget is spent).
PURGED_PAGE = 100
# The most retired generations that `janitor_backlog` counts.
BACKLOG_COUNT_LIMIT = 1000
# Backoff after `history_too_large` (6 h, 12 h, 24 h, then 24 h again) and after 3 other failures in a row.
BACKOFF_LADDER_S = (6 * 3600, 12 * 3600, 24 * 3600)
FAILURE_MEMO_S = 3600
FAILURES_BEFORE_MEMO = 3
# --- end of the T0-dependent numbers ---

# A failure that is not the company's fault: it changes no counter and starts no memo.
UNCOUNTED_FAILURES = frozenset({"fenced", "lease_lost"})
# The most characters of an error trace that a failed generation keeps (the frames nearest the error).
TRACE_CHARS = 2000
MAX_ROWS = 10**8 - 1
_UNSAFE_CLASS = re.compile(r"[^A-Za-z0-9_:.]")

ROW_FIELDS = (
	"kind",
	"voucher_type",
	"voucher_no",
	"party_type",
	"party",
	"party_name",
	"account",
	"posting_date",
	"due_date",
	"term",
	"original",
	"paid",
	"credit",
	"discount",
	"remaining",
	"account_remaining",
	"settlement",
	"allocation_basis",
	"allocation_reason",
	"permission",
	"residue",
	"hide_in_window",
	"window_refusal",
	"window_due_dates",
)
_DATES = frozenset({"posting_date", "due_date"})
_AMOUNTS = frozenset({"original", "paid", "credit", "discount", "remaining", "account_remaining"})
_FLAGS = frozenset({"residue", "hide_in_window", "window_refusal"})
_JSON = frozenset({"permission", "window_due_dates"})
_KIND_AT = ROW_FIELDS.index("kind")
_RESIDUE_AT = ROW_FIELDS.index("residue")

_clock = time.monotonic


class StoreFailure(Exception):
	"""A refusal with a fixed class (`klass`); the message holds no data."""

	klass = "error"

	def __init__(self, reason: str = ""):
		self.reason = reason
		super().__init__(self.klass)


class Fenced(StoreFailure):
	"""The claim, the epoch, the company, the switch or the generation no longer allow this write."""

	klass = "fenced"


class WriteDeadline(StoreFailure):
	"""The write, read-back and swap did not finish inside `WRITE_DEADLINE_S`: a slow database, not a big history."""

	klass = "write_deadline"


class ReadbackFailed(StoreFailure):
	klass = "readback"


class NotBuildable(StoreFailure):
	"""The swap check refused (`klass` is the class the check returned, e.g. `not_buildable:query_hook`)."""

	def __init__(self, klass: str):
		self.klass = klass
		super().__init__(klass)


class NotClaimed(Exception):
	"""No claim was taken; `reason` is one of `company_gone`, `generation_bound`, `memo`, `busy`."""

	def __init__(self, reason: str):
		self.reason = reason
		super().__init__(reason)


@dataclass(frozen=True)
class Claim:
	"""What a job holds while it builds: the fence values read at claim time and its building generation."""

	company: str
	side: str
	token: str
	epoch: int
	company_creation: datetime
	anchor: date
	cache_build: str
	night: bool
	generation: int
	prev: int | None


@dataclass
class BuildStats:
	"""Numbers of the build that the generation and the head record (the store measures the write itself)."""

	duration_ms: int = 0
	peak_rss_mb: int = 0
	read_view_ms: int = 0
	hook_fingerprint: str = ""


@dataclass(frozen=True)
class Published:
	outcome: str  # `swapped` or `unchanged`
	generation: int
	rows: int
	bytes: int
	max_write_txn_ms: int
	age_s: int
	hop_seq: int | None  # the new hop counter when the claim was released, else None
	kept_claim: bool


# ---------------------------------------------------------------------------------------------- public: claim


def claim(
	company: str, side: str, *, anchor: date, cache_build: str, night: bool, token: str | None = None
) -> Claim:
	"""Take the durable claim of (company, side) and insert its `building` generation, in one transaction.

	The own-side janitor runs first. Raises `NotClaimed` (`company_gone`, `generation_bound`, `memo`, `busy`).
	A head whose `company_creation` differs from the Company's (a reused name) is reset: epoch + 1, its
	generations `purged`.
	"""
	_check_side(side)
	creation = company_creation(company)
	if creation is None:
		raise NotClaimed("company_gone")
	janitor(company=company, side=side, cache_build=cache_build)
	if holding_count(company, side) >= GENERATION_BOUND:
		raise NotClaimed("generation_bound")
	token = token or secrets.token_hex(16)
	try:
		head = _lock_head_for_claim(company, side, creation, cache_build)
		frappe.db.sql(
			f"UPDATE `tab{HEAD}` SET claim_token=%s, claim_expires=NOW(6) + INTERVAL %s SECOND WHERE name=%s",
			(token, cache_state.claim_ttl_s(night=night), head_name(company, side)),
		)
		held = Claim(
			company, side, token, head["epoch"], creation, anchor, cache_build, night, 0, head["current"]
		)
		held = replace(held, generation=_insert_generation(held))
		if head["reset"]:
			_enqueue_purge_after_commit(read_log.company_digest_or(company, read_log.SITE_DIGEST))
		frappe.db.commit()
	except BaseException:
		with contextlib.suppress(Exception):
			frappe.db.rollback()
		raise
	return held


def next_generation(claim_: Claim, *, anchor: date | None = None, cache_build: str | None = None) -> Claim:
	"""The next iteration of a looping job: a new `building` generation under the SAME claim.

	Runs the own-side janitor, then locks the head (fence) and reads the generation that is current now as `prev`.
	"""
	janitor(company=claim_.company, side=claim_.side, cache_build=cache_build or claim_.cache_build)
	try:
		head = _fence(claim_)
		again = replace(
			claim_,
			prev=head["current"],
			anchor=anchor or claim_.anchor,
			cache_build=cache_build or claim_.cache_build,
		)
		again = replace(again, generation=_insert_generation(again))
		_renew_in_txn(again)
		frappe.db.commit()
	except BaseException:
		with contextlib.suppress(Exception):
			frappe.db.rollback()
		raise
	return again


def renew(claim_: Claim) -> None:
	"""Give the claim a full life again (`publish` does this per chunk; a caller with a long gap may call it)."""
	try:
		_fence(claim_)
		_renew_in_txn(claim_)
		frappe.db.commit()
	except BaseException:
		with contextlib.suppress(Exception):
			frappe.db.rollback()
		raise


def release_claim(claim_: Claim) -> int | None:
	"""Release the claim by token through a FRESH connection and count a hop; the new `hop_seq`, or None if not ours.

	For a `finally` that also runs after a job deadline: the main connection may hold a lock or be broken, so it
	is rolled back and the release does not use it. Released at swap time already (`keep_claim=False`)? Then the
	token no longer matches and this does nothing.
	"""
	with contextlib.suppress(Exception):
		frappe.db.rollback()
	with _fresh_connection() as db:
		db.sql(
			f"UPDATE `tab{HEAD}` SET claim_token=NULL, claim_expires=NULL, hop_seq=hop_seq + 1 "
			"WHERE name=%s AND claim_token=%s AND purge_epoch=%s",
			(head_name(claim_.company, claim_.side), claim_.token, claim_.epoch),
		)
		changed = db.sql("SELECT ROW_COUNT()")[0][0]
		hop = None
		if changed:
			hop = db.sql(
				f"SELECT hop_seq FROM `tab{HEAD}` WHERE name=%s", (head_name(claim_.company, claim_.side),)
			)[0][0]
		db.commit()
	return hop


# --------------------------------------------------------------------------------------------- public: publish


def publish(
	claim_: Claim,
	*,
	rows: Iterable[tuple],
	payload: dict,
	as_of: datetime,
	stats: BuildStats | None = None,
	verify: Callable[[], str | None] | None = None,
	keep_claim: bool = False,
	deadline_s: int = cache_state.WRITE_DEADLINE_S,
) -> Published:
	"""Write the rows, read them back, and swap (or skip when nothing changed).

	`rows` is a re-iterable of `ROW_FIELDS` tuples (a list). `as_of` is the server time read BEFORE the view
	started. `verify` runs inside the swap transaction after the head lock: it returns None, or a failure class
	such as `not_buildable:query_hook` (hook fingerprint and DocType set). `keep_claim` renews the claim and
	keeps it (a looping job); otherwise the swap clears it and counts a hop.

	On any failure the generation becomes `failed`, the own rows are deleted (up to the janitor budget) and the
	error is raised: `Fenced`, `WriteDeadline`, `ReadbackFailed`, `NotBuildable`, or the original error.
	A job deadline is raised untouched (no database work: the caller's `finally` releases the claim).
	"""
	stats = stats or BuildStats()
	try:
		check_payload(claim_.side, claim_.company, payload)
		return _publish(claim_, rows, payload, as_of, stats, verify, keep_claim, deadline_s)
	except TIMEOUTS:
		raise
	except StoreFailure as exc:
		_abandon(claim_, exc.klass, exc)
		raise
	except Exception as exc:
		_abandon(claim_, f"error:{type(exc).__name__}", exc)
		raise


def trace_of(exc: BaseException) -> str:
	"""The class and the frames of `exc`, cut to the last `TRACE_CHARS` characters. No message and no variable."""
	frames = "".join(traceback.format_tb(exc.__traceback__))
	return f"{type(exc).__name__}\n{frames}"[-TRACE_CHARS:]


def fail(claim_: Claim, klass: str, trace: str | None = None) -> None:
	"""Mark the claim's generation `failed` with its class and trace, count the failure on the head, delete own rows.

	`fenced` and `lease_lost` change no counter. `history_too_large` walks the 6/12/24 h ladder. Any other class
	counts; the third in a row starts a 1 h memo. The head is touched only while the claim is still ours, and only
	when this call is the one that failed a generation that was still `building`: a second call, and a call after
	the swap, count nothing.
	"""
	klass = _safe_class(klass)
	frappe.db.rollback()
	head = _first(
		f"SELECT purge_epoch, claim_token, backoff_level, failures FROM `tab{HEAD}` WHERE name=%s FOR UPDATE",
		(head_name(claim_.company, claim_.side),),
	)
	frappe.db.sql(
		f"UPDATE `tab{GENERATION}` SET state='failed', outcome='failed', failure_class=%s, failure_trace=%s, "
		"modified=NOW(6) WHERE name=%s AND state='building'",
		(klass, trace, claim_.generation),
	)
	changed = frappe.db.sql("SELECT ROW_COUNT()")[0][0]
	if changed and head and head[1] == claim_.token and head[0] == claim_.epoch:
		_record_failure(claim_.company, claim_.side, klass, level=head[2], failures=head[3])
	frappe.db.commit()
	_delete_rows(claim_.generation, _Budget(JANITOR_BUDGET_ROWS))


def fail_orphan(company: str, side: str, generation: int, klass: str) -> bool:
	"""Fail a `building` generation whose writer is gone, and count the failure unless it is stale news. True when it failed.

	For a tick that finds a generation past its deadline with no failure recorded (the writer died: an OOM
	kill, a reboot, a `MemoryError` whose connection could not be reopened). The head's claim token may be
	cleared already, so nothing here compares it. The head is locked first (same order as `fail`), then the
	generation moves from `building` to `failed` only if its deadline has passed and it is not the newest
	building generation of a live claim.

	The failure is counted on the head only when nothing newer decides the head's state: a later generation of
	the side (a retry that runs or ran), a newer current generation, or a success since the orphan began make
	the orphan stale, and it is failed with no counter and no memo. A second call finds it `failed` and does nothing.
	"""
	_check_side(side)
	klass = _safe_class(klass)
	frappe.db.rollback()
	head = _first(
		f"SELECT backoff_level, failures, current_generation, last_success FROM `tab{HEAD}` WHERE name=%s FOR UPDATE",
		(head_name(company, side),),
	)
	changed, stale = 0, False
	if not _is_live_claimants_generation(generation, company, side):
		stale = _superseded(company, side, generation, head)
		frappe.db.sql(
			f"UPDATE `tab{GENERATION}` SET state='failed', outcome='failed', failure_class=%s, modified=NOW(6) "
			"WHERE name=%s AND company=%s AND side=%s AND state='building' AND deadline < NOW(6)",
			(klass, generation, company, side),
		)
		changed = frappe.db.sql("SELECT ROW_COUNT()")[0][0]
	if changed and head and not stale:
		_record_failure(company, side, klass, level=head[0], failures=head[1])
	frappe.db.commit()
	if changed:
		_delete_rows(generation, _Budget(JANITOR_BUDGET_ROWS))
	return bool(changed)


def _superseded(company: str, side: str, generation: int, head) -> bool:
	"""True when something newer than `generation` already decides the state of the side's head."""
	if head and head[2] is not None and head[2] > generation:
		return True
	if _first(
		f"SELECT 1 FROM `tab{GENERATION}` WHERE company=%s AND side=%s AND name > %s LIMIT 1",
		(company, side, generation),
	):
		return True
	began = _first(f"SELECT creation FROM `tab{GENERATION}` WHERE name=%s", (generation,))
	return bool(head and head[3] is not None and began and head[3] > began[0])


def memo_blocks(company: str, side: str, cache_build: str) -> bool:
	"""True while a backoff or failure memo of this head applies to this build (a new build lifts it)."""
	row = _first(
		f"SELECT (memo_until IS NOT NULL AND memo_until > NOW(6)) FROM `tab{HEAD}` WHERE name=%s",
		(head_name(company, side),),
	)
	return bool(row and row[0]) and _memo_build_matches(company, side, cache_build)


def janitor_backlog() -> dict:
	"""What the janitor has not finished: `purged` generations, and `retired` ones past the grace that still hold rows.

	`oldest_s` is the age of the oldest of them. The count of retired generations is read up to a bound (the
	status shows how far behind the janitor is, it does not need the exact number of a huge backlog).
	"""
	purged = _first(
		f"SELECT COUNT(*), TIMESTAMPDIFF(SECOND, MIN(modified), NOW(6)) FROM `tab{GENERATION}` WHERE state='purged'"
	)
	retired = frappe.db.sql(
		f"SELECT TIMESTAMPDIFF(SECOND, g.modified, NOW(6)) FROM `tab{GENERATION}` g WHERE g.state='retired' "
		f"AND g.modified < NOW(6) - INTERVAL %s SECOND AND {_has_rows('g.name')} ORDER BY g.modified LIMIT %s",
		(GRACE_S, BACKLOG_COUNT_LIMIT),
	)
	ages = [
		age for age in ([purged[1]] if purged[0] else []) + [row[0] for row in retired[:1]] if age is not None
	]
	return {"purged": purged[0], "retired_with_rows": len(retired), "oldest_s": int(max(ages)) if ages else 0}


def holding_count(company: str, side: str) -> int:
	"""Unfinished generations of the side: `building` and `ready`.

	`retired` is terminal: its rows wait for the grace and the janitor, and a fast chain keeps several of them,
	so counting them would refuse a claim at the cadence of a fast chain. This bounds writers that left work behind.
	"""
	return _first(
		f"SELECT COUNT(*) FROM `tab{GENERATION}` WHERE company=%s AND side=%s AND state IN ('building', 'ready')",
		(company, side),
	)[0]


def company_creation(company: str) -> datetime | None:
	"""`creation` of the Company, or None when it does not exist (a reused name has another value)."""
	return frappe.db.get_value("Company", company, "creation")


def site_clock_offset_s() -> int:
	"""Seconds to add to a database time to get site time (the zone of `frappe.utils.now_datetime`).

	A generation's `as_of` is database time (`NOW(6)`); Frappe stamps `creation` and `modified` in site time, and
	the two zones differ on a real site. Zone offsets are whole quarter hours, so rounding to 900 s drops the
	few seconds of skew and the time between the two reads.
	"""
	database_now = frappe.db.sql("SELECT NOW(6)")[0][0]
	return round((frappe.utils.now_datetime() - database_now).total_seconds() / 900) * 900


def head_name(company: str, side: str) -> str:
	"""The Head's name: a hash of (company, side), so `INSERT IGNORE` is idempotent and nothing parses it."""
	return hashlib.sha256(f"{company}\x00{side}".encode()).hexdigest()[:40]


# ---------------------------------------------------------------------------------------- public: purge, janitor


def bump_epoch(company: str) -> bool:
	"""Fence every writer of the company and mark its generations `purged`, in the CALLER's transaction.

	Never commits (a doc event runs with transaction control off). Locks the heads in the order of `SIDES`,
	bumps `purge_epoch`, clears claim, current generation and memo, and marks every generation `purged`. Database
	errors propagate, so the caller fails as one unit. Returns True when there was anything to purge.
	"""
	# The order of `SIDES`, the order in which a request marks the heads as opened: one order, no deadlock.
	heads = frappe.db.sql(
		f"SELECT name FROM `tab{HEAD}` WHERE company=%s ORDER BY FIELD(side, {', '.join(['%s'] * len(SIDES))}) "
		"FOR UPDATE",
		(company, *SIDES),
	)
	frappe.db.sql(
		f"UPDATE `tab{HEAD}` SET purge_epoch=purge_epoch + 1, claim_token=NULL, claim_expires=NULL, "
		"current_generation=NULL, memo_until=NULL, backoff_level=0, failures=0, last_failure=NULL "
		"WHERE company=%s",
		(company,),
	)
	frappe.db.sql(
		f"UPDATE `tab{GENERATION}` SET state='purged', modified=NOW(6) WHERE company=%s AND state != 'purged'",
		(company,),
	)
	marked = frappe.db.sql("SELECT ROW_COUNT()")[0][0]
	return bool(heads) or bool(marked)


def purge_company(company: str) -> bool:
	"""`bump_epoch` + the `pcal_cache_purge` hooks + a purge job after the caller's commit. Never commits.

	The hooks are Redis-only. The job is queued on `long` (never the lane) from an
	`after_commit` callback that logs an enqueue error by class: the janitor resumes any `purged` generation.
	"""
	bumped = bump_epoch(company)
	digest = read_log.company_digest_or(company, read_log.SITE_DIGEST)
	notify_purged(digest)
	if bumped:
		_enqueue_purge_after_commit(digest)
	return bumped


def notify_purged(digest: str) -> None:
	"""Call the `pcal_cache_purge` hooks. Other errors are logged by class; rollback-class DB errors and deadlines propagate."""
	for path in frappe.get_hooks(PURGE_HOOK) or []:
		guard.guarded(lambda path=path: frappe.get_attr(path)(digest), "cache_purge_hook")


def run_purge(digest: str | None = None) -> int:
	"""The purge job: delete the rows and the metadata of every `purged` generation, then the heads of gone companies.

	Acts only on `purged` generations (a rollback to a savepoint keeps after-commit callbacks, so a queued job may
	find nothing to do). `digest` is for the log and the job id only. Returns the rows deleted.
	"""
	spent = _Budget(10**12)
	_resume_purged(_Scope(), spent)
	return 10**12 - spent.left


def janitor(
	*,
	company: str | None = None,
	side: str | None = None,
	cache_build: str | None = None,
	listed: Collection[str] | None = None,
	budget: int | None = None,
	time_budget_s: float = JANITOR_TIME_BUDGET_S,
) -> int:
	"""Delete what is finished with, within `budget` rows and `time_budget_s` seconds. Commits.

	Returns the rows deleted. With no `budget`, a run for one side (`company` and `side`) is sized to the data:
	at least twice the rows of the side's newest generation, so the chain can never write faster than it deletes
	(retired generations unbounded otherwise). Other runs use `JANITOR_BUDGET_ROWS`.

	`company` and `side` narrow it to one side (the call before an insert). `listed` is the cache company list;
	with it (and no `company`) every company that has heads but is not listed is purged (the tick when the mode is
	`off` or a company left the list). `cache_build` enables the old-build rule.
	"""
	if budget is None:
		budget = _own_side_budget(company, side) if company and side else JANITOR_BUDGET_ROWS
	spent, scope = _Budget(budget, time_budget_s), _Scope(company, side)
	if listed is not None and company is None:
		_purge_unlisted(set(listed))
	_purge_gone_companies(scope)
	_resume_purged(scope, spent)
	_delete_retired_rows(scope, spent)
	_delete_dead(scope, spent)
	if company is None:
		_delete_orphan_rows(spent)
	_delete_old_metadata(scope, spent, cache_build)
	return budget - spent.left


def _own_side_budget(company: str, side: str) -> int:
	row = _first(
		f"SELECT row_count FROM `tab{GENERATION}` WHERE company=%s AND side=%s AND state IN ('current', 'retired') "
		"ORDER BY name DESC LIMIT 1",
		(company, side),
	)
	return max(JANITOR_BUDGET_ROWS, 2 * (row[0] if row else 0))


# ------------------------------------------------------------------------------------------------ row codec


def row_tuple(fields: dict) -> tuple:
	"""A row as the tuple `publish` takes: values in `ROW_FIELDS` order; a flag defaults to 0, other fields to None."""
	unknown = set(fields) - set(ROW_FIELDS)
	if unknown:
		raise ValueError("A cache row has an unknown field.")
	return tuple(fields.get(name, 0 if name in _FLAGS else None) for name in ROW_FIELDS)


def amount_text(value) -> str:
	"""Exact Decimal text of an amount. A float or a bool is refused: money here is never inexact."""
	if isinstance(value, (float, bool)):
		raise TypeError("An amount is a Decimal, an int or Decimal text.")
	try:
		number = Decimal(value) if isinstance(value, (Decimal, int, str)) else None
	except InvalidOperation:
		raise ValueError("An amount is not a number.") from None
	if number is None:
		raise TypeError("An amount is a Decimal, an int or Decimal text.")
	if not number.is_finite():
		raise ValueError("An amount is not finite.")
	return format(number, "f")


def encode_rows(side: str, rows: Iterable[tuple]) -> Iterator[dict]:
	"""Each row as `{"values", "canon", "row_id", "bytes"}`, in input order; `row_id` counts identical bodies."""
	seen: dict[str, int] = {}
	for row in rows:
		encoded = encode_row(row)
		body = json.dumps(encoded["canon"], separators=(",", ":"), ensure_ascii=False)
		seen[body] = seen.get(body, 0) + 1
		encoded["row_id"] = hashlib.sha256(f"{side}\x00{body}\x00{seen[body]}".encode()).hexdigest()
		yield encoded


def encode_row(row: tuple) -> dict:
	"""`values` (what is stored: ISO date text, Decimal text, 0/1, canonical JSON text, None for empty) and `canon`."""
	if len(row) != len(ROW_FIELDS):
		raise ValueError("A cache row has the wrong number of fields.")
	values = [_encode_value(name, value) for name, value in zip(ROW_FIELDS, row, strict=True)]
	return {
		"values": values,
		"canon": canonical_of_stored(values),
		"bytes": sum(len(value.encode()) for value in values if isinstance(value, str)),
		"row_id": None,
	}


def canonical_of_stored(values) -> list:
	"""The checksum form of a row from STORED values (what the build holds, or what a SELECT returns)."""
	return [_canonical_value(name, value) for name, value in zip(ROW_FIELDS, values, strict=True)]


class RowDigest:
	"""An order-free digest of rows: their count and the sum of their sha256 values (mod 2**256)."""

	def __init__(self):
		self.count = 0
		self.total = 0

	def add(self, encoded: dict) -> None:
		text = json.dumps([encoded["row_id"], *encoded["canon"]], separators=(",", ":"), ensure_ascii=False)
		self.total = (self.total + int.from_bytes(hashlib.sha256(text.encode()).digest(), "big")) % (1 << 256)
		self.count += 1


def identity_checksum(
	digest: RowDigest,
	*,
	payload: dict,
	anchor: date,
	cache_build: str,
	company_creation: datetime,
	side: str,
) -> str:
	"""The reuse identity: rows + payload + anchor + build + company creation + side. 64 hex characters."""
	text = json.dumps(
		{
			"rows": digest.count,
			"sum": format(digest.total, "x"),
			"payload": payload_text(payload),
			"anchor": anchor.isoformat(),
			"build": cache_build,
			"creation": company_creation.strftime("%Y-%m-%d %H:%M:%S.%f"),
			"side": side,
		},
		sort_keys=True,
		separators=(",", ":"),
	)
	return hashlib.sha256(text.encode()).hexdigest()


def payload_text(payload: dict) -> str:
	"""Canonical JSON of a payload. A value that is not JSON-native (a Decimal, a date) raises `TypeError`."""
	return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def check_payload(side: str, company: str, payload) -> None:
	"""The payload contract: Receivable and Payable need what `cache_eligibility.read_generation` reads."""
	_check_side(side)
	if not isinstance(payload, dict):
		raise ValueError("A cache payload is an object.")
	text = payload_text(payload)
	if side == "Bank":
		return
	try:
		read_generation({"company": company, "side": side, "payload": text}, company)
	except UnknownGeneration:
		raise ValueError("A cache payload lacks the doctype lists or the permission columns.") from None


def _encode_value(name: str, value):
	if name in _DATES:
		return _encode_date(value)
	if name in _AMOUNTS:
		return None if value is None else amount_text(value)
	if name in _FLAGS:
		return 1 if value else 0
	if name in _JSON:
		return _encode_json(value)
	return None if value is None or value == "" else str(value)


def _encode_date(value) -> str | None:
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value.date().isoformat()
	if isinstance(value, date):
		return value.isoformat()
	if isinstance(value, str):
		return date.fromisoformat(value).isoformat()
	raise TypeError("A date field holds a date or ISO text.")


def _encode_json(value) -> str | None:
	if value is None:
		return None
	if not isinstance(value, (dict, list)):
		raise TypeError("A JSON field holds an object, a list or None.")
	return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _canonical_value(name: str, value):
	if name in _FLAGS:
		return int(value or 0)
	if value is None:
		return ""
	if name in _DATES:
		return value.isoformat() if isinstance(value, date) else str(value)
	if name in _JSON:
		return json.dumps(json.loads(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	return str(value)


# ----------------------------------------------------------------------------------------- publish internals


def _publish(claim_, rows, payload, as_of, stats, verify, keep_claim, deadline_s) -> Published:
	meta = _measure(claim_, rows, payload)
	deadline_at = _clock() + deadline_s
	if meta["prev_same"] and claim_.prev is not None:
		return _swap(claim_, meta, as_of, stats, verify, keep_claim, 0, unchanged=True)
	_begin_write(claim_, meta, as_of, stats)
	slowest = _write_chunks(claim_, rows, deadline_at)
	_read_back(claim_, meta["checksum"], deadline_at)
	_check_deadline(deadline_at)
	return _swap(claim_, meta, as_of, stats, verify, keep_claim, slowest, unchanged=False)


def _measure(claim_, rows, payload) -> dict:
	"""One pass over the rows: counts, bytes and the checksum; and whether the current generation has the same one."""
	digest, size, residue, excluded = RowDigest(), 0, 0, 0
	for encoded in encode_rows(claim_.side, rows):
		digest.add(encoded)
		size += encoded["bytes"]
		residue += 1 if encoded["values"][_RESIDUE_AT] else 0
		excluded += 1 if encoded["values"][_KIND_AT] == KIND_EXCLUDED else 0
	if digest.count > MAX_ROWS:
		raise ValueError("A generation holds too many rows.")
	checksum = identity_checksum(
		digest,
		payload=payload,
		anchor=claim_.anchor,
		cache_build=claim_.cache_build,
		company_creation=claim_.company_creation,
		side=claim_.side,
	)
	current = (
		_first(f"SELECT checksum FROM `tab{GENERATION}` WHERE name=%s AND state='current'", (claim_.prev,))
		if claim_.prev
		else None
	)
	return {
		"rows": digest.count,
		"bytes": size,
		"residue": residue,
		"excluded": excluded,
		"checksum": checksum,
		"payload": payload_text(payload),
		"prev_same": bool(current and current[0] == checksum),
	}


def _set_meta(claim_, meta, as_of, stats, **extra) -> None:
	frappe.db.sql(
		f"UPDATE `tab{GENERATION}` SET as_of=%s, row_count=%s, residue_count=%s, excluded_count=%s, bytes=%s, "
		"checksum=%s, payload=%s, hook_fingerprint=%s, duration_ms=%s, peak_rss_mb=%s, read_view_ms=%s, "
		"max_write_txn_ms=%s WHERE name=%s",
		(
			as_of,
			meta["rows"],
			meta["residue"],
			meta["excluded"],
			meta["bytes"],
			meta["checksum"],
			meta["payload"],
			stats.hook_fingerprint or None,
			stats.duration_ms,
			stats.peak_rss_mb,
			stats.read_view_ms,
			extra.get("max_write_txn_ms", 0),
			claim_.generation,
		),
	)


def _begin_write(claim_, meta, as_of, stats) -> None:
	try:
		_fence(claim_)
		_set_meta(claim_, meta, as_of, stats)
		frappe.db.commit()
	except BaseException:
		with contextlib.suppress(Exception):
			frappe.db.rollback()
		raise


def _write_chunks(claim_, rows, deadline_at) -> int:
	"""Insert the rows in chunks, one transaction each; returns the slowest transaction in ms."""
	slowest, seq = 0, 0
	source = encode_rows(claim_.side, rows)
	while chunk := list(itertools.islice(source, CHUNK_ROWS)):
		_check_deadline(deadline_at)
		started = _clock()
		try:
			_fence(claim_)
			_insert_rows(claim_.generation, seq, chunk)
			_renew_in_txn(claim_)
			frappe.db.commit()
		except BaseException:
			with contextlib.suppress(Exception):
				frappe.db.rollback()
			raise
		slowest = max(slowest, int((_clock() - started) * 1000))
		seq += len(chunk)
	return slowest


def _insert_rows(generation: int, start: int, chunk: list[dict]) -> None:
	columns = ", ".join(f"`{name}`" for name in ("row_id", *ROW_FIELDS))
	slot = (
		"(%s, NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0, %s, "
		+ ", ".join(["%s"] * (len(ROW_FIELDS) + 1))
		+ ")"
	)
	flat: list = []
	for offset, encoded in enumerate(chunk):
		flat += [f"{generation:012d}{start + offset:08d}", generation, encoded["row_id"], *encoded["values"]]
	frappe.db.sql(
		f"INSERT INTO `tab{ROW}` (name, creation, modified, modified_by, owner, docstatus, idx, generation, {columns}) "
		f"VALUES {', '.join([slot] * len(chunk))}",
		flat,
	)


def _read_back(claim_, expected: str, deadline_at) -> None:
	"""Recompute the checksum from the rows and the generation row as the database holds them (L4)."""
	stored = _first(
		f"SELECT payload, anchor, cache_build, company_creation, side FROM `tab{GENERATION}` WHERE name=%s",
		(claim_.generation,),
	)
	digest, last = RowDigest(), ""
	names = ", ".join(f"`{name}`" for name in ("row_id", *ROW_FIELDS))
	while True:
		_check_deadline(deadline_at)
		batch = frappe.db.sql(
			f"SELECT name, {names} FROM `tab{ROW}` WHERE generation=%s AND name > %s ORDER BY name LIMIT {int(READBACK_BATCH_ROWS)}",
			(claim_.generation, last),
		)
		if not batch:
			break
		for record in batch:
			digest.add({"row_id": record[1], "canon": canonical_of_stored(record[2:])})
		last = batch[-1][0]
	if stored is None or stored[0] is None:
		raise ReadbackFailed("generation")
	found = identity_checksum(
		digest,
		payload=json.loads(stored[0]),
		anchor=stored[1],
		cache_build=stored[2],
		company_creation=stored[3],
		side=stored[4],
	)
	if found != expected:
		raise ReadbackFailed("checksum")


def _swap(claim_, meta, as_of, stats, verify, keep_claim, slowest, *, unchanged: bool) -> Published:
	"""Step 7: one transaction. Checks, then new -> `current` and prev -> `retired`, or the no-change skip."""
	try:
		# End the read-back snapshot, so the company and the switch are read as they are now.
		frappe.db.commit()
		published = _swap_in_txn(claim_, meta, as_of, stats, verify, keep_claim, slowest, unchanged)
		frappe.db.commit()
	except BaseException:
		with contextlib.suppress(Exception):
			frappe.db.rollback()
		raise
	return published


def _swap_in_txn(claim_, meta, as_of, stats, verify, keep_claim, slowest, unchanged) -> Published:
	head = _fence(claim_)
	_check_world(claim_, head)
	failure = verify() if verify else None
	if failure:
		raise NotBuildable(failure)
	if unchanged:
		_set_meta(claim_, meta, as_of, stats)
	row = _first(
		f"SELECT state, row_count FROM `tab{GENERATION}` WHERE name=%s FOR UPDATE", (claim_.generation,)
	)
	if row is None or row[0] != "building" or row[1] != meta["rows"]:
		raise Fenced("generation")
	if head["current"] != claim_.prev:
		raise Fenced("cas")
	if not unchanged and _stored_rows(claim_.generation) != meta["rows"]:
		raise Fenced("rows")
	age = _age_s(claim_.prev)
	if unchanged:
		_skip_swap(claim_, as_of)
		generation = claim_.prev
	else:
		_replace_current(claim_, slowest)
		generation = claim_.generation
	hop = _finish_head(claim_, meta, stats, age, keep_claim)
	return Published(
		"unchanged" if unchanged else "swapped",
		generation,
		meta["rows"],
		meta["bytes"],
		slowest,
		age,
		hop,
		keep_claim,
	)


def _stored_rows(generation: int) -> int:
	return frappe.db.sql(f"SELECT COUNT(*) FROM `tab{ROW}` WHERE generation=%s", (generation,))[0][0]


def _check_world(claim_, head) -> None:
	"""The company, the switch and the list must still allow a swap."""
	if (
		company_creation(claim_.company) != claim_.company_creation
		or head["creation"] != claim_.company_creation
	):
		raise Fenced("company_changed")
	settings = cache_state.cache_settings()
	if not settings.active:
		raise Fenced("mode")
	if claim_.company not in settings.companies:
		raise Fenced("unlisted")


def _skip_swap(claim_, as_of) -> None:
	"""No rows: the current generation lives on with the new `as_of`; the new row is evidence only."""
	frappe.db.sql(
		f"UPDATE `tab{GENERATION}` SET as_of=%s WHERE name=%s AND state='current'", (as_of, claim_.prev)
	)
	frappe.db.sql(
		f"UPDATE `tab{GENERATION}` SET state='retired', outcome='unchanged', modified=NOW(6) WHERE name=%s",
		(claim_.generation,),
	)


def _replace_current(claim_, slowest: int) -> None:
	frappe.db.sql(
		f"UPDATE `tab{GENERATION}` SET state='current', outcome='swapped', max_write_txn_ms=%s, modified=NOW(6) "
		"WHERE name=%s",
		(slowest, claim_.generation),
	)
	if claim_.prev is not None:
		frappe.db.sql(
			f"UPDATE `tab{GENERATION}` SET state='retired', modified=NOW(6) WHERE name=%s AND state='current'",
			(claim_.prev,),
		)


def _finish_head(claim_, meta, stats, age: int, keep_claim: bool) -> int | None:
	"""Head metrics and the claim: renewed when the job loops, cleared (and a hop counted) when it stops."""
	current = claim_.prev if meta["prev_same"] else claim_.generation
	claim_sql = (
		"claim_expires=NOW(6) + INTERVAL %s SECOND"
		if keep_claim
		else "claim_token=NULL, claim_expires=NULL, hop_seq=hop_seq + 1"
	)
	values: list = [
		current,
		stats.duration_ms,
		meta["rows"],
		stats.peak_rss_mb,
		age,
		age,
	]
	if keep_claim:
		values.append(cache_state.claim_ttl_s(night=claim_.night))
	values.append(head_name(claim_.company, claim_.side))
	frappe.db.sql(
		f"UPDATE `tab{HEAD}` SET current_generation=%s, last_success=NOW(6), last_duration_ms=%s, last_rows=%s, "
		"peak_rss_mb=%s, failures=0, backoff_level=0, memo_until=NULL, last_failure=NULL, "
		"max_age_s_today=IF(max_age_date = CURDATE(), GREATEST(max_age_s_today, %s), %s), max_age_date=CURDATE(), "
		f"{claim_sql} WHERE name=%s",
		values,
	)
	if keep_claim:
		return None
	return frappe.db.sql(
		f"SELECT hop_seq FROM `tab{HEAD}` WHERE name=%s", (head_name(claim_.company, claim_.side),)
	)[0][0]


def _age_s(prev: int | None) -> int:
	if prev is None:
		return 0
	row = _first(f"SELECT TIMESTAMPDIFF(SECOND, as_of, NOW(6)) FROM `tab{GENERATION}` WHERE name=%s", (prev,))
	return max(int(row[0]), 0) if row and row[0] is not None else 0


def _abandon(claim_: Claim, klass: str, exc: BaseException | None = None) -> None:
	"""Best effort after a failed publish: the original error is the one the caller sees."""
	with contextlib.suppress(Exception):
		fail(claim_, klass, trace_of(exc) if exc else None)


def _check_deadline(deadline_at: float) -> None:
	if _clock() > deadline_at:
		raise WriteDeadline("write_deadline")


# ---------------------------------------------------------------------------------------- head, fence, claim


def _fence(claim_: Claim) -> dict:
	"""Lock the head and check the epoch and the token; the head's current values on success, else `Fenced`."""
	row = _first(
		"SELECT purge_epoch, claim_token, current_generation, company_creation "
		f"FROM `tab{HEAD}` WHERE name=%s FOR UPDATE",
		(head_name(claim_.company, claim_.side),),
	)
	if row is None:
		raise Fenced("head_gone")
	if row[0] != claim_.epoch:
		raise Fenced("epoch")
	if row[1] != claim_.token:
		raise Fenced("claim")
	return {"epoch": row[0], "current": row[2], "creation": row[3]}


def _renew_in_txn(claim_: Claim) -> None:
	frappe.db.sql(
		f"UPDATE `tab{HEAD}` SET claim_expires=NOW(6) + INTERVAL %s SECOND WHERE name=%s",
		(cache_state.claim_ttl_s(night=claim_.night), head_name(claim_.company, claim_.side)),
	)


def _lock_head_for_claim(company: str, side: str, creation: datetime, cache_build: str) -> dict:
	"""INSERT IGNORE the head, lock it, reset it for a reused name, and refuse a live claim or a memo."""
	name = head_name(company, side)
	frappe.db.sql(
		f"INSERT IGNORE INTO `tab{HEAD}` (name, creation, modified, modified_by, owner, docstatus, idx, company, side) "
		"VALUES (%s, NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0, %s, %s)",
		(name, company, side),
	)
	head = _read_head_for_claim(name)
	reset = head["creation"] is not None and head["creation"] != creation
	if reset:
		_reset_head(company, side, name)
	if head["creation"] != creation:
		frappe.db.sql(f"UPDATE `tab{HEAD}` SET company_creation=%s WHERE name=%s", (creation, name))
	head = _read_head_for_claim(name)
	if head["live"]:
		raise NotClaimed("busy")
	if head["memo"]:
		if _memo_build_matches(company, side, cache_build):
			raise NotClaimed("memo")
		frappe.db.sql(
			f"UPDATE `tab{HEAD}` SET memo_until=NULL, backoff_level=0, failures=0, last_failure=NULL WHERE name=%s",
			(name,),
		)
	return {**head, "reset": reset}


def _read_head_for_claim(name: str) -> dict:
	row = _first(
		"SELECT company_creation, purge_epoch, current_generation, "
		"(claim_token IS NOT NULL AND claim_expires >= NOW(6)), (memo_until IS NOT NULL AND memo_until > NOW(6)) "
		f"FROM `tab{HEAD}` WHERE name=%s FOR UPDATE",
		(name,),
	)
	if row is None:  # INSERT IGNORE skipped by a case-variant head of the same (company, side)
		raise NotClaimed("busy")
	return {
		"creation": row[0],
		"epoch": row[1],
		"current": row[2],
		"live": bool(row[3]),
		"memo": bool(row[4]),
	}


def _reset_head(company: str, side: str, name: str) -> None:
	"""A reused company name: the old generations are purged and the epoch fences any old writer."""
	frappe.db.sql(
		f"UPDATE `tab{HEAD}` SET purge_epoch=purge_epoch + 1, claim_token=NULL, claim_expires=NULL, "
		"current_generation=NULL, memo_until=NULL, backoff_level=0, failures=0, last_failure=NULL WHERE name=%s",
		(name,),
	)
	frappe.db.sql(
		f"UPDATE `tab{GENERATION}` SET state='purged', modified=NOW(6) WHERE company=%s AND side=%s AND state != 'purged'",
		(company, side),
	)


def _insert_generation(claim_: Claim) -> int:
	"""The `building` generation, with `deadline` = now + job timeout + 60 s."""
	number = frappe.db.get_next_sequence_val(GENERATION)
	frappe.db.sql(
		f"INSERT INTO `tab{GENERATION}` (name, creation, modified, modified_by, owner, docstatus, idx, company, side, "
		"company_creation, state, anchor, deadline, cache_build) "
		"VALUES (%s, NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0, %s, %s, %s, 'building', %s, "
		"NOW(6) + INTERVAL %s SECOND, %s)",
		(
			number,
			claim_.company,
			claim_.side,
			claim_.company_creation,
			claim_.anchor,
			cache_state.job_timeout_s(night=claim_.night) + 60,
			claim_.cache_build,
		),
	)
	return number


def _memo_build_matches(company: str, side: str, cache_build: str) -> bool:
	"""A memo holds for the build that failed; a newer build lifts it. No failed generation left: the memo holds."""
	row = _first(
		f"SELECT cache_build FROM `tab{GENERATION}` WHERE company=%s AND side=%s AND state='failed' "
		"ORDER BY name DESC LIMIT 1",
		(company, side),
	)
	return row is None or row[0] == cache_build


def _record_failure(company: str, side: str, klass: str, *, level: int, failures: int) -> None:
	if klass in UNCOUNTED_FAILURES:
		return
	name = head_name(company, side)
	if klass == "history_too_large":
		seconds = BACKOFF_LADDER_S[min(level, len(BACKOFF_LADDER_S) - 1)]
		frappe.db.sql(
			f"UPDATE `tab{HEAD}` SET backoff_level=%s, last_failure=%s, memo_until=NOW(6) + INTERVAL %s SECOND "
			"WHERE name=%s",
			(min(level + 1, len(BACKOFF_LADDER_S)), klass, seconds, name),
		)
		return
	count = failures + 1
	memo = "NOW(6) + INTERVAL %s SECOND" if count >= FAILURES_BEFORE_MEMO else "memo_until"
	values = [count, klass] + ([FAILURE_MEMO_S] if count >= FAILURES_BEFORE_MEMO else []) + [name]
	frappe.db.sql(
		f"UPDATE `tab{HEAD}` SET failures=%s, last_failure=%s, memo_until={memo} WHERE name=%s", values
	)


# ------------------------------------------------------------------------------------------ janitor internals


class _Budget:
	"""Rows still allowed and, when `seconds` is given, a monotonic stop time."""

	def __init__(self, rows: int, seconds: float | None = None):
		self.left = rows
		self.until = None if seconds is None else _clock() + seconds

	def timed_out(self) -> bool:
		return self.until is not None and _clock() > self.until


class _Scope:
	"""An optional (company, side) filter for the janitor queries, as SQL on a given column prefix."""

	def __init__(self, company: str | None = None, side: str | None = None):
		self.company, self.side = company, side

	def sql(self, prefix: str = "") -> tuple[str, list]:
		text, values = "", []
		for column, value in (("company", self.company), ("side", self.side)):
			if value is not None:
				text += f" AND {prefix}{column}=%s"
				values.append(value)
		return text, values


def _purge_unlisted(listed: set[str]) -> None:
	for (company,) in frappe.db.sql(f"SELECT DISTINCT company FROM `tab{HEAD}`"):
		if company not in listed:
			_bump_if_live(company)


def _purge_gone_companies(scope: _Scope) -> None:
	"""A company that is gone, or that a reused name now means another one: purge it like a delete."""
	where, values = scope.sql()
	for company, creation in frappe.db.sql(
		f"SELECT company, MAX(company_creation) FROM `tab{HEAD}` WHERE 1=1{where} GROUP BY company", values
	):
		now = company_creation(company)
		if now is None or (creation is not None and creation != now):
			_bump_if_live(company)


def _bump_if_live(company: str) -> None:
	"""Purge in the janitor's own transaction, only when something is left (else every tick would bump)."""
	live = _first(
		f"SELECT 1 FROM `tab{GENERATION}` WHERE company=%s AND state != 'purged' LIMIT 1", (company,)
	) or _first(
		f"SELECT 1 FROM `tab{HEAD}` WHERE company=%s AND (current_generation IS NOT NULL OR claim_token IS NOT NULL) LIMIT 1",
		(company,),
	)
	if not live:
		return
	try:
		bump_epoch(company)
		frappe.db.commit()
	except BaseException:
		with contextlib.suppress(Exception):
			frappe.db.rollback()
		raise
	notify_purged(read_log.company_digest_or(company, read_log.SITE_DIGEST))


def _resume_purged(scope: _Scope, budget: _Budget) -> None:
	"""Finish the `purged` generations, a page at a time, until the budget is spent or none is left."""
	where, values = scope.sql()
	while True:
		page = frappe.db.sql(
			f"SELECT name FROM `tab{GENERATION}` WHERE state='purged'{where} ORDER BY name LIMIT {PURGED_PAGE}",
			values,
		)
		for (generation,) in page:
			if not _delete_rows(generation, budget):
				return
			frappe.db.sql(f"DELETE FROM `tab{GENERATION}` WHERE name=%s AND state='purged'", (generation,))
			frappe.db.commit()
		if len(page) < PURGED_PAGE:
			break
	_delete_gone_heads(scope)


def _delete_gone_heads(scope: _Scope) -> None:
	"""Heads of a company that no longer exists (or has another `creation`), once no generation is left."""
	where, values = scope.sql()
	for name, company, side, creation in frappe.db.sql(
		f"SELECT name, company, side, company_creation FROM `tab{HEAD}` WHERE 1=1{where}", values
	):
		now = company_creation(company)
		if now is not None and (creation is None or creation == now):
			continue
		if _first(f"SELECT 1 FROM `tab{GENERATION}` WHERE company=%s AND side=%s LIMIT 1", (company, side)):
			continue
		frappe.db.sql(f"DELETE FROM `tab{HEAD}` WHERE name=%s", (name,))
		frappe.db.commit()


def _delete_retired_rows(scope: _Scope, budget: _Budget) -> None:
	where, values = scope.sql("g.")
	for (generation,) in frappe.db.sql(
		f"SELECT g.name FROM `tab{GENERATION}` g WHERE g.state='retired' AND g.modified < NOW(6) - INTERVAL %s SECOND "
		f"AND {_has_rows('g.name')}{where} ORDER BY g.name LIMIT 100",
		[GRACE_S, *values],
	):
		if not _delete_rows(generation, budget):
			return


def _delete_dead(scope: _Scope, budget: _Budget) -> None:
	"""`building` and `failed` generations past their deadline whose writer is gone (see the module docstring)."""
	where, values = scope.sql("g.")
	for generation, company, side, state in frappe.db.sql(
		f"SELECT g.name, g.company, g.side, g.state FROM `tab{GENERATION}` g WHERE g.state IN ('building', 'failed') "
		f"AND g.deadline < NOW(6) AND (g.state='building' OR {_has_rows('g.name')}){where} ORDER BY g.name LIMIT 200",
		values,
	):
		if state == "building" and _is_live_claimants_generation(generation, company, side):
			continue
		if not _delete_rows(generation, budget):
			return
		frappe.db.sql(
			f"UPDATE `tab{GENERATION}` SET state='failed', outcome='failed', failure_class='abandoned', "
			"modified=NOW(6) WHERE name=%s AND state='building'",
			(generation,),
		)
		frappe.db.commit()


def _is_live_claimants_generation(generation: int, company: str, side: str) -> bool:
	live = _first(
		f"SELECT (claim_token IS NOT NULL AND claim_expires >= NOW(6)) FROM `tab{HEAD}` WHERE company=%s AND side=%s",
		(company, side),
	)
	if not live or not live[0]:
		return False
	newest = _first(
		f"SELECT MAX(name) FROM `tab{GENERATION}` WHERE company=%s AND side=%s AND state='building'",
		(company, side),
	)
	return newest[0] == generation


def _delete_orphan_rows(budget: _Budget) -> None:
	"""Rows whose generation row is gone. Pages over the distinct generation ids of the Row table."""
	last = 0
	while budget.left > 0:
		ids = [
			row[0]
			for row in frappe.db.sql(
				f"SELECT DISTINCT generation FROM `tab{ROW}` WHERE generation > %s ORDER BY generation LIMIT 200",
				(last,),
			)
		]
		if not ids:
			return
		marks = ", ".join(["%s"] * len(ids))
		known = {
			row[0]
			for row in frappe.db.sql(f"SELECT name FROM `tab{GENERATION}` WHERE name IN ({marks})", ids)
		}
		for generation in ids:
			if generation not in known and not _delete_rows(generation, budget):
				return
		last = ids[-1]


def _delete_old_metadata(scope: _Scope, budget: _Budget, cache_build: str | None) -> None:
	where, values = scope.sql("g.")
	old = frappe.db.sql(
		f"SELECT g.name FROM `tab{GENERATION}` g WHERE g.state IN ('retired', 'failed') "
		f"AND g.modified < NOW(6) - INTERVAL %s DAY{where} ORDER BY g.name LIMIT 200",
		[METADATA_DAYS, *values],
	)
	rebuilt: tuple = ()
	if cache_build:
		rebuilt = frappe.db.sql(
			f"SELECT g.name FROM `tab{GENERATION}` g WHERE g.state IN ('retired', 'failed') AND g.cache_build != %s "
			"AND g.modified < NOW(6) - INTERVAL %s SECOND AND EXISTS ("
			f"SELECT 1 FROM `tab{HEAD}` h JOIN `tab{GENERATION}` c ON c.name=h.current_generation "
			f"WHERE h.company=g.company AND h.side=g.side AND c.cache_build=%s){where} ORDER BY g.name LIMIT 200",
			[cache_build, GRACE_S, cache_build, *values],
		)
	for (generation,) in (*old, *rebuilt):
		if not _delete_rows(generation, budget):
			return
		frappe.db.sql(
			f"DELETE FROM `tab{GENERATION}` WHERE name=%s AND state IN ('retired', 'failed')", (generation,)
		)
		frappe.db.commit()


def _delete_rows(generation: int, budget: _Budget) -> bool:
	"""Delete a generation's rows in chunks, committing each; False when the budget ran out first."""
	while budget.left > 0 and not budget.timed_out():
		size = min(JANITOR_CHUNK_ROWS, budget.left)
		frappe.db.sql(f"DELETE FROM `tab{ROW}` WHERE generation=%s LIMIT {int(size)}", (generation,))
		done = frappe.db.sql("SELECT ROW_COUNT()")[0][0]
		frappe.db.commit()
		budget.left -= done
		if done < size:
			return True
	return False


# ------------------------------------------------------------------------------------------------ small tools


def _enqueue_purge_after_commit(digest: str) -> None:
	"""Queue the purge job once the caller commits. An enqueue error is logged by class; the janitor resumes."""

	def enqueue():
		try:
			frappe.enqueue(
				PURGE_METHOD,
				queue="long",
				job_id=f"pcal-cache-purge:{digest}",
				deduplicate=True,
				digest=digest,
			)
		except TIMEOUTS:
			raise
		except Exception as exc:
			frappe.logger("payment_calendar").warning(
				"cache_purge enqueue failed class=%s", type(exc).__name__
			)

	frappe.db.after_commit.add(enqueue)


def _has_rows(generation_column: str) -> str:
	"""A correlated probe: one index lookup per candidate generation. `EXISTS` can make the optimizer build the
	set of every generation that has rows, which reads the whole Row table of all companies."""
	return f"(SELECT 1 FROM `tab{ROW}` r WHERE r.generation={generation_column} LIMIT 1) IS NOT NULL"


def _safe_class(klass: str) -> str:
	return _UNSAFE_CLASS.sub("_", str(klass))[:64] or "error"


def _check_side(side: str) -> None:
	if side not in SIDES:
		raise ValueError("Unknown cache side.")


def _first(query: str, values=()):
	rows = frappe.db.sql(query, values)
	return rows[0] if rows else None


@contextlib.contextmanager
def _fresh_connection():
	"""A second connection to the site database, as `frappe.connect` opens the main one."""
	from frappe.database import get_db

	conf = frappe.conf
	db = get_db(
		socket=conf.db_socket,
		host=conf.db_host,
		port=conf.db_port,
		user=conf.db_user or conf.db_name,
		password=conf.db_password,
		cur_db_name=conf.db_name,
	)
	db.connect()
	try:
		yield db
	finally:
		with contextlib.suppress(Exception):
			db.rollback()
		db.close()
