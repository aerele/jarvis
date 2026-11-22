"""Scheduled and on-demand runs of user agents.

A five-minute cron (``jarvis.hooks.scheduler_events``) calls
:func:`run_due_user_agents`. Each due agent gets a NEW chat bound to it (the same
session key scheme as a chat the owner opens) and its ``schedule_prompt`` as a
background turn, sent the way a scheduled macro step is (``api._enqueue_turn``).
The turn runs as the agent's owner, so the owner's ERP permissions apply.

Fail closed, as the macro and agent schedulers do:

* the site flag off: nothing runs and nothing is touched;
* an owner that is Administrator, Guest, disabled or without Jarvis access: the
  slot is skipped, the failure recorded and the schedule switched off;
* an agent that is switched off: the schedule moves on, nothing is recorded;
* anything that stops the turn (never synced, usage limit, no model, a full
  queue, a dispatch error): the failure is stored on the row as a code plus a
  sentence, and the owner is notified.

The slot is claimed (``next_run_at`` moved on by a compare-and-set) before the
turn is sent, so two sweeps never run one slot twice. A missed or failed slot is
not retried: the agent runs again at its next scheduled time.
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from jarvis._session import impersonate
from jarvis.chat import user_agents

DOCTYPE = user_agents.DOCTYPE
SWEEP_MAX_PER_TICK = 25

_NEXT = " It will run again at its next scheduled time."
_SENTENCES = {
	"owner_not_allowed": (
		"Scheduled run skipped: this agent's owner may not run unattended work (Administrator, "
		"a disabled account or no Jarvis access). The schedule was switched off."
	),
	"not_synced": "Scheduled run skipped: the agent is not on the runtime yet. Use Retry sync." + _NEXT,
	"usage_limit": "Scheduled run skipped: your monthly usage limit is reached." + _NEXT,
	"subscription_suspended": "Scheduled run skipped: the subscription does not include chat right now."
	+ _NEXT,
	"llm_not_configured": "Scheduled run skipped: no AI model connection is set up." + _NEXT,
	"release_update_required": "Scheduled run skipped: a Jarvis update is rolling out on this workspace."
	+ _NEXT,
	"workspace_resetting": "Scheduled run skipped: the workspace is being rebuilt." + _NEXT,
	"maintenance": "Scheduled run skipped: this workspace is being upgraded." + _NEXT,
	"site_is_copy": "Scheduled run skipped: this site is a restored copy and cannot reach Jarvis." + _NEXT,
	"overloaded": "Scheduled run skipped: Jarvis was too busy to start it." + _NEXT,
	"dispatch_failed": "Scheduled run could not be started." + _NEXT,
}


def run_due_user_agents() -> None:
	"""Start every enabled, scheduled agent whose ``next_run_at`` has passed."""
	if not user_agents.is_enabled():
		return
	now = now_datetime()
	# System context: the sweep runs every owner's schedule, each as its owner.
	due = frappe.get_all(
		DOCTYPE,
		filters={"schedule_enabled": 1, "next_run_at": ["<=", now]},
		fields=["*"],
		order_by="next_run_at asc, name asc",
		limit=SWEEP_MAX_PER_TICK,
	)
	for row in due:
		try:
			_sweep_one(row, now)
			# Per-agent isolation: a later row's rollback must not undo this row's claim
			# and outcome (the slot would run again on the next sweep).
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- per-record isolation in a batch
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"jarvis user agent sweep failed: {row.name}", message=frappe.get_traceback()
			)


def start_run(row, prompt: str, *, scheduled: bool) -> dict:
	"""Open a new chat on agent ``row`` and send ``prompt`` as its first turn. Runs
	as the current user, who must be the owner. Returns ``{ok, conversation}`` or
	``{ok: False, code}`` when the turn could not be queued."""
	from jarvis.chat import api as chat_api

	conversation = user_agents.open_conversation(row, agent_initiated=scheduled)
	frappe.db.set_value(DOCTYPE, row.name, "last_run_conversation", conversation, update_modified=False)
	out = chat_api._enqueue_turn(conversation, prompt, origin="macro", interactive=not scheduled) or {}
	if out.get("ok") is False:
		return {"ok": False, "code": "overloaded", "conversation": conversation}
	return {"ok": True, "conversation": conversation, "run_id": out.get("run_id")}


def entitlement_code(owner: str) -> str | None:
	"""The policy gate a typed message passes, as a code (None when allowed)."""
	from jarvis.chat.policy import validate_can_send

	ok, reason = validate_can_send(owner)
	return None if ok else (reason or "blocked")


def sentence_for(code: str) -> str:
	return _SENTENCES.get(code) or "Scheduled run skipped: Jarvis cannot run it right now." + _NEXT


def _sweep_one(row, now) -> None:
	from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

	if not frappe.utils.cint(row.enabled):
		_claim(row, now)
		return
	if not is_valid_unattended_owner(row.owner) or not has_jarvis_access(row.owner):
		frappe.db.set_value(
			DOCTYPE, row.name, {"schedule_enabled": 0, "next_run_at": None}, update_modified=False
		)
		_record(row, now, "owner_not_allowed", notify=False)
		return
	if not _claim(row, now):
		return
	# The claim must survive a failed launch's rollback, or the slot comes due again
	# on the next sweep and fails (and notifies) every five minutes.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- release the claimed slot row
	code = None if row.synced_at else "not_synced"
	code = code or entitlement_code(row.owner)
	if code:
		_record(row, now, code)
		return
	try:
		with impersonate(row.owner):
			out = start_run(row, row.schedule_prompt, scheduled=True)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"jarvis user agent run failed: {row.name}", message=frappe.get_traceback())
		_record(row, now, "dispatch_failed")
		return
	if not out.get("ok"):
		_record(row, now, out.get("code") or "dispatch_failed")
		return
	stamp_started(row.name, now, out["conversation"])


def stamp_started(name: str, now, conversation: str) -> None:
	frappe.db.set_value(
		DOCTYPE,
		name,
		{
			"last_run_at": now,
			"last_run_status": "Started",
			"last_run_error": "",
			"last_run_message": "",
			"last_run_conversation": conversation,
		},
		update_modified=False,
	)


def _claim(row, now) -> bool:
	"""Move ``next_run_at`` to the next slot, only while it still holds the value
	the sweep read. True when this sweep took the slot."""
	from jarvis.chat.macro_scheduler import compute_next_run

	nxt = compute_next_run(
		row.schedule_frequency,
		row.schedule_time,
		from_dt=now,
		weekday=row.schedule_weekday,
		day_of_month=row.schedule_day_of_month,
	)
	table = frappe.qb.DocType(DOCTYPE)
	frappe.qb.update(table).set(table.next_run_at, nxt).where(
		(table.name == row.name) & (table.next_run_at == row.next_run_at)
	).run()
	cursor = getattr(frappe.db, "_cursor", None)
	return bool(cursor and cursor.rowcount == 1)


def _record(row, now, code: str, *, notify: bool = True) -> None:
	sentence = sentence_for(code)
	frappe.db.set_value(
		DOCTYPE,
		row.name,
		{
			"last_run_at": now,
			"last_run_status": "Failed",
			"last_run_error": code,
			"last_run_message": sentence,
		},
		update_modified=False,
	)
	if notify:
		_notify_owner(row, sentence)


def _notify_owner(row, sentence: str) -> None:
	"""Best effort: a notice in the owner's bell. Never raises."""
	try:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": row.owner,
				"type": "Alert",
				"subject": f"Scheduled agent run did not start: {row.agent_name}",
				"email_content": sentence,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title=f"jarvis user agent notice failed: {row.name}", message=frappe.get_traceback())
