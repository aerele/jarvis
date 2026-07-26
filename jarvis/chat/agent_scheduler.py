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
* **O4:** ``next_run_at`` advances ONLY after a successful enqueue; on failure a
  ``failed`` run is recorded + the owner notified, and the missed slot is NOT
  backfilled (``compute_next_run`` from *now* yields a single next future slot).
* **O7:** identical ``(owner, agent, cadence, time)`` due rows are deduped.

``_launch_audit`` is shared with ``agents_api.run_agent_now`` so a manual
trigger takes the EXACT same code path as the scheduler.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import now_datetime

from jarvis.chat.agent_activity import log_activity
from jarvis.chat.macro_scheduler import compute_next_run
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
STALE_RUN_AFTER_SECONDS = 3 * 3600


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

	from jarvis.chat.agents_api import _user_allowed_for_agent

	original_user = frappe.session.user
	seen: set = set()  # O7: dedupe identical (owner, agent, cadence, time)
	for row in due:
		key = (row.owner, row.agent, row.schedule_frequency, str(row.schedule_time))
		if key in seen:
			_advance(row, now)
			continue
		seen.add(key)

		# R5-J8: never dispatch a scheduled run for a non-installable capability. A
		# reconcile marks an install installable=0 when a min_apps dependency
		# disappeared after install (the row is kept, not deleted); its run has no
		# data. Record why + consume the slot so the cadence does not busy-retry.
		if not frappe.utils.cint(row.installable):
			_record_failed(
				row, "scheduled audit skipped: capability not installable (app_absent_or_ineligible)"
			)
			_advance(row, now)
			continue

		# Only auditor + scribe agents run scheduled scans; an operator install
		# with a schedule set just consumes its slot (it drafts through the board,
		# not on a cron). A scribe's schedule is optional (manual run-now is the
		# primary path) but honoured here when set, so periodic re-learning works.
		nature = frappe.db.get_value(LISTING, row.agent, "nature")
		if nature not in ("Auditor", "Scribe"):
			_advance(row, now)
			continue

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
			continue

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
				continue

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
			continue

		# S1 fail-closed identity guard — never bind a scheduled audit turn to
		# Administrator / Guest / a disabled RUN-AS user.
		if not _valid_owner(run_as):
			_record_failed(row, "scheduled audit skipped: invalid run-as user (fail-closed guard)")
			_advance(row, now)
			continue

		# RBAC: the listing may have been restricted (or the run-as user's roles
		# revoked) AFTER install. Skip, record WHY, and consume the slot — never
		# dispatch a turn for a run-as identity the agent no longer permits
		# (gotcha #8 — the EXECUTING identity is gated, not the triggerer).
		if not _user_allowed_for_agent(row.agent, run_as):
			_record_failed(row, "run-as user's roles no longer permit this agent")
			_advance(row, now)
			continue

		# A14 cost cap — per installation + per-tenant aggregate (the subscription is
		# the tenant's). Manual + scheduled counted together; failed rows excluded, so
		# the _record_failed row we write below can never self-perpetuate the cap.
		over, why = _over_run_budget(row.name)
		if over:
			_record_failed(row, why)
			_notify_owner(row.owner, row, reason=why)
			_advance(row, now)
			continue

		# S1 hinge: mint the run session + create conv/run INSIDE set_user(run_as).
		# Row ownership is reassigned to the human owner inside _launch_audit; only
		# the ERP-read identity is the run-as user.
		try:
			frappe.set_user(run_as)
			inst = frappe.get_doc(INSTALLATION, row.name)
			_launch_audit(inst, trigger="scheduled", source_apps=source_apps)
			frappe.set_user(original_user)
			_advance(row, now)  # O4: advance ONLY after a successful enqueue
		except Exception:
			frappe.set_user(original_user)
			frappe.log_error(
				title=f"jarvis scheduled audit failed: {row.name}",
				message=frappe.get_traceback(),
			)
			_record_failed(row, "scheduled audit enqueue failed; see Error Log")
			_notify_owner(row.owner, row)
			# Do NOT advance -> retry next hour. compute_next_run(from=now) means
			# even a long outage yields ONE next slot, never a backfill storm.
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
	only AFTER the transition is won + committed."""
	from jarvis.chat import agent_runs
	from jarvis.tools.record_app_wiki import reconcile_run_pages

	cutoff = now_datetime() - timedelta(seconds=STALE_RUN_AFTER_SECONDS)
	reaped = 0
	for r in _stale_candidates(cutoff):
		try:
			# Re-read under a ROW LOCK immediately before deciding. The row lock serializes
			# against a concurrent record_app_wiki/finish (both write this row), so the
			# status + tally we act on are current, not the possibly-stale scan snapshot.
			cur = frappe.db.get_value(
				RUN,
				r.name,
				["status", "pages_written", "agent", "session_key", "owner", "installation"],
				as_dict=True,
				for_update=True,
			)
			if not cur or cur.status != "running":
				# A concurrent finish already moved it off running — leave it alone (never
				# overwrite a completed run with failed). Release the lock and move on.
				frappe.db.commit()
				continue
			nature = (frappe.db.get_value(LISTING, cur.agent, "nature") or "").strip().title()
			pages = int(cur.pages_written or 0)
			pages_meta = None
			if nature == "Scribe" and pages == 0:
				# CA2-3 fallback: rebuild the tally from page provenance before failing a
				# scribe run whose stored tally reads zero, so real work is never lost.
				pages_meta = reconcile_run_pages(r.name)
				if pages_meta is None:
					# CA3-4: the provenance QUERY failed — the true page count is UNKNOWN.
					# Neither terminalization is safe (completing with 0 would drop real
					# pages; failing would mislabel a success), so leave the run running and
					# retry on the next sweep. Release the row lock and move on.
					frappe.db.commit()
					continue
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
				frappe.db.set_value(RUN, r.name, values, update_modified=False)
				frappe.db.commit()  # win + release the row lock BEFORE tearing down the session
				agent_runs.teardown_run_session(cur.session_key)
				log_activity(
					agent=cur.agent,
					agent_title=frappe.db.get_value(LISTING, cur.agent, "title"),
					installation=cur.installation,
					action="run_completed",
					run=r.name,
					detail=f"reconciled to completed: scribe wrote {pages} page(s); finish not called",
					owner=cur.owner,
				)
				frappe.db.commit()
				reaped += 1
				continue
			# The row lock is already held and the compare-and-set above has already
			# established that the row is still ``running``, so this goes straight to
			# the shared terminalization tail.
			_terminalize_failed(
				r.name,
				agent=cur.agent,
				installation=cur.installation,
				session_key=cur.session_key,
				owner=cur.owner,
				error="run exceeded max duration; reaped by the stale-run sweep (A8 backstop)",
				detail="reaped: run exceeded max duration",
			)
			reaped += 1
		except Exception:
			frappe.log_error(
				title=f"jarvis agent: stale-run reap failed: {r.name}",
				message=frappe.get_traceback(),
			)
	return reaped


def _stale_candidates(cutoff) -> list:
	"""Runs stuck ``running`` past ``cutoff`` — the reaper's candidate set. Factored so
	the CA2-4 compare-and-set (re-read under a row lock before transitioning) can be
	exercised against a deliberately-stale candidate snapshot in tests."""
	return frappe.get_all(
		RUN,
		filters={"status": "running", "started_at": ["<", cutoff]},
		fields=["name", "session_key", "owner", "agent", "installation", "pages_written"],
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
# shared launch (scheduler + manual run_agent_now take the SAME path)
# --------------------------------------------------------------------------- #
def _launch_audit(inst, trigger: str, source_apps: list[str] | None = None) -> dict:
	"""Create the audit conversation + a ``running`` Jarvis Agent Run + enqueue
	the triggering turn. MUST run as the installation owner (the scheduler
	set_user's it; run_agent_now is already the owner). Returns
	``{run, conversation, session_key}``. Identity/budget guards + next_run_at
	advancement are the caller's job.

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
				"contact support — nothing was started."
			)
		)

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
	#     scheduled cron run (no human initiated it). On the manual path the caller has
	#     switched the session to the run-as user, so frappe.session.user is the
	#     triggering human ONLY on a self-mapped install (run_as == triggerer); see the
	#     cross-file note for the run_as != triggerer case.
	bundle_version = inst.installed_version or listing.version or None
	preparation_mode = inst.activation_state or "shadow"
	initiating_human = frappe.session.user if trigger == "manual" else None

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
	# openclaw session keys are `agent:<agent-id>:<key>` and the gateway resolves
	# the session under that agent-id. The delegate agent id is `agent-<slug>`
	# (fleet-agent compose.agent_delegates), so the id component MUST be the full
	# delegate id, not the bare slug — otherwise the gateway `agent` RPC's
	# agentId (`agent-<slug>`) and the session key's embedded id (`<slug>`)
	# disagree. The bench never parses this shape (it matches the Jarvis Chat
	# Session row verbatim), so aligning it is free on the bench side and correct
	# on the openclaw side.
	session_key = f"agent:agent-{slug}:{run.name}"
	_mint_run_session(session_key, run_as_user)
	frappe.db.set_value(RUN, run.name, "session_key", session_key, update_modified=False)

	# A6 explicit scope + A17 consistency watermark + A12 permission profile —
	# all best-effort (never abort the launch) and computed AS the run-as user.
	scope = _stamp_scope_and_watermark(run.name, inst, run_as_user, source_apps=source_apps)

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
	except Exception:
		# The dispatch call itself failed. Mark THIS Run failed (mirror
		# _record_failed's writeback onto the already-created "running" row so
		# it is never orphaned), then re-raise so the caller's retry/notify path
		# runs (scheduler: no next_run_at advance -> retry next hour;
		# run_agent_now: surfaces the error to the UI).
		frappe.db.set_value(
			RUN,
			run.name,
			{
				"status": "failed",
				"finished_at": frappe.utils.now(),
				"error": "agent-run dispatch failed; see Error Log",
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
	if not owner or owner in ("Administrator", "Guest"):
		return False
	return bool(frappe.db.get_value("User", owner, "enabled"))


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
						f"A scheduled agent run was skipped — {reason}. Runs resume next "
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
