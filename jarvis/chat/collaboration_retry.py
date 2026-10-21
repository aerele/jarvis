"""Requeue only an unsent, terminal actor request without changing its identity."""

import json

import frappe

from jarvis.chat import admission, prepare, txn
from jarvis.chat import collaboration as c
from jarvis.chat import turn_state as ts
from jarvis.chat.collaboration_bindings import require_live
from jarvis.chat.collaboration_turn import binding_for_run


def allowed(row):
	return bool(
		row.get("collaboration_retry_allowed")
		and row.state in {"cancelled", "errored"}
		and not row.collaboration_send_started_at
	)


def retry(conversation, run_id):
	actor = frappe.session.user
	binding = binding_for_run(run_id)
	if not binding or (binding.conversation, binding.actor) != (conversation, actor):
		c._deny()
	from jarvis.chat import policy

	ok, reason = policy.validate_can_send(actor)
	if not ok:
		return {"ok": False, "reason": reason}
	target = frappe.db.get_value(ts.TURN, run_id, "relay_target_id")

	def unit():
		ts._lock_shard(target)
		try:
			require_live(binding)
			row = frappe.db.get_value(ts.TURN, run_id, ["*"], as_dict=True, for_update=True)
			if not row or (row.conversation, row.actor, row.collaboration_binding, row.relay_target_id) != (
				conversation,
				actor,
				binding.name,
				target,
			):
				c._deny()
			if row.state in {"queued", "preparing", "ready", "dispatching", "streaming"}:
				frappe.db.commit()
				return {"ok": True, "duplicate": True, "run_id": run_id, "state": row.state}
			if row.state not in {"cancelled", "errored"} or row.collaboration_send_started_at:
				frappe.throw(
					"This request may already have run. Check its outcome before making another request."
				)
			if not allowed(row):
				frappe.throw(
					"This request cannot be retried. Check its outcome and send a new message if needed."
				)
			mode = frappe.db.get_value(ts.PUMP, target, "transport_mode")
			if not (
				(row.collaboration_executor == "pump" and mode == "pump")
				or (row.collaboration_executor == "rq" and mode == "legacy" and admission.admission_enabled())
			):
				frappe.throw(
					"Chat transport changed. Keep this message and start a new request after checking its outcome."
				)
			if admission._line_full(target, row.turn_class == "background"):
				frappe.db.rollback()
				return {
					"ok": False,
					"overloaded": True,
					"reason": "The site is busy. Your message is kept; try again shortly.",
				}
			seed = frappe.db.get_value(
				ts.MSG,
				row.seed_message,
				["conversation", "owner", "actor", "collaboration_binding", "collaboration_run_id", "role"],
				as_dict=True,
				for_update=True,
			)
			if not seed or tuple(
				seed.get(key)
				for key in (
					"conversation",
					"owner",
					"actor",
					"collaboration_binding",
					"collaboration_run_id",
					"role",
				)
			) != (conversation, actor, actor, binding.name, run_id, "user"):
				c._deny()
			if row.assistant_message:
				reply = frappe.db.get_value(
					ts.MSG,
					row.assistant_message,
					["conversation", "actor", "collaboration_binding", "collaboration_run_id", "role"],
					as_dict=True,
					for_update=True,
				)
				if not reply or tuple(
					reply.get(key)
					for key in (
						"conversation",
						"actor",
						"collaboration_binding",
						"collaboration_run_id",
						"role",
					)
				) != (conversation, actor, binding.name, run_id, "assistant"):
					c._deny()
				# Preserve the abandoned placeholder as audit, outside the common
				# transcript. The retry creates no duplicate user message.
				frappe.db.set_value(ts.MSG, row.assistant_message, {"hidden": 1, "streaming": 0})
			try:
				previous = json.loads(row.dispatch_payload or "{}")
			except (TypeError, ValueError, RecursionError):
				frappe.throw("This request cannot be retried because its preparation data is invalid.")
			if not isinstance(previous, dict):
				frappe.throw("This request cannot be retried because its preparation data is invalid.")
			attachments = prepare.original_attachments(previous)
			won = ts._run_cas(
				f"""UPDATE `tab{ts.TURN}` SET state='queued', reserved=0, pump_epoch=0,
				reservation_expires_at=NULL, preparing_at=NULL, ready_at=NULL, dispatching_at=NULL,
				first_event_at=NULL, deadline_at=NULL, recovery_started_at=NULL, done_at=NULL,
				terminal_observed_at=NULL, terminal_kind=NULL, terminal_payload=NULL,
				gateway_run_id=NULL, assistant_message=NULL, error=NULL, cancel_reason=NULL,
				cancel_requested=0, collaboration_retry_allowed=0,
				last_event_seq=0, usage_recorded=0, enriched_published=0,
				dispatch_payload=%(payload)s, enqueued_at=%(now)s, version=version+1
				WHERE name=%(run)s AND version=%(version)s AND state IN ('cancelled','errored')
				AND collaboration_send_started_at IS NULL""",
				{
					"run": run_id,
					"version": int(row.version),
					"now": frappe.utils.now_datetime(),
					"payload": json.dumps({"attachments": attachments}) if attachments else None,
				},
			)
			if not won:
				frappe.db.rollback()
				return {"ok": False, "reason": "This request changed. Reload the chat before retrying."}
			frappe.db.commit()
			return {"ok": True, "run_id": run_id, "state": "queued", "message_id": row.seed_message}
		except Exception:
			frappe.db.rollback()
			raise
		finally:
			ts.reset_lock_tracking()

	result = txn.replay_on_conflict(unit, label="collaboration.retry")
	if result.get("ok") and not result.get("duplicate"):
		from jarvis.chat.collaboration_ownership import wake_queued
		from jarvis.chat.events import publish_to_user

		publish_to_user(actor, {"kind": "run:queued", "conversation_id": conversation, "run_id": run_id})
		wake_queued(target)
	return result
