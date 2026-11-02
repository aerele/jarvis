"""Server-only recovery for an unusable payroll Deposit private source.

The plugin callback carries no run id, reason, tool name, source rows, or model
output. The signed session bearer is its only selector. This module derives the
one permitted run from that bearer and closes it while its row lock is held.
"""

from __future__ import annotations

import frappe

from jarvis._session import impersonate
from jarvis.permissions import is_valid_unattended_owner
from jarvis.tools import _delegate_capability, _private_output

RUN = "Jarvis Agent Run"
SESSION = "Jarvis Chat Session"
AGENT = "statutory-payroll-deposit-status"
SOURCE_TOOL = "get_payroll_deposit_inputs"
PRIVATE_SOURCE_UNAVAILABLE = "private_source_unavailable"


def recover_private_input(session_key: str, session_user: str) -> bool:
	"""Fail only the signed session's active Deposit run.

	The caller has already verified the bearer, HMAC, nonce, raw body, legacy
	session, user state, and device binding. This method still verifies every
	Deposit-specific fact before it obtains the Run lock. It accepts a callback
	before source capture because a normal-channel rate-limit refusal happens
	before a receipt can exist. The exact source capability and same-session
	locked-row match keep that necessary self-terminalization narrow.

	Returns ``True`` only for the worker that changed a running run to failed.
	All invalid, foreign, repeated, and already-terminal callbacks return False.
	"""
	if not session_key or not is_valid_unattended_owner(session_user):
		return False

	with impersonate(session_user):
		cap = _delegate_capability.resolve(session_key)
	if not _valid_capability(cap, session_user):
		return False

	# MariaDB REPEATABLE READ: resolve above may have opened a read view. Commit
	# before FOR UPDATE so the lock sees a concurrent capture/writeback terminal
	# state, instead of a stale snapshot.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- open the locked current view
	run = frappe.db.get_value(
		RUN,
		cap["run"],
		[
			"name",
			"status",
			"agent",
			"session_key",
			"owner",
			"installation",
			"capability_contract",
			"tools_allow_json",
		],
		as_dict=True,
		for_update=True,
	)
	locked_session_user = frappe.db.get_value(SESSION, {"session_key": session_key}, "user")
	if not _locked_run_matches(run, cap, session_key, session_user, locked_session_user):
		# The request owns this transaction. Release a non-mutating row lock before
		# returning so a later source capture or writeback is never held behind it.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- release rejected row lock
		return False

	from jarvis.chat import agent_scheduler

	return agent_scheduler.fail_locked_run(
		run,
		PRIVATE_SOURCE_UNAVAILABLE,
		detail=PRIVATE_SOURCE_UNAVAILABLE,
	)


def _valid_capability(cap, session_user: str) -> bool:
	"""Accept only an active, non-legacy Deposit source capability."""
	return bool(
		_private_output.has_private_source_capability(cap)
		and cap.get("agent") == AGENT
		and cap.get("run")
		and cap.get("run_as") == session_user
	)


def _locked_run_matches(
	run, cap, session_key: str, session_user: str, locked_session_user: str | None
) -> bool:
	"""Bind the earlier capability read to the row held by this transaction."""
	return bool(
		isinstance(run, dict)
		and run.get("status") == "running"
		and run.get("agent") == AGENT
		and run.get("name") == cap.get("run")
		and run.get("session_key") == session_key
		and locked_session_user == session_user
		and is_valid_unattended_owner(locked_session_user)
		and _locked_source_capability(run)
	)


def _locked_source_capability(run) -> bool:
	"""Recheck the exact source allowance from the row held by this callback."""
	if run.get("capability_contract") != _delegate_capability.CONTRACT_SNAPSHOT:
		return False
	return SOURCE_TOOL in _delegate_capability.bench_tools(
		_delegate_capability._parse_list(run.get("tools_allow_json"))
	)
