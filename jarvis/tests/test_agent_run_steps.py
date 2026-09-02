"""jarvis#1062 — the per-run STEP TIMELINE.

``Jarvis Agent Activity`` records a run's lifecycle (started / completed), and
nothing at all in between, so a customer watching a running agent got a static
"Run in progress" line for the whole audit. But the bench already SEES every
step: the delegate calls back into it for each ``jarvis__*`` tool over the run's
own session bearer.

What is proven here:

  * ``record_step`` appends monotonically-sequenced, owner-pinned rows and never
    raises, whatever the caller does to it;
  * ``humanize_tool_call`` turns a tool call into one short sentence about
    SHAPES — DocType names, report names, counts — and never quotes a payload;
  * the ``jarvis.api`` dispatch hook records a ``tool`` step for a delegate run
    and records NOTHING for an ordinary chat session;
  * a tool that fails still leaves an ``error`` step, and a tool that raises past
    the envelope still leaves one AND still raises;
  * ``record_agent_run`` narrates itself once (a ``writeback`` step), never twice;
  * ``_launch_audit`` opens the timeline with a ``dispatched`` step only after the
    fleet accepted the turn;
  * ``list_run_steps`` is ownership-gated: a foreign run reads as an empty
    timeline, never as an existence oracle.

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.test_agent_run_steps
"""

import json
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import agent_catalog, agent_run_steps, agent_scheduler, agents_api
from jarvis.tools import _agent_run_ctx, _delegate_capability

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
STEP = "Jarvis Agent Run Step"
SESSION = "Jarvis Chat Session"
CONVERSATION = "Jarvis Conversation"

SLUG = "rs-auditor"
# Two reads plus the writeback pair: enough surface for the dispatch hook tests
# without widening what a real auditor may do.
TOOLS_ALLOW = ["jarvis__get_schema", "jarvis__get_list", "jarvis__record_agent_run", "canvas", "message"]


def _mk_user(email: str, roles=("System Manager",)) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	existing = {r.role for r in frappe.get_doc("User", email).roles}
	for role in roles:
		if role not in existing:
			frappe.get_doc("User", email).add_roles(role)
	return email


def _mk_listing(slug: str) -> None:
	"""A dependency-free published listing (no min_apps / doctypes_required), so
	the install-time installability preflight passes on any bench."""
	if frappe.db.exists(LISTING, slug):
		frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": LISTING,
			"agent_slug": slug,
			"title": f"Run steps {slug}",
			"nature": "Auditor",
			"delivery": "delegate",
			"status": "Published",
			"version": "0.1.0",
			"rule_tokens": json.dumps([]),
			"min_apps": json.dumps([]),
			"doctypes_required": json.dumps([]),
			"writes": json.dumps([]),
		}
	).insert(ignore_permissions=True)


def _mk_run(slug: str, owner: str, session_key: str, status: str = "running") -> str:
	inst = frappe.get_doc(
		{"doctype": INSTALLATION, "agent": slug, "run_as_user": owner, "activation_state": "shadow"}
	)
	inst.owner = owner
	inst.flags.ignore_permissions = True
	inst.insert(ignore_permissions=True)
	run = frappe.get_doc(
		{
			"doctype": RUN,
			"agent": slug,
			"installation": inst.name,
			"trigger": "manual",
			"status": status,
			"started_at": frappe.utils.now(),
			"session_key": session_key,
			"capability_contract": _delegate_capability.CONTRACT_SNAPSHOT,
			"tools_allow_json": json.dumps(TOOLS_ALLOW),
			"capability_nature": "auditor",
			"capability_writes_json": json.dumps([]),
		}
	)
	run.owner = owner
	run.flags.ignore_permissions = True
	run.insert(ignore_permissions=True)
	frappe.db.set_value(RUN, run.name, "owner", owner, update_modified=False)
	return run.name


def _steps(run: str) -> list:
	return frappe.get_all(
		STEP,
		filters={"run": run},
		fields=["name", "seq", "kind", "tool", "label", "detail", "status", "duration_ms", "owner"],
		order_by="seq asc",
		ignore_permissions=True,
	)


def _wipe(slug: str) -> None:
	for run in frappe.get_all(RUN, filters={"agent": slug}, pluck="name", ignore_permissions=True):
		for step in frappe.get_all(STEP, filters={"run": run}, pluck="name", ignore_permissions=True):
			frappe.delete_doc(STEP, step, force=True, ignore_permissions=True)
	for dt in ("Jarvis Agent Activity", RUN, INSTALLATION):
		for name in frappe.get_all(dt, filters={"agent": slug}, pluck="name", ignore_permissions=True):
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
	if frappe.db.exists(LISTING, slug):
		frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)


# --------------------------------------------------------------------------- #
# record_step
# --------------------------------------------------------------------------- #
class TestRecordStep(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("rs-owner@example.com")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe(SLUG)
		_mk_listing(SLUG)
		self.key = f"agent:agent-{SLUG}:rs-run"
		self.run = _mk_run(SLUG, self.owner, self.key)

	def tearDown(self):
		_agent_run_ctx.clear_session_key()
		frappe.set_user("Administrator")
		frappe.db.delete(SESSION, {"session_key": self.key})
		_wipe(SLUG)

	def test_steps_are_numbered_from_one_and_monotonic(self):
		for label in ("first", "second", "third"):
			agent_run_steps.record_step(self.run, kind="note", label=label)
		self.assertEqual([s.seq for s in _steps(self.run)], [1, 2, 3])
		self.assertEqual([s.label for s in _steps(self.run)], ["first", "second", "third"])

	def test_owner_is_pinned_to_the_run_owner_not_the_session_user(self):
		"""The hook runs impersonated (the run-as user) or as the scheduler's
		Administrator, so an unpinned row would be invisible on the owner-scoped
		(if_owner) timeline it exists for."""
		frappe.set_user("Administrator")
		agent_run_steps.record_step(self.run, kind="note", label="pinned", owner=self.owner)
		self.assertEqual(_steps(self.run)[0].owner, self.owner)

	def test_label_and_detail_are_clipped(self):
		agent_run_steps.record_step(self.run, kind="note", label="L" * 400, detail="D" * 900, status="error")
		row = _steps(self.run)[0]
		self.assertEqual(len(row.label), agent_run_steps.LABEL_MAX)
		self.assertEqual(len(row.detail), agent_run_steps.DETAIL_MAX)
		self.assertEqual(row.status, "error")

	def test_an_unknown_status_falls_back_to_ok(self):
		agent_run_steps.record_step(self.run, kind="note", label="x", status="weird")
		self.assertEqual(_steps(self.run)[0].status, "ok")

	def test_a_missing_run_records_nothing_and_does_not_raise(self):
		self.assertIsNone(agent_run_steps.record_step("", kind="note", label="orphan"))

	def test_a_failing_insert_is_swallowed(self):
		"""Narration must never be able to fail the run it narrates."""
		with mock.patch.object(frappe, "get_doc", side_effect=RuntimeError("db down")):
			self.assertIsNone(agent_run_steps.record_step(self.run, kind="note", label="x"))

	def test_running_run_for_session_resolves_only_a_running_run(self):
		got = agent_run_steps.running_run_for_session(self.key)
		self.assertEqual(got["name"], self.run)
		self.assertEqual(got["owner"], self.owner)
		frappe.db.set_value(RUN, self.run, "status", "completed", update_modified=False)
		self.assertIsNone(agent_run_steps.running_run_for_session(self.key))
		self.assertIsNone(agent_run_steps.running_run_for_session("not-an-agent-session"))
		self.assertIsNone(agent_run_steps.running_run_for_session(None))


# --------------------------------------------------------------------------- #
# humanize_tool_call
# --------------------------------------------------------------------------- #
class TestHumanizeToolCall(unittest.TestCase):
	def test_get_list_names_the_doctype_and_the_row_count(self):
		label, _ = agent_run_steps.humanize_tool_call(
			"jarvis__get_list", {"doctype": "Sales Invoice"}, [{"name": "SI-1"}, {"name": "SI-2"}]
		)
		self.assertEqual(label, "Read Sales Invoice, 2 rows")

	def test_get_list_singular_row(self):
		label, _ = agent_run_steps.humanize_tool_call("get_list", {"doctype": "ToDo"}, [{"name": "T"}])
		self.assertEqual(label, "Read ToDo, 1 row")

	def test_get_doc_names_the_document(self):
		label, _ = agent_run_steps.humanize_tool_call(
			"get_doc", {"doctype": "Journal Entry", "name": "JV-0007"}, {"name": "JV-0007"}
		)
		self.assertEqual(label, "Read Journal Entry JV-0007")

	def test_get_doc_batch_counts_documents(self):
		label, _ = agent_run_steps.humanize_tool_call(
			"get_doc", {"doctype": "ToDo", "names": ["a", "b", "c"]}, {"count": 3}
		)
		self.assertEqual(label, "Read ToDo, 3 documents")

	def test_run_report_names_the_report_and_counts_result_rows(self):
		label, detail = agent_run_steps.humanize_tool_call(
			"run_report", {"report_name": "Trial Balance"}, {"columns": [], "result": [1, 2, 3]}
		)
		self.assertEqual(label, "Ran report Trial Balance")
		self.assertEqual(detail, "3 rows")

	def test_query_names_the_from_doctype(self):
		label, detail = agent_run_steps.humanize_tool_call(
			"query", {"spec": {"from": "GL Entry"}}, {"sql": "select 1", "rows": [1, 2]}
		)
		self.assertEqual(label, "Queried GL Entry")
		self.assertEqual(detail, "2 rows")

	def test_get_balance_on_names_the_account(self):
		label, _ = agent_run_steps.humanize_tool_call(
			"get_balance_on", {"account": "Debtors - AB"}, {"balance": 1200.0}
		)
		self.assertEqual(label, "Checked balance for Debtors - AB")

	def test_record_agent_run_counts_the_findings(self):
		label, _ = agent_run_steps.humanize_tool_call(
			"jarvis__record_agent_run", {"findings": [{}, {}]}, {"findings_count": 2}
		)
		self.assertEqual(label, "Recorded 2 findings")

	def test_save_agent_dashboard_has_fixed_copy(self):
		label, _ = agent_run_steps.humanize_tool_call("save_agent_dashboard", {}, {"dashboard": "D-1"})
		self.assertEqual(label, "Saved dashboard")

	def test_an_unknown_tool_falls_back_to_a_title_cased_name(self):
		label, _ = agent_run_steps.humanize_tool_call("jarvis__get_fiscal_year", {}, {})
		self.assertEqual(label, "Get Fiscal Year")

	def test_a_failed_call_still_yields_a_label(self):
		"""``result`` is None on the error path — the step still has to say what
		was attempted."""
		label, _ = agent_run_steps.humanize_tool_call("get_list", {"doctype": "ToDo"}, None)
		self.assertEqual(label, "Read ToDo")

	def test_the_label_never_quotes_the_payload(self):
		"""A step carries shapes, not customer data: nothing from the returned row
		may reach the label or the detail."""
		label, detail = agent_run_steps.humanize_tool_call(
			"get_list",
			{"doctype": "Customer"},
			[{"name": "CUST-1", "customer_name": "Aerele Technologies", "credit_limit": 900000}],
		)
		self.assertNotIn("Aerele", label + detail)
		self.assertNotIn("900000", label + detail)

	def test_labels_and_details_are_clipped(self):
		label, _ = agent_run_steps.humanize_tool_call("get_list", {"doctype": "D" * 400}, [])
		self.assertLessEqual(len(label), agent_run_steps.LABEL_MAX)


# --------------------------------------------------------------------------- #
# the jarvis.api dispatch hook
# --------------------------------------------------------------------------- #
class TestDispatchHookRecordsSteps(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.run_as = _mk_user("rs-runas@example.com")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe(SLUG)
		_mk_listing(SLUG)
		self.key = f"agent:agent-{SLUG}:rs-dispatch"
		self.run = _mk_run(SLUG, self.run_as, self.key)

	def tearDown(self):
		_agent_run_ctx.clear_session_key()
		frappe.set_user("Administrator")
		frappe.db.delete(SESSION, {"session_key": self.key})
		_wipe(SLUG)

	def _dispatch(self, session_key: str, tool: str, args: dict | None = None) -> dict:
		"""The REAL plugin dispatch entry point — impersonation, capability gate,
		_run_tool, receipt — not a hand-rolled stand-in for it."""
		return api._dispatch_from_session(self.run_as, session_key, tool, args or {})

	def test_a_delegate_tool_call_lands_one_step(self):
		res = self._dispatch(self.key, "get_schema", {"doctype": "ToDo"})
		self.assertTrue(res["ok"], res)
		rows = _steps(self.run)
		self.assertEqual(len(rows), 1, rows)
		self.assertEqual(rows[0].kind, "tool")
		self.assertEqual(rows[0].tool, "get_schema")
		self.assertEqual(rows[0].status, "ok")
		self.assertEqual(rows[0].owner, self.run_as)
		self.assertIsNotNone(rows[0].duration_ms)

	def test_an_ordinary_chat_session_records_nothing(self):
		"""THE control: the hook must be invisible to non-agent chat, which is
		almost all of the traffic on this endpoint."""
		res = self._dispatch("chat-session-with-no-agent-run", "get_schema", {"doctype": "ToDo"})
		self.assertTrue(res["ok"], res)
		self.assertEqual(_steps(self.run), [])

	def test_a_terminal_run_records_nothing(self):
		"""Only a RUNNING run has a timeline to extend; a bearer replayed after the
		run finished must not append to its history."""
		frappe.db.set_value(RUN, self.run, "status", "completed", update_modified=False)
		self._dispatch(self.key, "get_schema", {"doctype": "ToDo"})
		self.assertEqual(_steps(self.run), [])

	def test_a_tool_error_envelope_leaves_an_error_step(self):
		with mock.patch.object(
			api, "_run_tool", return_value={"ok": False, "error": {"code": "X", "message": "no"}}
		):
			res = self._dispatch(self.key, "get_list", {"doctype": "ToDo"})
		self.assertFalse(res["ok"], res)
		rows = _steps(self.run)
		self.assertEqual(len(rows), 1, rows)
		self.assertEqual(rows[0].status, "error")
		self.assertEqual(rows[0].label, "Read ToDo")

	def test_a_raising_tool_still_raises_and_still_leaves_an_error_step(self):
		"""A fault past _run_tool's envelope translation is a real bug: it must
		still reach Frappe's handler, and the step must be ATTEMPTED on the way
		out. In a real request the handler then rolls the transaction back and
		takes the row with it - deliberately, since committing to save it would
		also flush the raising tool's partial writes. The step that SURVIVES a
		tool failure is the ok=False envelope one above, which is how a tool
		error normally arrives."""
		with mock.patch.object(api, "_run_tool", side_effect=RuntimeError("boom")):
			with self.assertRaises(RuntimeError):
				self._dispatch(self.key, "get_list", {"doctype": "ToDo"})
		rows = _steps(self.run)
		self.assertEqual(len(rows), 1, rows)
		self.assertEqual(rows[0].status, "error")
		self.assertEqual(rows[0].tool, "get_list")

	def test_the_writeback_tool_is_not_double_narrated_by_the_hook(self):
		"""``record_agent_run`` writes its OWN writeback step (it is the only caller
		that knows how many findings actually persisted), so the generic hook must
		skip it rather than adding a second row for the same act."""
		with mock.patch.object(api, "_run_tool", return_value={"ok": True, "data": {}}):
			self._dispatch(self.key, "record_agent_run", {"findings": []})
		self.assertEqual(_steps(self.run), [])

	def test_a_refused_tool_records_no_step(self):
		"""A capability refusal never reaches dispatch, so there is no step to
		record — the refusal is the receipt, and the audit trail already has it."""
		res = self._dispatch(self.key, "get_doc", {"doctype": "ToDo", "name": "nope"})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "CapabilityDeniedError")
		self.assertEqual(_steps(self.run), [])

	def test_a_broken_step_hook_never_breaks_the_tool_call(self):
		with mock.patch.object(agent_run_steps, "record_step", side_effect=RuntimeError("nope")):
			res = self._dispatch(self.key, "get_schema", {"doctype": "ToDo"})
		self.assertTrue(res["ok"], res)


# --------------------------------------------------------------------------- #
# list_run_steps
# --------------------------------------------------------------------------- #
class TestListRunSteps(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# CI runs a fresh DB: the role the timeline's if_owner read depends on has
		# to exist before a user can be given it (the local bench is role-polluted
		# and hides this).
		from jarvis.permissions import ensure_jarvis_user_role

		ensure_jarvis_user_role()
		cls.owner = _mk_user("rs-mine@example.com", roles=("Jarvis User",))
		cls.stranger = _mk_user("rs-stranger@example.com", roles=("Jarvis User",))
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe(SLUG)
		_mk_listing(SLUG)
		self.key = f"agent:agent-{SLUG}:rs-list"
		self.run = _mk_run(SLUG, self.owner, self.key)
		agent_run_steps.record_step(
			self.run, kind="dispatched", label="Dispatched to the agent", owner=self.owner
		)
		agent_run_steps.record_step(
			self.run, kind="tool", tool="get_list", label="Read ToDo, 2 rows", owner=self.owner
		)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete(SESSION, {"session_key": self.key})
		_wipe(SLUG)

	def test_the_owner_reads_their_timeline_in_sequence_order(self):
		frappe.set_user(self.owner)
		res = agents_api.list_run_steps(self.run)
		self.assertEqual(res["count"], 2)
		self.assertEqual([s["kind"] for s in res["steps"]], ["dispatched", "tool"])
		self.assertEqual([s["seq"] for s in res["steps"]], [1, 2])

	def test_a_foreign_run_reads_as_an_empty_timeline_not_an_oracle(self):
		"""Identical to an unknown run: a stranger must not be able to tell that
		this run exists, let alone how busy it was."""
		frappe.set_user(self.stranger)
		self.assertEqual(agents_api.list_run_steps(self.run), {"steps": [], "count": 0})
		self.assertEqual(agents_api.list_run_steps("RUN-does-not-exist"), {"steps": [], "count": 0})

	def test_a_jarvis_admin_may_read_someone_elses_run(self):
		admin = _mk_user("rs-admin@example.com", roles=("Jarvis User", "System Manager"))
		frappe.set_user(admin)
		self.assertEqual(agents_api.list_run_steps(self.run)["count"], 2)

	def test_a_blank_run_is_an_empty_timeline(self):
		frappe.set_user(self.owner)
		self.assertEqual(agents_api.list_run_steps("")["count"], 0)


# --------------------------------------------------------------------------- #
# the launch opens the timeline
# --------------------------------------------------------------------------- #
class TestLaunchRecordsDispatchedStep(unittest.TestCase):
	"""``_launch_audit`` commits, so this class cleans up after itself rather than
	relying on a test-case rollback."""

	SLUG = "rs-launch-agent"
	OWNER = "rs-launch-owner@example.com"
	DECLARED = ["jarvis__get_schema", "jarvis__record_agent_run", "canvas", "message"]

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_mk_user(cls.OWNER)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self.created: list[tuple[str, str]] = []
		_mk_listing(self.SLUG)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		for run in frappe.get_all(RUN, filters={"agent": self.SLUG}, pluck="name", ignore_permissions=True):
			for step in frappe.get_all(STEP, filters={"run": run}, pluck="name", ignore_permissions=True):
				frappe.delete_doc(STEP, step, force=True, ignore_permissions=True)
		for dt, name in self.created:
			try:
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
			except Exception:
				pass
		for dt in ("Jarvis Agent Activity", RUN, INSTALLATION):
			for n in frappe.get_all(dt, filters={"agent": self.SLUG}, pluck="name", ignore_permissions=True):
				try:
					frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
				except Exception:
					pass
		if frappe.db.exists(LISTING, self.SLUG):
			frappe.delete_doc(LISTING, self.SLUG, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _launch(self, post=None) -> dict:
		import jarvis.admin_client as admin_client

		inst = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": self.SLUG,
				"run_as_user": self.OWNER,
				"activation_state": "shadow",
				"enabled": 1,
			}
		)
		inst.owner = self.OWNER
		inst.flags.ignore_permissions = True
		inst.insert(ignore_permissions=True)
		frappe.db.commit()

		orig = admin_client.post_agent_run
		admin_client.post_agent_run = post or (lambda **kw: {"run_id": kw.get("run_id"), "status": "queued"})
		frappe.set_user(self.OWNER)
		try:
			with mock.patch.object(agent_catalog, "registry_tools_allow", return_value=self.DECLARED):
				result = agent_scheduler._launch_audit(inst, trigger="manual")
		finally:
			frappe.set_user("Administrator")
			admin_client.post_agent_run = orig
		self.created.append((RUN, result["run"]))
		self.created.append((CONVERSATION, result["conversation"]))
		session = frappe.db.get_value(SESSION, {"session_key": result["session_key"]}, "name")
		if session:
			self.created.append((SESSION, session))
		return result

	def test_launch_opens_the_timeline_with_a_dispatched_step(self):
		result = self._launch()
		rows = _steps(result["run"])
		self.assertEqual(len(rows), 1, rows)
		self.assertEqual(rows[0].kind, "dispatched")
		self.assertEqual(rows[0].seq, 1)
		self.assertEqual(rows[0].label, "Dispatched to the agent")
		self.assertEqual(rows[0].owner, self.OWNER)

	def test_a_failed_dispatch_opens_no_timeline(self):
		""" "Dispatched" must mean the fleet really accepted the turn — a refused
		dispatch has to leave the timeline empty, not claim a step that never
		happened."""

		def _boom(**kw):
			raise RuntimeError("fleet said no")

		with self.assertRaises(RuntimeError):
			self._launch(post=_boom)
		runs = frappe.get_all(RUN, filters={"agent": self.SLUG}, pluck="name", ignore_permissions=True)
		self.assertTrue(runs)
		for run in runs:
			self.assertEqual(_steps(run), [])
