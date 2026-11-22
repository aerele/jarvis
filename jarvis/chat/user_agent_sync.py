"""Push the site's user agents to the runtime (admin ``push_user_agents``).

Every save, delete or switch of an agent asks for ONE deduplicated background
job, which sends the FULL enabled set of the site (fleet removes what is not
listed) and writes the outcome on every row it concerns. The job sends again
while the set keeps changing under it, so a save landing mid-push is not lost.
Admin allows 30 pushes per workspace per hour, so pushes are per job, never per row.

Outcomes, written with ``db.set_value`` so they never re-trigger a sync:

* success: every row Synced; enabled rows get ``synced_at``.
* a retryable refusal (``apply_in_progress``, 429, 502, admin unreachable): the
  changed rows stay Pending with a code and a sentence; Retry sync sends again.
* anything else: the changed rows become Failed with a code and a sentence.

Rows that were already Synced keep that state on a failure: the runtime still
has them as they were.
"""

from __future__ import annotations

import time

import frappe
from frappe.utils import cint, now_datetime

from jarvis.chat import user_agents

DOCTYPE = user_agents.DOCTYPE
JOB_ID = "jarvis_user_agents_sync"
_LOCK = "jarvis:user_agents_sync"
TIMEOUT_S = 900
_MAX_SENDS = 3
_RESEND_WITHIN_S = 60
MAX_BODY = 20000

_BRIEF = """You are "{name}", an agent a Jarvis user built for their own work.{description}

How you work:
- Act only through your tools. Never say you did something a tool did not do.
- Ask before you create, change, submit, cancel or delete anything.
- You act with your owner's own permissions. If a tool refuses, say so plainly.
- Be brief. Use plain language, no jargon.
{shell}

Your owner's instructions:
"""
_NO_SHELL = "- You have no shell and cannot write files."
_SHELL = "- Your shell and files live in your own sandbox. Keep work there."

RETRYABLE = frozenset({"apply_in_progress", "RateLimitExceeded", "FleetError", "admin_unreachable"})
_SENTENCES = {
	"InvalidUserAgents": "The runtime refused this agent's settings. Check its instructions and tools, then save again.",
	"NoRunningTenant": "Your Jarvis workspace is not running right now. Use Retry sync once it is back.",
	"user_agents_disabled": "Custom agents are not switched on for this workspace yet. Ask your Jarvis admin to contact support.",
	"user_agents_unsupported_fleet": "This workspace's host is too old for custom agents. Ask support to update it, then use Retry sync.",
	"user_agents_unsupported_image": "This workspace runs an older Jarvis runtime that cannot host custom agents. Ask support to update it, then use Retry sync.",
	"sandbox_image_unconfigured": "Shell access is not set up on this workspace yet. Turn off Allow shell, or ask support to set it up.",
	"apply_in_progress": "Another change is being applied to your workspace. Use Retry sync in a minute.",
	"RateLimitExceeded": "Too many agent changes in the last hour. Use Retry sync in a few minutes.",
	"FleetError": "Jarvis could not apply this agent to your workspace. Use Retry sync in a minute.",
	"admin_unreachable": "Jarvis could not reach your workspace to apply this agent. Use Retry sync in a minute.",
	"admin_auth": "This site is not connected to Jarvis. Ask your Jarvis admin to reconnect it, then use Retry sync.",
	"runtime_missing": "The runtime did not confirm this agent. Use Retry sync, and contact support if it keeps failing.",
	"unexpected": "Something went wrong while applying this agent. Use Retry sync, and contact support if it keeps failing.",
}


def request_sync() -> None:
	"""Ask for one push after the current transaction commits (inline in tests)."""
	run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
	frappe.enqueue(
		"jarvis.chat.user_agent_sync.run_sync",
		queue="long",
		timeout=TIMEOUT_S,
		enqueue_after_commit=not run_inline,
		now=run_inline,
		job_id=JOB_ID,
		deduplicate=True,
	)


def build_payload() -> list[dict]:
	"""The fleet ``user_agents`` list: every enabled agent of the site."""
	# System context: the sync covers every owner's agents, not the caller's.
	rows = frappe.get_all(
		DOCTYPE,
		filters={"enabled": 1},
		fields=["agent_id", "agent_name", "description", "instructions", "tools_json", "allow_shell"],
		order_by="creation asc",
	)
	return [_entry(row) for row in rows if user_agents.ID_RE.match(row.agent_id or "")]


def body_for(row) -> str:
	"""The agent's AGENTS.md: a short fixed brief plus the owner's instructions."""
	description = (row.get("description") or "").strip()
	brief = _BRIEF.format(
		name=(row.get("agent_name") or "Agent").strip(),
		description=f" You are for: {description}" if description else "",
		shell=_SHELL if cint(row.get("allow_shell")) else _NO_SHELL,
	)
	return (brief + (row.get("instructions") or "").strip())[:MAX_BODY]


def run_sync() -> None:
	"""Background job: push the set, again while it changes, and record the outcome."""
	from jarvis._redis_lock import redis_lock

	with redis_lock(_LOCK, timeout_s=TIMEOUT_S, blocking_timeout_s=60.0) as acquired:
		if not acquired:
			_record_failure("apply_in_progress")
			return
		try:
			payload, result = _send_until_settled()
			_record_success(payload, result)
		except Exception as exc:
			frappe.db.rollback()
			code = classify(exc)
			if code == "unexpected":
				frappe.log_error(title="Jarvis: user agents sync failed", message=frappe.get_traceback())
			_record_failure(code)


def classify(exc: BaseException) -> str:
	"""A stable code for a failed push. Reads admin's machine code, never its prose."""
	from jarvis.exceptions import (
		AdminAuthError,
		AdminRateLimitedError,
		AdminUnreachableError,
		AdminValidationError,
	)

	if isinstance(exc, AdminRateLimitedError):
		return "RateLimitExceeded"
	if isinstance(exc, AdminAuthError):
		return "admin_auth"
	code = str(getattr(exc, "code", "") or "")
	if isinstance(exc, AdminValidationError):
		return code or "InvalidUserAgents"
	if isinstance(exc, AdminUnreachableError):
		return code or "admin_unreachable"
	return "unexpected"


def sentence_for(code: str) -> str:
	return _SENTENCES.get(code) or (
		f"The runtime refused this change ({code}). Use Retry sync, and contact support if it keeps failing."
	)


def _entry(row) -> dict:
	return {
		"id": row.agent_id,
		"body": body_for(row),
		"tools_allow": [user_agents.TOOL_PREFIX + t for t in user_agents.picked_tools(row.tools_json)][
			: user_agents.MAX_TOOLS
		],
		"sandbox": bool(cint(row.allow_shell)),
		"timeout_s": TIMEOUT_S,
	}


def _send_until_settled() -> tuple[list, dict]:
	from jarvis import admin_client

	began = time.monotonic()
	payload = build_payload()
	result: dict = {}
	for sends in range(1, _MAX_SENDS + 1):
		result = admin_client.post_push_user_agents(user_agents=payload) or {}
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- see rows saved during the send
		fresh = build_payload()
		if fresh == payload or sends == _MAX_SENDS or time.monotonic() - began > _RESEND_WITHIN_S:
			return payload, result
		payload = fresh
	return payload, result


def _record_success(payload: list, result: dict) -> None:
	sent = {entry["id"] for entry in payload}
	confirmed = result.get("user_agents") if isinstance(result, dict) else None
	confirmed = set(confirmed) if isinstance(confirmed, list) else sent
	now = now_datetime()
	# System context: the outcome lands on every owner's rows.
	for row in frappe.get_all(DOCTYPE, fields=["name", "agent_id", "enabled"]):
		if row.agent_id in sent and row.agent_id not in confirmed:
			_set(row.name, "Failed", "runtime_missing")
		elif row.agent_id in sent:
			_set(row.name, "Synced", "", synced_at=now)
		elif not cint(row.enabled):
			_set(row.name, "Synced", "")


def _record_failure(code: str) -> None:
	status = "Pending" if code in RETRYABLE else "Failed"
	# System context, as in _record_success. Synced rows are still right on the runtime.
	for name in frappe.get_all(DOCTYPE, filters={"sync_status": ["!=", "Synced"]}, pluck="name"):
		_set(name, status, code)


def _set(name: str, status: str, code: str, **extra) -> None:
	values = {
		"sync_status": status,
		"sync_error": code,
		"sync_error_message": sentence_for(code) if code else "",
		**extra,
	}
	frappe.db.set_value(DOCTYPE, name, values, update_modified=False)
