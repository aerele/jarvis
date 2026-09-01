"""#1061: an agent run must be STOPPABLE and must never sit ``running`` forever.

Three lifecycle holes, one module:

  * **A1** — ``stopped`` was not a Jarvis Agent Run status at all, so there was no
    honest terminal state for "an operator ended this early". The only way to clear a
    wedged run was to let the 3h reaper relabel it as a duration timeout it never hit.
  * **A2** — ``stop_agent_run`` is the operator's soft stop: it terminalizes the row,
    revokes the run's session bearer (so a late writeback is inert) and best-effort
    aborts the gateway session.
  * **A3** — ``poll_dispatched_runs`` closes the loop the other way. The fleet already
    knows a run failed or finished; before this the bench learned that only from the
    delegate's own writeback, so a delegate that died after dispatch left the row
    ``running`` for three hours (jarvis#1058).

The concurrency contract these tests defend is the one the reaper already had: every
terminal transition is a COMPARE-AND-SET on ``status='running'`` under a row lock, so
a bench-side stop always wins over a later fleet flip and no sweep ever overwrites a
real outcome.

Run:
  bench --site test_site run-tests --app jarvis \
    --module jarvis.tests.test_agent_run_lifecycle
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, now_datetime

from jarvis import admin_client
from jarvis.chat import agent_scheduler, agents_api

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
ACTIVITY = "Jarvis Agent Activity"
SESSION = "Jarvis Chat Session"

SLUG = "run-lifecycle-auditor"
SCRIBE_SLUG = "run-lifecycle-scribe"
OWNER = "run-lifecycle-owner@example.com"
STRANGER = "run-lifecycle-stranger@example.com"


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


def _mk_listing(slug: str, nature: str) -> str:
	if not frappe.db.exists(LISTING, slug):
		frappe.get_doc(
			{
				"doctype": LISTING,
				"agent_slug": slug,
				"title": f"Run lifecycle {nature.lower()}",
				"rule_tokens": json.dumps(["tok"]),
				"doctypes_required": json.dumps([]),
				"rule_pack": f"pack-{slug}",
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value(LISTING, slug, {"status": "Published", "nature": nature}, update_modified=False)
	return slug


def _wipe() -> None:
	"""Everything these tests commit. The endpoints under test commit, so
	FrappeTestCase's own rollback cannot reclaim any of it."""
	for slug in (SLUG, SCRIBE_SLUG):
		for name in frappe.get_all(RUN, filters={"agent": slug}, pluck="name", ignore_permissions=True):
			frappe.delete_doc(RUN, name, force=True, ignore_permissions=True)
		for name in frappe.get_all(ACTIVITY, filters={"agent": slug}, pluck="name", ignore_permissions=True):
			frappe.delete_doc(ACTIVITY, name, force=True, ignore_permissions=True)
		for name in frappe.get_all(
			INSTALLATION, filters={"agent": slug}, pluck="name", ignore_permissions=True
		):
			frappe.delete_doc(INSTALLATION, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(
		SESSION, filters={"session_key": ["like", "agent:agent-run-lifecycle%"]}, pluck="name"
	):
		frappe.delete_doc(SESSION, name, force=True, ignore_permissions=True)
	frappe.db.commit()


class RunLifecycleTestCase(FrappeTestCase):
	"""One published auditor + one published scribe, each installed for one owner."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user(OWNER)
		cls.stranger = _mk_user(STRANGER)
		_mk_listing(SLUG, "Auditor")
		_mk_listing(SCRIBE_SLUG, "Scribe")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()
		_mk_listing(SLUG, "Auditor")
		_mk_listing(SCRIBE_SLUG, "Scribe")
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe()

	# ------------------------------------------------------------------ #
	# fixtures
	# ------------------------------------------------------------------ #
	def _install(self, slug: str = SLUG) -> str:
		doc = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": slug,
				"run_as_user": self.owner,
				"reviewer": self.owner,
				"enabled": 1,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(INSTALLATION, doc.name, "owner", self.owner, update_modified=False)
		# A claimed schedule slot (#672): _claim_slot advanced next_run_at BEFORE the
		# dispatch, so these are the values a terminalization must leave untouched.
		frappe.db.set_value(
			INSTALLATION,
			doc.name,
			{
				"schedule_enabled": 1,
				"schedule_frequency": "daily",
				"last_run_at": now_datetime(),
				"next_run_at": add_days(now_datetime(), 1),
			},
			update_modified=False,
		)
		frappe.db.commit()
		return doc.name

	def _run(self, inst: str, *, slug: str = SLUG, status: str = "running", age_s: int = 0, **extra) -> str:
		"""A committed run row, optionally aged so a sweep's cutoff sees it."""
		started = add_to_date(now_datetime(), seconds=-age_s)
		doc = frappe.get_doc(
			{
				"doctype": RUN,
				"agent": slug,
				"installation": inst,
				"trigger": "manual",
				"status": "running",
				"started_at": started,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		session_key = f"agent:agent-{slug}:{doc.name}"
		frappe.get_doc({"doctype": SESSION, "session_key": session_key, "user": self.owner}).insert(
			ignore_permissions=True
		)
		values = {"owner": self.owner, "session_key": session_key, "started_at": started, **extra}
		if status != "running":
			values["status"] = status
		frappe.db.set_value(RUN, doc.name, values, update_modified=False)
		frappe.db.commit()
		return doc.name

	def _status(self, run: str) -> str:
		return frappe.db.get_value(RUN, run, "status")

	def _slot(self, inst: str) -> dict:
		return frappe.db.get_value(INSTALLATION, inst, ["next_run_at", "last_run_at"], as_dict=True)


# --------------------------------------------------------------------------- #
# A1 — ``stopped`` is a real, terminal run status
# --------------------------------------------------------------------------- #
class TestStoppedStatusIsTerminal(RunLifecycleTestCase):
	def test_doctype_offers_stopped(self):
		options = frappe.get_meta(RUN).get_field("status").options.split("\n")
		self.assertIn("stopped", options)

	def test_the_accepted_status_list_matches_the_doctype(self):
		"""``agents_api._RUN_STATUSES`` is hand-maintained and is what the Runs page
		validates a filter against, so a status added to the DocType and forgotten here
		is a value the customer can see on a row but never filter by — and one removed
		from the DocType and left here is a filter that silently matches nothing."""
		options = {o.strip() for o in frappe.get_meta(RUN).get_field("status").options.split("\n")}
		self.assertEqual(set(agents_api._RUN_STATUSES), options - {""})

	def test_run_history_filter_accepts_stopped(self):
		"""The Runs page must be able to filter on the new status — an accepted value
		the list endpoint refuses is a status the customer can never look up."""
		inst = self._install()
		run = self._run(inst, status="stopped")
		frappe.set_user(self.owner)
		try:
			rows = agents_api.list_runs_page(status="stopped")["rows"]
		finally:
			frappe.set_user("Administrator")
		self.assertIn(run, [r["name"] for r in rows])

	def test_stopped_run_is_not_live(self):
		"""#672's liveness guard is keyed on ``status='running'``, so a stopped run must
		not keep the installation's Run-now button (and the cron sweep) locked out."""
		inst = self._install()
		self._run(inst, status="stopped")
		self.assertIsNone(agent_scheduler._live_run(inst))

	def test_stopped_run_is_not_a_reaper_candidate(self):
		"""The 3h backstop must never relabel an operator stop as a duration timeout."""
		inst = self._install()
		run = self._run(inst, status="stopped", age_s=agent_scheduler.STALE_RUN_AFTER_SECONDS + 60)
		cutoff = add_to_date(now_datetime(), seconds=-agent_scheduler.STALE_RUN_AFTER_SECONDS)
		self.assertNotIn(run, [r.name for r in agent_scheduler._stale_candidates(cutoff)])


# --------------------------------------------------------------------------- #
# A2 — stop_agent_run
# --------------------------------------------------------------------------- #
class TestStopAgentRun(RunLifecycleTestCase):
	def _stop(self, run: str, *, as_user: str | None = None) -> dict:
		"""Call the endpoint with the gateway abort stubbed out — it is best-effort and
		its own test covers it; every other assertion here is about the durable state."""
		user = as_user or self.owner
		frappe.set_user(user)
		try:
			with patch.object(agents_api, "_try_abort_gateway_session"):
				return agents_api.stop_agent_run(run)
		finally:
			frappe.set_user("Administrator")

	def test_stop_terminalizes_a_running_run(self):
		inst = self._install()
		run = self._run(inst)
		out = self._stop(run)

		self.assertEqual(out["status"], "stopped")
		row = frappe.db.get_value(RUN, run, ["status", "finished_at", "error"], as_dict=True)
		self.assertEqual(row.status, "stopped")
		self.assertTrue(row.finished_at)
		self.assertIn("Stopped by operator", row.error)

	def test_stop_revokes_the_run_session_bearer(self):
		"""The bearer must not outlive the run: a late tool call from a delegate that is
		still winding down must resolve no identity and 401, so nothing it does can write
		back onto the stopped run."""
		inst = self._install()
		run = self._run(inst)
		key = frappe.db.get_value(RUN, run, "session_key")
		self.assertTrue(frappe.db.exists(SESSION, {"session_key": key}))
		self._stop(run)
		self.assertFalse(frappe.db.exists(SESSION, {"session_key": key}))

	def test_stop_logs_the_activity(self):
		inst = self._install()
		run = self._run(inst)
		self._stop(run)
		rows = frappe.get_all(
			ACTIVITY, filters={"run": run}, fields=["action", "owner"], ignore_permissions=True
		)
		self.assertEqual([r.action for r in rows], ["run_stopped"])
		self.assertEqual(rows[0].owner, self.owner)

	def test_stop_leaves_the_claimed_schedule_slot_consumed(self):
		"""SLOT PARITY with the reaper: ``_claim_slot`` spent the slot BEFORE dispatch and
		no terminalization hands it back, so a stopped run must not resurrect it — that
		would re-dispatch the same slot on the next hourly tick."""
		inst = self._install()
		before = self._slot(inst)
		run = self._run(inst)
		self._stop(run)
		self.assertEqual(self._slot(inst), before)

	def test_a_stopped_run_still_counts_against_the_monthly_budget(self):
		"""Deliberately UNLIKE ``failed`` (which A14 excludes so a skip path cannot make
		the cap self-perpetuating): a stopped run really occupied the container, so
		start-then-stop must not be an unlimited free-run loop around the budget."""
		inst = self._install()
		run = self._run(inst)
		self._stop(run)
		self.assertEqual(agent_scheduler._runs_this_month(installation=inst), 1)

	def test_stop_is_idempotent_on_a_terminal_run(self):
		"""A second click, or a stop racing a finish, must report the state the run
		actually reached and never overwrite it."""
		inst = self._install()
		run = self._run(inst, status="completed")
		out = self._stop(run)
		self.assertEqual(out, {"ok": True, "status": "completed", "idempotent": True})
		self.assertEqual(self._status(run), "completed")

	def test_stop_refuses_a_non_owner(self):
		inst = self._install()
		run = self._run(inst)
		with self.assertRaises(frappe.PermissionError):
			self._stop(run, as_user=self.stranger)
		self.assertEqual(self._status(run), "running")

	def test_a_broken_gateway_abort_never_undoes_the_stop(self):
		"""The soft stop is the guarantee; the gateway abort is opportunistic. The run
		lane is dispatched with ``--expect-final`` rather than as a chat session, so this
		call is expected to be a no-op in production — it must never raise out."""
		inst = self._install()
		run = self._run(inst)

		class _Boom:
			def __enter__(self):
				raise RuntimeError("gateway down")

			def __exit__(self, *a):
				return False

		frappe.set_user(self.owner)
		try:
			with patch("jarvis.chat.agent_session_pool.checkout", return_value=_Boom()):
				out = agents_api.stop_agent_run(run)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(out["status"], "stopped")
		self.assertEqual(self._status(run), "stopped")

	def test_a_late_writeback_has_no_run_to_land_on(self):
		"""``record_agent_run`` resolves its run SOLELY by the session_key on its bearer
		row, and refuses anything that is not ``running``. Both doors are shut after a
		stop: the bearer row is gone (so the lookup finds no run at all) and the status
		is terminal (so even a surviving bearer would hit the idempotency branch)."""
		inst = self._install()
		run = self._run(inst)
		key = frappe.db.get_value(RUN, run, "session_key")
		self._stop(run)
		self.assertFalse(frappe.db.exists(SESSION, {"session_key": key}))
		self.assertNotEqual(self._status(run), "running")


# --------------------------------------------------------------------------- #
# A3 — poll_dispatched_runs
# --------------------------------------------------------------------------- #
# Comfortably past POLL_GRACE_SECONDS, so the row is a candidate for the sweep.
DISPATCHED_S = agent_scheduler.POLL_GRACE_SECONDS + 180


class TestPollDispatchedRuns(RunLifecycleTestCase):
	def _poll(self, states: dict, default=None) -> list:
		"""Run the sweep with the admin relay stubbed per run id. Returns the run ids it
		was asked about, so a test can assert a run was NEVER polled (or count the calls
		the circuit breaker allowed).

		``default`` answers every run not named in ``states``, and defaults to "still
		running" — this is a shared site and other modules leave ``running`` rows behind,
		which the site-wide sweep would otherwise judge on this test's fixture."""
		asked: list = []

		def _status(run_id):
			asked.append(run_id)
			state = states.get(run_id, default if default is not None else {"status": "running"})
			if isinstance(state, Exception):
				raise state
			return state

		with patch.object(admin_client, "get_agent_run_status", side_effect=_status):
			self.terminalized = agent_scheduler.poll_dispatched_runs()
		return asked

	def _relayed(self, state: dict) -> dict:
		"""Admin's own success envelope around the fleet run-state — the shape the bench
		really receives (``api/_responses._ok``), one level deeper than
		``admin_client.get_agent_run_status``'s docstring reads."""
		return {"ok": True, "data": state}

	def _fleet_failed(self, message: str) -> dict:
		return self._relayed({"status": "failed", "error": {"code": "run_failed", "message": message}})

	def _fleet_completed(self, *, finished_ago_s: float) -> dict:
		# The fleet stamps finished_at as a UNIX epoch float.
		return self._relayed(
			{"status": "completed", "finished_at": time.time() - finished_ago_s, "error": None}
		)

	# ------------------------------------------------------------------ #
	def test_a_fleet_failure_is_surfaced_with_its_real_message(self):
		"""The whole point of polling: the customer reads what actually broke, not the
		reaper's "exceeded max duration" three hours later."""
		inst = self._install()
		run = self._run(inst, age_s=DISPATCHED_S)
		self._poll({run: self._fleet_failed("delegate exec died: exit 137")})

		row = frappe.db.get_value(RUN, run, ["status", "error", "finished_at"], as_dict=True)
		self.assertEqual(row.status, "failed")
		self.assertIn("exit 137", row.error)
		self.assertTrue(row.finished_at)

	def test_a_young_run_is_never_polled(self):
		"""A fresh dispatch is left alone entirely — no admin round trip per run per
		tick while the delegate is still starting up."""
		inst = self._install()
		run = self._run(inst, age_s=10)
		asked = self._poll({run: self._fleet_failed("should never be read")})
		self.assertNotIn(run, asked)
		self.assertEqual(self._status(run), "running")

	def test_a_stopped_run_is_never_reopened_by_a_later_fleet_flip(self):
		"""The bench stop is authoritative. The fleet may report the same run failed
		seconds later; the compare-and-set on ``status='running'`` is what keeps the
		operator's verdict."""
		inst = self._install()
		run = self._run(inst, age_s=DISPATCHED_S, status="stopped")
		self._poll({run: self._fleet_failed("late fleet failure")})
		self.assertEqual(self._status(run), "stopped")

	def test_an_unreachable_admin_leaves_every_run_alone(self):
		"""A transport fault is never a verdict — the 3h reaper stays the backstop."""
		from jarvis.exceptions import AdminUnreachableError

		inst = self._install()
		run = self._run(inst, age_s=DISPATCHED_S)
		self._poll({run: AdminUnreachableError("admin is down")})
		self.assertEqual(self._status(run), "running")

	def test_a_lost_run_record_is_left_alone_while_young(self):
		"""A 404 from the host may just mean the state file is not visible yet
		(container mid-restart), so a young run is not condemned on it."""
		from jarvis.exceptions import AdminUnreachableError

		inst = self._install()
		run = self._run(inst, age_s=DISPATCHED_S)
		self._poll({run: AdminUnreachableError("unknown agent run RUN-1")})
		self.assertEqual(self._status(run), "running")

	def test_a_lost_run_record_fails_the_run_once_it_is_old(self):
		from jarvis.exceptions import AdminUnreachableError

		inst = self._install()
		run = self._run(inst, age_s=agent_scheduler.MISSING_RECORD_FALLBACK_SECONDS + 60)
		self._poll({run: AdminUnreachableError("unknown agent run RUN-1")})

		row = frappe.db.get_value(RUN, run, ["status", "error"], as_dict=True)
		self.assertEqual(row.status, "failed")
		self.assertIn("no record of this run", row.error)

	def test_a_just_finished_run_is_given_its_writeback_grace(self):
		"""Anchored on the HOST's finish time, not the run's age: this run is hours old
		and finished ten seconds ago, so its writeback is still plausibly in flight."""
		inst = self._install()
		run = self._run(inst, age_s=agent_scheduler.STALE_RUN_AFTER_SECONDS - 60)
		self._poll({run: self._fleet_completed(finished_ago_s=10)})
		self.assertEqual(self._status(run), "running")

	def test_a_finished_run_whose_writeback_never_came_fails_honestly(self):
		inst = self._install()
		run = self._run(inst, age_s=DISPATCHED_S)
		self._poll({run: self._fleet_completed(finished_ago_s=agent_scheduler.WRITEBACK_GRACE_SECONDS + 60)})

		row = frappe.db.get_value(RUN, run, ["status", "error"], as_dict=True)
		self.assertEqual(row.status, "failed")
		self.assertIn("delegate finished without recording results", row.error)

	def test_a_scribe_with_pages_is_completed_not_failed(self):
		"""CA-4: the pages are already durably written, so a scribe that merely forgot to
		call finish is an honest SUCCESS. The poll must reach the same ruling the reaper
		does — they share the transition helper precisely so they cannot diverge."""
		inst = self._install(SCRIBE_SLUG)
		run = self._run(inst, slug=SCRIBE_SLUG, age_s=DISPATCHED_S, pages_written=4)
		self._poll({run: self._fleet_completed(finished_ago_s=agent_scheduler.WRITEBACK_GRACE_SECONDS + 60)})
		self.assertEqual(self._status(run), "completed")

	def test_a_scribe_with_a_zero_tally_is_rebuilt_from_page_provenance(self):
		"""CA2-3: a stored tally of 0 is rebuilt from the pages themselves before any
		verdict, so real work is never failed away on a false zero."""
		inst = self._install(SCRIBE_SLUG)
		run = self._run(inst, slug=SCRIBE_SLUG, age_s=DISPATCHED_S, pages_written=0)
		meta = {"count": 2, "pages": [{"slug": "a", "title": "A"}, {"slug": "b", "title": "B"}]}
		with patch("jarvis.tools.record_app_wiki.reconcile_run_pages", return_value=meta) as recon:
			self._poll(
				{run: self._fleet_completed(finished_ago_s=agent_scheduler.WRITEBACK_GRACE_SECONDS + 60)}
			)
		recon.assert_called_once_with(run)

		row = frappe.db.get_value(RUN, run, ["status", "pages_written"], as_dict=True)
		self.assertEqual(row.status, "completed")
		self.assertEqual(row.pages_written, 2)

	def test_a_still_running_fleet_state_is_left_alone(self):
		inst = self._install()
		run = self._run(inst, age_s=DISPATCHED_S)
		self._poll({run: self._relayed({"status": "running"})})
		self.assertEqual(self._status(run), "running")

	def test_an_unenveloped_run_state_is_read_too(self):
		"""The relay shape is a cross-repo contract. Misreading the wrapper would leave
		every poll seeing a blank status and terminalizing nothing — silently — so both
		the enveloped and the bare state must be understood."""
		inst = self._install()
		run = self._run(inst, age_s=DISPATCHED_S)
		self._poll({run: {"status": "failed", "error": {"message": "bare shape"}}})
		self.assertEqual(self._status(run), "failed")

	# ------------------------------------------------------------------ #
	# CA-4 parity: the scribe ruling is keyed on the run's own durable pages,
	# NEVER on which sweep reached it or why.
	# ------------------------------------------------------------------ #
	def test_a_fleet_failed_scribe_with_pages_is_completed_not_failed(self):
		"""A scribe that wrote its pages and then crashed late is a SUCCESS the delegate
		merely never finalized. Failing it here would report the same row two different
		ways depending on whether the poll or the 3h reaper got to it first."""
		inst = self._install(SCRIBE_SLUG)
		run = self._run(inst, slug=SCRIBE_SLUG, age_s=DISPATCHED_S, pages_written=5)
		self._poll({run: self._fleet_failed("delegate exec died: exit 137")})
		self.assertEqual(self._status(run), "completed")

	def test_a_lost_record_scribe_with_pages_is_completed_not_failed(self):
		"""Same ruling when the host lost the run record entirely: the pages are in the
		wiki either way, and a pruned state file is not evidence the work never happened."""
		from jarvis.exceptions import AdminUnreachableError

		inst = self._install(SCRIBE_SLUG)
		run = self._run(
			inst,
			slug=SCRIBE_SLUG,
			age_s=agent_scheduler.MISSING_RECORD_FALLBACK_SECONDS + 60,
			pages_written=3,
		)
		self._poll({run: AdminUnreachableError("unknown agent run RUN-1")})
		self.assertEqual(self._status(run), "completed")

	def test_a_fleet_failed_auditor_still_fails(self):
		"""The CA-4 carve-out is for scribes with durable pages ONLY — an auditor has no
		such evidence and must still be failed with the delegate's real message."""
		inst = self._install()
		run = self._run(inst, age_s=DISPATCHED_S, pages_written=4)
		self._poll({run: self._fleet_failed("evaluator blew up")})

		row = frappe.db.get_value(RUN, run, ["status", "error"], as_dict=True)
		self.assertEqual(row.status, "failed")
		self.assertIn("evaluator blew up", row.error)

	# ------------------------------------------------------------------ #
	# outage circuit breaker
	# ------------------------------------------------------------------ #
	def test_the_sweep_stops_calling_admin_after_two_transport_failures(self):
		"""The sweep is sequential and each admin call can block for the client's full
		150s timeout, so with admin down a handful of in-flight runs would outlast the
		5-minute cron interval and sweeps would stack. Two consecutive transport failures
		is enough evidence that the relay, not any one run, is the problem."""
		from jarvis.exceptions import AdminUnreachableError

		inst = self._install()
		runs = [self._run(inst, age_s=DISPATCHED_S) for _ in range(3)]

		with patch.object(frappe, "log_error") as logged:
			asked = self._poll({}, default=AdminUnreachableError("admin is down"))

		self.assertEqual(len(asked), agent_scheduler.MAX_CONSECUTIVE_POLL_FAILURES)
		self.assertEqual(self.terminalized, 0)
		# ONE aggregate line for the whole sweep, never one per run.
		self.assertEqual(logged.call_count, 1)
		for run in runs:
			self.assertEqual(self._status(run), "running")
