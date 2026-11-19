"""Server reload of an expired report snapshot (plan B.9.2 - B.9.4).

`reload_snapshot` is called by `_report_snapshots.ensure_live` only, before the query's
own deadline starts. One attempt runs in a fixed order:

    switches -> agent path -> slow guard -> per-user lock -> re-read -> killed-attempt guard
    -> rate cap -> mark `reload_started_at` -> run `run_report` inline-only -> write

`reload_started_at` is cleared on every exit that raises an `Exception` or returns without
deleting the keys, with a compare-and-set on this attempt's own stamp. It is never cleared
in a `finally` (a killed worker must leave it set) and never after a lost write fence.
Redis rules are those of `_report_snapshot_store` (plain redis-py methods on `make_key`'d keys).
"""

import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import NamedTuple

import frappe
import redis

from jarvis import _failure_kind, telemetry
from jarvis.exceptions import (
	DelegateReportRefused,
	FefoScopeRefused,
	InvalidArgumentError,
	PermissionDeniedError,
	RetryLaterError,
	SnapshotReloadRefused,
)
from jarvis.tools import _report_snapshot_store as store
from jarvis.tools._report_snapshot_digest import perm_digest
from jarvis.tools._report_snapshot_events import (
	NO_ACCESS,
	NOT_AVAILABLE,
	UNAVAILABLE,
	log,
	read_event,
	unavailable,
)
from jarvis.tools._source_process import BUSY as WORKER_BUSY

LOCK_PREFIX = "jarvis:report_snapshot_reload:"
RATE_PREFIX = "jarvis:report_snapshot_reload_rate:"
RELOAD_LOCK_TTL_S = 300
STALE_AFTER_S = 30
SLOW_RUN_MS = 8000
RATE_LIMIT = 6
RATE_WINDOW_S = 3600

BUSY_TEXT = "This report is reloading. Send the same call again in a few seconds."
TRANSIENT_TEXT = "The report could not be reloaded just now. Send the same call again."

_agent_reload = ContextVar("jarvis_report_snapshot_agent_reload", default=False)


class ReloadInfo(NamedTuple):
	"""What a successful reload tells the caller (B.9.5)."""

	generation: int
	reloaded_at: str
	data_changed: bool
	perm_changed: bool
	report_name: str
	evicted: bool = False  # the rows were lost to Redis inside their hour (B.7 eviction signal)


def clock() -> float:
	"""Monotonic seconds. The one clock of the reload's time budget (the handback in
	`query_report` reads it too); tests replace it (`Deadline` keeps its own, so
	`time.monotonic` is never patched)."""
	return time.monotonic()


@contextmanager
def agent_reload_scope():
	"""Mark the agent session path, the only place a reload may run (B.9.2). Entered at
	the agent call site in `api._dispatch_from_session`; shared dispatch and the desk
	`call_tool` never enter it."""
	token = _agent_reload.set(True)
	try:
		yield
	finally:
		_agent_reload.reset(token)


def in_agent_reload_scope() -> bool:
	return _agent_reload.get()


class _Stop(Exception):
	"""An attempt that ends with a public error. `outcome` is the `reload_end` outcome,
	`read` the outcome of the `read` event the failed call reports, `reason` the exception
	class (or a short label) that both events carry."""

	def __init__(self, outcome, error, read=None, reason=None):
		super().__init__(outcome)
		self.outcome, self.error, self.read, self.reason = outcome, error, read, reason


def reload_snapshot(snapshot_id, meta, *, perm_changed, miss):
	"""Reload `snapshot_id` or raise why it was not. Returns a `ReloadInfo`, or None when
	another call finished the reload meanwhile (the caller then reads as usual). `miss`
	(`changed`, `evicted` or `not_available`) is why the rows were not live."""
	attempt = _Attempt(snapshot_id, meta, perm_changed, miss)
	try:
		return attempt.run()
	except _Stop as stop:
		fields = {"reason": stop.reason} if stop.reason else {}
		attempt.end(stop.outcome, **fields)
		if stop.read:
			evicted = {"evicted": True} if attempt.miss == "evicted" else {}
			read_event(stop.read, snapshot_id, attempt.meta, **fields, **evicted)
		raise stop.error from None


class _Attempt:
	def __init__(self, snapshot_id, meta, perm_changed, miss):
		self.id = snapshot_id
		self.meta = meta
		self.perm_changed = perm_changed
		self.miss = miss
		self.owner = frappe.session.user
		self.generation = meta["generation"]
		self.stamp = None
		self.started = clock()
		self.data_changed = False

	# --- events -------------------------------------------------------------------

	def _emit(self, event, outcome, **extra):
		telemetry.record_snapshot_event(
			event,
			outcome,
			snapshot_id=self.id,
			generation=self.generation,
			reload_ms=int((clock() - self.started) * 1000),
			perm_changed=self.perm_changed,
			origin_created_at=self.meta.get("created_at"),
			**extra,
		)

	def end(self, outcome, **extra):
		self._emit("reload_end", outcome, data_changed=self.data_changed, **extra)

	# --- stops --------------------------------------------------------------------

	def _not_available(self, outcome, reason=None):
		# Eviction is a field of the `read` event, not an outcome of its own.
		read = "not_available" if self.miss == "evicted" else self.miss
		return _Stop(outcome, InvalidArgumentError(NOT_AVAILABLE), read, reason)

	def _transient(self, outcome, reason):
		return _Stop(outcome, RetryLaterError(TRANSIENT_TEXT), "error", reason)

	def _redis_down(self, exc):
		return _Stop("failed:redis_error", unavailable(exc, "reload"), "redis_error", type(exc).__name__)

	# --- the attempt --------------------------------------------------------------

	def run(self):
		self._before_lock()
		lock = frappe.cache().lock(self._key(LOCK_PREFIX), timeout=RELOAD_LOCK_TTL_S)
		acquired = False
		try:
			acquired = self._acquire(lock)
			return self._locked()
		finally:
			if acquired:
				self._release(lock)

	def _key(self, prefix):
		return frappe.cache().make_key(prefix + self.owner)

	def _before_lock(self):
		if not store.reload_enabled():
			raise self._not_available("disabled")
		if not in_agent_reload_scope():
			raise self._not_available("not_agent_path")
		if (self.meta.get("run_ms") or 0) > SLOW_RUN_MS:
			raise self._not_available("refused:slow")

	def _acquire(self, lock):
		try:
			if lock.acquire(blocking=False):
				return True
			remaining_ms = frappe.cache().pttl(self._key(LOCK_PREFIX))
		except redis.exceptions.RedisError as exc:
			raise self._redis_down(exc) from None
		if remaining_ms == -1:
			log("reload_lock_without_ttl")
			remaining_ms = 1  # treated as stale
		# Held for more than STALE_AFTER_S: a dead or stuck holder, not a reload in flight.
		if 0 < remaining_ms < (RELOAD_LOCK_TTL_S - STALE_AFTER_S) * 1000:
			raise self._not_available("refused:lock_stale")
		raise _Stop("busy", RetryLaterError(BUSY_TEXT), "busy")

	def _release(self, lock):
		try:
			lock.release()
		except Exception as exc:  # LockError: not owned (it expired) or already released
			log("reload_lock_release", exc)

	def _locked(self):
		fresh = self._read_state()
		if fresh is None:
			return None  # another call finished the reload: read as usual
		self.meta, self.generation = fresh, fresh["generation"]
		if fresh.get("reload_started_at"):
			store.delete_keys(self.id)  # the previous attempt died
			raise self._not_available("failed:killed")
		self._count_attempt()
		self._mark()
		return self._run_and_store()

	def _read_state(self):
		try:
			meta, content_pttl = store.read_state(self.id)
		except redis.exceptions.RedisError as exc:
			raise self._redis_down(exc) from None
		if meta is None or meta["owner"] != self.owner:
			raise self._not_available("refused:gone")
		if content_pttl != -2 and perm_digest(self.owner, meta["report_name"]) == meta["perm_digest"]:
			return None
		return meta

	def _count_attempt(self):
		"""Fixed one-hour window: `SET NX EX` and `INCR` in one MULTI, so `INCR` never
		extends the window."""
		key = self._key(RATE_PREFIX)
		try:
			with frappe.cache().pipeline() as pipe:
				pipe.set(key, 0, ex=RATE_WINDOW_S, nx=True)
				pipe.incr(key)
				count = pipe.execute()[1]
		except redis.exceptions.RedisError as exc:
			raise self._redis_down(exc) from None
		if count > RATE_LIMIT:
			raise self._not_available("capped")

	def _mark(self):
		self.stamp = f"{frappe.utils.now()} {secrets.token_hex(4)}"
		try:
			state = store.update_meta(
				self.id,
				lambda current: {**current, "reload_started_at": self.stamp},
				check=lambda current: not current.get("reload_started_at"),
			)
		except redis.exceptions.RedisError as exc:
			# A lost reply does not mean the stamp was not applied: a stamp left behind
			# reads as a killed attempt and costs the user the snapshot. Take back our own.
			self._clear_stamp()
			raise self._redis_down(exc) from None
		if state == "gone":
			raise self._not_available("refused:gone")
		if state != "ok":  # a newer writer owns the record
			self.stamp = None
			raise self._transient("failed:watch", "WatchError")

	def _clear_stamp(self, run_ms=None):
		"""Compare-and-set on this attempt's own stamp, so a stamp that is not ours is never
		touched; records the attempt's wall time when it ran. A lost WATCH is retried once;
		any other result than `ok` is logged (the stamp may stay set: the next call then
		treats the attempt as killed)."""
		change = (
			{"reload_started_at": None} if run_ms is None else {"reload_started_at": None, "run_ms": run_ms}
		)
		state = "error"
		for _ in range(2):
			try:
				state = store.update_meta(
					self.id,
					lambda current: {**current, **change},
					check=lambda current: current.get("reload_started_at") == self.stamp,
				)
			except Exception as exc:
				log("reload_clear_error", exc)
				return
			if state != "watch":
				break
		if state != "ok":
			log(f"reload_clear_{state}")

	# --- run and write ------------------------------------------------------------

	def _run_and_store(self):
		self._emit("reload_start", "started", data_changed=False)
		began = clock()
		try:
			data = self._run_inline()
		except Exception as exc:
			raise self._after_run_error(exc, self._ms_since(began)) from None
		run_ms = self._ms_since(began)
		meta = self.meta
		stored = store.create(
			data,
			report_name=meta["report_name"],
			filters=meta["filters"],
			run_ms=run_ms,
			snapshot_id=self.id,
			generation=self.generation + 1,
			created=(meta.get("created_at"), meta.get("created_ts")),
		)
		if stored[0] is None:
			raise self._after_write_refusal(stored[1], stored.error, run_ms)
		return self._reloaded(meta)

	def _ms_since(self, began):
		return int((clock() - began) * 1000)

	def _run_inline(self):
		from jarvis.tools.run_report import _run_report

		muted = frappe.flags.mute_messages
		frappe.flags.mute_messages = True
		try:
			return _run_report(self.meta["report_name"], self.meta["filters"], inline_only=True)
		finally:
			frappe.flags.mute_messages = muted

	def _after_run_error(self, exc, run_ms):
		"""Context refusals keep the snapshot, transient failures keep it for a retry,
		everything else deletes it."""
		if isinstance(exc, FefoScopeRefused | DelegateReportRefused):
			self._clear_stamp(run_ms)
			return self._not_available(
				"refused:fefo" if isinstance(exc, FefoScopeRefused) else "refused:delegate"
			)
		if isinstance(exc, SnapshotReloadRefused):
			store.delete_keys(self.id)
			return self._not_available(f"refused:{exc.reason}")
		if isinstance(exc, redis.exceptions.RedisError):
			self._clear_stamp(run_ms)
			return self._redis_down(exc)
		if _is_transient(exc):
			self._clear_stamp(run_ms)
			return self._transient(f"failed:{type(exc).__name__}", type(exc).__name__)
		store.delete_keys(self.id)
		if isinstance(exc, PermissionDeniedError | frappe.PermissionError):
			return _Stop("refused:permission", PermissionDeniedError(NO_ACCESS), "refused", "permission")
		log("reload_failed", exc, level="error")  # the report's own fault: class only, no values
		return self._not_available(f"failed:{type(exc).__name__}", type(exc).__name__)

	def _after_write_refusal(self, reason, error, run_ms):
		if reason == "stale":
			# A newer writer won, or a reader wrote the meta between WATCH and EXEC. Delete
			# nothing. Take back our own stamp: left set, the next call reads it as a killed
			# attempt and deletes the keys. The compare-and-set leaves a newer writer's meta
			# alone, as it carries no stamp of ours.
			self._clear_stamp(run_ms)
			return self._transient("failed:watch", "WatchError")
		if reason == "gone":  # trimmed meanwhile; the content is already deleted
			return self._not_available("refused:gone")
		if reason == "private":
			self._clear_stamp(run_ms)
			return self._not_available("refused:private")
		if reason == "error":
			self._clear_stamp(run_ms)
			return self._transient("failed:error", error)
		store.delete_keys(self.id)
		return self._not_available(f"refused:{reason}")

	def _reloaded(self, before):
		try:
			fresh = store.load_meta(self.id)
		except redis.exceptions.RedisError as exc:
			raise self._redis_down(exc) from None
		if fresh is None or fresh["owner"] != self.owner:
			raise self._not_available("refused:gone")
		self.generation = fresh["generation"]
		self.data_changed = fresh["sha256"] != before["sha256"]
		info = ReloadInfo(
			fresh["generation"],
			fresh.get("reloaded_at") or "",
			self.data_changed,
			self.perm_changed,
			before["report_name"],
			self.miss == "evicted",
		)
		self.end("ok")
		return info


def _is_transient(exc):
	return (
		_failure_kind.too_slow(exc)
		or _failure_kind.kind_of(exc) == _failure_kind.RETRY_LATER
		or (isinstance(exc, InvalidArgumentError) and str(exc) == WORKER_BUSY)
	)
