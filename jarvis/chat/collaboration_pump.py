"""Issue-and-poll policy checks preserve the pump's nonblocking reactor."""

import json

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import policy, txn
from jarvis.chat import turn_state as ts
from jarvis.chat.collaboration_bindings import require_live
from jarvis.chat.collaboration_context import require_prompt_budget
from jarvis.chat.collaboration_runtime import RuntimePolicy, verify_response
from jarvis.chat.collaboration_turn import binding_for_run
from jarvis.exceptions import SITE_IS_COPY_CODE, AgentUnreachableError


def record_terminal(ctx, rs, kind, payload):
	from jarvis.chat.collaboration_output import lock_reply
	from jarvis.chat.scrub import scrub_for_share

	binding = binding_for_run(rs.run_id)
	if not binding or (binding.conversation, binding.actor, binding.session_key) != (
		rs.conversation,
		rs.owner,
		rs.session_key,
	):
		c._deny()
	safe = {
		key: scrub_for_share(value) for key in ("text", "error") if isinstance(value := payload.get(key), str)
	}
	if payload.get("state") in {"aborted", "error", "failed_final"}:
		safe["state"] = payload["state"]
	if payload.get("aborted") is True:
		safe["aborted"] = True

	def unit():
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		lock_reply(binding, rs.run_id, rs.assistant_message)
		row = frappe.db.get_value(
			ts.TURN,
			rs.run_id,
			["state", "version", "pump_epoch", "collaboration_executor"],
			as_dict=True,
			for_update=True,
		)
		if (
			row.state != "streaming"
			or row.collaboration_executor != "pump"
			or int(row.pump_epoch) != ctx.epoch
		):
			return None
		# Stop changes the durable version without invalidating this exact
		# run's actual terminal. Never discard its precious answer as stale.
		version = int(row.version)
		if not ts.mark_terminal_observed(rs.run_id, version, ctx.epoch, kind, safe):
			return None
		return version, safe

	return txn.replay_on_conflict(unit, label="collaboration.pump.terminal", fresh=True)


def _reject(ctx, turn, binding, *, retry_allowed=False):
	from jarvis.chat import pump

	uncertain = binding and frappe.db.get_value(c.BINDING, binding.name, "state") == "Uncertain"
	frappe.db.rollback()

	def cancel():
		frappe.db.get_value(c.CONVERSATION, turn["conversation"], "name", for_update=True)
		if uncertain:
			frappe.db.set_value(c.BINDING, binding.name, "state", "Uncertain")
		won = pump._cancel_ready_turn(turn)
		if won:
			frappe.db.set_value(
				ts.TURN, turn["run_id"], "collaboration_retry_allowed", int(retry_allowed and not uncertain)
			)
		if won and turn.get("assistant_message"):
			frappe.db.set_value(
				"Jarvis Chat Message",
				turn["assistant_message"],
				"error",
				"This shared request could not start because access or the isolated session changed. Your message is kept.",
			)
		# Persist the refused dispatch and release locks before publishing its error.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return won

	if txn.replay_on_conflict(cancel, label="collaboration.dispatch_refused", fresh=True) and binding:
		ts.publish_fenced(
			binding.actor,
			"run:error",
			conversation_id=binding.conversation,
			run_id=turn["run_id"],
			message_id=turn.get("assistant_message"),
			code="permission",
			error="Shared access or the isolated session changed. Check the request before retrying.",
			changed_data=False,
		)
	ctx.shared_preflights.pop(turn["run_id"], None)
	return False


def dispatch_one(ctx, turn):
	from jarvis.chat import pump

	run_id, binding = turn["run_id"], None
	try:
		c.require_writes_ready()
		binding = binding_for_run(run_id)
		if not binding:
			c._deny()
		payload = json.loads(turn.get("dispatch_payload") or "{}")
		runtime = payload.get("collaboration_runtime") or {}
		expected = RuntimePolicy(runtime.get("skills_root") or "", runtime.get("model_ref") or "")
		physical = runtime.get("physical_id")
		if (
			payload.get("session_key") != binding.session_key
			or payload.get("chat_user") != binding.actor
			or not physical
		):
			c._deny()
		pending = ctx.shared_preflights.get(run_id)
		if not pending or pending["version"] != int(turn["version"]):
			fut = ctx.mux.issue_rpc(
				"sessions.patch",
				{"key": binding.session_key, "expectedSessionId": physical},
				timeout_s=pump.ACK_TIMEOUT_S,
			)
			ctx.shared_preflights[run_id] = {
				"fut": fut,
				"version": int(turn["version"]),
				"deadline": pump._monotonic() + pump.ACK_TIMEOUT_S,
			}
			return False
		if not pending["fut"].done and pump._monotonic() < pending["deadline"]:
			return False
		response = pending["fut"].result(0)
		ctx.shared_preflights.pop(run_id, None)
		verify_response(binding, expected, physical, response)
		require_prompt_budget(
			payload.get("message") or "",
			attachments=payload.get("attachments") or [],
			context_tokens=runtime.get("context_tokens"),
			runtime_reserve=runtime.get("runtime_reserve"),
		)
		ok, _reason = policy.validate_can_send(binding.actor, model=expected.model_ref.split("/", 1)[1])
		if not ok:
			c._deny()

		def claim():
			require_live(binding)
			if not ts.confirm_dispatching(run_id, int(turn["version"]), ctx.epoch, ctx.relay_target_id):
				frappe.db.rollback()
				return False
			frappe.db.set_value(
				ts.TURN,
				run_id,
				"collaboration_send_started_at",
				frappe.utils.now_datetime(),
				update_modified=False,
			)
			frappe.db.commit()
			return True

		if not txn.replay_on_conflict(claim, label="collaboration.dispatch", fresh=True):
			if pump._shard_epoch_lost(ctx):
				ts.lease_lost_exit(run_id)
			return False
	except (
		frappe.PermissionError,
		frappe.ValidationError,
		AgentUnreachableError,
		ValueError,
		TypeError,
		KeyError,
	) as exc:
		return _reject(
			ctx,
			turn,
			binding,
			retry_allowed=isinstance(exc, AgentUnreachableError)
			and exc.code not in ("INVALID_REQUEST", SITE_IS_COPY_CODE),
		)

	rs = pump._RunState(
		run_id=run_id,
		conversation=binding.conversation,
		owner=binding.actor,
		assistant_message=turn.get("assistant_message"),
		session_key=binding.session_key,
		version=int(turn["version"]) + 1,
		send_key=run_id,
		accept_ms=pump._dt_to_ms(turn.get("enqueued_at")),
	)
	ctx.runs[run_id] = rs
	ctx.peak_occupancy = max(ctx.peak_occupancy, len(ctx.runs))
	ts.publish_fenced(
		binding.actor,
		"run:start",
		conversation_id=binding.conversation,
		run_id=run_id,
		message_id=rs.assistant_message,
		pump_epoch=ctx.epoch,
		relay_target_id=ctx.relay_target_id,
	)
	try:
		fut = ctx.mux.send_chat(
			binding.session_key,
			payload["message"],
			run_id,
			pump._make_handler(ctx, rs),
			timeout_s=pump.ACK_TIMEOUT_S,
			start_seq=int(turn.get("last_event_seq") or 0),
			attachments=payload.get("attachments"),
			verified_session_id=physical,
		)
	except AgentUnreachableError as exc:
		pump._handle_ack_failure(ctx, rs, exc)
		return False
	ctx.pending_acks[run_id] = pump._PendingAck(
		fut=fut,
		deadline=pump._monotonic() + pump.ACK_TIMEOUT_S,
		rs=rs,
		drained_note_ids=payload.get("drained_note_ids") or [],
	)
	return True
