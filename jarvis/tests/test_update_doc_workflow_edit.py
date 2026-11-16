"""aerele/jarvis#2506: a Workflow holds for Jarvis users as it does in Desk.

``TestLockedState`` checks the edit rule against Desk's ``frappe.workflow.is_read_only``
with a mocked Workflow. Everything else runs a REAL Workflow through Frappe's real
code (no faked ``apply_workflow`` / ``run_workflow_transition``): Bank Transaction is
submittable, needs no other record, and has an allow-on-submit text field
(``reference_number``) that serves as the state field, so inserting the Workflow adds
no Custom Field and runs no DDL. Users and roles are made per test; ``frappe.db.commit``
is a no-op for the class (bulk approval commits per document) and every test is
rolled back.

The users: the EDITOR may edit in Draft and moves a document to Pending; the
APPROVER may approve Pending but edits nowhere (Abhas's "approver who is not an
editor"); only the LEAD role, which nobody holds, may edit Pending / Approved.
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.desk.doctype.note.note import Note
from frappe.model import workflow as fw
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools import _workflow_edit as we
from jarvis.tools import _write_risk as wr
from jarvis.tools.update_doc import update_doc

BT = "Bank Transaction"
WF = "Jarvis Test 2506"
FIELD = "reference_number"
EDITOR_ROLE, APPROVER_ROLE, LEAD_ROLE = "Jarvis 2506 Editor", "Jarvis 2506 Approver", "Jarvis 2506 Lead"
EDITOR, APPROVER = "wf2506-editor@example.com", "wf2506-approver@example.com"
HANDLER = "jarvis.tools._workflow_edit.guard_doc_event"


# --------------------------------------------------------------------------- #
# The edit rule, against Desk's is_read_only (mocked workflow)
# --------------------------------------------------------------------------- #
def _mock_workflow():
	return frappe._dict(
		workflow_state_field="workflow_state",
		states=[
			frappe._dict(state="Draft", doc_status="0", allow_edit="Sales User"),
			frappe._dict(state="Pending Approval", doc_status="0", allow_edit="Sales Manager"),
			frappe._dict(state="Pending Approval", doc_status="0", allow_edit="Accounts Manager"),
			frappe._dict(state="Approved", doc_status="1", allow_edit="Sales Manager"),
		],
	)


def _mock_doc(state=None, docstatus=0, new=False):
	d = frappe._dict(doctype="Sales Order", name="SO-1", docstatus=docstatus, workflow_state=state)
	d.is_new = lambda: new
	d.as_dict = lambda: {k: v for k, v in d.items() if not callable(v)}
	return d


class TestLockedState(FrappeTestCase):
	roles = ["Sales User"]
	has_workflow = True

	def setUp(self):
		self.workflow = _mock_workflow()
		for p in (
			patch.object(we, "get_workflow_name", side_effect=lambda dt: "WF" if self.has_workflow else None),
			patch.object(we, "get_workflow", side_effect=lambda dt: self.workflow),
			patch.object(we.frappe, "get_roles", side_effect=lambda user=None: list(self.roles)),
		):
			p.start()
			self.addCleanup(p.stop)

	def test_locked_when_user_lacks_every_edit_role(self):
		self.assertEqual(
			we.locked_state(_mock_doc("Pending Approval")),
			("Pending Approval", ["Accounts Manager", "Sales Manager"]),
		)

	def test_any_listed_role_unlocks(self):
		self.roles = ["Sales User", "Accounts Manager"]
		self.assertIsNone(we.locked_state(_mock_doc("Pending Approval")))

	def test_editable_state_is_not_locked(self):
		self.assertIsNone(we.locked_state(_mock_doc("Draft")))

	def test_no_active_workflow_is_never_locked(self):
		self.has_workflow = False
		self.assertIsNone(we.locked_state(_mock_doc("Pending Approval")))

	def test_new_document_is_never_locked(self):
		self.assertIsNone(we.locked_state(_mock_doc("Pending Approval", new=True)))

	def test_empty_state_uses_the_first_state_of_its_docstatus(self):
		# Desk's get_default_state: docstatus 0 -> Draft (editable), 1 -> Approved (locked).
		self.assertIsNone(we.locked_state(_mock_doc(None, docstatus=0)))
		self.assertEqual(we.locked_state(_mock_doc(None, docstatus=1)), ("Approved", ["Sales Manager"]))

	def test_state_without_edit_roles_is_read_only_for_everyone(self):
		self.workflow.states.append(frappe._dict(state="Frozen", doc_status="0", allow_edit=""))
		self.roles = ["Sales Manager", "Accounts Manager", "Sales User"]
		self.assertEqual(we.locked_state(_mock_doc("Frozen")), ("Frozen", []))
		with self.assertRaises(PermissionDeniedError) as cm:
			we.require_workflow_edit(_mock_doc("Frozen"))
		self.assertIn("nobody may edit", str(cm.exception))

	def test_refusal_names_the_state_and_roles(self):
		with self.assertRaises(PermissionDeniedError) as cm:
			we.require_workflow_edit(_mock_doc("Pending Approval"))
		msg = str(cm.exception)
		self.assertIn("Pending Approval", msg)
		self.assertIn("only Accounts Manager or Sales Manager may edit", msg)
		self.assertIn("apply_workflow_action", msg)

	def test_a_change_of_the_state_field_is_a_move(self):
		with self.assertRaises(PermissionDeniedError):
			we.require_no_state_move(_mock_doc("Draft"), {"workflow_state": "Pending Approval"})
		we.require_no_state_move(_mock_doc("Draft"), {"workflow_state": "Draft", "x": 1})
		we.require_no_state_move(_mock_doc("Draft"), {"x": 1})
		# An empty state is the docstatus's first state, so naming it is no move.
		we.require_no_state_move(_mock_doc(None), {"workflow_state": "Draft"})


# --------------------------------------------------------------------------- #
# A real Workflow
# --------------------------------------------------------------------------- #
def _ensure(doctype, name, **values):
	if not frappe.db.exists(doctype, name):
		frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)


def _user(email, *roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	frappe.get_doc("User", email).add_roles(*roles)


def _workflow(cancel_step=True):
	if frappe.db.exists("Workflow", WF):
		frappe.delete_doc("Workflow", WF, ignore_permissions=True, force=True)
	w = frappe.new_doc("Workflow")
	w.update(
		{
			"workflow_name": WF,
			"document_type": BT,
			"workflow_state_field": FIELD,
			"is_active": 1,
			"send_email_alert": 0,
		}
	)
	for state, status, edit in (
		("Draft", "0", EDITOR_ROLE),
		("Pending", "0", LEAD_ROLE),
		("Approved", "1", LEAD_ROLE),
		("Cancelled", "2", LEAD_ROLE),
	):
		w.append("states", {"state": state, "doc_status": status, "allow_edit": edit})
	transitions = [
		("Draft", "Review", "Pending", EDITOR_ROLE, 1),
		("Pending", "Approve", "Approved", APPROVER_ROLE, 0),
	]
	if cancel_step:
		transitions.append(("Approved", "Reject", "Cancelled", APPROVER_ROLE, 0))
	for state, action, next_state, allowed, self_ok in transitions:
		w.append(
			"transitions",
			{
				"state": state,
				"action": action,
				"next_state": next_state,
				"allowed": allowed,
				"allow_self_approval": self_ok,
			},
		)
	w.insert(ignore_permissions=True)
	frappe.clear_cache(doctype=BT)
	return w


class _RealWorkflow(FrappeTestCase):
	cancel_step = True

	def setUp(self):
		frappe.set_user("Administrator")
		commit = patch.object(frappe.db, "commit", lambda *a, **k: None)
		commit.start()
		self.addCleanup(commit.stop)
		self.addCleanup(self._rollback)
		for state in ("Draft", "Pending", "Approved", "Cancelled"):
			_ensure("Workflow State", state, workflow_state_name=state)
		for action in ("Review", "Approve", "Reject"):
			_ensure("Workflow Action Master", action, workflow_action_name=action)
		for role in (EDITOR_ROLE, APPROVER_ROLE, LEAD_ROLE):
			_ensure("Role", role, role_name=role, desk_access=1)
		_user(EDITOR, "Accounts Manager", EDITOR_ROLE)
		_user(APPROVER, "Accounts Manager", APPROVER_ROLE)
		self.company = frappe.get_all("Company", pluck="name", limit=1)[0]
		self.made = []
		_workflow(self.cancel_step)
		self.assertEqual(fw.get_workflow_name(BT), WF)

	def _rollback(self):
		from frappe.utils.file_lock import delete_lock

		frappe.set_user("Administrator")
		# A queued submission locks its document with a FILE, which no rollback undoes.
		for name in self.made:
			delete_lock(frappe.get_doc({"doctype": BT, "name": name}).get_signature())
		frappe.db.rollback()
		frappe.clear_cache(doctype=BT)

	def _make(self, state="Draft"):
		"""A Bank Transaction the EDITOR made, moved to ``state`` as Desk would."""
		frappe.set_user(EDITOR)
		doc = frappe.get_doc(
			{
				"doctype": BT,
				"date": frappe.utils.today(),
				"company": self.company,
				"deposit": 100,
				"description": "before",
			}
		).insert()
		self.made.append(doc.name)
		self.assertEqual(doc.get(FIELD), "Draft")
		if state in ("Pending", "Approved"):
			fw.apply_workflow(doc, "Review")
		if state == "Approved":
			frappe.set_user(APPROVER)
			fw.apply_workflow(frappe.get_doc(BT, doc.name), "Approve")
		frappe.set_user("Administrator")
		return doc.name

	def _row(self, name):
		return frappe.db.get_value(BT, name, [FIELD, "docstatus", "description", "party_type"], as_dict=True)

	@contextmanager
	def _tool_call(self, user):
		"""A Jarvis tool call as ``user``, rolled back to its savepoint on error, as
		``api._run_tool`` does."""
		frappe.set_user(user)
		frappe.db.savepoint("jarvis_2506")
		try:
			with wr.guard_scope([]):
				yield
		except Exception:
			frappe.db.rollback(save_point="jarvis_2506")
			raise
		finally:
			frappe.set_user("Administrator")

	def _client_save(self, name, **values):
		doc = frappe.get_doc(BT, name).as_dict()
		doc.update(values)
		return frappe.client.save(doc)


class TestApproverWhoIsNotAnEditor(_RealWorkflow):
	"""Abhas's case: every route of a transition, through Frappe's real code."""

	def test_apply_workflow_action_tool(self):
		from jarvis.tools.apply_workflow_action import apply_workflow_action

		name = self._make("Pending")
		with self._tool_call(APPROVER):
			apply_workflow_action(BT, name, "Approve")
		self.assertEqual((self._row(name)[FIELD], self._row(name).docstatus), ("Approved", 1))

	def test_bulk_approval_through_run_method(self):
		from jarvis.tools.run_method import run_method

		names = [self._make("Pending"), self._make("Pending")]
		with self._tool_call(APPROVER):
			run_method(
				"frappe.model.workflow.bulk_workflow_approval",
				{"docnames": json.dumps(names), "doctype": BT, "action": "Approve"},
			)
		for name in names:
			self.assertEqual((self._row(name)[FIELD], self._row(name).docstatus), ("Approved", 1), name)

	def test_run_workflow_transition_directly(self):
		# What _bulk_workflow_action and the emailed action's confirm_action call.
		name = self._make("Pending")
		with self._tool_call(APPROVER):
			fw.run_workflow_transition(frappe.get_doc(BT, name), "Approve")
		self.assertEqual(self._row(name)[FIELD], "Approved")

	def test_emailed_confirm_action(self):
		from frappe.workflow.doctype.workflow_action import workflow_action as wa

		name = self._make("Pending")
		with (
			patch.object(wa, "verify_request", return_value=True),
			patch.object(wa, "return_success_page"),
			self._tool_call(APPROVER),
		):
			wa.confirm_action(BT, name, APPROVER, "Approve")
		self.assertEqual(self._row(name)[FIELD], "Approved")

	def test_frappe_still_refuses_a_transition_the_user_lacks(self):
		name = self._make("Pending")
		with self.assertRaises(fw.WorkflowTransitionError), self._tool_call(EDITOR):
			fw.run_workflow_transition(frappe.get_doc(BT, name), "Approve")
		self.assertEqual(self._row(name)[FIELD], "Pending")


class TestEveryOtherWrite(_RealWorkflow):
	def test_edit_in_a_locked_state_is_refused_for_editor_and_approver(self):
		name = self._make("Pending")
		for user in (EDITOR, APPROVER):
			with self.assertRaises(PermissionDeniedError), self._tool_call(user):
				self._client_save(name, description="after")
		self.assertEqual(self._row(name).description, "before")

	def test_edit_in_an_editable_state_passes(self):
		name = self._make("Draft")
		with self._tool_call(EDITOR):
			self._client_save(name, description="after")
		self.assertEqual(self._row(name).description, "after")

	def test_moving_the_state_by_a_save_is_refused(self):
		name = self._make("Draft")
		with self.assertRaises(PermissionDeniedError) as cm, self._tool_call(EDITOR):
			self._client_save(name, **{FIELD: "Pending"})
		self.assertIn("only through one of the workflow's actions", str(cm.exception))
		self.assertEqual(self._row(name)[FIELD], "Draft")

	def test_moving_the_state_does_not_unlock_an_edit(self):
		name = self._make("Pending")
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			self._client_save(name, **{FIELD: "Draft", "description": "after"})
		self.assertEqual((self._row(name)[FIELD], self._row(name).description), ("Pending", "before"))

	def test_direct_submit_is_refused(self):
		# Frappe would set the first submitted state ("Approved") and skip the approval.
		name = self._make("Draft")
		for submit in (
			lambda: frappe.client.submit(frappe.get_doc(BT, name).as_dict()),
			lambda: frappe.get_doc(BT, name).submit(),
			lambda: self._client_save(name, docstatus=1),
		):
			with self.assertRaises(PermissionDeniedError) as cm, self._tool_call(EDITOR):
				submit()
			self.assertIn("submitted by one of the workflow's actions", str(cm.exception))
		self.assertEqual((self._row(name)[FIELD], self._row(name).docstatus), ("Draft", 0))

	def test_edit_after_submit_is_refused_even_under_ignore_validate(self):
		name = self._make("Approved")

		def quiet_save():
			doc = frappe.get_doc(BT, name)
			doc.party_type = "Customer"
			doc.flags.ignore_validate = True  # Frappe then skips before_update_after_submit
			doc.save()

		for edit in (lambda: self._client_save(name, party_type="Customer"), quiet_save):
			with self.assertRaises(PermissionDeniedError), self._tool_call(APPROVER):
				edit()
		self.assertEqual(self._row(name).party_type, None)

	def test_db_set_of_the_state_is_refused_other_fields_pass(self):
		name = self._make("Pending")
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			frappe.get_doc(BT, name).db_set(FIELD, "Approved")
		self.assertEqual(self._row(name)[FIELD], "Pending")
		with self._tool_call(EDITOR):
			frappe.get_doc(BT, name).db_set("transaction_type", "NEFT")  # a status-style write, as Desk
		self.assertEqual(frappe.db.get_value(BT, name, "transaction_type"), "NEFT")

	def test_a_swallowed_refusal_is_still_seen(self):
		name = self._make("Pending")
		wr.take_refusal()
		with self._tool_call(EDITOR):
			try:
				self._client_save(name, description="after")
			except PermissionDeniedError:
				pass  # a method that swallows it
			hidden = wr.take_refusal()
		self.assertIsInstance(hidden, PermissionDeniedError)

	def test_outside_a_tool_call_frappe_decides_as_before(self):
		name = self._make("Pending")
		frappe.set_user(EDITOR)
		self._client_save(name, description="after")
		frappe.set_user("Administrator")
		self.assertEqual(self._row(name).description, "after")

	def test_a_nested_save_passes(self):
		# A document's own code saving another document while it is saved, as from Desk.
		name = self._make("Pending")
		note = frappe.get_doc({"doctype": "Note", "title": "n2506", "public": 1}).insert(
			ignore_permissions=True
		)

		def on_update(n):
			other = frappe.get_doc(BT, name)
			other.description = "after"
			other.save(ignore_permissions=True)

		with patch.object(Note, "on_update", on_update, create=True), self._tool_call(EDITOR):
			n = frappe.get_doc("Note", note.name)
			n.content = "x"
			n.save(ignore_permissions=True)
		self.assertEqual(self._row(name).description, "after")

	def test_handler_is_hooked_on_every_event(self):
		star = frappe.get_hooks("doc_events")["*"]
		for event in (
			"before_validate",
			"before_update_after_submit",
			"on_update_after_submit",
			"before_cancel",
			"on_cancel",
			"before_change",
			"on_trash",
		):
			self.assertIn(HANDLER, star[event], event)

	def test_inserting_a_document_already_submitted_is_refused(self):
		# Frappe would stamp the first submitted state ("Approved") on insert.
		frappe.set_user(EDITOR)
		values = {"doctype": BT, "date": frappe.utils.today(), "company": self.company, "deposit": 5}
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			frappe.client.insert({**values, "docstatus": 1})
		self.assertEqual(frappe.db.count(BT, {"deposit": 5}), 0)
		with self._tool_call(EDITOR):
			name = frappe.client.insert(values)["name"]  # a draft starts in Draft, as in Desk
		self.made.append(name)
		self.assertEqual(self._row(name)[FIELD], "Draft")

	def test_a_child_row_of_a_locked_document_is_not_changed_on_its_own(self):
		name = self._make("Pending")
		parent = frappe.get_doc(BT, name)
		parent.append("payment_entries", {"payment_document": BT, "payment_entry": name})
		parent.db_update_all()  # a row put there outside any tool call
		row = frappe.get_doc(BT, name).payment_entries[0]
		edited = {**row.as_dict(), "allocated_amount": 50}
		for change in (
			lambda: frappe.client.save(edited),
			lambda: frappe.delete_doc(row.doctype, row.name),
			lambda: frappe.get_doc(
				{
					"doctype": row.doctype,
					"parenttype": BT,
					"parent": name,
					"parentfield": "payment_entries",
					"payment_document": BT,
					"payment_entry": self.made[0],
				}
			).insert(),
		):
			with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
				change()
		rows = frappe.get_all(
			row.doctype, filters={"parent": name}, fields=["allocated_amount", "payment_entry"]
		)
		self.assertEqual([(r.payment_entry, r.allocated_amount) for r in rows], [(name, 0)])


class TestUpdateDoc(_RealWorkflow):
	def test_locked_is_refused_before_anything_is_set(self):
		name = self._make("Pending")
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			update_doc(BT, name, {"description": "after"})
		self.assertEqual(self._row(name).description, "before")

	def test_a_state_move_is_refused(self):
		name = self._make("Draft")
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			update_doc(BT, name, {FIELD: "Approved"})
		self.assertEqual(self._row(name)[FIELD], "Draft")

	def test_batch_with_one_locked_document_saves_nothing(self):
		editable, locked = self._make("Draft"), self._make("Pending")
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			update_doc(
				BT,
				updates=[
					{"name": editable, "changes": {"description": "after"}},
					{"name": locked, "changes": {"description": "after"}},
				],
			)
		self.assertEqual(self._row(editable).description, "before")
		self.assertEqual(self._row(locked).description, "before")

	def test_editable_state_updates(self):
		name = self._make("Draft")
		with self._tool_call(EDITOR):
			update_doc(BT, name, {"description": "after"})
		self.assertEqual(self._row(name).description, "after")


class TestCancelWithACancelStep(_RealWorkflow):
	cancel_step = True

	def test_direct_cancel_is_refused_even_under_ignore_validate(self):
		name = self._make("Approved")

		def quiet_cancel():
			doc = frappe.get_doc(BT, name)
			doc.flags.ignore_validate = True  # Frappe then skips before_cancel
			doc.cancel()

		for cancel in (lambda: frappe.client.cancel(BT, name), quiet_cancel):
			with self.assertRaises(PermissionDeniedError) as cm, self._tool_call(APPROVER):
				cancel()
			self.assertIn("its own cancel action", str(cm.exception))
		self.assertEqual(self._row(name).docstatus, 1)

	def test_the_cancel_action_passes(self):
		name = self._make("Approved")
		with self._tool_call(APPROVER):
			fw.run_workflow_transition(frappe.get_doc(BT, name), "Reject")
		self.assertEqual((self._row(name)[FIELD], self._row(name).docstatus), ("Cancelled", 2))


class TestCancelWithoutACancelStep(_RealWorkflow):
	cancel_step = False

	def test_direct_cancel_passes_as_in_desk(self):
		name = self._make("Approved")
		with self._tool_call(APPROVER):
			frappe.client.cancel(BT, name)
		# Frappe runs no validate_workflow on a cancel, so the state field stays.
		self.assertEqual(self._row(name).docstatus, 2)


class TestSubmissionQueue(_RealWorkflow):
	"""A doctype set to "Queue in Background" submits through a Submission Queue job."""

	def setUp(self):
		super().setUp()
		# On the DocType (rolled back with the test), so every copy of its meta has it.
		frappe.db.set_value("DocType", BT, "queue_in_background", 1)
		frappe.clear_cache(doctype=BT)
		self.assertTrue(frappe.get_meta(BT).queue_in_background)
		for target in (
			"frappe.utils.scheduler.is_scheduler_inactive",
			"frappe.desk.form.save.is_scheduler_inactive",
		):
			p = patch(target, return_value=False)
			p.start()
			self.addCleanup(p.stop)

	def _entries(self, name):
		return frappe.get_all(
			"Submission Queue", filters={"ref_doctype": BT, "ref_docname": name}, pluck="name"
		)

	def test_queuing_a_direct_submit_is_refused(self):
		from frappe.desk.form.save import savedocs

		name = self._make("Draft")
		doc = frappe.get_doc(BT, name).as_dict()
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			savedocs(json.dumps(doc, default=str), "Submit")
		self.assertEqual(self._entries(name), [])

	def test_the_approval_queues_and_its_job_submits(self):
		name = self._make("Pending")
		with self._tool_call(APPROVER):
			fw.run_workflow_transition(frappe.get_doc(BT, name), "Approve")
		[entry_name] = self._entries(name)
		self.assertEqual(self._row(name).docstatus, 0)  # queued, not yet submitted
		# The worker side (Frappe's execute_action unlocks, then runs the job), inside
		# the guard scope the queued job carries.
		from frappe.model.document import execute_action

		queued = frappe.get_doc(BT, name)
		queued.set(FIELD, "Approved")
		with (
			patch("frappe.core.doctype.submission_queue.submission_queue.get_current_job") as job,
			self._tool_call(APPROVER),
		):
			job.return_value.id = "job-2506"
			execute_action(
				"Submission Queue",
				entry_name,
				"background_submission",
				to_be_queued_doc=queued,
				action_for_queuing="Submit",
			)
		self.assertEqual(frappe.db.get_value("Submission Queue", entry_name, "status"), "Finished")
		self.assertEqual((self._row(name)[FIELD], self._row(name).docstatus), ("Approved", 1))


class TestReviewRoutes(_RealWorkflow):
	"""Routes from the adversarial review of #2657, through run_method as a tool would."""

	cancel_step = False  # cancel allowed as in Desk: only the state and the payload are judged

	def _run(self, method, args=None, **kw):
		from jarvis.tools.run_method import run_method

		return run_method(method, args, **kw)

	def test_desk_cancel_cannot_set_another_state(self):
		name = self._make("Approved")
		with self.assertRaises(PermissionDeniedError), self._tool_call(APPROVER):
			self._run(
				"frappe.desk.form.save.cancel",
				{"doctype": BT, "name": name, "workflow_state_fieldname": FIELD, "workflow_state": "Draft"},
			)
		self.assertEqual((self._row(name)[FIELD], self._row(name).docstatus), ("Approved", 1))
		# Without run_method's pre-check, the document handler refuses it too.
		with self.assertRaises(PermissionDeniedError), self._tool_call(APPROVER):
			from frappe.desk.form.save import cancel

			cancel(BT, name, FIELD, "Draft")
		self.assertEqual(self._row(name).docstatus, 1)
		# What Desk sends (the current state) passes.
		with self._tool_call(APPROVER):
			self._run(
				"frappe.desk.form.save.cancel",
				{
					"doctype": BT,
					"name": name,
					"workflow_state_fieldname": FIELD,
					"workflow_state": "Approved",
				},
			)
		self.assertEqual(self._row(name).docstatus, 2)

	def test_a_cancel_cannot_carry_edits(self):
		name = self._make("Approved")
		payload = {**frappe.get_doc(BT, name).as_dict(), "docstatus": 2, "description": "after"}
		for method, args in (
			("frappe.client.save", {"doc": json.dumps(payload, default=str)}),
			("frappe.handler.run_doc_method", {"method": "cancel", "docs": json.dumps(payload, default=str)}),
		):
			with self.assertRaises(PermissionDeniedError) as cm, self._tool_call(APPROVER):
				self._run(method, args)
			self.assertIn("cannot also change description", str(cm.exception))
		self.assertEqual((self._row(name).docstatus, self._row(name).description), (1, "before"))

	def test_a_locked_draft_is_not_discarded(self):
		if not hasattr(frappe.model.document.Document, "discard"):
			self.skipTest("Discard arrived in Frappe 16")
		name = self._make("Pending")
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			self._run("frappe.desk.form.save.discard", {"doctype": BT, "name": name})
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			frappe.get_doc(BT, name).discard()
		self.assertEqual(self._row(name).docstatus, 0)

	def test_a_locked_document_is_not_renamed(self):
		name = self._make("Pending")
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			self._run("frappe.client.rename_doc", {"doctype": BT, "old_name": name, "new_name": name + "-x"})
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			frappe.get_doc(BT, name).run_method("before_rename", name, name + "-x", False)
		self.assertTrue(frappe.db.exists(BT, name))

	def test_a_stale_currently_saving_entry_does_not_skip_the_check(self):
		name = self._make("Pending")
		frappe.flags.currently_saving.append((BT, name))  # left by a save that failed
		self.addCleanup(
			lambda: (BT, name) in frappe.flags.currently_saving
			and frappe.flags.currently_saving.remove((BT, name))
		)
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			frappe.get_doc(BT, name).db_set(FIELD, "Approved")
		self.assertEqual(self._row(name)[FIELD], "Pending")

	def test_payload_edits_of_a_locked_document_are_refused_before_the_call(self):
		name = self._make("Pending")
		doc = {**frappe.get_doc(BT, name).as_dict(), "description": "after"}
		for method, args in (
			("frappe.client.save", {"doc": json.dumps(doc, default=str)}),
			(
				"frappe.client.set_value",
				{"doctype": BT, "name": name, "fieldname": "description", "value": "after"},
			),
			("frappe.handler.run_doc_method", {"method": "validate", "docs": json.dumps(doc, default=str)}),
		):
			with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
				self._run(method, args)
		self.assertEqual(self._row(name).description, "before")


class TestCancelIsJudgedBeforeItsHooks(_RealWorkflow):
	cancel_step = True

	def test_no_cancel_hook_runs_before_the_refusal(self):
		from erpnext.accounts.doctype.bank_transaction.bank_transaction import BankTransaction

		from jarvis.tools.run_method import run_method

		name = self._make("Approved")
		ran = []
		with (
			patch.object(BankTransaction, "before_cancel", lambda self: ran.append(self.name), create=True),
			self.assertRaises(PermissionDeniedError),
			self._tool_call(APPROVER),
		):
			run_method("frappe.client.cancel", {"doctype": BT, "name": name})
		self.assertEqual(ran, [])  # an e-invoice / e-way bill cancel would have run here
		self.assertEqual(self._row(name).docstatus, 1)


class TestThroughTheToolGate(_RealWorkflow):
	"""The whole Jarvis tool path (``api._run_tool``: parse, gate, card, savepoint,
	take_refusal, error envelope), and ``confirm_tool`` for a card."""

	def setUp(self):
		super().setUp()
		for user in (EDITOR, APPROVER):
			frappe.get_doc("User", user).add_roles("Jarvis User")

	def _as(self, user, tool, args):
		from jarvis import api

		frappe.set_user(user)
		try:
			return api._run_tool(tool, args)
		finally:
			frappe.set_user("Administrator")

	def _confirmed(self, user, tool, args):
		"""Park ``tool``'s card as ``user`` and confirm it as ``user``."""
		from jarvis import api
		from jarvis.chat import pending_confirm
		from jarvis.chat.actions_api import confirm_tool

		captured = {}
		real = pending_confirm.mint

		def spy(**kwargs):
			captured["token"] = real(**kwargs)
			return captured["token"]

		frappe.set_user(user)
		try:
			with patch("jarvis.chat.pending_confirm.mint", side_effect=spy):
				parked = api._run_tool(tool, args)
			if "token" not in captured:
				return parked  # refused before a card was parked
			self.assertEqual(parked["data"]["status"], "pending_confirmation", parked)
			return confirm_tool(captured["token"])
		finally:
			frappe.set_user("Administrator")

	def test_refused_edit_comes_back_as_a_permission_error_envelope(self):
		name = self._make("Pending")
		res = self._confirmed(
			EDITOR, "update_doc", {"doctype": BT, "name": name, "changes": {"description": "after"}}
		)
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"].get("code") or res["error"].get("type"), "PermissionDeniedError", res)
		self.assertIn("Pending", res["error"]["message"])
		self.assertEqual(self._row(name).description, "before")

	def test_approval_through_the_gate(self):
		name = self._make("Pending")
		res = self._confirmed(
			APPROVER, "apply_workflow_action", {"doctype": BT, "name": name, "action": "Approve"}
		)
		self.assertTrue(res["ok"], res)
		self.assertEqual((self._row(name)[FIELD], self._row(name).docstatus), ("Approved", 1))

	def test_bulk_approval_through_the_gate(self):
		names = [self._make("Pending") for _ in range(3)]
		res = self._confirmed(
			APPROVER,
			"run_method",
			{
				"method": "frappe.model.workflow.bulk_workflow_approval",
				"args": {"docnames": json.dumps(names), "doctype": BT, "action": "Approve"},
			},
		)
		self.assertTrue(res["ok"], res)
		for name in names:
			self.assertEqual(self._row(name)[FIELD], "Approved", name)

	def test_run_method_save_is_refused_and_nothing_sticks(self):
		name = self._make("Pending")
		doc = json.dumps({**frappe.get_doc(BT, name).as_dict(), "description": "after"}, default=str)
		res = self._confirmed(EDITOR, "run_method", {"method": "frappe.client.save", "args": {"doc": doc}})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"].get("code") or res["error"].get("type"), "PermissionDeniedError", res)
		self.assertEqual(self._row(name).description, "before")


class TestEdgeCases(_RealWorkflow):
	def test_two_approval_levels_with_different_approvers(self):
		w = frappe.get_doc("Workflow", WF)
		_ensure("Workflow State", "Checked", workflow_state_name="Checked")
		_ensure("Workflow Action Master", "Check", workflow_action_name="Check")
		w.append("states", {"state": "Checked", "doc_status": "0", "allow_edit": LEAD_ROLE})
		for t in w.transitions:
			if t.action == "Approve":
				t.state = "Checked"
		w.append(
			"transitions",
			{
				"state": "Pending",
				"action": "Check",
				"next_state": "Checked",
				"allowed": EDITOR_ROLE,
				"allow_self_approval": 1,
			},
		)
		w.save(ignore_permissions=True)
		frappe.clear_cache(doctype=BT)
		name = self._make("Pending")
		with self._tool_call(EDITOR):
			fw.run_workflow_transition(frappe.get_doc(BT, name), "Check")
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			self._client_save(name, description="after")  # Checked: only the lead edits
		with self._tool_call(APPROVER):
			fw.run_workflow_transition(frappe.get_doc(BT, name), "Approve")
		self.assertEqual((self._row(name)[FIELD], self._row(name).docstatus), ("Approved", 1))

	def test_a_record_from_before_the_workflow_has_an_empty_state(self):
		name = self._make("Draft")
		frappe.db.set_value(BT, name, FIELD, None)  # as if made before the workflow existed
		with self._tool_call(EDITOR):
			self._client_save(name, description="after")  # empty = Draft, which the editor edits
		self.assertEqual(self._row(name).description, "after")
		with self.assertRaises(PermissionDeniedError), self._tool_call(EDITOR):
			self._client_save(name, **{FIELD: "Approved"})

	def test_the_all_role_lets_every_user_edit(self):
		w = frappe.get_doc("Workflow", WF)
		for s in w.states:
			if s.state == "Pending":
				s.allow_edit = "All"
		w.save(ignore_permissions=True)
		frappe.clear_cache(doctype=BT)
		name = self._make("Pending")
		with self._tool_call(APPROVER):
			self._client_save(name, description="after")
		self.assertEqual(self._row(name).description, "after")

	def test_an_inactive_workflow_changes_nothing(self):
		name = self._make("Pending")
		frappe.db.set_value("Workflow", WF, "is_active", 0)
		frappe.clear_cache(doctype=BT)
		self.assertIsNone(fw.get_workflow_name(BT))
		with self._tool_call(EDITOR):
			self._client_save(name, description="after")
			frappe.client.submit(frappe.get_doc(BT, name).as_dict())
		self.assertEqual((self._row(name).description, self._row(name).docstatus), ("after", 1))

	def test_amend_after_a_cancel_starts_a_new_draft(self):
		name = self._make("Approved")
		with self._tool_call(APPROVER):
			fw.run_workflow_transition(frappe.get_doc(BT, name), "Reject")
		frappe.set_user(EDITOR)
		amended = frappe.copy_doc(frappe.get_doc(BT, name))
		amended.amended_from = name
		amended.set(FIELD, None)  # as Desk's amend leaves the state to the workflow
		amended.docstatus = 0  # Frappe 15's copy_doc keeps it; Desk's Amend starts a draft
		frappe.set_user("Administrator")
		with self._tool_call(EDITOR):
			amended.insert()
		self.made.append(amended.name)
		self.assertEqual(self._row(amended.name)[FIELD], "Draft")
		with self._tool_call(EDITOR):
			self._client_save(amended.name, description="amended")
			fw.run_workflow_transition(frappe.get_doc(BT, amended.name), "Review")
		self.assertEqual(self._row(amended.name)[FIELD], "Pending")

	def test_frappe_still_refuses_self_approval(self):
		name = self._make("Pending")
		frappe.get_doc("User", EDITOR).add_roles(APPROVER_ROLE)  # the editor made it
		with self.assertRaises(frappe.ValidationError), self._tool_call(EDITOR):
			fw.run_workflow_transition(frappe.get_doc(BT, name), "Approve")
		self.assertEqual(self._row(name)[FIELD], "Pending")

	def test_a_lead_edits_locked_states_including_after_submit(self):
		lead = "wf2506-lead@example.com"
		_user(lead, "Accounts Manager", LEAD_ROLE)
		pending, approved = self._make("Pending"), self._make("Approved")
		with self._tool_call(lead):
			update_doc(BT, pending, {"description": "lead"})
			self._client_save(approved, party_type="Customer")
		self.assertEqual(self._row(pending).description, "lead")
		self.assertEqual(self._row(approved).party_type, "Customer")

	def test_a_doctype_without_a_workflow_is_untouched(self):
		note = frappe.get_doc({"doctype": "Note", "title": "n2506b", "public": 1}).insert(
			ignore_permissions=True
		)
		with self._tool_call(EDITOR):
			n = frappe.get_doc("Note", note.name)
			n.content = "x"
			n.save(ignore_permissions=True)
			n.db_set("title", "renamed title")
		self.assertEqual(frappe.db.get_value("Note", note.name, "title"), "renamed title")

	def test_bulk_approval_of_25_goes_through_the_background_job(self):
		"""25 documents: Frappe enqueues _bulk_workflow_action. The job is caught as
		Jarvis's guarded queue hands it to rq, then run the way the worker runs it
		(execute_guarded_job, inside the carried guard scope), in this process."""
		from frappe.utils import background_jobs

		from jarvis.tools import _write_risk
		from jarvis.tools.run_method import run_method

		names = [self._make("Pending") for _ in range(25)]
		jobs = []

		def capture(self_q, func, args=None, kwargs=None, **options):
			jobs.append((func, args, kwargs))

		import rq

		with patch.object(rq.Queue, "enqueue_call", capture), self._tool_call(APPROVER):
			run_method(
				"frappe.model.workflow.bulk_workflow_approval",
				{"docnames": json.dumps(names), "doctype": BT, "action": "Approve"},
			)
		self.assertEqual(self._row(names[0]).docstatus, 0)  # queued, nothing yet
		[(func, args, kwargs)] = jobs
		self.assertEqual(func, _write_risk._GUARDED_EXECUTE_JOB)  # the guard rides with the job

		def in_process(site, method, event, job_name, kwargs, user=None, is_async=True, retry=0, **_):
			frappe.set_user(user or "Administrator")
			try:
				method = frappe.get_attr(method) if isinstance(method, str) else method
				method(**kwargs)
			finally:
				frappe.set_user("Administrator")

		with patch.object(background_jobs, "execute_job", in_process):
			target = frappe.get_attr(func) if isinstance(func, str) else func
			target(*(args or ()), **(kwargs or {}))
		for name in names:
			self.assertEqual((self._row(name)[FIELD], self._row(name).docstatus), ("Approved", 1), name)
