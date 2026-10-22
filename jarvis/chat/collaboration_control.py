"""Explicit actor controls; ordinary owner endpoints keep their original gates."""

import json

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import turn_state as ts
from jarvis.chat import txn
from jarvis.chat.collaboration_bindings import require_live
from jarvis.chat.collaboration_read import authorize_read
from jarvis.chat.collaboration_runtime import RuntimePolicy, verify_dispatched_session
from jarvis.chat.collaboration_turn import binding_for_run


def stop(conversation: str, run_id: str):
	actor = frappe.session.user
	binding = binding_for_run(run_id)
	if not binding or binding.conversation != conversation:
		c._deny()

	def intent():
		# A participant controls their own run; only the actual owner retains
		# moderation. System Manager is not another actor's execution identity.
		reader = authorize_read(conversation)
		if actor == reader.doc.owner:
			pass  # Existing owner moderation remains available with writes off.
		elif actor == binding.actor:
			require_live(binding)
		else:
			c._deny()
		row = frappe.db.get_value(
			ts.TURN,
			run_id,
			[
				"actor",
				"collaboration_binding",
				"state",
				"version",
				"assistant_message",
				"relay_target_id",
				"collaboration_executor",
				"pump_epoch",
				"collaboration_send_started_at",
				"gateway_run_id",
				"dispatch_payload",
			],
			as_dict=True,
			for_update=True,
		)
		if not row or (row.actor, row.collaboration_binding) != (binding.actor, binding.name):
			c._deny()
		v, state = int(row.version), row.state
		won, abort = False, False
		if state == "queued":
			won = ts.cancel_queued(run_id, v)
		elif state in {"preparing", "ready"}:
			won = ts.cancel_preparing_or_ready(run_id, v)
		elif state == "dispatching" and not row.collaboration_send_started_at:
			won = ts._run_cas(
				f"""UPDATE `tab{ts.TURN}` SET state='cancelled', cancel_requested=1,
				reserved=0, reservation_expires_at=NULL, done_at=%(now)s, version=version+1
				WHERE name=%(r)s AND state='dispatching' AND version=%(v)s
				AND collaboration_send_started_at IS NULL""",
				{"now": frappe.utils.now_datetime(), "r": run_id, "v": v},
			)
		elif state in {"dispatching", "streaming"}:
			won = ts.request_cancel(run_id, v)
			abort = bool(won)
		elif state == "recovering":
			# The mode-independent recovery host owns this result. Fence new
			# callbacks immediately, without pretending the outcome is known.
			won = ts._run_cas(
				f"""UPDATE `tab{ts.TURN}` SET cancel_requested=1, version=version+1
				WHERE name=%(r)s AND state='recovering' AND version=%(v)s""",
				{"r": run_id, "v": v},
			)
			abort = bool(won and row.collaboration_send_started_at)
		else:
			frappe.db.commit()
			return row, "already_finished"
		if won:
			# Only the initiating actor's own unsent stop is reversible. Owner
			# moderation and access fences never create a retry capability.
			frappe.db.set_value(
				ts.TURN,
				run_id,
				"collaboration_retry_allowed",
				int(actor == binding.actor and not row.collaboration_send_started_at),
				update_modified=False,
			)
		if won and not abort and state != "recovering" and row.assistant_message:
			# Only the stored row for THIS turn may be made inert. No other
			# actor's message is changed by a client-supplied pointer.
			message = frappe.db.get_value(
				ts.MSG,
				row.assistant_message,
				["conversation", "actor", "collaboration_binding", "collaboration_run_id", "role"],
				as_dict=True,
				for_update=True,
			)
			if not message or tuple(
				message.get(k)
				for k in ("conversation", "actor", "collaboration_binding", "collaboration_run_id", "role")
			) != (conversation, binding.actor, binding.name, run_id, "assistant"):
				c._deny()
			frappe.db.set_value(ts.MSG, row.assistant_message, {"streaming": 0, "stopped": 1})
		frappe.db.commit()
		return row, "abort" if abort else "cancelled" if won else "changed"

	row, action = txn.replay_on_conflict(intent, label="collaboration.stop")
	if action == "cancelled":
		from jarvis.chat.collaboration_ownership import wake_queued

		wake_queued(row.relay_target_id)
	if action == "abort":
		# No grant/Turn/Message locks survive the intent commit. The opaque
		# key, verified physical UUID and exact run identify the abort target.
		try:
			from jarvis.chat import agent_session_pool

			payload = json.loads(row.dispatch_payload or "{}")
			runtime = payload.get("collaboration_runtime") or {}
			policy = RuntimePolicy(runtime.get("skills_root") or "", runtime.get("model_ref") or "")
			settings = frappe.get_cached_doc("Jarvis Settings")
			with agent_session_pool.checkout(settings.agent_url or "") as sess:
				verify_dispatched_session(sess, binding, policy)
				frappe.db.commit()
				sess.chat_abort(binding.session_key, row.gateway_run_id or run_id)
			# The RPC acknowledgement is not the terminal reply. Preserve the
			# run until its actual aborted/error/final event (or fenced recovery)
			# supplies the complete text and honest outcome.
		except Exception as exc:
			frappe.db.rollback()
			from jarvis.chat.collaboration_runtime import preserve_drift

			preserve_drift(binding, exc)
			frappe.log_error(title="shared request stop", message=f"run={run_id} cause={type(exc).__name__}")
			return {"ok": True, "run_id": run_id, "state": "stopping"}
	return {
		"ok": action != "changed",
		"run_id": run_id,
		"state": "stopping" if action == "abort" else action,
	}
