"""Non-executing transitions: discard, Stop/archive, conversation delete, and the
bulk rollback tools (§4.5). Every terminal write goes through ``_store``."""

from __future__ import annotations

import frappe

from jarvis._session import authenticated_user
from jarvis.chat.pending_actions import _seal
from jarvis.chat.pending_actions._execute import _refusal, authorize
from jarvis.chat.pending_actions._settle import settle
from jarvis.chat.pending_actions._store import (
	CANCELLED,
	DISCARDED,
	EXECUTING,
	FAILED,
	PENDING,
	TERMINAL,
	_terminal_update,
	_transition,
	claim_settled,
	get_row,
	lock_conversation,
	null_sealed,
	table_ready,
	waiters,
)
from jarvis.permissions import refuse_in_tool_dispatch

_BULK_LIMIT = 500


def _clear_autorun(conversation: str) -> None:
	"""A6: retiring a card ends any approved skill run / "confirm all" behind it."""
	flags = frappe.db.get_value(
		"Jarvis Conversation", conversation, ["skill_autorun", "request_autorun"], as_dict=True
	)
	if not flags:
		return
	if flags.skill_autorun:
		from jarvis.chat import turn_message_binding

		turn_message_binding.clear_skill_autorun(conversation)
	if flags.request_autorun:
		from jarvis import api

		api._request_autorun_clear(conversation)


def discard(name: str, *, kind: str = "chat", conversation: str | None = None) -> dict:
	"""The human declined: ``Pending -> Discarded`` (authz only; no identity preflight)."""
	refuse_in_tool_dispatch()
	approver = authenticated_user()
	if not approver or approver == "Guest":
		raise frappe.PermissionError("authentication required")
	row = get_row(str(name or "").strip()) if name else None
	allowed, _adopt = authorize(row, approver, kind, conversation)
	if not allowed:
		return _refusal("not_found", "This confirmation is no longer valid.")
	frappe.db.commit()
	lock_conversation(row.conversation)
	moved = _transition(row.name, [PENDING], DISCARDED, reason_code="discarded", decided_by=approver)
	if moved != "ok":
		frappe.db.rollback()
		if moved == "locked":
			return _refusal("busy", "This action is being handled right now. Try again in a moment.")
		fresh = get_row(row.name)
		if fresh and fresh.status == EXECUTING:
			return _refusal(
				"executing", "This action is already running; it can't be discarded.", pa_status=EXECUTING
			)
		if fresh and fresh.status in TERMINAL and not fresh.settled:
			settle(row.name)
		return {"ok": True, "data": {"status": "already_handled"}, "reason_code": "already_handled"}
	from jarvis import agent_audit, api

	if row.tool in api._WRITE_TOOLS:
		try:
			args = _seal.unseal_call(row).get("args") or {}
		except _seal.SealError:
			args = {}
		agent_audit.record_write(
			actor=approver,
			tool=row.tool,
			args=args,
			result=None,
			outcome="discarded",
			provenance="chat" if kind == "chat" else "approval",
			provenance_name=row.name if kind == "file_box_held" else "",
		)
	frappe.db.commit()
	if kind == "chat" and row.conversation:
		_clear_autorun(row.conversation)
	settle(row.name)
	return {"ok": True, "data": {"status": "discarded", "tool": row.tool}, "reason_code": "discarded"}


def _drop_waiter(parent: str, conversation: str, reason: str) -> None:
	"""Take ``conversation`` off a held row's waiter list, promoting the next waiter
	when it was the primary; a Pending row left with no waiter is Cancelled. Does
	not commit."""
	row = get_row(parent, lock="update")
	if not row:
		return
	members = waiters(parent)
	mine = [w for w in members if w.conversation == conversation]
	if not mine:
		return
	frappe.db.sql("DELETE FROM `tabJarvis Pending Action Waiter` WHERE name=%(n)s", {"n": mine[0].name})
	rest = [w for w in members if w.name != mine[0].name]
	if row.conversation == conversation or mine[0].role == "primary":
		promoted = rest[0] if rest else None
		if promoted:
			frappe.db.sql(
				"UPDATE `tabJarvis Pending Action Waiter` SET role='primary' WHERE name=%(n)s",
				{"n": promoted.name},
			)
		frappe.db.sql(
			"UPDATE `tabJarvis Pending Action` SET conversation=%(c)s WHERE name=%(n)s",
			{"n": parent, "c": promoted.conversation if promoted else None},
		)
	if not rest and row.status == PENDING:
		_terminal_update(parent, [PENDING], CANCELLED, reason_code=reason)


def _held_parents(conversation: str) -> list[str]:
	return frappe.db.sql_list(
		"SELECT DISTINCT parent FROM `tabJarvis Pending Action Waiter`"
		" WHERE conversation=%(c)s AND parenttype='Jarvis Pending Action'",
		{"c": conversation},
	)


def cancel_for_conversation(conversation: str, *, reason: str = "cancelled") -> list[str]:
	"""Stop / archive: cancel this conversation's Pending chat cards (only those
	that actually transition; an executing card keeps running) and drop it from any
	held row's waiters. Returns the chat cards cancelled."""
	if not conversation or not table_ready():
		return []
	frappe.db.commit()
	lock_conversation(conversation)
	chat = frappe.db.sql_list(
		"SELECT name FROM `tabJarvis Pending Action` WHERE conversation=%(c)s AND kind='chat' AND status='Pending'",
		{"c": conversation},
	)
	cancelled = [n for n in chat if _transition(n, [PENDING], CANCELLED, reason_code=reason) == "ok"]
	held = _held_parents(conversation)
	for parent in held:
		_drop_waiter(parent, conversation, reason)
	frappe.db.commit()
	for name in cancelled + held:
		settle(name)
	return cancelled


def on_conversation_trash(doc, method=None) -> None:
	"""``Jarvis Conversation`` on_trash (inside the delete's transaction; never
	commits). Chat cards are cancelled then raw-deleted, except an Executing one,
	whose final write/settle copes with the missing conversation. Held rows drop
	the conversation from their waiters; one left terminal with nobody waiting has
	nothing to deliver, so it is marked settled."""
	if not table_ready():
		return
	conv = doc.name
	rows = frappe.db.sql(
		"SELECT name, status FROM `tabJarvis Pending Action` WHERE conversation=%(c)s AND kind='chat'"
		" AND status != 'Executing' FOR UPDATE SKIP LOCKED",
		{"c": conv},
		as_dict=True,
	)
	for r in rows:
		if r.status == PENDING:
			_terminal_update(r.name, [PENDING], CANCELLED, reason_code="cancelled")
	names = tuple(r.name for r in rows)
	if names:
		frappe.db.sql("DELETE FROM `tabJarvis Pending Action` WHERE name IN %(n)s", {"n": names})
	for parent in _held_parents(conv):
		_drop_waiter(parent, conv, "cancelled")
		row = get_row(parent)
		if row and row.status in TERMINAL and not row.settled and not waiters(parent):
			null_sealed(claim_settled([parent]))


def _bulk_terminal(sql: str, to: str, reason_code: str) -> int:
	"""Flip every Pending row ``sql`` selects (runbook tools), then settle them."""
	done = 0
	while True:
		names = frappe.db.sql_list(sql + " LIMIT %(lim)s", {"lim": _BULK_LIMIT})
		moved = [n for n in names if _transition(n, [PENDING], to, reason_code=reason_code) == "ok"]
		frappe.db.commit()
		for n in moved:
			settle(n)
		done += len(moved)
		if len(names) < _BULK_LIMIT or not moved:
			return done


def cancel_all_chat_pending() -> int:
	"""Runbook (D4 flag -> 0): cancel every Pending chat card."""
	return _bulk_terminal(
		"SELECT name FROM `tabJarvis Pending Action` WHERE kind='chat' AND status='Pending'",
		CANCELLED,
		"cancelled",
	)


def withdraw_all_held() -> int:
	"""Runbook (PR-2c rollback): withdraw every Pending held write."""
	return _bulk_terminal(
		"SELECT name FROM `tabJarvis Pending Action` WHERE kind='file_box_held' AND status='Pending'",
		CANCELLED,
		"cancelled",
	)


def cancel_unverifiable() -> int:
	"""Runbook (after a key rotation/restore): fail every Pending row whose seal no
	longer opens, so none sits forever un-actionable."""
	done = 0
	names = frappe.db.sql_list("SELECT name FROM `tabJarvis Pending Action` WHERE status='Pending'")
	for name in names:
		row = get_row(name)
		try:
			_seal.unseal_call(row)
			continue
		except _seal.SealError as e:
			code = e.reason_code
		if _transition(name, [PENDING], FAILED, reason_code=code) == "ok":
			frappe.db.commit()
			settle(name)
			done += 1
		else:
			frappe.db.rollback()
	return done
