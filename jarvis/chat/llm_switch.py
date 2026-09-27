"""A proxy <-> direct switch recreates the agent container. Hold it until no
reply is in flight (or a cap), tell every open chat, then apply (jarvis#1425
follow-up, ../../plans/2026-09-27-seamless-llm-switch-design.md Part C).

One switch record lives in the redis cache (key ``KEY``, TTL ``TTL_S`` so a
crashed job can never lock chat forever): ``{started_at, deadline, job,
job_kwargs, applied}``. ``begin()`` stores it and broadcasts once; ``try_apply()``
enqueues the held job once nothing is in flight (or the cap has passed);
``end()`` clears it and lets admission drain what queued during the hold.
``maintenance_notice.boot_payload()`` surfaces it through the existing
maintenance hold (send gate + SPA banner) so there is one hold mechanism, not
two. Routing the actual switches through ``begin()`` (``_enqueue_handover``,
the switching ``_enqueue_pool_sync`` calls, the onboarding pollers) is Task 5.
"""

from __future__ import annotations

import time

import frappe

KEY = "jarvis:llm_switch"
TTL_S = 1200
CAP_S = 300
EVENT = "jarvis:llm_switch"
MESSAGE = "Updating your AI setup. Chat will be back in a moment."

# admission._shard_inflight's legacy (non-Turn-row) leg freshness-gates a streaming
# assistant Message's `modified` at admission._INFLIGHT_FRESH_SECONDS (180s) - tuned
# for admission's OWN purpose (spotting a genuinely abandoned stream so a new send
# isn't queued forever behind a dead one). Traced against turn_handler.py's assistant
# batcher and tool-event handling (2026-09-27, see task-4-report.md): the only writes
# to the assistant row's `modified` during streaming are gated on new TEXT content
# (_AssistantContentBatcher.flush is a no-op unless text is pending); a tool call does
# not touch the row while it runs. A long tool call therefore lets `modified` go stale
# well past 180s while the reply is still genuinely in flight. Reusing admission's
# 180s window here would let the switch apply mid-reply and cut it - the exact failure
# this module exists to prevent - so the switch's own inflight check widens the legacy
# leg to 900s. The asymmetry is deliberate: over-counting a truly-abandoned stream only
# delays the switch until the 5-minute cap; under-counting cuts a live one.
_SWITCH_FRESH_S = 900


def status() -> dict | None:
	"""The active switch record, or None. Fails to None on a redis error - callers
	treat None as "no switch", so a cache blip never strands the send gate or the
	poller behind a hold nothing can lift."""
	try:
		return frappe.cache().get_value(KEY, expires=True)
	except Exception:
		frappe.log_error(title="llm_switch.status failed", message=frappe.get_traceback())
		return None


def is_active() -> bool:
	return status() is not None


def _inflight() -> int:
	"""Dual-signal inflight, mirroring admission._shard_inflight but with the legacy
	leg widened to _SWITCH_FRESH_S (see the module comment). Turn-row `dispatching` is
	state-based, not freshness-based, so it is reused as-is."""
	from jarvis.chat import admission

	target = admission.DEFAULT_RELAY_TARGET
	cutoff = frappe.utils.add_to_date(None, seconds=-_SWITCH_FRESH_S)
	return admission._turn_dispatching_count(target) + admission._legacy_streaming_count(
		target, fresh_cutoff=cutoff
	)


def _broadcast(state: str, **extra) -> None:
	"""Tell every open chat at once. No user= -> frappe sends it to the site room
	"all" (every connected socket), matching the spec's "tell everyone" flow. Never
	raises into a caller that is trying to start or end a switch."""
	try:
		frappe.publish_realtime(EVENT, {"state": state, **extra})
	except Exception:
		frappe.log_error(title="llm_switch broadcast failed", message=frappe.get_traceback())


def begin(job: str, **kwargs) -> None:
	"""Hold a proxy switch until no reply is in flight (or the cap), then apply.

	The actual work (store the record, broadcast once, try_apply) runs AFTER the
	caller's transaction commits, mirroring enqueue_after_commit's own rule by hand
	(the work here is a cache write + a realtime publish, not an enqueue) - so a save
	that later rolls back never raises the banner for a switch that never happened.
	In tests (or under run_admin_sync_inline) it runs immediately, same inline rule
	the enqueue helpers use, so assertions don't have to poll or commit.

	IDEMPOTENT: a switch already active (the handover's pool fallback, a reconcile
	re-drive) is re-targeted to the new job with NO second broadcast. If the active
	switch had already applied, the container is already restarting for the OLD job
	and there is nothing left to drain, so the retargeted job is applied right away;
	otherwise the retarget just changes what the pending apply will run."""
	run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
	if run_inline:
		_begin_now(job, kwargs)
	else:
		frappe.db.after_commit.add(lambda: _begin_now(job, kwargs))


def _begin_now(job: str, kwargs: dict) -> None:
	rec = status()
	if rec is None:
		rec = {
			"started_at": time.time(),
			"deadline": time.time() + CAP_S,
			"job": job,
			"job_kwargs": kwargs,
			"applied": False,
		}
		frappe.cache().set_value(KEY, rec, expires_in_sec=TTL_S)
		_broadcast("switching")
		try_apply()
		return

	was_applied = bool(rec.get("applied"))
	rec = {**rec, "job": job, "job_kwargs": kwargs, "applied": False}
	frappe.cache().set_value(KEY, rec, expires_in_sec=TTL_S)
	try_apply(force=was_applied)


def try_apply(*, force: bool = False) -> bool:
	"""Enqueue the held job when nothing is in flight (or the cap has passed), or
	unconditionally when force=True (an idempotent retarget of an ALREADY-applied
	switch - see begin()). Under a redis lock (a cache compare-and-set alone is not
	atomic); the enqueued job's own job_id + deduplicate=True is the backstop against
	a double-fire that slips past the lock. Safe to call from anywhere, any number of
	times - a no-op when there is no active switch or it already applied."""
	from jarvis._redis_lock import redis_lock

	with redis_lock("jarvis_llm_switch") as acquired:
		if not acquired:
			return False
		rec = status()
		if rec is None or rec.get("applied"):
			return False
		if not force:
			past_deadline = time.time() >= rec.get("deadline", 0)
			if not past_deadline and _inflight() > 0:
				return False
		rec = {**rec, "applied": True}
		frappe.cache().set_value(KEY, rec, expires_in_sec=TTL_S)
		_enqueue(rec["job"], rec["job_kwargs"])
		return True


def _enqueue(job: str, kwargs: dict) -> None:
	"""Mirror JarvisSettings._enqueue_pool_sync / _enqueue_handover's enqueue rules
	exactly (queue, timeout, inline-in-tests, enqueue_after_commit) and re-stamp
	last_sync_requested_at - readiness' 15-minute applying window and the handover
	attempt-freshness check both key off it, and 15 min covers the 5-minute cap plus
	a ~4-minute apply. ``job`` and any job_id/dedupe kwargs are the caller's
	(begin()'s caller, Task 5) - this helper only supplies the shared envelope.

	frappe._dict() (not frappe.get_single) is enough here: _write_settings_fields
	only needs something to call .update() on, and the real write is the
	frappe.db.set_single_value it already does - fetching the Single doc just to
	discard it would be a pure-read get_single with no method call on it."""
	from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
		ADMIN_SYNC_RQ_TIMEOUT_S,
		_write_settings_fields,
	)

	_write_settings_fields(frappe._dict(), {"last_sync_requested_at": frappe.utils.now()})
	run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
	frappe.enqueue(
		job,
		queue="long",
		timeout=ADMIN_SYNC_RQ_TIMEOUT_S,
		enqueue_after_commit=not run_inline,
		now=run_inline,
		**kwargs,
	)


def end(outcome: str) -> None:
	"""Clear the record, tell every open chat it is over, and let admission drain
	what queued during the hold. A no-op when there is no active record so a double
	poll (a settle callback racing the poller) never spams a second "done" broadcast
	or an idle promote_next."""
	if status() is None:
		return
	frappe.cache().delete_value(KEY)
	_broadcast("done", outcome=outcome)
	from jarvis.chat import admission

	try:
		admission.promote_next()
	except Exception:
		frappe.log_error(title="llm_switch.end promote_next failed", message=frappe.get_traceback())


def reconcile() -> None:
	"""Poller hook (onboarding.get_llm_sync_status / reconcile_pending_llm_sync,
	wired in Task 5): drive the held switch forward, then end it once the apply it
	enqueued has reached a terminal status.

	Precondition this relies on: the caller stamps a "pending: ..." last_sync_status
	BEFORE begin() enqueues (every existing sync helper already does this), so seeing
	"ok"/"failed:" here means THIS switch's apply landed, not a status left over from
	before the switch started."""
	try_apply()
	rec = status()
	if not rec or not rec.get("applied"):
		return
	last_status = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status") or ""
	if last_status.startswith("ok") or last_status.startswith("failed:"):
		end(last_status)
