"""A proxy <-> direct switch recreates the agent container. Hold it until no
reply is in flight (or a cap), tell every open chat, then apply (jarvis#1425
follow-up, ../../plans/2026-09-27-seamless-llm-switch-design.md Part C).

One switch record lives in the redis cache (key ``KEY``, TTL ``TTL_S`` so a
crashed job can never lock chat forever): ``{started_at, deadline, job,
job_kwargs, applied, run_id, next}``. ``begin()`` stores it and broadcasts
once; ``try_apply()`` enqueues the held job once nothing is in flight (or the
cap has passed); ``end()`` clears it and lets admission drain what queued
during the hold. ``maintenance_notice.boot_payload()`` surfaces it through the
existing maintenance hold (send gate + SPA banner) so there is one hold
mechanism, not two.

2026 review fix wave (Important, "retarget race"): a released job (one
``try_apply()``/``begin()`` has actually enqueued) carries a fresh ``run_id``
(``rec["run_id"]``) passed to it as the ``llm_switch_run_id`` job kwarg and
baked into its ``job_id`` suffix, so two releases can never collide under rq's
job-id dedup (STARTED reads like QUEUED to rq's dedup, so reusing a job_id
across two releases could silently drop the newer one). A retarget that
arrives while the current run is still executing does NOT force a second
enqueue for the same container - it parks the newer job in ``rec["next"]``
(``{"job", "job_kwargs", "pending_status"}`` - latest wins); the running
job's own ``finish(run_id, outcome)`` call releases it as a fresh run once it
reports its outcome. ``finish()`` acts only when the caller's ``run_id``
still matches the active record's - a job that was never released by the
switch (or has since been superseded) must not touch it.

2026 review, second pass (Critical): releasing a parked ``next`` re-stamps
last_sync_status to its own captured ``pending_status`` (the string the
caller stamped when it was parked) - the run being handed off just wrote ITS
OWN terminal status over last_sync_status, and without the re-stamp a
poller's ``reconcile()`` (every held-chat poll runs one, see
``maintenance_notice.check()``) would read that stale terminal status,
match it against the freshly-minted run_id, and wrongly end the switch while
the handed-off job is still queued or running."""

from __future__ import annotations

import time
import uuid

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
	re-drive, an unrelated apply joining an active switch) is re-targeted to the new
	job with NO second broadcast:
	- if the active switch has NOT yet applied (still held), the retarget just
	  changes what the pending apply will run, same as before;
	- if it HAS already applied (its job is running RIGHT NOW - 2026 review fix
	  wave, "retarget race"), the new job is parked in the record's ``next`` slot
	  instead of being force-enqueued alongside the still-running one - the running
	  job's own ``finish()`` call releases ``next`` as a fresh run once it reports
	  its outcome.

	``_begin_now`` (queued here, or run inline) never raises: frappe's
	CallbackManager does not wrap ``after_commit`` callbacks in try/except, so an
	uncaught error here would surface in whatever request or job happens to trigger
	the NEXT commit, whose own write already landed (2026 review round 1)."""
	run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
	if run_inline:
		_begin_now(job, kwargs)
	else:
		frappe.db.after_commit.add(lambda: _begin_now(job, kwargs))


def _begin_now(job: str, kwargs: dict) -> None:
	"""Runs the whole check-then-create-or-retarget-or-park decision under the
	``jarvis_llm_switch`` lock, end to end (2026 review round 1: the previous version
	checked ``status()`` and created the record OUTSIDE any lock, so two concurrent
	``begin()`` calls could both see no active record and both broadcast a
	"switching" banner). A short bounded wait (unlike ``try_apply``'s non-blocking
	acquire, which is called from many places and can just let a LATER trigger drive
	the existing record forward): here, losing the race silently would drop this
	call's own ``job`` on the floor, so it is worth a brief wait for the concurrent
	``begin()``/``try_apply()`` to finish rather than a bare no-op.

	CR-1 (2026 review fix wave): if the lock is still contended past that wait, the
	job is enqueued directly - exactly as a non-switch apply would, with its own
	plain job_id, untracked by the switch - rather than dropped on the floor."""
	try:
		from jarvis._redis_lock import redis_lock

		with redis_lock("jarvis_llm_switch", blocking_timeout_s=2.0) as acquired:
			if not acquired:
				frappe.log_error(
					title="llm_switch.begin lock contended",
					message=f"job={job!r} - a concurrent begin()/try_apply()/finish() held the "
					"lock past the 2s wait; enqueuing directly (untracked by the switch) instead "
					"of dropping it (2026 review fix wave, CR-1).",
				)
				_enqueue_unswitched(job, kwargs)
				return
			rec = status()
			if rec is None:
				rec = {
					"started_at": time.time(),
					"deadline": time.time() + CAP_S,
					"job": job,
					"job_kwargs": kwargs,
					"applied": False,
					"run_id": None,
					"next": None,
				}
				frappe.cache().set_value(KEY, rec, expires_in_sec=TTL_S)
				_broadcast("switching")
				_try_apply_locked(force=False)
				return
			if rec.get("applied"):
				# 2026 review fix wave (Important, "retarget race"): the current
				# run is already executing - a force-enqueue here would run TWO
				# jobs against the same container, and whichever finishes first
				# would end the switch out from under the other. Park the newer
				# config instead; the running job's own finish() (or the next
				# reconcile() poll, once it reaches a terminal status) releases
				# it as a FRESH run. No second banner - the switch is already
				# showing one.
				#
				# 2026 review, second pass (Critical): also capture the
				# "pending: ..." status the CALLER already stamped on Jarvis
				# Settings before reaching begin() (every enqueue path stamps
				# one first) - the currently-running job will overwrite
				# last_sync_status with ITS OWN terminal outcome before this
				# park is ever released, and without re-stamping it back at
				# release time, a poller's reconcile() (maintenance_notice.check()
				# runs on every held-chat poll) would read that STALE terminal
				# status, match it against the NEW run_id finish() just minted,
				# and wrongly end the switch while the handed-off job is still
				# queued or running. The fallback only guards a state that
				# should not occur - every existing enqueue path stamps
				# "pending: ..." synchronously before calling begin().
				pending_status = (
					frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status") or ""
				)
				if not pending_status.startswith("pending"):
					pending_status = "pending: applying"
				rec = {
					**rec,
					"next": {"job": job, "job_kwargs": kwargs, "pending_status": pending_status},
				}
				frappe.cache().set_value(KEY, rec, expires_in_sec=TTL_S)
				return
			rec = {**rec, "job": job, "job_kwargs": kwargs, "next": None}
			frappe.cache().set_value(KEY, rec, expires_in_sec=TTL_S)
			_try_apply_locked(force=False)
	except Exception:
		frappe.log_error(title="llm_switch.begin failed", message=frappe.get_traceback())


def try_apply(*, force: bool = False) -> bool:
	"""Enqueue the held job when nothing is in flight (or the cap has passed), or
	unconditionally when force=True. Under a redis lock (a cache compare-and-set
	alone is not atomic); each release's own fresh run_id (baked into its job_id
	suffix) is the backstop against a double-fire that slips past the lock. Safe to
	call from anywhere, any number of times - a no-op when there is no active switch
	or it already applied.

	Never raises (2026 review round 1): this backs a whitelisted poller
	(``onboarding.get_llm_sync_status`` via ``reconcile()``, Task 5) as well as every
	``settle_turn``/``settle_conversation_dispatching`` call, so a transient redis or
	DB error here must degrade to "didn't apply this time", not a 500."""
	try:
		from jarvis._redis_lock import redis_lock

		with redis_lock("jarvis_llm_switch") as acquired:
			if not acquired:
				return False
			return _try_apply_locked(force=force)
	except Exception:
		frappe.log_error(title="llm_switch.try_apply failed", message=frappe.get_traceback())
		return False


def _try_apply_locked(*, force: bool) -> bool:
	"""The actual apply decision. Assumes the caller (``try_apply()`` or
	``_begin_now()``) already holds the ``jarvis_llm_switch`` lock - never acquires it
	itself, which would deadlock against the caller's own held lock (the underlying
	redis-py lock is not reentrant)."""
	rec = status()
	if rec is None or rec.get("applied"):
		return False
	if not force:
		past_deadline = time.time() >= rec.get("deadline", 0)
		if not past_deadline and _inflight() > 0:
			return False
	return _release_job_locked(rec["job"], rec["job_kwargs"], rec)


def _release_job_locked(
	job: str, job_kwargs: dict, base_rec: dict, *, pending_status: str | None = None
) -> bool:
	"""Enqueue ``job`` under a FRESH run_id and store the resulting record in one
	write (the run_id is generated BEFORE the enqueue call, not after, so there is
	no window where the record reads ``applied: True`` under a STALE run_id).
	Assumes the caller holds the ``jarvis_llm_switch`` lock. Shared by
	``_try_apply_locked`` (a held switch's first release - ``pending_status`` stays
	``None``, the caller's own pending stamp is still fresh) and ``finish()``
	(handing off a parked ``next`` to a new run - ``pending_status`` is the string
	captured when it was parked, see ``_begin_now``). Returns whether the enqueue
	landed; on failure, records ``applied: False`` so the next ``try_apply()``
	trigger (a settle callback, a poller) retries instead of stranding the switch
	for the full 20-minute TTL."""
	run_id = uuid.uuid4().hex
	rec = {
		**base_rec,
		"job": job,
		"job_kwargs": job_kwargs,
		"applied": True,
		"next": None,
		"run_id": run_id,
	}
	frappe.cache().set_value(KEY, rec, expires_in_sec=TTL_S)
	try:
		_enqueue(job, job_kwargs, run_id, pending_status=pending_status)
	except Exception:
		frappe.cache().set_value(KEY, {**rec, "applied": False}, expires_in_sec=TTL_S)
		frappe.log_error(title="llm_switch._enqueue failed", message=frappe.get_traceback())
		return False
	return True


def _enqueue(job: str, kwargs: dict, run_id: str, *, pending_status: str | None = None) -> None:
	"""Mirror JarvisSettings._enqueue_pool_sync / _enqueue_handover's enqueue rules
	exactly (queue, timeout, inline-in-tests, enqueue_after_commit) and re-stamp
	last_sync_requested_at - readiness' 15-minute applying window and the handover
	attempt-freshness check both key off it, and 15 min covers the 5-minute cap plus
	a ~4-minute apply. ``job`` and any job_id/dedupe kwargs are the caller's
	(begin()'s caller, Task 5) - this helper only supplies the shared envelope, plus
	(2026 review fix wave) the run token: ``run_id`` is passed to the job as the
	``llm_switch_run_id`` kwarg (the three workers accept it and use it to end/hand
	off the switch in their own finally, never touching a switch they were not
	released by), and folded into ``job_id`` (when the caller passed one) so two
	releases can never collide under rq's job-id dedup - rq treats a STARTED job
	like a QUEUED one for dedup purposes, so reusing a job_id across two releases
	could silently drop the newer one.

	``pending_status`` (2026 review, second pass, "stale terminal status on
	hand-off"): set ONLY when releasing a job parked in ``next``. The run this
	hand-off is replacing just wrote its OWN terminal status ("ok ..."/"failed:
	...") over last_sync_status; re-stamping it back to the pending string the
	parked job was stamped with when it parked - in the SAME write as
	last_sync_requested_at, so no extra commit is needed - means a poller's
	reconcile() running right after the hand-off reads "pending: ...", not the
	PRIOR run's stale terminal status, and never wrongly ends the switch while
	the handed-off job is still queued or running.

	frappe._dict() (not frappe.get_single) is enough here: _write_settings_fields
	only needs something to call .update() on, and the real write is the
	frappe.db.set_single_value it already does - fetching the Single doc just to
	discard it would be a pure-read get_single with no method call on it."""
	from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
		ADMIN_SYNC_RQ_TIMEOUT_S,
		_write_settings_fields,
	)

	fields = {"last_sync_requested_at": frappe.utils.now()}
	if pending_status:
		fields["last_sync_status"] = pending_status
	_write_settings_fields(frappe._dict(), fields)
	run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
	enqueue_kwargs = dict(kwargs)
	job_id = enqueue_kwargs.get("job_id")
	if job_id:
		enqueue_kwargs["job_id"] = f"{job_id}:{run_id[:8]}"
	enqueue_kwargs["llm_switch_run_id"] = run_id
	frappe.enqueue(
		job,
		queue="long",
		timeout=ADMIN_SYNC_RQ_TIMEOUT_S,
		enqueue_after_commit=not run_inline,
		now=run_inline,
		**enqueue_kwargs,
	)


def _enqueue_unswitched(job: str, kwargs: dict) -> None:
	"""Enqueue ``job`` exactly as a non-switch apply would (its own plain job_id, no
	run token, no re-stamped ``last_sync_requested_at``) - used only when the
	``jarvis_llm_switch`` lock is still contended past ``begin()``'s short wait
	(CR-1, 2026 review fix wave): dropping the job here would silently discard a
	real config change, so it runs untracked by the switch rather than not running
	at all. Mirrors ``JarvisSettings._switch_or_enqueue``'s own non-switch branch -
	deliberately NOT calling ``_enqueue`` above, which would hand it a run token the
	switch record was never updated to expect."""
	from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import ADMIN_SYNC_RQ_TIMEOUT_S

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
	or an idle promote_next. Called only from ``finish()`` (or directly by tests) -
	callers with a run_id should call ``finish()`` instead, which verifies the
	record is still theirs (and hands off to a parked ``next`` instead of ending)
	before ever reaching here."""
	if status() is None:
		return
	frappe.cache().delete_value(KEY)
	_broadcast("done", outcome=outcome)
	from jarvis.chat import admission

	try:
		admission.promote_next()
	except Exception:
		frappe.log_error(title="llm_switch.end promote_next failed", message=frappe.get_traceback())


def finish(run_id: str | None, outcome: str) -> None:
	"""Called by a released job's worker when it reaches a terminal outcome (2026
	review fix wave, "retarget race"). Acts ONLY when the active record's OWN
	``run_id`` still matches ``run_id`` - a job that was never released by the
	switch (``run_id`` is ``None``) or whose run has since been superseded (a
	retarget while it was running moved the record on to a newer run before this
	call - can't happen with today's single hand-off-at-a-time flow, but the check
	is what makes hand-off safe to add later) must never end or hand off a switch
	it does not own.

	If a NEWER apply was parked while this run was executing (``rec["next"]`` - see
	``_begin_now``'s retarget-while-applied case), releases it as a FRESH run (new
	run_id, ``applied`` stays True, enqueued) instead of ending - no second
	"switching" broadcast, since the switch never stopped being active from the
	caller's point of view. Otherwise ends the switch with ``outcome``.

	Never raises - callers (the three sync workers' own ``finally``) must never let
	a failure here mask their own real exception."""
	if not run_id:
		return
	should_end = False
	try:
		from jarvis._redis_lock import redis_lock

		with redis_lock("jarvis_llm_switch") as acquired:
			if not acquired:
				frappe.log_error(
					title="llm_switch.finish lock contended",
					message=f"run_id={run_id!r} outcome={outcome!r} - could not acquire the lock; "
					"reconcile() will pick this up on the next poll.",
				)
				return
			rec = status()
			if rec is None or rec.get("run_id") != run_id:
				return
			next_apply = rec.get("next")
			if next_apply:
				_release_job_locked(
					next_apply["job"],
					next_apply["job_kwargs"],
					rec,
					pending_status=next_apply.get("pending_status"),
				)
				return
			should_end = True
	except Exception:
		frappe.log_error(title="llm_switch.finish failed", message=frappe.get_traceback())
		return
	if should_end:
		end(outcome)


def reconcile() -> None:
	"""Poller hook (onboarding.get_llm_sync_status / reconcile_pending_llm_sync,
	wired in Task 5): drive the held switch forward, then hand it off (release a
	parked ``next``) or end it once the apply it enqueued has reached a terminal
	status - via ``finish()``, the SAME hand-off logic a worker's own terminal
	``finally`` uses (2026 review fix wave).

	Precondition this relies on: the caller stamps a "pending: ..." last_sync_status
	BEFORE begin() enqueues (every existing sync helper already does this), so seeing
	"ok"/"failed:" here means THIS switch's apply landed, not a status left over from
	before the switch started.

	Never raises (2026 review round 1): this backs ``get_llm_sync_status``, a
	whitelisted endpoint the SPA polls - a transient redis/DB error must degrade to
	"didn't reconcile this poll", not a 500 in front of the user."""
	try:
		try_apply()
		rec = status()
		if not rec or not rec.get("applied"):
			return
		last_status = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status") or ""
		if last_status.startswith("ok") or last_status.startswith("failed:"):
			finish(rec.get("run_id"), last_status)
	except Exception:
		frappe.log_error(title="llm_switch.reconcile failed", message=frappe.get_traceback())


def _reset_for_tests() -> None:
	"""Test-only: clear the switch record. CI finding (2026 review, fourth
	pass): the record lives in redis, which FrappeTestCase's per-test
	transaction rollback does NOT touch - a test that begins a switch
	(directly, or via any Jarvis Settings save that becomes one) and never
	ends/finishes it leaks an ACTIVE switch into every later test in the same
	process. ``maintenance_notice.boot_payload()`` then reports it as an
	active hold - the send gate, the macro run gate, and any Settings save
	that turns into a switch (parking behind the leaked, already-``applied``
	record instead of actually running) all misbehave for whatever remains
	of the redis key's 20-minute TTL. Call from ``setUp`` AND via
	``addCleanup`` in every test class that can begin a switch or save LLM
	config on Jarvis Settings - ``setUp`` matters too, so a leak from an
	OLDER module cannot poison a class that never gets to run its own
	cleanup first."""
	try:
		frappe.cache().delete_value(KEY)
	except Exception:
		frappe.log_error(title="llm_switch._reset_for_tests failed", message=frappe.get_traceback())
