"""Confirm a container push whose answer timed out, instead of reporting it failed.

The skills/agents pushes (custom, learned, agent bundles) are full reconciles that
restart the container when something changed. admin serves them inside a web request,
so its read of the fleet agent is cut to the request limit (about 105 s), while a
2026.11 container takes longer to stop and come back. The push then lands but the
bench hears a timeout and showed "failed", with the catalog still dirty.

A re-send of the same payload is cheap when the first one landed: the fleet agent
restarts only on a real difference and otherwise hot-applies. So an unconfirmed push
is retried once, after the gateway answers again, and only a second unconfirmed
answer is reported as a failure.
"""

from __future__ import annotations

import time

import frappe

from jarvis.exceptions import AdminAmbiguousError, AdminUnreachableError

CONFIRMING = "pending: confirming (the assistant is still restarting)"
WAIT_FOR_AGENT_S = 300
_POLL_S = 10
# The confirm job waits for the gateway, then sends a push that may restart it again.
CONFIRM_JOB_TIMEOUT_S = WAIT_FOR_AGENT_S + 240


def is_unconfirmed(error: Exception) -> bool:
	"""True when the push may have landed: the bench's own call may still be running
	(``AdminAmbiguousError``), or admin answered a gateway error saying its call to the
	fleet agent timed out."""
	if isinstance(error, AdminAmbiguousError):
		return True
	if not isinstance(error, AdminUnreachableError):
		return False
	return getattr(error, "status_code", None) in (502, 504) and "timed out" in str(error).lower()


def wait_for_agent(timeout_s: int = WAIT_FOR_AGENT_S, poll_s: int = _POLL_S, *, sleep=time.sleep) -> bool:
	"""Wait until the assistant answers the paired device's connect handshake.
	True when it did within ``timeout_s``; never raises."""
	from jarvis import agent_ws

	url = (frappe.db.get_single_value("Jarvis Settings", "agent_url") or "").strip()
	if not url:
		return False
	deadline = time.monotonic() + timeout_s
	while True:
		try:
			agent_ws.ping(url)
			return True
		except Exception:
			if time.monotonic() >= deadline:
				return False
			sleep(poll_s)


def enqueue_confirm(method: str, job_id: str, **kwargs) -> None:
	"""Queue the one confirm attempt of ``method`` (called with ``confirm=1``)."""
	run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
	frappe.enqueue(
		method,
		queue="long",
		timeout=CONFIRM_JOB_TIMEOUT_S,
		enqueue_after_commit=not run_inline,
		now=run_inline,
		job_id=f"{job_id}:confirm",
		deduplicate=True,
		confirm=1,
		**kwargs,
	)
