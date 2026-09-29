"""Operator levers for a stuck ``Jarvis Pending Action`` (System Manager, POST,
never from inside a tool). Each leaves an Error Log trail; row text stays fixed.

The File Box sheet runbook: a sheet stuck collecting, sealed or executing is ended
with ``operator_fail``; one still collecting ends ``stuck`` (its run is told the sheet
got stuck while being listed, not that it could not be applied), as the reconciler
fails one collecting past 2 h. One stuck unsettled is released with
``operator_settle``. Either way its questions close and it keeps a valid, possibly
empty, ``sheet_outcome``. Before rolling sheets back: switch ``file_box_sheets``
off, let collecting sheets seal, then ``withdraw_all_held`` (or apply/skip them)."""

from __future__ import annotations

import frappe

from jarvis._session import authenticated_user
from jarvis.chat.pending_actions._settle import settle
from jarvis.chat.pending_actions._store import (
	EXECUTING,
	FAILED,
	PENDING,
	SHEET,
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
	"""End a Pending (a collecting sheet too: ``stuck``) or Executing row as Failed. An
	Executing one may have run, so it ends ``interrupted`` (chip "unknown"), never
	"nothing changed"."""
	refuse_in_tool_dispatch()
	frappe.only_for("System Manager")
	operator = authenticated_user()
	frappe.db.commit()
	row = get_row(str(name or "").strip(), lock="update")
	if not row or row.status not in (PENDING, EXECUTING):
		frappe.db.rollback()
		return {"ok": False, "reason_code": "already_handled"}
	code = "interrupted" if row.status == EXECUTING else "stuck" if row.collecting else "failed"
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
		won = claim_settled([row.name])
		if won and row.kind == SHEET:
			from jarvis.chat.held_sheet_seal import close_ended

			close_ended(won)
		null_sealed(won)
	frappe.log_error(
		title="jarvis.pending_action.operator_settle",
		message=f"{row.name}: settled by {operator} (delivered={delivered})",
	)
	frappe.db.commit()
	return {"ok": True, "delivered": delivered}
