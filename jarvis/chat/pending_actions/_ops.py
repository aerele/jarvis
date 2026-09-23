"""Operator levers for a stuck ``Jarvis Pending Action`` (System Manager, POST,
never from inside a tool). Each leaves an Error Log trail; row text stays fixed."""

from __future__ import annotations

import frappe

from jarvis._session import authenticated_user
from jarvis.chat.pending_actions._settle import settle
from jarvis.chat.pending_actions._store import (
	EXECUTING,
	FAILED,
	PENDING,
	TERMINAL,
	_terminal_update,
	claim_settled,
	get_row,
	null_sealed,
)
from jarvis.permissions import refuse_in_tool_dispatch

_OPERATOR_REASON = "Failed by an operator."


@frappe.whitelist(methods=["POST"])
def operator_fail(name: str, reason: str = "") -> dict:
	"""End a Pending or Executing row as Failed. An Executing one may have run, so it
	ends ``interrupted`` (chip "unknown"), never "nothing changed"."""
	refuse_in_tool_dispatch()
	frappe.only_for("System Manager")
	operator = authenticated_user()
	frappe.db.commit()
	row = get_row(str(name or "").strip(), lock="update")
	if not row or row.status not in (PENDING, EXECUTING):
		frappe.db.rollback()
		return {"ok": False, "reason_code": "already_handled"}
	code = "interrupted" if row.status == EXECUTING else "failed"
	_terminal_update(
		row.name, [row.status], FAILED, reason_code=code, reason=_OPERATOR_REASON, decided_by=operator
	)
	frappe.log_error(
		title="jarvis.pending_action.operator_fail",
		message=f"{row.name}: {row.status} -> Failed/{code} by {operator}. Note: {str(reason or '')[:500]}",
	)
	frappe.db.commit()
	settle(row.name)
	return {"ok": True, "pa_status": FAILED, "reason_code": code}


@frappe.whitelist(methods=["POST"])
def operator_settle(name: str) -> dict:
	"""Release a quarantined terminal row: one more delivery attempt, then mark it
	settled regardless (its seals are dropped either way)."""
	refuse_in_tool_dispatch()
	frappe.only_for("System Manager")
	operator = authenticated_user()
	row = get_row(str(name or "").strip())
	if not row or row.status not in TERMINAL or row.settled:
		return {"ok": False, "reason_code": "already_handled"}
	delivered = settle(row.name, force=True)
	if not delivered:
		null_sealed(claim_settled([row.name]))
	frappe.log_error(
		title="jarvis.pending_action.operator_settle",
		message=f"{row.name}: settled by {operator} (delivered={delivered})",
	)
	frappe.db.commit()
	return {"ok": True, "delivered": delivered}
