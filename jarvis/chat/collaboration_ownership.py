"""Executor ownership is separate from actor identity and pump lease epochs."""

import frappe

TURN = "Jarvis Chat Turn"


def executor_clause(executor: str, alias: str = "") -> str:
	"""Static SQL suffix for transport mutations, never capacity accounting.

	During expand-first rollout old private rows remain eligible. A bound row
	with no valid executor is deliberately ineligible for either transport.
	"""
	assert executor in {"rq", "pump"} and alias in {"", "t"}
	if not frappe.db.has_column(TURN, "collaboration_executor"):
		return ""
	prefix = alias + "." if alias else ""
	return (
		f" AND (COALESCE({prefix}collaboration_binding,'')='' OR {prefix}collaboration_executor='{executor}')"
	)


def legacy_clause(alias: str = "") -> str:
	assert alias in {"", "t"}
	if not frappe.db.has_column(TURN, "collaboration_binding"):
		return ""
	prefix = alias + "." if alias else ""
	return f" AND COALESCE({prefix}collaboration_binding,'')=''"


def require_drained_for_mode(target: str, mode: str):
	"""Caller holds the admission shard lock through this check and its flip."""
	current = frappe.db.get_value("Jarvis Relay Pump", target, "transport_mode", for_update=True)
	if current == mode or not frappe.db.has_column(TURN, "collaboration_binding"):
		return
	# A missing RQ job is still a durable live turn, never evidence of a drain.
	rows = frappe.db.get_values(
		TURN,
		{
			"relay_target_id": target,
			"collaboration_binding": ["is", "set"],
			# These states still depend on their transport. Finalizing and
			# recovering retain their executor and have independent job hosts.
			"state": [
				"in",
				["queued", "preparing", "ready", "dispatching", "streaming", "terminal_observed"],
			],
		},
		"name",
		limit=1,
		for_update=True,
	)
	if rows:
		frappe.throw("Finish or reconcile shared requests before changing chat transport.")


def wake_queued(target: str):
	"""After a slot-release commit, preserve private and shared queue progress."""
	from jarvis.chat import admission, pump

	try:
		if pump.pump_configured(from_db=True, target=target):
			pump.ensure_pump(target)
			pump.lpush_wake(target, "settled")
		else:
			admission.promote_next(target)
	except Exception as exc:
		frappe.log_error(title="shared queue wake", message=f"target={target} cause={type(exc).__name__}")
