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
# Self-heal grace (2026 review, third pass, "lost release"): a released job's
# enqueue can be lost after the record already reads applied:True - a rollback
# after enqueue_after_commit registered it (the exact residual this session
# flagged: a worker's own backstop re-raises after the enqueue call, so RQ's
# execute_job rolls back the transaction the after_commit callback was
# registered in), a worker killed before it ever started, or a redis blip on
# the enqueue call itself. Without this, the record is stuck applied:True with
# nothing behind it until the full TTL_S (20 minutes) - the banner and the
# send hold stay up the whole time. RELEASE_GRACE_S bounds how long a
# genuinely in-flight enqueue (rq dequeue latency, a slow worker pool) gets
# before a missing/failed job is treated as lost rather than merely pending.
#
# 90s, not 30 (2026 review, scoped re-review): released_at is stamped when
# _release_job_locked WRITES the record, but the actual rq enqueue only lands
# at commit (enqueue_after_commit=True in production) - a releasing request or
# job slow to commit could still look "lost" to a poller past a tighter grace
# and get healed into a double-fire. The two runs would serialize on the
# admin-sync redis lock and the older run's own finish() would be ignored (its
# run_id no longer matches), so the worst case is one extra apply/restart, not
# corruption - but a genuinely lost release is rare, and 90s plus one poll
# cycle still beats the 20-minute TTL by far, so there is no real cost to
# widening it.
RELEASE_GRACE_S = 90

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


def blocks_stamp() -> bool:
	"""True when a converged-ok stamp must be refused right now (2026 review,
	fourth pass, "awaiting admin"): a switch is HELD (not yet applied) or
	applied but not yet confirmed as pushed. Admin's "Ready" during either of
	those describes the config BEFORE the switch - stamping would strand the
	tenant reading "ok" while it is not what is actually being served.

	Once a released worker calls ``await_admin()`` (admin ACCEPTED the push;
	only convergence confirmation is pending - ``rec["awaiting_admin"]``),
	Ready now genuinely describes THIS config, and a stamp is correct. Every
	poller that guards a converged-ok stamp on this (``onboarding.
	_reconcile_pending_applying``, ``reconcile_pending_llm_sync``, ``account.
	_confirm_apply_via_admin``, ``resync_llm``) must also call ``reconcile()``
	right after a stamp that lands, so the switch ends on it immediately
	rather than waiting for the next tick."""
	rec = status()
	if rec is None:
		return False
	return not (rec.get("applied") and rec.get("awaiting_admin"))


def await_admin(run_id: str | None) -> None:
	"""Called by a switch-released worker that exits normally with admin
	having ACCEPTED the push but not yet confirmed convergence (
	``_PENDING_APPLYING_STATUS`` - see ``jarvis_settings._finish_switch_run``,
	which routes here instead of ``finish()`` for exactly this status). Ending
	the switch here (the old behavior) cleared the banner and the send-hold
	before the container actually finished restarting - a live e2e2 finding
	(direct->proxy: switch released at 91.7s, admin's OWN converge-config job
	then restarted the container at 201s/212s, chat open the whole time).

	Sets ``rec["awaiting_admin"] = True`` + ``rec["awaiting_since"] =
	time.time()`` under the lock, ``run_id``-matched exactly like ``finish()``
	- a stale/superseded run must never touch a switch it no longer owns.
	No-op if ``run_id`` is falsy or does not match the active record. Never
	raises."""
	if not run_id:
		return
	try:
		from jarvis._redis_lock import redis_lock

		with redis_lock("jarvis_llm_switch") as acquired:
			if not acquired:
				return
			rec = status()
			if rec is None or rec.get("run_id") != run_id:
				return
			rec = {**rec, "awaiting_admin": True, "awaiting_since": time.time()}
			frappe.cache().set_value(KEY, rec, expires_in_sec=TTL_S)
	except Exception:
		frappe.log_error(title="llm_switch.await_admin failed", message=frappe.get_traceback())


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


def apply_if_active(source: str = "") -> None:
	"""Poke a held switch forward right after a reply's own terminal write
	commits (jarvis#1425 review, live e2e2, 2026-09-27): every terminal-write
	call site across the chat surfaces - the legacy ``turn_handler`` exit AND
	its error/abandon path, a pump lane's definite pre-ack rejection, the
	Relay Pump's settlement (``chat/settlement.py``, shared by every pump
	terminal - success, error, aborted, reconcile-owed, recovery) and the
	pump's own budget-exhausted recovery-errored path that settles without
	going through it - calls this immediately after ITS OWN commit of
	``streaming=0`` (or the equivalent Turn-leaves-dispatching write), so the
	switch applies the moment nothing is left in flight rather than waiting
	for the next poll (up to 60s on the SPA's maintenance check). ONE cheap
	``is_active()`` redis GET skips the heavier lock-acquiring ``try_apply()``
	on the overwhelming common case (no switch held). Never raises - a reply
	must never fail because this poke did. Idempotent with every other
	trigger (``settle_turn``, ``settle_conversation_dispatching``, another
	call site's own poke on the same terminal) - calling it twice for one
	reply end is harmless.

	``source`` (2026 review, scoped re-review): a short caller-supplied tag
	(e.g. ``"pump.ack_failure"``, ``"settlement.invoke_settlement"``) folded
	into the Error Log title on the rare failure path, so a poke that raised
	says WHERE it was called from without anyone chasing a bare "failed"
	across every call site."""
	try:
		if is_active():
			try_apply()
	except Exception:
		title = "llm_switch.apply_if_active failed"
		if source:
			title = f"{title} ({source})"
		frappe.log_error(title=title, message=frappe.get_traceback())


def _try_apply_locked(*, force: bool) -> bool:
	"""The actual apply decision. Assumes the caller (``try_apply()`` or
	``_begin_now()``) already holds the ``jarvis_llm_switch`` lock - never acquires it
	itself, which would deadlock against the caller's own held lock (the underlying
	redis-py lock is not reentrant).

	A healed record (``_heal_lost_release_locked`` reset ``applied`` to
	``False`` but left ``next`` alone) is released exactly like ``finish()``'s
	own hand-off: if a retarget parked while the now-lost run was executing,
	THAT config releases - never the stale one the lost run was carrying -
	with its own captured ``pending_status`` re-stamped, same reason
	``finish()`` re-stamps it (the lost run's own last write must not read as
	this fresh run's terminal status)."""
	rec = status()
	if rec is None:
		return False
	if rec.get("applied"):
		healed = _heal_lost_release_locked(rec)
		if healed is None:
			return False
		rec = healed
	if not force:
		past_deadline = time.time() >= rec.get("deadline", 0)
		if not past_deadline and _inflight() > 0:
			return False
	next_apply = rec.get("next")
	if next_apply:
		return _release_job_locked(
			next_apply["job"], next_apply["job_kwargs"], rec, pending_status=next_apply.get("pending_status")
		)
	return _release_job_locked(rec["job"], rec["job_kwargs"], rec)


def _rq_job_id(job_kwargs: dict, run_id: str) -> str | None:
	"""The exact (un-site-prefixed) rq job id a release will enqueue under, or
	``None`` when the caller passed no ``job_id`` (an untracked/unswitched
	enqueue never reaches here, but any future switch job without a job_id
	would - self-heal then has nothing to check and stays a no-op, never a
	false positive). Mirrors ``_enqueue``'s own suffixing exactly; kept as one
	shared computation so the two can never drift apart."""
	job_id = job_kwargs.get("job_id")
	return f"{job_id}:{run_id[:8]}" if job_id else None


def _heal_lost_release_locked(rec: dict) -> dict | None:
	"""Self-heal a release whose enqueue never landed (2026 review, third
	pass, "lost release" - see ``RELEASE_GRACE_S``'s module comment). Assumes
	the caller holds the ``jarvis_llm_switch`` lock and that ``rec.get(
	"applied")`` is true. Returns the healed record (``applied: False``) when
	it flips the record, else ``None`` (nothing to do - stay applied).

	Conservative by construction: skipped entirely in-process (inline mode
	has no rq job to check - a fast ``now=True`` call already ran and either
	finished the switch or is genuinely, legitimately pending for its own
	reason); skipped inside the grace window (rq dequeue latency is normal);
	skipped with no ``rq_job_id`` to check at all (every real release stamps
	one - see ``_release_job_locked`` - so this is a defensive "nothing to
	act on" no-op, never a guess that a job is gone); skipped when the job is
	QUEUED/STARTED/anything but missing-or-FAILED (alive, leave it); skipped
	unless ``last_sync_status`` still reads "pending" (a terminal status means
	finish()/end() already own this, or reconcile() will via the
	terminal-status branch - never race that).

	A parked ``next`` (a retarget that arrived while this now-lost run was
	still "executing") is left exactly as-is - ``_try_apply_locked`` releases
	it in preference to the stale job (latest wins, same as ``_begin_now``'s
	own not-yet-applied retarget, and with its captured ``pending_status``
	re-stamped exactly like ``finish()``'s own hand-off), so a config change
	that arrived after the lost release is never silently dropped.

	Skipped entirely when ``rec.get("awaiting_admin")`` (2026 review, fourth
	pass): that worker's rq job finished ON PURPOSE (a normal exit -
	``await_admin()`` is only ever called after the enqueued job returns) -
	"missing from rq" is the EXPECTED state, not evidence of a lost release,
	and self-heal must never re-release a config admin is still converging."""
	if frappe.flags.in_test or frappe.flags.run_admin_sync_inline:
		return None
	if rec.get("awaiting_admin"):
		return None
	released_at = rec.get("released_at")
	if not released_at or (time.time() - released_at) <= RELEASE_GRACE_S:
		return None
	rq_job_id = rec.get("rq_job_id")
	if not rq_job_id:
		return None
	try:
		from frappe.utils.background_jobs import get_job_status

		job_status = get_job_status(rq_job_id)
	except Exception:
		return None  # can't tell right now; never guess a job away
	if job_status is not None:
		value = getattr(job_status, "value", None) or str(job_status)
		if value != "failed":
			return None  # queued/started/finished/... - alive or already settled
	last_status = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status") or ""
	if not last_status.startswith("pending"):
		return None
	frappe.log_error(
		title="llm_switch: healing a lost release",
		message=(
			f"run_id={rec.get('run_id')!r} rq_job_id={rq_job_id!r} "
			f"released_at={released_at!r} last_sync_status={last_status!r} - "
			"the job is missing or failed past the grace window; re-releasing "
			"under a fresh run_id."
		),
	)
	healed = {**rec, "applied": False}
	frappe.cache().set_value(KEY, healed, expires_in_sec=TTL_S)
	return healed


def _release_job_locked(
	job: str, job_kwargs: dict, base_rec: dict, *, pending_status: str | None = None
) -> bool:
	"""Enqueue ``job`` under a FRESH run_id and store the resulting record in one
	write (the run_id is generated BEFORE the enqueue call, not after, so there is
	no window where the record reads ``applied: True`` under a STALE run_id).
	Assumes the caller holds the ``jarvis_llm_switch`` lock. Shared by
	``_try_apply_locked`` (a held switch's first release, or releasing a
	parked ``next`` after a heal - ``pending_status`` stays ``None`` for a
	first release, the caller's own pending stamp is still fresh; it is the
	parked string when releasing a ``next``, same as below) and ``finish()``
	(handing off a parked ``next`` to a new run - ``pending_status`` is the
	string captured when it was parked, see ``_begin_now``). Returns whether
	the enqueue
	landed; on failure, records ``applied: False`` so the next ``try_apply()``
	trigger (a settle callback, a poller) retries instead of stranding the switch
	for the full 20-minute TTL.

	``released_at``/``rq_job_id`` (2026 review, third pass): recorded here, not
	after the enqueue call, so a crash between the two (or the enqueue itself
	losing the race - the exact "lost release" class) still leaves a record
	``_heal_lost_release_locked`` can act on past the grace window."""
	run_id = uuid.uuid4().hex
	rec = {
		**base_rec,
		"job": job,
		"job_kwargs": job_kwargs,
		"applied": True,
		"next": None,
		"run_id": run_id,
		"released_at": time.time(),
		"rq_job_id": _rq_job_id(job_kwargs, run_id),
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
	rq_job_id = _rq_job_id(kwargs, run_id)
	if rq_job_id:
		enqueue_kwargs["job_id"] = rq_job_id
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


#: Ceiling on how long an "awaiting admin" switch (a released worker exited
#: normally with admin having ACCEPTED the push, per ``await_admin()``) waits
#: for admin's own convergence to confirm before this poller gives up and
#: ends the switch anyway with whatever status is currently recorded (2026
#: review, fourth pass). Chat must not stay blocked indefinitely behind an
#: admin apply that is taking unusually long; ``reconcile_pending_llm_sync``
#: (the */5 scheduled safety net) or the next SPA poll finishes the "ok"
#: stamp later, off the SAME converged-ok machinery, once admin does confirm.
AWAIT_ADMIN_MAX_S = 600

#: Throttle for the admin probe ``_reconcile_awaiting_admin`` makes on behalf
#: of every held chat's maintenance poll - AT MOST ONE probe per this window,
#: across every caller (a shared cache key, not per-request), so N open chats
#: polling every few seconds do not turn into N concurrent admin round-trips.
_AWAIT_ADMIN_PROBE_KEY = "jarvis:llm_switch_await_admin_probe"
_AWAIT_ADMIN_PROBE_THROTTLE_S = 10


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

	An "awaiting admin" record (``rec["awaiting_admin"]`` - the released
	worker exited normally with admin having ACCEPTED the push; see
	``await_admin()``) is NOT a terminal ``last_sync_status`` locally, so the
	check above would never fire for it - handled separately by
	``_reconcile_awaiting_admin`` (2026 review, fourth pass), which every held
	chat's 60s maintenance check drives forward on its own throttled probe.

	Never raises (2026 review round 1): this backs ``get_llm_sync_status``, a
	whitelisted endpoint the SPA polls - a transient redis/DB error must degrade to
	"didn't reconcile this poll", not a 500 in front of the user."""
	try:
		try_apply()
		rec = status()
		if not rec or not rec.get("applied"):
			return
		if rec.get("awaiting_admin"):
			_reconcile_awaiting_admin(rec)
			return
		last_status = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status") or ""
		if last_status.startswith("ok") or last_status.startswith("failed:"):
			finish(rec.get("run_id"), last_status)
	except Exception:
		frappe.log_error(title="llm_switch.reconcile failed", message=frappe.get_traceback())


def _reconcile_awaiting_admin(rec: dict) -> None:
	"""Drive an "awaiting admin" switch forward: past ``AWAIT_ADMIN_MAX_S``,
	end it with whatever status is currently recorded (the ceiling - chat
	unblocks; a later poll/`*/5` reconcile finishes the "ok" stamp). Otherwise,
	at most once per ``_AWAIT_ADMIN_PROBE_THROTTLE_S`` across every caller,
	probe admin via the SAME readiness check ``onboarding.
	_reconcile_pending_applying`` uses, stamp converged on Ready, then finish
	the switch with whatever ``last_sync_status`` reads after that (whether
	the stamp landed or not - a lost race means someone else already wrote
	the same "ok", which is just as good a reason to end this run). Never
	raises - called from ``reconcile()``'s own try/except, but the throttle
	check touches redis on its own before that guard, so it gets one too."""
	run_id = rec.get("run_id")
	status_now = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status") or ""
	# Already terminal (2026 review, "avoid the double admin round-trip"): one
	# of the four converged-ok guard sites just stamped from ITS OWN Ready
	# probe (or a failure landed via some other path) and is about to call
	# (or just called) reconcile() right after - no need for a SECOND admin
	# round-trip here to learn what is already on the record.
	if status_now.startswith("ok") or status_now.startswith("failed:"):
		finish(run_id, status_now)
		return
	awaiting_since = rec.get("awaiting_since") or 0
	if time.time() - awaiting_since >= AWAIT_ADMIN_MAX_S:
		finish(run_id, status_now)
		return
	try:
		cache = frappe.cache()
		if cache.get_value(_AWAIT_ADMIN_PROBE_KEY, expires=True):
			return
		cache.set_value(_AWAIT_ADMIN_PROBE_KEY, 1, expires_in_sec=_AWAIT_ADMIN_PROBE_THROTTLE_S)
	except Exception:
		return
	from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
		_admin_chat_readiness,
		_commit_terminal_sync_status,
		_stamp_converged_ok,
	)
	from jarvis.jarvis.pool_serialize import compute_pool_mode

	state, _reason = _admin_chat_readiness()
	if state != "Ready":
		return
	settings = frappe.get_single("Jarvis Settings")
	# 2026 review (standing Frappe rule, "no bare frappe.db.commit()"): this
	# function can run inside account._confirm_apply_via_admin ->
	# is_ready_for_chat on the desk-boot GET, whose own docstring warns a bare
	# commit there would commit whatever else that GET happens to be
	# carrying. _stamp_converged_ok already routes its own write through this
	# SAME gated helper (commits only in a job/migrate context via
	# frappe.local.job; a POST commits on its own at request end; a GET rolls
	# back and the next probe re-confirms - acceptable, since the switch
	# still only ends on admin's own Ready). The call below is a second,
	# idempotent no-op in every context except one this function does not
	# control - it is what makes calling this from ANY caller safe without
	# knowing which context it is.
	_stamp_converged_ok(settings, is_pool=compute_pool_mode(settings))
	_commit_terminal_sync_status()
	status_now = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status") or ""
	finish(run_id, status_now)


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
