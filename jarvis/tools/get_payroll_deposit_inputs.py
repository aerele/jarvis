"""Return the active payroll Deposit-Status run's sealed private inputs."""

import frappe

from jarvis.chat import payroll_deposit_snapshot
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _delegate_capability
from jarvis.tools._agent_run_ctx import get_session_key
from jarvis.tools._private_output import require_source_transport, source_envelope


def get_payroll_deposit_inputs() -> dict:
	"""Capture exactly one fixed, permission-bounded source snapshot.

	The API has already authenticated the private transport. This function repeats
	the run and capability checks because a direct Python dispatcher must not bypass
	the contract.
	"""
	require_source_transport("get_payroll_deposit_inputs")
	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError("Payroll deposit evidence requires a delegate run session.")
	cap = _delegate_capability.resolve(session_key)
	if not _private_output_allowed(cap):
		raise InvalidArgumentError("Payroll deposit evidence requires its active private delegate run.")
	# The query implementation reads as the current Frappe user. Bind that user
	# back to the session row before any source query, so a direct dispatcher
	# cannot borrow a valid run bearer under Administrator or another user.
	session_user = frappe.db.get_value("Jarvis Chat Session", {"session_key": session_key}, "user")
	if not session_user or session_user != frappe.session.user or cap.get("run_as") != session_user:
		raise InvalidArgumentError("Payroll deposit evidence must run as its session user.")
	run = frappe.db.get_value(
		"Jarvis Agent Run",
		{"session_key": session_key},
		[
			"name",
			"session_key",
			"agent",
			"status",
			"assessment_config_json",
			"input_snapshot_json",
			"scope_json",
		],
		as_dict=True,
		for_update=True,
	)
	if (
		not isinstance(run, dict)
		or run.get("name") != cap.get("run")
		or run.get("session_key") != session_key
		or run.get("agent") != payroll_deposit_snapshot.AGENT
		or run.get("status") != "running"
	):
		raise InvalidArgumentError("Payroll deposit evidence requires its active private delegate run.")
	return source_envelope(payroll_deposit_snapshot.capture(frappe._dict(run)))


def _private_output_allowed(cap) -> bool:
	return (
		isinstance(cap, dict)
		and cap.get("run")
		and cap.get("agent") == payroll_deposit_snapshot.AGENT
		and cap.get("status") == "running"
		and not cap.get("legacy")
	)
