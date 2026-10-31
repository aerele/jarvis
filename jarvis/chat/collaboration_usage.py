"""At-most-once usage for an actual run of the immutable initiating actor."""

import time

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import txn, usage


class UsagePending(frappe.ValidationError):
	"""The previous run's usage is not reconciled; this child has not sent."""


def record(session_key: str, row, run_id: str | None, *, reply_chars=None):
	if not isinstance(row, dict) or not run_id:
		return usage.USAGE_RETRY
	# This owns one complete accrual transaction. It must never discard another
	# caller's writes or commit an unfinished effect alongside its counters.
	if not txn.replay_is_safe():
		return usage.USAGE_RETRY
	try:
		binding = c.resolve_identity(session_key)
	except frappe.PermissionError:
		return usage.USAGE_RETRY

	def unit():
		conv = frappe.db.get_value(c.CONVERSATION, binding.conversation, "owner", for_update=True)
		if not conv:
			return usage.USAGE_RETRY
		frappe.db.get_values(
			"User", {"name": ["in", sorted({conv, binding.actor})]}, "name", for_update=True, order_by="name"
		)
		metadata = frappe.db.get_value(
			c.BINDING,
			binding.name,
			["actor", "conversation", "runtime_session_id", "runtime_policy_hash"],
			as_dict=True,
			for_update=True,
		)
		auth = frappe.db.get_value(
			c.SESSION,
			{"session_key": session_key},
			["user", "collaboration_binding"],
			as_dict=True,
			for_update=True,
		)
		turn = frappe.db.get_value(
			"Jarvis Chat Turn",
			run_id,
			[
				"conversation",
				"actor",
				"collaboration_binding",
				"collaboration_send_started_at",
				"gateway_run_id",
				"usage_recorded",
				"collaboration_usage_unavailable",
				"state",
				"terminal_kind",
				"assistant_message",
			],
			as_dict=True,
			for_update=True,
		)
		if (
			not metadata
			or not auth
			or not turn
			or (metadata.actor, metadata.conversation) != (binding.actor, binding.conversation)
			or (auth.user, auth.collaboration_binding) != (binding.actor, binding.name)
			or (turn.conversation, turn.actor, turn.collaboration_binding)
			!= (binding.conversation, binding.actor, binding.name)
			or not turn.collaboration_send_started_at
			or turn.state not in {"terminal_observed", "finalizing", "done", "errored", "cancelled"}
			or turn.terminal_kind not in {"relay:final", "relay:error"}
		):
			return usage.USAGE_RETRY
		if turn.usage_recorded or turn.collaboration_usage_unavailable:
			return usage.USAGE_VALID_ZERO
		if (
			row.get("key") != session_key
			or not metadata.runtime_session_id
			or not metadata.runtime_policy_hash
			or row.get("sessionId") != metadata.runtime_session_id
			or row.get("lastRunId") != (turn.gateway_run_id or run_id)
			or not row.get("totalTokensFresh")
			or row.get("hasActiveRun") is not False
			or (row.get("inputTokens") is None and row.get("outputTokens") is None)
		):
			return usage.USAGE_RETRY
		# The guard and the existing aggregate/per-model/per-turn writes commit
		# together. A second finalizer or a continuation's preflight cannot bill
		# the same gateway counters again.
		if turn.assistant_message:
			from jarvis.chat.collaboration_output import lock_reply

			lock_reply(binding, run_id, turn.assistant_message)
			model, provider = usage.resolved_model_identity(row)
			if model:
				frappe.db.set_value(
					"Jarvis Chat Message",
					turn.assistant_message,
					{"model": model[:140], "provider": provider[:140]},
					update_modified=False,
				)
		frappe.db.set_value("Jarvis Chat Turn", run_id, "usage_recorded", 1, update_modified=False)
		outcome = usage._record_turn_usage(session_key, row, run_id, reply_chars)
		if outcome == usage.USAGE_RETRY:
			frappe.db.rollback()
		elif outcome == usage.USAGE_VALID_ZERO:
			# Persist a valid zero-usage disposition so it cannot be lost at job exit.
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return outcome

	return txn.replay_on_conflict(unit, label="collaboration.usage", fresh=True)


def mark_unavailable(binding, run_id):
	"""A force-done meter records an undercount, never a fabricated zero charge."""

	def unit():
		won = mark_unavailable_locked(binding, run_id)
		if won:
			frappe.log_error(
				title="Shared request usage unavailable", message=f"run={run_id} cause=usage_retry_exhausted"
			)
		# Persist exhausted-accounting disposition and its audit as one replayable unit.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return won

	return txn.replay_on_conflict(unit, label="collaboration.usage_unavailable", fresh=True)


def mark_unavailable_locked(binding, run_id):
	"""Repair the disposition within a caller-owned delete/retirement transaction."""
	from jarvis.chat import turn_state as ts

	frappe.db.get_value(c.CONVERSATION, binding.conversation, "name", for_update=True)
	row = frappe.db.get_value(ts.TURN, run_id, ["*"], as_dict=True, for_update=True)
	if not row or (row.actor, row.conversation, row.collaboration_binding) != (
		binding.actor,
		binding.conversation,
		binding.name,
	):
		c._deny()
	effect = frappe.db.get_value(
		ts.EFFECT,
		{"turn": run_id, "effect_name": "usage"},
		["status", "attempts"],
		as_dict=True,
		for_update=True,
	)
	if (
		not row.usage_recorded
		and not row.collaboration_usage_unavailable
		and row.collaboration_send_started_at
		and row.terminal_kind in {"relay:final", "relay:error"}
		and row.state in {"finalizing", "done", "errored", "cancelled"}
		and effect
		and effect.status == "done"
		and int(effect.attempts) >= ts.FINALIZE_MAX_ATTEMPTS
	):
		frappe.db.set_value(ts.TURN, run_id, "collaboration_usage_unavailable", 1, update_modified=False)
		return True
	return False


def capture(binding, run_id: str, *, session=None):
	"""Poll only this binding, outside locks, then atomically accrue its actual child.

	A shared poll deliberately avoids the legacy helper's raw-session diagnostics.
	Only the fixed cause and turn identifier are safe to put into an exportable log.
	"""
	if not txn.replay_is_safe():
		return usage.USAGE_RETRY
	turn = frappe.db.get_value(
		"Jarvis Chat Turn",
		run_id,
		[
			"actor",
			"collaboration_binding",
			"usage_recorded",
			"collaboration_usage_unavailable",
			"state",
			"terminal_kind",
			"collaboration_send_started_at",
		],
		as_dict=True,
	)
	if not turn or (turn.actor, turn.collaboration_binding) != (binding.actor, binding.name):
		return usage.USAGE_RETRY
	if turn.usage_recorded or turn.collaboration_usage_unavailable:
		return usage.USAGE_VALID_ZERO
	if (
		not turn.collaboration_send_started_at
		or turn.state not in {"terminal_observed", "finalizing", "done", "errored", "cancelled"}
		or turn.terminal_kind not in {"relay:final", "relay:error"}
	):
		return usage.USAGE_RETRY
	# Release the read-only actor snapshot before gateway usage I/O.
	# read-only snapshot; never hold actor locks across gateway I/O
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	try:
		if session is None:
			from jarvis.chat import agent_session_pool

			settings = frappe.get_cached_doc("Jarvis Settings")
			with agent_session_pool.checkout(settings.agent_url or "") as checked_out:
				return capture(binding, run_id, session=checked_out)
		row = None
		for attempt in range(3):
			rows = session.list_sessions()
			row = next((r for r in rows if isinstance(r, dict) and r.get("key") == binding.session_key), None)
			if (
				row
				and row.get("totalTokensFresh")
				and (row.get("inputTokens") is not None or row.get("outputTokens") is not None)
			):
				break
			if attempt < 2:
				time.sleep(1.5)
		reply_chars = usage.reply_char_count(run_id) if usage.is_claude_cli_row(row) else None
		return record(binding.session_key, row, run_id, reply_chars=reply_chars)
	except Exception as exc:
		frappe.db.rollback()
		frappe.log_error(title="shared usage", message=f"run={run_id} cause={type(exc).__name__}")
		return usage.USAGE_RETRY


def require_previous_usage(binding, run_id: str, session):
	"""A child may not overwrite the only counters for its preceding actual run."""
	rows = frappe.get_all(
		"Jarvis Chat Turn",
		{
			"collaboration_binding": binding.name,
			"name": ["!=", run_id],
			"collaboration_send_started_at": ["is", "set"],
			"usage_recorded": 0,
			"collaboration_usage_unavailable": 0,
			"terminal_kind": ["in", ["relay:final", "relay:error"]],
			"state": ["in", ["terminal_observed", "finalizing", "done", "errored", "cancelled"]],
		},
		pluck="name",
		limit=2,
	)
	for previous in rows:
		mark_unavailable(binding, previous)
	rows = [
		previous
		for previous in rows
		if not frappe.db.get_value("Jarvis Chat Turn", previous, "collaboration_usage_unavailable")
	]
	if frappe.db.exists(
		"Jarvis Chat Turn",
		{
			"collaboration_binding": binding.name,
			"collaboration_usage_unavailable": 1,
		},
	):
		# Lost accounting does not license overlapping model work. Runtime
		# identity and idleness remain independently mandatory for a child.
		physical = frappe.db.get_value(c.BINDING, binding.name, "runtime_session_id")
		# Release actor reads before verifying native idleness after unavailable usage.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		entry = next(
			(item for item in session.list_sessions() if item.get("key") == binding.session_key), None
		)
		if not entry or entry.get("sessionId") != physical or entry.get("hasActiveRun") is not False:
			frappe.throw("The previous shared request is not safely idle yet. Your message is kept.")
	if not rows:
		return
	if len(rows) > 1 or capture(binding, rows[0], session=session) == usage.USAGE_RETRY:
		frappe.throw(
			"The previous reply's usage is not available yet. Your message is kept; "
			"wait for it to finish before retrying this continuation.",
			UsagePending,
		)
