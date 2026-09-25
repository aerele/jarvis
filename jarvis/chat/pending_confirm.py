"""Pending-confirmation store for the write-safety confirmation gate (issue #186).

A gated write is parked under a single-use token instead of running; only a human
Confirm (``jarvis.chat.actions_api``) runs it. This module is the facade every
caller imports; its public signatures predate the durable store and are kept.

Since PR-3a a card in a real conversation is a ``Jarvis Pending Action`` row (the
token is the row name): the card and its chat display row commit together, it
survives Redis loss, and ``pending_actions.execute`` / ``discard`` act on it. The
Redis store (``pending_confirm_legacy``) stays for dual-read until PR-4 (D4):

- ``mint`` parks a pending action, unless the ``jarvis_pa_chat_cards`` site flag is
  0, the table is absent, or the conversation is not a real ``Jarvis Conversation``
  (a conversation-less or stray id can host neither the row's Link nor a display
  row) - those still mint a Redis token;
- every read (``peek``, the lists, the gate's single-flight) unions both stores; a
  token with no pending-action row is a legacy token;
- a pending-action card never expires (a legacy token keeps its Redis 15 minutes);
  the next proposal after a human message supersedes it instead (D7,
  ``supersede_on_park``), and every listed card carries ``seq`` / ``recent`` for the
  typed-reply scope (decision 6).

Pending-action rows are read with plain SELECTs, never unsealed on a list. A DB
error there surfaces as ``PendingConfirmStorageError`` on a strict read, exactly
like a Redis one, so no caller mistakes an outage for an empty store."""

from __future__ import annotations

import time

import frappe

from jarvis.chat import pending_confirm_legacy as legacy
from jarvis.chat.pending_actions._park import ConfirmationPendingError  # re-exported for the gate
from jarvis.chat.pending_confirm_legacy import (  # re-exported for callers
	_CLAIM_PREFIX,
	_OWNER_PREFIX,
	_PREFIX,
	_TTL_S,
	PendingConfirmOutcomeUnknown,
	PendingConfirmStorageError,
	_claim_key,
	_key,
	_owner_key,
	_pending_item,
	args_hash,
)

FLAG = "jarvis_pa_chat_cards"
MSG = "Jarvis Chat Message"
# Who "spoke" for the typed scope and D7 supersede: a board answer is the user too
# (decision 11); automatic resumes never are.
HUMAN_ORIGINS = ("human", "board_answer")
_COLS = (
	"name, kind, status, conversation, owner_user, exec_user, tool, run_id, preview,"
	" skill_docname, summary, creation"
)


def _db_errors() -> tuple:
	return (frappe.db.OperationalError, frappe.db.InterfaceError)


def _pa_ready() -> bool:
	from jarvis.chat.pending_actions._store import table_ready

	try:
		return table_ready()
	except Exception:
		return False


def pa_minting(conversation: str | None) -> bool:
	"""Whether a new card for ``conversation`` parks as a pending action (D4)."""
	if not conversation or not frappe.utils.cint(frappe.conf.get(FLAG, 1)):
		return False
	return _pa_ready() and bool(frappe.db.exists("Jarvis Conversation", conversation))


def _select(where: str, params: dict, *, strict: bool) -> list:
	"""Chat pending-action rows matching ``where`` (sealed columns never read)."""
	if not _pa_ready():
		return []
	try:
		return frappe.db.sql(
			f"SELECT {_COLS} FROM `tabJarvis Pending Action` WHERE kind='chat' AND {where}",
			params,
			as_dict=True,
		)
	except _db_errors() as exc:
		frappe.logger("jarvis.pending_confirm").error("pending action read failed: %s", type(exc).__name__)
		if strict:
			raise PendingConfirmStorageError("pending action read failed") from exc
		return []


def _live(row) -> bool:
	return row.status == "Pending"


def _epoch(dt) -> int:
	"""Site-timezone ``dt`` as epoch seconds (the legacy records' clock)."""
	age = frappe.utils.now_datetime() - frappe.utils.get_datetime(dt)
	return int(time.time() - age.total_seconds())


def _record(row) -> dict:
	"""A pending-action row in the legacy record shape (no args: never unsealed)."""
	created = _epoch(row.creation)
	preview = row.preview
	if isinstance(preview, str):
		preview = frappe.parse_json(preview) if preview else None
	return {
		"token": row.name,
		"conversation": row.conversation or "",
		"owner": row.owner_user,
		"exec_user": row.exec_user,
		"tool": row.tool,
		"run_id": row.run_id or "",
		"preview": preview,
		"skill_docname": row.skill_docname or None,
		"summary": row.summary or "",
		"expires_at": None,
		"created_at": created,
		"pending_action": True,
	}


def _legacy_strict(strict: bool) -> bool:
	"""With pending-action cards on, Redis holds only leftovers: its outage reads as
	"no Redis card", so it never blocks a new card, the gate or a typed reply."""
	return strict and not (frappe.utils.cint(frappe.conf.get(FLAG, 1)) and _pa_ready())


def _legacy_pending(owner: str, conversation: str) -> bool:
	"""Park's ``legacy_pending`` hook: a live Redis card strictly in ``conversation``."""
	return any(
		r.get("conversation") == conversation
		for r in legacy.list_for_owner(owner, conversation=conversation, strict=_legacy_strict(True))
	)


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
	skill_docname: str | None = None,
) -> str | None:
	"""Park ``tool(args)`` for the owner's Confirm and return its single-use token,
	or None when it could not be stored (the gate then publishes no card). Raises
	``ConfirmationPendingError`` when a live card here refuses it (single-flight).

	``owner`` is the conversation owner (who confirms); ``exec_user`` the identity
	the confirmed call runs as (defaults to ``owner``); ``preview`` the park-time
	preview (its ``card`` is what the chat renders); ``skill_docname`` the Approve &
	run stamp; ``expires_at`` only bounds a legacy token. A pending action gets its
	display row here, in park's transaction, so the gate's later
	``persist_pending_action`` is a no-op; it never expires."""
	if not pa_minting(conversation):
		token = legacy.mint(
			conversation=conversation,
			owner=owner,
			tool=tool,
			args=args,
			run_id=run_id,
			exec_user=exec_user,
			preview=preview,
			expires_at=expires_at,
			skill_docname=skill_docname,
		)
		return _supersede_for_legacy(conversation, owner, token) if token else None
	from jarvis import api
	from jarvis.chat import pending_actions

	try:
		summary = api._describe_call(tool, args or {})
	except Exception:
		summary = ""  # cosmetic: a card is never dropped over its label
		frappe.log_error(
			title="pending_confirm: confirmation summary build failed", message=frappe.get_traceback()
		)
	try:
		return pending_actions.park(
			kind="chat",
			owner_user=owner,
			exec_user=exec_user or owner,
			tool=tool,
			args=args,
			card=preview.get("card") if isinstance(preview, dict) else None,
			preview=preview,
			summary=summary,
			conversation=conversation,
			skill_docname=skill_docname,
			run_id=run_id,
			legacy_pending=lambda conv: _legacy_pending(owner, conv),
			display_row=lambda doc: api._insert_pending_row(conversation, tool, preview, doc.name, None),
		)
	except (frappe.PermissionError, ConfirmationPendingError):
		raise
	except Exception:
		frappe.log_error(
			title="pending_confirm: park failed; token not stored", message=frappe.get_traceback()
		)
		return None


def _supersede_for_legacy(conversation: str, owner: str, token: str) -> str | None:
	"""D4: a legacy (Redis) mint still supersedes this conversation's pending-action
	cards under D7, exactly as park would. Runs only once ``token`` is stored, so a
	failed mint never retires the old card; any refusal or failure here removes
	``token`` (``ConfirmationPendingError`` re-raised, else None)."""
	if not conversation or not _pa_ready():
		return token
	from jarvis.chat.pending_actions._park import _check_single_flight
	from jarvis.chat.pending_actions._settle import settle
	from jarvis.chat.pending_actions._store import lock_conversation

	frappe.db.commit()
	try:
		lock_conversation(conversation)
		superseded = _check_single_flight(conversation, owner, None)
		frappe.db.commit()
	except ConfirmationPendingError:
		frappe.db.rollback()
		legacy.rollback_token(token, owner)
		raise
	except Exception:
		frappe.db.rollback()
		legacy.rollback_token(token, owner)
		frappe.log_error(
			title="jarvis.pending_confirm.legacy_supersede_failed",
			message=frappe.get_traceback(),
		)
		return None
	for name in superseded:
		settle(name)
	return token


def rollback_token(token: str, owner: str) -> None:
	"""Tear down a just-minted card the gate could not deliver. A pending action ends
	``Cancelled`` and settled with no chip, its never-shown display row removed.
	NEVER raises."""
	if not token:
		return
	try:
		rows = _select("name=%(n)s", {"n": token}, strict=True)
	except Exception:
		rows = []
	if not rows:
		legacy.rollback_token(token, owner)
		return
	from jarvis.chat.pending_actions._store import (
		CANCELLED,
		PENDING,
		_terminal_update,
		claim_settled,
		null_sealed,
	)

	try:
		if _terminal_update(token, [PENDING], CANCELLED, reason_code="cancelled"):
			frappe.db.sql(
				"DELETE FROM `tabJarvis Chat Message` WHERE tool_call_id=%(t)s AND tool_status='pending'",
				{"t": token},
			)
			null_sealed(claim_settled([token]))
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="jarvis.pending_confirm.rollback_failed", message=frappe.get_traceback())


def peek(token: str, *, strict: bool = False) -> dict | None:
	"""The live record for ``token`` without consuming it, or None (unknown, used, or
	a legacy token past its 15 minutes). Does NOT check ownership - callers that act
	on it must.
	``strict=True`` raises ``PendingConfirmStorageError`` instead of reading an
	outage as "gone"."""
	if not token:
		return None
	rows = _select("name=%(n)s", {"n": token}, strict=strict)
	if rows:
		return _record(rows[0]) if _live(rows[0]) else None
	return legacy.peek(token, strict=strict)


def is_pending_action(token: str) -> bool:
	"""``token`` names a chat pending action in any state: a click on a card that left
	Pending still goes to the executor, which says why and settles a lost settle."""
	return bool(token) and bool(_select("name=%(n)s", {"n": token}, strict=False))


def consume(token: str, *, owner: str, conversation: str) -> dict | None:
	"""Validate and single-use consume a legacy ``token`` (owner always; the stored
	conversation when it has one), returning its record, or None without burning it.

	A pending action has no legacy record, so it is never consumed (None, untouched):
	it runs only through ``pending_actions.execute`` (claim-first, at most once) and
	its sealed args never leave the executor."""
	if not token:
		return None
	return legacy.consume(token, owner=owner, conversation=conversation)


def list_for_owner(owner: str, conversation: str | None = None, *, strict: bool = False) -> list[dict]:
	"""The owner's live cards from both stores (each with its ``token``), unordered.
	``conversation`` filters; a legacy conversation-less token still surfaces under
	any filter (F1). Never another user's card. ``strict=True`` raises on a read
	failure (on Redis only while it is the minting store, ``_legacy_strict``)."""
	if not owner:
		return []
	where, params = "owner_user=%(o)s AND status='Pending'", {"o": owner}
	if conversation is not None:
		where += " AND conversation=%(c)s"
		params["c"] = conversation
	cards = [_record(r) for r in _select(where, params, strict=strict)]
	return cards + legacy.list_for_owner(owner, conversation=conversation, strict=_legacy_strict(strict))


def list_items_for_owner(owner: str, conversation: str | None = None, *, strict: bool = False) -> list[dict]:
	"""Client-facing items for the owner's live cards (the resync endpoint and the
	``run:end`` terminal), in the one ``_pending_item`` shape plus ``seq`` /
	``recent`` (``with_recency``). A pending action's summary is the one stored at
	park; a legacy card's is built here and degrades to "" rather than dropping the
	card."""
	items = []
	for r in list_for_owner(owner, conversation=conversation, strict=strict):
		if r.get("pending_action"):
			items.append(
				{k: r.get(k) for k in ("token", "tool", "preview", "summary", "conversation", "run_id")}
				| {"expires_at": r["expires_at"], "created_at": r["created_at"]}
			)
			continue
		items.append(
			_pending_item(
				token=r.get("token"),
				tool=r.get("tool"),
				args=r.get("args") or {},
				preview=r.get("preview"),
				conversation=r.get("conversation"),
				run_id=r.get("run_id"),
				expires_at=r.get("expires_at"),
				created_at=r.get("created_at"),
			)
		)
	return with_recency(items, strict=strict)


def latest_human_seq(conversation: str) -> int | None:
	"""The seq of the conversation's latest human user row. For the typed scope an
	unset origin (a row older than the column) counts as human: a bare "yes" then
	binds fewer cards, never more."""
	return frappe.db.sql(
		f"SELECT MAX(seq) FROM `tab{MSG}` WHERE conversation=%(c)s AND role='user'"
		" AND IFNULL(origin, '') IN %(o)s",
		{"c": conversation, "o": ("", *HUMAN_ORIGINS)},
	)[0][0]


def with_recency(items: list[dict], *, strict: bool = False) -> list[dict]:
	"""Stamp each item's ``seq`` (its chat display row) and ``recent``: parked after
	the conversation's latest human message, so a bare typed "yes"/"no" may bind it
	(decision 6). A card with no display row (conversation-less F1) is never recent."""
	groups: dict[str, list[dict]] = {}
	for it in items:
		it["seq"], it["recent"] = None, False
		if it.get("conversation") and it.get("token"):
			groups.setdefault(it["conversation"], []).append(it)
	try:
		for conv, group in groups.items():
			seqs = dict(
				frappe.db.sql(
					f"SELECT tool_call_id, seq FROM `tab{MSG}` WHERE conversation=%(c)s AND role='tool'"
					" AND tool_call_id IN %(t)s",
					{"c": conv, "t": tuple(i["token"] for i in group)},
				)
			)
			latest = latest_human_seq(conv)
			for it in group:
				seq = seqs.get(it["token"])
				it["seq"] = int(seq) if seq is not None else None
				it["recent"] = seq is not None and (latest is None or seq > latest)
	except _db_errors() as exc:
		frappe.logger("jarvis.pending_confirm").error("card recency read failed: %s", type(exc).__name__)
		if strict:
			raise PendingConfirmStorageError("card recency read failed") from exc
	return items


def _supersedable(token: str, conversation: str) -> bool:
	"""D7: the running turn's triggering message is a ``human`` / ``board_answer`` user
	row (an unset origin is NOT human here) newer than this card's display row."""
	from jarvis.chat import turn_message_binding

	try:
		msg = turn_message_binding.current_turn_message_id(conversation)
	except Exception:
		return False
	if not msg:
		return False
	trigger = frappe.db.get_value(MSG, msg, ["conversation", "role", "origin", "seq"], as_dict=True)
	if not trigger or trigger.conversation != conversation or trigger.role != "user":
		return False
	if trigger.origin not in HUMAN_ORIGINS:
		return False
	card = frappe.db.get_value(
		MSG, {"conversation": conversation, "tool_call_id": token, "role": "tool"}, "seq"
	)
	return card is not None and trigger.seq > card


def supersede_on_park(row, *, conversation: str, owner_user: str) -> bool:
	"""``_park.SUPERSEDE_HOOK`` (D7), under park's conversation lock and never
	committing: retire ``row`` as ``Superseded`` (SKIP LOCKED: a card another actor
	holds stays and refuses the park), flip its chip, and end the autorun it paused
	(A6). Park settles it after its commit (the DATA-quoted supersede note)."""
	from jarvis.chat.pending_actions._store import PENDING, SUPERSEDED, _transition

	if row.status != PENDING or row.conversation != conversation:
		return False
	if not _supersedable(row.name, conversation):
		return False
	if _transition(row.name, [PENDING], SUPERSEDED, reason_code="superseded") != "ok":
		return False
	frappe.db.sql(
		f"UPDATE `tab{MSG}` SET tool_status='', action_outcome='superseded', pending_card=NULL,"
		" content=%(content)s WHERE conversation=%(c)s AND tool_call_id=%(t)s AND tool_status='pending'",
		{"c": conversation, "t": row.name, "content": f"{row.tool} → superseded"},
	)
	_end_paused_autorun(conversation)
	return True


def _end_paused_autorun(conversation: str) -> None:
	"""A6 for supersede, without committing (park's transaction). The request-scoped
	approval this very message armed ("..., confirm all") is its own, not the paused
	run's, so it stays."""
	from jarvis.chat import turn_message_binding

	try:
		trigger = turn_message_binding.current_turn_message_id(conversation) or ""
	except Exception:
		trigger = ""
	frappe.db.sql(
		"UPDATE `tabJarvis Conversation` SET skill_autorun=0, skill_autorun_at=NULL,"
		" skill_autorun_skill=NULL WHERE name=%(c)s AND skill_autorun=1",
		{"c": conversation},
	)
	frappe.db.sql(
		"UPDATE `tabJarvis Conversation` SET request_autorun=0, request_autorun_at=NULL,"
		" request_autorun_msg=NULL WHERE name=%(c)s AND request_autorun=1"
		" AND IFNULL(request_autorun_msg, '') != %(m)s",
		{"c": conversation, "m": trigger},
	)


def blocks_new_card(owner: str, conversation: str) -> bool:
	"""The gate's single-flight (F16), strict on ``conversation`` (a conversation-less
	card never blocks here): a live card the next park could not supersede (D7), a
	running one, or a live legacy token. Raises ``PendingConfirmStorageError`` when
	the minting store can't answer."""
	cards = [
		c
		for c in list_for_owner(owner, conversation=conversation, strict=True)
		if c.get("conversation") == conversation
	]
	if any(not (c.get("pending_action") and _supersedable(c["token"], conversation)) for c in cards):
		return True
	return bool(_select("conversation=%(c)s AND status='Executing'", {"c": conversation}, strict=True))


def clear_for_conversation(owner: str, conversation: str, run_id: str | None = None) -> int:
	"""The Stop / armed-macro-stop sweep: retire the cards strictly bound to
	``conversation`` and return how many. Pending actions go through
	``pending_actions.cancel_for_conversation``, which COMMITS the caller's
	transaction first (call this at a safe commit point) and also takes the
	conversation off held writes it waits on; legacy tokens as before (``run_id``
	scopes only those). Best-effort: never raises."""
	if not owner or not conversation:
		return 0
	cleared = 0
	if _pa_ready():
		from jarvis.chat import pending_actions

		try:
			cleared += len(pending_actions.cancel_for_conversation(conversation))
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="jarvis.pending_confirm.conversation_cleanup_failed", message=frappe.get_traceback()
			)
	return cleared + legacy.clear_for_conversation(owner, conversation, run_id)


def cards_open_gauge() -> int:
	"""Open cards across both stores (observability only)."""
	count = legacy.cards_open_gauge()
	if _pa_ready():
		try:
			count += frappe.db.sql(
				"SELECT COUNT(*) FROM `tabJarvis Pending Action` WHERE kind='chat' AND status='Pending'"
			)[0][0]
		except Exception:
			pass
	return count
