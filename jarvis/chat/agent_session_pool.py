"""WebSocket connection pool for OpenclawSession.

Each chat worker turn previously opened a fresh WS to the tenant's
openclaw gateway: DNS + TCP + TLS + WS upgrade + 3-phase signed
handshake before sending the agent RPC. The spike (workflow
``w0s0vqhqu``) measured this as 50-200ms per turn (recurring) and
confirmed:

- The gateway dispatches concurrent RPCs on one WS (multiplex)
- One warm WS per gateway can serve every subsequent turn

CAVEAT (2026-07 latency review): the RQ worker *parent* process is
long-lived, but stock ``bench worker`` uses rq's forking Worker - each
job runs in a forked work-horse child that exits after the job, taking
this module-level pool with it. The pool therefore only amortizes under
a non-forking executor: the Python-socketio realtime process (Path B,
``socketio_backend: "python"``) or ``bench worker-pool`` with
``FRAPPE_BACKGROUND_WORKERS_NOFORK=1``. The pool-hit/pool-miss log
lines in ``checkout``/``_do_connect`` measure which world you're in.

Concurrency model (Stage B, 2026-07-03): the pool keeps UP TO
``POOL_MAX_PER_GATEWAY`` sessions per gateway URL. ``checkout`` hands
out any free healthy entry, opens a new connection while the gateway is
below the cap, and blocks on a Condition once every slot is busy. The
bench-side ``OpenclawSession`` is NOT safe for concurrent
``stream_agent_turn``/``relay_turn_events`` on the same WS - ``_recv``
is shared state and would steal frames between turns - so PER-ENTRY
exclusivity is preserved: one turn per pooled session at a time. What
Stage B removes is the per-GATEWAY exclusivity that made two
same-tenant turns serialize end-to-end under Path B's greenlet
executor (measured: turn B's first token landed exactly at turn A's
finish). Under the default fork-per-job RQ executor the cap is
irrelevant (one turn per process); the same code serves both executors
unchanged - both dispatch flows are first-class.

Waiting is done on ``threading.Condition``, which the Path B realtime
process monkey-patches (gevent) into a cooperative wait; under plain
RQ threads it is a normal blocking wait. No gevent import here.

Lifetime: an ``atexit`` hook drains all pooled sessions when the
worker process exits. SIGTERM during a job lets the in-flight call
finish before the drain proceeds (atexit runs after the foreground
work completes).
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from jarvis.chat.agent_client import OpenclawSession
from jarvis.exceptions import OpenclawUnreachableError

logger = logging.getLogger(__name__)


# Max idle seconds before a pooled session is discarded on next
# checkout. Below the typical 5-min openclaw gateway idle disconnect
# so the WS is fresh when handed out. Aggressive eviction is cheap;
# the next checkout reconnects.
IDLE_MAX_S = 240

# Max concurrent pooled sessions per gateway. Small on purpose: each is
# a live WS + handshake state on the tenant container, and >3 truly
# simultaneous turns for ONE tenant is already unusual. The fourth
# concurrent checkout blocks until a slot frees (bounded in practice by
# the turn soft deadline). Recovery and prewarm use their own dedicated
# connections and never draw from this pool.
POOL_MAX_PER_GATEWAY = 3


@dataclass
class _PooledEntry:
	"""One pooled session. ``in_use`` is guarded by the owning group's
	Condition; the session object itself is only ever touched by the
	checkout that marked the entry in-use."""

	session: OpenclawSession
	last_used: float
	gateway_url: str
	in_use: bool = False


@dataclass
class _GatewayGroup:
	"""All pooled state for one gateway URL.

	``cond`` guards ``entries`` membership, every entry's ``in_use``
	flag, and ``connecting`` (slots reserved by in-flight connection
	attempts so a burst of checkouts cannot overshoot the cap). Slow
	work - connect, healthcheck, close - happens OUTSIDE the Condition,
	on entries the caller owns."""

	gateway_url: str
	cond: threading.Condition = field(default_factory=threading.Condition)
	entries: list[_PooledEntry] = field(default_factory=list)
	connecting: int = 0


# Module-level pool keyed by gateway_url. Group creation goes through
# ``_POOL_LOCK`` (cheap, no I/O); everything inside a group is guarded
# by the group's own Condition so gateways never contend.
_POOL: dict[str, _GatewayGroup] = {}
_POOL_LOCK = threading.Lock()


@contextlib.contextmanager
def checkout(gateway_url: str) -> Iterator[OpenclawSession]:
	"""Acquire a healthy OpenclawSession for ``gateway_url``.

	Hands out any free pooled session (healthchecked, reconnected in
	place if stale), opens a new connection while the gateway is below
	``POOL_MAX_PER_GATEWAY``, and blocks until a slot frees once at the
	cap. The yielded session is exclusive to this caller until the
	context exits. On ``OpenclawUnreachableError`` inside the block the
	entry is evicted (next checkout reconnects); other exceptions
	return the entry to the pool intact.
	"""
	group = _get_or_create_group(gateway_url)
	entry = _acquire_entry(group)
	evict_on_exit = False
	try:
		yield entry.session
		entry.last_used = time.monotonic()
	except OpenclawUnreachableError:
		# Connection failed mid-use. Evict so the next checkout opens
		# a fresh WS rather than handing out the broken one.
		evict_on_exit = True
		raise
	finally:
		with group.cond:
			if evict_on_exit:
				_remove_entry(group, entry)
			else:
				entry.in_use = False
			group.cond.notify()
		if evict_on_exit:
			_close_quietly(entry.session)


def _acquire_entry(group: _GatewayGroup) -> _PooledEntry:
	"""Claim a free entry, create one below the cap, or wait for a slot.

	Returns an entry already marked ``in_use`` whose session passed the
	healthcheck (reconnected in place when stale). Raises
	``OpenclawUnreachableError`` if a needed connect fails - after
	releasing the reserved slot so waiters retry."""
	while True:
		create_new = False
		with group.cond:
			entry = next((e for e in group.entries if not e.in_use), None)
			if entry is not None:
				entry.in_use = True
			elif len(group.entries) + group.connecting < POOL_MAX_PER_GATEWAY:
				group.connecting += 1
				create_new = True
			else:
				group.cond.wait()
				continue

		if create_new:
			try:
				sess = _do_connect(group.gateway_url)
			except BaseException:
				with group.cond:
					group.connecting -= 1
					group.cond.notify()
				raise
			new_entry = _PooledEntry(
				session=sess,
				last_used=time.monotonic(),
				gateway_url=group.gateway_url,
				in_use=True,
			)
			with group.cond:
				group.connecting -= 1
				group.entries.append(new_entry)
			return new_entry

		# Healthcheck the claimed entry OUTSIDE the Condition - we own it
		# (in_use=True), so the slow reconnect never blocks other slots.
		if _is_alive(entry.session, entry.last_used):
			# Symmetric to the pool-miss log in _do_connect so the hit rate
			# is measurable from logs (latency plan, Phase 0). Under stock
			# fork-per-job RQ this line should ~never appear - that absence
			# is itself the signal that the persistent executor is needed.
			logger.info(
				"agent_session_pool: pool-hit gateway=%s idle_s=%.1f",
				group.gateway_url,
				time.monotonic() - entry.last_used,
			)
			return entry
		logger.info(
			"agent_session_pool: stale entry, reconnecting gateway=%s idle_s=%.1f connected=%s",
			group.gateway_url,
			time.monotonic() - entry.last_used,
			_safe_connected(entry.session),
		)
		try:
			# _close_quietly swallows Exception but not BaseException (a
			# greenlet kill delivered inside close() would otherwise leak
			# this slot forever: in_use stays True with no notify), so the
			# close sits INSIDE the same guard as the reconnect.
			_close_quietly(entry.session)
			entry.session = _do_connect(group.gateway_url)
		except BaseException:
			# Reconnect failed: drop the dead entry entirely and free the
			# slot so a waiter (or retry) can attempt its own connect.
			with group.cond:
				_remove_entry(group, entry)
				group.cond.notify()
			raise
		return entry


def _get_or_create_group(gateway_url: str) -> _GatewayGroup:
	"""Look up or create the group for ``gateway_url``. Group creation
	is cheap (no I/O), so it happens under ``_POOL_LOCK`` directly."""
	with _POOL_LOCK:
		group = _POOL.get(gateway_url)
		if group is None:
			group = _GatewayGroup(gateway_url=gateway_url)
			_POOL[gateway_url] = group
		return group


def _remove_entry(group: _GatewayGroup, entry: _PooledEntry) -> None:
	"""Drop ``entry`` from the group by identity. Caller holds
	``group.cond``. Closing the session is the caller's job (outside
	the Condition)."""
	try:
		group.entries.remove(entry)
	except ValueError:
		pass


def _do_connect(gateway_url: str) -> OpenclawSession:
	"""Open a fresh WS + handshake. Wraps ``OpenclawSession.connect``
	for timing visibility - the actual connect logic stays in the
	client. Phase-level breakdown is logged inside ``_attempt_connect``
	itself; this just captures the total."""
	t0 = time.monotonic()
	sess = OpenclawSession.connect(gateway_url)
	elapsed_ms = int((time.monotonic() - t0) * 1000)
	logger.info(
		"agent_session_pool: pool-miss connect gateway=%s total_ms=%d",
		gateway_url,
		elapsed_ms,
	)
	return sess


def _is_alive(session: OpenclawSession, last_used: float) -> bool:
	"""True if the session looks healthy enough to reuse: WS is open
	and the entry hasn't sat idle past the eviction threshold."""
	if time.monotonic() - last_used > IDLE_MAX_S:
		return False
	return _safe_connected(session)


def _safe_connected(session: OpenclawSession) -> bool:
	"""Defensively read ``session._ws.connected``; some WS libraries
	don't expose the field, in which case treat as not-connected."""
	try:
		return bool(session._ws.connected)
	except AttributeError:
		return False


def _close_quietly(session: OpenclawSession) -> None:
	"""Close a session, swallowing any errors. Used during eviction
	where the session is already dead / dying."""
	try:
		session.close()
	except Exception:
		pass


def drain_all() -> None:
	"""Close every pooled session. Called from ``atexit`` on worker
	shutdown so the gateway sees clean disconnects rather than dangling
	half-open TCP connections. Also exposed for tests. In-use entries
	are closed too - this only runs at process exit (or from tests),
	when no turn should still be live."""
	with _POOL_LOCK:
		groups = list(_POOL.values())
		_POOL.clear()
	for group in groups:
		with group.cond:
			entries = list(group.entries)
			group.entries.clear()
			group.cond.notify_all()
		for entry in entries:
			_close_quietly(entry.session)


# Register the drain hook at module import time. ``atexit`` runs in
# reverse-registration order, which is correct: any code that
# registered before us (e.g. Frappe DB connections) drains first,
# then we close the WS.
atexit.register(drain_all)
