"""The other guarded structure writes (round 2, J1c): a Workflow create or update,
CRM Settings with the Frappe CRM data sync on, and Domain Settings. Each only
through a confirmation card with no trial run; a failure puts back what it wrote.

Like the Custom Field unit these tests run REAL DDL, so every workflow is made on
a scratch ``custom=1`` DocType created in ``setUpClass`` under a unique name and
dropped by ``addClassCleanup`` (``_ScratchBase``); the Workflow States and Actions
they name are made here under unique names and removed again. Never ToDo or any
shared doctype, and no Workflow is ever left active on one.

CRM Settings and Domain Settings are Singles on a shared site: their whole state
is read in ``setUpClass`` and put back (and asserted) at the end, and the forms
the data sync / a domain adds fields to are patched to the scratch DocTypes, so
Quotation and Customer are never altered here. SERIAL ONLY: one run of this
module per site at a time (it holds real structure locks, saves the two shared
settings documents and puts them back). It reconciles and purges only its own
rows.
"""

from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from jarvis import api
from jarvis.chat import actions_api
from jarvis.chat import pending_actions as pa
from jarvis.chat.pending_actions import _seal
from jarvis.tests._pending_action_helpers import AGENT_WRITE, PA, SM_USER, as_user, ensure_user
from jarvis.tests.test_custom_field_exception import (
	WIDE_FIELDS,
	PlantedError,
	_columns,
	_field_rows,
	_ScratchBase,
)
from jarvis.tools import _guarded_structure as gs
from jarvis.tools import _settings_guard as sg
from jarvis.tools import _workflow_guard as wg
from jarvis.tools import _write_risk as wr

WF = "Workflow"
CRM = "CRM Settings"
DOMAIN = "Domain Settings"
CODE_LINE = "This runs code for every user."
ACCESS_LINE = "This changes who can see or edit."
STATE = "workflow_state"
NOT_SM = "pa-owner@example.com"  # a Jarvis User without System Manager


def wf_validation_hook(doc, method=None):
	raise frappe.ValidationError("planted: a hook refused the workflow")


def wf_other_hook(doc, method=None):
	"""Stores something the card never showed, as a controller change in a later
	Frappe (or another app's hook) could."""
	doc.send_email_alert = 1
	if doc.transitions:
		doc.transitions[0].allowed = "All"


def wf_crash_hook(doc, method=None):
	raise PlantedError("planted crash")


def crm_validation_hook(doc, method=None):
	raise frappe.ValidationError("planted: a hook refused the settings")


class _WfBase(_ScratchBase):
	"""A scratch form, and Workflow States / Actions of its own."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.tag = uuid.uuid4().hex[:6]
		cls.s_open, cls.s_ok, cls.s_no = (f"Jwf {w} {cls.tag}" for w in ("Open", "Approved", "Rejected"))
		cls.a_ok, cls.a_no = (f"Jwf {w} {cls.tag}" for w in ("Approve", "Reject"))
		cls.addClassCleanup(cls._drop_workflow_records)
		for state in (cls.s_open, cls.s_ok, cls.s_no):
			frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": state}).insert()
		for action in (cls.a_ok, cls.a_no):
			frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": action}).insert()
		frappe.db.commit()

	@classmethod
	def _wipe_workflows(cls):
		for dt in cls.scratch:
			for name in frappe.get_all(WF, filters={"document_type": dt}, pluck="name"):
				wg._delete_workflow(name)
				frappe.clear_document_cache(WF, name)
			frappe.cache.hdel("workflow", dt)

	@classmethod
	def _drop_workflow_records(cls):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		cls._wipe_workflows()
		frappe.db.delete("Workflow State", {"name": ["like", f"Jwf % {cls.tag}"]})
		frappe.db.delete("Workflow Action Master", {"name": ["like", f"Jwf % {cls.tag}"]})
		frappe.db.commit()
		assert not frappe.db.exists(WF, {"document_type": ["in", cls.scratch]}), "a scratch workflow was left"
		assert not frappe.db.exists("Workflow State", {"name": ["like", f"Jwf % {cls.tag}"]})
		assert not frappe.db.exists("Workflow Action Master", {"name": ["like", f"Jwf % {cls.tag}"]})

	def setUp(self):
		super().setUp()
		self.addCleanup(self._reset_workflows)  # runs before the scratch reset

	def _reset_workflows(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.local.doc_events_hooks = None
		self._wipe_workflows()
		# What the operator's undo filed when a test ran it as Administrator.
		frappe.db.delete(AGENT_WRITE, {"tool": "undo_confirmation", "creation": [">=", self._pa_t0]})
		for dt in self.scratch:
			# What Frappe writes when a test moves a record through a workflow.
			frappe.db.delete("Workflow Action", {"reference_doctype": dt})
			frappe.db.delete("Comment", {"reference_doctype": dt})
			frappe.db.delete(dt)
			frappe.db.delete("Custom Field", {"dt": dt})
			frappe.db.commit()
			for column in (STATE, "jwf_state"):
				if column in _columns(dt):
					frappe.db.sql_ddl(f"ALTER TABLE `tab{dt}` DROP COLUMN `{column}`")
			frappe.clear_cache(doctype=dt)
		frappe.db.commit()

	# -- helpers ---------------------------------------------------------------
	def wf_values(self, name=None, dt=None, **over) -> dict:
		return {
			"workflow_name": name or f"Jwf Flow {self.tag}",
			"document_type": dt or self.dt,
			"is_active": 1,
			"states": [
				{"state": self.s_open, "allow_edit": "System Manager"},
				{"state": self.s_ok, "allow_edit": "System Manager"},
			],
			"transitions": [
				{
					"state": self.s_open,
					"action": self.a_ok,
					"next_state": self.s_ok,
					"allowed": "System Manager",
				}
			],
			**over,
		}

	def create_wf(self, **over) -> dict:
		return self.run_tool("create_doc", {"doctype": WF, "values": self.wf_values(**over)})

	def park_wf(self, **over) -> str:
		r = self.create_wf(**over)
		self.assertTrue(r.get("ok"), r)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		return self.card_name()

	def update_wf(self, name: str, **changes) -> dict:
		return self.run_tool("update_doc", {"doctype": WF, "name": name, "changes": changes})

	def park_update(self, name: str, **changes) -> str:
		r = self.update_wf(name, **changes)
		self.assertTrue(r.get("ok"), r)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		return self.card_name()

	def card(self, name: str) -> dict:
		return frappe.parse_json(self.row(name).card)

	def call(self, name: str) -> dict:
		return _seal.unseal_call(self.row(name))["args"]

	def desk_workflow(self, name=None, dt=None, **over) -> str:
		"""A workflow saved the way Desk saves it (real DDL on the scratch form)."""
		with as_user(SM_USER):
			doc = frappe.get_doc({"doctype": WF, **self.wf_values(name=name, dt=dt, **over)}).insert()
		frappe.db.commit()
		frappe.clear_messages()
		return doc.name

	def records(self, count: int, dt=None, **values) -> list[str]:
		names = []
		for i in range(count):
			doc = frappe.get_doc({"doctype": dt or self.dt, "title": f"r{i}", **values})
			doc.flags.ignore_permissions = True
			# Before any workflow: no state is set on insert.
			doc.db_insert()
			names.append(doc.name)
		frappe.db.commit()
		return names

	def states_of(self, dt=None, column=STATE) -> dict:
		dt = dt or self.dt
		if column not in _columns(dt):
			return {}
		return dict(frappe.db.sql(f"SELECT name, `{column}` FROM `tab{dt}`"))

	def active(self, dt=None) -> list[str]:
		frappe.db.rollback()
		return frappe.get_all(WF, filters={"document_type": dt or self.dt, "is_active": 1}, pluck="name")

	def assert_no_workflow(self, name=None, dt=None):
		frappe.db.rollback()
		dt = dt or self.dt
		self.assertFalse(frappe.db.exists(WF, name or f"Jwf Flow {self.tag}"), "no workflow row")
		self.assertNotIn(STATE, _field_rows(dt), "no state field row")
		self.assertNotIn(STATE, _columns(dt), "no state column")

	def wf_hook(self, handler: str, event="on_update", doctype=WF) -> None:
		hooks = copy.deepcopy(frappe.get_doc_hooks())
		hooks.setdefault(doctype, {}).setdefault(event, []).append(
			f"jarvis.tests.test_workflow_settings_exception.{handler}"
		)
		frappe.local.doc_events_hooks = hooks
		self.addCleanup(setattr, frappe.local, "doc_events_hooks", None)

	def audit(self, tool="create_doc", doctype=WF) -> list[str]:
		return frappe.get_all(
			AGENT_WRITE,
			filters={"tool": tool, "ref_doctype": doctype, "actor": SM_USER},
			pluck="outcome",
			order_by="creation asc",
		)

	def table_of(self, card: dict, label: str) -> dict:
		"""A create card's table, as {column label: [cell of each row]}."""
		table = next(t for t in card["tables"] if t["label"] == label)
		return {c: [r["cells"][i] for r in table["rows"]] for i, c in enumerate(table["columns"])}


# --------------------------------------------------------------------------- #
# Classification: what is guarded, what stays refused
# --------------------------------------------------------------------------- #
class TestRiskClasses(_WfBase):
	def test_single_writes_are_their_guarded_class(self):
		for tool, args, risk in (
			("create_doc", {"doctype": WF, "values": {}}, "workflow_new"),
			("create_doc", {"doctype": "workflow", "values": {}}, "workflow_new"),
			("update_doc", {"doctype": WF, "name": "x", "changes": {}}, "workflow_edit"),
			("update_doc", {"doctype": DOMAIN, "name": DOMAIN, "changes": {}}, "domain_settings"),
			("update_doc", {"doctype": " domain settings ", "changes": {}}, "domain_settings"),
		):
			with self.subTest(tool=tool, args=args):
				self.assertEqual(wr.risk_of(tool, args), risk)

	def test_everything_else_about_them_stays_plain_structure(self):
		for tool, args in (
			("delete_doc", {"doctype": WF, "name": "x"}),
			("delete_doc", {"doctype": CRM, "name": CRM}),
			("delete_doc", {"doctype": DOMAIN, "name": DOMAIN}),
			("create_doc", {"doctype": CRM, "values": {"close_opportunity_after_days": 3}}),
			("create_doc", {"doctype": DOMAIN, "values": {"active_domains": []}}),
			("create_doc", {"docs": [{"doctype": WF, "values": {}}]}),
			("create_doc", {"docs": [{"doctype": WF, "values": {}}, {"doctype": "ToDo", "values": {}}]}),
			("update_doc", {"doctype": WF, "updates": [{"name": "x", "changes": {"is_active": 1}}]}),
			("update_doc", {"doctype": WF, "changes": {"is_active": 1}}),
			("update_doc", {"doctype": DOMAIN, "name": "Other", "changes": {}}),
			("update_doc", {"doctype": CRM, "name": "Other", "changes": {wr.CRM_SYNC_FIELD: 1}}),
			("submit_doc", {"doctype": WF, "name": "x"}),
			("run_import", {"doctype": WF, "file_url": "/x.csv"}),
		):
			with self.subTest(tool=tool, args=args):
				self.assertEqual(wr.risk_of(tool, args), "structure")
				with self.assertRaises(wr.StructureRefusedError):
					wr.check(tool, args, guarded=True)

	def _raw_row(self, doctype: str, parent: str, parentfield: str, **values) -> str:
		"""A stored child row, written raw (no save of its parent), removed after."""
		row = frappe.get_doc(
			{"doctype": doctype, "parent": parent, "parenttype": parent, "parentfield": parentfield, **values}
		)
		row.db_insert()
		frappe.db.commit()
		self.addCleanup(lambda: (frappe.db.delete(doctype, {"name": row.name}), frappe.db.commit()))
		return row.name

	def test_a_row_of_a_workflow_or_a_setting_is_never_written_on_its_own(self):
		"""Review: a state or transition row saved by its own name went round the
		card, the code line, the compile check and the lock, uncarded in auto mode.
		The child-row rule (aerele/jarvis#1841) refuses it by its STORED parent, and
		this unit's parents (a Workflow, the two settings) are structure parents."""
		wf = self.desk_workflow()
		doc = frappe.get_doc(WF, wf)
		rows = {
			"Workflow Transition": (doc.transitions[0].name, {"condition": "True", "allowed": "All"}, wf),
			"Workflow Document State": (doc.states[0].name, {"allow_edit": "All", "update_field": "who"}, wf),
			"Has Domain": (
				self._raw_row("Has Domain", DOMAIN, "active_domains", domain="Jwf None"),
				{"domain": "Retail"},
				DOMAIN,
			),
			"Frappe CRM Allowed User": (
				self._raw_row("Frappe CRM Allowed User", CRM, "allowed_users", user=SM_USER),
				{"user": NOT_SM},
				CRM,
			),
		}
		frappe.db.set_value("Jarvis Conversation", self.conv, "skip_confirmation", 1)
		frappe.db.commit()
		for doctype, (name, changes, parent) in rows.items():
			with self.subTest(doctype=doctype):
				args = {"doctype": doctype, "name": name, "changes": changes}
				self.assertEqual(wr.risk_of("update_doc", args), wr.CHILD_ROW)
				self.assertEqual(
					wr.risk_of("delete_doc", {"doctype": doctype.lower(), "name": name}), wr.CHILD_ROW
				)
				r = self.run_tool("update_doc", args)
				self.assertEqual(
					(r["error"]["code"], r["error"]["kind"]), ("structure_refused", "not_fixable"), r
				)
				message = r["error"]["message"]
				self.assertIn("is part of", message)
				self.assertIn("Change this through its record", message)
				expected = wr.desk_path(parent if parent != wf else WF, parent)
				self.assertEqual(r["error"]["desk_path"], expected, "the parent record's own page")
				# Its record CAN be changed from chat, through its own card: the
				# refusal sends the assistant there, not to Desk.
				parenttype = WF if parent == wf else parent
				self.assertIn(f"Use update_doc on {parenttype} with its", message)
				self.assertIn("with its name from get_doc", message)
				self.assertIn("confirmation card", message)
				self.assertNotIn("set up in Desk", message)
				self.assertIn("update that record's", r["error"]["hint"])
				# With the site switch off the guarded card is gone, and the wording
				# with it. (CRM Settings with the data sync off is ordinary sensitive
				# configuration, carded whatever the switch says.)
				with patch.object(gs, "disabled", return_value=True):
					off = self.run_tool("update_doc", args)["error"]
				self.assertEqual(off["code"], "structure_refused")
				if parent == CRM:
					self.assertIn("confirmation card", off["message"])
				else:
					self.assertIn("it is set up in Desk, not from chat", off["message"])
					self.assertNotIn("update_doc", off["message"])
					self.assertNotIn("confirmation card", off["message"])
				with as_user(SM_USER):
					out = api.dispatch_confirmed("update_doc", args, uncarded=True)
				self.assertEqual(out["error"]["code"], "structure_refused", out)
		frappe.db.rollback()
		self.assertFalse(frappe.db.get_value("Workflow Transition", doc.transitions[0].name, "condition"))
		self.assertEqual(
			frappe.db.get_value("Workflow Document State", doc.states[0].name, "allow_edit"), "System Manager"
		)
		self.assertEqual(
			wr.doc_risk(doc.transitions[0], "before_validate"), wr.CHILD_ROW, "also at the save itself"
		)
		tasks = {"doctype": "Workflow Transition Tasks", "name": "x", "changes": {"tasks": []}}
		self.assertEqual(
			wr.risk_of("update_doc", tasks), "sensitive", "a task list runs scripts and webhooks"
		)
		self.assertEqual(wr.risk_line("update_doc", tasks), CODE_LINE)

	def test_a_row_of_a_transitions_task_list_is_refused_through_its_list(self):
		"""Workflow Transition Tasks is sensitive (code) in this unit, which is what
		puts its rows under the child-row rule."""
		if not frappe.db.exists("DocType", "Workflow Transition Task"):
			self.skipTest("Frappe 15 has no transition tasks")
		tasks = f"Jwf Tasks {self.tag}"
		frappe.get_doc({"doctype": "Workflow Transition Tasks", "__newname": tasks}).insert()
		frappe.db.commit()
		self.addCleanup(
			lambda: (frappe.db.delete("Workflow Transition Tasks", {"name": tasks}), frappe.db.commit())
		)
		row = frappe.get_doc(
			{
				"doctype": "Workflow Transition Task",
				"parent": tasks,
				"parenttype": "Workflow Transition Tasks",
				"parentfield": "tasks",
				"task": "Webhook",
			}
		)
		row.db_insert()
		frappe.db.commit()
		self.addCleanup(
			lambda: (frappe.db.delete("Workflow Transition Task", {"name": row.name}), frappe.db.commit())
		)
		args = {"doctype": "Workflow Transition Task", "name": row.name, "changes": {"task": "Server Script"}}
		self.assertEqual(wr.risk_of("update_doc", args), wr.CHILD_ROW)
		r = self.run_tool("update_doc", args)
		self.assertEqual((r["error"]["code"], r["error"]["kind"]), ("sensitive_refused", "not_fixable"), r)
		self.assertIn(f"is part of Workflow Transition Tasks {tasks}", r["error"]["message"])

	def test_a_guarded_class_is_handed_back_only_to_a_caller_that_can_card_it(self):
		args = {"doctype": WF, "values": {}}
		self.assertEqual(wr.check("create_doc", args, guarded=True), "workflow_new")
		with self.assertRaises(wr.StructureRefusedError):
			wr.check("create_doc", args)

	def test_each_class_admits_only_itself_at_the_save(self):
		for risk in ("workflow_new", "workflow_edit", "crm_settings_sync", "domain_settings"):
			self.assertEqual(wr._ALLOWABLE[risk], frozenset({risk}))
			self.assertIn(risk, wr.GUARDED_STRUCTURE)
			self.assertIn(risk, wr._STRUCTURE_RISKS, "never carried into a background job")
			self.assertIn(risk, gs.HANDLERS)
		allow = wr.allow_entries("update_doc", {"doctype": DOMAIN, "changes": {"active_domains": []}})
		self.assertEqual(
			[(a.doctype, a.name, a.risks) for a in allow], [(DOMAIN, DOMAIN, {"domain_settings"})]
		)
		allow = wr.allow_entries("create_doc", {"doctype": WF, "values": {"workflow_name": "x"}})
		self.assertEqual([(a.doctype, a.name, a.risks) for a in allow], [(WF, None, {"workflow_new"})])

	def test_the_save_itself_is_classified_the_same_way(self):
		new = frappe.new_doc(WF)
		self.assertEqual(wr.doc_risk(new, "before_validate"), "workflow_new")
		self.assertEqual(wr.doc_risk(new, "on_trash"), "structure")
		self.assertEqual(wr.doc_risk(new, "before_rename"), "structure")
		domain = frappe.get_doc(DOMAIN)
		self.assertEqual(wr.doc_risk(domain, "before_validate"), "domain_settings")
		self.assertEqual(wr.doc_risk(domain, "on_trash"), "structure")


# --------------------------------------------------------------------------- #
# Workflow: park
# --------------------------------------------------------------------------- #
class TestWorkflowPark(_WfBase):
	WIDE = True

	def test_a_new_workflow_parks_a_card_and_nothing_exists(self):
		self.records(3)
		before = _columns(self.dt)
		with patch("jarvis.api._run_preview") as trial:
			r = self.create_wf()
		trial.assert_not_called()  # described, never trial-run
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		self.assertTrue(r["data"]["preview"]["described"])
		card = self.card(self.card_name())
		self.assertEqual((card["kind"], card["doctype"]), ("create", WF))
		self.assertEqual((card["risk"], card["structural"]), ("sensitive", True))
		line = card["risk_line"]
		self.assertTrue(line.startswith(gs.STRUCTURAL_LINE), line)
		self.assertIn(f"Adds the hidden field {STATE} to {self.dt}.", line)
		self.assertIn(f"It becomes the active workflow of {self.dt}", line)
		self.assertIn("It fills the state on 3 records that have none; 0 records keep states", line)
		self.assertIn(ACCESS_LINE, line)
		self.assertNotIn(CODE_LINE, line, "no condition: no code line")
		self.assertNotIn("approve_run", card)
		self.assertEqual(_columns(self.dt), before, "no column after the park")
		self.assert_no_workflow()
		self.assertEqual(self.active(), [])

	def test_the_card_shows_every_state_transition_role_and_self_approval(self):
		name = self.park_wf(
			transitions=[
				{
					"state": self.s_open,
					"action": self.a_ok,
					"next_state": self.s_ok,
					"allowed": "System Manager",
					"condition": "doc.title == 'go'",
					"allow_self_approval": 1,
				},
				{
					"state": self.s_open,
					"action": self.a_no,
					"next_state": self.s_ok,
					"allowed": "Administrator",
				},
			]
		)
		card = self.card(name)
		rows = {r["label"]: r["value"] for r in card["rows"]}
		self.assertEqual(rows["Document Type"], self.dt)
		self.assertEqual(rows["Is Active"], "Yes")
		self.assertEqual(rows["Workflow State Field"], STATE, "Frappe's default, spelled out")
		states = self.table_of(card, "Document States")
		self.assertEqual(states["State"], [self.s_open, self.s_ok])
		self.assertEqual(states["Only Allow Edit For"], ["System Manager", "System Manager"])
		self.assertEqual(states["Doc Status"], ["0", "0"])
		transitions = self.table_of(card, "Transitions")
		self.assertEqual(transitions["Action"], [self.a_ok, self.a_no])
		self.assertEqual(transitions["Allowed"], ["System Manager", "Administrator"])
		self.assertEqual(
			transitions["Allow Self Approval"], ["Yes", "No"], "asked for on one; off where nothing was said"
		)
		self.assertEqual(transitions["Condition"][0], "doc.title == 'go'")
		line = card["risk_line"]
		self.assertNotIn(CODE_LINE, line, "a comparison chat may write is shown, not called code")
		self.assertIn("Self-approval is allowed on 1 of 2 transitions", line)
		self.assertLess(
			line.index("Self-approval is allowed"), line.index(ACCESS_LINE), "what is particular comes first"
		)

	def test_a_transition_from_chat_does_not_allow_self_approval_unless_asked(self):
		"""Owner decision R2-14: Frappe's default is on, and a request for "needs the
		manager's approval" then made a rule its own author could pass."""
		from frappe.model.workflow import apply_workflow

		name = self.park_wf()
		card = self.card(name)
		self.assertEqual(self.call(name)["values"]["transitions"][0]["allow_self_approval"], 0)
		self.assertEqual(self.table_of(card, "Transitions")["Allow Self Approval"], ["No"])
		self.assertIn(
			"Self-approval is off on every transition: the workflow action refuses the person who "
			"created the record (the Administrator excepted).",
			card["risk_line"],
		)
		self.assertNotIn("Self-approval is allowed", card["risk_line"])
		self.assertTrue(self.confirm(name)["ok"])
		with as_user(SM_USER):
			doc = frappe.get_doc({"doctype": self.dt, "title": "mine"}).insert()
			with self.assertRaisesRegex(frappe.ValidationError, "Self approval is not allowed"):
				apply_workflow(doc, self.a_ok)
		frappe.db.rollback()
		# A transition someone made in Desk keeps what it has when chat edits the workflow.
		wf = f"Jwf Flow {self.tag}"
		frappe.db.set_value("Workflow Transition", {"parent": wf}, "allow_self_approval", 1)
		frappe.db.commit()
		frappe.clear_document_cache(WF, wf)
		self.conv = self.make_conv(SM_USER)
		row = frappe.get_doc(WF, wf).transitions[0].name
		edit = self.park_update(wf, send_email_alert=1, transitions=[{"name": row}])
		self.assertEqual(self.call(edit)["changes"]["transitions"][0]["allow_self_approval"], 1)

	def test_each_condition_gets_a_row_of_its_own_shown_whole(self):
		"""Flow review: the Transitions table scrolls sideways on the board and the
		phone, and a condition is the one piece of code on the card. Each one is also
		a full-width row, whole and wrapped, beside its place in the table."""
		t = {"state": self.s_open, "allowed": "System Manager"}
		long = " or ".join(["doc.idx > 1"] * 15)
		self.assertGreaterEqual(len(long), 215)
		name = self.park_wf(
			states=[
				{"state": self.s_open, "allow_edit": "System Manager"},
				{"state": self.s_ok, "allow_edit": "System Manager"},
				{"state": self.s_no, "allow_edit": "System Manager"},
			],
			transitions=[
				{**t, "action": self.a_ok, "next_state": self.s_ok, "condition": "doc.title == 'go'"},
				{**t, "action": self.a_no, "next_state": self.s_no, "condition": long},
				{**t, "action": self.a_no, "next_state": self.s_no, "condition": "doc.idx == 2"},
				{**t, "action": self.a_ok, "next_state": self.s_no},
			],
		)
		card = self.card(name)
		rows = {r["label"]: r for r in card["rows"]}
		first = rows[f"Condition on {self.a_ok}, from {self.s_open} to {self.s_ok}"]
		self.assertEqual((first["value"], first["multiline"]), ("doc.title == 'go'", True))
		second = rows[f"Condition on {self.a_no}, from {self.s_open} to {self.s_no} (row 2)"]
		self.assertEqual(second["value"], long, "whole: not clipped, no ellipsis")
		self.assertTrue(second["multiline"])
		self.assertEqual(
			rows[f"Condition on {self.a_no}, from {self.s_open} to {self.s_no} (row 3)"]["value"],
			"doc.idx == 2",
		)
		self.assertEqual(
			sum(1 for label in rows if label.startswith("Condition on")), 3, "only rows that carry one"
		)
		self.assertEqual(
			self.table_of(card, "Transitions")["Condition"][1], long, "the table keeps its column"
		)
		wide = "This table is wider than the card. Scroll it sideways to see every column."
		for table in card["tables"]:
			self.assertIn(wide, table["note"], table["label"])

	def test_the_wide_table_note_is_for_full_cards_and_wide_tables_only(self):
		from jarvis.chat import _record_summary as rs

		wide = "This table is wider than the card. Scroll it sideways to see every column."
		row = {
			"state": self.s_open,
			"action": self.a_ok,
			"next_state": self.s_ok,
			"allowed": "All",
			"condition": "x",
		}
		meta = frappe.get_meta(WF)
		self.assertEqual(rs.table_rows(meta, "transitions", [row], True)["note"], wide)
		self.assertNotIn(
			"note", rs.table_rows(meta, "transitions", [row], False), "an ordinary card is unchanged"
		)
		self.assertNotIn(
			"note", rs.table_rows(meta, "transitions", [{"state": self.s_open}], True), "a narrow one"
		)
		broken = rs.table_rows(meta, "transitions", [{**row, "condition": "a\rb"}], True)["note"]
		self.assertIn(rs.HIDDEN_BREAK_NOTE, broken)
		self.assertIn(wide, broken, "joined, neither dropped")

	def test_what_a_workflow_carries_is_said(self):
		name = self.park_wf(
			send_email_alert=1,
			states=[
				{"state": self.s_open, "allow_edit": "System Manager"},
				{
					"state": self.s_ok,
					"allow_edit": "System Manager",
					"update_field": "title",
					"update_value": "approved",
				},
			],
		)
		line = self.card(name)["risk_line"]
		self.assertIn(f"Moving a record to {self.s_ok} also sets its title to 'approved'.", line)
		self.assertIn("It emails the people who can act next on each record.", line)

	def test_card_seal_and_write_are_one_stored_form(self):
		"""Lesson 2: words, padding and numbers are read once, as the save stores
		them, and that one form is carded, sealed and written."""
		name = self.park_wf(
			is_active="yes",
			send_email_alert=" 1 ",
			override_status="false",
			states=[
				{"state": self.s_open, "allow_edit": "System Manager", "doc_status": 0, "send_email": "no"},
				{"state": self.s_ok, "allow_edit": "System Manager", "doc_status": " 0 "},
			],
			transitions=[
				{
					"state": self.s_open,
					"action": self.a_ok,
					"next_state": self.s_ok,
					"allowed": "System Manager",
					"allow_self_approval": "FALSE",
					"condition": "  doc.title == 'x'  ",
				}
			],
		)
		sealed = self.call(name)
		self.assertEqual(sealed["doctype"], WF)
		values = sealed["values"]
		self.assertEqual(
			(values["is_active"], values["send_email_alert"], values["override_status"]), (1, 1, 0)
		)
		self.assertEqual([s["doc_status"] for s in values["states"]], ["0", "0"])
		self.assertEqual([s["send_email"] for s in values["states"]], [0, 1])
		self.assertEqual(values["transitions"][0]["allow_self_approval"], 0)
		self.assertEqual(values["transitions"][0]["condition"], "doc.title == 'x'")
		card = self.card(name)
		self.assertEqual(self.table_of(card, "Transitions")["Allow Self Approval"], ["No"])
		self.assertNotIn("Self-approval is allowed", card["risk_line"])
		out = self.confirm(name)
		self.assertTrue(out["ok"], out)
		doc = frappe.get_doc(WF, values["workflow_name"])
		self.assertEqual((doc.is_active, doc.send_email_alert, doc.override_status), (1, 1, 0))
		self.assertEqual(doc.transitions[0].allow_self_approval, 0)
		self.assertEqual(doc.transitions[0].condition, "doc.title == 'x'")
		self.assertEqual([s.send_email for s in doc.states], [0, 1])

	def test_a_park_changes_nothing_another_request_reads(self):
		"""Lesson 1: the hypothetical state field never reaches a cached form."""
		before = [df.fieldname for df in frappe.get_meta(self.dt).fields]
		self.park_wf()
		metas = [frappe.get_meta(self.dt), frappe.get_meta(self.dt, cached=False)]
		client_cache = getattr(frappe, "client_cache", None)
		if client_cache is not None and client_cache.get_value(f"doctype_meta::{self.dt}"):
			metas.append(client_cache.get_value(f"doctype_meta::{self.dt}"))
		for meta in metas:
			self.assertEqual([df.fieldname for df in meta.fields], before)
		with as_user(SM_USER):
			frappe.get_doc({"doctype": self.dt, "title": "after the park"}).insert()
		frappe.db.rollback()
		self.assertEqual(self.active(), [], "nothing is enforced by an unconfirmed card")

	def test_each_check_refuses_naming_what_is_wrong(self):
		t = {"state": self.s_open, "action": self.a_ok, "next_state": self.s_ok, "allowed": "System Manager"}
		one = {"state": self.s_open, "allow_edit": "System Manager"}
		for over, says in (
			({"workflow_name": ""}, "workflow_name is missing"),
			({"document_type": "Jwf No Such Form"}, "there is no DocType named"),
			({"document_type": self.dt.lower()}, f"is named {self.dt!r}"),
			(
				{"states": [{"state": "Jwf Nope", "allow_edit": "System Manager"}]},
				"does not exist yet: state 'Jwf Nope'",
			),
			(
				{
					"states": [
						{"state": "Jwf Nope", "allow_edit": "System Manager"},
						{"state": "Jwf Nada", "allow_edit": "System Manager"},
					],
					"transitions": [{**t, "state": "Jwf Nope", "action": "Jwf Go", "next_state": "Jwf Nada"}],
				},
				"does not exist yet: states 'Jwf Nope', 'Jwf Nada'; action 'Jwf Go'. Each is an ordinary record",
			),
			(
				{"states": [{"state": self.s_open.upper(), "allow_edit": "System Manager"}]},
				"Use its exact name",
			),
			({"states": [{"state": self.s_open, "allow_edit": "Jwf No Role"}]}, "no Role named"),
			({"states": [{"state": self.s_open}]}, "allow_edit is missing"),
			({"transitions": [{**t, "action": "Jwf Nope"}]}, "does not exist yet: action 'Jwf Nope'"),
			({"transitions": [{**t, "next_state": self.s_no}]}, "not a valid State"),
			({"transitions": [{**t, "condition": "doc.title ="}]}, "is not set from chat"),
			({"transitions": [{**t, "condition": "(lambda: 1)()"}]}, "is not set from chat"),
			({"transitions": [{**t, "condition": "doc.__class__"}]}, "is not set from chat"),
			({"states": [], "transitions": []}, "needs at least one state"),
			({"states": [one, one], "transitions": []}, "is listed more than once"),
			(
				{"states": [{**one, "doc_status": 1}], "transitions": []},
				"cannot be submitted",
			),
			({"states": [{**one, "doc_status": "7"}], "transitions": []}, 'must be one of "0", "1", "2"'),
			(
				{"states": [{**one, "update_field": "nope"}], "transitions": []},
				"not a field of",
			),
			({"workflow_state_field": "who"}, "cannot hold a workflow state"),
			({"workflow_state_field": "Bad Name"}, "cannot be used as the workflow state field"),
			({"workflow_data": {"x": 1}}, "Workflow Builder"),
			({"bogus": 1}, "no property named bogus"),
			(
				{"states": [{"state": self.s_open, "allow_edit": "System Manager", "bogus": 1}]},
				"no property named bogus",
			),
			({"states": "x"}, "must be a list of rows"),
			({"is_active": "maybe"}, "Is Active"),
		):
			with self.subTest(over=over):
				self.assert_refused(self.create_wf(**over), "InvalidArgumentError", says)
				self.assert_no_workflow()

	def test_refusals_hold_however_the_call_is_spelled(self):
		"""Lesson 3: the doctype in another case or padded, and keys of a row."""
		for doctype in ("workflow", " Workflow ", "WORKFLOW"):
			r = self.run_tool(
				"create_doc", {"doctype": doctype, "values": self.wf_values(document_type="User")}
			)
			self.assert_refused(r, "structure_refused", "A workflow on User is not set up from chat")
		name = self.run_tool("create_doc", {"doctype": "workflow", "values": self.wf_values()})
		self.assertEqual(name["data"]["status"], "pending_confirmation", name)
		self.assertEqual(self.call(self.card_name())["doctype"], WF, "sealed by its exact name")

	def test_forms_a_workflow_is_never_put_on_from_chat(self):
		for dt in (
			"User",
			"Role",
			"DocType",
			"Custom Field",
			"Server Script",
			"Version",
			"Jarvis Settings",
			"Has Role",
		):
			with self.subTest(dt=dt):
				r = self.create_wf(dt=dt)
				self.assert_refused(r, "structure_refused", f"A workflow on {dt} is not set up from chat")
				self.assertEqual(r["error"]["desk_path"], "/app/workflow/new")
		for dt, says in (
			("System Settings", "is not set up from chat"),
			("Website Settings", "a single settings document"),
		):
			with self.subTest(dt=dt):
				r = self.create_wf(dt=dt)
				self.assertFalse(r["ok"], r)
				self.assertIn(says, r["error"]["message"])
		child = frappe.get_all("DocType", filters={"istable": 1, "module": "Contacts"}, pluck="name", limit=1)
		if child:
			self.assert_refused(self.create_wf(dt=child[0]), "InvalidArgumentError", "a child table")
		self.assertFalse(frappe.db.exists(PA, {"conversation": self.conv}))

	def test_transition_tasks_are_never_attached_from_chat(self):
		if "transition_tasks" not in wg._row_keys("transitions"):
			self.skipTest("Frappe 15 has no transition tasks")
		t = {"state": self.s_open, "action": self.a_ok, "next_state": self.s_ok, "allowed": "System Manager"}
		r = self.create_wf(transitions=[{**t, "transition_tasks": "Anything"}])
		self.assert_refused(r, "InvalidArgumentError", "transition tasks run scripts and webhooks")

	def test_an_expression_as_update_value_is_code(self):
		if "evaluate_as_expression" not in wg._row_keys("states"):
			self.skipTest("Frappe 15 has no expressions on a state")
		state = {
			"state": self.s_ok,
			"allow_edit": "System Manager",
			"update_field": "title",
			"evaluate_as_expression": 1,
		}
		first = {"state": self.s_open, "allow_edit": "System Manager"}
		r = self.create_wf(states=[first, {**state, "update_value": "doc.title +"}])
		self.assert_refused(r, "InvalidArgumentError", "is not set from chat")
		name = self.park_wf(states=[first, {**state, "update_value": "doc.who"}])
		self.assertNotIn(CODE_LINE, self.card(name)["risk_line"], "an allowed expression is shown whole")

	def test_a_state_field_that_cannot_be_added_is_refused_at_park(self):
		"""Lesson 8: what could only fail at Confirm is refused at park."""
		frappe.db.sql_ddl(f"ALTER TABLE `tab{self.dt}` ADD COLUMN `jwf_state` varchar(140)")
		r = self.create_wf(workflow_state_field="jwf_state")
		self.assert_refused(r, "InvalidArgumentError", "already has a column named jwf_state")
		r = self.create_wf(dt=self.wide)
		self.assert_refused(r, "InvalidArgumentError", ["no room for the field", STATE])
		self.assert_no_workflow(dt=self.wide)

	def test_a_condition_may_only_compare_the_documents_own_fields(self):
		"""Controller default (the owner may relax it): Frappe evaluates a condition on
		the server for every user, with ``frappe.db.get_value`` in reach. From chat it
		is one restricted expression over ``doc``; anything more is set up in Desk."""
		allowed = (
			"doc.title == 'go'",
			"doc.docstatus == 0 and not doc.owner == 'x'",
			"doc.get('title') in ('a', 'b') or doc.get('idx', 0) <= 2.5",
			"doc['title'] != \"x\" and (doc.idx * 2 + 1) / 4 >= -doc.docstatus",
			"doc.who not in ['a@b.c', 'c@d.e'] and doc.owner is not None",
			"doc.title == 'Geöffnet'",
			"doc.workflow_state == 'x' or doc.name != 'y'",
			"1 < doc.idx <= 5",
			"not (doc.idx > 3 or doc.title in {'a', 'b'})",
			# ordering two numbers, equality of anything, a plain non-zero divisor
			"doc.idx > 1 and doc.creation == '2026-01-01'",
			"doc.get('idx', 0) > 1 and doc.title != None",
			"doc.idx / 2 > 1 and doc.idx % -3 == 0",
			"doc.idx >= True",
		)
		refused = (
			"frappe.db.get_value('User', doc.owner, 'api_key') == 1",
			"frappe.session.user == doc.owner",
			"__import__('os').system('id') == 0",
			"doc.__class__ == 1",
			"doc.save() == 1",
			"doc.get('title').upper() == 'A'",
			"doc.items[0].qty > 1",
			"(lambda: 1)() == 1",
			"[x for x in (1, 2)] == 1",
			"any(d for d in doc.items)",
			"f'{doc.title}' == 'a'",
			"(y := 1) == 1",
			"(doc.title if doc.idx else 1) == 1",
			"other.title == 1",
			"frappe",
			"user == 'Administrator'",
			"doc.get(doc.title) == 1",
			"doc.get('title', b=1) == 1",
			"doc.idx ** 99 > 1",
			"'a' * 99 == doc.title",
			"doc.title == 'a\u200bb'",
			"\uff44oc.title == 1",
			"doc.title ==",
			" or ".join(["doc.idx > 1"] * 80),
			# arithmetic on a container, and a container that is not small (the review)
			"[0] * 999999999 == doc.title",
			"(0,) * 99999999 * 99 == doc.title",
			"doc.title in [1] * 99999999",
			"doc.title == [1, 2]",
			"doc.title in (" + ", ".join(str(i) for i in range(101)) + ")",
			"doc.idx > 99999999999999999999",
			# the top level is a comparison, or and / or / not of comparisons
			"True",
			"1",
			"[1, 2]",
			"'5'",
			"doc.idx",
			"doc.idx + 1",
			"not doc.title",
			"doc.idx > 1 and doc.title",
			# only real fields of this form
			"doc.nope > 1",
			"doc.save == 1",
			"doc.get('nope') == 1",
			"doc['nope'] == 1",
			# text formatting, text arithmetic, division by nothing
			"doc.title % 'x' == 1",
			"'%s' % doc.title == 'a'",
			"doc.title == 'a' + 'b'",
			"doc.title * 999999999 == 'x'",
			"doc.title + 1 > 2",
			"doc.idx / 0 > 1",
			"doc.idx % 0 == 1",
			"doc.idx // 0.0 == 1",
			# what raises when Frappe evaluates it, for every user who opens the record:
			# ordering anything but two numbers, and a divisor that can be zero
			"doc.creation > '2026-01-01'",
			"doc.idx > 1 and doc.modified >= '2026-11-01'",
			"'2026-01-01' < doc.creation",
			"doc.idx >= '10'",
			"doc.title > 'a'",
			"1 < doc.title",
			"doc.idx > None",
			"1 < doc.idx < doc.title",
			"doc.get('idx', 'x') > 1",
			"doc.get('title', 0) > 1",
			"doc.idx / doc.docstatus > 1",
			"doc.idx / (1 - 1) > 1",
			"doc.idx % doc.idx == 0",
			"doc.idx // -0 == 1",
			"doc.idx / True > 1",
			"-None > 1",
			"None + 1 > 0",
			"True + None == 1",
			# enforced since the first cut, pinned here (the review's surviving mutations)
			"doc.db_set('title') == 1",
			"doc.get_value('title') == 1",
			"doc.idx * 'abc' > 1",
			"other['title'] == 1",
			# in / not in a literal list only
			"doc.title in doc.who",
			"doc.title in 'abc'",
			"doc.title not in (doc.who, 'a')",
		)
		for expression in allowed:
			self.assertIsNone(wg.expression_problem(expression, self.dt), expression)
		for expression in refused:
			self.assertTrue(wg.expression_problem(expression, self.dt), expression)
		self.assertIn("nope", wg.expression_problem("doc.nope > 1", self.dt), "the field is named")
		self.assertIn(
			"Desk", wg.expression_problem("doc.creation > '2026-01-01'", self.dt), "where a date rule goes"
		)
		self.assertIn("use == or !=", wg.expression_problem("doc.title > 'a'", self.dt))
		for expression in ("doc.creation > '2026-01-01'", "doc.title > 'a'"):
			# The tool layer strips what looks like a tag: "< or >" came out as "".
			said = wg.expression_problem(expression, self.dt)
			self.assertEqual(frappe.utils.strip_html(said), said)
			self.assertNotIn("<", said)
		# Only the types Frappe never leaves empty are numbers: a Duration, a Long Int
		# and a Rating can be NULL, and None is neither ordered nor added to.
		kinds = wg._fields_of(self.dt)
		self.assertEqual((kinds["idx"], kinds["title"], kinds["creation"]), (wg.NUMBER, wg.TEXT, wg.DATE))
		self.assertEqual(wg._NUMERIC_TYPES, {"Int", "Float", "Currency", "Percent", "Check"})
		nullable = {**kinds, "dur": wg.TEXT, "stars": wg.TEXT, "big": wg.TEXT}
		with patch.object(wg, "_fields_of", return_value=nullable):
			for expression in (
				"doc.dur > 86400",
				"doc.get('stars', 0) > 3",
				"doc.big + 1 == 2",
				"-doc.dur < 0",
				"doc['dur'] / 60 > 3",
			):
				self.assertTrue(wg.expression_problem(expression, self.dt), expression)
			self.assertIsNone(wg.expression_problem("doc.dur == None or doc.stars == 3", self.dt))
		self.assertIn("not zero", wg.expression_problem("doc.idx / doc.docstatus > 1", self.dt))
		# A value Frappe 16 computes for a state (update value as an expression) need
		# not be a comparison; everything else holds for it too.
		for expression in ("doc.idx + 1", "doc.title", "'x'", "doc.idx > 1", "-doc.idx * 2"):
			self.assertIsNone(wg.expression_problem(expression, self.dt, value=True), expression)
		for expression in (
			"frappe.session.user",
			"[0] * 9",
			"doc.nope",
			"doc.title * 3",
			"[1, 2]",
			"doc.idx / doc.docstatus",
			"doc.creation > '2026-01-01'",
		):
			self.assertTrue(wg.expression_problem(expression, self.dt, value=True), expression)
		t = {"state": self.s_open, "action": self.a_ok, "next_state": self.s_ok, "allowed": "System Manager"}
		bad = "frappe.db.get_value('User', doc.owner, 'api_key')"
		r = self.create_wf(transitions=[{**t, "condition": bad}])
		self.assert_refused(
			r,
			"InvalidArgumentError",
			[bad, "may only compare this document's own fields", "doc.grand_total > 10000", "in Desk"],
		)
		r = self.create_wf(transitions=[{**t, "condition": "doc.amount > 5"}])
		self.assert_refused(r, "InvalidArgumentError", ["amount", f"not a field of {self.dt}"])
		name = self.park_wf(transitions=[{**t, "condition": "doc.get('title') in ('a', 'b')"}])
		self.assertNotIn(CODE_LINE, self.card(name)["risk_line"], "an allowed condition is not called code")

	def test_roles_a_workflow_hands_out(self):
		"""Guest is never an approver or an editor; everyone signed in may be, and
		the banner says so."""
		t = {"state": self.s_open, "action": self.a_ok, "next_state": self.s_ok, "allowed": "System Manager"}
		one = {"state": self.s_open, "allow_edit": "System Manager"}
		two = {"state": self.s_ok, "allow_edit": "System Manager"}
		r = self.create_wf(transitions=[{**t, "allowed": "Guest"}])
		self.assert_refused(r, "InvalidArgumentError", ["Guest", "not signed in"])
		self.assertEqual(r["error"]["kind"], "fixable", "a value the request can correct stays fixable")
		r = self.create_wf(states=[{**one, "allow_edit": "Guest"}, two])
		self.assert_refused(r, "InvalidArgumentError", ["Guest", "not signed in"])
		name = self.park_wf(states=[{**one, "allow_edit": "All"}, two], transitions=[{**t, "allowed": "All"}])
		line = self.card(name)["risk_line"]
		self.assertIn(f"Anyone signed in can change records in {self.s_open}.", line)
		self.assertIn(f"Anyone signed in can take {self.a_ok} on records in {self.s_open}.", line)
		self.conv = self.make_conv(SM_USER)
		name = self.park_wf(name=f"Jwf Admin {self.tag}", transitions=[{**t, "allowed": "Administrator"}])
		self.assertIn(
			f"Only the Administrator can take {self.a_ok} on records in {self.s_open}.",
			self.card(name)["risk_line"],
		)

	def test_the_fill_cap_holds_at_its_real_boundary_with_and_without_a_new_column(self):
		"""5,000 records is the most one confirm fills, counted on the form, whether
		the state column is new or already there."""
		self.assertEqual(wg.FILL_NAMES_MAX, 5000)
		for existing in (False, True):
			if existing:
				self.desk_workflow(name=f"Jwf Old {self.tag}", is_active=0, states=[], transitions=[])
			with self.subTest(existing=existing):
				with patch.object(wg, "_count_fill", return_value=5001):
					r = self.create_wf()
				self.assert_refused(r, "structure_refused", "More than 5000 records")
				with patch.object(wg, "_count_fill", return_value=5000):
					name = self.park_wf()
				self.assertIn(
					"It fills the state on 5000 records that have none", self.card(name)["risk_line"]
				)
				frappe.db.delete(PA, {"conversation": self.conv})
				frappe.db.commit()

	def test_a_state_field_frappe_would_rename_or_cannot_fill_is_refused(self):
		"""Review: Frappe saves a field named ``parent`` as ``parent1`` and then fills
		``parent``; ``select`` is a method of its query table. Both parked, added a
		column and only then failed."""
		for fieldname in ("parent", "flags", "file_list", "parenttype", "select", "update", "insert"):
			with self.subTest(fieldname=fieldname):
				r = self.create_wf(workflow_state_field=fieldname)
				self.assert_refused(r, "InvalidArgumentError", [fieldname, "workflow_state"])
		self.assert_no_workflow()

	def test_values_too_long_to_store_are_refused_at_park(self):
		t = {"state": self.s_open, "action": self.a_ok, "next_state": self.s_ok, "allowed": "System Manager"}
		r = self.create_wf(name="Jwf " + "x" * 200)
		self.assert_refused(r, "InvalidArgumentError", "140 characters")
		r = self.create_wf(transitions=[{**t, "workflow_builder_id": "b" * 300}])
		self.assert_refused(r, "InvalidArgumentError", "140 characters")

	def test_a_table_with_pending_changes_is_refused(self):
		sort_field = "creation" if frappe.__version__.startswith("15") else "modified"
		frappe.db.set_value("DocType", self.dt, "sort_field", sort_field, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache(doctype=self.dt)
		self.addCleanup(self._restore_sort_field)
		r = self.create_wf()
		self.assert_refused(r, "structure_refused", "other structure changes waiting")
		self.assertEqual(r["error"]["desk_path"], "/app/workflow/new")

	def _restore_sort_field(self):
		default = "modified" if frappe.__version__.startswith("15") else "creation"
		frappe.db.set_value("DocType", self.dt, "sort_field", default, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache(doctype=self.dt)

	def test_an_existing_state_field_means_no_column_and_no_structural_line(self):
		self.desk_workflow(name=f"Jwf Old {self.tag}", is_active=0)
		self.records(2)
		name = self.park_wf()
		line = self.card(name)["risk_line"]
		self.assertTrue(
			line.startswith(f"Confirming changes the workflow rules of {self.dt} for every user."), line
		)
		self.assertNotIn(gs.STRUCTURAL_LINE, line)
		self.assertNotIn("Adds the hidden field", line)
		self.assertTrue(self.card(name)["structural"], "still a guarded card")

	def test_the_counts_are_frappes_own(self):
		"""Plan rev 4 item 4: N records filled, M keep a state this workflow lacks."""
		self.desk_workflow(
			name=f"Jwf Old {self.tag}",
			is_active=0,
			states=[{"state": self.s_no, "allow_edit": "System Manager"}],
			transitions=[],
		)
		names = self.records(5)
		frappe.db.set_value(self.dt, names[0], STATE, self.s_no, update_modified=False)
		frappe.db.set_value(self.dt, names[1], STATE, self.s_no, update_modified=False)
		frappe.db.set_value(self.dt, names[2], STATE, self.s_ok, update_modified=False)
		frappe.db.commit()
		line = self.card(self.park_wf())["risk_line"]
		self.assertIn(
			"It fills the state on 2 records that have none; 2 records keep states this workflow lacks, "
			"and they cannot be saved or moved until their state is changed to one of this workflow's.",
			line,
		)
		self.conv = self.make_conv(SM_USER)
		line = self.card(self.park_wf(is_active=0))["risk_line"]
		self.assertIn("2 records keep states this workflow lacks.", line, "inactive: nothing is held yet")
		self.conv = self.make_conv(SM_USER)
		with patch.object(wg, "KEPT_COUNT_MAX", 1):
			line = self.card(self.park_wf())["risk_line"]
		self.assertIn("more than 1 record keep states this workflow lacks", line, "counted to a bound")
		frappe.db.set_value(self.dt, names[3], STATE, "", update_modified=False)
		frappe.db.commit()
		self.assertEqual(
			wg._count_kept(self.dt, STATE, [self.s_open, self.s_ok]), 2, "an empty state is filled, not kept"
		)

	def test_the_counts_stop_at_their_bound(self):
		"""Review: the fill was counted whole before the cap was applied."""
		self.desk_workflow(name=f"Jwf Old {self.tag}", is_active=0, states=[], transitions=[])
		self.records(4)
		with patch.object(wg, "FILL_NAMES_MAX", 2):
			self.assertEqual(wg._count_fill(self.dt, STATE, [0], True), 3, "one past the cap, no further")
		with patch.object(wg, "KEPT_COUNT_MAX", 2):
			self.assertEqual(wg._count_kept(self.dt, STATE, [self.s_open]), 0)
			frappe.db.sql(f"UPDATE `tab{self.dt}` SET `{STATE}`=%s", self.s_no)
			self.assertEqual(wg._count_kept(self.dt, STATE, [self.s_open]), 3)

	def test_a_form_with_no_table_is_refused(self):
		real = frappe.db.table_exists

		def gone(doctype, *args, **kwargs):
			return False if doctype == self.dt else real(doctype, *args, **kwargs)

		with patch.object(frappe.db, "table_exists", side_effect=gone):
			r = self.create_wf()
		self.assert_refused(r, "structure_refused", "has no table of its own")

	def test_what_changes_a_lot_is_said_or_refused(self):
		"""Review: an active workflow with no transitions, an existing text field used
		as the state field, and a state that writes an access-bearing field."""
		line = self.card(self.park_wf(transitions=[]))["risk_line"]
		self.assertIn("It has no transitions: no record of this form can be moved", line)
		self.conv = self.make_conv(SM_USER)
		line = self.card(self.park_wf(workflow_state_field="title"))["risk_line"]
		self.assertIn("The state is kept in the existing field Title 0 (title)", line)
		self.conv = self.make_conv(SM_USER)
		states = [
			{"state": self.s_open, "allow_edit": "System Manager"},
			{"state": self.s_ok, "allow_edit": "System Manager", "update_field": "who", "update_value": "x"},
		]
		with patch.dict(wr._FIELD_SENSITIVE, {self.dt: ("access", ("who",))}):
			r = self.create_wf(states=states)
		self.assert_refused(r, "InvalidArgumentError", ["'who'", "gives access"])
		# Employee status switches the linked login on and off.
		real = frappe.get_meta
		employee = frappe._dict(get_field=lambda name: frappe._dict(fieldtype="Select"))
		with (
			patch.object(
				frappe,
				"get_meta",
				side_effect=lambda dt, *a, **k: employee if dt == "Employee" else real(dt, *a, **k),
			),
			self.assertRaises(wg.InvalidFieldValueError) as caught,
		):
			wg.NewWorkflow({})._check_updates([{"state": "x", "update_field": "status"}], "Employee")
		self.assertIn("gives access", str(caught.exception))

	def test_it_says_which_active_workflow_it_replaces(self):
		old = self.desk_workflow(name=f"Jwf Old {self.tag}")
		line = self.card(self.park_wf())["risk_line"]
		self.assertIn(f"It replaces the active workflow {old}.", line)
		self.assertEqual(self.active(), [old], "still active: nothing ran")

	def test_an_inactive_workflow_says_so(self):
		line = self.card(self.park_wf(is_active=0))["risk_line"]
		self.assertIn("It is saved inactive: nothing is enforced until it is made active.", line)
		self.assertNotIn("becomes the active workflow", line)

	def test_too_many_records_to_undo_is_refused(self):
		self.desk_workflow(name=f"Jwf Old {self.tag}", is_active=0, states=[], transitions=[])
		self.records(3)
		with patch.object(wg, "FILL_NAMES_MAX", 2):
			r = self.create_wf()
		self.assert_refused(r, "structure_refused", "More than 2 records")

	def test_someone_who_is_not_a_system_manager_is_refused_at_park(self):
		r = self.run_tool(
			"create_doc",
			{"doctype": WF, "values": self.wf_values()},
			conv=self.make_conv(NOT_SM),
			user=NOT_SM,
		)
		self.assertFalse(r["ok"], r)
		self.assertEqual(r["error"]["code"], "PermissionDeniedError", r)
		self.assert_no_workflow()

	def test_workflow_create_permission_is_checked_at_park(self):
		real = frappe.has_permission

		def no_workflow_create(doctype=None, ptype="read", *args, **kwargs):
			if (doctype, ptype) == (WF, "create"):
				return False
			return real(doctype, ptype, *args, **kwargs)

		with patch.object(frappe, "has_permission", side_effect=no_workflow_create):
			r = self.create_wf()
		self.assertEqual(r["error"]["code"], "PermissionDeniedError", r)
		self.assertIn("permission to create a Workflow", r["error"]["message"])

	def test_a_state_field_of_another_type_is_refused(self):
		with as_user(SM_USER):
			frappe.get_doc(
				{
					"doctype": "Custom Field",
					"dt": self.dt,
					"fieldname": "jcf_num",
					"label": "Num",
					"fieldtype": "Int",
				}
			).insert()
		frappe.db.commit()
		r = self.create_wf(workflow_state_field="jcf_num")
		self.assert_refused(r, "InvalidArgumentError", "cannot hold a workflow state (Int)")

	def test_the_off_switch_refuses_like_any_structure_write(self):
		with patch.object(gs, "disabled", return_value=True):
			r = self.create_wf()
		self.assert_refused(r, "structure_refused", "Workflow changes the database structure")
		name = self.park_wf()
		with patch.object(gs, "disabled", return_value=True):
			out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual(out["error"]["code"], "structure_refused", "a card parked before the switch")
		self.assert_no_workflow()

	def test_routes_without_a_card_still_refuse(self):
		args = {"doctype": WF, "values": self.wf_values()}
		with as_user(SM_USER):
			out = api.dispatch_confirmed("create_doc", args, allow_risky=True)
			self.assertEqual(
				out["error"]["code"], "structure_refused", "no lock held: not its card's confirm"
			)
			with gs.structure_lock(self.dt):
				out = api.dispatch_confirmed("create_doc", args)
			self.assertEqual(
				out["error"]["code"], "structure_refused", "a lock alone is not a confirmed card"
			)
			out = api.dispatch_confirmed("create_doc", args, uncarded=True)
			self.assertEqual(out["error"]["code"], "structure_refused")
		self.assert_no_workflow()

	def test_every_uncarded_mode_parks_instead_of_running(self):
		frappe.db.set_value("Jarvis Conversation", self.conv, "skip_confirmation", 1)
		frappe.db.commit()
		r = self.create_wf()
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		self.assert_no_workflow()

	def test_the_draft_panel_turns_it_into_the_gated_card(self):
		with as_user(SM_USER), patch("jarvis.chat.events.publish_to_user"):
			out = actions_api.apply_action(
				action={
					"verb": "create",
					"doctype": WF,
					"values": self.wf_values(),
					"conversation": self.conv,
					"message": "m1",
				}
			)
		self.assertTrue(out.get("parked"), out)
		self.assertTrue(self.card(self.card_name())["structural"])
		self.assert_no_workflow()


# --------------------------------------------------------------------------- #
# Workflow: confirm, failure, clean-up
# --------------------------------------------------------------------------- #
class TestWorkflowConfirm(_WfBase):
	WIDE = True

	def test_confirm_makes_the_workflow_its_field_and_fills_the_states(self):
		names = self.records(3)
		name = self.park_wf()
		out = self.confirm(name)
		self.assertTrue(out["ok"], out)
		self.assertEqual((out["pa_status"], out["outcome"]), ("Executed", "confirmed"))
		wf = f"Jwf Flow {self.tag}"
		self.assertEqual(self.active(), [wf])
		self.assertIn(STATE, _field_rows(self.dt))
		self.assertIn(STATE, _columns(self.dt))
		self.assertEqual(self.states_of(), dict.fromkeys(names, self.s_open))
		self.assertEqual(self.audit(), ["applied"])
		again = self.confirm(name)
		self.assertFalse(again["ok"], "a second confirm of the same card runs nothing")
		with as_user(SM_USER):
			doc = frappe.get_doc({"doctype": self.dt, "title": "under the workflow"}).insert()
		self.assertEqual(doc.get(STATE), self.s_open, "the workflow is enforced")

	def test_the_undo_of_a_confirmed_workflow_stays_on_its_row(self):
		"""Plan rev 4 item 4: the fill is snapshotted on the confirmation row so it
		can be undone, also after the confirm succeeded."""
		old = self.desk_workflow(name=f"Jwf Old {self.tag}", states=[], transitions=[], is_active=0)
		frappe.db.set_value(WF, old, "is_active", 1)
		names = self.records(3)
		frappe.db.set_value(self.dt, names[2], STATE, self.s_no, update_modified=False)
		frappe.db.commit()
		name = self.park_wf()
		self.assertTrue(self.confirm(name)["ok"])
		undo = _seal.unseal_undo(self.row(name))
		self.assertEqual(sorted(undo["fill"]["names"]), sorted(names[:2]))
		self.assertEqual(undo["was_active"], [old])
		self.reconcile_own(name)
		self.assertTrue(self.row(name).sealed_undo, "the reconciler leaves a confirmed row's undo alone")
		self.assertEqual(self.active(), [f"Jwf Flow {self.tag}"])
		note = frappe._dict(gs.undo_confirmation(name))
		self.assertTrue(note.complete, note.message)
		self.assertEqual(self.active(), [old], "the workflow it replaced is active again")
		self.assertFalse(frappe.db.exists(WF, f"Jwf Flow {self.tag}"))
		states = self.states_of()
		self.assertEqual(
			[states[n] for n in names], [None, None, self.s_no], "only what it filled is emptied"
		)
		self.assertFalse(self.row(name).sealed_undo)

	def test_a_failed_alter_leaves_no_workflow_and_the_old_one_active(self):
		"""Plan rev 4 item 10: a Workflow replace whose state-field ALTER fails ends
		with the old workflow active again. Frappe commits the workflow, the
		deactivation of the old one and the field row BEFORE the ALTER."""
		old = self.desk_workflow(name=f"Jwf Old {self.tag}", dt=self.wide, workflow_state_field="title_1")
		self.assertEqual(self.active(self.wide), [old])
		with patch.object(gs, "ROW_SIZE_LIMIT", 10**9):
			name = self.park_wf(dt=self.wide)
			self.assertIn(f"It replaces the active workflow {old}.", self.card(name)["risk_line"])
			out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual(
			(out["pa_status"], out["reason_code"], out["outcome"]), ("Failed", "failed", "failed")
		)
		self.assertEqual(out["error"]["kind"], "not_fixable")
		message = out["error"]["message"]
		self.assertIn("row size limit", message)
		self.assertIn(f"The workflow Jwf Flow {self.tag} was removed.", message)
		self.assertIn(f"The workflow {old} is active again.", message)
		self.assertIn(f"The half-made field {STATE} was removed", message)
		self.assert_no_workflow(dt=self.wide)
		self.assertEqual(self.active(self.wide), [old])
		self.assertFalse(frappe.get_meta(self.wide).has_field(STATE), "cache cleared")
		with as_user(SM_USER):
			frappe.get_list(self.wide, fields=["name", "title"])  # the form still lists
			frappe.get_doc({"doctype": self.wide, "title": "still saves"}).insert()
		frappe.db.rollback()
		self.assertEqual(self.audit(), ["failed"])
		self.assertFalse(self.row(name).sealed_undo, "cleaned up: nothing kept")
		self.assertIn("nothing was changed", self.last_continuation())

	def test_a_save_that_stores_something_else_than_the_card_is_not_kept(self):
		"""Owner decision R2-17: the record is read back and held to the card. Here a
		hook turns the email alert on and opens the transition to everyone signed in;
		the card said neither. The column had been added by then, so the clean-up
		runs as after any failed confirm."""
		names = self.records(2)
		name = self.park_wf()
		self.assertNotIn("It emails", self.card(name)["risk_line"])
		self.wf_hook("wf_other_hook", event="validate")
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertIn("did not match the confirmation card", out["error"]["message"])
		self.assertNotIn("was not kept", out["error"]["message"], "the clean-up says what stays")
		self.assertIn("was removed", out["error"]["message"])
		frappe.db.rollback()
		self.assertFalse(
			frappe.db.exists(WF, f"Jwf Flow {self.tag}"), "what the card did not show is not kept"
		)
		self.assertEqual(self.active(), [])
		self.assertEqual(self.states_of(), dict.fromkeys(names, None), "no state was left filled in")
		logged = self.logged("post_write_mismatch", name)
		self.assertEqual(len(logged), 1, logged)
		self.assertIn("differs from the card in:", logged[0])
		self.assertEqual(self.row(name).status, "Failed")

	def test_the_card_shows_markup_as_the_save_stores_it(self):
		"""Frappe's XSS pass rewrites markup in a state's message on save. The card
		shows the stored form, so the post-write check finds what the card showed;
		a new row's document status sent as null is shown as the default it takes."""
		states = [
			{
				"state": self.s_open,
				"allow_edit": "System Manager",
				"message": "<p>Please approve & sign</p>",
				"doc_status": None,
			},
			{"state": self.s_ok, "allow_edit": "System Manager", "message": "Line 1<br/>Line 2"},
		]
		name = self.park_wf(states=states)
		sealed = self.call(name)["values"]["states"]
		self.assertEqual(
			[s["message"] for s in sealed], ["<p>Please approve &amp; sign</p>", "Line 1<br>Line 2"]
		)
		self.assertEqual(sealed[0]["doc_status"], "0")
		self.assertTrue(self.confirm(name)["ok"])
		doc = frappe.get_doc(WF, f"Jwf Flow {self.tag}")
		self.assertEqual([s.message for s in doc.states], [s["message"] for s in sealed])
		self.assertEqual(self.logged("post_write_mismatch"), [])

	def test_markup_the_card_cannot_show_as_stored_is_refused(self):
		"""The save sanitises the stored form again, so text that comes out
		different on every pass, and an update value the HTML filter would rewrite
		(it reads ``<doc.b and doc.c>`` as a tag), are refused at the card."""
		first = {"state": self.s_open, "allow_edit": "System Manager"}
		second = {"state": self.s_ok, "allow_edit": "System Manager", "update_field": "title"}
		r = self.create_wf(states=[first, {**second, "update_value": "doc.idx<doc.docstatus and doc.idx>1"}])
		self.assert_refused(r, "InvalidArgumentError", ["HTML filter would rewrite", "Put spaces around"])
		from frappe.utils import html_utils

		with patch.object(html_utils, "sanitize_html", side_effect=lambda text, **kw: text + "!"):
			r = self.create_wf(states=[{**first, "message": "<p>never settles</p>"}, second])
		self.assert_refused(r, "InvalidArgumentError", "rewrites differently each time it is saved")
		# Spaced, the same comparison is an ordinary value.
		name = self.park_wf(states=[first, {**second, "update_value": "a < b and c > 1"}])
		self.assertEqual(self.call(name)["values"]["states"][1]["update_value"], "a < b and c > 1")

	def test_the_post_write_check_reads_only_what_the_call_named(self):
		from jarvis.chat.pending_actions import _verify

		name = self.park_wf()
		args = self.call(name)  # the sealed call is gone once the card has run
		self.assertTrue(self.confirm(name)["ok"], "an ordinary confirm is like its card")
		wf = f"Jwf Flow {self.tag}"
		undo = {"written": {"name": wf}}
		self.assertIsNone(_verify.unlike_the_card(args, undo))
		values = copy.deepcopy(args["values"])
		# What the call did not name is not the card's claim.
		self.assertIsNone(_verify.unlike_the_card({"doctype": WF, "values": {"is_active": 1}}, undo))
		self.assertIsNone(
			_verify.unlike_the_card({"doctype": WF, "values": {**values, "no_such_key": 1}}, undo)
		)
		other = copy.deepcopy(values)
		other["transitions"][0]["allowed"] = "All"
		said = _verify.unlike_the_card({"doctype": WF, "values": other}, undo)
		self.assertEqual(said, "Transitions row 1, Allowed")
		self.assertNotIn("All", said.replace("Allowed", ""), "which field, never a value")
		fewer = {**values, "states": values["states"][:1]}
		self.assertIn(
			"2 rows were saved, the card showed 1",
			_verify.unlike_the_card({"doctype": WF, "values": fewer}, undo),
		)
		self.assertEqual(
			_verify.unlike_the_card({"doctype": WF, "values": {**values, "send_email_alert": 1}}, undo),
			"Send Email Alert",
		)
		# Fails closed: a record that cannot be named or read back is unlike its card.
		self.assertIn("could not be found", _verify.unlike_the_card(args, {}))
		self.assertIn(
			"could not be read back", _verify.unlike_the_card(args, {"written": {"name": "Jwf Nope"}})
		)
		self.assertIn("could not be read", _verify.unlike_the_card({"doctype": WF}, undo))

	def test_a_hook_that_fails_after_the_column_was_added(self):
		"""The workflow and its column both committed before the hook raised. The
		failure is reported, so the workflow does not stay live; the complete field
		is left in place and the outcome says partly applied."""
		names = self.records(2)
		self.wf_hook("wf_validation_hook")
		name = self.park_wf()
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual((out["reason_code"], out["outcome"]), ("partial", "partial"))
		self.assertIn("was removed", out["error"]["message"])
		self.assertIn("left in place", out["error"]["message"])
		frappe.db.rollback()
		self.assertFalse(frappe.db.exists(WF, f"Jwf Flow {self.tag}"), "never left live")
		self.assertEqual(self.active(), [])
		self.assertIn(STATE, _columns(self.dt), "a complete field is not half-made")
		self.assertEqual(self.states_of(), dict.fromkeys(names, None), "no state was left filled in")
		self.assertEqual(self.audit(), ["partial"])

	def test_a_crash_after_the_commit(self):
		self.wf_hook("wf_crash_hook")
		out = self.confirm(self.park_wf())
		self.assertEqual((out["error"]["code"], out["outcome"]), ("InternalError", "partial"))
		self.assertEqual(self.active(), [])
		self.assertTrue(self.logged("dispatch_crashed"))

	def test_a_failure_with_nothing_committed_is_a_plain_failure(self):
		"""The state field exists, so Frappe commits nothing mid-save: a failure is
		one rolled-back transaction."""
		old = self.desk_workflow(name=f"Jwf Old {self.tag}")
		names = self.records(2)
		self.wf_hook("wf_validation_hook")
		name = self.park_wf()
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual((out["reason_code"], out["outcome"]), ("failed", "failed"))
		self.assertEqual(self.active(), [old], "the deactivation rolled back with the rest")
		self.assertEqual(self.states_of(), dict.fromkeys(names, None))
		self.assertFalse(frappe.db.exists(WF, f"Jwf Flow {self.tag}"))

	def test_clean_up_removes_only_what_this_confirmation_made(self):
		"""Lesson 5: by the stamp on the confirmation row, never by owner or time."""
		mine = f"Jwf Flow {self.tag}"
		theirs = self.desk_workflow(name=mine, is_active=0)
		stamp = frappe.db.get_value(WF, theirs, "creation")
		other = {"name": mine, "modified": str(frappe.utils.add_to_date(stamp, seconds=5))}
		note = wg.NewWorkflow.clean_up(
			{"risk": "workflow_new", "name": mine, "dt": self.dt, "written": other}
		)
		self.assertIn("did not make it", note.text)
		self.assertTrue(frappe.db.exists(WF, mine))
		note = wg.NewWorkflow.clean_up({"risk": "workflow_new", "name": mine, "dt": self.dt})
		self.assertEqual(note.text, "Nothing was changed.", "no stamp: nothing of it committed")
		self.assertTrue(frappe.db.exists(WF, mine))
		note = wg.NewWorkflow.clean_up(
			{
				"risk": "workflow_new",
				"name": mine,
				"dt": self.dt,
				"written": {"name": mine, "modified": str(stamp)},
			}
		)
		self.assertIn("was removed", note.text)
		self.assertFalse(frappe.db.exists(WF, mine))

	def test_the_old_workflow_is_not_reactivated_over_a_newer_one(self):
		old = self.desk_workflow(name=f"Jwf Old {self.tag}", is_active=0)
		newer = self.desk_workflow(name=f"Jwf Newer {self.tag}")
		mine = self.desk_workflow(name=f"Jwf Flow {self.tag}", is_active=0)
		stamp = str(frappe.db.get_value(WF, mine, "creation"))
		note = wg.NewWorkflow.clean_up(
			{
				"risk": "workflow_new",
				"name": mine,
				"dt": self.dt,
				"was_active": [old],
				"written": {"name": mine, "modified": stamp},
			}
		)
		self.assertNotIn("active again", note.text)
		self.assertEqual(self.active(), [newer], "someone activated another one since: theirs stands")

	def _die_mid_confirm(self, name: str) -> None:
		def _killed(query, debug=False):
			frappe.db.commit()  # Frappe's sql_ddl commits, then the ALTER never runs
			raise SystemExit("worker killed")

		with patch.object(frappe.db, "sql_ddl", side_effect=_killed), self.assertRaises(SystemExit):
			self.confirm(name)
		frappe.db.rollback()
		self.assertEqual(self.row(name).status, "Executing")

	def test_the_reconciler_cleans_up_an_interrupted_confirm(self):
		old = self.desk_workflow(name=f"Jwf Old {self.tag}", workflow_state_field="title")
		name = self.park_wf()
		self._die_mid_confirm(name)
		self.assertEqual(
			self.active(), [f"Jwf Flow {self.tag}"], "live, with a state field that has no column"
		)
		self.assertNotIn(STATE, _columns(self.dt))
		self._age(name)
		with gs.structure_lock(self.dt):
			self.reconcile_own(name)
		self.assertEqual(self.row(name).status, "Executing", "left for the next run: the form is locked")
		self.reconcile_own(name)
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "interrupted"))
		self.assert_no_workflow()
		self.assertEqual(self.active(), [old])
		self.assertTrue(self.logged("structure_cleanup", "was removed"))
		self.assertEqual(self.audit(), ["partial"])

	def _age(self, name: str) -> None:
		frappe.db.sql(
			f"UPDATE `tab{PA}` SET executing_at=%(t)s WHERE name=%(n)s",
			{"t": frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-700), "n": name},
		)
		frappe.db.commit()

	def _saved_but_never_recorded(self) -> tuple[str, list[str]]:
		"""The confirm saved everything and its worker died before it could say so:
		the workflow, its field and the filled states are there, the row is still
		Executing, long enough ago for the reconciler to take it."""
		names = self.records(3)
		name = self.park_wf()
		with (
			patch("jarvis.chat.pending_actions._execute._terminal_update", side_effect=SystemExit("killed")),
			self.assertRaises(SystemExit),
		):
			self.confirm(name)
		frappe.db.rollback()
		self.assertEqual(self.active(), [f"Jwf Flow {self.tag}"])
		# The fill the dying worker had not committed yet, as a worker that died one
		# statement later leaves it.
		frappe.db.sql(f"UPDATE `tab{self.dt}` SET `{STATE}`=%(s)s", {"s": self.s_open})
		frappe.db.sql(
			f"UPDATE `tab{PA}` SET executing_at=%(t)s WHERE name=%(n)s AND status='Executing'",
			{"t": frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-700), "n": name},
		)
		frappe.db.commit()
		return name, names

	def _executing_since(self, name: str, seconds: int) -> None:
		frappe.db.sql(
			f"UPDATE `tab{PA}` SET executing_at=%(t)s WHERE name=%(n)s AND status='Executing'",
			{"t": frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-seconds), "n": name},
		)
		frappe.db.commit()

	def test_a_dead_confirm_is_cleaned_up_as_soon_as_its_lock_is_free(self):
		"""Review: recovery waited out ten minutes with a field row that has no
		column. The form's lock is held for the whole confirm, so a row still
		Executing whose lock can be taken has no worker."""
		from jarvis.chat.pending_actions import _reconcile

		self.assertLess(_reconcile.STRUCTURE_DEAD_AFTER_S, _reconcile.INTERRUPT_AFTER_S)
		name, _names = self._saved_but_never_recorded()
		self._executing_since(name, _reconcile.STRUCTURE_DEAD_AFTER_S - 30)
		self.reconcile_own(name)
		self.assertEqual(self.row(name).status, "Executing", "too fresh to judge")
		self._executing_since(name, _reconcile.STRUCTURE_DEAD_AFTER_S + 30)
		with gs.structure_lock(self.dt):  # its confirm is still running
			self.reconcile_own(name)
		self.assertEqual(self.row(name).status, "Executing", "never beside a running change")
		self.assertEqual(self.active(), [f"Jwf Flow {self.tag}"])
		self.reconcile_own(name)
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "interrupted"))
		self.assertFalse(frappe.db.exists(WF, f"Jwf Flow {self.tag}"))

	def test_a_clean_up_given_up_on_names_what_it_was(self):
		from jarvis.chat.pending_actions import _reconcile

		name, _names = self._saved_but_never_recorded()
		_reconcile._give_up_structure(name)
		logged = self.logged("structure_needs_a_person", name)
		self.assertEqual(len(logged), 1)
		self.assertIn(self.dt, logged[0])
		self.assertIn(f"Jwf Flow {self.tag}", logged[0])

	def test_a_failed_confirm_left_for_a_person_is_logged(self):
		"""Review: only the reconciler logged it; a clean-up inside the confirm that
		had to leave the workflow in place said so on the card and nowhere else."""
		self.wf_hook("wf_validation_hook")
		name = self.park_wf()
		left = gs.CleanUp("left in place, it needs a person", clean=False, needs_person=True)
		with patch.object(gs, "clean_up", return_value=left):
			out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		logged = self.logged("structure_needs_a_person", name)
		self.assertEqual(len(logged), 1, logged)
		self.assertIn("left in place", logged[0])

	def test_the_off_switch_is_off_when_either_file_says_so(self):
		"""Review: a site-level 0 switched back on what the bench had set off."""

		def files(common, site):
			return lambda path, missing_ok=False: common if "common_site_config" in path else site

		was = frappe.conf.pop(gs.OFF_SWITCH, None)
		if was is not None:
			self.addCleanup(frappe.conf.__setitem__, gs.OFF_SWITCH, was)
		for common, site, off in (
			({gs.OFF_SWITCH: 1}, {gs.OFF_SWITCH: 0}, True),
			({gs.OFF_SWITCH: 0}, {gs.OFF_SWITCH: 1}, True),
			({}, {gs.OFF_SWITCH: 0}, False),
			({}, {}, False),
		):
			with patch.object(gs, "_read_json", side_effect=files(common, site)):
				self.assertEqual(gs.disabled(), off, (common, site))

	def test_an_interrupted_workflow_nobody_used_is_reverted(self):
		name, names = self._saved_but_never_recorded()
		self.reconcile_own(name)
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "interrupted"))
		self.assertFalse(frappe.db.exists(WF, f"Jwf Flow {self.tag}"))
		self.assertEqual(self.states_of(), dict.fromkeys(names, None), "the states it filled are emptied")

	def test_an_interrupted_workflow_people_already_used_is_left_for_a_person(self):
		"""Review: reverting it would leave records in a state no workflow owns."""
		name, names = self._saved_but_never_recorded()
		frappe.db.set_value(self.dt, names[0], STATE, self.s_ok, update_modified=False)
		frappe.db.commit()
		self.reconcile_own(name)
		self.reconcile_own(name)
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "partial"))
		wf = f"Jwf Flow {self.tag}"
		self.assertEqual(self.active(), [wf], "left active")
		self.assertEqual(self.states_of()[names[0]], self.s_ok)
		self.assertEqual(self.states_of()[names[1]], self.s_open, "nothing was emptied")
		logged = self.logged("structure_needs_a_person", name)
		self.assertEqual(len(logged), 1, "logged once")
		self.assertIn(wf, logged[0])
		self.assertIn("needs a person", logged[0])
		self.assertEqual(self.audit(), ["partial"])

	def test_the_operators_undo(self):
		"""M1. ``undo_confirmation`` is an operator's recovery aid, not a feature: it
		is not whitelisted, needs a System Manager when a request calls it, refuses
		inside a tool call, is idempotent, and files what it did."""
		self.assertNotIn(gs.undo_confirmation, frappe.whitelisted)
		name = self.park_wf()
		self.assertTrue(self.confirm(name)["ok"])
		frappe.local.request = frappe._dict(method="POST")
		self.addCleanup(
			lambda: frappe.local.__delattr__("request") if hasattr(frappe.local, "request") else None
		)
		with as_user(NOT_SM), self.assertRaises(frappe.PermissionError):
			gs.undo_confirmation(name)
		self.assertTrue(frappe.db.exists(WF, f"Jwf Flow {self.tag}"))
		del frappe.local.request
		frappe.local.jarvis_dispatch_depth = 1
		try:
			with self.assertRaises(Exception):
				gs.undo_confirmation(name)
		finally:
			frappe.local.jarvis_dispatch_depth = 0
		self.assertTrue(frappe.db.exists(WF, f"Jwf Flow {self.tag}"))
		# Only a confirmation that ran: a row in any other state carries a clean-up
		# for the reconciler, not an undo.
		self.set_col(name, status="Failed")
		self.assertIn("Nothing is kept", gs.undo_confirmation(name)["message"])
		self.set_col(name, status="Executed")
		self.assertTrue(frappe.db.exists(WF, f"Jwf Flow {self.tag}"))
		before = frappe.db.count(AGENT_WRITE, {"tool": "undo_confirmation"})
		with as_user(SM_USER):
			note = frappe._dict(gs.undo_confirmation(name))
		self.assertIn("was removed", note.message)
		# What ``bench execute`` prints: plain values, nothing it cannot write out.
		self.assertEqual(sorted(note), ["complete", "message", "needs_person", "restored"])
		self.assertEqual(frappe.parse_json(frappe.as_json(dict(note))), dict(note))
		import json

		json.dumps(dict(note))
		self.assertFalse(frappe.db.exists(WF, f"Jwf Flow {self.tag}"))
		self.assertEqual(frappe.db.count(AGENT_WRITE, {"tool": "undo_confirmation"}), before + 1, "filed")
		with as_user(SM_USER):
			again = frappe._dict(gs.undo_confirmation(name))
		self.assertIn("Nothing is kept", again.message, "idempotent")
		self.assertIn("Nothing is kept", gs.undo_confirmation("no-such-row")["message"])

	def test_the_undo_refuses_a_workflow_changed_or_used_since(self):
		names = self.records(2)
		name = self.park_wf()
		self.assertTrue(self.confirm(name)["ok"])
		frappe.db.set_value(self.dt, names[0], STATE, self.s_ok, update_modified=False)
		frappe.db.commit()
		note = frappe._dict(gs.undo_confirmation(name))
		self.assertTrue(note.needs_person, note.message)
		self.assertEqual(self.active(), [f"Jwf Flow {self.tag}"])
		self.assertTrue(self.row(name).sealed_undo, "kept: nothing was undone")

	def test_a_record_made_and_approved_after_the_confirm_counts_as_use(self):
		"""Review: with a state field that already existed, only the records the save
		filled were looked at. A record created under the new workflow and moved on
		through it is use just the same."""
		from frappe.model.workflow import apply_workflow

		self.desk_workflow(name=f"Jwf Old {self.tag}", is_active=0, states=[], transitions=[])
		self.records(2)
		name = self.park_wf()
		self.assertTrue(self.confirm(name)["ok"])
		# Made by one person, approved by another: chat's transitions do not let a
		# person approve their own record.
		doc = frappe.get_doc({"doctype": self.dt, "title": "made afterwards"}).insert()
		self.assertEqual(doc.get(STATE), self.s_open)
		with as_user(SM_USER):
			apply_workflow(frappe.get_doc(self.dt, doc.name), self.a_ok)
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(self.dt, doc.name, STATE), self.s_ok)
		note = frappe._dict(gs.undo_confirmation(name))
		self.assertTrue(note.needs_person, note.message)
		self.assertFalse(note.restored)
		self.assertEqual(self.active(), [f"Jwf Flow {self.tag}"], "left for a person")

	def test_a_record_created_after_the_confirm_counts_as_use(self):
		"""Review: a record made under the new workflow holds one of ITS states. Put
		the workflow it replaced back and that record can no longer be saved."""
		old = self.desk_workflow(
			name=f"Jwf Old {self.tag}",
			states=[{"state": self.s_no, "allow_edit": "System Manager"}],
			transitions=[],
		)
		name = self.park_wf()
		self.assertTrue(self.confirm(name)["ok"])
		self.assertEqual(self.active(), [f"Jwf Flow {self.tag}"], f"it replaced {old}")
		with as_user(SM_USER):
			doc = frappe.get_doc({"doctype": self.dt, "title": "made afterwards"}).insert()
		frappe.db.commit()
		note = frappe._dict(gs.undo_confirmation(name))
		self.assertTrue(note.needs_person, note.message)
		self.assertFalse(note.restored)
		self.assertEqual(self.active(), [f"Jwf Flow {self.tag}"], "left for a person")
		self.assertEqual(frappe.db.get_value(self.dt, doc.name, STATE), self.s_open)

	def test_the_kept_undo_is_purged_with_its_row(self):
		"""M1 (b). The snapshot lives on the confirmation row and nowhere else, and
		the daily purge takes the whole row at its seven-day horizon."""
		name = self.park_wf()
		self.assertTrue(self.confirm(name)["ok"])
		blob = self.row(name).sealed_undo
		self.assertTrue(blob)
		others = frappe.db.sql(
			"SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = DATABASE()"
			" AND column_name = 'sealed_undo'"
		)
		self.assertEqual([tuple(r) for r in others], [(f"tab{PA}", "sealed_undo")], "one column holds it")
		from jarvis.chat.pending_actions import _reconcile

		self.assertEqual(_reconcile.purge(only=name), 0, "not before its horizon")
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-(_reconcile.PURGE_AFTER_DAYS + 1))
		frappe.db.sql(
			f"UPDATE `tab{PA}` SET settled=1, settled_at=%(t)s WHERE name=%(n)s", {"t": old, "n": name}
		)
		frappe.db.commit()
		self.assertEqual(_reconcile.purge(only=name), 1)
		self.assertFalse(frappe.db.exists(PA, name), "the row and its undo are gone")

	def test_clean_up_runs_as_the_person_not_as_administrator(self):
		"""Plan rev 4 item 10: the clean-up works for a System Manager who is not
		Administrator."""
		seen = []
		real = wg.NewWorkflow.clean_up

		def _spy(undo):
			seen.append(frappe.session.user)
			return real(undo)

		self.wf_hook("wf_validation_hook")
		with patch.object(wg.NewWorkflow, "clean_up", staticmethod(_spy)):
			out = self.confirm(self.park_wf())
		self.assertFalse(out["ok"])
		self.assertEqual(seen, [SM_USER])
		self.assertNotIn("Administrator", seen)

	def test_a_held_lock_says_try_again_and_keeps_the_card(self):
		name = self.park_wf()
		with gs.structure_lock(self.dt):
			out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual((out["error"]["kind"], out["pa_status"]), ("retry_later", "Pending"))
		self.assert_no_workflow()
		self.assertTrue(self.confirm(name)["ok"], "the same card runs once the form is free")

	def test_the_checks_run_again_under_the_lock(self):
		name = self.park_wf()
		frappe.db.sql_ddl(f"ALTER TABLE `tab{self.dt}` ADD COLUMN `{STATE}` varchar(140)")
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertIn(f"already has a column named {STATE}", out["error"]["message"])
		self.assertEqual(
			out["error"]["kind"], "not_fixable", "a structure write is never offered a correction"
		)
		self.assertFalse(frappe.db.exists(WF, f"Jwf Flow {self.tag}"))

	def test_what_changed_since_the_park_is_refused_at_confirm(self):
		"""Review: "checks repeated at Confirm" was pinned for two kinds of change. A
		role deleted since, a fill grown past the cap, and a confirmer who lost the
		permission are each refused there too, with nothing made."""
		role = f"Jwf Role {self.tag}"
		frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(lambda: (frappe.db.delete("Role", {"name": role}), frappe.db.commit()))
		states = [{"state": self.s_open, "allow_edit": role}, {"state": self.s_ok, "allow_edit": role}]
		name = self.park_wf(states=states)
		frappe.db.delete("Role", {"name": role})
		frappe.db.commit()
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertIn("no Role named", out["error"]["message"])
		self.assert_no_workflow()
		self.assertEqual(self.logged("snapshot_failed"), [], "a refusal is not logged as a fault")

		self.conv = self.make_conv(SM_USER)
		name = self.park_wf()
		self.records(3)
		with patch.object(wg, "FILL_NAMES_MAX", 2):
			out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertIn("More than 2 records", out["error"]["message"])
		self.assert_no_workflow()

		self.conv = self.make_conv(SM_USER)
		name = self.park_wf()
		real = frappe.has_permission

		def lost(doctype=None, *args, **kwargs):
			return False if doctype == WF else real(doctype, *args, **kwargs)

		with patch.object(frappe, "has_permission", side_effect=lost):
			out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assert_no_workflow()

	def test_a_write_with_no_chat_behind_it_never_parks(self):
		"""An agent run has no conversation bound to its session: there is no card to
		show anyone, so the workflow write is refused before anything parks."""
		before = frappe.db.count(PA)
		with as_user(SM_USER):
			r = api._run_tool("create_doc", {"doctype": WF, "values": self.wf_values()}, conversation=None)
		self.assertFalse(r.get("ok"), r)
		self.assertEqual(frappe.db.count(PA), before, "nothing parked")
		self.assert_no_workflow()

	def test_a_snapshot_that_cannot_be_taken_stops_the_write(self):
		"""Review: a failed snapshot was swallowed, the confirm went on with nothing
		to undo by, and a failure after the commit then read "Nothing was changed"."""
		names = self.records(2)
		name = self.park_wf()
		with patch.object(wg, "_fill_snapshot", side_effect=RuntimeError("boom")):
			r = self.confirm(name)
		self.assertFalse(r["ok"], r)
		self.assertIn("could not record what this change replaces", frappe.as_json(r))
		self.assertEqual(r["error"]["kind"], "retry_later")
		self.assertEqual(self.row(name).status, "Pending", "not claimed: the same card confirms again")
		self.assert_no_workflow()
		self.assertEqual(self.states_of(), {}, "no state field, nothing filled")
		self.assertEqual(len(names), frappe.db.count(self.dt))
		self.assertTrue(self.logged("snapshot_failed", "Workflow"))
		self.assertTrue(self.confirm(name)["ok"], "and it confirms once the snapshot can be read")

	def test_a_written_change_with_no_whole_snapshot_is_never_called_clean(self):
		note = gs.clean_up({"risk": wg.RISK_NEW, "name": "", gs.INCOMPLETE: True, "written": {"name": "x"}})
		self.assertFalse(note.clean)
		self.assertTrue(note.needs_person)
		self.assertNotIn("Nothing was changed", note.text)

	def test_a_card_check_that_cannot_be_made_does_not_run_the_write(self):
		name = self.park_wf()
		with patch.object(wg.NewWorkflow, "card_holds", side_effect=RuntimeError("boom")):
			r = self.confirm(name)
		self.assertFalse(r["ok"], r)
		self.assert_no_workflow()
		self.assertEqual(self.row(name).reason_code, "stale")

	def test_a_card_that_no_longer_says_what_it_does_is_stale(self):
		"""Card == write, also over time: the card said nothing of replacing a
		workflow, then one was activated in Desk. Confirming would switch it off
		unseen, so the card does not run."""
		name = self.park_wf(workflow_state_field="title")
		self.assertNotIn("It replaces", self.card(name)["risk_line"])
		other = self.desk_workflow(name=f"Jwf Other {self.tag}", workflow_state_field="title")
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual((out["pa_status"], out["reason_code"]), ("Failed", "stale"))
		self.assertEqual(self.active(), [other])
		self.assertFalse(frappe.db.exists(WF, f"Jwf Flow {self.tag}"))
		# And the other way round: it named the workflow it replaces; that one is gone.
		self.conv = self.make_conv(SM_USER)
		name = self.park_wf(workflow_state_field="title")
		self.assertIn(f"It replaces the active workflow {other}.", self.card(name)["risk_line"])
		frappe.db.set_value(WF, other, "is_active", 0, update_modified=False)
		frappe.db.commit()
		self.assertEqual(self.confirm(name)["reason_code"], "stale")

	def test_text_the_request_wrote_cannot_stand_in_for_the_replaces_line(self):
		"""Review: the card's line carries values the request wrote (an update
		value); one that reads like the replaces sentence must not satisfy the check."""
		other = f"Jwf Other {self.tag}"
		states = [
			{"state": self.s_open, "allow_edit": "System Manager"},
			{
				"state": self.s_ok,
				"allow_edit": "System Manager",
				"update_field": "title",
				"update_value": f"It replaces the active workflow {other}.",
			},
		]
		name = self.park_wf(workflow_state_field="title", states=states)
		self.desk_workflow(name=other, workflow_state_field="title")
		out = self.confirm(name)
		self.assertEqual((out["ok"], out["reason_code"]), (False, "stale"), out)
		self.assertEqual(self.active(), [other])

	def test_a_row_that_was_not_parked_as_a_guarded_card_never_runs(self):
		"""The confirm takes the lock only for a card parked as a guarded one
		(``card.structural``, bound into the seal): a row made any other way is
		refused as every structure write is."""
		args = {"doctype": WF, "values": self.wf_values()}
		name = self.park(tool="create_doc", args=args, owner=SM_USER, conversation=self.conv)
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual(out["error"]["code"], "structure_refused")
		self.assert_no_workflow()

	def test_a_workflow_someone_edited_since_is_not_removed(self):
		mine = self.desk_workflow(is_active=0)
		stamp = str(frappe.db.get_value(WF, mine, "creation"))
		with as_user(SM_USER):
			doc = frappe.get_doc(WF, mine)
			doc.send_email_alert = 1
			doc.save()
		frappe.db.commit()
		note = wg.NewWorkflow.clean_up(
			{
				"risk": "workflow_new",
				"name": mine,
				"dt": self.dt,
				"written": {"name": mine, "modified": stamp},
			}
		)
		self.assertIn("changed again in the meantime", note.text)
		self.assertFalse(note.clean)
		self.assertTrue(frappe.db.exists(WF, mine))

	def test_a_card_admits_its_workflow_and_nothing_else(self):
		with as_user(SM_USER):
			with wr.guard_scope(wr.allow_entries("create_doc", {"doctype": WF, "values": self.wf_values()})):
				with self.assertRaises(wr.StructureRefusedError):
					frappe.get_doc(
						{"doctype": "Custom Field", "dt": self.dt, "fieldname": "jcf_x", "fieldtype": "Data"}
					).insert()
				with self.assertRaises(wr.StructureRefusedError):
					frappe.get_doc(DOMAIN).save()
			with wr.guard_scope([]):
				with self.assertRaises(wr.StructureRefusedError):
					frappe.get_doc({"doctype": WF, **self.wf_values()}).insert()
		frappe.db.rollback()
		self.assert_no_workflow()


# --------------------------------------------------------------------------- #
# Workflow: update
# --------------------------------------------------------------------------- #
class TestWorkflowEdit(_WfBase):
	def setUp(self):
		super().setUp()
		self.wf = self.desk_workflow(is_active=0)

	def test_activating_shows_the_whole_workflow_and_says_what_it_replaces(self):
		old = self.desk_workflow(name=f"Jwf Old {self.tag}")
		self.records(2)
		name = self.park_update(self.wf, is_active="true")
		card = self.card(name)
		self.assertEqual((card["kind"], card["name"], card["structural"]), ("update", self.wf, True))
		diff = {d["label"]: d for d in card["diff"]}
		self.assertEqual((diff["Is Active"]["from"], diff["Is Active"]["to"]), ("No", "Yes"))
		for label, rows in (("Document States", 2), ("Transitions", 1)):
			self.assertEqual(
				diff[label]["to_table"]["count"], rows, "every row of the workflow being activated"
			)
			self.assertEqual(diff[label]["from_table"]["count"], rows)
		self.assertIn("Allow Self Approval", diff["Transitions"]["to_table"]["columns"])
		line = card["risk_line"]
		self.assertTrue(line.startswith(f"Confirming changes the workflow rules of {self.dt}"), line)
		self.assertIn(f"It replaces the active workflow {old}.", line)
		self.assertIn("It fills the state on 2 records", line)
		self.assertEqual(self.active(), [old])
		out = self.confirm(name)
		self.assertTrue(out["ok"], out)
		self.assertEqual(self.active(), [self.wf])
		self.assertEqual(self.audit("update_doc"), ["applied"])

	def test_rows_are_kept_changed_added_and_removed_by_name(self):
		doc = frappe.get_doc(WF, self.wf)
		kept, dropped = doc.states[0].name, doc.states[1].name
		name = self.park_update(
			self.wf,
			states=[
				{"name": kept, "allow_edit": "Administrator"},
				{"state": self.s_no, "allow_edit": "System Manager"},
			],
			transitions=[],
		)
		sealed = self.call(name)["changes"]
		self.assertEqual(sealed["states"][0]["name"], kept)
		self.assertEqual(sealed["states"][0]["state"], self.s_open, "a kept row is carried whole")
		self.assertNotIn("name", sealed["states"][1])
		self.assertTrue(self.confirm(name)["ok"])
		doc = frappe.get_doc(WF, self.wf)
		self.assertEqual(
			[(s.name == kept, s.state, s.allow_edit) for s in doc.states],
			[(True, self.s_open, "Administrator"), (False, self.s_no, "System Manager")],
		)
		self.assertNotIn(dropped, [s.name for s in doc.states])
		self.assertEqual(doc.transitions, [])

	def test_edits_that_are_refused(self):
		doc = frappe.get_doc(WF, self.wf)
		for changes, code, says in (
			({}, "InvalidArgumentError", "changes is empty"),
			({"document_type": "ToDo"}, "structure_refused", "makes it another workflow"),
			({"workflow_name": "Jwf Renamed"}, "structure_refused", "makes it another workflow"),
			({"workflow_state_field": "jwf_state"}, "structure_refused", "Changing the state field"),
			(
				{"states": [{"state": self.s_open, "allow_edit": "System Manager"}]},
				"InvalidArgumentError",
				"without saying which is which",
			),
			(
				{"states": [{"name": "nope", "allow_edit": "System Manager"}]},
				"InvalidArgumentError",
				"no row named nope",
			),
			({"states": []}, "InvalidArgumentError", "not a valid State"),
			(
				{"transitions": [{"name": doc.transitions[0].name, "condition": "1 +"}]},
				"InvalidArgumentError",
				"is not set from chat",
			),
		):
			with self.subTest(changes=changes):
				r = self.update_wf(self.wf, **changes)
				self.assert_refused(r, code, says)
		r = self.update_wf("Jwf No Such Workflow", is_active=1)
		self.assert_refused(r, "InvalidArgumentError", "There is no Workflow named")
		r = self.update_wf(self.wf.upper(), is_active=1)
		self.assert_refused(r, "InvalidArgumentError", f"is named {self.wf!r}")
		self.assertEqual(frappe.db.get_value(WF, self.wf, "document_type"), self.dt)

	def test_a_transition_that_runs_tasks_keeps_them_only_as_it_is(self):
		if "transition_tasks" not in wg._row_keys("transitions"):
			self.skipTest("Frappe 15 has no transition tasks")
		tasks = f"Jwf Tasks {self.tag}"
		frappe.get_doc({"doctype": "Workflow Transition Tasks", "__newname": tasks}).insert()
		self.addCleanup(
			lambda: (frappe.db.delete("Workflow Transition Tasks", {"name": tasks}), frappe.db.commit())
		)
		row = frappe.get_doc(WF, self.wf).transitions[0].name
		frappe.db.set_value("Workflow Transition", row, "transition_tasks", tasks, update_modified=False)
		frappe.db.commit()
		self.park_update(self.wf, send_email_alert=1)  # untouched, the row keeps its tasks
		self.conv = self.make_conv(SM_USER)
		r = self.update_wf(self.wf, transitions=[{"name": row, "allowed": "Administrator"}])
		self.assert_refused(r, "InvalidArgumentError", "runs tasks")

	def test_an_echoed_unchanged_name_and_form_are_not_a_change(self):
		name = self.park_update(self.wf, workflow_name=self.wf, document_type=self.dt, send_email_alert=1)
		self.assertNotIn("document_type", self.call(name)["changes"])
		self.assertNotIn("workflow_name", self.call(name)["changes"])

	def test_an_update_card_carries_the_code_line_and_the_same_rules(self):
		row = frappe.get_doc(WF, self.wf).transitions[0].name
		name = self.park_update(self.wf, transitions=[{"name": row, "condition": "doc.title == 'go'"}])
		self.assertNotIn(CODE_LINE, self.card(name)["risk_line"])
		# A richer condition someone set up in Desk is kept, named, and called code.
		frappe.db.set_value(
			"Workflow Transition", row, "condition", "frappe.session.user == 'x'", update_modified=False
		)
		frappe.db.commit()
		frappe.clear_document_cache(WF, self.wf)
		self.conv = self.make_conv(SM_USER)
		line = self.card(self.park_update(self.wf, send_email_alert=1))["risk_line"]
		self.assertIn(
			f"The condition on {self.a_ok} from {self.s_open}: set up in Desk and kept as it is. "
			"It is not checked here.",
			line,
		)
		self.assertIn(CODE_LINE, line)
		frappe.db.set_value("Workflow Transition", row, "condition", None, update_modified=False)
		frappe.db.commit()
		frappe.clear_document_cache(WF, self.wf)
		self.conv = self.make_conv(SM_USER)
		r = self.update_wf(self.wf, transitions=[{"name": row, "condition": "frappe.session.user == 'x'"}])
		self.assert_refused(r, "InvalidArgumentError", "may only compare this document's own fields")
		r = self.update_wf(self.wf, transitions=[{"name": row, "allowed": "Guest"}])
		self.assert_refused(r, "InvalidArgumentError", "Guest")

	def test_an_update_shows_a_changed_condition_as_a_diff_and_a_kept_one_plainly(self):
		with as_user(SM_USER):
			doc = frappe.get_doc(WF, self.wf)
			doc.transitions[0].condition = "doc.title == 'old'"
			doc.append(
				"transitions",
				{
					"state": self.s_ok,
					"action": self.a_no,
					"next_state": self.s_open,
					"allowed": "System Manager",
					"condition": "doc.idx > 3",
				},
			)
			doc.save()
		frappe.db.commit()
		changed, kept = (r.name for r in frappe.get_doc(WF, self.wf).transitions)
		name = self.park_update(
			self.wf, transitions=[{"name": changed, "condition": "doc.title == 'new'"}, {"name": kept}]
		)
		card = self.card(name)
		labels = [d["label"] for d in card["diff"]]
		self.assertLess(
			labels.index("Transitions"),
			labels.index(f"Condition on {self.a_ok}, from {self.s_open} to {self.s_ok}"),
			"after its table",
		)
		diff = {d["label"]: d for d in card["diff"]}
		row = diff[f"Condition on {self.a_ok}, from {self.s_open} to {self.s_ok}"]
		self.assertEqual((row["from"], row["to"]), ("doc.title == 'old'", "doc.title == 'new'"))
		self.assertTrue(row["multiline"])
		self.assertEqual([line["op"] for line in row["lines"]], ["-", "+"])
		same = diff[f"Condition on {self.a_no}, from {self.s_ok} to {self.s_open} (unchanged)"]
		self.assertEqual((same["from"], same["to"], same["multiline"]), ("doc.idx > 3", "doc.idx > 3", True))
		self.assertNotIn("lines", same)

	def test_a_condition_set_up_in_desk_is_kept_shown_and_flagged(self):
		"""An edit that leaves a richer condition as it is does not have to remove
		it; changing it is held to the chat rule."""
		row = frappe.get_doc(WF, self.wf).transitions[0].name
		desk = "frappe.session.user != doc.owner"
		frappe.db.set_value("Workflow Transition", row, "condition", desk, update_modified=False)
		frappe.db.commit()
		name = self.park_update(self.wf, send_email_alert=1)
		card = self.card(name)
		self.assertIn(CODE_LINE, card["risk_line"])
		diff = {d["label"]: d for d in card["diff"]}
		self.assertIn(desk, str(diff["Transitions"]["to_table"]["rows"]))
		kept = diff[f"Condition on {self.a_ok}, from {self.s_open} to {self.s_ok} (unchanged)"]
		self.assertEqual((kept["to"], kept["multiline"]), (desk, True), "whole, in a row of its own")
		self.conv = self.make_conv(SM_USER)
		r = self.update_wf(self.wf, transitions=[{"name": row, "condition": desk + " or True"}])
		self.assert_refused(r, "InvalidArgumentError", "may only compare this document's own fields")

	def test_a_rename_and_a_batch_are_refused_on_the_route(self):
		r = self.update_wf(self.wf, workflow_name="Jwf Renamed")
		self.assert_refused(r, "structure_refused", "makes it another workflow")
		self.assertEqual(r["error"]["kind"], "not_fixable", "a gate refusal is never offered a correction")
		docs = [
			{"doctype": WF, "values": self.wf_values(name="Jwf Batch")},
			{"doctype": "ToDo", "values": {"description": "x"}},
		]
		for args in ({"docs": docs}, {"docs": docs[:1]}):
			self.assert_refused(self.run_tool("create_doc", args), "structure_refused")
		updates = [{"name": self.wf, "changes": {"is_active": 1}}]
		self.assert_refused(
			self.run_tool("update_doc", {"doctype": WF, "updates": updates}), "structure_refused"
		)
		self.assertFalse(frappe.db.exists(WF, "Jwf Batch"))
		self.assertEqual(self.active(), [])

	def test_the_card_says_whether_it_becomes_stays_or_stops_being_active(self):
		line = self.card(self.park_update(self.wf, is_active=1))["risk_line"]
		self.assertIn(f"It becomes the active workflow of {self.dt}", line)
		frappe.db.delete(PA, {"conversation": self.conv})
		frappe.db.set_value(WF, self.wf, "is_active", 1)
		frappe.db.commit()
		line = self.card(self.park_update(self.wf, send_email_alert=1))["risk_line"]
		self.assertIn(f"It stays the active workflow of {self.dt}", line)
		self.assertNotIn("becomes the active workflow", line)
		frappe.db.delete(PA, {"conversation": self.conv})
		frappe.db.commit()
		line = self.card(self.park_update(self.wf, is_active=0))["risk_line"]
		self.assertIn(
			f"It stops being the active workflow of {self.dt}: its records are no longer held to it.", line
		)
		self.assertNotIn("stays the active", line)

	def test_counts_read_as_one_or_many(self):
		self.assertEqual([gs.counted(n, "record") for n in (0, 1, 2)], ["0 records", "1 record", "2 records"])
		self.assertEqual(gs.counted(1, "role assignment"), "1 role assignment")
		self.assertEqual(gs.counted(2, "person", "people"), "2 people")
		self.assertEqual(gs.counted(1, "person", "people"), "1 person")
		frappe.db.set_value(WF, self.wf, "is_active", 1)
		frappe.db.commit()
		names = self.records(3)
		frappe.db.set_value(self.dt, names[0], STATE, self.s_open, update_modified=False)
		frappe.db.set_value(self.dt, names[1], STATE, self.s_no, update_modified=False)
		frappe.db.commit()
		line = self.card(self.park_update(self.wf, send_email_alert=1))["risk_line"]
		self.assertIn(
			"It fills the state on 1 record that has none; 1 record keeps a state this workflow lacks, "
			"and it cannot be saved or moved until its state is changed to one of this workflow's.",
			line,
		)
		self.assertIn("Self-approval is allowed on 1 of 1 transitions", line)

	def test_a_failure_after_the_edit_committed_restores_the_workflow(self):
		"""The form lost its state field since the workflow was saved, so the edit
		adds it again: Frappe commits the edited workflow and then alters the table;
		the planted failure comes after that."""
		old = self.desk_workflow(name=f"Jwf Old {self.tag}")
		frappe.db.delete("Custom Field", {"dt": self.dt, "fieldname": STATE})
		frappe.db.commit()
		frappe.db.sql_ddl(f"ALTER TABLE `tab{self.dt}` DROP COLUMN `{STATE}`")
		frappe.clear_cache(doctype=self.dt)
		before = frappe.get_doc(WF, self.wf).as_dict()
		self.wf_hook("wf_validation_hook")
		doc = frappe.get_doc(WF, self.wf)
		name = self.park_update(
			self.wf,
			is_active=1,
			transitions=[{"name": doc.transitions[0].name, "allow_self_approval": 0}],
		)
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual(out["outcome"], "partial", "the complete field workflow_state stays")
		self.assertIn(f"The change to the workflow {self.wf} was undone.", out["error"]["message"])
		self.assertIn(f"The workflow {old} is active again.", out["error"]["message"])
		frappe.db.rollback()
		after = frappe.get_doc(WF, self.wf).as_dict()
		self.assertEqual(after, before, "restored exactly, rows and timestamps included")
		self.assertEqual(self.active(), [old])

	def test_an_edit_that_stores_something_else_than_the_card_is_rolled_back(self):
		"""No column is added, so the edit is one transaction: the mismatch rolls it
		back and nothing of it is left."""
		before = frappe.get_doc(WF, self.wf).as_dict()
		row = frappe.get_doc(WF, self.wf).transitions[0].name
		name = self.park_update(self.wf, transitions=[{"name": row, "allow_self_approval": 0}])
		self.wf_hook("wf_other_hook", event="validate")
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertIn("did not match the confirmation card", out["error"]["message"])
		frappe.db.rollback()
		frappe.clear_document_cache(WF, self.wf)
		self.assertEqual(frappe.get_doc(WF, self.wf).as_dict(), before, "rows and timestamps included")
		self.assertEqual(len(self.logged("post_write_mismatch", name)), 1)

	def test_an_edit_waits_for_the_lock_and_obeys_the_off_switch(self):
		name = self.park_update(self.wf, send_email_alert=1)
		with gs.structure_lock(self.dt):
			out = self.confirm(name)
		self.assertEqual((out["ok"], out["error"]["kind"]), (False, "retry_later"), out)
		self.assertEqual(self.row(name).status, "Pending", "the card is still there to confirm")
		frappe.conf[gs.OFF_SWITCH] = 1
		self.addCleanup(frappe.conf.pop, gs.OFF_SWITCH, None)
		out = self.confirm(name)
		self.assertEqual((out["ok"], out["error"]["code"]), (False, "structure_refused"), out)
		self.assertEqual(frappe.db.get_value(WF, self.wf, "send_email_alert"), 0)
		frappe.conf.pop(gs.OFF_SWITCH, None)
		self.conv = self.make_conv(SM_USER)
		self.assert_refused_edit_when_off()

	def assert_refused_edit_when_off(self):
		frappe.conf[gs.OFF_SWITCH] = 1
		r = self.update_wf(self.wf, send_email_alert=1)
		frappe.conf.pop(gs.OFF_SWITCH, None)
		self.assert_refused(r, "structure_refused")

	def test_the_operators_undo_puts_an_edit_back(self):
		before = frappe.get_doc(WF, self.wf).as_dict()
		name = self.park_update(self.wf, send_email_alert=1, is_active=1)
		self.assertTrue(self.confirm(name)["ok"])
		self.assertEqual(self.active(), [self.wf])
		note = frappe._dict(gs.undo_confirmation(name))
		self.assertTrue(note.restored, note.message)
		frappe.db.rollback()
		frappe.clear_document_cache(WF, self.wf)
		self.assertEqual(frappe.get_doc(WF, self.wf).as_dict(), before, "rows and timestamps included")
		self.assertEqual(self.active(), [])

	def test_an_edit_whose_worker_died_is_put_back(self):
		before = frappe.get_doc(WF, self.wf).as_dict()
		name = self.park_update(self.wf, send_email_alert=1)
		with (
			patch("jarvis.chat.pending_actions._execute._terminal_update", side_effect=SystemExit("killed")),
			self.assertRaises(SystemExit),
		):
			self.confirm(name)
		frappe.db.rollback()
		# No column is added, so the edit and its record of it are one transaction:
		# the dead worker's edit went with it, and the row is all that is left.
		self.assertEqual(self.row(name).status, "Executing")
		frappe.db.sql(
			f"UPDATE `tab{PA}` SET executing_at=%(t)s WHERE name=%(n)s AND status='Executing'",
			{"t": frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-700), "n": name},
		)
		frappe.db.commit()
		self.reconcile_own(name)
		row = self.row(name)
		self.assertEqual((row.status, row.reason_code), ("Failed", "interrupted"))
		frappe.clear_document_cache(WF, self.wf)
		self.assertEqual(frappe.get_doc(WF, self.wf).as_dict(), before)

	def test_clean_up_never_overwrites_a_later_edit(self):
		before = wg._before_image(self.wf)
		with as_user(SM_USER):
			doc = frappe.get_doc(WF, self.wf)
			doc.send_email_alert = 1
			doc.save()
		frappe.db.commit()
		ours = str(frappe.db.get_value(WF, self.wf, "modified"))
		undo = {"risk": "workflow_edit", "name": self.wf, "dt": self.dt, "before": before}
		with as_user(SM_USER):
			doc = frappe.get_doc(WF, self.wf)
			doc.override_status = 1
			doc.save()
		frappe.db.commit()
		note = wg.WorkflowEdit.clean_up({**undo, "written": {"name": self.wf, "modified": ours}})
		self.assertIn("changed again in the meantime", note.text)
		self.assertFalse(note.clean)
		self.assertEqual(frappe.db.get_value(WF, self.wf, ["send_email_alert", "override_status"]), (1, 1))
		note = wg.WorkflowEdit.clean_up(undo)
		self.assertIn("was not changed", note.text, "no stamp: nothing of it committed")

	def test_a_stale_card_does_not_run(self):
		name = self.park_update(self.wf, is_active=1)
		with as_user(SM_USER):
			doc = frappe.get_doc(WF, self.wf)
			doc.send_email_alert = 1
			doc.save()
		frappe.db.commit()
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual(out["reason_code"], "stale")
		self.assertEqual(self.active(), [])

	def test_someone_who_is_not_a_system_manager_is_refused_at_park(self):
		r = self.run_tool(
			"update_doc",
			{"doctype": WF, "name": self.wf, "changes": {"is_active": 1}},
			conv=self.make_conv(NOT_SM),
			user=NOT_SM,
		)
		self.assertFalse(r["ok"], r)
		self.assertEqual(r["error"]["code"], "PermissionDeniedError", r)


# --------------------------------------------------------------------------- #
# The two settings documents
# --------------------------------------------------------------------------- #
class TestSubmittableWorkflow(_WfBase):
	"""A workflow on a form that can be submitted: what the card says about it,
	Frappe's own document-status rules, and that an approval counts as use."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sub = "Jcf Sub " + uuid.uuid4().hex[:6]
		cls.scratch.append(cls.sub)
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": cls.sub,
				"module": "Custom",
				"custom": 1,
				"autoname": "hash",
				"is_submittable": 1,
				"fields": [{"label": "Title", "fieldname": "title", "fieldtype": "Data"}],
				"permissions": [
					{
						"role": "System Manager",
						"read": 1,
						"write": 1,
						"create": 1,
						"submit": 1,
						"cancel": 1,
					}
				],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def sub_values(self, **over) -> dict:
		return self.wf_values(
			dt=self.sub,
			states=[
				{"state": self.s_open, "doc_status": "0", "allow_edit": "System Manager"},
				{"state": self.s_ok, "doc_status": "1", "allow_edit": "System Manager"},
				{"state": self.s_no, "doc_status": "2", "allow_edit": "System Manager"},
			],
			transitions=[
				{
					"state": self.s_open,
					"action": self.a_ok,
					"next_state": self.s_ok,
					"allowed": "System Manager",
				},
				{
					"state": self.s_ok,
					"action": self.a_no,
					"next_state": self.s_no,
					"allowed": "System Manager",
				},
			],
			**over,
		)

	def park_sub(self, **over) -> str:
		r = self.run_tool("create_doc", {"doctype": WF, "values": self.sub_values(**over)})
		self.assertTrue(r.get("ok"), r)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		return self.card_name()

	def test_the_card_says_which_states_submit_and_cancel(self):
		line = self.card(self.park_sub())["risk_line"]
		self.assertIn(f"Moving a record to {self.s_ok} submits it.", line)
		self.assertIn(f"Moving a record to {self.s_no} cancels it.", line)

	def test_frappes_own_document_status_rules_are_refused_at_park(self):
		back = self.sub_values()
		back["transitions"].append(
			{"state": self.s_ok, "action": self.a_ok, "next_state": self.s_open, "allowed": "System Manager"}
		)
		r = self.run_tool("create_doc", {"doctype": WF, "values": back})
		self.assert_refused(r, "InvalidArgumentError", "cannot be converted back to draft")
		self.assert_no_workflow(dt=self.sub)

	def test_the_probe_frappe_checks_names_its_form(self):
		"""Frappe 16.51 reads the form's meta inside ``validate_docstatus``; an older
		bench does not, so this holds the probe to it on every version."""
		from frappe.workflow.doctype.workflow.workflow import Workflow

		seen = []
		real = Workflow.validate_docstatus

		def spy(doc):
			seen.append(doc.document_type)
			frappe.get_meta(doc.document_type)
			return real(doc)

		with patch.object(Workflow, "validate_docstatus", spy):
			self.park_sub()
		self.assertTrue(seen)
		self.assertEqual(set(seen), {self.sub})

	def _confirmed_with_records(self):
		names = self.records(2, dt=self.sub)
		name = self.park_sub()
		self.assertTrue(self.confirm(name)["ok"])
		self.assertEqual(set(self.states_of(self.sub).values()), {self.s_open})
		return name, names

	def test_an_approved_record_counts_as_use(self):
		"""Review: the save fills the first state of every document status, so a
		record approved since carries a state the fill itself could have written.
		Only its save tells, and the undo must leave the workflow for a person."""
		from frappe.model.workflow import apply_workflow

		name, names = self._confirmed_with_records()
		with as_user(SM_USER):
			apply_workflow(frappe.get_doc(self.sub, names[0]), self.a_ok)
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(self.sub, names[0], ["docstatus", STATE]), (1, self.s_ok))
		note = frappe._dict(gs.undo_confirmation(name))
		self.assertTrue(note.needs_person, note.message)
		self.assertFalse(note.restored)
		self.assertEqual(self.active(self.sub), [f"Jwf Flow {self.tag}"], "left for a person")
		self.assertEqual(
			self.states_of(self.sub), {names[0]: self.s_ok, names[1]: self.s_open}, "nothing was emptied"
		)

	def test_emptying_the_fill_never_touches_a_record_saved_since(self):
		name, names = self._confirmed_with_records()
		undo = _seal.unseal_undo(self.row(name))
		since = undo["written"]["modified"]
		frappe.db.set_value(
			self.sub, names[0], "modified", frappe.utils.add_to_date(since, seconds=5), update_modified=False
		)
		frappe.db.commit()
		self.assertTrue(wg._used_since(self.sub, undo["fill"], since), "a save since is use")
		self.assertEqual(wg._empty_fill(self.sub, undo["fill"], since), 1)
		frappe.db.commit()
		self.assertEqual(self.states_of(self.sub), {names[0]: self.s_open, names[1]: None})

	def test_an_undo_nobody_used_is_clean(self):
		name, names = self._confirmed_with_records()
		note = frappe._dict(gs.undo_confirmation(name))
		self.assertTrue(note.restored, note.message)
		self.assertFalse(frappe.db.exists(WF, f"Jwf Flow {self.tag}"))
		self.assertEqual(self.states_of(self.sub), dict.fromkeys(names, None), "what it filled is emptied")


class _SettingsBase(_WfBase):
	"""Scratch forms to stand in for the forms a setting adds fields to, and the
	shared settings documents put back exactly as they were found."""

	WIDE = True
	SETTINGS = ""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.dt2 = "Jcf Second " + uuid.uuid4().hex[:6]
		cls.scratch.append(cls.dt2)
		cls._make_doctype(cls.dt2, 2)
		cls._found = sg._before(cls.SETTINGS)
		cls._found_default = frappe.db.get_default("campaign_naming_by")
		cls._found_fields = cls._shared_fields()
		cls.addClassCleanup(cls._put_settings_back)

	@classmethod
	def _shared_fields(cls):
		"""The real forms the CRM data sync adds fields to: never touched here."""
		return (
			sorted(
				frappe.get_all(
					"Custom Field", filters={"dt": ["in", ["Quotation", "Customer"]]}, pluck="name"
				)
			),
			sorted(_columns("Quotation")),
			sorted(_columns("Customer")),
		)

	@classmethod
	def _put_settings_back(cls):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		sg._restore(cls.SETTINGS, cls._found)
		if cls._found_default is not None:
			frappe.db.set_default("campaign_naming_by", cls._found_default)
		frappe.db.commit()
		frappe.clear_cache()
		assert sg._before(cls.SETTINGS) == cls._found, f"{cls.SETTINGS} was not put back"
		assert cls._shared_fields() == cls._found_fields, "a shared form was altered"

	def setUp(self):
		super().setUp()
		self.addCleanup(self._reset_settings)

	def _reset_settings(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		sg._restore(self.SETTINGS, self._found)
		frappe.db.commit()

	def update_settings(self, **changes) -> dict:
		return self.run_tool(
			"update_doc", {"doctype": self.SETTINGS, "name": self.SETTINGS, "changes": changes}
		)

	def park_settings(self, **changes) -> str:
		r = self.update_settings(**changes)
		self.assertTrue(r.get("ok"), r)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		return self.card_name()

	def stored(self, fieldname: str):
		frappe.db.rollback()
		rows = frappe.db.sql(
			"SELECT value FROM `tabSingles` WHERE doctype=%(d)s AND field=%(f)s",
			{"d": self.SETTINGS, "f": fieldname},
		)
		return rows[0][0] if rows else None


SYNC = wr.CRM_SYNC_FIELD


class TestCrmSettings(_SettingsBase):
	SETTINGS = CRM

	def setUp(self):
		super().setUp()
		self.forms = [self.dt, self.dt2]
		stub = patch(
			"erpnext.crm.doctype.crm_settings.crm_settings.CRMSettings.get_frappe_crm_custom_fields",
			new=staticmethod(lambda: self.sync_fields()),
		)
		stub.start()
		self.addCleanup(stub.stop)
		# The site starts with the data sync off, whatever a developer left on it.
		frappe.db.set_single_value(CRM, SYNC, 0, update_modified=False)
		frappe.db.commit()
		frappe.clear_document_cache(CRM, CRM)

	def sync_fields(self) -> dict:
		field = {
			"fieldname": "jcf_deal",
			"fieldtype": "Data",
			"label": "Frappe CRM Deal",
			"insert_after": "title",
		}
		return {dt: [dict(field)] for dt in self.forms}

	def on(self, **more) -> dict:
		return {SYNC: 1, "allowed_users": [{"user": SM_USER}], **more}

	def assert_no_sync_fields(self):
		frappe.db.rollback()
		for dt in self.scratch:
			self.assertNotIn("jcf_deal", _field_rows(dt), dt)
			self.assertNotIn("jcf_deal", _columns(dt), dt)

	# -- the plain update: settings, no structure ---------------------------------
	def test_with_the_sync_off_it_is_an_ordinary_settings_card(self):
		args = {"doctype": CRM, "name": CRM, "changes": {"close_opportunity_after_days": "21"}}
		self.assertEqual(wr.risk_of("update_doc", args), "sensitive")
		self.assertEqual(wr.risk_line("update_doc", args), "This changes a setting for every user.")
		frappe.db.set_value("Jarvis Conversation", self.conv, "skip_confirmation", 1)
		frappe.db.commit()
		name = self.park_settings(close_opportunity_after_days="21")  # carded in every mode
		card = self.card(name)
		self.assertEqual(card["risk"], "sensitive")
		self.assertNotIn("structural", card, "no structure changes: not a guarded card")
		self.assertEqual(card["risk_line"], "This changes a setting for every user.")
		self.assertEqual(card["diff"][0]["to"], "21")
		frappe.db.set_value("Jarvis Conversation", self.conv, "skip_confirmation", 0)
		frappe.db.commit()
		with patch.object(gs, "structure_lock", side_effect=AssertionError("no lock for plain settings")):
			out = self.confirm(name)
		self.assertTrue(out["ok"], out)
		self.assertEqual(cint_(self.stored("close_opportunity_after_days")), 21)
		self.assert_no_sync_fields()

	def test_the_switch_is_read_however_it_is_spelled(self):
		"""Lesson 3: a word, padding or another case never makes it the plain class."""
		for value in (1, "1", " 1 ", "true", "YES", "on", True, 2, "abc"):
			args = {"doctype": CRM, "name": CRM, "changes": {SYNC: value}}
			self.assertEqual(wr.risk_of("update_doc", args), "crm_settings_sync", value)
			self.assertEqual(
				wr.risk_of("update_doc", {**args, "doctype": "crm settings"}), "crm_settings_sync"
			)
		for value in (0, "0", "false", "No", " off ", None, ""):
			args = {"doctype": CRM, "name": CRM, "changes": {SYNC: value}}
			self.assertEqual(wr.risk_of("update_doc", args), "sensitive", value)
		frappe.db.set_single_value(CRM, SYNC, 1, update_modified=False)
		frappe.db.commit()
		args = {"doctype": CRM, "name": CRM, "changes": {"close_opportunity_after_days": 9}}
		self.assertEqual(wr.risk_of("update_doc", args), "crm_settings_sync", "the stored switch counts")
		doc = frappe.get_doc(CRM)
		self.assertEqual(wr.doc_risk(doc, "before_validate"), "crm_settings_sync")
		doc.set(SYNC, 0)
		self.assertEqual(wr.doc_risk(doc, "before_validate"), "sensitive")

	def test_a_plain_card_cannot_run_once_the_sync_is_on(self):
		"""The card was parked as plain settings; then the sync was switched on in
		Desk. The same call would now add fields nobody was shown: refused."""
		name = self.park_settings(close_opportunity_after_days=22)
		frappe.db.set_single_value(CRM, SYNC, 1, update_modified=False)
		frappe.db.commit()
		frappe.clear_document_cache(CRM, CRM)
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertEqual(out["error"]["code"], "structure_refused")
		self.assert_no_sync_fields()
		self.assertNotEqual(cint_(self.stored("close_opportunity_after_days")), 22)

	# -- the data sync: guarded ---------------------------------------------------
	def test_turning_the_sync_on_parks_a_structural_card_and_nothing_exists(self):
		cached = frappe.get_cached_doc(CRM).as_dict()
		with patch("jarvis.api._run_preview") as trial:
			name = self.park_settings(**{SYNC: "yes", "allowed_users": [{"user": SM_USER}]})
		trial.assert_not_called()
		card = self.card(name)
		self.assertEqual((card["kind"], card["risk"], card["structural"]), ("update", "sensitive", True))
		line = card["risk_line"]
		self.assertTrue(line.startswith(gs.STRUCTURAL_LINE), line)
		self.assertIn(
			f"The Frappe CRM data sync adds the fields jcf_deal to {self.dt}; jcf_deal to {self.dt2}.", line
		)
		self.assertIn("the allowed users can create customers", line)
		self.assertIn(ACCESS_LINE, line)
		sealed = self.call(name)
		self.assertEqual(
			sealed, {"doctype": CRM, "name": CRM, "changes": {SYNC: 1, "allowed_users": [{"user": SM_USER}]}}
		)
		self.assert_no_sync_fields()
		self.assertEqual(cint_(self.stored(SYNC)), 0)
		self.assertEqual(
			frappe.get_cached_doc(CRM).as_dict(), cached, "lesson 1: the cached settings are untouched"
		)

	def test_confirm_adds_the_fields_to_every_form_under_all_their_locks(self):
		name = self.park_settings(**self.on())
		for held in (CRM, self.dt, self.dt2):
			with gs.structure_lock(held):
				out = self.confirm(name)
			self.assertEqual((out["error"]["kind"], out["pa_status"]), ("retry_later", "Pending"), held)
			self.assert_no_sync_fields()
		out = self.confirm(name)
		self.assertTrue(out["ok"], out)
		for dt in self.forms:
			self.assertIn("jcf_deal", _field_rows(dt))
			self.assertIn("jcf_deal", _columns(dt))
		self.assertEqual(cint_(self.stored(SYNC)), 1)
		self.assertEqual(self.audit("update_doc", CRM), ["applied"])
		self.assertFalse(self.row(name).sealed_undo)

	def test_a_later_save_with_the_sync_on_is_guarded_and_adds_nothing(self):
		self.assertTrue(self.confirm(self.park_settings(**self.on()))["ok"])
		self.conv = self.make_conv(SM_USER)
		name = self.park_settings(close_opportunity_after_days=30)
		card = self.card(name)
		self.assertTrue(card["structural"])
		self.assertTrue(
			card["risk_line"].startswith("Confirming changes CRM settings for every user."), card["risk_line"]
		)
		self.assertIn("so none is added", card["risk_line"])
		self.assertTrue(self.confirm(name)["ok"])
		self.assertEqual(cint_(self.stored("close_opportunity_after_days")), 30)

	def _own_field(self) -> dict:
		"""A person's own field on a form the sync writes to, under the sync's name."""
		with as_user(SM_USER):
			doc = frappe.get_doc(
				{
					"doctype": "Custom Field",
					"dt": self.dt,
					"fieldname": "jcf_deal",
					"label": "My Deal",
					"fieldtype": "Data",
					"description": "ours",
				}
			).insert()
		frappe.db.commit()
		return frappe.db.get_value("Custom Field", doc.name, "*", as_dict=True)

	def test_a_field_the_sync_overwrites_is_named_and_restored(self):
		"""Review m1. ``create_custom_fields`` re-saves an existing field of the same
		name with ERPNext's own values. The card says so, and a failed confirm puts
		that field back exactly: it is never reported as nothing changed while the
		label stays rewritten."""
		before = self._own_field()
		name = self.park_settings(**self.on())
		line = self.card(name)["risk_line"]
		self.assertIn(
			f"It also rewrites the existing field jcf_deal on {self.dt} (label: My Deal to Frappe CRM Deal",
			line,
		)
		self.assertIn(f"adds the field jcf_deal to {self.dt2}.", line)
		self.wf_hook("crm_validation_hook", doctype=CRM)
		out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertIn(f"The field jcf_deal on {self.dt} was put back as it was.", out["error"]["message"])
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Custom Field", before.name, "*", as_dict=True), before)
		frappe.local.doc_events_hooks = None
		# A later change to that field is never overwritten, and never called clean.
		self.conv = self.make_conv(SM_USER)
		name = self.park_settings(**self.on())
		undo_seen = {}
		real = sg.CrmSettingsSync.clean_up

		def _edited_meanwhile(undo):
			frappe.db.set_value("Custom Field", before.name, "label", "Changed since")
			undo_seen.update(undo)
			return real(undo)

		self.wf_hook("crm_validation_hook", doctype=CRM)
		with patch.object(sg.CrmSettingsSync, "clean_up", staticmethod(_edited_meanwhile)):
			out = self.confirm(name)
		self.assertEqual(out["outcome"], "partial")
		self.assertIn("was changed again in the meantime", out["error"]["message"])
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Custom Field", before.name, "label"), "Changed since")
		self.assertTrue(undo_seen.get("rewrote"), "the rewrite was stamped on the confirmation row")

	def test_park_checks(self):
		for changes, code, says in (
			({SYNC: 1}, "InvalidArgumentError", "Allowed Users"),
			(
				self.on(allowed_users=[{"user": "nobody@example.com"}]),
				"InvalidArgumentError",
				"there is no user",
			),
			(self.on(allowed_users=[{"user": SM_USER.upper()}]), "InvalidArgumentError", "there is no user"),
			(
				self.on(allowed_users=[{"user": SM_USER, "role": "x"}]),
				"InvalidArgumentError",
				"leave out role",
			),
			(self.on(bogus=1), "InvalidArgumentError", "no setting named bogus"),
			(self.on(close_opportunity_after_days="soon"), "InvalidArgumentError", "whole number"),
		):
			with self.subTest(changes=changes):
				self.assert_refused(self.update_settings(**changes), code, says)
		self.assert_no_sync_fields()
		self.assertEqual(cint_(self.stored(SYNC)), 0)

	def test_every_form_is_pre_checked(self):
		"""Plan rev 4 item 6: refused unless every table takes exactly its field."""
		frappe.db.sql_ddl(f"ALTER TABLE `tab{self.dt2}` ADD COLUMN `jcf_deal` varchar(140)")
		r = self.update_settings(**self.on())
		self.assert_refused(
			r, "structure_refused", ["cannot add its field to", "already has a column named jcf_deal"]
		)
		self.assertEqual(r["error"]["desk_path"], "/app/crm-settings")
		frappe.db.sql_ddl(f"ALTER TABLE `tab{self.dt2}` DROP COLUMN `jcf_deal`")
		self.forms = [self.dt, self.wide]
		self.assert_refused(self.update_settings(**self.on()), "structure_refused", "no room for the field")
		self.forms = [self.dt, "Jwf No Such Form"]
		self.assert_refused(self.update_settings(**self.on()), "structure_refused", "not a form on this site")
		self.assert_no_sync_fields()

	def test_a_failed_alter_puts_everything_back(self):
		"""The settings and BOTH field rows commit before the first ALTER. Whichever
		form Frappe alters first, no field row is left without its column and the
		settings read as before."""
		self.forms = [self.dt2, self.wide]
		before = sg._before(CRM)
		with patch.object(gs, "ROW_SIZE_LIMIT", 10**9):
			name = self.park_settings(**self.on())
			out = self.confirm(name)
		self.assertFalse(out["ok"], out)
		self.assertIn("row size limit", out["error"]["message"])
		self.assertIn("CRM Settings was put back as it was.", out["error"]["message"])
		frappe.db.rollback()
		self.assertEqual(sg._before(CRM), before, "the settings, their users and their timestamp")
		self.assertNotIn("jcf_deal", _field_rows(self.wide))
		complete = "jcf_deal" in _columns(self.dt2)
		self.assertEqual(
			"jcf_deal" in _field_rows(self.dt2), complete, "never a field row without its column"
		)
		self.assertEqual(out["outcome"], "partial" if complete else "failed")
		self.assertEqual(out["error"]["kind"], "not_fixable")
		for dt in (self.dt2, self.wide):
			with as_user(SM_USER):
				frappe.get_list(dt, fields=["name", "title"])

	def test_a_hook_failure_after_the_fields_were_added(self):
		self.wf_hook("crm_validation_hook", doctype=CRM)
		before = sg._before(CRM)
		out = self.confirm(self.park_settings(**self.on()))
		self.assertFalse(out["ok"], out)
		self.assertEqual(out["outcome"], "partial")
		self.assertIn("left in place", out["error"]["message"])
		frappe.db.rollback()
		self.assertEqual(sg._before(CRM), before)
		for dt in self.forms:
			self.assertIn("jcf_deal", _columns(dt))

	def test_clean_up_never_overwrites_a_later_change(self):
		before = sg._before(CRM)
		frappe.db.set_single_value(CRM, "close_opportunity_after_days", 44)
		frappe.db.commit()
		ours = str(sg._modified(CRM))
		undo = {"risk": "crm_settings_sync", "before": before}
		frappe.db.set_single_value(CRM, "close_opportunity_after_days", 55)
		frappe.db.set_single_value(
			CRM, "modified", frappe.utils.add_to_date(ours, seconds=3), update_modified=False
		)
		frappe.db.commit()
		note = sg.CrmSettingsSync.clean_up({**undo, "written": {"name": CRM, "modified": ours}})
		self.assertIn("changed again in the meantime", note.text)
		self.assertFalse(note.clean)
		self.assertEqual(cint_(self.stored("close_opportunity_after_days")), 55)
		self.assertEqual(sg.CrmSettingsSync.clean_up(undo).text, "Nothing was changed.")

	def test_the_reconciler_cleans_up_an_interrupted_sync(self):
		before = sg._before(CRM)
		name = self.park_settings(**self.on())

		def _killed(query, debug=False):
			frappe.db.commit()
			raise SystemExit("worker killed")

		with patch.object(frappe.db, "sql_ddl", side_effect=_killed), self.assertRaises(SystemExit):
			self.confirm(name)
		frappe.db.rollback()
		self.assertEqual(sorted(_field_rows(self.dt) + _field_rows(self.dt2)), ["jcf_deal", "jcf_deal"])
		self.assertEqual(cint_(self.stored(SYNC)), 1)
		frappe.db.sql(
			f"UPDATE `tab{PA}` SET executing_at=%(t)s WHERE name=%(n)s",
			{"t": frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-700), "n": name},
		)
		frappe.db.commit()
		with gs.structure_lock(self.dt2):
			self.reconcile_own(name)
		self.assertEqual(
			self.row(name).status, "Executing", "one of its forms is locked: left for the next run"
		)
		self.reconcile_own(name)
		self.assertEqual(self.row(name).status, "Failed")
		self.assert_no_sync_fields()
		self.assertEqual(sg._before(CRM), before)

	def test_someone_who_may_not_change_it_is_refused(self):
		conv = self.make_conv(NOT_SM)
		r = self.run_tool(
			"update_doc", {"doctype": CRM, "name": CRM, "changes": self.on()}, conv=conv, user=NOT_SM
		)
		self.assertEqual(r["error"]["code"], "PermissionDeniedError", r)


def cint_(value) -> int:
	return frappe.utils.cint(value)


class TestDomainSettings(_SettingsBase):
	SETTINGS = DOMAIN

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.d_on, cls.d_off = f"Jwf Domain On {cls.tag}", f"Jwf Domain Off {cls.tag}"
		cls.r_on, cls.r_off = f"Jwf Role On {cls.tag}", f"Jwf Role Off {cls.tag}"
		cls.module = f"Jwf Module {cls.tag}"
		cls.addClassCleanup(cls._drop_domain_records)
		for domain in (cls.d_on, cls.d_off):
			frappe.get_doc({"doctype": "Domain", "domain": domain}).insert()
		for role in (cls.r_on, cls.r_off):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert()
		# Written raw: a Module Def's own checks (which app it belongs to, whether that
		# app is installed) differ between Frappe releases, and nothing here needs them.
		module = frappe.get_doc({"doctype": "Module Def", "module_name": cls.module, "custom": 1})
		module.name = cls.module
		module.db_insert()
		frappe.db.commit()

	@classmethod
	def _drop_domain_records(cls):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		like = f"Jwf % {cls.tag}"
		frappe.db.delete("Has Role", {"role": ["like", like]})
		frappe.db.delete("Has Domain", {"domain": ["like", like]})
		for doctype in ("Role", "Domain", "Module Def"):
			frappe.db.delete(doctype, {"name": ["like", like]})
		frappe.db.commit()
		frappe.clear_cache()
		for doctype in ("Role", "Domain", "Module Def", "Has Role"):
			key = "role" if doctype == "Has Role" else "name"
			assert not frappe.db.exists(doctype, {key: ["like", like]}), f"a scratch {doctype} was left"

	def setUp(self):
		super().setUp()
		ensure_user(NOT_SM)
		self.addCleanup(self._reset_domain_records)
		self.holder = self.give(NOT_SM, self.r_off)

	def _reset_domain_records(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.db.delete("Has Role", {"role": ["in", [self.r_on, self.r_off]]})
		for role in (self.r_on, self.r_off):
			frappe.db.set_value(
				"Role", role, {"disabled": 0, "restrict_to_domain": None}, update_modified=False
			)
		frappe.db.set_value("Module Def", self.module, "restrict_to_domain", None, update_modified=False)
		frappe.db.commit()

	def give(self, user: str, role: str) -> str:
		row = frappe.get_doc(
			{
				"doctype": "Has Role",
				"parent": user,
				"parenttype": "User",
				"parentfield": "roles",
				"role": role,
			}
		)
		row.db_insert()
		frappe.db.commit()
		return row.name

	@contextmanager
	def declared(self, **domains):
		"""Domains an app declares, read through the two calls Frappe's own save
		and the park check both make."""
		real = frappe.get_hooks

		def hooks(hook=None, *args, **kwargs):
			if hook == "domains":
				return {name: ["jarvis.tests.declared"] for name in domains}
			return real(hook, *args, **kwargs)

		with (
			patch.object(frappe, "get_hooks", side_effect=hooks),
			patch.object(frappe, "get_domain_data", side_effect=lambda d: frappe._dict(domains.get(d) or {})),
		):
			yield

	def both(self, **on_data) -> dict:
		return {
			self.d_on: {"restricted_roles": [self.r_on], **on_data},
			self.d_off: {"restricted_roles": [self.r_off], "modules": [self.module]},
		}

	def active_domains(self) -> list[str]:
		frappe.db.rollback()
		return sorted(frappe.get_all("Has Domain", filters={"parent": DOMAIN}, pluck="domain"))

	def test_on_a_site_with_no_declared_domains_only_the_list_changes(self):
		name = self.park_settings(active_domains=[{"domain": self.d_on}])
		card = self.card(name)
		self.assertEqual((card["kind"], card["risk"], card["structural"]), ("update", "sensitive", True))
		self.assertEqual(
			card["risk_line"],
			"Confirming changes the active domains of this site for every user. No app on this site declares "
			"domains, so only the list of active domains changes: no role or module is switched.",
		)
		self.assertEqual(self.active_domains(), [], "nothing after the park")
		self.assertTrue(self.confirm(name)["ok"])
		self.assertEqual(self.active_domains(), [self.d_on])
		self.assertTrue(frappe.db.exists("Has Role", self.holder), "no role was touched")
		# Replacing the list keeps working: the saved row is re-used, not orphaned.
		self.conv = self.make_conv(SM_USER)
		name = self.park_settings(active_domains=[{"domain": self.d_off}])
		self.assertTrue(self.confirm(name)["ok"], "a list that names none of the saved rows still saves")
		self.assertEqual(self.active_domains(), [self.d_off])

	def test_a_domain_that_runs_code_or_writes_elsewhere_is_refused(self):
		for key, says in (
			("on_setup", "runs its own setup code"),
			("set_value", "writes values into other documents"),
			("properties", "changes field properties on other forms"),
		):
			with self.subTest(key=key), self.declared(**{self.d_off: {key: ["x"]}}):
				r = self.update_settings(active_domains=[{"domain": self.d_on}])
				self.assert_refused(r, "structure_refused", [self.d_off, says])
				self.assertEqual(r["error"]["desk_path"], "/app/domain-settings")
		self.assertEqual(self.active_domains(), [])

	def test_the_card_lists_the_roles_and_modules_turned_off(self):
		with self.declared(**self.both()):
			name = self.park_settings(active_domains=[{"domain": self.d_on}])
		line = self.card(name)["risk_line"]
		self.assertIn(
			f"It turns off for every user: the role {self.r_off} (1 role assignment is removed) and the "
			f"module {self.module}.",
			line,
		)
		self.assertIn(f"It turns on: the role {self.r_on} (also given to you).", line)
		self.assertIn(ACCESS_LINE, line)
		self.assertTrue(frappe.db.exists("Has Role", self.holder), "nothing after the park")
		self.assertFalse(frappe.db.get_value("Role", self.r_off, "disabled"))

	def test_confirm_switches_them_and_the_undo_stays_on_the_row(self):
		"""Plan rev 4 item 7: the Has Role rows it deletes are snapshotted on the
		confirmation row so an undo exists."""
		before = sg._before(DOMAIN)
		with self.declared(**self.both()):
			name = self.park_settings(active_domains=[{"domain": self.d_on}])
			out = self.confirm(name)
			self.assertTrue(out["ok"], out)
			self.assertFalse(frappe.db.exists("Has Role", self.holder), "the role was taken from its holder")
			self.assertEqual(frappe.db.get_value("Role", self.r_off, "disabled"), 1)
			self.assertEqual(frappe.db.get_value("Module Def", self.module, "restrict_to_domain"), self.d_off)
			self.assertTrue(
				frappe.db.exists("Has Role", {"parent": SM_USER, "role": self.r_on}), "given to the confirmer"
			)
			undo = _seal.unseal_undo(self.row(name))
			self.assertEqual([r["name"] for r in undo["has_role"]], [self.holder])
			self.reconcile_own(name)
			self.assertTrue(self.row(name).sealed_undo, "kept: it is the undo, not a clean-up left to do")
			note = frappe._dict(gs.undo_confirmation(name))
		self.assertTrue(note.complete, note.message)
		self.assertIn("1 role assignment it removed was put back.", note.message)
		frappe.db.rollback()
		self.assertEqual(
			frappe.db.get_value("Has Role", self.holder, ["parent", "role"]), (NOT_SM, self.r_off)
		)
		self.assertEqual(
			frappe.db.get_value("Role", self.r_off, ["disabled", "restrict_to_domain"]), (0, None)
		)
		self.assertIsNone(frappe.db.get_value("Module Def", self.module, "restrict_to_domain"))
		self.assertFalse(frappe.db.exists("Has Role", {"parent": SM_USER, "role": self.r_on}))
		self.assertEqual(sg._before(DOMAIN), before)

	def test_park_checks(self):
		for changes, says in (
			({}, "give the whole active_domains list"),
			({"bogus": 1}, "leave out bogus"),
			({"active_domains": "x"}, "must be a list of rows"),
			({"active_domains": [{"domain": "Jwf No Such Domain"}]}, "there is no Domain named"),
			({"active_domains": [{"domain": self.d_on.upper()}]}, "there is no Domain named"),
			({"active_domains": [{"domain": self.d_on}, {"domain": self.d_on}]}, "more than once"),
			({"active_domains": [{"domain": self.d_on, "extra": 1}]}, "leave out extra"),
		):
			with self.subTest(changes=changes):
				r = self.update_settings(**changes)
				self.assertFalse(r["ok"], r)
				self.assertIn(says, r["error"]["message"])
		self.assertEqual(self.active_domains(), [])

	def test_too_many_role_assignments_to_undo_is_refused(self):
		with self.declared(**self.both()), patch.object(sg, "HAS_ROLE_MAX", 0):
			r = self.update_settings(active_domains=[{"domain": self.d_on}])
		self.assert_refused(r, "structure_refused", "is not done from chat")

	def test_the_role_cap_holds_at_its_real_boundary(self):
		self.assertEqual(sg.HAS_ROLE_MAX, 5000)
		real = frappe.db.count

		def counted(n):
			return lambda dt, *a, **k: n if dt == "Has Role" else real(dt, *a, **k)

		with self.declared(**self.both()):
			self.assertEqual(
				sorted(sg.declared_domains()), sorted([self.d_on, self.d_off]), "read as Frappe reads them"
			)
			with patch.object(frappe.db, "count", side_effect=counted(5001)):
				r = self.update_settings(active_domains=[{"domain": self.d_on}])
			self.assert_refused(r, "structure_refused", ["5001", "5000"])
			with patch.object(frappe.db, "count", side_effect=counted(5000)):
				name = self.park_settings(active_domains=[{"domain": self.d_on}])
		self.assertIn("5000 role assignments are removed", self.card(name)["risk_line"])

	def test_a_domain_field_is_pre_checked_added_and_cleaned_up(self):
		field = {"fieldname": "jcf_dom", "fieldtype": "Data", "label": "Dom", "insert_after": "title"}
		with self.declared(**self.both(custom_fields={self.dt2: [field]})):
			name = self.park_settings(active_domains=[{"domain": self.d_on}])
			line = self.card(name)["risk_line"]
			self.assertTrue(line.startswith(gs.STRUCTURAL_LINE), line)
			self.assertIn(f"It adds the field jcf_dom to {self.dt2}.", line)
			with gs.structure_lock(self.dt2):
				self.assertEqual(
					self.confirm(name)["pa_status"], "Pending", "the form it alters is locked too"
				)
			# Frappe decides which domains are active from a CACHED list while it
			# saves (domain_settings.py ``restrict_roles_and_modules``). Left stale, it
			# takes the domain being switched on for an inactive one and deletes the
			# field it has just added: the confirm drops that cache first.
			self.assertNotIn(self.d_on, frappe.get_active_domains(), "the stale list is in the cache")
			self.assertTrue(self.confirm(name)["ok"])
			self.assertIn("jcf_dom", _columns(self.dt2))
			self.assertIn("jcf_dom", _field_rows(self.dt2), "not deleted again by a stale list")
		# Not active any more: saving would delete that field, which chat never does.
		self.conv = self.make_conv(SM_USER)
		with self.declared(**self.both(custom_fields={self.dt2: [field]})):
			r = self.update_settings(active_domains=[])
		self.assert_refused(r, "structure_refused", "a field is never deleted from chat")
		self.assertIn("jcf_dom", _field_rows(self.dt2))

	def test_a_failed_domain_field_puts_the_settings_back(self):
		field = {"fieldname": "jcf_dom", "fieldtype": "Data", "label": "Dom", "insert_after": "title"}
		before = sg._before(DOMAIN)
		with (
			self.declared(**self.both(custom_fields={self.wide: [field]})),
			patch.object(gs, "ROW_SIZE_LIMIT", 10**9),
		):
			out = self.confirm(self.park_settings(active_domains=[{"domain": self.d_on}]))
		self.assertFalse(out["ok"], out)
		self.assertEqual(out["outcome"], "failed")
		self.assertIn("Domain Settings was put back as it was.", out["error"]["message"])
		frappe.db.rollback()
		self.assertEqual(sg._before(DOMAIN), before)
		self.assertNotIn("jcf_dom", _field_rows(self.wide))
		self.assertFalse(
			frappe.db.exists("Has Role", {"parent": SM_USER, "role": self.r_on}),
			"the role it gave is taken back",
		)
		self.assertTrue(frappe.db.exists("Has Role", self.holder))

	def test_someone_who_is_not_a_system_manager_is_refused(self):
		conv = self.make_conv(NOT_SM)
		changes = {"active_domains": [{"domain": self.d_on}]}
		r = self.run_tool(
			"update_doc", {"doctype": DOMAIN, "name": DOMAIN, "changes": changes}, conv=conv, user=NOT_SM
		)
		self.assertEqual(r["error"]["code"], "PermissionDeniedError", r)

	def test_the_off_switch_covers_the_settings_too(self):
		with patch.object(gs, "disabled", return_value=True):
			r = self.update_settings(active_domains=[{"domain": self.d_on}])
		self.assert_refused(r, "structure_refused", "Domain Settings changes the database structure")
