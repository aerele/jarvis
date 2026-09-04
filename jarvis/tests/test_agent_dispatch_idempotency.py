"""#672: a scheduled agent slot must be dispatched EXACTLY once.

``_launch_audit`` commits its ``running`` Jarvis Agent Run before it returns, so
anything that went wrong between that commit and the schedule advance left the row
still DUE with a live audit going, and the next hourly tick dispatched the SAME slot
again. The customer was billed twice against the A14 budget and their only
notification said the run could not be started.

These tests drive the real shapes rather than asserting on a mock at the boundary
under repair, because the three holes an earlier attempt (PR #662) left open are all
invisible to a healthy-input test:

  * a crash AFTER a successful launch, followed by the run COMPLETING, which is the
    common case: audits take minutes and ticks are hourly, so any guard keyed on run
    liveness has stopped protecting by the time the next tick arrives;
  * a manual ``run_agent_now`` and the cron sweep genuinely INTERLEAVED (driven here
    by re-entering one path from inside the other's dispatch call, which is where the
    two really overlap), not two sequential calls;
  * a launch that raises MID-WAY, which used to leave an orphaned ``running`` row
    (the pending inserts were flushed by ``_record_failed``'s own commit) and made
    every later tick skip a real slot while the failure went unreported.

Run:
  bench --site test_site run-tests --app jarvis \
    --module jarvis.tests.test_agent_dispatch_idempotency
"""

import json
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, now_datetime

from jarvis import admin_client
from jarvis.chat import agent_catalog, agent_scheduler, agents_api
from jarvis.tests._agent_access import allow_listing_for

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
CONV = "Jarvis Conversation"
SESSION = "Jarvis Chat Session"
ACTIVITY = "Jarvis Agent Activity"
NOTIFICATION = "Notification Log"

SLUG = "dispatch-idem-auditor"
OWNER = "dispatch-idem-owner@example.com"

# Any non-empty declared surface: _launch_audit refuses a bundle that declares no
# tools before it creates a row, and this module is about dispatch, not contracts.
DECLARED = ["jarvis__get_schema"]


def _mk_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		user.insert(ignore_permissions=True)
		user.add_roles("Jarvis User")
	return email


def _mk_listing() -> str:
	if not frappe.db.exists(LISTING, SLUG):
		frappe.get_doc(
			{
				"doctype": LISTING,
				"agent_slug": SLUG,
				"title": "Dispatch idempotency auditor",
				"rule_tokens": json.dumps(["tok"]),
				"doctypes_required": json.dumps([]),
				"rule_pack": f"pack-{SLUG}",
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value(LISTING, SLUG, {"status": "Published", "nature": "Auditor"}, update_modified=False)
	return SLUG


def _wipe() -> None:
	"""Everything a launch leaves behind. Launches commit, so FrappeTestCase's own
	rollback cannot reclaim any of it."""
	for name in frappe.get_all(RUN, filters={"agent": SLUG}, pluck="name", ignore_permissions=True):
		frappe.delete_doc(RUN, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(ACTIVITY, filters={"agent": SLUG}, pluck="name", ignore_permissions=True):
		frappe.delete_doc(ACTIVITY, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(INSTALLATION, filters={"agent": SLUG}, pluck="name", ignore_permissions=True):
		frappe.delete_doc(INSTALLATION, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(CONV, filters={"owner": OWNER}, pluck="name", ignore_permissions=True):
		frappe.delete_doc(CONV, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(SESSION, filters={"user": OWNER}, pluck="name", ignore_permissions=True):
		frappe.delete_doc(SESSION, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(
		NOTIFICATION, filters={"for_user": OWNER}, pluck="name", ignore_permissions=True
	):
		frappe.delete_doc(NOTIFICATION, name, force=True, ignore_permissions=True)
	frappe.db.commit()


class DispatchIdempotencyTestCase(FrappeTestCase):
	"""Shared fixture: one published auditor, installed for one owner, due now, with
	the fleet dispatch stubbed so ``_launch_audit`` runs for real end to end."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user(OWNER)
		_mk_listing()
		# jarvis#1062: agent access is DENY BY DEFAULT, and this module's owner is a
		# plain Jarvis User. Every dispatch path here (run_agent_now, the sweep,
		# _launch_audit) gates on it, so without an explicit grant the whole module
		# tests nothing but the refusal. Persists module-wide - neither _wipe() nor
		# _mk_listing() touches the allow rows.
		allow_listing_for(SLUG, user=cls.owner)
		# The A14 ceiling counts every non-failed run on the SITE, including rows other
		# platform-test modules commit and never clean, so it can refuse a legitimate
		# dispatch here for reasons that have nothing to do with this module.
		cls._orig_budget = frappe.db.get_single_value("Jarvis Settings", "agent_run_budget_monthly")
		frappe.db.set_single_value("Jarvis Settings", "agent_run_budget_monthly", 1000000)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Jarvis Settings", "agent_run_budget_monthly", cls._orig_budget)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()
		_mk_listing()
		frappe.db.commit()
		self.dispatched = []

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe()

	# ------------------------------------------------------------------ #
	# fixture
	# ------------------------------------------------------------------ #
	def _due_install(self) -> str:
		doc = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": SLUG,
				"run_as_user": self.owner,
				"reviewer": self.owner,
				"enabled": 1,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(INSTALLATION, doc.name, "owner", self.owner, update_modified=False)
		frappe.db.set_value(
			INSTALLATION,
			doc.name,
			{
				"schedule_enabled": 1,
				"schedule_frequency": "daily",
				"next_run_at": add_days(now_datetime(), -1),
			},
			update_modified=False,
		)
		frappe.db.commit()
		return doc.name

	def _due_row(self, inst: str):
		"""The snapshot ``run_due_agent_audits`` hands ``_sweep_one``, so a test can
		replay a worker that read the row while it was still due."""
		return frappe.get_all(
			INSTALLATION,
			filters={"name": inst},
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
			ignore_permissions=True,
		)[0]

	# ------------------------------------------------------------------ #
	# harness
	# ------------------------------------------------------------------ #
	def _dispatch_stub(self, **kw):
		self.dispatched.append(kw)
		return {"run_id": kw.get("run_id"), "status": "queued"}

	@contextmanager
	def _delegate_stubbed(self, dispatch=None):
		"""Run the REAL ``_launch_audit`` without a fleet: only the outbound admin call
		and the bundled registry lookup are stubbed, so the conversation, the run row,
		the session and every commit boundary are exercised."""
		with ExitStack() as stack:
			stack.enter_context(patch.object(admin_client, "post_agent_run", dispatch or self._dispatch_stub))
			stack.enter_context(patch.object(agent_catalog, "registry_tools_allow", return_value=DECLARED))
			# Keeps the manual path's lock wait off the clock when a test deliberately
			# collides with a launch that is still in flight. create=True so the module
			# without the fix (where nothing reads it) patches cleanly too.
			stack.enter_context(patch.object(agents_api, "DISPATCH_LOCK_WAIT_S", 0.1, create=True))
			yield

	@contextmanager
	def _only_this_install_due(self):
		"""Park every OTHER due installation. This is a shared site and other modules
		leave due rows behind; the sweep is site-wide."""
		now = now_datetime()
		parked = {
			r.name: r.next_run_at
			for r in frappe.get_all(
				INSTALLATION,
				filters={
					"enabled": 1,
					"schedule_enabled": 1,
					"next_run_at": ["<=", now],
					"agent": ["!=", SLUG],
				},
				fields=["name", "next_run_at"],
				ignore_permissions=True,
			)
		}
		for name in parked:
			frappe.db.set_value(INSTALLATION, name, "next_run_at", add_days(now, 2), update_modified=False)
		frappe.db.commit()
		try:
			yield
		finally:
			for name, ts in parked.items():
				frappe.db.set_value(INSTALLATION, name, "next_run_at", ts, update_modified=False)
			frappe.db.commit()

	def _sweep(self, *, dispatch=None, launch=None) -> None:
		"""One hourly tick."""
		with self._only_this_install_due(), self._delegate_stubbed(dispatch), ExitStack() as stack:
			if launch is not None:
				stack.enter_context(patch.object(agent_scheduler, "_launch_audit", launch))
			agent_scheduler.run_due_agent_audits()

	def _sweep_as_administrator(self) -> None:
		"""A tick firing on a cron worker while some OTHER dispatch is mid-flight on
		this thread. The session is restored, so the caller's launch continues under
		the identity it had."""
		prev = frappe.session.user
		frappe.set_user("Administrator")
		try:
			with self._only_this_install_due():
				agent_scheduler.run_due_agent_audits()
		finally:
			frappe.set_user(prev)

	def _run_now(self, inst: str, *, dispatch=None):
		with self._delegate_stubbed(dispatch):
			prev = frappe.session.user
			frappe.set_user(self.owner)
			try:
				return agents_api.run_agent_now(inst)
			finally:
				frappe.set_user(prev)

	@staticmethod
	def _launch_then_crash():
		"""The launch gets all the way through, then the worker dies before the sweep
		can finish its bookkeeping: a killed RQ worker, an OOM, a failed set_value."""
		real = agent_scheduler._launch_audit

		def _wrapped(inst, **kw):
			real(inst, **kw)
			raise RuntimeError("worker killed after a successful launch")

		return _wrapped

	# ------------------------------------------------------------------ #
	# assertions
	# ------------------------------------------------------------------ #
	def _launched(self, inst: str) -> list:
		"""Runs that actually STARTED an audit. A skip records a ``failed`` row without
		ever reaching the container, so reaching ``running`` is what makes it a launch,
		and the row survives the audit completing (which is exactly the case a
		liveness-only guard misses)."""
		return frappe.get_all(
			RUN,
			filters={"installation": inst, "status": ["in", ("running", "completed")]},
			order_by="creation asc",
			pluck="name",
			ignore_permissions=True,
		)

	def _runs(self, inst: str, status: str) -> list:
		return frappe.get_all(
			RUN,
			filters={"installation": inst, "status": status},
			order_by="creation asc",
			pluck="name",
			ignore_permissions=True,
		)

	def _dispatched_for(self, inst: str) -> list:
		"""Dispatch calls that belong to THIS installation. The sweep is site-wide and
		CI runs four shards against one site, so a foreign installation falling due
		mid-test must never read as a second audit of ours."""
		mine = set(frappe.get_all(RUN, filters={"installation": inst}, pluck="name", ignore_permissions=True))
		return [kw for kw in self.dispatched if kw.get("run_id") in mine]

	def _next_run_at(self, inst: str):
		return frappe.db.get_value(INSTALLATION, inst, "next_run_at")


class TestScheduledSlotIsDispatchedOnce(DispatchIdempotencyTestCase):
	"""The reported defect and its immediate neighbours."""

	def test_a_crash_after_the_launch_does_not_re_dispatch_the_completed_slot(self):
		"""THE regression. The launch succeeds, the sweep dies before the schedule can
		be advanced, the audit finishes normally within the hour, and the next tick
		finds the slot still due. A guard keyed on a live run cannot see this: by then
		the run is ``completed``."""
		inst = self._due_install()

		self._sweep(launch=self._launch_then_crash())
		launched = self._launched(inst)
		self.assertEqual(len(launched), 1, "the first tick launches exactly one audit")
		self.assertEqual(len(self._dispatched_for(inst)), 1)

		# The writeback lands: the audit is done, minutes into the hour.
		frappe.db.set_value(RUN, launched[0], "status", "completed", update_modified=False)
		frappe.db.commit()

		self._sweep()  # the next hourly tick
		self.assertEqual(
			self._launched(inst), launched, "the slot was already dispatched; it must not run again"
		)
		self.assertEqual(len(self._dispatched_for(inst)), 1, "no second audit reaches the container")

	def test_a_crash_after_the_launch_is_not_reported_as_a_failure_to_start(self):
		"""The customer's audit IS running, so a ``failed`` run and a "could not be
		started" alert would both be untrue."""
		inst = self._due_install()

		self._sweep(launch=self._launch_then_crash())

		self.assertEqual(self._runs(inst, "failed"), [], "a live audit is not a failed one")
		self.assertEqual(
			frappe.get_all(NOTIFICATION, filters={"for_user": self.owner}, pluck="name"),
			[],
			"the owner is not told a run that started could not start",
		)

	def test_a_second_cron_worker_with_a_stale_due_snapshot_does_not_re_dispatch(self):
		"""Two workers read the same due row; one dispatches and consumes the slot. The
		other is still holding its snapshot, and by the time it acts the first audit has
		finished, so nothing about the RUNS tells it the slot is spent. Only the durable
		slot claim does."""
		inst = self._due_install()
		stale = self._due_row(inst)  # what both workers read

		self._sweep()
		launched = self._launched(inst)
		self.assertEqual(len(launched), 1)
		frappe.db.set_value(RUN, launched[0], "status", "completed", update_modified=False)
		frappe.db.commit()

		with self._delegate_stubbed():
			agent_scheduler._sweep_one(stale, now_datetime(), "Administrator", set())

		self.assertEqual(self._launched(inst), launched, "the stale worker must not re-dispatch")
		self.assertEqual(len(self._dispatched_for(inst)), 1)

	def test_a_long_audit_spanning_a_tick_is_not_double_launched(self):
		"""Control: the ordinary healthy path. The audit is still ``running`` when the
		next tick fires, and the slot is not due again until tomorrow."""
		inst = self._due_install()

		self._sweep()
		launched = self._launched(inst)
		self.assertEqual(len(launched), 1)
		self.assertEqual(frappe.db.get_value(RUN, launched[0], "status"), "running")

		self._sweep()
		self.assertEqual(self._launched(inst), launched)
		self.assertEqual(frappe.db.get_value(RUN, launched[0], "status"), "running")

	def test_a_dispatch_that_fails_for_real_leaves_the_slot_due_and_nothing_running(self):
		"""The launch reaches the fleet call and THAT fails, which is a different shape
		from the whole launch failing: the run row is already committed, so
		``_launch_audit`` stamps it ``failed`` itself and re-raises. Nothing is running,
		so the slot is genuinely unspent and must go back.

		Known and deliberately not asserted as exactly-one: this attempt leaves TWO
		``failed`` rows, one from the launch's own writeback and one from the sweep's
		skip record. That predates the dispatch claim. What matters here is that no run
		is left alive and the slot returns."""
		inst = self._due_install()
		now = now_datetime()

		def _unreachable(**kw):
			raise RuntimeError("admin unreachable")

		self._sweep(dispatch=_unreachable)

		self.assertEqual(self._runs(inst, "running"), [], "a failed dispatch leaves nothing alive")
		errors = frappe.get_all(
			RUN,
			filters={"installation": inst, "status": "failed"},
			fields=["error"],
			ignore_permissions=True,
		)
		self.assertTrue(errors, "the failure is recorded for the customer")
		self.assertTrue(
			any("dispatch failed" in (e["error"] or "") for e in errors),
			f"the real dispatch failure is named: {errors}",
		)
		self.assertTrue(
			frappe.get_all(NOTIFICATION, filters={"for_user": self.owner}, pluck="name"),
			"the owner is told",
		)
		self.assertLessEqual(self._next_run_at(inst), now, "the slot goes back for a retry")

	def test_a_genuine_launch_failure_records_notifies_and_leaves_the_slot_due(self):
		"""Control: nothing durable was created, so the customer must still be told and
		the slot must stay due for the next hour."""
		inst = self._due_install()
		now = now_datetime()

		def _boom(inst_doc, **kw):
			raise RuntimeError("enqueue exploded")

		self._sweep(launch=_boom)

		failed = frappe.get_all(
			RUN,
			filters={"installation": inst, "status": "failed"},
			fields=["error"],
			ignore_permissions=True,
		)
		self.assertEqual(len(failed), 1, "a real launch failure is recorded for the customer")
		self.assertIn("enqueue failed", failed[0]["error"])
		self.assertTrue(
			frappe.get_all(NOTIFICATION, filters={"for_user": self.owner}, pluck="name"),
			"the owner is told the run could not be started",
		)
		self.assertLessEqual(self._next_run_at(inst), now, "the slot stays due for a retry")
		self.assertEqual(self._dispatched_for(inst), [], "nothing reached the container")


class TestLaunchAtomicity(DispatchIdempotencyTestCase):
	"""A launch that raises mid-way must leave NOTHING behind. Its rows used to sit
	pending in the transaction until ``_record_failed``'s own commit flushed them,
	which is how a failed launch produced a phantom in-flight run."""

	@staticmethod
	def _mid_launch_failure():
		"""Raises AFTER the conversation and the ``running`` run are inserted and
		BEFORE the launch commits: the exact window that produced the orphan."""
		return patch.object(
			agent_scheduler, "_mint_run_session", side_effect=RuntimeError("session mint blew up")
		)

	def test_a_mid_launch_failure_leaves_no_orphaned_running_run(self):
		inst = self._due_install()

		with self._mid_launch_failure():
			self._sweep()

		self.assertEqual(self._runs(inst, "running"), [], "no phantom in-flight run")
		self.assertEqual(len(self._runs(inst, "failed")), 1, "the failure is recorded instead")
		self.assertEqual(
			frappe.get_all(CONV, filters={"owner": self.owner}, pluck="name"),
			[],
			"and no orphan conversation either",
		)

	def test_the_next_tick_reports_the_failure_again_rather_than_skipping_it(self):
		"""An orphan would make every later tick believe an audit was in flight, so the
		slot would be silently skipped and advanced, hiding a real failure until the 3h
		reaper fired."""
		inst = self._due_install()

		with self._mid_launch_failure():
			self._sweep()
			self._sweep()

		self.assertEqual(len(self._runs(inst, "failed")), 2, "the second tick reports it too")
		self.assertEqual(self._runs(inst, "running"), [])

	def test_a_wedged_run_does_not_suppress_a_later_due_slot(self):
		"""A run stuck ``running`` past the stale-run reaper's cutoff is not in flight,
		it is dead, and treating it as live would skip real slots for up to three hours.
		This is why dispatch is guarded by a durable slot claim and not by run status."""
		inst = self._due_install()
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"agent": SLUG,
				"installation": inst,
				"trigger": "scheduled",
				"status": "running",
				"started_at": add_to_date(
					now_datetime(), seconds=-(agent_scheduler.STALE_RUN_AFTER_SECONDS + 600)
				),
			}
		)
		run.flags.ignore_permissions = True
		run.insert(ignore_permissions=True)
		frappe.db.commit()

		self._sweep()

		self.assertEqual(len(self._dispatched_for(inst)), 1, "today's slot still runs")
		self.assertEqual(len(self._launched(inst)), 2, "the wedged row plus the new audit")


class TestManualAndScheduledCannotInterleave(DispatchIdempotencyTestCase):
	"""``run_agent_now`` and the sweep share one installation and one budget. Both used
	to check "is anything running?" and act on the answer with nothing serializing the
	two, so a Run Now landing in the same tick as the cron launched a second audit."""

	def test_a_sweep_firing_during_a_manual_launch_does_not_launch_a_second_audit(self):
		"""The hourly tick arrives while the manual run is still being dispatched: the
		manual run's row is already committed ``running``, the slot is still due."""
		inst = self._due_install()
		reentered = []

		def _dispatch(**kw):
			if not reentered:
				reentered.append(True)
				self._sweep_as_administrator()
			return self._dispatch_stub(**kw)

		self._run_now(inst, dispatch=_dispatch)

		self.assertTrue(reentered, "the re-entrant tick must actually have run")
		self.assertEqual(len(self._launched(inst)), 1, "exactly one audit for the two triggers")

	def test_a_manual_run_racing_the_sweep_is_refused_rather_than_launched(self):
		"""The mirror ordering: the cron is mid-dispatch when the customer clicks Run
		Now. One of the two has to lose, and it cannot be the scheduled slot, which has
		already been claimed."""
		inst = self._due_install()
		outcome = []

		def _dispatch(**kw):
			if not outcome:
				outcome.append("pending")
				try:
					self._run_now(inst)
					outcome[0] = "launched"
					outcome.append(None)
				except Exception as e:
					outcome[0] = f"refused: {e}"
					outcome.append(type(e))
			return self._dispatch_stub(**kw)

		self._sweep(dispatch=_dispatch)

		self.assertTrue(outcome and outcome[0] != "pending", "the manual run must have been attempted")
		self.assertTrue(outcome[0].startswith("refused"), f"the manual run was not refused: {outcome[0]}")
		# Refused for the RIGHT reason: any old crash would also read as "refused".
		self.assertEqual(outcome[1], frappe.ValidationError)
		self.assertIn("already", outcome[0])
		self.assertEqual(len(self._launched(inst)), 1, "exactly one audit for the two triggers")

	def test_a_manual_run_is_refused_while_its_own_audit_is_still_running(self):
		"""Two concurrent audits of one installation are two bills for one answer, so
		the second trigger is refused whichever surface it came from."""
		inst = self._due_install()
		self._run_now(inst)
		self.assertEqual(len(self._launched(inst)), 1)

		with self.assertRaises(frappe.ValidationError) as cm:
			self._run_now(inst)
		self.assertIn("already running", str(cm.exception))
		self.assertEqual(len(self._launched(inst)), 1)

	def test_a_tick_arriving_during_a_live_manual_audit_consumes_the_slot(self):
		"""The non-racing overlap, which is the common one: the manual run started a
		minute ago and is still going when the hourly tick fires. Nothing is contended,
		the lock is free, and the tick has to decide on its own. It must not queue a
		second audit of the same books, it must consume the slot rather than busy-retry
		every hour behind a long audit, and the deferral has to be visible: every other
		skip in this sweep records its reason, so a customer never finds a scheduled
		audit that simply did not appear."""
		inst = self._due_install()
		self._run_now(inst)
		self.assertEqual(len(self._launched(inst)), 1)
		now = now_datetime()

		self._sweep()

		self.assertEqual(len(self._launched(inst)), 1, "no second concurrent audit")
		self.assertEqual(len(self._dispatched_for(inst)), 1)
		self.assertGreater(self._next_run_at(inst), now, "the slot is consumed, not left due")
		skipped = frappe.get_all(
			RUN,
			filters={"installation": inst, "status": "failed"},
			fields=["error"],
			ignore_permissions=True,
		)
		self.assertEqual(len(skipped), 1, "the deferral is recorded for the customer")
		self.assertIn("already running", skipped[0]["error"])

	def test_a_manual_run_is_allowed_again_once_the_audit_finishes(self):
		"""Control: the refusal is about CONCURRENCY, never a lockout. A finished audit
		leaves the button working."""
		inst = self._due_install()
		self._run_now(inst)
		frappe.db.set_value(RUN, self._launched(inst)[0], "status", "completed", update_modified=False)
		frappe.db.commit()

		self._run_now(inst)
		self.assertEqual(len(self._launched(inst)), 2)


class TestAmbiguousDispatchIsNotRetriedAsANewRun(DispatchIdempotencyTestCase):
	"""#743: a dispatch that TIMES OUT (admin may already be running the turn) must not
	terminalize the run, must not hand the slot back, and must not mint a fresh run id
	an hour later - that fresh id is what bypassed the fleet's run-id idempotency and
	billed the customer for two audits of one slot. The run is left ``running`` for the
	3h stale-run reaper to arbitrate."""

	def _ambiguous(self, **kw):
		self.dispatched.append(kw)
		raise admin_client.AdminAmbiguousError("admin did not answer in time; may be running")

	def test_an_ambiguous_dispatch_leaves_the_run_running_and_claims_the_slot(self):
		inst = self._due_install()
		now = now_datetime()

		self._sweep(dispatch=self._ambiguous)

		running = self._runs(inst, "running")
		self.assertEqual(len(running), 1, "the timed-out run is left running for the reaper")
		self.assertEqual(self._runs(inst, "failed"), [], "an ambiguous dispatch is not recorded as a failure")
		self.assertEqual(len(self._dispatched_for(inst)), 1, "exactly one dispatch attempt")
		self.assertEqual(
			frappe.get_all(NOTIFICATION, filters={"for_user": self.owner}, pluck="name"),
			[],
			"the owner is not told a run that may be running could not start",
		)
		self.assertGreater(self._next_run_at(inst), now, "the slot is CLAIMED, never handed back")

	def test_a_later_tick_does_not_mint_a_second_run_for_the_still_running_slot(self):
		"""THE money assertion. Even if the slot is forced due again, the still-running
		ambiguous run blocks a second audit (via ``_live_run``): no new run id, no second
		dispatch reaches the container."""
		inst = self._due_install()

		self._sweep(dispatch=self._ambiguous)
		running = self._runs(inst, "running")
		self.assertEqual(len(running), 1)

		# Force the slot due again and run a HEALTHY tick: prove the running run - not
		# merely the advanced schedule - is what stops the duplicate.
		frappe.db.set_value(
			INSTALLATION, inst, "next_run_at", add_days(now_datetime(), -1), update_modified=False
		)
		frappe.db.commit()
		self._sweep()

		self.assertEqual(self._runs(inst, "running"), running, "the same single run; no new id minted")
		self.assertEqual(len(self._dispatched_for(inst)), 1, "no second audit reaches the container")

	def test_a_confirmed_refusal_still_records_notifies_and_returns_the_slot(self):
		"""Control (unchanged behavior): a CONFIRMED refusal - a base AdminUnreachableError,
		not the ambiguous subclass - is terminalized, notified and retried exactly as before."""
		inst = self._due_install()
		now = now_datetime()

		def _refused(**kw):
			self.dispatched.append(kw)
			raise admin_client.AdminUnreachableError("admin returned a 400 error")

		self._sweep(dispatch=_refused)

		self.assertEqual(self._runs(inst, "running"), [], "a confirmed refusal leaves nothing alive")
		self.assertTrue(
			frappe.get_all(RUN, filters={"installation": inst, "status": "failed"}, ignore_permissions=True),
			"the failure is recorded for the customer",
		)
		self.assertTrue(
			frappe.get_all(NOTIFICATION, filters={"for_user": self.owner}, pluck="name"),
			"the owner is told the run could not start",
		)
		self.assertLessEqual(self._next_run_at(inst), now, "the slot goes back for a retry")


class TestAmbiguousDispatchManualPath(DispatchIdempotencyTestCase):
	"""#743: the manual ``run_agent_now`` path must behave the SAME as the scheduled path
	for both outcomes - an ambiguous dispatch leaves the run running (a re-click is
	refused), a confirmed refusal fails it (a re-click is allowed)."""

	def test_a_manual_ambiguous_dispatch_leaves_the_run_running_and_blocks_a_retry(self):
		inst = self._due_install()

		def _ambiguous(**kw):
			self.dispatched.append(kw)
			raise admin_client.AdminAmbiguousError("admin did not answer in time; may be running")

		with self.assertRaises(admin_client.AdminAmbiguousError):
			self._run_now(inst, dispatch=_ambiguous)

		self.assertEqual(len(self._runs(inst, "running")), 1, "the run is left running, not failed")
		self.assertEqual(self._runs(inst, "failed"), [], "an ambiguous dispatch is not a failure")

		# The customer clicks again: the live run must refuse it, so the timed-out turn
		# admin may already be running is never dispatched a second time.
		with self.assertRaises(frappe.ValidationError) as cm:
			self._run_now(inst)
		self.assertIn("already running", str(cm.exception))
		self.assertEqual(len(self._dispatched_for(inst)), 1, "the second click never reaches the container")

	def test_a_manual_confirmed_refusal_fails_the_run_and_allows_a_retry(self):
		inst = self._due_install()

		def _refused(**kw):
			self.dispatched.append(kw)
			raise admin_client.AdminUnreachableError("admin returned a 400 error")

		with self.assertRaises(admin_client.AdminUnreachableError):
			self._run_now(inst, dispatch=_refused)

		self.assertEqual(self._runs(inst, "running"), [], "a confirmed refusal leaves nothing alive")
		self.assertEqual(len(self._runs(inst, "failed")), 1, "the run is recorded failed")

		# Unchanged: a confirmed refusal is retryable, so the next click starts a fresh audit.
		self._run_now(inst)
		self.assertEqual(len(self._launched(inst)), 1, "the retry starts a fresh audit")


class TestReaperDoesNotRelabelAnAmbiguousRun(DispatchIdempotencyTestCase):
	"""#743 bullet 4: whatever an ambiguous run is left as, the stale-run reaper must not
	relabel a REAL outcome. A run admin actually completed (record_agent_run lands within
	the hour) stays completed; a run that truly never started is the reaper's to fail."""

	def _stale_running_run(self, inst: str) -> str:
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"agent": SLUG,
				"installation": inst,
				"trigger": "scheduled",
				"status": "running",
				"started_at": add_to_date(
					now_datetime(), seconds=-(agent_scheduler.STALE_RUN_AFTER_SECONDS + 600)
				),
			}
		)
		run.flags.ignore_permissions = True
		run.insert(ignore_permissions=True)
		frappe.db.commit()
		return run.name

	def test_a_completed_writeback_is_never_relabeled_failed(self):
		inst = self._due_install()
		run = self._stale_running_run(inst)
		# The writeback lands: admin DID run the turn, minutes into the hour.
		frappe.db.set_value(RUN, run, "status", "completed", update_modified=False)
		frappe.db.commit()

		agent_scheduler.reap_stale_agent_runs()

		self.assertEqual(
			frappe.db.get_value(RUN, run, "status"), "completed", "the reaper never overwrites a real outcome"
		)

	def test_a_still_running_ambiguous_run_is_the_reapers_to_fail(self):
		inst = self._due_install()
		run = self._stale_running_run(inst)

		agent_scheduler.reap_stale_agent_runs()

		self.assertEqual(
			frappe.db.get_value(RUN, run, "status"),
			"failed",
			"a run that truly never started is failed by the 3h backstop, the single arbiter",
		)
