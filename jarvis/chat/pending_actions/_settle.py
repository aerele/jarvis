"""Settlement: deliver a terminal row's outcome exactly once (§4.5).

Settle is idempotent: the chat receipt flips in place (keyed on the PA name), the
continuation fires behind the ``settled`` compare-and-set, and both envelopes are
NULLed afterwards. A row that keeps failing is quarantined for an operator."""

from __future__ import annotations

import frappe

from jarvis.chat.pending_actions import _seal
from jarvis.chat.pending_actions._store import (
	CANCELLED,
	CONV,
	DISCARDED,
	EXECUTED,
	FAILED,
	SUPERSEDED,
	TERMINAL,
	bump_settle_attempts,
	claim_settled,
	get_row,
	null_sealed,
)

MAX_SETTLE_ATTEMPTS = 5
# Failures of these may still have reached the outside world: the chip says
# "unknown" instead of "nothing changed" (presentation only; the code stays).
MAY_HAVE_EXTERNAL_EFFECT = frozenset({"call_connector", "run_method"})

# Per-kind continuation, as a dotted path resolved at call time: a bare cron then
# settles with the same wiring as a request (no import-order dependency). The
# callable is ``fn(conversation: str | None, items: list[dict]) -> None`` and MUST
# call ``_store.claim_settled(names)`` inside the transaction that commits its
# effect (D5), acting only for the names it returns. ``None`` = settle marks the
# rows settled itself. PR-3a wires "chat"; PR-2c wires "file_box_held".
CONTINUATIONS: dict[str, str | None] = {"chat": None, "file_box_held": None}

_CHIP_BY_STATUS = {
	EXECUTED: "confirmed",
	DISCARDED: "discarded",
	SUPERSEDED: "superseded",
	CANCELLED: "cancelled",
}


def outcome_for(status: str, reason_code: str | None, tool: str | None) -> str:
	"""The chip (``action_outcome``) for a terminal row (§4.5 outcome table)."""
	if status == FAILED:
		if reason_code == "interrupted":
			return "unknown"
		if reason_code == "partial":
			return "partial"
		if reason_code == "failed" and tool in MAY_HAVE_EXTERNAL_EFFECT:
			return "unknown"
		return "failed"
	if status == CANCELLED and reason_code == "expired":
		return "expired"
	return _CHIP_BY_STATUS.get(status, "failed")


def _item(row) -> dict:
	"""What a continuation needs; args/result come from the seals (``{}`` / ``None``
	when a seal can't be opened: a tampered or unverifiable row)."""
	try:
		call = _seal.unseal_call(row)
	except _seal.SealError:
		call = {}
	try:
		settlement = _seal.unseal_settlement(row) or {}
	except _seal.SealError:
		settlement = {}
	return {
		"name": row.name,
		"kind": row.kind,
		"tool": row.tool,
		"args": call.get("args") or {},
		"result": settlement.get("result"),
		"status": row.status,
		"reason_code": row.reason_code or "",
		"outcome": outcome_for(row.status, row.reason_code, row.tool),
		"conversation": _delivery_conversation(row),
		"owner_user": row.owner_user,
		"exec_user": row.exec_user,
		"decided_by": row.decided_by,
		"result_doctype": row.result_doctype,
		"result_name": row.result_name,
		"batch_id": row.batch_id,
	}


def _delivery_conversation(row) -> str | None:
	conv = row.conversation or row.adopted_conversation
	return conv if conv and frappe.db.exists(CONV, conv) else None


def _deliver(item: dict) -> None:
	"""The chat receipt chip + the "change saved" push. Held rows have no chip."""
	conv = item["conversation"]
	if item["kind"] != "chat" or not conv:
		return
	from jarvis import api

	# A legacy sweep may have mislabelled an executing card; its real outcome wins.
	overwrite = ("cancelled", "superseded") if item["status"] in (EXECUTED, FAILED) else ()
	api.persist_tool_receipt(
		conv,
		item["tool"] or "",
		item["args"],
		item["result"],
		action_outcome=item["outcome"],
		flip_token=item["name"],
		overwrite_outcomes=overwrite,
	)
	if item["status"] == EXECUTED:
		from jarvis.chat import admission

		admission.publish_action_confirmed(conv)


def _continue(kind: str, conversation: str | None, items: list[dict]) -> None:
	path = CONTINUATIONS.get(kind)
	if path:
		frappe.get_attr(path)(conversation, items)
	else:
		claim_settled([i["name"] for i in items])
	frappe.db.commit()


def _begin(row, force: bool) -> bool:
	"""Count an attempt (committed first, so a crash still counts). False once the
	row is quarantined; the first refusal logs it."""
	if not force and (row.settle_attempts or 0) >= MAX_SETTLE_ATTEMPTS:
		if (row.settle_attempts or 0) == MAX_SETTLE_ATTEMPTS:
			bump_settle_attempts(row.name)
			frappe.log_error(
				title="jarvis.pending_action.settle_quarantined",
				message=f"{row.name}: settle failed {MAX_SETTLE_ATTEMPTS} times; use operator_settle.",
			)
			frappe.db.commit()
		return False
	bump_settle_attempts(row.name)
	frappe.db.commit()
	return True


def _settleable(row) -> bool:
	return bool(row) and not row.settled and row.status in TERMINAL


def settle(name: str, *, force: bool = False) -> bool:
	"""Deliver ``name``'s outcome (receipt, push, continuation), then drop its seals.
	True when this call settled it."""
	row = get_row(name)
	if not _settleable(row) or not _begin(row, force):
		return False
	try:
		item = _item(row)
		_deliver(item)
		_continue(row.kind, item["conversation"], [item])
		null_sealed([name])
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="jarvis.pending_action.settle_failed", message=frappe.get_traceback())
		frappe.db.commit()
		return False
	return bool(frappe.db.get_value("Jarvis Pending Action", name, "settled"))


def settle_batch(batch_id: str) -> int:
	"""Settle a typed batch: one receipt per row, ONE continuation per conversation."""
	if not batch_id:
		return 0
	names = frappe.db.sql_list(
		"SELECT name FROM `tabJarvis Pending Action` WHERE batch_id=%(b)s AND settled=0 ORDER BY decided_at, name",
		{"b": batch_id},
	)
	groups: dict[tuple, list[dict]] = {}
	for name in names:
		row = get_row(name)
		if not _settleable(row) or not _begin(row, False):
			continue
		try:
			item = _item(row)
			_deliver(item)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="jarvis.pending_action.settle_failed", message=frappe.get_traceback())
			frappe.db.commit()
			continue
		groups.setdefault((row.kind, item["conversation"]), []).append(item)
	settled = 0
	for (kind, conv), items in groups.items():
		try:
			_continue(kind, conv, items)
			null_sealed([i["name"] for i in items])
			frappe.db.commit()
			settled += len(items)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="jarvis.pending_action.settle_failed", message=frappe.get_traceback())
			frappe.db.commit()
	return settled
