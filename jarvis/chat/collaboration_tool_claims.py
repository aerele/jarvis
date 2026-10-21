"""Commit callback claims before tool I/O; a lost response never repeats a call."""

import hashlib
import json
import time

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import txn
from jarvis.chat.collaboration_callbacks import MAX_CALLS, permitted_callback


def receipt_id(binding, run_id: str, call_id: str) -> str:
	value = json.dumps([binding.name, run_id, call_id], separators=(",", ":"))
	return "shared-" + hashlib.sha256(value.encode()).hexdigest()


def has_unsettled_claims(*, conversation: str | None = None) -> bool:
	"""A terminal model run does not prove its previously claimed tool finished."""
	if not frappe.db.has_column("Jarvis Chat Turn", "collaboration_dispatches"):
		return False
	filters = {"collaboration_binding": ["!=", ""], "collaboration_dispatches": ["is", "set"]}
	if conversation:
		filters["conversation"] = conversation
	for raw in frappe.db.get_values("Jarvis Chat Turn", filters, "collaboration_dispatches", pluck=True):
		if unsettled(raw):
			return True
	return False


def unsettled(raw):
	try:
		entries = json.loads(raw or "{}")
	except (TypeError, ValueError, RecursionError):
		return True
	return (
		not isinstance(entries, dict)
		or len(entries) > MAX_CALLS
		or any(
			not isinstance(entry, dict)
			or not (
				(entry.get("state") == "Done" and entry.get("message"))
				or (entry.get("state") == "Abandoned" and entry.get("abandoned_at"))
			)
			for entry in entries.values()
		)
	)


def _ledger(permit):
	row = frappe.db.get_value(
		"Jarvis Chat Turn",
		permit.turn.run_id,
		[
			"conversation",
			"actor",
			"collaboration_binding",
			"collaboration_send_started_at",
			"collaboration_dispatches",
		],
		as_dict=True,
		for_update=True,
	)
	if (
		not row
		or not row.collaboration_send_started_at
		or (row.conversation, row.actor, row.collaboration_binding)
		!= (permit.turn.binding.conversation, permit.turn.binding.actor, permit.turn.binding.name)
	):
		c._deny()
	try:
		entries = json.loads(row.collaboration_dispatches or "{}")
	except (TypeError, ValueError, RecursionError):
		c._deny()
	if not isinstance(entries, dict) or len(entries) > MAX_CALLS:
		c._deny()
	return entries


def _store(permit, entries):
	frappe.db.set_value(
		"Jarvis Chat Turn",
		permit.turn.run_id,
		"collaboration_dispatches",
		json.dumps(entries, separators=(",", ":")),
		update_modified=False,
	)
	frappe.db.commit()


def claim(permit) -> bool:
	"""Joins the live callback authorization's Conv/User/Share/Role/Turn locks."""
	if permitted_callback(permit.turn.binding.conversation, permit.tool) != permit:
		c._deny()
	entries = _ledger(permit)
	if permit.call_id in entries:
		return False
	if len(entries) >= MAX_CALLS:
		c._deny()
	entries[permit.call_id] = {
		"tool": permit.tool,
		"state": "Running",
		"started_at": str(frappe.utils.now_datetime()),
	}
	# Grant revocation can now proceed while a previously claimed operation
	# finishes. Request-local actor permission inputs remain scoped to this claim.
	_store(permit, entries)
	return True


def complete(permit, message: str):
	"""Actual output may settle after revoke; this never authorizes another call."""
	return txn.replay_on_conflict(
		lambda: _complete(permit, message), label="collaboration.tool_receipt", fresh=True
	)


def _complete(permit, message: str):
	binding = c.resolve_identity(permit.turn.binding.session_key)
	if binding != permit.turn.binding:
		c._deny()
	frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
	entries = _ledger(permit)
	entry = entries.get(permit.call_id)
	if (
		not isinstance(entry, dict)
		or entry.get("tool") != permit.tool
		or entry.get("state") not in {"Running", "Uncertain", "Done", "Abandoned"}
	):
		c._deny()
	row = frappe.db.get_value(
		"Jarvis Chat Message",
		message,
		["conversation", "actor", "collaboration_binding", "collaboration_run_id"],
		as_dict=True,
	)
	if not row or (row.conversation, row.actor, row.collaboration_binding, row.collaboration_run_id) != (
		binding.conversation,
		binding.actor,
		binding.name,
		permit.turn.run_id,
	):
		c._deny()
	entry.update(state="Done", message=message)
	_store(permit, entries)


def reconcile(limit=25, *, conversation=None):
	"""Retain lost receipts as audit, never replay a claimed tool or a write.

	Only terminal runs beyond a conservative callback grace are eligible. Even
	if a delayed reader returns later, its receipt can still replace this audit
	marker. A delayed write callback must reauthorize before parking an action;
	actual writes run only behind the separate human decision claim.
	"""
	if not c.schema_ready() or not txn.owns_transaction():
		return {"reconciled": 0}
	grace = max(900, frappe.utils.cint(frappe.conf.get("http_timeout")) + 60)
	filters = {
		"collaboration_binding": ["!=", ""],
		"collaboration_dispatches": ["like", '%"Running"%'],
		"state": ["in", ["done", "errored", "cancelled"]],
	}
	if conversation:
		filters["conversation"] = conversation
	rows = frappe.get_all(
		"Jarvis Chat Turn",
		filters,
		["name", "conversation"],
		order_by="collaboration_recovery_checked_at asc, enqueued_at asc, name asc",
		limit=max(1, min(50, frappe.utils.cint(limit))),
	)
	changed, deadline = 0, time.monotonic() + 15
	for row in rows:
		if time.monotonic() >= deadline:
			break

		def unit():
			frappe.db.get_value(c.CONVERSATION, row.conversation, "name", for_update=True)
			turn = frappe.db.get_value(
				"Jarvis Chat Turn",
				row.name,
				["conversation", "actor", "collaboration_binding", "state", "collaboration_dispatches"],
				as_dict=True,
				for_update=True,
			)
			if (
				not turn
				or turn.conversation != row.conversation
				or turn.state not in {"done", "errored", "cancelled"}
			):
				frappe.db.rollback()
				return 0
			try:
				entries = json.loads(turn.collaboration_dispatches or "{}")
			except (TypeError, ValueError, RecursionError):
				frappe.db.rollback()
				return 0
			if not isinstance(entries, dict) or len(entries) > MAX_CALLS:
				frappe.db.rollback()
				return 0
			count = 0
			for call_id, entry in entries.items():
				if (
					not isinstance(entry, dict)
					or entry.get("state") != "Running"
					or not entry.get("started_at")
				):
					continue
				try:
					age = frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), entry["started_at"])
				except (TypeError, ValueError, OverflowError):
					continue
				if age <= grace:
					continue
				key = hashlib.sha256((turn.collaboration_binding + "\0" + call_id).encode()).hexdigest()
				action = frappe.db.get_value(
					"Jarvis Pending Action",
					{
						"collaboration_call_key": key,
						"conversation": turn.conversation,
						"collaboration_binding": turn.collaboration_binding,
						"actor": turn.actor,
						"run_id": row.name,
						"tool": entry.get("tool"),
					},
					["name", "status"],
					as_dict=True,
					for_update=True,
				)
				if action and action.status == "Executing":
					continue
				entry.update(state="Abandoned", abandoned_at=str(frappe.utils.now_datetime()))
				if action:
					entry["pending_action"] = action.name
				count += 1
			if count:
				frappe.db.set_value(
					"Jarvis Chat Turn",
					row.name,
					"collaboration_dispatches",
					json.dumps(entries, separators=(",", ":")),
				)
			frappe.db.set_value(
				"Jarvis Chat Turn",
				row.name,
				"collaboration_recovery_checked_at",
				frappe.utils.now_datetime(),
				update_modified=False,
			)
			frappe.db.commit()
			return count

		changed += txn.replay_on_conflict(unit, label="collaboration.claim_recovery", fresh=True)
	return {"reconciled": changed}
