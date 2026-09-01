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
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, now_datetime

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
