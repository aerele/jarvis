"""Scheduled auditor runs — the identity-safe agent scheduler.

An hourly cron (``jarvis.hooks.scheduler_events``) calls
:func:`run_due_agent_audits`, which fires every enabled AUDITOR installation
whose ``next_run_at`` has passed. Modeled on
``jarvis.chat.macro_scheduler.run_due_macros`` but hardened per the adversarial
review:

* **S1 (THE HINGE):** the audit conversation + triggering message row are
  created INSIDE ``frappe.set_user(installation.owner)`` (try/finally that
  restores the user). Frappe scheduler jobs run as Administrator with no
  session; a ``jarvis__*`` call runs as the DB owner of the triggering message
  row, so binding it to Administrator would bypass every DocType permission,
  silently, unattended. A fail-closed guard REFUSES to bind a scheduled audit
  to Administrator / Guest / a disabled user.
* **O1/A14:** a monthly agent-run budget keyed PER INSTALLATION (with a per-tenant
  aggregate ceiling), read from ``Jarvis Settings.agent_run_budget_monthly``, that
  counts manual + scheduled runs together and EXCLUDES failed rows (skip + record
  ``failed`` + notify the owner when over budget), so scans can't drain the
  customer's own subscription. Keyed on the installation, not the owner, because
  ``run_as_user`` decouples the executing identity from the owner.
* **O3:** the turn is dispatched ``background=1`` (unattended), so it never
  jumps ahead of a human's queued question.
* **O4 (#672):** the slot is CLAIMED (``next_run_at`` advanced) under a
  per-installation lock BEFORE the enqueue, so a crash anywhere in the launch
  leaves the slot spent rather than due; a launch that provably created nothing
  hands the claim back, records a ``failed`` run and notifies the owner. The
  missed slot is NOT backfilled (``compute_next_run`` from *now* yields a single
  next future slot).
* **O7:** identical ``(owner, agent, cadence, time)`` due rows are deduped.

``_launch_audit`` is shared with ``agents_api.run_agent_now`` so a manual
trigger takes the EXACT same code path as the scheduler.
"""

import time
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import now_datetime

from jarvis._session import authenticated_user
from jarvis.chat.agent_activity import log_activity
from jarvis.chat.macro_scheduler import compute_next_run
from jarvis.permissions import is_valid_unattended_owner
from jarvis.tools import _delegate_capability

INSTALLATION = "Jarvis Agent Installation"
LISTING = "Jarvis Agent Listing"
RUN = "Jarvis Agent Run"
CONV = "Jarvis Conversation"

# A14: the per-INSTALLATION monthly run budget (manual + scheduled combined; failed
# runs excluded). Read from Jarvis Settings.agent_run_budget_monthly at run time; a
# floor of 31 (a full daily schedule) is enforced so a misconfigured 0 can never
# wedge every scheduled agent for a whole month. Default leaves headroom for a daily
# schedule PLUS ad-hoc manual runs.
DEFAULT_AGENT_RUN_BUDGET_MONTHLY = 62
MIN_AGENT_RUN_BUDGET_MONTHLY = 31

# A8/A15 reaper: a run genuinely stuck ``running`` past this is dead — the fleet
# run worker fails a run on exec/RPC death (A15) and record_agent_run finalizes a
# live one, so this is a pure BACKSTOP. Sits well above the max bundle
# ``timeout_s`` (manifest ceiling 5400s) so it can only ever catch orphans.
#
# It has a SECOND consumer since #672: ``_live_run`` treats a run older than this as
# not-in-flight, precisely because this is the age at which the reaper declares it
# dead. Keep the two answers to "is this run alive?" on one number, or a run becomes
# too old to count as live and too young to be reaped.
STALE_RUN_AFTER_SECONDS = 3 * 3600

# #1061 run poll (jarvis#1058). The reaper above is a 3h BACKSTOP; these are the
# cutoffs of the 5-minute sweep that asks the fleet what actually happened, so a run
# whose delegate died after dispatch is terminalized in minutes with its REAL error
# instead of three hours later as a duration timeout it never hit.
#
# POLL_GRACE_SECONDS — how long a fresh dispatch is left alone. Below this the fleet
# state is still ``queued``/``running`` in the normal case, so polling only costs an
# admin round trip per run per tick.
POLL_GRACE_SECONDS = 120
# WRITEBACK_GRACE_SECONDS — how long the bench waits, AFTER the delegate finished, for
# ``record_agent_run`` to land before calling the run finished-without-results. Measured
# from the FLEET's ``finished_at``, never from the run's own age: a two-hour scribe is
# already far past any grace the instant its fleet state flips to completed, and failing
# it with the writeback seconds in flight would mislabel a success.
WRITEBACK_GRACE_SECONDS = 600
# MISSING_RECORD_FALLBACK_SECONDS — a 404 from the fleet ("unknown agent run") means the
# host has NO record: either the run never landed, or its state file was pruned. Both are
# indistinguishable from here, so a young run is left alone (the state file may not be
# visible yet / the container may be mid-restart) and only an old one is failed.
MISSING_RECORD_FALLBACK_SECONDS = 1800
# The fleet-agent's own 404 text for a run it has no state file for
# (``jarvis_fleet_agent/main.py``: ``NotFoundError(f"unknown agent run {run_id}")``),
# relayed to the bench through admin as an Admin*Error message. Matched as a SUBSTRING,
# the same idiom as the "not an installed delegate" translation in ``_launch_audit`` —
# admin does not re-code this refusal, so the fleet's sentence is the only signal. A
# CROSS-REPO literal: renaming it fleet-side turns every lost run back into a 3h reap.
_FLEET_UNKNOWN_RUN = "unknown agent run"
# The fleet-agent's words for a run that ENDED CLEANLY on the host. Its dispatch worker
# stamps ``done`` when the container-side turn returns (fleet-agent ``main.py``,
# ``_dispatch_agent_run``); ``completed`` is accepted too so a relay or a future fleet
# that normalises the word cannot silently turn every clean exit back into "still in
# flight" (the mistake this constant replaces: the poll originally matched only
# ``completed`` and would never have seen a real ``done``).
_FLEET_FINISHED_STATUSES = ("done", "completed")
# Circuit breaker on the poll's outbound calls. The sweep is SEQUENTIAL and each admin
# call can block for admin_client.DEFAULT_TIMEOUT_S (150s), so with admin down three
# in-flight runs already outlast the 5-minute cron interval and sweeps start stacking.
# Two consecutive transport failures is enough evidence that the RELAY is down rather
# than any one run being odd, so the sweep abandons the rest of the tick.
MAX_CONSECUTIVE_POLL_FAILURES = 2

# #672: TTL on the per-installation DISPATCH lock, which is held across the slot
# claim AND the whole launch, including the admin call (admin_client.DEFAULT_TIMEOUT_S
# is 150s). Comfortably above that, because the TTL is a crash backstop and NOT a
# concurrency budget: expiring it while a launch is still in flight would hand the
# same installation to a second dispatcher, which is the thing this lock exists to
# prevent.
DISPATCH_LOCK_TTL_S = 300


# --------------------------------------------------------------------------- #
# hourly cron
# --------------------------------------------------------------------------- #
def run_due_agent_audits() -> None:
	"""Run every enabled auditor installation whose next_run_at is due. Runs as
	Administrator (the scheduler user); each audit executes as its own owner."""
	now = now_datetime()
	due = frappe.get_all(
		INSTALLATION,
		filters={"enabled": 1, "schedule_enabled": 1, "next_run_at": ["<=", now]},
		fields=[
			"name",
			"owner",
			"run_as_user",
			"agent",
			"schedule_frequency",
			"schedule_time",
			"installable",
			"source_apps_json",
			"activation_state",
		],
	)
	if not due:
		return

	original_user = frappe.session.user
	seen: set = set()  # O7: dedupe identical (owner, agent, cadence, time)
	for row in due:
		# #648: fault-isolate each installation, the agent twin of #472's macro sweep.
		# The per-row try below covers only the DISPATCH; the dedupe, the schedule
		# arithmetic, the identity guards and every _record_failed / _notify_owner sat
		# OUTSIDE it, so anything they raised propagated out of the whole sweep and every
		# installation behind this one silently missed its slot, for good, since the next
		# hourly tick re-reads the same due set and dies on the same row again. The
		# concrete case was a schedule_time the arithmetic could not use, but the
		# guarantee wanted here is structural: no single poisoned row can starve a
		# tenant's agents. The slot is left DUE on this path (a failure never advances
		# the schedule), so the next tick retries it.
		try:
			_sweep_one(row, now, original_user, seen)
		except Exception:
			if frappe.session.user != original_user:
				frappe.set_user(original_user)
			frappe.db.rollback()
			frappe.log_error(
				title=f"jarvis scheduled agent sweep failed: {row.name}",
				message=frappe.get_traceback(),
			)


def _sweep_one(row, now, original_user: str, seen: set) -> None:
	"""Handle ONE due installation. Extracted from the loop so ``run_due_agent_audits``
	can wrap it whole (#648); every ``return`` here was a ``continue`` in the loop it
	came from. ``seen`` is the caller's dedupe set and is mutated in place."""
	from jarvis.chat.agents_api import _user_allowed_for_agent

	key = (row.owner, row.agent, row.schedule_frequency, str(row.schedule_time))
	if key in seen:
		_advance(row, now)
		return
	seen.add(key)

	# R5-J8: never dispatch a scheduled run for a non-installable capability. A
	# reconcile marks an install installable=0 when a min_apps dependency
	# disappeared after install (the row is kept, not deleted); its run has no
	# data. Record why + consume the slot so the cadence does not busy-retry.
	if not frappe.utils.cint(row.installable):
		_record_failed(row, "scheduled audit skipped: capability not installable (app_absent_or_ineligible)")
		_advance(row, now)
		return

	# Only auditor + scribe agents run scheduled scans; an operator install
	# with a schedule set just consumes its slot (it drafts through the board,
	# not on a cron). A scribe's schedule is optional (manual run-now is the
	# primary path) but honoured here when set, so periodic re-learning works.
	listing = frappe.db.get_value(LISTING, row.agent, ["nature", "status"], as_dict=True) or frappe._dict()
	nature = listing.get("nature")
	if nature not in ("Auditor", "Scribe"):
		_advance(row, now)
		return

	# #457: an admin who deprecates a listing must stop its schedules. The push
	# already drops an unpublished agent from the roster, so the container has no
	# such delegate and a dispatched run can only fail three hours later as a
	# mislabelled timeout. ``_launch_audit`` is the authoritative gate, but refuse
	# HERE too and consume the slot: a throw out of _launch_audit lands in the
	# generic except below, which does NOT advance, so the cadence would retry
	# hourly and write an Error Log every time for a state only a human can fix.
	if (listing.get("status") or "") != "Published":
		_record_failed(
			row,
			"scheduled run skipped: this agent is no longer published "
			f"({listing.get('status') or 'unknown'}); uninstall it or ask an admin to republish it",
		)
		_advance(row, now)
		return

	# CX5-5: a scribe writes the LIVE Org wiki with no confirmation gate, so there is
	# no such thing as a shadow scribe run — refuse it here exactly as the manual path
	# does, and consume the slot so the cadence does not busy-retry.
	if nature == "Scribe" and (row.get("activation_state") or "shadow") == "shadow":
		_record_failed(
			row,
			"scheduled run skipped: this agent writes the wiki directly; promote the "
			"installation to live to run it",
		)
		_advance(row, now)
		return

	# CX5-2: a Custom App Learning run may only read the apps an ADMIN authorized.
	# The cron has no human to ask, so it REUSES the durable selection the last
	# manual launch persisted on the installation (``source_apps_json``) — and when
	# there is none (or the selected apps have since been uninstalled) it SKIPS with
	# a recorded reason rather than launching a run that could read the whole bench.
	source_apps = None
	if _is_app_learning(row.agent):
		from jarvis.learning import app_source

		try:
			source_apps = app_source.validate_source_apps(row.get("source_apps_json") or [])
		except ValueError as e:
			_record_failed(
				row,
				f"scheduled app-learning skipped: no valid authorized app selection ({e})",
			)
			_advance(row, now)
			return

	# Phase 1 identity: the audit executes AS the install's run_as_user (its
	# jarvis__* reads are permission-bounded to that user).
	#
	# R1-F3: there is deliberately NO ``or row.owner`` fallback. A blank
	# run_as_user means the install is MISCONFIGURED, not that it should run as
	# the row owner: the owner is an identity ``_validate_run_as_escalation``
	# never litigated, so binding an unattended run to it would execute ERP
	# reads under rights nobody vetted. Refuse, record WHY, and CONSUME the slot
	# — retrying hourly would just relog the same failure 24x/day and can never
	# fix itself; a human has to set a run-as user or disable the install.
	run_as = (row.run_as_user or "").strip()
	if not run_as:
		_record_failed(
			row, "scheduled audit skipped: no run-as user on this install (set one, or disable it)"
		)
		_advance(row, now)
		return

	# S1 fail-closed identity guard — never bind a scheduled audit turn to
	# Administrator / Guest / a disabled RUN-AS user.
	if not _valid_owner(run_as):
		_record_failed(row, "scheduled audit skipped: invalid run-as user (fail-closed guard)")
		_advance(row, now)
		return

	# RBAC: the listing may have been restricted (or the run-as user's roles
	# revoked) AFTER install. Skip, record WHY, and consume the slot — never
	# dispatch a turn for a run-as identity the agent no longer permits
	# (gotcha #8 — the EXECUTING identity is gated, not the triggerer).
	if not _user_allowed_for_agent(row.agent, run_as):
		_record_failed(row, "run-as user's roles no longer permit this agent")
		_advance(row, now)
		return

	# A14 cost cap — per installation + per-tenant aggregate (the subscription is
	# the tenant's). Manual + scheduled counted together; failed rows excluded, so
	# the _record_failed row we write below can never self-perpetuate the cap.
	over, why = _over_run_budget(row.name)
	if over:
		_record_failed(row, why)
		_notify_owner(row.owner, row, reason=why)
		_advance(row, now)
		return

	_dispatch(row, now, run_as=run_as, original_user=original_user, source_apps=source_apps)


def _dispatch(row, now, *, run_as: str, original_user: str, source_apps: list[str] | None) -> None:
	"""Launch this installation's due audit exactly once (#672).

	Dispatch is the one step of the sweep that can be executed twice for a single
	slot, and the duplicate is unrecoverable: a second real audit, billed a second
	time against the A14 budget, for a customer whose only notification said the run
	could not be started. Three things close it and all three are load-bearing.

	1. **The lock.** Dispatch used to be an unlocked check-then-act shared with
	   ``agents_api.run_agent_now``: a manual Run Now landing in the same tick as the
	   cron had both pass their checks and both launch. Both paths now take this
	   per-installation lock, so the check and the act cannot interleave. NOT
	   acquiring it means another dispatcher owns this installation right now, so
	   this row is left EXACTLY as it is: the holder's decision is the authoritative
	   one, and advancing here as well could consume a slot the holder then declines
	   to run.
	2. **The claim.** ``_claim_slot`` writes the durable "this slot is spent" marker
	   BEFORE the launch, so a failed set_value, a killed RQ worker or an OOM leaves
	   the slot consumed rather than due. That is the safe direction: a lost slot
	   resumes at the next cadence, a duplicate audit cannot be taken back. Keying
	   idempotency on the RUN instead does not work: audits finish in minutes and
	   ticks are hourly, so by the next tick the run is ``completed`` and a liveness
	   check sees nothing at all.
	3. **The live-run check**, which stops a SECOND CONCURRENT audit of one
	   installation rather than a second dispatch of one slot. It is bounded by run
	   freshness, never status alone (see ``_live_run``).

	The failure handling then splits on what the launch actually left behind, because
	"it raised" does not mean "nothing ran". ``_launch_audit`` commits its ``running``
	run before it dispatches, so a throw AFTER that commit that leaves the run alive is
	a failure of the bookkeeping, not of the audit, and handing the slot back there is
	exactly how the duplicate was reached. A throw from the dispatch call itself is the
	other case: ``_launch_audit`` has already stamped its own run ``failed``, nothing is
	running, and the slot is genuinely unspent. Reading the run rather than the
	exception is what tells the two apart.

	The fourth case, a dispatch that fails AMBIGUOUSLY (a timeout, or a reset mid-flight,
	where admin may already have started the turn), is #743. ``_launch_audit`` no longer
	records it ``failed``: it leaves the run ``running`` and re-raises AdminAmbiguousError,
	and the explicit branch below keeps the slot CLAIMED rather than handing it back. So no
	fresh run id is minted an hour later, the fleet's run-id idempotency is never bypassed,
	and the 3h stale-run reaper is the single arbiter of the still-running run. Leaving the
	customer with a ``running`` run after a timeout is the accepted cost of never billing a
	second audit for one slot.

	A Redis fault propagates out of the lock instead of dispatching, which is
	deliberate: without the lock there is no exclusivity to be had, and a slot left due
	is recoverable where a duplicate is not.
	"""
	from jarvis import admin_client
	from jarvis._redis_lock import redis_lock

	with redis_lock(_dispatch_lock_name(row.name), timeout_s=DISPATCH_LOCK_TTL_S) as acquired:
		if not acquired:
			return
		if _live_run(row.name):
			# Already auditing: a manual Run Now, or an audit that is genuinely still
			# going. The slot's work is being done, so CONSUME it instead of queueing a
			# second concurrent audit of the same installation. Recorded like every other
			# skip in this sweep, so the customer is never left with a scheduled audit
			# that silently did not appear.
			_record_failed(row, "scheduled run skipped: an audit for this agent was already running")
			_advance(row, now)
			return
		prev = _claim_slot(row, now)
		if prev is None:
			return

		# S1 hinge: mint the run session + create conv/run INSIDE set_user(run_as).
		# Row ownership is reassigned to the human owner inside _launch_audit; only
		# the ERP-read identity is the run-as user.
		try:
			frappe.set_user(run_as)
			inst = frappe.get_doc(INSTALLATION, row.name)
			# initiating_human=None is EXPLICIT (JF-021): a cron run is unattended, so
			# it has no initiating human: the scheduler user (Administrator) is not one.
			_launch_audit(inst, trigger="scheduled", source_apps=source_apps, initiating_human=None)
		except admin_client.AdminAmbiguousError:
			# #743: the dispatch timed out and admin MAY be running this turn.
			# ``_launch_audit`` deliberately left the run ``running``, so the slot must
			# stay CLAIMED: do NOT _unclaim it, do NOT _record_failed, do NOT notify the
			# owner a run could not start (it may have). Keep the claim and let the 3h
			# reaper arbitrate the running run. This is the same net effect as the
			# generic ``_live_run`` branch below would reach for this row, made explicit
			# so the money decision is not read out of a liveness query.
			frappe.set_user(original_user)
			frappe.db.commit()
			return
		except Exception:
			frappe.set_user(original_user)
			frappe.log_error(
				title=f"jarvis scheduled audit failed: {row.name}",
				message=frappe.get_traceback(),
			)
			# Did the audit actually start? Only the launch itself can have written a
			# live run here (the lock is still held and there was none before the
			# claim), and ``_launch_audit`` is atomic up to its own commit, so this
			# answers honestly rather than guessing from the exception.
			if _live_run(row.name):
				# It is running. Recording a ``failed`` run, telling the owner nothing
				# started, and handing the slot back would each be untrue, and the last
				# one is the duplicate dispatch this whole path exists to prevent. Keep
				# the claim; the Error Log above is the operator's signal.
				frappe.db.commit()
				return
			# Nothing durable was created, so this slot really is unspent: hand it back
			# and retry next hour. compute_next_run(from=now) means even a long outage
			# yields ONE next slot, never a backfill storm.
			_unclaim_slot(row, prev)
			_record_failed(row, "scheduled audit enqueue failed; see Error Log")
			_notify_owner(row.owner, row)
		finally:
			if frappe.session.user != original_user:
				frappe.set_user(original_user)


# --------------------------------------------------------------------------- #
# A8 stale-run reaper (backstop) — hooks cron
# --------------------------------------------------------------------------- #
def reap_stale_agent_runs() -> int:
	"""Terminalize agent runs stuck ``running`` past ``STALE_RUN_AFTER_SECONDS`` and
	tear down their orphaned per-run session rows (A8). Backstop only: a healthy run
	finalizes via ``record_agent_run`` and a dead delegate fails itself via the
	fleet worker (A15); this catches the crash that killed both. Returns the count
	terminalized. Runs as Administrator (scheduler); best-effort, never raises out.

	CA-4 (server-owned scribe completion): a Custom App Learning *scribe* delegate
	writes wiki pages via ``record_app_wiki`` (which stamps ``pages_written`` on the
	run) and is MEANT to call ``finish_app_learning_run`` to reach ``completed`` —
	but the model may simply forget. Completion must not depend on model behavior:
	a stuck scribe run whose durable page tally proves it SUCCEEDED is reconciled to
	``completed`` here (not mislabeled ``failed``). Everything else is a genuinely
	dead run and is failed as before.

	CA2-4 (finish-vs-sweep race): the cached status/tally from the bulk scan may be
	STALE — a concurrent ``record_app_wiki``/``finish`` can complete a run between the
	scan and its transition here. Each candidate is therefore re-read under a ROW LOCK
	and transitioned with a COMPARE-AND-SET on ``status='running'``: a row a concurrent
	finish already moved off running is LEFT ALONE, and the per-run session is torn down
	only AFTER the transition is won + committed. That whole locked transition lives in
	``_terminalize_stuck_run``, shared with the #1061 poll so the two sweeps cannot
	drift apart on it."""
	cutoff = now_datetime() - timedelta(seconds=STALE_RUN_AFTER_SECONDS)
	reaped = 0
	for r in _stale_candidates(cutoff):
		try:
			if _terminalize_stuck_run(
				r.name,
				error="run exceeded max duration; reaped by the stale-run sweep (A8 backstop)",
				detail="reaped: run exceeded max duration",
			):
				reaped += 1
		except Exception:
			frappe.log_error(
				title=f"jarvis agent: stale-run reap failed: {r.name}",
				message=frappe.get_traceback(),
			)
	return reaped


def _terminalize_stuck_run(run_name: str, *, error: str, detail: str) -> bool:
	"""Terminalize ONE run that is still ``running`` when it should not be. Returns True
	iff this call is the one that transitioned it.

	Shared by both sweeps that terminalize from the outside — the 3h stale-run reaper
	(A8) and the 5-minute fleet poll (#1061). One copy on purpose: they differ only in
	WHEN they decide a run is stuck and in the sentence they record, never in how the
	transition is made, and two copies of a compare-and-set are two chances to drift.

	Re-reads under a ROW LOCK immediately before deciding. The row lock serializes
	against a concurrent ``record_app_wiki``/``finish`` (both write this row), so the
	status + tally acted on are current, not a possibly-stale scan snapshot; a row a
	concurrent finish already moved off ``running`` is LEFT ALONE (never overwrite a
	real outcome with failed), which is also what makes an operator's ``stopped`` win.

	CA-4 (server-owned scribe completion): a Custom App Learning *scribe* whose durable
	page tally proves it SUCCEEDED is reconciled to ``completed`` rather than mislabelled
	``failed`` — completion must not depend on the model remembering to call finish. This
	ruling belongs to the run's OWN durable evidence, not to the reason the caller decided
	the run was stuck, which is why EVERY outside-in terminalization comes through here:
	pages already written are pages already written, whether the sweep's trigger was the
	3h clock, a fleet-reported failure, or a lost fleet record."""
	from jarvis.chat import agent_runs
	from jarvis.tools.record_app_wiki import reconcile_run_pages

	frappe.db.commit()  # REPEATABLE-READ discipline: FOR UPDATE goes first
	cur = frappe.db.get_value(
		RUN,
		run_name,
		["status", "pages_written", "agent", "session_key", "owner", "installation"],
		as_dict=True,
		for_update=True,
	)
	if not cur or cur.status != "running":
		# A concurrent finish / stop already moved it off running — leave it alone.
		# Release the lock and move on.
		frappe.db.commit()
		return False
	nature = (frappe.db.get_value(LISTING, cur.agent, "nature") or "").strip().title()
	pages = int(cur.pages_written or 0)
	pages_meta = None
	if nature == "Scribe" and pages == 0:
		# CA2-3 fallback: rebuild the tally from page provenance before failing a
		# scribe run whose stored tally reads zero, so real work is never lost.
		pages_meta = reconcile_run_pages(run_name)
		if pages_meta is None:
			# CA3-4: the provenance QUERY failed — the true page count is UNKNOWN.
			# Neither terminalization is safe (completing with 0 would drop real
			# pages; failing would mislabel a success), so leave the run running and
			# retry on the next sweep. Release the row lock and move on.
			frappe.db.commit()
			return False
		pages = pages_meta["count"]
	if nature == "Scribe" and pages > 0:
		# Server-owned terminalization: the pages are already durably written,
		# so this is an honest SUCCESS the model merely never finalized. Complete
		# it with its tally rather than failing real work.
		values = {"status": "completed", "finished_at": frappe.utils.now()}
		if pages_meta is not None:
			# CA3-4: the tally was rebuilt from provenance (the stored one read 0) —
			# PERSIST the reconciled ``pages_written`` + ``pages_json`` in the SAME
			# locked terminalization write, so a run completed off provenance shows
			# its real page count + list in the durable tally + the Runs UI, never 0.
			values["pages_written"] = pages
			values["pages_json"] = frappe.as_json(pages_meta["pages"])[:60000]
		frappe.db.set_value(RUN, run_name, values, update_modified=False)
		frappe.db.commit()  # win + release the row lock BEFORE tearing down the session
		agent_runs.teardown_run_session(cur.session_key)
		log_activity(
			agent=cur.agent,
			agent_title=frappe.db.get_value(LISTING, cur.agent, "title"),
			installation=cur.installation,
			action="run_completed",
			run=run_name,
			detail=f"reconciled to completed: scribe wrote {pages} page(s); finish not called",
			owner=cur.owner,
		)
		frappe.db.commit()
		return True
	# The row lock is already held and the compare-and-set above has already
	# established that the row is still ``running``, so this goes straight to
	# the shared terminalization tail.
	_terminalize_failed(
		run_name,
		agent=cur.agent,
		installation=cur.installation,
		session_key=cur.session_key,
		owner=cur.owner,
		error=error,
		detail=detail,
	)
	return True


def _stale_candidates(cutoff) -> list:
	"""Runs stuck ``running`` past ``cutoff`` — the candidate set of BOTH outside-in
	sweeps (the A8 reaper at 3h, the #1061 poll at 2 minutes; only the cutoff differs).
	Factored so the CA2-4 compare-and-set (re-read under a row lock before
	transitioning) can be exercised against a deliberately-stale snapshot in tests."""
	return frappe.get_all(
		RUN,
		filters={"status": "running", "started_at": ["<", cutoff]},
		fields=[
			"name",
			"session_key",
			"owner",
			"agent",
			"installation",
			"pages_written",
			"started_at",
		],
	)


def _terminalize_failed(
	run_name: str,
	*,
	agent: str | None,
	installation: str | None,
	session_key: str | None,
	owner: str | None,
	error: str,
	detail: str | None = None,
) -> None:
	"""Stamp a run ``failed`` + tear down its bearer + log the activity — the tail
	shared by every path that kills a RUNNING run.

	The caller must already have established that the row is still ``running``:
	the reaper holds the row lock from its compare-and-set, ``fail_run`` takes one.
	Ordering is load-bearing — the status write is committed (releasing the row
	lock) BEFORE the session teardown, so a teardown never runs under the lock."""
	from jarvis.chat import agent_runs

	frappe.db.set_value(
		RUN,
		run_name,
		{
			"status": "failed",
			"finished_at": frappe.utils.now(),
			"error": (error or "")[:140],
		},
		update_modified=False,
	)
	frappe.db.commit()  # win + release the row lock BEFORE tearing down the session
	# A8: the session bearer must not outlive the (now-failed) run.
	agent_runs.teardown_run_session(session_key)
	log_activity(
		agent=agent,
		agent_title=frappe.db.get_value(LISTING, agent, "title") if agent else "",
		installation=installation,
		action="run_failed",
		run=run_name,
		detail=(detail or error or "")[:140],
		owner=owner,
	)
	frappe.db.commit()


def fail_run(run_name: str, error: str, *, detail: str | None = None) -> bool:
	"""Terminalize a RUNNING agent run as ``failed`` with ``error``. Returns True
	iff this call is the one that transitioned it.

	The honest-failure entry point for a run that is provably dead before it can
	finalize itself — today, a delegate whose capability contract authorises no
	tool at all (JF-017), which is refused on every call INCLUDING the
	``record_agent_run`` writeback. Without this the row would sit ``running``
	until the 3h stale-run sweep relabelled it "exceeded max duration", sending
	the customer after a timeout that never happened.

	Same discipline as the sweep: commit first so the ``FOR UPDATE`` opens the
	read view, compare-and-set on ``status='running'`` under the row lock so a
	concurrent finish is never overwritten with ``failed``. Best-effort — a
	failure to record the failure must never take down the caller's response."""
	if not run_name:
		return False
	try:
		frappe.db.commit()  # REPEATABLE-READ discipline: FOR UPDATE goes first
		cur = frappe.db.get_value(
			RUN,
			run_name,
			["status", "agent", "installation", "session_key", "owner"],
			as_dict=True,
			for_update=True,
		)
		if not cur or cur.status != "running":
			frappe.db.commit()  # release the lock; a concurrent finish already won
			return False
		_terminalize_failed(
			run_name,
			agent=cur.agent,
			installation=cur.installation,
			session_key=cur.session_key,
			owner=cur.owner,
			error=error,
			detail=detail,
		)
		return True
	except Exception:
		frappe.log_error(
			title=f"jarvis agent: run failure stamp failed: {run_name}",
			message=frappe.get_traceback(),
		)
		return False


# --------------------------------------------------------------------------- #
# #1061 dispatched-run poll (jarvis#1058) — hooks cron, every 5 minutes
# --------------------------------------------------------------------------- #
def poll_dispatched_runs() -> int:
	"""Ask the fleet what actually happened to each run the bench still believes is
	``running``, and terminalize the ones that are over. Returns the count transitioned.
	Runs as Administrator (scheduler); best-effort, never raises out.

	The bench used to learn a run's outcome from exactly ONE source: the delegate's own
	``record_agent_run`` writeback. A delegate that died after dispatch — a container
	restart, a stale gateway roster (jarvis#1058), an exec failure the fleet recorded but
	nobody read — therefore left the row ``running`` for three hours, blocking every
	further run of that installation behind the #672 liveness guard, before the reaper
	relabelled it "exceeded max duration": a timeout it never hit, and a diagnosis that
	sent the customer looking in the wrong place.

	This closes that loop from the other side. It NEVER touches a row that is not still
	``running``: every terminal write goes through ``fail_run`` /
	``_terminalize_stuck_run``, which re-read under a row lock and compare-and-set on
	``status='running'``. So an operator's ``stop_agent_run``, a real writeback and this
	sweep can race freely — the first to win the lock decides, and a later fleet flip can
	never overwrite a stopped or completed run.

	The reaper stays: it is the backstop for everything this cannot see (admin down, the
	relay itself broken), which is why an unreachable admin here is a SKIP, never a
	failure verdict."""
	from jarvis import admin_client

	now = now_datetime()
	cutoff = now - timedelta(seconds=POLL_GRACE_SECONDS)
	candidates = _stale_candidates(cutoff)
	terminalized = 0
	unreachable = 0
	consecutive = 0
	abandoned = 0
	for i, r in enumerate(candidates):
		# Per-run fault isolation (the health_check 1020 lesson): one poisoned row must
		# never abort the sweep and starve every run behind it.
		try:
			try:
				state = _polled_state(admin_client.get_agent_run_status(r.name))
			except Exception as e:
				# Deliberately caught by MESSAGE, not by class. A relayed fleet 404 arrives
				# as an Admin*Error whose class is shared with genuine transport faults
				# (AdminRejectedError IS an AdminUnreachableError), so branching on the
				# class first would swallow every lost-record case forever.
				if _FLEET_UNKNOWN_RUN not in str(e).lower():
					# Transport / admin fault: skip this run, the reaper backstops it.
					unreachable += 1
					consecutive += 1
					if consecutive >= MAX_CONSECUTIVE_POLL_FAILURES:
						# CIRCUIT BREAKER. Each call can block for admin_client's full
						# DEFAULT_TIMEOUT_S (150s), and this sweep is sequential on a 5-minute
						# cron, so with admin down even three in-flight runs push one tick past
						# the next and the sweeps stack — an outage would spend the scheduler
						# on calls that are all going to time out anyway. Two consecutive
						# transport failures is enough evidence that the relay, not the run, is
						# the problem: abandon the rest of the sweep and let the next tick
						# retry from the top.
						abandoned = len(candidates) - (i + 1)
						break
					continue
				state = {"status": "missing"}
			consecutive = 0  # a readable answer (a relayed 404 included) closes the breaker
			if _reconcile_polled_run(r, state, now):
				terminalized += 1
		except Exception:
			frappe.log_error(
				title=f"jarvis agent: run poll failed: {r.name}",
				message=frappe.get_traceback(),
			)
	if unreachable:
		# ONE line per sweep, not one per run: an admin outage would otherwise write an
		# Error Log row for every in-flight run every five minutes.
		frappe.log_error(
			title="jarvis agent: run poll could not reach admin",
			message=(
				f"{unreachable} dispatched run(s) failed to poll and {abandoned} more were "
				f"left unattempted (breaker opened after {MAX_CONSECUTIVE_POLL_FAILURES} "
				"consecutive transport failures). No run state was changed; the next tick "
				"retries and the stale-run reaper remains the backstop."
			),
		)
	return terminalized


def _polled_state(payload) -> dict:
	"""The fleet's run-state record out of whatever the relay returned.

	``admin_client._do_post`` already peels Frappe's ``message`` wrapper, but admin's OWN
	success envelope survives it: ``api.tenant.agent_run_status`` returns
	``{"ok": True, "data": {<run state>}}``, so the state sits one level further down than
	``admin_client.get_agent_run_status``'s docstring suggests. Reading only the envelope
	would be just as brittle in the other direction, so accept both shapes — a poll that
	misreads the wrapper sees a blank status and silently never terminalizes anything,
	which is the failure mode this whole sweep exists to remove."""
	if not isinstance(payload, dict):
		return {}
	data = payload.get("data")
	if isinstance(data, dict) and ("status" in data or "run_id" in data):
		return data
	return payload


def _reconcile_polled_run(row, state: dict, now) -> bool:
	"""Decide what the fleet's run-state means for a bench row still ``running``, and
	transition it if it is over. Returns True iff this call terminalized it.

	``state`` is the fleet's run-state record relayed verbatim through admin
	(``{run_id, status: queued|running|completed|failed, error, finished_at, ...}``),
	plus the synthetic ``missing`` this module uses for a relayed 404."""
	status = (state.get("status") or "").strip().lower()

	# Every terminal branch below goes through ``_terminalize_stuck_run``, never through
	# ``fail_run`` directly: the CA-4 scribe ruling is keyed on the run's OWN durable page
	# tally, not on why the sweep decided the run was over. Calling fail_run here stamped
	# ``failed`` on a scribe that had already written its pages, while the reaper
	# reconciled the identical row to ``completed`` — the same run reported two different
	# outcomes depending on which sweep reached it first.
	if status == "missing":
		# The host has NO record of this run. Either it never landed, or its state file
		# was pruned. Indistinguishable from here, so age is the only honest tiebreak:
		# a young run may simply not be visible yet (container mid-restart, a state
		# write not yet flushed) and is left alone.
		if _run_age_seconds(row, now) < MISSING_RECORD_FALLBACK_SECONDS:
			return False
		return _terminalize_stuck_run(
			row.name,
			error="the agent host has no record of this run; it never started or its state was lost",
			detail="polled: no run record on the host",
		)

	if status == "failed":
		# The delegate really failed and the fleet knows why. Surface THAT sentence, not
		# a generic one: this is the whole point of polling rather than waiting for the
		# reaper's "exceeded max duration".
		return _terminalize_stuck_run(
			row.name,
			error=_fleet_error_message(state),
			detail="polled: the delegate reported a failure",
		)

	if status not in _FLEET_FINISHED_STATUSES:
		# queued / running / anything unrecognised: still in flight as far as the host is
		# concerned. Leave it alone — an unknown state is never a verdict.
		return False

	# The host can report a clean ``done`` while its OWN result says the turn never
	# really finished - a gateway timeout/abort still writes a "done" run-state (the
	# process exited), with the honest verdict buried in ``result`` instead. Read
	# there BEFORE the writeback grace: waiting it out just delays the same "delegate
	# finished without recording results" non-answer by five more minutes, discarding
	# the real reason (``result.reply``) the gateway already handed us.
	result = state.get("result")
	if isinstance(result, dict):
		gateway_status = result.get("gateway_status")
		aborted = gateway_status not in ("ok", "completed", "", None) or result.get("summary") == "aborted"
		if aborted:
			return _terminalize_stuck_run(
				row.name,
				error=_gateway_abort_error_message(result, gateway_status),
				detail=f"polled: gateway reported {gateway_status}",
			)

	# Finished on the host, still ``running`` on the bench: the writeback is either in
	# flight or it is never coming. Wait out the grace before deciding, measured from the
	# host's own finish time (see WRITEBACK_GRACE_SECONDS).
	if _writeback_grace_age(state, row, now) < WRITEBACK_GRACE_SECONDS:
		return False
	# Past the grace. A scribe whose durable page tally proves it wrote real pages is
	# COMPLETED, not failed — the shared helper owns that ruling, exactly as the reaper
	# gets it.
	return _terminalize_stuck_run(
		row.name,
		error="delegate finished without recording results",
		detail="polled: delegate finished without recording results",
	)


def _gateway_abort_error_message(result: dict, gateway_status) -> str:
	"""The delegate's real abort sentence out of the fleet result's ``reply`` - its
	first line, trimmed to the same 140-char budget every polled error uses - falling
	back to a generic-but-honest sentence naming the gateway status when there is no
	reply to read."""
	reply = result.get("reply")
	if isinstance(reply, str) and reply.strip():
		first_line = reply.strip().splitlines()[0].strip()
		if first_line:
			return first_line[:140]
	return f"the agent turn was aborted by the gateway ({gateway_status})"


def _fleet_error_message(state: dict) -> str:
	"""The delegate's real failure sentence out of the fleet run-state ``error``
	(``{code, message}``), falling back to something honest rather than empty — an
	error field the customer reads as blank is worse than a generic sentence."""
	err = state.get("error")
	if isinstance(err, dict):
		msg = (err.get("message") or "").strip() or (err.get("code") or "").strip()
		if msg:
			return msg[:140]
	elif isinstance(err, str) and err.strip():
		return err.strip()[:140]
	return "the agent host reported this run as failed"


def _run_age_seconds(row, now) -> float:
	"""Seconds since the bench dispatched this run. 0.0 when ``started_at`` is unreadable
	— which reads as "too young to judge", the fail-safe direction for every caller here."""
	try:
		return (now - frappe.utils.get_datetime(row.started_at)).total_seconds()
	except Exception:
		return 0.0


def _writeback_grace_age(state: dict, row, now) -> float:
	"""Seconds since the DELEGATE finished — what the writeback grace must be measured
	from. Anchoring it on the run's own age would fail any run longer than the grace the
	instant its fleet state flipped to completed, with the writeback still seconds in
	flight, which is precisely the long scribe run this sweep must not mislabel.

	The fleet stamps ``finished_at`` as a UNIX epoch float (fleet-agent
	``agent_run.new_run_state``). Falls back to the bench-side run age only when the
	relayed state carries none."""
	finished = state.get("finished_at")
	if finished is not None:
		try:
			delta = time.time() - float(finished)
		except (TypeError, ValueError):
			delta = -1.0
		if delta >= 0:
			return delta
	return _run_age_seconds(row, now)


# --------------------------------------------------------------------------- #
# shared launch (scheduler + manual run_agent_now take the SAME path)
# --------------------------------------------------------------------------- #
def _resolve_initiating_human(trigger: str, initiating_human: str | None) -> str | None:
	"""JF-021 — the validated human to stamp as a run's immutable ``initiating_human``.

	Provenance is PASSED IN, never read from ambient session state. By the time
	``_launch_audit`` runs, BOTH launch paths have already switched the session to the
	installation's ``run_as_user``, so ``frappe.session.user`` is the EXECUTING identity;
	stamping it misattributed every manual run whose triggerer != run-as user, forever
	(the field is immutable once inserted).

	Fail-closed rules:

	* **scheduled** — no human initiates a cron run, so the field stays empty. A caller
	  that supplies one is REFUSED rather than believed (attributing an unattended run
	  to a person is exactly the lie this field exists to prevent).
	* **manual** — the identity must equal the AUTHENTICATED session user of this
	  request (``jarvis._session.authenticated_user`` — the user as of BEFORE any
	  ``impersonate`` switch). A supplied value can therefore only ever CONFIRM what the
	  server already knows; it can never introduce a third identity, so nothing
	  client-reachable can forge attribution. Omitting it resolves to that same
	  authenticated user (the in-process callers that never impersonate).
	* the resolved identity must be a real ENABLED User and never Guest — an
	  unattributable "human" is refused, not stamped.
	"""
	if trigger != "manual":
		if initiating_human:
			frappe.throw(_("A scheduled agent run has no initiating human."))
		return None

	# The pre-impersonation session user: the server's own answer to "who triggered
	# this", derived independently of whatever the caller passed.
	actual = (authenticated_user() or "").strip()
	human = (initiating_human or actual).strip()
	if human != actual:
		frappe.throw(
			_("An agent run can only be attributed to the user who triggered it."),
			frappe.PermissionError,
		)
	if human in ("", "Guest") or not frappe.db.get_value("User", human, "enabled"):
		frappe.throw(_("A manual agent run must be attributable to an enabled user."))
	return human


def _launch_audit(
	inst,
	trigger: str,
	source_apps: list[str] | None = None,
	initiating_human: str | None = None,
) -> dict:
	"""Create the audit conversation + a ``running`` Jarvis Agent Run + enqueue
	the triggering turn. MUST run as the installation owner (the scheduler
	set_user's it; run_agent_now is already the owner). Returns
	``{run, conversation, session_key}``. Identity/budget guards + next_run_at
	advancement are the caller's job.

	``initiating_human`` (JF-021) is the human who triggered a MANUAL run, captured by
	the caller BEFORE it impersonated the run-as user and validated here by
	``_resolve_initiating_human``. The scheduler passes ``None`` — a cron run has no
	initiating human.

	``source_apps`` (CX5-2) is the Custom App Learning run's ADMIN-AUTHORIZED app
	selection, already validated by the caller (``app_source.validate_source_apps``).
	It is stamped SERVER-SIDE into the run's ``scope_json`` and is the ONLY thing
	that authorises the source-read/wiki tools to touch an app — the model can
	neither author nor widen it. Both launch paths (manual + cron) must supply it
	for that agent; ``None`` leaves the run with no authorized apps, i.e. the tools
	refuse everything."""
	listing = frappe.get_doc(LISTING, inst.agent)
	owner = inst.owner
	# Phase 1 identity: the run's ERP-read identity. The caller (scheduler /
	# run_agent_now) has already switched the session to this user, so
	# frappe.session.user == run_as_user here — scope + watermark below are
	# resolved AS the run-as user (permission-bounded).
	#
	# R1-F3: the LAST line of defence, and the reason no ``or owner`` fallback
	# survives anywhere on the run path. ``run_as_user`` is no longer ``reqd`` (a
	# legacy row carrying none must stay DISABLEABLE) and the controller check that
	# replaced it lives in ``validate()``, which frappe skips WHOLESALE under
	# ``flags.ignore_validate`` (``run_before_save_methods`` returns early;
	# ``_validate`` — the ``reqd`` enforcer — is what always runs). So a future
	# seeder/importer adopting that flag could persist an ENABLED install with no
	# executing identity. Refuse to dispatch it rather than falling back to the row
	# OWNER: that identity was never litigated by the escalation guard, so the
	# fallback amounts to a privilege grant nobody reviewed. Thrown BEFORE any row
	# is inserted, so a refused launch leaves no orphan conversation/run.
	run_as_user = (inst.run_as_user or "").strip()
	if not run_as_user:
		frappe.throw(
			_(
				"This agent installation has no run-as user, so there is no identity to run "
				"it as. Set a run-as user on the installation, or disable it."
			)
		)

	# JF-021: validate the launch-time provenance identity BEFORE any row is inserted,
	# for the same reason as the run-as guard above — a refused launch must leave no
	# orphan conversation/run behind. Deliberately ORDERED BEFORE the capability
	# check below: an unauthorized triggerer must be answered with the authorization
	# refusal, never with a bundle-configuration diagnosis (merge ruling, 2026-07-26
	# composition review).
	initiating_human = _resolve_initiating_human(trigger, initiating_human)

	# #457: the listing must still be Published. This is the AUTHORITATIVE status
	# gate — both dispatch paths funnel through here, so no caller can dispatch a
	# delegate the push has stopped advertising. ``listing.status`` used to be
	# checked only at INSTALL time, so an admin flipping a live installed agent to
	# Deprecated changed nothing about its schedule: the next Apply reconciled the
	# roster without that slug, then every cadence dispatched to a container that
	# had no such delegate, the container rejected the unknown agent id, the bench
	# never learned (the completion writeback never fires because the delegate never
	# starts), and the run sat ``running`` for three hours until the stale-run sweep
	# terminalized it as a duration timeout it never hit. Refuse at launch instead,
	# with the real reason and no orphan conversation/run — same discipline as the
	# run-as guard above, and ORDERED AFTER the authorization check for the same
	# reason it precedes the bundle-configuration check below.
	if (listing.status or "") != "Published":
		# #1062 polish: one sentence, one action.
		frappe.throw(
			_(
				"This agent is no longer published ({0}); uninstall it or ask an admin to republish it."
			).format(listing.status or "unknown")
		)

	# JF-017: the run's CAPABILITY CONTRACT. Resolved HERE, before any row exists,
	# because an empty declared surface is not a runnable state: the bench would
	# refuse every call the delegate makes — record_agent_run included — so the run
	# could never finalize itself and would sit "running" until the 3h stale-run
	# sweep relabelled it a timeout it never hit. Refuse the LAUNCH instead, with
	# the real reason, and leave no orphan conversation/run behind (same discipline
	# as the run_as_user guard above). Imported inside the function so the bundled
	# registry stays the one seam tests inject a declared surface at.
	from jarvis.chat.agent_catalog import registry_tools_allow

	declared_tools = registry_tools_allow(listing.agent_slug)
	if not declared_tools:
		frappe.throw(
			_(
				"This agent's bundle declares no tools, so the run would be refused at "
				"every step and could never finish. Reinstall or update the agent, or "
				"contact support: nothing was started."
			)
		)

	# #672: everything from here to the commit below is ONE unit. It used to be a
	# sequence of pending inserts, so a throw anywhere in it (a rejected insert, a
	# duplicate session key, a DB blip) left a ``running`` Jarvis Agent Run PENDING in
	# the transaction, and the caller's own error handling (``_record_failed``, which
	# commits) flushed it. That phantom run counted against the A14 budget, made
	# every liveness check believe an audit was in flight, and was finally relabelled
	# by the 3h stale-run reaper as a duration timeout it never hit. With the
	# savepoint this function either commits a real ``running`` run or leaves nothing
	# at all, which is what lets the callers ask "did it actually start?" and get a
	# true answer. The commit is deliberately OUTSIDE the try: a savepoint does not
	# survive it, so rolling back to one after it could only ever fail.
	savepoint = "jv_launch_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	try:
		# Fresh conversation. ROW ownership is the human owner (reassigned below) so
		# if_owner visibility works; the ERP-read identity is the run-as user.
		# ignore_permissions matches the macro engine.
		conv = frappe.get_doc({"doctype": CONV, "title": f"{listing.title} audit"[:140], "status": "Active"})
		conv.flags.ignore_permissions = True
		conv.insert()

		# PP-5: the run's IMMUTABLE launch-time provenance, stamped once at insert (the
		# controller's _IMMUTABLE_LAUNCH_FIELDS guard refuses any later ORM change):
		#   * bundle_version — a SNAPSHOT of the version this run actually executes, taken
		#     from the installation's installed_version (falling back to the listing) so it
		#     is fixed even though the listing/installation versions are mutable.
		#   * preparation_mode — a snapshot of the installation's activation_state
		#     (shadow|live) at launch, so a run made in shadow is forever attributable as
		#     such even after the install is later promoted.
		#   * initiating_human — the human who triggered a MANUAL run; None for a
		#     scheduled cron run (no human initiated it). Resolved above from the caller's
		#     EXPLICIT argument, never from frappe.session.user: the session here is the
		#     run-as user, which is a different person whenever the triggerer did not run
		#     their own self-mapped install (JF-021).
		bundle_version = inst.installed_version or listing.version or None
		preparation_mode = inst.activation_state or "shadow"

		# Stamped in the same insert and under the same immutability guard — the
		# declared ``tools_allow`` resolved above plus the listing's ``nature``/
		# ``writes`` as they stand RIGHT NOW. From here on the bench authorises this
		# run's tool calls against the snapshot, never against the mutable listing, so
		# neither a listing edit mid-run nor a compromised container can widen what an
		# in-flight run may do.
		capability = _delegate_capability.contract_for_launch(listing, declared_tools)

		run = frappe.get_doc(
			{
				"doctype": RUN,
				"agent": inst.agent,
				"installation": inst.name,
				"trigger": trigger,
				"status": "running",
				"conversation": conv.name,
				"started_at": frappe.utils.now(),
				"bundle_version": bundle_version,
				"preparation_mode": preparation_mode,
				"initiating_human": initiating_human,
				**capability,
			}
		)
		run.flags.ignore_permissions = True
		run.insert()

		# Defensive: hand the row-owned rows to the intended HUMAN owner (mirrors
		# macros.run_macro). When run_as_user != owner the session user here is the
		# run-as user, so this reassignment is what keeps row ownership = owner.
		if owner != frappe.session.user:
			for dt, name in ((CONV, conv.name), (RUN, run.name)):
				frappe.db.set_value(dt, name, "owner", owner, update_modified=False)

		# Phase 1: mint a per-run Jarvis Chat Session bound to the RUN-AS user and
		# stamp it on the Run. This is the row the delegate's jarvis__* calls resolve
		# their identity from (api.py:44-141 → impersonate(run_as_user)). The dispatch
		# itself is stubbed until Phase 2; the session + scope + watermark are the
		# Phase-1 deliverable.
		slug = listing.agent_slug
		# agent session keys are `agent:<agent-id>:<key>` and the gateway resolves
		# the session under that agent-id. The delegate agent id is `agent-<slug>`
		# (fleet-agent compose.agent_delegates), so the id component MUST be the full
		# delegate id, not the bare slug — otherwise the gateway `agent` RPC's
		# agentId (`agent-<slug>`) and the session key's embedded id (`<slug>`)
		# disagree. The bench never parses this shape (it matches the Jarvis Chat
		# Session row verbatim), so aligning it is free on the bench side and correct
		# on the agent side.
		session_key = f"agent:agent-{slug}:{run.name}"
		_mint_run_session(session_key, run_as_user)
		frappe.db.set_value(RUN, run.name, "session_key", session_key, update_modified=False)

		# A6 explicit scope + A17 consistency watermark + A12 permission profile —
		# all best-effort (never abort the launch) and computed AS the run-as user.
		scope = _stamp_scope_and_watermark(run.name, inst, run_as_user, source_apps=source_apps)

	except Exception:
		# Undo every row this launch wrote, so a caller that later commits (and both
		# of them do, to record the failure) cannot flush a half-built run.
		frappe.db.rollback(save_point=savepoint)
		raise

	frappe.db.commit()

	# Activity trail (best-effort, Link-free): row is owner-scoped like the run.
	log_activity(
		agent=inst.agent,
		agent_title=listing.title,
		installation=inst.name,
		action="run_started",
		run=run.name,
		detail=f"trigger: {trigger}",
	)

	# Dispatch: every agent is a DELEGATE — it runs server-side via admin -> fleet
	# -> the tenant's gateway, sharing every identity/scope guard above. The fleet
	# dispatches the turn DETACHED on the cron lane (A11 — never queues customer
	# chat) and returns 202; the Run stays "running" until the Phase-3
	# record_agent_run writeback (or a status poll) marks it done — the dispatch
	# does NOT block on the run finishing.
	from jarvis import admin_client
	from jarvis.chat.agent_catalog import registry_timeout_s

	try:
		admin_client.post_agent_run(
			run_id=run.name,
			agent_id=f"agent-{slug}",
			session_key=session_key,
			message=_audit_prompt(listing, inst, trigger, scope),
			timeout_s=registry_timeout_s(slug),
		)
	except admin_client.AdminAmbiguousError:
		# #743: the dispatch TIMED OUT (or the connection reset mid-flight) - admin
		# MAY already be running this turn. Terminalizing the run ``failed`` here is
		# exactly what let the slot be retried under a FRESH run id an hour later, so
		# the fleet's run-id idempotency never engaged and the customer paid for two
		# audits of one slot. So do the opposite of the confirmed-failure branch
		# below: leave the run ``running`` (it is already committed so), do NOT tear
		# down its session (a genuinely-live delegate resolves its identity and its
		# record_agent_run writeback through that row), and re-raise the SAME
		# ambiguous signal so the caller keeps the slot claimed instead of handing it
		# back. The 3h stale-run reaper is then the single arbiter: it completes a
		# run the writeback lands for and fails one that truly never started - it
		# never relabels a real outcome. The commit persists the Error Log before the
		# re-raise trips the request-level rollback on the manual path.
		frappe.log_error(
			title=f"jarvis agent-run dispatch ambiguous (left running): {run.name}",
			message=frappe.get_traceback(),
		)
		frappe.db.commit()
		raise
	except Exception as e:
		# The dispatch call itself failed, and CONFIRMEDLY: nothing was sent (a clean
		# refusal / connect timeout), a 4xx rejection, or an auth denial. Mark THIS
		# Run failed (mirror _record_failed's writeback onto the already-created
		# "running" row so it is never orphaned), then re-raise so the caller's
		# retry/notify path runs (scheduler: no next_run_at advance -> retry next
		# hour; run_agent_now: surfaces the error to the UI).
		#
		# One confirmed failure has a self-service fix, so translate it rather than
		# leaving a raw fleet 502: the fleet-agent's "agent_id '<x>' is not an
		# installed delegate on <container>" means the agent is ENABLED on the bench
		# but its skill was never pushed to the container. Enabling only flags the
		# catalog dirty; the skill reaches the container on APPLY. So the operator
		# skipped (or has a pending) "Apply catalog changes" -- tell them that
		# instead of a stack trace.
		#
		# jarvis#1062: "them" is not always someone who CAN apply it. The catalog
		# push needs a reviewer role (Jarvis Skill Reviewer / Jarvis Admin / System
		# Manager — is_skill_reviewer). Branch on the INSTALLATION OWNER, never
		# ``frappe.session.user``: by this point the session is impersonated as the
		# run-as user (Phase 1 identity, see above), which is deliberately decoupled
		# from the owner (R1-F3) and is not even always a human. The OWNER is who
		# actually reads this message back on the Runs board (the Run row's owner is
		# always the installation owner, never the run-as user), on both the manual
		# and the cron path (run_due_agent_audits sweeps by owner too).
		not_applied = "not an installed delegate" in str(e)
		if not_applied:
			from jarvis.permissions import is_skill_reviewer

			if is_skill_reviewer(owner):
				error_msg = _(
					"This agent is not loaded on your container yet. Open the Agents page, "
					'click "Apply catalog changes" to push it, then run it again.'
				)
			else:
				error_msg = _(
					"This agent is not ready on your workspace yet. Ask your administrator "
					"to apply catalog changes."
				)
		else:
			error_msg = "agent-run dispatch failed; see Error Log"
		frappe.db.set_value(
			RUN,
			run.name,
			{
				"status": "failed",
				"finished_at": frappe.utils.now(),
				"error": error_msg,
			},
			update_modified=False,
		)
		# A8: a run that never dispatched must not leave its session bearer.
		from jarvis.chat import agent_runs

		agent_runs.teardown_run_session(session_key)
		frappe.db.commit()
		frappe.log_error(
			title=f"jarvis agent-run dispatch failed: {run.name}",
			message=frappe.get_traceback(),
		)
		if not_applied:
			# Surface the actionable message straight to the SPA (a clean user error),
			# not the raw AdminUnreachableError 502/500 the fleet verb produced. The
			# failed Run + session teardown above are already committed, so this only
			# swaps which exception the caller re-raises.
			frappe.throw(error_msg, title=_("Agent not applied"))
		raise
	return {"run": run.name, "conversation": conv.name, "session_key": session_key}


# --------------------------------------------------------------------------- #
# Phase 1 identity helpers — per-run session, scope, watermark, perm-profile
# --------------------------------------------------------------------------- #
def _mint_run_session(session_key: str, user: str) -> None:
	"""Insert the per-run ``Jarvis Chat Session`` row that maps session_key →
	run-as user, mirroring ``chat/api._ensure_session_key``'s shape: snapshot the
	bench's current ``chat_device_id`` so a re-pair invalidates the row (the
	device-binding check at ``api.py:106-139``). ignore_permissions — this is
	trusted server infrastructure, and session_key is unique (run.name is a hash)."""
	device_id = (frappe.db.get_single_value("Jarvis Settings", "chat_device_id") or "").strip()
	frappe.get_doc(
		{
			"doctype": "Jarvis Chat Session",
			"session_key": session_key,
			"user": user,
			"chat_device_id": device_id,
		}
	).insert(ignore_permissions=True)


def _stamp_scope_and_watermark(
	run_name: str, inst, run_as_user: str, source_apps: list[str] | None = None
) -> dict | None:
	"""Resolve the explicit scope (A6), compute the GL consistency watermark
	(A17) and the run-as permission profile (A12), and stamp them on the Run.

	All best-effort: a bench without a resolvable Company (e.g. no erpnext setup)
	must NOT abort the launch — it degrades to an unscoped run (no watermark). The
	watermark + scope are resolved AS the run-as user (this runs under that
	session). Returns the resolved scope dict (or None).

	``source_apps`` (CX5-2) is folded into the SAME ``scope_json`` stamp OUTSIDE
	the best-effort try, so an ERP scope-resolution failure can never drop the
	authorization the source-read tools read back — a scribe run either carries its
	admin-authorized app list or reads nothing."""
	from jarvis.chat import agent_scope

	values: dict = {}
	scope = None
	try:
		scope = agent_scope.resolve_scope(inst)
	except Exception:
		frappe.log_error(
			title="jarvis agent: scope resolution failed (unscoped run)",
			message=frappe.get_traceback(),
		)
	if source_apps is not None:
		scope = dict(scope or {})
		scope["source_apps"] = list(source_apps)
	if scope is not None:
		values["scope_json"] = frappe.as_json(scope)

	if scope and scope.get("company") and scope.get("to_date"):
		# A17: row-count + max(modified) over the scope's GL as-of window. The old
		# engine ran the whole pack in one snapshot; the chunked container run
		# spans minutes, so a mid-run backdated JV (endemic at Indian year-end)
		# is caught by recomputing this at writeback (Phase 3) and comparing.
		try:
			wm = frappe.db.sql(
				"""select count(*) n, max(modified) m from `tabGL Entry`
				   where company = %(company)s and posting_date <= %(to_date)s""",
				{"company": scope["company"], "to_date": scope["to_date"]},
				as_dict=True,
			)[0]
			values["wm_row_count"] = int(wm.n or 0)
			values["wm_gl_max_modified"] = wm.m
		except Exception:
			frappe.log_error(
				title="jarvis agent: GL watermark computation failed",
				message=frappe.get_traceback(),
			)

	try:
		values["permission_profile"] = _permission_profile(run_as_user)
	except Exception:
		pass

	if values:
		frappe.db.set_value(RUN, run_name, values, update_modified=False)
	return scope


def _permission_profile(user: str) -> str:
	"""A compact JSON summary + sha256 of the run-as user's roles + user-permission
	keys, so a drift between mapping-time and run-time perms is detectable (A12)."""
	import hashlib
	import json

	from frappe.permissions import get_user_permissions

	roles = sorted(frappe.get_roles(user))
	try:
		perms = get_user_permissions(user) or {}
	except Exception:
		perms = {}
	up_keys: dict = {}
	for dt, entries in perms.items():
		vals = sorted({(e.get("doc") if isinstance(e, dict) else str(e)) for e in (entries or [])})
		up_keys[dt] = [v for v in vals if v]
	summary = {"roles": roles, "user_permissions": up_keys}
	digest = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
	return json.dumps({"hash": digest, **summary})


def _audit_prompt(listing, inst, trigger: str, scope: dict | None = None) -> str:
	"""The GENERIC, non-leaky run message handed to the delegate (A2/A6).

	It names NO rule, tool, threshold, engagement step, or domain — the delegate's
	bundled SKILL.md (sourced admin-side from the PRIVATE bundle store, never the
	bench) carries the actual "how". The bench injects only:
	  * a pointer to the engagement config on the installation (the delegate reads
	    it there via its own permission-bounded tools — the config is not dumped
	    into context), and
	  * the EXPLICIT resolved SCOPE verbatim (A6), so the bundle NEVER infers "the
	    current period" (a UTC container clock vs an IST site picks the wrong FY for
	    ~5.5h/day, catastrophic at Mar-31/Apr-1). Prior-FY selection stays versioned
	    bench code, injected — never LLM prose.
	Kept short: the delegate resolves the run/installation linkage for its Phase-3
	writeback from the session_key the bench minted, not from this text."""
	scope_block = ""
	# CX5-2: name the admin-authorized apps explicitly. The bench ENFORCES the same
	# list on every source-read/wiki call, so this is a courtesy so the delegate does
	# not waste the run discovering that everything else is refused.
	if scope and scope.get("source_apps"):
		apps = ", ".join(str(a) for a in scope["source_apps"])
		scope_block += f"\n\nAUTHORIZED APPS (the ONLY apps you may read source from or write about): {apps}."
	if scope and scope.get("company"):
		scope_block += (
			"\n\nEXPLICIT SCOPE (use these EXACT values; never infer the period): "
			f'company="{scope.get("company")}", '
			f'fiscal_year="{scope.get("fiscal_year")}", '
			f'from_date="{scope.get("from_date")}", '
			f'to_date="{scope.get("to_date")}", '
			f'prior_fy_start="{scope.get("prior_fy_start")}", '
			f'prior_fy_end="{scope.get("prior_fy_end")}".'
		)
	return (
		f"[Automated {trigger} run] Run your bundled playbook for this trigger now over "
		f"the scope below. Your engagement configuration is on your installation "
		f"({inst.name}); read it there. Follow your skill exactly and do only what it "
		f"authorises."
		f"{scope_block}"
	)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _is_app_learning(agent: str) -> bool:
	"""True iff this listing is the Custom App Learning capability — the one agent
	whose launch REQUIRES an explicit admin app selection (CX5-2)."""
	from jarvis.tools import _app_learning_ctx

	return _app_learning_ctx.is_app_learning_agent(agent)


def _valid_owner(owner: str) -> bool:
	"""S1 fail-closed identity guard. The rule itself now lives in
	``jarvis.permissions.is_valid_unattended_owner`` so the macro scheduler shares
	one definition with this one instead of re-deriving it (jarvis #469); this
	wrapper keeps the local name every call site here already uses."""
	return is_valid_unattended_owner(owner)


def _agent_run_budget_monthly() -> int:
	"""A14: the per-INSTALLATION monthly run budget from Jarvis Settings, floored at
	MIN_AGENT_RUN_BUDGET_MONTHLY (a full daily schedule) so a misconfigured 0/blank
	never wedges every scheduled agent for a month. Read at run time (not a constant)
	so a bench admin can raise it without a code change."""
	try:
		v = frappe.utils.cint(frappe.db.get_single_value("Jarvis Settings", "agent_run_budget_monthly"))
	except Exception:
		v = 0
	return v if v >= MIN_AGENT_RUN_BUDGET_MONTHLY else DEFAULT_AGENT_RUN_BUDGET_MONTHLY


def _expected_monthly_runs(frequency: str) -> int:
	"""Upper-bound runs/month a schedule frequency generates (daily ~31, weekly 5,
	monthly 1). Used at install/validate to warn when a schedule can't fit its
	budget."""
	return {"daily": 31, "weekly": 5, "monthly": 1}.get((frequency or "").strip().lower(), 31)


def _runs_this_month(*, installation: str | None = None) -> int:
	"""This month's NON-FAILED agent runs — for ONE installation (the per-install
	budget) or the whole tenant (the aggregate ceiling). Failed rows are EXCLUDED
	(A14): every skip path writes a ``failed`` row, so counting them would make the
	cap self-perpetuating once hit. Manual + scheduled are counted together."""
	month_start = frappe.utils.get_first_day(frappe.utils.today())
	filters = {"creation": [">=", month_start], "status": ["!=", "failed"]}
	if installation:
		filters["installation"] = installation
	return frappe.db.count(RUN, filters)


def _over_run_budget(installation: str) -> tuple[bool, str]:
	"""A14 gate for BOTH the scheduler and the manual run_agent_now path. Returns
	``(over, reason)`` when THIS installation's next run would breach either:
	  * the per-installation monthly budget, OR
	  * the per-tenant aggregate ceiling (budget × enabled installs) — a backstop so
	    N installs can't multiply the drain even if per-install accounting is bypassed
	    by a burst.
	Keyed on the installation + tenant, NEVER the owner (run_as_user decouples the
	executing identity from the owner, so a per-owner count both mis- and
	under-counts)."""
	budget = _agent_run_budget_monthly()
	if _runs_this_month(installation=installation) >= budget:
		return True, "monthly run budget exceeded for this agent"
	enabled = frappe.db.count(INSTALLATION, {"enabled": 1}) or 1
	if _runs_this_month() >= budget * enabled:
		return True, "tenant-wide monthly agent run budget exceeded"
	return False, ""


def _dispatch_lock_name(installation: str) -> str:
	"""The per-installation dispatch lock, shared by the cron sweep and the manual
	``run_agent_now``. One name in one place: two spellings would serialize each path
	against itself and neither against the other, which is the race (#672)."""
	return f"jarvis_agent_dispatch:{installation}"


def _live_run(installation: str) -> str | None:
	"""The name of an IN-FLIGHT run for this installation, or None (#672).

	Liveness is status PLUS freshness, never status alone. A row still ``running``
	past ``STALE_RUN_AFTER_SECONDS`` is precisely what ``reap_stale_agent_runs``
	exists to terminalize, so believing it here would let one wedged run suppress
	every dispatch (and every manual run) until the reaper caught up, silently
	skipping real slots for up to three hours. The two contracts are deliberately
	keyed to the SAME cutoff: nothing the reaper would kill counts as in flight, so
	there is no window where a row is too old to be live and too young to be reaped.
	The reaper's own behaviour is unchanged.

	``ignore_permissions`` because this is a server-side concurrency guard: runs are
	owner-scoped by an ``if_owner`` query condition, so a System Manager triggering
	someone else's install would otherwise be told there is no live run when there
	is one."""
	if not installation:
		return None
	fresh = now_datetime() - timedelta(seconds=STALE_RUN_AFTER_SECONDS)
	rows = frappe.get_all(
		RUN,
		filters={"installation": installation, "status": "running", "started_at": [">", fresh]},
		pluck="name",
		limit=1,
		ignore_permissions=True,
	)
	return rows[0] if rows else None


def _claim_slot(row, now) -> dict | None:
	"""Consume this slot durably BEFORE dispatching it, and return what it held so a
	launch that provably created nothing can hand it back. None means another
	dispatcher already claimed it.

	Compare-and-set under a ROW LOCK: the sweep's ``frappe.get_all`` does not lock, so
	two dispatchers can read the same due row; re-reading ``next_run_at`` for update
	and confirming it is still due is what makes exactly one of them the dispatcher.
	``compute_next_run`` returns a time strictly after ``now``, so a slot another
	dispatcher has claimed reads as not-due here even within the same second."""
	frappe.db.commit()  # REPEATABLE-READ discipline: FOR UPDATE goes first
	cur = frappe.db.get_value(
		INSTALLATION, row.name, ["next_run_at", "last_run_at"], as_dict=True, for_update=True
	)
	if not cur or not cur.next_run_at or frappe.utils.get_datetime(cur.next_run_at) > now:
		frappe.db.commit()  # release the row lock; this slot is not ours to run
		return None
	_advance(row, now)  # commits, releasing the row lock
	return {"next_run_at": cur.next_run_at, "last_run_at": cur.last_run_at}


def _unclaim_slot(row, prev: dict) -> None:
	"""Give a claimed slot back, unchanged, after a launch that created nothing. Only
	ever called with the dispatch lock held and with a launch that left no live run,
	so this cannot resurrect a slot some other dispatcher is running."""
	frappe.db.set_value(
		INSTALLATION,
		row.name,
		{"next_run_at": prev["next_run_at"], "last_run_at": prev["last_run_at"]},
		update_modified=False,
	)
	frappe.db.commit()


def _advance(row, now) -> None:
	"""Advance the schedule with a raw set_value (no re-validate). ``last_run_at``
	is stamped whether the slot produced a real, failed, or skipped run — the
	slot was consumed either way."""
	frappe.db.set_value(
		INSTALLATION,
		row.name,
		{
			"last_run_at": now,
			"next_run_at": compute_next_run(row.schedule_frequency, row.schedule_time, from_dt=now),
		},
		update_modified=False,
	)
	frappe.db.commit()


def _record_failed(row, reason: str) -> None:
	"""Write a ``failed`` Jarvis Agent Run row (owned by the installation owner)
	so the customer sees WHY a scheduled slot did not run."""
	run = frappe.get_doc(
		{
			"doctype": RUN,
			"agent": row.agent,
			"installation": row.name,
			"trigger": "scheduled",
			"status": "failed",
			"started_at": frappe.utils.now(),
			"finished_at": frappe.utils.now(),
			"error": (reason or "")[:140],
		}
	)
	run.flags.ignore_permissions = True
	run.insert()
	if row.owner and row.owner != frappe.session.user:
		frappe.db.set_value(RUN, run.name, "owner", row.owner, update_modified=False)
	# Activity trail (best-effort, Link-free) — every other run outcome logs
	# one; the explicit owner keeps the feed row owner-scoped even though the
	# scheduler runs as Administrator.
	log_activity(
		agent=row.agent,
		agent_title=frappe.db.get_value(LISTING, row.agent, "title"),
		installation=row.name,
		action="run_failed",
		run=run.name,
		detail=(reason or "")[:140],
		owner=row.owner,
	)
	frappe.db.commit()


def _notify_owner(owner: str, row, reason: str | None = None) -> None:
	"""Best-effort owner notification on enqueue failure OR budget exhaustion (A14),
	never raises. A budget message says the cap is hit + resets next month (it will
	NOT simply retry next hour), so the owner is not left waiting on a run that can't
	start until the budget rolls over or an admin raises it."""
	if not _valid_owner(owner):
		return
	is_budget = bool(reason) and "budget" in reason.lower()
	try:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": owner,
				"type": "Alert",
				"subject": (
					f"Agent run budget reached: {row.agent}"
					if is_budget
					else f"Scheduled audit could not start: {row.agent}"
				),
				"email_content": (
					(
						f"A scheduled agent run was skipped: {reason}. Runs resume next "
						"month, or ask an admin to raise the monthly agent-run budget in "
						"Jarvis Settings."
					)
					if is_budget
					else (
						"A scheduled agent audit could not be started. It will retry on the next hourly run."
					)
				),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		pass
