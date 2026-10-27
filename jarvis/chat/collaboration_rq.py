"""Actor-bound RQ execution; epoch zero is an executor fence, never a lease."""

import json
from types import SimpleNamespace

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import pump, settlement, txn
from jarvis.chat import turn_state as ts
from jarvis.chat.collaboration_bindings import insert_message, require_live
from jarvis.chat.collaboration_output import lock_reply
from jarvis.chat.collaboration_runtime import RuntimePolicy, verify_before_send
from jarvis.chat.collaboration_turn import binding_for_run, turn_context
from jarvis.chat.collaboration_usage import UsagePending
from jarvis.chat.collaboration_workers import build_payload
from jarvis.exceptions import SITE_IS_COPY_CODE, AgentUnreachableError

_FIELDS = [
	"conversation",
	"actor",
	"collaboration_binding",
	"collaboration_executor",
	"seed_message",
	"assistant_message",
	"state",
	"version",
	"pump_epoch",
	"relay_target_id",
	"preparing_at",
	"collaboration_send_started_at",
	"cancel_requested",
]


def _row(run_id, *, lock=False):
	return frappe.db.get_value(ts.TURN, run_id, _FIELDS, as_dict=True, for_update=lock)


def _matches(row, binding):
	return bool(
		row
		and binding
		and (
			row.conversation,
			row.actor,
			row.collaboration_binding,
			row.collaboration_executor,
			int(row.pump_epoch or 0),
		)
		== (binding.conversation, binding.actor, binding.name, "rq", 0)
	)


def _claim(binding, run_id, seed, target):
	ts._lock_shard(target)
	try:
		if frappe.db.get_value(ts.PUMP, target, "transport_mode") != "legacy":
			c._deny()
		require_live(binding)
		row = _row(run_id, lock=True)
		if not _matches(row, binding) or row.seed_message != seed:
			c._deny()
		if row.state != "dispatching" or row.preparing_at or row.collaboration_send_started_at:
			frappe.db.rollback()
			return None
		if row.cancel_requested:
			frappe.db.rollback()
			return None
		reply = insert_message(binding, {"role": "assistant", "content": "", "streaming": 1}, run_id=run_id)
		prepared_at = frappe.utils.now_datetime()
		won = ts._run_cas(
			f"""UPDATE `tab{ts.TURN}` SET preparing_at=%(p)s, assistant_message=%(m)s,
			reservation_expires_at=NULL, version=version+1
			WHERE name=%(r)s AND state='dispatching' AND version=%(v)s
			AND collaboration_executor='rq' AND pump_epoch=0 AND cancel_requested=0
			AND preparing_at IS NULL AND collaboration_send_started_at IS NULL""",
			{"p": prepared_at, "m": reply.name, "r": run_id, "v": int(row.version)},
		)
		if not won:
			frappe.db.rollback()
			return None
		# Persist the exclusive RQ preparation claim and reply before native preparation.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return SimpleNamespace(version=int(row.version) + 1, reply=reply.name, prepared_at=prepared_at)
	finally:
		ts.reset_lock_tracking()


def _claim_send(binding, run_id, target, claim, payload):
	ts._lock_shard(target)
	try:
		if frappe.db.get_value(ts.PUMP, target, "transport_mode") != "legacy":
			c._deny()
		require_live(binding)
		row = _row(run_id, lock=True)
		if not _matches(row, binding) or row.assistant_message != claim.reply:
			c._deny()
		won = ts._run_cas(
			f"""UPDATE `tab{ts.TURN}` SET collaboration_send_started_at=%(now)s,
			dispatch_payload=%(p)s, deadline_at=%(dl)s, version=version+1
			WHERE name=%(r)s AND state='dispatching' AND version=%(v)s
			AND collaboration_executor='rq' AND pump_epoch=0 AND cancel_requested=0
			AND preparing_at=%(prepared)s AND collaboration_send_started_at IS NULL""",
			{
				"now": frappe.utils.now_datetime(),
				"p": json.dumps(payload),
				"r": run_id,
				"dl": frappe.utils.add_to_date(None, seconds=ts.TURN_TIMEOUT_SECONDS),
				"v": claim.version,
				"prepared": claim.prepared_at,
			},
		)
		if not won:
			frappe.db.rollback()
			return False
		# Persist the one-shot send marker before gateway I/O can have an unknown outcome.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return True
	finally:
		ts.reset_lock_tracking()


def _finish_unsent(binding, run_id, claim, *, retry_allowed=False):
	"""Only the unclaimed job or this exact preparation may finish unsent work."""

	def unit():
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		row = _row(run_id, lock=True)
		if not _matches(row, binding) or row.state != "dispatching" or row.collaboration_send_started_at:
			frappe.db.rollback()
			return False
		if row.preparing_at != (claim.prepared_at if claim else None):
			frappe.db.rollback()
			return False
		won = ts._run_cas(
			f"""UPDATE `tab{ts.TURN}` SET state='cancelled', reserved=0,
			reservation_expires_at=NULL, done_at=%(now)s, version=version+1,
			cancel_reason='Shared request did not start; your message is kept.',
			collaboration_retry_allowed=%(retry)s
			WHERE name=%(r)s AND state='dispatching' AND version=%(v)s
			AND collaboration_executor='rq' AND collaboration_send_started_at IS NULL""",
			{
				"now": frappe.utils.now_datetime(),
				"r": run_id,
				"v": int(row.version),
				"retry": int(retry_allowed),
			},
		)
		if won and row.assistant_message:
			frappe.db.set_value(ts.MSG, row.assistant_message, {"streaming": 0, "stopped": 1})
		# Persist the confirmed-unsent finish before releasing the queue slot.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return row.relay_target_id if won else None

	target = txn.replay_on_conflict(unit, label="collaboration.rq.unsent", fresh=True)
	if target:
		from jarvis.chat.collaboration_ownership import wake_queued

		wake_queued(target)
	return bool(target)


def _finish_rejected(binding, run_id, claim):
	"""A matching negative chat.send response proves this attempt was refused.

	Keep the send-attempt stamp as audit. It never grants a replay or authorizes
	model callbacks, and unlike a lost acknowledgement it needs no unknown recovery.
	"""

	def unit():
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		row = _row(run_id, lock=True)
		if not (
			_matches(row, binding)
			and row.state == "dispatching"
			and row.collaboration_send_started_at
			and row.preparing_at == claim.prepared_at
			and row.assistant_message == claim.reply
		):
			frappe.db.rollback()
			return False
		lock_reply(binding, run_id, claim.reply)
		won = ts._run_cas(
			f"""UPDATE `tab{ts.TURN}` SET state='errored', reserved=0,
			reservation_expires_at=NULL, deadline_at=NULL, done_at=%(now)s,
			version=version+1, terminal_kind='rejected', collaboration_retry_allowed=0,
			error='The gateway refused this shared request. Your message is kept.'
			WHERE name=%(run)s AND state='dispatching' AND version=%(version)s
			AND collaboration_executor='rq' AND collaboration_send_started_at IS NOT NULL""",
			{"run": run_id, "version": int(row.version), "now": frappe.utils.now_datetime()},
		)
		if won:
			frappe.db.set_value(
				ts.MSG,
				claim.reply,
				{
					"streaming": 0,
					"recovering": 0,
					"error": "The gateway refused this shared request. Your message is kept.",
				},
			)
		# Persist the definite gateway rejection before waking queued work.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return row.relay_target_id if won else None

	result = txn.replay_on_conflict(unit, label="collaboration.rq.rejected", fresh=True)
	if result:
		from jarvis.chat.collaboration_ownership import wake_queued

		wake_queued(result)
	return bool(result)


def _park(binding, run_id, reply):
	def unit():
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		row = _row(run_id, lock=True)
		if (
			not _matches(row, binding)
			or not row.collaboration_send_started_at
			or row.state not in ("dispatching", "streaming")
		):
			frappe.db.rollback()
			return False
		valid_reply = True
		try:
			lock_reply(binding, run_id, reply)
		except frappe.PermissionError:
			# Preserve audit evidence without touching the inconsistent pointer.
			valid_reply = False
			frappe.db.set_value(c.BINDING, binding.name, "state", "Uncertain")
		won = ts.mark_recovering(run_id, int(row.version), epoch=0)
		if won and valid_reply:
			frappe.db.set_value(ts.MSG, reply, "recovering", 1, update_modified=False)
		# Persist unknown-send recovery state before this worker exits.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return won

	return txn.replay_on_conflict(unit, label="collaboration.rq.park", fresh=True)


def _advance(binding, run_id, reply, states, mutation):
	"""Bounded metadata re-sync; a losing worker never retries chat.send."""

	def unit():
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		row = _row(run_id, lock=True)
		if not _matches(row, binding) or row.state not in states:
			frappe.db.rollback()
			return None
		lock_reply(binding, run_id, reply)
		if not mutation(row):
			frappe.db.rollback()
			return None
		# Persist this fenced event transition before waiting for another gateway event.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return int(row.version) + 1

	return txn.replay_on_conflict(unit, label="collaboration.rq.event", fresh=True)


def handle(payload):
	"""Dedicated job host; a stray pub/sub delivery cannot take the RQ claim."""
	if not txn.owns_transaction():
		return {"ok": False, "reason": "shared_rq_requires_job"}
	run_id, claim, sent, binding = payload["run_id"], None, False, None
	try:
		binding = binding_for_run(run_id)
		row = _row(run_id)
		if not _matches(row, binding) or (row.conversation, row.seed_message) != (
			payload["conversation_id"],
			payload["message_id"],
		):
			return {"ok": False, "reason": "invalid_job_identity"}
		claim = txn.replay_on_conflict(
			lambda: _claim(binding, run_id, row.seed_message, row.relay_target_id),
			label="collaboration.rq.claim",
			fresh=True,
		)
		if not claim:
			_finish_unsent(binding, run_id, None)
			return {"ok": True, "skipped": "already_claimed"}
		with turn_context(binding, run_id):
			prepared = build_payload(binding, run_id, row.seed_message)
		from jarvis.chat import agent_session_pool

		settings = frappe.get_cached_doc("Jarvis Settings")
		with agent_session_pool.checkout(settings.agent_url or "") as sess:
			runtime = prepared["collaboration_runtime"]
			policy = RuntimePolicy(runtime["skills_root"], runtime["model_ref"])
			physical = verify_before_send(sess, binding, policy)
			frappe.db.commit()
			sess.subscribe_session(binding.session_key)
			if not txn.replay_on_conflict(
				lambda: _claim_send(binding, run_id, row.relay_target_id, claim, prepared),
				label="collaboration.rq.send",
				fresh=True,
			):
				_finish_unsent(binding, run_id, claim)
				return {"ok": False, "reason": "send_claim_lost"}
			sent = True
			rs = pump._RunState(
				run_id=run_id,
				conversation=binding.conversation,
				owner=binding.actor,
				assistant_message=claim.reply,
				session_key=binding.session_key,
				version=claim.version + 1,
			)
			ctx = SimpleNamespace(epoch=0, relay_target_id=row.relay_target_id)

			def observe(event):
				with turn_context(binding, run_id):
					pump._default_apply_tool(ctx, rs, event)

			ack = sess.chat_send(
				binding.session_key,
				prepared["message"],
				run_id,
				attachments=prepared.get("attachments"),
				verified_session_id=physical,
				event_observer=observe,
			)
			gateway_run = ack.get("runId") or run_id
			v = _advance(
				binding,
				run_id,
				claim.reply,
				{"dispatching"},
				lambda current: ts.mark_streaming(run_id, int(current.version), 0, gateway_run),
			)
			if v is None:
				return {"ok": True, "skipped": "ack_claim_lost"}
			rs.version = v
			try:
				from jarvis.chat.collaboration_notes import clear_after_ack

				clear_after_ack(binding, run_id, prepared.get("drained_note_ids") or [])
			except Exception as exc:
				frappe.db.rollback()
				frappe.log_error(
					title="shared note acknowledgement", message=f"run={run_id} cause={type(exc).__name__}"
				)
			for event in sess.relay_turn_events(binding.session_key, gateway_run):
				if event.get("kind") == "tool":
					observe(event)
				elif event.get("kind") in {"relay:final", "relay:error"}:
					# No raw terminal metadata/media/native paths enter this boundary.
					terminal = {key: event[key] for key in ("text", "error", "state") if key in event}
					from jarvis.chat.scrub import scrub_for_share

					terminal = {key: scrub_for_share(str(value)) for key, value in terminal.items()}
					v = _advance(
						binding,
						run_id,
						claim.reply,
						{"streaming"},
						lambda current: ts.mark_terminal_observed(
							run_id, int(current.version), 0, event["kind"], terminal
						),
					)
					if v is not None:
						settlement.invoke_settlement(
							run_id,
							relay_target_id=row.relay_target_id,
							epoch=0,
							version=v,
							terminal_kind=event["kind"],
							terminal_payload=terminal,
							assistant_message=claim.reply,
							owner=binding.actor,
							conversation=binding.conversation,
							deps=pump._default_deps(),
						)
					return {"ok": True}
				elif event.get("kind") == "relay:interrupted":
					break
			_park(binding, run_id, claim.reply)
			return {"ok": False, "reason": "delivery_unknown"}
	except Exception as exc:
		frappe.db.rollback()
		rejected = isinstance(exc, AgentUnreachableError) and exc.rejected_method == "chat.send"
		if binding:
			try:
				from jarvis.chat.collaboration_runtime import preserve_drift

				preserve_drift(binding, exc)
				current = _row(run_id)
				sent = sent or bool(current and current.collaboration_send_started_at)
				if sent and claim and rejected:
					_finish_rejected(binding, run_id, claim)
				elif sent and claim:
					_park(binding, run_id, claim.reply)
				elif not sent:
					_finish_unsent(
						binding,
						run_id,
						claim,
						retry_allowed=isinstance(exc, (AgentUnreachableError, UsagePending))
						and getattr(exc, "code", None) != SITE_IS_COPY_CODE,
					)
			except Exception:
				# A DB outage leaves the durable claim for periodic recovery,
				# never a fresh enqueue with an unknown delivery outcome.
				frappe.db.rollback()
		# Never log prompt, callback arguments, credentials or raw gateway output.
		frappe.log_error(title="shared RQ request", message=f"run={run_id} cause={type(exc).__name__}")
		return {
			"ok": False,
			"reason": "request_rejected"
			if rejected
			else "delivery_unknown"
			if sent
			else "request_not_started",
		}
