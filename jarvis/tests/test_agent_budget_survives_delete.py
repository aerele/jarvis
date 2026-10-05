"""The monthly agent-run budget (A14) survives an uninstall.

The budget is counted from this month's non-failed ``Jarvis Agent Run`` rows: per
installation, and across the tenant for the aggregate ceiling. Uninstalling deleted
every run row of the installation, so uninstall-and-reinstall handed the month's budget
back, and the tenant's ceiling dropped with it.

Uninstall now keeps the rows that count (ended, not failed, this month) with their
installation link cleared, so no screen lists them; a new installation of the same agent
by the same owner (the (owner, agent) pair is unique) is charged with them, and the
hourly agent job purges them once their month is over (the agent twin of #1621 for
macros).

Run:
  bench --site test_site run-tests --app jarvis \
    --module jarvis.tests.test_agent_budget_survives_delete
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import frappe
from frappe.desk.reportview import execute
from frappe.model import no_value_fields
from frappe.utils import add_months, get_first_day, now_datetime, today

from jarvis.chat import agent_models, agent_scheduler, agents_api
from jarvis.chat.agent_activity import log_activity
from jarvis.tests._agent_access import allow_listing_for
from jarvis.tests.test_agent_dispatch_idempotency import (
	ACTIVITY,
	INSTALLATION,
	RUN,
	SESSION,
	SLUG,
	DispatchIdempotencyTestCase,
	_mk_user,
)

OTHER_OWNER = "budget-keep-other@example.com"
REVIEWER = "budget-keep-reviewer@example.com"
STEP = "Jarvis Agent Run Step"
DASHBOARD = "Jarvis Dashboard"

# What a finished run produced, set on a run that an uninstall then keeps.
_RESULT = {
	"findings_count": 7,
	"advisory_findings_count": 4,
	"blocker_count": 2,
	"result_state": "partial",
	"wm_row_count": 12345,
	"integrity_digest": "budget-keep-digest",
	"model_used": "budget-keep/model",
	"preparation_mode": "shadow",
	"error": "produced by the run",
	"coverage_note": "produced by the run",
	"scope_json": "{}",
	"assessment_output_json": "{}",
}


class AgentBudgetTestCase(DispatchIdempotencyTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.other = _mk_user(OTHER_OWNER)
		allow_listing_for(SLUG, user=cls.other)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		# A site whose schema predates the min-model setting cannot read it, and the
		# install path then fails before it creates a row. A migrated site has the field
		# and runs the real check.
		if not frappe.get_meta("Jarvis Settings").has_field("enforce_agent_min_model"):
			patcher = patch.object(agent_models, "is_enforced", return_value=False)
			patcher.start()
			self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in frappe.get_all(STEP, filters={"label": "budget-keep-step"}, pluck="name"):
			frappe.delete_doc(STEP, name, force=True, ignore_permissions=True)
		for name in frappe.get_all(
			DASHBOARD, filters={"dashboard_title": ["like", "budget-keep%"]}, pluck="name"
		):
			frappe.db.delete(DASHBOARD, {"name": name})
		for name in frappe.get_all(ACTIVITY, filters={"owner": OTHER_OWNER}, pluck="name"):
			frappe.delete_doc(ACTIVITY, name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": "budget-keep todo"})
		runs = frappe.get_all(RUN, filters={"agent": SLUG}, pluck="name")
		if runs:
			frappe.db.delete("Version", {"ref_doctype": RUN, "docname": ["in", runs]})
			frappe.db.delete("Comment", {"reference_doctype": RUN, "reference_name": ["in", runs]})
		frappe.db.commit()
		super().tearDown()

	# ------------------------------------------------------------------ #
	# fixture
	# ------------------------------------------------------------------ #
	def _install_as(self, user: str) -> str:
		frappe.set_user(user)
		try:
			return agents_api.install_agent(SLUG)["data"]["name"]
		finally:
			frappe.set_user("Administrator")

	def _uninstall_as(self, user: str, inst: str) -> None:
		frappe.set_user(user)
		try:
			agents_api.uninstall_agent(inst)
		finally:
			frappe.set_user("Administrator")

	def _run(self, inst: str, status: str = "completed", *, months_ago: int = 0, owner=None, **extra) -> str:
		doc = frappe.get_doc(
			{
				"doctype": RUN,
				"agent": SLUG,
				"installation": inst,
				"trigger": "manual",
				"status": status,
				"started_at": now_datetime(),
				**extra,
			}
		)
		doc.insert(ignore_permissions=True)
		values = {"owner": owner or frappe.db.get_value(INSTALLATION, inst, "owner")}
		if months_ago:
			values["creation"] = add_months(now_datetime(), -months_ago)
		frappe.db.set_value(RUN, doc.name, values, update_modified=False)
		frappe.db.commit()
		return doc.name

	def _as(self, user: str, fn, *args, **kwargs):
		frappe.set_user(user)
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")


class TestBudgetSurvivesUninstall(AgentBudgetTestCase):
	def test_uninstall_and_reinstall_keeps_the_installations_month_count(self):
		inst = self._install_as(self.owner)
		for _ in range(3):
			self._run(inst)
		self._run(inst, "failed")  # never counted
		self._run(inst, months_ago=1)  # last month: not this month's budget
		self.assertEqual(agent_scheduler._runs_this_month(installation=inst), 3)

		self._uninstall_as(self.owner, inst)
		again = self._install_as(self.owner)

		self.assertNotEqual(again, inst)
		self.assertEqual(
			agent_scheduler._runs_this_month(installation=again),
			3,
			"uninstall and reinstall handed the month's runs back",
		)

	def test_the_tenant_count_does_not_drop_on_uninstall(self):
		inst = self._install_as(self.owner)
		for status in ("completed", "partial", "stopped"):
			self._run(inst, status)
		before = agent_scheduler._runs_this_month()

		self._uninstall_as(self.owner, inst)

		self.assertEqual(agent_scheduler._runs_this_month(), before, "the tenant ceiling lost the runs")

	def test_a_reinstalled_agent_over_its_budget_is_still_refused(self):
		inst = self._install_as(self.owner)
		for _ in range(3):
			self._run(inst)
		self._uninstall_as(self.owner, inst)
		again = self._install_as(self.owner)

		with patch.object(agent_scheduler, "_agent_run_budget_monthly", return_value=3):
			over, why = agent_scheduler._over_run_budget(again)

		self.assertTrue(over)
		self.assertEqual(why, "monthly run budget exceeded for this agent")

	def test_an_admin_uninstall_keeps_the_count_for_the_owners_reinstall(self):
		inst = self._install_as(self.owner)
		self._run(inst)
		self._uninstall_as("Administrator", inst)
		again = self._install_as(self.owner)
		self.assertEqual(agent_scheduler._runs_this_month(installation=again), 1)

	def test_another_owners_install_of_the_same_agent_is_not_charged(self):
		inst = self._install_as(self.owner)
		self._run(inst)
		self._uninstall_as(self.owner, inst)

		theirs = self._install_as(self.other)

		self.assertEqual(agent_scheduler._runs_this_month(installation=theirs), 0)

	def test_a_shadow_run_rehomed_to_the_reviewer_still_counts_for_the_owner(self):
		"""PP-4 re-homes a shadow installation's runs to its reviewer, so the row's owner
		is not always the installer. The kept row is handed back to the installer, the
		identity a reinstall is matched on."""
		frappe.set_user("Administrator")
		reviewer = _mk_user(REVIEWER)
		inst = self._install_as(self.owner)
		self._run(inst, owner=reviewer)
		self._uninstall_as(self.owner, inst)
		again = self._install_as(self.owner)
		self.assertEqual(agent_scheduler._runs_this_month(installation=again), 1)

	def test_only_ended_counted_rows_of_this_month_are_kept(self):
		inst = self._install_as(self.owner)
		kept = {self._run(inst, s) for s in ("completed", "partial", "stopped")}
		gone = {self._run(inst, "failed"), self._run(inst, months_ago=1)}

		self._uninstall_as(self.owner, inst)

		left = set(frappe.get_all(RUN, filters={"agent": SLUG}, pluck="name"))
		self.assertEqual(left & kept, kept)
		self.assertEqual(left & gone, set(), "a row the budget does not count was kept")
		self.assertEqual(
			{
				r.installation
				for r in frappe.get_all(RUN, filters={"name": ["in", list(kept)]}, fields=["installation"])
			},
			{None},
		)

	def test_a_kept_row_drops_what_the_run_produced(self):
		inst = self._install_as(self.owner)
		dash = frappe.get_doc(
			{
				"doctype": DASHBOARD,
				"dashboard_title": "budget-keep dashboard",
				"scope": "User",
				"target_user": self.owner,
			}
		)
		dash.flags.ignore_permissions = True
		dash.flags.ignore_mandatory = True
		dash.insert(ignore_permissions=True)
		run = self._run(inst, dashboard=dash.name, session_key=f"budget-keep-{inst}", **_RESULT)
		frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": RUN, "reference_name": run}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		self._uninstall_as(self.owner, inst)

		row = frappe.db.get_value(RUN, run, "*", as_dict=True)
		# Every field of the doctype but the few a kept row needs, so a field added later
		# is checked too.
		left = {
			df.fieldname: row[df.fieldname]
			for df in frappe.get_meta(RUN).fields
			if df.fieldtype not in no_value_fields
			and df.fieldname
			not in ("agent", "status", "trigger", "started_at", "finished_at", "conversation")
			and row[df.fieldname]
		}
		self.assertEqual(left, {}, "a kept row still carries what the run produced")
		self.assertEqual((row.owner, row.status, row.agent), (self.owner, "completed", SLUG))
		self.assertFalse(
			frappe.db.exists("Comment", {"reference_doctype": RUN, "reference_name": run}),
			"a note on the run outlived the uninstall",
		)
		# A kept row must not hold the dashboard: deleting it would be refused as linked.
		frappe.delete_doc(DASHBOARD, dash.name, ignore_permissions=True)
		self.assertFalse(frappe.db.exists(DASHBOARD, dash.name))


class TestUninstallStopsALiveRun(AgentBudgetTestCase):
	"""A run still going at uninstall is stopped first, then kept and counted like any
	other stopped run (the agent twin of a macro delete stopping its run)."""

	def _live_run(self, inst: str) -> tuple[str, str]:
		run = self._run(inst, "running")
		key = f"agent:agent-{SLUG}:{run}"
		agent_scheduler._mint_run_session(key, self.owner)
		frappe.db.set_value(RUN, run, "session_key", key, update_modified=False)
		frappe.db.commit()
		return run, key

	def test_a_running_run_is_stopped_kept_and_counted(self):
		inst = self._install_as(self.owner)
		run, key = self._live_run(inst)

		frappe.set_user(self.owner)
		try:
			out = agents_api.uninstall_agent(inst)
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(out, {"ok": True, "stopped_runs": 1})
		row = frappe.db.get_value(RUN, run, ["status", "installation", "finished_at"], as_dict=True)
		self.assertIsNotNone(row, "the live run was deleted instead of stopped")
		self.assertEqual((row.status, row.installation), ("stopped", None))
		self.assertIsNotNone(row.finished_at)
		self.assertFalse(
			frappe.db.exists(SESSION, {"session_key": key}), "the run's session bearer outlived it"
		)
		again = self._install_as(self.owner)
		self.assertEqual(agent_scheduler._runs_this_month(installation=again), 1)

	def test_a_run_committed_while_the_uninstall_waits_is_stopped(self):
		"""A launch on another worker held the lock and committed a running run after this
		request read the installation: the uninstall must see it past its old snapshot."""
		from jarvis import _redis_lock
		from jarvis.tests.race_harness import other_connection

		inst = self._install_as(self.owner)
		name = "budget-race-" + frappe.generate_hash(length=8)
		real_lock = _redis_lock.redis_lock

		@contextmanager
		def _launched_elsewhere_while_waiting(lock_name, **kw):
			with other_connection() as db:
				db.sql(
					f"""INSERT INTO `tab{RUN}` (name, creation, modified, owner, modified_by, agent,
					installation, `trigger`, status, started_at)
					VALUES (%s, NOW(), NOW(), %s, %s, %s, %s, 'scheduled', 'running', NOW())""",
					(name, self.owner, self.owner, SLUG, inst),
				)
				db.commit()
			with real_lock(lock_name, **kw) as acquired:
				yield acquired

		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.db.delete, RUN, {"name": name})
		frappe.set_user(self.owner)
		try:
			frappe.get_doc(INSTALLATION, inst)  # the request's first read opens its snapshot
			with patch.object(_redis_lock, "redis_lock", _launched_elsewhere_while_waiting):
				out = agents_api.uninstall_agent(inst)
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(out, {"ok": True, "stopped_runs": 1})
		row = frappe.db.get_value(RUN, name, ["status", "installation"], as_dict=True)
		self.assertEqual((row.status, row.installation), ("stopped", None))

	def test_an_uninstall_while_a_launch_holds_the_lock_is_refused(self):
		from jarvis._redis_lock import redis_lock

		inst = self._install_as(self.owner)
		with (
			patch.object(agents_api, "DISPATCH_LOCK_WAIT_S", 0.1),
			redis_lock(agent_scheduler._dispatch_lock_name(inst), timeout_s=30) as held,
		):
			self.assertTrue(held)
			with self.assertRaises(frappe.ValidationError):
				self._uninstall_as(self.owner, inst)
		self.assertTrue(frappe.db.exists(INSTALLATION, inst), "the refused uninstall deleted it anyway")

	def test_a_launch_during_the_uninstall_leaves_no_run_behind(self):
		"""A sweep fires while the uninstall is cascading (re-entered from inside the
		cascade, where a real one would overlap). It must not start a run that the
		cascade has already passed over, on an installation about to be deleted."""
		inst = self._due_install()
		real = agents_api._detach_last_seen_run
		fired = []

		def _sweep_then_detach(run_names):
			fired.append(1)
			self._sweep_as_administrator()
			return real(run_names)

		with self._delegate_stubbed(), patch.object(agents_api, "_detach_last_seen_run", _sweep_then_detach):
			self._uninstall_as(self.owner, inst)

		self.assertEqual(fired, [1])
		self.assertEqual(self.dispatched, [], "a run was launched during the uninstall")
		self.assertFalse(frappe.db.exists(INSTALLATION, inst))
		self.assertEqual(frappe.get_all(RUN, filters={"installation": inst}, pluck="name"), [])

	def test_a_run_now_that_waited_out_an_uninstall_starts_nothing(self):
		"""Run Now reads the installation, then waits for the dispatch lock while an
		uninstall holds it. When it gets the lock the installation is gone."""
		from jarvis import _redis_lock

		inst = self._due_install()
		real_lock = _redis_lock.redis_lock

		waited = []

		@contextmanager
		def _uninstalled_while_waiting(name, **kw):
			if not waited:  # Run Now's wait; the uninstall takes the real lock
				waited.append(name)
				prev = frappe.session.user
				frappe.set_user(self.owner)
				try:
					agents_api.uninstall_agent(inst)
				finally:
					frappe.set_user(prev)
			with real_lock(name, **kw) as acquired:
				yield acquired

		with (
			patch.object(_redis_lock, "redis_lock", _uninstalled_while_waiting),
			self.assertRaises(frappe.DoesNotExistError),
		):
			self._run_now(inst)
		self.assertEqual(waited, [agent_scheduler._dispatch_lock_name(inst)])
		self.assertEqual(self.dispatched, [])
		self.assertEqual(frappe.get_all(RUN, filters={"installation": inst}, pluck="name"), [])

	def test_run_now_rechecks_the_installation_past_its_snapshot(self):
		"""The uninstall ran on another worker: this request's snapshot, taken when it
		read the installation, still holds the row."""
		from jarvis import _redis_lock
		from jarvis.tests.race_harness import other_connection

		inst = self._due_install()
		real_lock = _redis_lock.redis_lock

		@contextmanager
		def _deleted_elsewhere_while_waiting(name, **kw):
			with other_connection() as db:
				db.sql(f"DELETE FROM `tab{INSTALLATION}` WHERE name = %s", (inst,))
				db.commit()
			with real_lock(name, **kw) as acquired:
				yield acquired

		with (
			patch.object(_redis_lock, "redis_lock", _deleted_elsewhere_while_waiting),
			self.assertRaises(frappe.DoesNotExistError),
		):
			self._run_now(inst)
		self.assertEqual(self.dispatched, [])


class TestTheTenantCeilingAfterAnUninstall(AgentBudgetTestCase):
	"""The tenant ceiling is the budget times the installs. The kept runs of an
	uninstalled agent still count toward the tenant, so the uninstalled agent keeps its
	share of the ceiling for the rest of the month."""

	def _ours_only(self):
		"""Scope the tenant-wide figures to this module's agent: the site is shared, and
		other modules leave enabled installations and runs behind."""
		real_runs = agent_scheduler._runs_this_month
		real_count = frappe.db.count

		def _runs(*, installation=None):
			if installation:
				return real_runs(installation=installation)
			return real_count(
				RUN,
				{"agent": SLUG, "creation": [">=", get_first_day(today())], "status": ["!=", "failed"]},
			)

		def _count(doctype, filters=None, *a, **kw):
			if doctype == INSTALLATION and filters == {"enabled": 1}:
				filters = {"enabled": 1, "agent": SLUG}
			return real_count(doctype, filters, *a, **kw)

		stack = ExitStack()
		stack.enter_context(patch.object(agent_scheduler, "_runs_this_month", side_effect=_runs))
		stack.enter_context(patch.object(frappe.db, "count", side_effect=_count))
		stack.enter_context(patch.object(agent_scheduler, "_agent_run_budget_monthly", return_value=3))
		return stack

	def test_uninstalling_a_used_agent_does_not_refuse_another(self):
		a = self._install_as(self.owner)
		b = self._install_as(self.other)
		for inst in (a, b):
			frappe.db.set_value(INSTALLATION, inst, "enabled", 1, update_modified=False)
		for _ in range(3):
			self._run(a)
		self._run(b)

		self._uninstall_as(self.owner, a)

		with self._ours_only():
			self.assertEqual(agent_scheduler._over_run_budget(b), (False, ""))

	def test_an_uninstalled_agent_holds_one_share_until_it_is_installed_again(self):
		base = agent_scheduler._uninstalled_this_month()
		inst = self._install_as(self.owner)
		self._run(inst)
		self._run(inst)
		self._uninstall_as(self.owner, inst)
		self.assertEqual(agent_scheduler._uninstalled_this_month(), base + 1)

		self._install_as(self.owner)

		self.assertEqual(agent_scheduler._uninstalled_this_month(), base, "the pair is counted twice")

	def test_an_uninstall_with_nothing_kept_holds_no_share(self):
		base = agent_scheduler._uninstalled_this_month()
		inst = self._install_as(self.owner)
		self._run(inst, "failed")
		self._uninstall_as(self.owner, inst)
		self.assertEqual(agent_scheduler._uninstalled_this_month(), base)


class TestKeptRowsAreHidden(AgentBudgetTestCase):
	def _kept_run(self) -> str:
		inst = self._install_as(self.owner)
		run = self._run(inst)
		log_activity(
			agent=SLUG, agent_title="t", installation=inst, action="run_completed", run=run, owner=self.owner
		)
		frappe.get_doc(
			{
				"doctype": STEP,
				"run": run,
				"seq": 1,
				"kind": "tool",
				"label": "budget-keep-step",
				"status": "ok",
				"occurred_at": now_datetime(),
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(STEP, {"run": run}, "owner", self.owner, update_modified=False)
		frappe.db.commit()
		self._uninstall_as(self.owner, inst)
		self.assertTrue(frappe.db.exists(RUN, run))
		return run

	def test_list_runs_does_not_show_a_kept_row(self):
		run = self._kept_run()
		names = [r["name"] for r in self._as(self.owner, agents_api.list_runs)]
		self.assertNotIn(run, names)

	def test_list_runs_page_neither_shows_nor_counts_a_kept_row(self):
		run = self._kept_run()
		page = self._as(self.owner, agents_api.list_runs_page, agent=SLUG)
		self.assertNotIn(run, [r["name"] for r in page["rows"]])
		self.assertEqual(page["total"], 0)

	def test_a_kept_rows_steps_are_not_listed(self):
		run = self._kept_run()
		self.assertEqual(self._as(self.owner, agents_api.list_run_steps, run)["count"], 0)

	def test_a_kept_rows_findings_drilldown_is_empty(self):
		run = self._kept_run()
		self.assertEqual(self._as(self.owner, agents_api.list_findings, run=run)["total"], 0)

	def test_the_activity_feed_shows_no_status_for_a_kept_row(self):
		run = self._kept_run()
		rows = self._as(self.owner, agents_api.list_agent_activity_page, agent=SLUG)["rows"]
		mine = [r for r in rows if r.get("run") == run]
		self.assertTrue(mine)
		self.assertEqual({r["run_status"] for r in mine}, {None}, "the feed reads a deleted run's status")


class TestKeptRowsAreNotReadable(AgentBudgetTestCase):
	"""The generic document API (Desk, REST) reads runs through the permission hooks,
	not through the agent screens, so a kept row is hidden there by the hooks."""

	def _readable_by(self, user: str, run: str) -> dict:
		frappe.set_user(user)
		try:
			listed = frappe.get_list(RUN, filters={"name": run}, pluck="name")
			report = execute(RUN, fields=["name"], filters={"name": run}, as_list=True)
			try:
				got = bool(frappe.client.get(RUN, run))
			except frappe.PermissionError:
				got = False
			return {"list": bool(listed), "report": bool(report), "get": got}
		finally:
			frappe.set_user("Administrator")

	def test_the_installer_never_reads_a_reviewers_shadow_run(self):
		frappe.set_user("Administrator")
		reviewer = _mk_user(REVIEWER)
		inst = self._install_as(self.owner)
		run = self._run(inst, owner=reviewer, **_RESULT)
		nothing = {"list": False, "report": False, "get": False}
		self.assertEqual(self._readable_by(self.owner, run), nothing)

		self._uninstall_as(self.owner, inst)

		self.assertTrue(frappe.db.exists(RUN, run))
		self.assertEqual(frappe.db.get_value(RUN, run, "owner"), self.owner)
		self.assertEqual(
			self._readable_by(self.owner, run), nothing, "the uninstall opened the run to the installer"
		)
		self.assertEqual(self._readable_by(reviewer, run), nothing)

	def test_a_kept_row_of_ones_own_is_not_readable_either(self):
		inst = self._install_as(self.owner)
		run = self._run(inst)
		everything = {"list": True, "report": True, "get": True}
		self.assertEqual(self._readable_by(self.owner, run), everything)

		self._uninstall_as(self.owner, inst)

		self.assertEqual(self._readable_by(self.owner, run), {"list": False, "report": False, "get": False})

	def test_an_admin_still_reads_a_kept_row(self):
		inst = self._install_as(self.owner)
		run = self._run(inst)
		self._uninstall_as(self.owner, inst)
		self.assertTrue(frappe.get_list(RUN, filters={"name": run}, pluck="name"))
		self.assertEqual(frappe.client.get(RUN, run)["name"], run)


class TestKeptRowsArePurged(AgentBudgetTestCase):
	def _kept(self, *, months_ago: int) -> str:
		inst = self._install_as(self.owner)
		run = self._run(inst)
		self._uninstall_as(self.owner, inst)
		if months_ago:
			frappe.db.set_value(
				RUN, run, "creation", add_months(now_datetime(), -months_ago), update_modified=False
			)
			frappe.db.commit()
		return run

	def test_previous_months_kept_rows_are_purged(self):
		old = self._kept(months_ago=1)
		agent_scheduler.purge_kept_budget_rows(owner=self.owner)
		frappe.db.commit()
		self.assertFalse(frappe.db.exists(RUN, old))

	def test_the_purge_removes_what_points_at_a_purged_row(self):
		old = self._kept(months_ago=1)
		inst = self._install_as(self.owner)
		history = self._run(inst, months_ago=2)
		for run in (old, history):
			self._traces_on(run)

		agent_scheduler.purge_kept_budget_rows(owner=self.owner)
		frappe.db.commit()

		self.assertEqual(self._traces(old), {}, "a purged run left rows pointing at it")
		self.assertEqual(set(self._traces(history)), {"Version", "Comment", "ToDo"})

	def _traces_on(self, run: str) -> None:
		frappe.db.delete("Version", {"ref_doctype": RUN, "docname": run})
		frappe.get_doc({"doctype": "Version", "ref_doctype": RUN, "docname": run, "data": "{}"}).insert(
			ignore_permissions=True
		)
		frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": RUN, "reference_name": run}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "budget-keep todo",
				"allocated_to": self.owner,
				"reference_type": RUN,
				"reference_name": run,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def _traces(self, run: str) -> dict:
		found = {
			"Version": frappe.db.count("Version", {"ref_doctype": RUN, "docname": run}),
			"Comment": frappe.db.count("Comment", {"reference_doctype": RUN, "reference_name": run}),
			"ToDo": frappe.db.count("ToDo", {"reference_type": RUN, "reference_name": run}),
		}
		return {k: v for k, v in found.items() if v}

	def test_this_months_kept_rows_are_not_purged(self):
		cur = self._kept(months_ago=0)
		agent_scheduler.purge_kept_budget_rows(owner=self.owner)
		frappe.db.commit()
		self.assertTrue(frappe.db.exists(RUN, cur))

	def test_the_purge_never_touches_a_live_installations_runs(self):
		inst = self._install_as(self.owner)
		history = self._run(inst, months_ago=2)
		agent_scheduler.purge_kept_budget_rows(owner=self.owner)
		frappe.db.commit()
		self.assertTrue(frappe.db.exists(RUN, history), "run history of an installed agent was purged")

	def test_the_purge_leaves_a_row_that_never_counted(self):
		"""Only ended, counted rows are kept by an uninstall, so a row with no
		installation in any other status is not one, and the purge leaves it."""
		names = []
		for status in ("running", "failed"):
			doc = frappe.get_doc(
				{
					"doctype": RUN,
					"agent": SLUG,
					"trigger": "manual",
					"status": status,
					"started_at": now_datetime(),
				}
			)
			doc.insert(ignore_permissions=True)
			frappe.db.set_value(
				RUN,
				doc.name,
				{"owner": self.owner, "creation": add_months(now_datetime(), -2)},
				update_modified=False,
			)
			names.append(doc.name)
		frappe.db.commit()

		agent_scheduler.purge_kept_budget_rows(owner=self.owner)
		frappe.db.commit()

		self.assertEqual(
			sorted(frappe.get_all(RUN, filters={"name": ["in", names]}, pluck="name")), sorted(names)
		)

	def test_the_purge_spares_another_users_kept_row(self):
		theirs = self._install_as(self.other)
		their_run = self._run(theirs)
		self._uninstall_as(self.other, theirs)
		frappe.db.set_value(RUN, their_run, "creation", add_months(now_datetime(), -1), update_modified=False)
		frappe.db.commit()
		mine = self._kept(months_ago=1)

		agent_scheduler.purge_kept_budget_rows(owner=self.owner)
		frappe.db.commit()

		self.assertFalse(frappe.db.exists(RUN, mine))
		self.assertTrue(frappe.db.exists(RUN, their_run), "the owner filter was ignored")

	def test_the_hourly_agent_job_runs_the_purge(self):
		from jarvis import hooks

		self.assertIn("jarvis.chat.agent_scheduler.reap_stale_agent_runs", hooks.scheduler_events["hourly"])
		old = self._kept(months_ago=1)
		real = agent_scheduler.purge_kept_budget_rows
		with patch.object(
			agent_scheduler, "purge_kept_budget_rows", side_effect=lambda: real(owner=self.owner)
		) as purge:
			agent_scheduler.reap_stale_agent_runs()
		purge.assert_called_once()
		self.assertFalse(frappe.db.exists(RUN, old))

	def test_the_month_boundary_is_the_budgets(self):
		self.assertEqual(agent_scheduler._budget_month_start(), get_first_day(today()))
