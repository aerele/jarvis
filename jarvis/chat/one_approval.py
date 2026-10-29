"""One approval per proposed create (admin-v2 #777 / #778).

The agent can stage one new record twice: as a draft card (a ``jarvis-action`` block in
its reply, saved through ``actions_api.apply_action``) and as a gated ``create_doc``
confirmation card. A create has no target that goes stale, so each approval used to
insert, the second one with the values the person had not edited.

Saving a draft create now:

- binds to the reply that proposed it (``message``): an assistant reply in the
  caller's conversation whose first ``jarvis-action`` block creates this doctype. A
  named reply that is anything else is refused. That reply is used once:
  ``draft_saved_as`` is claimed by a conditional UPDATE and set to the new record in
  the write's own transaction, so a second save of the same draft (concurrent, or a
  retry after a lost receipt) finds it taken;
- compares it with every create card of the doctype in the chat that is waiting,
  running, or ran after the reply was proposed. A card IS this proposal only when it
  came from the same turn (no user message between its chat row and the reply) AND
  its original call equals the reply's original block (``card.proposal_key``, stamped
  at park). Nothing else proves two proposals apart but the doctype: a card from an
  earlier turn with other values may be the very record the person just edited
  ("make it the 30th"). So every other card of the doctype (another turn, other
  values, no key, a Redis-era card, a block that does not map) is ambiguous and the
  save is refused with what to do: confirm or discard the waiting card, or ask for a
  new draft when one already ran;
- retires a waiting card that IS this proposal in the save's transaction (a failed
  save rolls it back), and refuses when such a card already ran ("already created
  from its confirmation card").

Both product clients name the reply. A save that names none (the API is older than
draft keys) cannot be bound: it is refused while any create card of the doctype exists
in the chat, and is otherwise a plain create.

Limits: a Redis-era card (only while the pending-action store is off) is never retired
from here and its Confirm takes no row lock: a draft saved between that Confirm taking
the token and writing its receipt is not seen. Two identical records proposed in one
turn, one per route, read as one proposal (the card is retired visibly and the
assistant is told it did not run)."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal

import frappe
from frappe import _

MSG = "Jarvis Chat Message"
PA = "Jarvis Pending Action"
_SAVING = "saving"
# A number written as text: no leading zero (a reference "0436..." stays text).
_NUMBER = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?")

SAME, UNSURE = "same", "unsure"


def create_key(doctype: str | None, values) -> str | None:
	"""The identity of a proposed create: its doctype and ORIGINAL values, exactly.
	Blanks are dropped and a number written as text is that number (2546 is "2546",
	never rounded; "0436" stays text); every value is tagged with its kind, so no text
	can stand in for a number, a row or a list. None when there are no values."""
	if not doctype or not isinstance(values, dict):
		return None
	body = json.dumps(["create", doctype, _norm(values)], sort_keys=True)
	return hashlib.sha256(body.encode()).hexdigest()


def _norm(value):
	if isinstance(value, dict):
		kept = {str(k): _norm(v) for k, v in value.items() if not _blank(v)}
		return ["map", sorted(kept.items())]
	if isinstance(value, list | tuple):
		return ["list", [_norm(v) for v in value]]
	if isinstance(value, bool | int | float):
		return ["num", _decimal(repr(int(value) if isinstance(value, bool) else value))]
	if isinstance(value, str):
		text = value.strip()
		return ["num", _decimal(text)] if _NUMBER.fullmatch(text) else ["text", text]
	return ["other", json.dumps(value, sort_keys=True, default=str)]


def _blank(value) -> bool:
	return value is None or (isinstance(value, str) and not value.strip()) or value in ([], {})


def _decimal(text: str) -> str:
	return format(Decimal(text).normalize(), "f")


def _proposal(conversation: str, message: str, doctype: str):
	"""``(row, original_values)`` for the reply that proposed this draft, or None when
	``message`` is not an assistant reply here whose card creates ``doctype``.
	``original_values`` is None when a field of the block does not map to the doctype."""
	from jarvis.chat import filebox_cards

	row = frappe.db.get_value(
		MSG,
		{"name": message, "conversation": conversation},
		["name", "role", "content", "creation", "seq"],
		as_dict=True,
	)
	if not row or row.role != "assistant":
		return None
	block = filebox_cards.parse_action(row.content)
	if not block or (block.get("verb") or "create") != "create" or block.get("doctype") != doctype:
		return None
	call = filebox_cards.tool_call(block)
	return row, (call[1].get("values") if call else None)


def _claim(message: str) -> bool:
	"""Take the reply's one save. A conditional UPDATE reads the latest committed row
	and locks it, so a concurrent save of the same draft waits here and then misses."""
	from jarvis.chat.pending_actions._store import rowcount

	m = frappe.qb.DocType(MSG)
	(
		frappe.qb.update(m)
		.set(m.draft_saved_as, _SAVING)
		.where((m.name == message) & ((m.draft_saved_as.isnull()) | (m.draft_saved_as == "")))
	).run()
	return rowcount() == 1


def mark_saved(message: str, name: str) -> None:
	"""Name the record the draft became, in the write's transaction."""
	if message:
		frappe.db.set_value(MSG, message, "draft_saved_as", name, update_modified=False)


# -- the create cards a draft is compared with ------------------------------------------


def _card_of(preview) -> dict:
	if isinstance(preview, str):
		preview = frappe.parse_json(preview) if preview else None
	card = preview.get("card") if isinstance(preview, dict) else None
	return card if isinstance(card, dict) else {}


def _pa_cards(conversation: str, since) -> list[frappe._dict]:
	"""Chat create cards that can still run, and those that ran since ``since`` (all of
	them when None). A locking read (NOWAIT), so it sees the latest committed state
	whatever this request read before: a card a Confirm is claiming right now raises
	``frappe.QueryTimeoutError``, one changed after this request's read view (snapshot
	isolation on) ``frappe.QueryDeadlockError``."""
	pa = frappe.qb.DocType(PA)
	status = pa.status.isin(["Pending", "Executing"])
	ran = pa.status == "Executed"
	if since is not None:
		ran = ran & (pa.decided_at >= since)
	rows = (
		frappe.qb.from_(pa)
		.select(pa.name, pa.status, pa.preview, pa.result_doctype, pa.result_name)
		.where(
			(pa.kind == "chat")
			& (pa.conversation == conversation)
			& (pa.tool == "create_doc")
			& (status | ran)
		)
		.for_update(nowait=True)
	).run(as_dict=True)
	cards = []
	for row in rows:
		card = _card_of(row.preview)
		cards.append(
			frappe._dict(
				token=row.name,
				state={"Pending": "waiting", "Executing": "running"}.get(row.status, "ran"),
				doctype=row.result_doctype or card.get("doctype"),
				key=card.get("proposal_key"),
				result=row.result_name,
				retirable=True,
			)
		)
	return cards


def _legacy_cards(conversation: str, since) -> list[frappe._dict]:
	"""Redis-era create cards here: live tokens, and those confirmed since ``since``
	(their chat receipt; a pending action's receipt is its row, read above)."""
	from jarvis.chat import pending_confirm_legacy

	cards = []
	# Strict: an unreadable store is a refusal to try again, never "no card". A token
	# with no conversation (F1) may be this chat's too.
	for r in pending_confirm_legacy.list_for_owner(
		frappe.session.user, conversation=conversation, strict=True
	):
		if r.get("conversation") in (conversation, "") and r.get("tool") == "create_doc":
			args = r.get("args") or {}
			cards.append(
				frappe._dict(
					token=r.get("token"),
					state="waiting",
					doctype=args.get("doctype"),
					key=create_key(args.get("doctype"), args.get("values")),
					result=None,
					retirable=False,
				)
			)
	m = frappe.qb.DocType(MSG)
	receipts = (
		frappe.qb.from_(m)
		.select(m.tool_call_id, m.tool_args, m.tool_result)
		.where(
			(m.conversation == conversation)
			& (m.role == "tool")
			& (m.tool_name == "create_doc")
			& (m.action_outcome == "confirmed")
			& m.tool_call_id.isnotnull()
			& (m.tool_call_id != "")
		)
		.for_update(nowait=True)
	)
	if since is not None:
		receipts = receipts.where(m.modified >= since)
	receipts = receipts.run(as_dict=True)
	# A pending action's receipt is its row, read above.
	if receipts:
		pa = frappe.qb.DocType(PA)
		rows = frappe.qb.from_(pa).select(pa.name).where(pa.name.isin([r.tool_call_id for r in receipts]))
		of_cards = set(rows.run(pluck=True))
		receipts = [r for r in receipts if r.tool_call_id not in of_cards]
	for row in receipts:
		args = frappe.parse_json(row.tool_args) or {}
		data = (frappe.parse_json(row.tool_result) or {}).get("data") or {}
		cards.append(
			frappe._dict(
				token=row.tool_call_id,
				state="ran",
				doctype=data.get("doctype") or args.get("doctype"),
				key=create_key(args.get("doctype"), args.get("values")),
				result=data.get("name"),
				retirable=False,
			)
		)
	return cards


def _chat_rows(conversation: str, tokens: list[str]) -> dict[str, int]:
	if not tokens:
		return {}
	m = frappe.qb.DocType(MSG)
	rows = (
		frappe.qb.from_(m)
		.select(m.tool_call_id, m.seq)
		.where((m.conversation == conversation) & (m.role == "tool") & m.tool_call_id.isin(tokens))
	).run()
	return {token: seq for token, seq in rows}


def _user_between(conversation: str, a: int, b: int) -> bool:
	low, high = sorted((a, b))
	m = frappe.qb.DocType(MSG)
	return bool(
		frappe.qb.from_(m)
		.select(m.name)
		.where((m.conversation == conversation) & (m.role == "user") & (m.seq > low) & (m.seq < high))
		.limit(1)
		.run()
	)


def _relation(card, draft, key: str | None, conversation: str, seqs: dict) -> str:
	"""SAME proposal, or UNSURE (see the module docstring). A card whose chat row this
	request cannot see has no known turn, so it is never SAME."""
	if draft is None or not key or card.key != key:
		return UNSURE
	seq = seqs.get(card.token)
	if seq is None or _user_between(conversation, seq, draft.seq):
		return UNSURE
	return SAME


# -- the check ---------------------------------------------------------------------------


def claim_create(conversation: str, doctype: str, message: str) -> tuple[dict | None, list[str]]:
	"""Before a draft create is written: ``(refusal, [])`` or ``(None, retired_cards)``.

	Takes the conversation lock (held to the caller's commit) and re-checks ownership
	under it. The reads that can change the answer (the claim, card states, receipts)
	are locking reads, so they see the latest committed state however old this
	request's read view is; a row that moved after that view (snapshot isolation) or
	one held right now is a refusal to try again, never a guess. The plain reads are of
	rows that do not change (the reply, chat row order), and a card whose chat row is
	not visible is ambiguous. A refusal rolls back. Otherwise the reply is claimed and the
	cards that are this proposal are retired in the caller's transaction; the caller
	calls ``mark_saved`` with the write and ``settle_retired`` after its commit."""
	from jarvis.chat import pending_confirm
	from jarvis.chat.actions_api import _require_own_conversation
	from jarvis.chat.pending_actions._store import PENDING, SUPERSEDED, _transition, lock_conversation

	def refuse(said: str):
		frappe.db.rollback()
		return {"ok": False, "error": {"code": "AlreadyApprovedError", "message": said}}, []

	try:
		lock_conversation(conversation)
		_require_own_conversation(conversation)
		draft = original = None
		if message:
			found = _proposal(conversation, message, doctype)
			if not found:
				return refuse(_("This draft is no longer available. Reload the chat and try again."))
			draft, original = found
			if not _claim(draft.name):
				return refuse(_("This draft was already saved. Nothing new was created."))
		since = draft.creation if draft else None
		cards = _pa_cards(conversation, since) + _legacy_cards(conversation, since)
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError, pending_confirm.PendingConfirmStorageError):
		return refuse(_("This chat changed while saving, for example a card is being confirmed. Try again."))
	key = create_key(doctype, original)
	seqs = _chat_rows(conversation, [c.token for c in cards if c.token])
	retire = []
	for card in cards:
		if card.doctype not in (doctype, None):
			continue
		relation = _relation(card, draft, key, conversation, seqs)
		if card.state == "running":
			return refuse(
				_("A {0} is being created from a confirmation card right now. Nothing was saved.").format(
					doctype
				)
			)
		if card.state == "waiting":
			if relation == SAME and card.retirable:
				retire.append(card.token)
				continue
			return refuse(_waiting(doctype))
		if relation == SAME:
			return refuse(
				_(
					"{0} {1} was already created from its confirmation card. Nothing new was created. "
					"To change it, ask in the chat to update {1}."
				).format(doctype, card.result)
			)
		return refuse(
			_(
				"{0} {1} was created from a confirmation card after this draft was proposed and may be this "
				"same record, so this draft was not saved. If you need another one, ask in the chat to draft it again."
			).format(doctype, card.result)
		)
	if len(retire) > 1:  # two cards claim to be this proposal: never pick one
		return refuse(_waiting(doctype))
	for token in retire:
		reason = "The same record was saved from its draft card; this card was not run."
		# Held since the locked read, so this cannot miss; checked all the same.
		if _transition(token, [PENDING], SUPERSEDED, reason_code="superseded", reason=reason) != "ok":
			return refuse(_waiting(doctype))
		pending_confirm._flip_superseded_chip(conversation, token, "create_doc")
	return None, retire


def _waiting(doctype: str) -> str:
	return _(
		"A confirmation card for a {0} is still waiting in this chat and may be this same record. "
		"Confirm or discard it first. Nothing was saved."
	).format(doctype)


def settle_retired(names: list[str]) -> None:
	"""Tell the chat and the assistant about the retired cards, after the commit. The
	record is saved by now: a failure here is logged, never returned (a "failed" save
	would invite a retry), and the reconciler settles what is left."""
	if not names:
		return
	from jarvis.chat.pending_actions import settle

	for name in names:
		try:
			settle(name)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="jarvis.one_approval.settle_failed", message=frappe.get_traceback())
