"""Mode-independent backstop for shared jobs; unknown sends are never replayed."""

import json

import frappe

from jarvis.chat import admission, pump, settlement, txn
from jarvis.chat import collaboration as c
from jarvis.chat import turn_state as ts
from jarvis.chat.collaboration_output import lock_reply
from jarvis.chat.collaboration_runtime import RuntimePolicy, verify_dispatched_session
from jarvis.chat.collaboration_turn import binding_for_run
from jarvis.chat.scrub import scrub_for_share

RECOVERY_BUDGET_S = 900
_UNKNOWN = "The result of this shared request is unknown. Check any changes before retrying."
_FIELDS = [
	"name",
	"conversation",
	"actor",
	"collaboration_binding",
	"collaboration_executor",
	"state",
	"reserved",
	"version",
	"pump_epoch",
	"seed_message",
	"assistant_message",
	"relay_target_id",
	"enqueued_at",
	"preparing_at",
	"ready_at",
	"reservation_expires_at",
	"deadline_at",
	"collaboration_send_started_at",
	"recovery_started_at",
	"gateway_run_id",
	"dispatch_payload",
	"terminal_kind",
	"terminal_payload",
	"cancel_requested",
]


def _old(value, seconds):
	return bool(value) and frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), value) > seconds


def _current(run_id):
	return frappe.db.get_value(ts.TURN, run_id, _FIELDS, as_dict=True, for_update=True)


def _same(current, observed, binding):
	return bool(
		current
		and (
			current.conversation,
			current.actor,
			current.collaboration_binding,
			current.collaboration_executor,
			current.state,
			int(current.version),
			int(current.pump_epoch or 0),
		)
		== (
			binding.conversation,
			binding.actor,
			binding.name,
			observed.collaboration_executor,
			observed.state,
			int(observed.version),
			int(observed.pump_epoch or 0),
		)
	)


def _mirror(binding, row, fields):
	if row.assistant_message:
		lock_reply(binding, row.name, row.assistant_message)
		frappe.db.set_value(ts.MSG, row.assistant_message, fields, update_modified=False)


def _expire_unsent(binding, observed):
	def unit():
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		row = _current(observed.name)
		if not _same(row, observed, binding) or row.collaboration_send_started_at:
			frappe.db.rollback()
			return False
		# Include the actual timeout in the write predicate. A live worker's
		# preparation/send claim and this expiry cannot both win.
		won = ts._run_cas(
			f"""UPDATE `tab{ts.TURN}` SET state='cancelled', reserved=0,
			reservation_expires_at=NULL, done_at=%(now)s, version=version+1,
			cancel_reason='The shared request did not start. Your message is kept.'
			WHERE name=%(r)s AND state=%(s)s AND version=%(v)s
			AND collaboration_send_started_at IS NULL
			AND ((preparing_at IS NOT NULL AND preparing_at < %(cut)s)
			OR (preparing_at IS NULL AND reservation_expires_at < %(now)s)
			OR (state='ready' AND ready_at < %(cut)s))""",
			{
				"r": row.name,
				"s": row.state,
				"v": int(row.version),
				"now": frappe.utils.now_datetime(),
				"cut": frappe.utils.add_to_date(None, seconds=-ts.PREPARE_DEADLINE_S),
			},
		)
		if won and row.assistant_message:
			message = frappe.db.get_value(
				ts.MSG,
				row.assistant_message,
				["conversation", "actor", "collaboration_binding", "collaboration_run_id", "role"],
				as_dict=True,
				for_update=True,
			)
			if message and tuple(
				message.get(k)
				for k in ("conversation", "actor", "collaboration_binding", "collaboration_run_id", "role")
			) == (binding.conversation, binding.actor, binding.name, row.name, "assistant"):
				frappe.db.set_value(ts.MSG, row.assistant_message, {"streaming": 0, "stopped": 1})
		# Persist the unsent expiry and matching reply disposition before returning.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return bool(won)

	return txn.replay_on_conflict(unit, label="collaboration.expire_unsent", fresh=True)


def _expire_unknown(binding, observed):
	def unit():
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		row = _current(observed.name)
		if not _same(row, observed, binding) or not _old(row.recovery_started_at, RECOVERY_BUDGET_S):
			frappe.db.rollback()
			return False
		if not ts.recover_errored(row.name, int(row.version), _UNKNOWN):
			frappe.db.rollback()
			return False
		# Capability survives for actual receipts; new dispatch is fenced off.
		frappe.db.set_value(c.BINDING, binding.name, "state", "Uncertain")
		try:
			_mirror(binding, row, {"streaming": 0, "recovering": 0, "error": _UNKNOWN})
		except frappe.PermissionError:
			# An inconsistent reply pointer is audit evidence, never permission
			# to overwrite the pointed-to row. Retain the honest Turn outcome.
			pass
		# Persist the unknown outcome and uncertainty fence; never replay a possible send.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return True

	return txn.replay_on_conflict(unit, label="collaboration.expire_unknown", fresh=True)


def _reclaim_prepare_reservation(binding, observed):
	"""Retry only an unclaimed pump prepare, never a claimed/possibly sent run."""

	def unit():
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		row = _current(observed.name)
		if not _same(row, observed, binding):
			frappe.db.rollback()
			return False
		won = ts._run_cas(
			f"""UPDATE `tab{ts.TURN}` SET reserved=0, reservation_expires_at=NULL, version=version+1
			WHERE name=%(r)s AND state='queued' AND version=%(v)s
			AND collaboration_executor='pump' AND reserved=1 AND reservation_expires_at < %(now)s
			AND preparing_at IS NULL AND collaboration_send_started_at IS NULL
			AND COALESCE(assistant_message,'')=''""",
			{"r": row.name, "v": int(row.version), "now": frappe.utils.now_datetime()},
		)
		# Persist the expired preparation reservation CAS before promotion resumes.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return bool(won)

	return txn.replay_on_conflict(unit, label="collaboration.prepare_reservation", fresh=True)


def _recover_result(binding, observed):
	from jarvis.chat import agent_session_pool, runtime_profile, turn_recovery

	payload = json.loads(observed.dispatch_payload or "{}")
	runtime = payload.get("collaboration_runtime") or {}
	watermark = payload.get("shared_history_watermark")
	if type(watermark) is not int or watermark < 0:
		return False
	policy = RuntimePolicy(runtime.get("skills_root") or "", runtime.get("model_ref") or "")
	settings = frappe.get_cached_doc("Jarvis Settings")
	# Release the recovery read snapshot before gateway verification RPCs.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	with agent_session_pool.checkout(settings.agent_url or "") as sess:
		physical = verify_dispatched_session(sess, binding, policy)
		# Release identity-verification reads before polling native session metadata.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		metadata = next(
			(item for item in sess.list_sessions(timeout_s=5) if item.get("key") == binding.session_key), None
		)
		if not metadata or (
			metadata.get("sessionId") != physical
			or metadata.get("lastRunId") != (observed.gateway_run_id or observed.name)
			or metadata.get("hasActiveRun") is not False
		):
			return False
		messages = sess.get_session_messages(binding.session_key, limit=100, timeout_s=5)
		key = runtime_profile.get_profile().message_metadata_key
		messages = [
			item
			for item in messages
			if isinstance(item, dict)
			and isinstance(item.get(key), dict)
			and type(item[key].get("seq")) is int
			and item[key]["seq"] > watermark
		]
		text = turn_recovery._latest_assistant_text(messages, min_seq=watermark)
		if not text:
			return False
		verify_dispatched_session(sess, binding, policy)
		# End native-verification reads before the independent terminal-observation CAS.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
	terminal = {"text": scrub_for_share(text)}
	latest = max(
		(item for item in messages if item.get("role") == "assistant"),
		key=lambda item: item[key]["seq"],
		default={},
	)
	kind = "relay:final"
	if observed.cancel_requested and latest.get("stopReason") == "aborted":
		kind = "relay:error"
		terminal["state"] = "aborted"

	def unit():
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		row = _current(observed.name)
		if not _same(row, observed, binding):
			frappe.db.rollback()
			return None
		lock_reply(binding, row.name, row.assistant_message)
		won = ts.recover_to_terminal_observed(
			row.name, int(row.version), int(row.pump_epoch or 0), kind, terminal
		)
		if not won:
			frappe.db.rollback()
			return None
		frappe.db.set_value(ts.MSG, row.assistant_message, "recovering", 0, update_modified=False)
		# Persist recovered terminal evidence before invoking settlement effects.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return int(row.version) + 1

	v = txn.replay_on_conflict(unit, label="collaboration.recovered_result", fresh=True)
	if v is None:
		return False
	settlement.invoke_settlement(
		observed.name,
		relay_target_id=observed.relay_target_id,
		epoch=int(observed.pump_epoch or 0),
		version=v,
		terminal_kind=kind,
		terminal_payload=terminal,
		assistant_message=observed.assistant_message,
		owner=binding.actor,
		conversation=binding.conversation,
		deps=pump._default_deps(),
	)
	return True


def _park_deadline(binding, observed):
	def unit():
		frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
		row = _current(observed.name)
		if not _same(row, observed, binding):
			frappe.db.rollback()
			return False
		won = ts.mark_recovering(
			row.name, int(row.version), epoch=int(row.pump_epoch or 0), require_deadline_passed=True
		)
		if won:
			try:
				_mirror(binding, row, {"recovering": 1})
			except frappe.PermissionError:
				frappe.db.set_value(c.BINDING, binding.name, "state", "Uncertain")
		# Persist the deadline recovery CAS and uncertainty fence for the next worker.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return won

	return txn.replay_on_conflict(unit, label="collaboration.deadline", fresh=True)


def sweep(limit: int = 10, *, conversation: str | None = None):
	"""Scheduler/job-only, independent of rollout and transport kill switches."""
	if not frappe.db.has_column(ts.TURN, "collaboration_recovery_checked_at"):
		return {"changed": 0}
	condition = " AND conversation=%(conversation)s" if conversation else ""
	params = {
		"conversation": conversation,
		"limit": max(1, min(50, int(limit))),
		"now": frappe.utils.now_datetime(),
		"prepare_cut": frappe.utils.add_to_date(None, seconds=-ts.PREPARE_DEADLINE_S),
		**ts.queued_age_cutoffs(),
	}
	fields = ", ".join(f"`{field}`" for field in _FIELDS)
	# Fresh queued requests and live preparations cannot crowd recovery.
	# Durable scheduling metadata rotates polls of long-lived or broken rows.
	# Only the fixed field list, DocType and optional predicate are formatted; values stay bound.
	rows = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection
		f"""SELECT {fields} FROM `tab{ts.TURN}`
		WHERE collaboration_executor IN ('rq','pump') AND COALESCE(collaboration_binding,'')<>''
		AND (
		 state IN ('recovering','terminal_observed')
		 OR (state='finalizing' AND EXISTS (
		   SELECT 1 FROM `tabJarvis Turn Effect` e WHERE e.turn=`tab{ts.TURN}`.name AND e.status<>'done'))
		 OR (state='queued' AND (
		   enqueued_at < CASE WHEN turn_class='background' THEN %(bcut)s ELSE %(cut)s END
		   OR (collaboration_executor='pump' AND reserved=1 AND reservation_expires_at < %(now)s
		       AND preparing_at IS NULL AND collaboration_send_started_at IS NULL AND COALESCE(assistant_message,'')='')))
		 OR (state IN ('preparing','ready','dispatching') AND collaboration_send_started_at IS NULL
		   AND (preparing_at < %(prepare_cut)s OR (preparing_at IS NULL AND reservation_expires_at < %(now)s)
		        OR (state='ready' AND ready_at < %(prepare_cut)s)))
		 OR (state IN ('dispatching','streaming') AND collaboration_send_started_at IS NOT NULL AND deadline_at < %(now)s)
		) {condition}
		ORDER BY COALESCE(collaboration_recovery_checked_at,'1970-01-01'), enqueued_at, name LIMIT %(limit)s""",
		params,
		as_dict=True,
	)
	changed, targets = 0, set()
	for row in rows:
		binding = None
		try:
			# This hint never grants ownership; actual mutations below still
			# prove their state/version and immutable identity.
			ts._run_cas(
				f"""UPDATE `tab{ts.TURN}` SET collaboration_recovery_checked_at=%(now)s
				WHERE name=%(name)s AND version=%(version)s""",
				{"now": frappe.utils.now_datetime(), "name": row.name, "version": int(row.version)},
			)
			frappe.db.commit()
			binding = binding_for_run(row.name)
			if not binding:
				continue
			targets.add(row.relay_target_id)
			if row.state == "queued":
				if row.collaboration_executor == "pump" and row.reserved:
					changed += int(_reclaim_prepare_reservation(binding, row))
					continue
				if ts.cancel_queued_max_age(
					row.name, int(row.version), "The shared request waited too long. Your message is kept."
				):
					frappe.db.commit()
					changed += 1
				continue
			if row.state == "finalizing":
				pump._default_enqueue_finalize(row.name, row.relay_target_id)
				continue
			if row.state == "terminal_observed":
				settlement.invoke_settlement(
					row.name,
					relay_target_id=row.relay_target_id,
					epoch=int(row.pump_epoch or 0),
					version=int(row.version),
					terminal_kind=row.terminal_kind,
					terminal_payload=settlement._coerce_payload(row.terminal_payload),
					assistant_message=row.assistant_message,
					owner=binding.actor,
					conversation=binding.conversation,
					deps=pump._default_deps(),
				)
				changed += 1
			elif not row.collaboration_send_started_at:
				changed += int(_expire_unsent(binding, row))
			elif row.state == "recovering":
				expired = _old(row.recovery_started_at, RECOVERY_BUDGET_S)
				try:
					recovered = _recover_result(binding, row)
				except Exception as exc:
					if not expired:
						raise
					frappe.db.rollback()
					from jarvis.chat.collaboration_runtime import preserve_drift

					preserve_drift(binding, exc)
					recovered = False
				changed += int(recovered or (expired and _expire_unknown(binding, row)))
			elif _old(row.deadline_at, 0):
				changed += int(_park_deadline(binding, row))
		except Exception as exc:
			frappe.db.rollback()
			if binding:
				from jarvis.chat.collaboration_runtime import preserve_drift

				preserve_drift(binding, exc)
			frappe.log_error(title="shared recovery", message=f"run={row.name} cause={type(exc).__name__}")
	# A lost wake for fresh work also needs a backstop, independently of the
	# actionable recovery window. Promotion keeps its own mode/flag gates.
	# Only the fixed DocType and optional parameterized conversation predicate are formatted.
	queued = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection
		f"""SELECT DISTINCT relay_target_id FROM `tab{ts.TURN}`
		WHERE collaboration_executor IN ('rq','pump') AND COALESCE(collaboration_binding,'')<>''
		AND state IN ('queued','preparing','ready') {condition} LIMIT 50""",
		params,
	)
	targets.update(item[0] for item in queued)
	from jarvis.chat.collaboration_ownership import wake_queued

	for target in targets:
		wake_queued(target)
	return {"changed": changed}
