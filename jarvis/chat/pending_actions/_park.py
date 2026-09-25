"""Park: persist a proposed call as a sealed ``Pending`` row (§4.4 Park).

One transaction under the conversation lock: single-flight, the insert (sealed in
the controller's ``validate``), the caller's display row, one commit."""

from __future__ import annotations

from collections.abc import Callable

import frappe

from jarvis.chat.pending_actions import _seal
from jarvis.chat.pending_actions._store import EXECUTING, KINDS, PA, PENDING, SHEET, lock_conversation
from jarvis.permissions import refuse_in_tool_dispatch

# D7 supersede, as a dotted path. Called under the conversation lock with a live
# Pending chat row of the same conversation: ``fn(row, *, conversation,
# owner_user) -> bool``. It must not commit; True means it moved ``row`` to
# Superseded (via ``_store._transition``) and park may proceed. None = never.
SUPERSEDE_HOOK: str | None = "jarvis.chat.pending_confirm.supersede_on_park"

# Target-bound tools re-validate against this snapshot at execute (D2); creates are
# validated by the real write instead.
TARGET_BOUND = frozenset(
	{"update_doc", "submit_doc", "cancel_doc", "amend_doc", "delete_doc", "apply_workflow_action"}
)
# A File Box sheet's own columns (``park(sheet=...)``).
_SHEET_FIELDS = frozenset(
	{"collecting", "collecting_turn", "record_count", "sheet_counts", "question_count", "needs_fix"}
)


class ConfirmationPendingError(Exception):
	"""A live card already waits in this conversation (single-flight)."""


def _target_names(args: dict) -> list[str]:
	if isinstance(args.get("names"), list):
		return [str(n) for n in args["names"] if n]
	if isinstance(args.get("updates"), list):
		return [str(u["name"]) for u in args["updates"] if isinstance(u, dict) and u.get("name")]
	return [str(args["name"])] if args.get("name") else []


def target_state(doctype: str, name: str) -> list:
	"""``[modified_us, docstatus]`` now, or ``[None, None]`` when the record is gone."""
	cur = frappe.db.get_value(doctype, name, ["modified", "docstatus"], as_dict=True)
	if not cur:
		return [None, None]
	return [frappe.utils.get_datetime(cur.modified).strftime("%Y-%m-%d %H:%M:%S.%f"), int(cur.docstatus or 0)]


def snapshot_targets(tool: str, args: dict) -> list[list]:
	"""``[[doctype, name, modified_us, docstatus], ...]`` for a target-bound call."""
	if tool not in TARGET_BOUND or not isinstance(args, dict):
		return []
	doctype = args.get("doctype")
	if not isinstance(doctype, str) or not frappe.db.exists("DocType", doctype):
		return []
	if frappe.get_meta(doctype).issingle:
		return []
	return [[doctype, n, *target_state(doctype, n)] for n in _target_names(args)]


def _check_single_flight(conversation: str, owner_user: str, legacy_pending) -> list[str]:
	"""Refuse a second live chat card here; returns the names the supersede hook
	retired (settled after the commit)."""
	superseded = []
	live = frappe.db.sql(
		"SELECT * FROM `tabJarvis Pending Action` WHERE conversation=%(c)s AND kind='chat'"
		" AND status IN ('Pending', 'Executing')",
		{"c": conversation},
		as_dict=True,
	)
	for row in live:
		if row.status == EXECUTING or not SUPERSEDE_HOOK:
			raise ConfirmationPendingError("answer or discard the pending card")
		if not frappe.get_attr(SUPERSEDE_HOOK)(row, conversation=conversation, owner_user=owner_user):
			raise ConfirmationPendingError("answer or discard the pending card")
		superseded.append(row.name)
	if legacy_pending and legacy_pending(conversation):
		raise ConfirmationPendingError("answer or discard the pending card")
	return superseded


def park(
	*,
	kind: str,
	owner_user: str,
	exec_user: str,
	tool: str,
	args: dict,
	card=None,
	preview=None,
	summary: str = "",
	conversation: str | None = None,
	skill_docname: str | None = None,
	run_id: str | None = None,
	dedup_key: str | None = None,
	dedup_keys: list[str] | tuple = (),
	waiters: list[str] | tuple = (),
	needs_input=None,
	sheet: dict | None = None,
	legacy_pending: Callable[[str], bool] | None = None,
	display_row: Callable[[object], None] | None = None,
) -> str:
	"""Park ``tool(args)`` for a human decision and return the PA name.

	- ``legacy_pending(conversation) -> bool``: the pre-PA card store's single-flight
	  (PR-3a's Redis union); True refuses the park.
	- ``display_row(doc)``: inserts the chat pending row in park's transaction (it must
	  not commit), so the card and its row land or roll back together.
	- ``waiters``: held rows' conversations, primary first (defaults to ``conversation``).
	- ``dedup_key`` / ``dedup_keys``: a held row's primary key (the unique ``open_key``)
	  and every per-item key, stored keyed (HMAC) for the waiter subset rule.
	- ``sheet``: a File Box sheet's own columns (``_SHEET_FIELDS``).

	Raises ``ConfirmationPendingError`` (single-flight) and ``frappe.PermissionError``
	inside a tool call (S6)."""
	refuse_in_tool_dispatch()
	if kind not in KINDS or not (owner_user and exec_user and tool) or not isinstance(args, dict):
		raise ValueError("park: kind, owner_user, exec_user, tool and args are required")
	conversation = conversation or None
	if set(sheet or ()) - _SHEET_FIELDS:
		raise ValueError("park: not a sheet column")
	waiters = list(waiters) or ([conversation] if kind in ("file_box_held", SHEET) and conversation else [])
	if waiters and waiters[0] != conversation:
		raise ValueError("park: the primary waiter must be the conversation")
	frappe.db.commit()
	try:
		lock_conversation(conversation)
		superseded = (
			_check_single_flight(conversation, owner_user, legacy_pending)
			if kind == "chat" and conversation
			else []
		)
		doc = frappe.get_doc(
			{
				"doctype": PA,
				"kind": kind,
				"status": PENDING,
				"tool": tool,
				"summary": summary,
				"card": card,
				"preview": preview,
				"origin_conversation": conversation or "",
				"conversation": conversation,
				"owner_user": owner_user,
				"exec_user": exec_user,
				"skill_docname": skill_docname or "",
				"run_id": run_id or "",
				"open_key": _seal.open_key(owner_user, dedup_key) if dedup_key else None,
				"dedup_keys": _seal.canonical([_seal.open_key(owner_user, k) for k in dedup_keys])
				if dedup_keys
				else None,
				"needs_input": needs_input,
				**(sheet or {}),
				"waiters": [
					{"conversation": c, "role": "primary" if i == 0 else "waiter"}
					for i, c in enumerate(waiters)
				],
			}
		)
		doc.flags.jarvis_server_write = True
		doc.flags.pa_args = args
		doc.flags.pa_targets = snapshot_targets(tool, args)
		doc.insert(ignore_permissions=True)
		if display_row:
			display_row(doc)
		frappe.db.commit()
	except BaseException:
		frappe.db.rollback()
		raise
	if superseded:
		from jarvis.chat.pending_actions._settle import settle

		for name in superseded:
			settle(name)
	return doc.name
