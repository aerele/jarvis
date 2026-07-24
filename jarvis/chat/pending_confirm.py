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
# or reconnect. TTL discipline mirrors selfhost.get_active_turn: dead members
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
) -> str:
	"""Store a pending call and return a fresh single-use token
	(secrets.token_urlsafe(24)). The stored record carries conversation,
	owner, tool, args (the full dict - this is the authoritative payload
	that will execute), args_hash, run_id, exec_user, preview. TTL _TTL_S.
	Returns the token.

	``preview`` is the park-time confirmation preview (dry-run "would" doc or
	described-intent dict). It is stored so the resync endpoint can return it
	verbatim instead of RE-running the dry-run - re-running fires unsandboxed
	on_submit/on_cancel side effects on every reload/reconnect (F2). Tokens
	minted before this field existed simply carry no ``preview``.

	``owner`` is the CONVERSATION OWNER - the human who sees the card, clicks
	Confirm, and whose browser is subscribed. Delivery + binding + confirm all
	key off this identity. ``exec_user`` is the scoped model-execution identity
	the confirmed write must run AS (so a confirm can never exceed the model
	path's permission scope). In managed mode owner == exec_user; in self-host
	owner is the operator and exec_user is the restricted tool user. It defaults
	to ``owner`` when omitted (managed-mode back-compat).
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
	frappe.cache().set_value(_key(token), record, expires_in_sec=_TTL_S)
	# cards_open gauge +1 (self-healing on expiry via the ZSET score).
	_gauge_add(token, record["expires_at"])
	_emit_cards_open("mint")
	# Index the token under its owner so list_for_owner can re-surface it. Best
	# effort: the token record is the source of truth (owner binding + execution
	# both read it), so an index hiccup must never block the park.
	try:
		cache = frappe.cache()
		cache.sadd(_owner_key(owner), token)
		cache.expire_key(_owner_key(owner), _TTL_S)
	except Exception:
		pass
	return token


def peek(token: str) -> dict | None:
	"""Return the stored record (dict) without consuming it, or None if the
	token is unknown/expired. Used to build the preview/UI event. Does NOT
	validate ownership - callers that act on it must.
	"""
	if not token:
		return None
	return frappe.cache().get_value(_key(token), use_local_cache=False)


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
	ownership matches do we delete - and that delete uses Redis' GETDEL,
	a single atomic server-side command (get-and-delete in one round trip,
	no separate check-then-delete on our side). If two confirmed consumes
	race each other here, the server serializes the two GETDELs: exactly
	one gets the pickled record back, the other gets None. That is the
	single-use guarantee - it does not depend on Python-level locking,
	which would not help anyway across separate worker processes.
	"""
	if not token:
		return None
	record = frappe.cache().get_value(_key(token), use_local_cache=False)
	if not record:
		return None
	# Owner is the real security boundary and is always enforced.
	if record.get("owner") != owner:
		return None
	# Conversation is a SECONDARY replay guard, not the boundary. A token minted
	# without a resolvable conversation ("" - a managed session_key->conversation
	# lookup miss, or self-host ambiguous concurrency) carries no conversation
	# binding, so an owner-matched consume must still succeed even when the caller
	# passes its current conversation id. Without this skip such a card is
	# delivered to the owner but EVERY Confirm click fails here and shows a
	# misleading "expired" toast (the card is un-confirmable for its full TTL).
	# When a conversation WAS bound, it is still enforced.
	stored_conv = record.get("conversation")
	if stored_conv and stored_conv != conversation:
		return None

	full_key = frappe.cache().make_key(_key(token))
	# GETDEL is a raw redis-py command, not one of RedisWrapper's own wrapped
	# methods (get_value/set_value/...), so unlike those it is NOT wrapped in
	# RedisWrapper's usual suppress(redis.exceptions.ConnectionError) - a
	# transient redis blip here would otherwise propagate as an uncaught 500
	# instead of the graceful None the caller expects (treated as
	# not-consumable -> InvalidConfirmation; the token is not burned, the user
	# can retry). Also defensively catch ResponseError: GETDEL requires
	# redis-server >= 6.2, and an older/misconfigured server rejects the
	# command outright. Either error returns None here WITHOUT falling back to
	# a non-atomic get-then-delete, which would reintroduce the very race
	# GETDEL exists to close - only the same atomic getdel is retried on a
	# later call.
	try:
		raw = frappe.cache().getdel(full_key)
	except (redis.exceptions.ConnectionError, redis.exceptions.ResponseError):
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
		record = peek(token)
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


def clear_for_conversation(owner: str, conversation: str, run_id: str | None = None) -> int:
	"""Delete all of ``owner``'s live tokens STRICTLY bound to ``conversation``
	and return the count cleared. Conversation-less tokens ("") are left alone -
	they carry no conversation binding and may belong to another view. Used when a
	run is stopped so its parked confirmation cards cannot linger or resurface on
	resync (F6). Best-effort: a token consumed concurrently just isn't counted.

	``run_id``: when given AND the token itself carries a run_id (self-host), only
	that run's cards are swept - so stopping one run does not consume a sibling
	run's still-valid card. In managed mode the token's run_id is always "" (it is
	never tracked there), so the filter no-ops and the whole conversation is swept,
	which is the intended behaviour for the single-run-per-conversation case."""
	if not owner or not conversation:
		return 0
	n = 0
	for rec in list_for_owner(owner, conversation=conversation):
		if rec.get("conversation") != conversation:
			continue  # list_for_owner surfaces conv-less tokens under any filter
		if run_id and rec.get("run_id") and rec.get("run_id") != run_id:
			continue  # a sibling run's card (self-host only; managed run_id is "")
		if consume(rec["token"], owner=owner, conversation=conversation) is not None:
			n += 1
	return n
