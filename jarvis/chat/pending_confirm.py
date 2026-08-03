"""Pending-confirmation token store for the write-safety confirmation gate
(issue #186).

A mutating tool call that needs a human's go-ahead is parked here under a
single-use token instead of running immediately. The gate itself (later
task) mints a token when it intercepts such a call, shows the user a
preview built from ``peek``, and only actually runs the call once
``consume`` returns the stored record for the confirming click.

This module only owns the store - it does not decide which tools need
confirmation and does not run anything.

Storage: ``frappe.cache()`` (Redis), one key per token, single round trip
TTL so a token that nobody clicks self-expires instead of leaking forever.

Portability floor: this module runs on the CUSTOMER bench, whose Frappe/Redis
versions vary. It must stay compatible down to **Frappe 15** and **Redis 6.0**.
Do NOT reach for a Frappe >= 16/17-only RedisWrapper method (``expire_key``, the
``use_local_cache`` get_value kwarg) or a Redis >= 6.2-only command (``GETDEL``,
``GETEX``, ``COPY``, ``SINTERCARD``, ...) - use an inherited ``redis.Redis``
builtin or a MULTI/EXEC transaction instead.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import secrets
import time

import frappe
import redis.exceptions

_TTL_S = 900  # 15 min; a confirmation token the user must click within
_PREFIX = "jarvis:pending_confirm:"
# Per-owner index: a Redis set of the owner's currently-live token ids, so the
# resync endpoint can enumerate a user's own parked confirmations after a reload
# or reconnect. TTL discipline: dead members
# (token record expired/consumed) are pruned on read; the set key itself is
# given a refreshed TTL on every mint so an emptied set self-expires.
_OWNER_PREFIX = "jarvis:pending_confirm:owner:"


def _key(token: str) -> str:
	return _PREFIX + token


def _owner_key(owner: str) -> str:
	return _OWNER_PREFIX + owner


# --------------------------------------------------------------------------- #
# cards_open gauge (BUILD-DIRECTIVE §1 — the WP-0 C-series gap, wired here)
# --------------------------------------------------------------------------- #
#
# A live count of open confirmation cards: mint +1, consume -1, and EXPIRY
# auto-pruned. Backed by a single site-scoped Redis ZSET scored by each token's
# expiry epoch — reading purges expired members (ZREMRANGEBYSCORE) then ZCARD, so
# a card nobody clicks decrements the gauge exactly when its TTL lapses without any
# code running on expiry. This is OBSERVABILITY, not authority (the token records
# remain the source of truth), so every op is best-effort. Raw ``execute_command``
# with an explicit site-scoped key (mirrors the pump wake-bus) so ZADD/ZREM/ZCARD
# all agree regardless of RedisWrapper's key prefixing.

_GAUGE_SUFFIX = "jarvis:pending_confirm:open"


def _gauge_key() -> str:
	local = getattr(frappe, "local", None)
	db = (local.conf.get("db_name") if local else None) or getattr(local, "site", "") or ""
	return f"{db}|{_GAUGE_SUFFIX}"


def cards_open_gauge() -> int:
	"""Live count of open confirmation cards (expired members pruned on read)."""
	try:
		cache = frappe.cache()
		key = _gauge_key()
		cache.execute_command("ZREMRANGEBYSCORE", key, 0, int(time.time()))
		return int(cache.execute_command("ZCARD", key) or 0)
	except Exception:
		return 0


def _gauge_add(token: str, expires_at: int) -> None:
	try:
		frappe.cache().execute_command("ZADD", _gauge_key(), int(expires_at), token)
	except Exception:
		pass


def _gauge_remove(token: str) -> None:
	try:
		frappe.cache().execute_command("ZREM", _gauge_key(), token)
	except Exception:
		pass


def _emit_cards_open(source: str) -> None:
	"""Mirror the gauge to the latency channel (the pilot greps ``cards_open`` for
	the C-series). Best-effort."""
	try:
		from jarvis.chat.latency import get_logger

		get_logger().info("cards_open gauge=%d source=%s", cards_open_gauge(), source)
	except Exception:
		pass


def args_hash(tool: str, args: dict) -> str:
	"""Stable hash of the tool + its canonical args, so a token is bound to
	the EXACT call. Canonical = json.dumps(args, sort_keys=True, default=str).
	"""
	canonical = json.dumps(args, sort_keys=True, default=str)
	return hashlib.sha256(f"{tool}:{canonical}".encode()).hexdigest()


def mint(
	*,
	conversation: str,
	owner: str,
	tool: str,
	args: dict,
	run_id: str,
	exec_user: str | None = None,
	preview: dict | None = None,
	expires_at: int | None = None,
) -> str | None:
	"""Store a pending call and return a fresh single-use token
	(secrets.token_urlsafe(24)). The stored record carries conversation,
	owner, tool, args (the full dict - this is the authoritative payload
	that will execute), args_hash, run_id, exec_user, preview. TTL _TTL_S.
	Returns the token, or ``None`` when the park could not be stored (a transient
	cache failure): the record+index must BOTH land or the caller must treat it as
	a retryable failure and publish NO card - a token whose record does not exist is
	an un-confirmable card that wedges the turn.

	``preview`` is the park-time confirmation preview (dry-run "would" doc or
	described-intent dict). It is stored so the resync endpoint can return it
	verbatim instead of RE-running the dry-run - re-running fires unsandboxed
	on_submit/on_cancel side effects on every reload/reconnect (F2). Tokens
	minted before this field existed simply carry no ``preview``.

	``owner`` is the CONVERSATION OWNER - the human who sees the card, clicks
	Confirm, and whose browser is subscribed. Delivery + binding + confirm all
	key off this identity. ``exec_user`` is the scoped model-execution identity
	the confirmed write must run AS (so a confirm can never exceed the model
	path's permission scope). It defaults to ``owner`` when omitted.
	"""
	token = secrets.token_urlsafe(24)
	record = {
		"conversation": conversation,
		"owner": owner,
		"exec_user": exec_user or owner,
		"tool": tool,
		"args": args,
		"args_hash": args_hash(tool, args),
		"run_id": run_id,
		"preview": preview,
		# Wall-clock expiry (epoch seconds) so the SPA can show a real countdown
		# and distinguish a genuine TTL lapse from other confirm failures (F15).
		# Defaults to now + TTL when the caller does not pass one, so every record
		# carries it (the resync payload reads it straight off the record).
		"expires_at": expires_at if expires_at is not None else int(time.time()) + _TTL_S,
	}
	cache = frappe.cache()
	# Persist the record, index it under its owner, and VERIFY it landed - all three
	# must hold or the park is a failure. Two ways a park silently breaks:
	#   1. The owner-index write fails: an unindexed record is INVISIBLE to the resync
	#      endpoint -> a confirmation card that never re-surfaces (the bulk-create card
	#      that parked but never rendered).
	#   2. set_value SUPPRESSES a transient redis ConnectionError, so the record itself
	#      can fail to persist with no exception raised at all.
	# In EITHER case, returning a token would make the gate publish a card whose token
	# has no record -> an un-confirmable "expired" card that wedges the turn. So on ANY
	# failure roll the writes back (no orphan), log LOUDLY (it was once a silent
	# try/except: pass), and return None so the gate surfaces a RETRYABLE tool error and
	# the model can simply call again.
	try:
		cache.set_value(_key(token), record, expires_in_sec=_TTL_S)
		cache.sadd(_owner_key(owner), token)
		# set_value swallows a redis blip, so confirm the record is really readable
		# before we let a card be published against this token.
		if peek(token) is None:
			raise RuntimeError("pending-confirm record did not persist")
	except Exception:
		frappe.log_error(
			title="pending_confirm: park failed; token not stored",
			message=frappe.get_traceback(),
		)
		for _rollback in (
			lambda: cache.delete_value(_key(token)),
			lambda: cache.srem(_owner_key(owner), token),
		):
			try:
				_rollback()
			except Exception:
				pass
		return None
	# Refresh the owner-index set's TTL so an emptied set self-expires (see the module
	# docstring). PURE HYGIENE - dead members are pruned on read regardless - so it must
	# NEVER fail the park: a record that already persisted + verified is live and
	# confirmable whether or not this TTL lands, which is why it sits OUTSIDE the try
	# above. Uses the raw redis ``expire`` on the make_key'd set key (present on every
	# Frappe version) rather than ``RedisWrapper.expire_key`` - Frappe >= 17 only, whose
	# AttributeError on a v15 bench was caught by the park's try/except and rolled back a
	# good record, taking down every gated confirmation card.
	try:
		cache.expire(cache.make_key(_owner_key(owner)), _TTL_S)
	except Exception as exc:
		# Best-effort hygiene (see above): must never fail the park. DEBUG (not a
		# loud log - it is non-fatal and could be frequent) so a DURABLE failure
		# (e.g. a Redis ACL that permits SADD but denies EXPIRE) is discoverable
		# when the log level is raised, rather than silent forever.
		frappe.logger("jarvis.pending_confirm").debug(
			"owner-index TTL refresh failed (non-fatal): %s", type(exc).__name__
		)
	# cards_open gauge +1 (self-healing on expiry via the ZSET score). Bumped only
	# after a successful persist+index+verify so the gauge never over-counts a
	# rolled-back park.
	_gauge_add(token, record["expires_at"])
	_emit_cards_open("mint")
	return token


def _read_record(token: str, *, swallow: bool = True) -> dict | None:
	"""Read the parked record straight from Redis, bypassing the worker-local
	cache. Bypassing local is load-bearing: mint()'s post-persist verify must catch
	the case where set_value swallowed a ConnectionError and the record never landed
	in Redis (a local copy would falsely read as success), and consume() must never
	act on a stale local snapshot. Version-portable: Frappe < 16's get_value has no
	``use_local_cache`` kwarg (passing it TypeErrors on a v15 bench), so instead pop
	any local entry (forces the read to miss local and hit Redis) and pass
	``expires=True`` (the token key has a TTL) so the read does not repopulate local -
	together equivalent to the v17-only ``use_local_cache=False``.

	``swallow`` (default True): a RedisError on the read becomes None - the safe,
	retryable outcome for peek/consume/mint-verify (not-consumable / not-found /
	persist-miss->rollback), because get_value itself only suppresses ConnectionError,
	not its TimeoutError sibling or ResponseError, so an uncaught one would be a 500.
	``list_for_owner`` passes ``swallow=False`` so it can tell a TRANSIENT read blip
	(skip, leave the member) apart from a genuinely-absent token (prune) - swallowing
	there would orphan a still-live token from the index (the invisible-card class).
	"""
	cache = frappe.cache()
	frappe.local.cache.pop(cache.make_key(_key(token)), None)
	try:
		return cache.get_value(_key(token), expires=True)
	except redis.exceptions.RedisError:
		if not swallow:
			raise
		return None


def peek(token: str) -> dict | None:
	"""Return the stored record (dict) without consuming it, or None if the
	token is unknown/expired. Used to build the preview/UI event. Does NOT
	validate ownership - callers that act on it must.
	"""
	if not token:
		return None
	return _read_record(token)


def _get_and_delete(full_key) -> bytes | None:
	"""Atomically read-and-remove a raw cache key, returning its pickled bytes (or
	None if absent). Uses a MULTI/EXEC transaction (GET then DEL) rather than the
	one-shot GETDEL command: GETDEL is redis-server >= 6.2 only, and a customer
	bench may run older (a v6.0 server rejects it), whereas GET/DEL under MULTI/EXEC
	run as one indivisible unit on EVERY redis version. That keeps the single-winner
	consume guarantee - the server serializes the two transactions, so of two racing
	confirms exactly one sees the value before DEL removes it - without the version
	floor. ``full_key`` is the already-make_key'd key (raw redis, no re-namespacing),
	matching what the previous getdel operated on.
	"""
	pipe = frappe.cache().pipeline(transaction=True)
	pipe.get(full_key)
	pipe.delete(full_key)
	raw, _deleted = pipe.execute()
	return raw


def consume(token: str, *, owner: str, conversation: str) -> dict | None:
	"""Validate AND atomically single-use-consume. Returns the stored record
	ONLY when: token exists, record.owner == owner, and (when the record has a
	non-empty stored conversation) record.conversation == conversation. A record
	whose stored conversation is "" carries no conversation binding and passes
	that check for any caller conversation (owner + single-use still hold). On
	success the token is deleted BEFORE returning (single use - a second consume
	returns None). On any mismatch returns None and does NOT delete (so a
	wrong-owner probe cannot burn a legitimate token).

	Atomicity: ownership is checked first with a plain (non-destructive)
	read, so a mismatched call never touches the stored key. Only once
	ownership matches do we delete - and that delete is an atomic
	get-and-delete (``_get_and_delete``: GET then DEL inside one MULTI/EXEC
	transaction, portable to redis < 6.2 which lacks GETDEL). If two
	confirmed consumes race each other here, the server serializes the two
	transactions: exactly one gets the pickled record back, the other gets
	None. That is the single-use guarantee - it does not depend on
	Python-level locking, which would not help anyway across separate
	worker processes.
	"""
	if not token:
		return None
	record = _read_record(token)
	if not record:
		return None
	# Owner is the real security boundary and is always enforced.
	if record.get("owner") != owner:
		return None
	# Conversation is a SECONDARY replay guard, not the boundary. A token minted
	# without a resolvable conversation ("" - a session_key->conversation
	# lookup miss) carries no conversation
	# binding, so an owner-matched consume must still succeed even when the caller
	# passes its current conversation id. Without this skip such a card is
	# delivered to the owner but EVERY Confirm click fails here and shows a
	# misleading "expired" toast (the card is un-confirmable for its full TTL).
	# When a conversation WAS bound, it is still enforced.
	stored_conv = record.get("conversation")
	if stored_conv and stored_conv != conversation:
		return None

	full_key = frappe.cache().make_key(_key(token))
	# Atomic get-and-delete. GETDEL would be the one-command way but it is
	# redis-server >= 6.2 ONLY, and a customer bench may run older (a v6.0 server
	# rejects it outright) - so _get_and_delete uses a MULTI/EXEC transaction
	# (GET then DEL as one indivisible unit) instead. That keeps the single-winner
	# guarantee (of two concurrent confirms exactly one sees the value before DEL
	# removes it) while working on every redis version; a plain get-then-delete
	# would reopen the very race the atomicity exists to close.
	try:
		raw = _get_and_delete(full_key)
	except redis.exceptions.RedisError as exc:
		# ANY redis-level failure on the burn must degrade to a graceful None, NOT a
		# 500 on the whitelisted Confirm endpoint: a transient blip (ConnectionError),
		# a socket timeout (TimeoutError - a SIBLING of ConnectionError, not a
		# subclass, so the old narrow tuple missed it), or a degraded-write state
		# (ResponseError - -MISCONF stop-writes-on-bgsave-error, a read-only replica)
		# under which the DEL inside the transaction errors. None => not-consumable
		# => retryable InvalidConfirmation, and the token is NOT burned (the DEL never
		# applied), so a retry against a healthy cache still succeeds. Breadcrumb at
		# .error (NOT frappe.log_error - that floods the DB Error Log under a sustained
		# outage; and NOT .warning - a PROD bench's default log level is ERROR, so a
		# warning is filtered out and never written) so a tenant-wide "Confirm does
		# nothing" incident is greppable in the log file and distinguishable from
		# ordinary token expiry.
		frappe.logger("jarvis.pending_confirm").error(
			"consume burn failed; treating as not-consumable (retryable): %s",
			type(exc).__name__,
		)
		return None
	frappe.local.cache.pop(full_key, None)
	if raw is None:
		return None
	# cards_open gauge -1 (the card was consumed).
	_gauge_remove(token)
	_emit_cards_open("consume")
	# Drop the now-dead token from the owner index (best effort - list_for_owner
	# also prunes dead members on read, so a miss here self-heals).
	try:
		frappe.cache().srem(_owner_key(owner), token)
	except Exception:
		pass
	return pickle.loads(raw)


def list_for_owner(owner: str, conversation: str | None = None) -> list[dict]:
	"""Return the owner's currently-live parked records (each with its ``token``
	attached), newest-first is NOT guaranteed. Reads the per-owner index, peeks
	each token, and:
	  - prunes dead members (token record expired/consumed) from the index,
	  - filters to records whose stored owner matches ``owner`` (defense in
	    depth - never returns another user's token),
	  - filters to ``conversation`` when one is given, EXCEPT conversation-less
	    records ("") which carry no binding and surface under any filter (F1).

	Never returns another user's tokens. Used by the resync endpoint so the SPA
	can re-surface confirmation cards after a reload/reconnect.
	"""
	if not owner:
		return []
	try:
		members = {
			m.decode() if isinstance(m, bytes) else m
			for m in (frappe.cache().smembers(_owner_key(owner)) or set())
		}
	except Exception:
		return []
	if not members:
		return []
	out: list[dict] = []
	dead: list[str] = []
	for token in members:
		try:
			record = _read_record(token, swallow=False)
		except redis.exceptions.RedisError:
			# TRANSIENT read blip on this token (Redis reachable enough for smembers,
			# but this GET failed). Do NOT prune - the record may still be live, and
			# pruning would orphan it from the index (invisible to every future resync
			# for its full TTL). Skip it this round; a later clean resync re-surfaces it.
			continue
		if not record:
			dead.append(token)
			continue
		if record.get("owner") != owner:
			continue
		# A conversation-less record ("" - see consume/F1) carries no binding, so
		# surface it under ANY conversation filter: otherwise the card is
		# confirmable while its live event is on screen but VANISHES on reload
		# (resync drops it) for the full TTL - the SPA re-binds it to the current
		# conversation. Bound records are still filtered to the asked conversation.
		rec_conv = record.get("conversation")
		if conversation is not None and rec_conv and rec_conv != conversation:
			continue
		out.append({**record, "token": token})
	if dead:
		try:
			frappe.cache().srem(_owner_key(owner), *dead)
		except Exception:
			pass
	return out


def _pending_item(
	*,
	token: str,
	tool: str,
	args: dict,
	preview: dict | None,
	conversation: str,
	run_id: str,
	expires_at: int | None,
) -> dict:
	"""The ONE client-facing pending-confirmation item shape, shared by the live
	``action:pending`` push (jarvis.api), the resync endpoint, and the ``run:end``
	terminal - so the three cannot drift. Carries
	``token``/``tool``/``preview``/``summary``/``conversation``/``run_id``/
	``expires_at`` and NEVER the internal ``args``/``exec_user``/``args_hash``.

	``summary`` is COSMETIC: if ``_describe_call`` throws it degrades to "" (and is
	logged) - a confirmable card must NEVER be dropped because its human label failed
	to build. That is the invisible-card bug this whole change closes."""
	from jarvis.api import _describe_call

	try:
		summary = _describe_call(tool, args or {})
	except Exception:
		summary = ""
		frappe.log_error(
			title="pending_confirm: confirmation summary build failed",
			message=frappe.get_traceback(),
		)
	return {
		"token": token,
		"tool": tool,
		"preview": preview,
		"summary": summary,
		"conversation": conversation,
		"run_id": run_id,
		"expires_at": expires_at,
	}


def list_items_for_owner(owner: str, conversation: str | None = None) -> list[dict]:
	"""Client-facing pending-confirmation items for ``owner`` (optionally filtered to
	``conversation``), each built through the shared ``_pending_item`` shape so the
	resync endpoint and the ``run:end`` terminal cannot drift: a card missed on the
	best-effort live push re-surfaces on the turn's (fenced, backstopped) terminal
	without a manual reload. Item building never drops a record - only its cosmetic
	summary can degrade (see ``_pending_item``)."""
	return [
		_pending_item(
			token=r.get("token"),
			tool=r.get("tool"),
			args=r.get("args") or {},
			preview=r.get("preview"),
			conversation=r.get("conversation"),
			run_id=r.get("run_id"),
			expires_at=r.get("expires_at"),
		)
		for r in list_for_owner(owner, conversation=conversation)
	]


def clear_for_conversation(owner: str, conversation: str, run_id: str | None = None) -> int:
	"""Delete all of ``owner``'s live tokens STRICTLY bound to ``conversation``
	and return the count cleared. Conversation-less tokens ("") are left alone -
	they carry no conversation binding and may belong to another view. Used when a
	run is stopped so its parked confirmation cards cannot linger or resurface on
	resync (F6). Best-effort: a token consumed concurrently just isn't counted.

	``run_id``: when given AND the token itself carries a run_id, only that run's
	cards are swept - so stopping one run does not consume a sibling run's
	still-valid card. Nothing populates the token's run_id today (it is always
	""), so the filter no-ops and the whole conversation is swept, which is the
	intended behaviour for the single-run-per-conversation case."""
	if not owner or not conversation:
		return 0
	n = 0
	for rec in list_for_owner(owner, conversation=conversation):
		if rec.get("conversation") != conversation:
			continue  # list_for_owner surfaces conv-less tokens under any filter
		if run_id and rec.get("run_id") and rec.get("run_id") != run_id:
			continue  # a sibling run's card (run_id is "" today, so this no-ops)
		if consume(rec["token"], owner=owner, conversation=conversation) is not None:
			n += 1
	return n
