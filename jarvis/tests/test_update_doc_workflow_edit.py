"""aerele/jarvis#2506: update_doc honours a workflow state's edit roles, as Desk does.

The helper is checked against Desk's ``frappe.workflow.is_read_only`` rules with a
mocked Workflow (no Workflow insert: that adds a custom field and a column). The
update_doc tests run on real ``Note`` rows, with the Note's ``title`` standing in as
the workflow-state field, so the save path, the batch savepoint and the
"nothing changed" guarantee are exercised for real.
"""

from unittest.mock import patch

import frappe
from frappe.desk.doctype.note.note import Note
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import PermissionDeniedError
from jarvis.tools import _workflow_edit as we
from jarvis.tools import _write_risk as wr
from jarvis.tools.update_doc import update_doc

NOTE = "Note"


def _workflow(state_field="workflow_state"):
	return frappe._dict(
		workflow_state_field=state_field,
		states=[
			frappe._dict(state="Draft", doc_status="0", allow_edit="Sales User"),
			frappe._dict(state="Pending Approval", doc_status="0", allow_edit="Sales Manager"),
			frappe._dict(state="Pending Approval", doc_status="0", allow_edit="Accounts Manager"),
			frappe._dict(state="Approved", doc_status="1", allow_edit="Sales Manager"),
		],
	)


def _doc(state=None, docstatus=0, new=False, doctype="Sales Order"):
	d = frappe._dict(doctype=doctype, name="SO-1", docstatus=docstatus, workflow_state=state)
	d.is_new = lambda: new
	return d


class _Patched(FrappeTestCase):
	roles = ["Sales User"]
	has_workflow = True

	def setUp(self):
		for p in (
			patch.object(we, "get_workflow_name", side_effect=lambda dt: "WF" if self.has_workflow else None),
			patch.object(we, "get_workflow", side_effect=lambda dt: self.workflow),
			patch.object(we.frappe, "get_roles", side_effect=lambda user=None: list(self.roles)),
		):
			p.start()
			self.addCleanup(p.stop)
		self.workflow = _workflow()


class TestLockedState(_Patched):
	def test_locked_when_user_lacks_every_edit_role(self):
		self.assertEqual(
			we.locked_state(_doc("Pending Approval")),
			("Pending Approval", ["Accounts Manager", "Sales Manager"]),
		)

	def test_any_listed_role_unlocks(self):
		self.roles = ["Sales User", "Accounts Manager"]
		self.assertIsNone(we.locked_state(_doc("Pending Approval")))

	def test_editable_state_is_not_locked(self):
		self.assertIsNone(we.locked_state(_doc("Draft")))

	def test_no_active_workflow_is_never_locked(self):
		self.has_workflow = False
		self.assertIsNone(we.locked_state(_doc("Pending Approval")))

	def test_new_document_is_never_locked(self):
		self.assertIsNone(we.locked_state(_doc("Pending Approval", new=True)))

	def test_empty_state_uses_the_first_state_of_its_docstatus(self):
		# Desk's get_default_state: docstatus 0 -> Draft (editable), 1 -> Approved (locked).
		self.assertIsNone(we.locked_state(_doc(None, docstatus=0)))
		self.assertEqual(we.locked_state(_doc(None, docstatus=1)), ("Approved", ["Sales Manager"]))

	def test_state_without_edit_roles_is_read_only_for_everyone(self):
		self.workflow.states.append(frappe._dict(state="Frozen", doc_status="0", allow_edit=""))
		self.roles = ["Sales Manager", "Accounts Manager", "Sales User"]
		self.assertEqual(we.locked_state(_doc("Frozen")), ("Frozen", []))

	def test_refusal_names_the_state_and_roles(self):
		with self.assertRaises(PermissionDeniedError) as cm:
			we.require_workflow_edit(_doc("Pending Approval"))
		msg = str(cm.exception)
		self.assertIn("Pending Approval", msg)
		self.assertIn("Accounts Manager or Sales Manager", msg)
		self.assertIn("apply_workflow_action", msg)


class _NoteRows(_Patched):
	"""Real Note rows; the Note's title plays the workflow-state field."""

	def setUp(self):
		super().setUp()
		self.workflow = _workflow(state_field="title")
		self.names = []
		for title in ("Pending Approval", "Draft"):
			n = frappe.get_doc({"doctype": NOTE, "title": title, "content": "before", "public": 1})
			n.insert(ignore_permissions=True)
			self.names.append(n.name)
		self.locked, self.editable = self.names
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		for n in self.names:
			frappe.delete_doc(NOTE, n, ignore_permissions=True, force=True)

	def _content(self, name):
		return frappe.db.get_value(NOTE, name, "content")


class TestUpdateDocHonoursWorkflowEdit(_NoteRows):
	def test_single_update_in_a_locked_state_is_refused_and_unchanged(self):
		with self.assertRaises(PermissionDeniedError):
			update_doc(NOTE, self.locked, {"content": "after"})
		self.assertEqual(self._content(self.locked), "before")

	def test_moving_the_state_field_does_not_unlock_it(self):
		with self.assertRaises(PermissionDeniedError):
			update_doc(NOTE, self.locked, {"title": "Draft", "content": "after"})
		self.assertEqual(self._content(self.locked), "before")

	def test_batch_with_one_locked_document_saves_nothing(self):
		with self.assertRaises(PermissionDeniedError):
			update_doc(
				NOTE,
				updates=[
					{"name": self.editable, "changes": {"content": "after"}},
					{"name": self.locked, "changes": {"content": "after"}},
				],
			)
		self.assertEqual(self._content(self.editable), "before")
		self.assertEqual(self._content(self.locked), "before")

	def test_editable_state_still_updates(self):
		update_doc(NOTE, self.editable, {"content": "after"})
		self.assertEqual(self._content(self.editable), "after")

	def test_a_user_with_an_edit_role_updates_the_locked_state(self):
		self.roles = ["Sales Manager"]
		update_doc(NOTE, self.locked, {"content": "after"})
		self.assertEqual(self._content(self.locked), "after")

	def test_doctype_without_workflow_is_untouched(self):
		self.has_workflow = False
		update_doc(NOTE, self.locked, {"content": "after"})
		self.assertEqual(self._content(self.locked), "after")


class TestWorkflowLockOnEverySave(_NoteRows):
	"""The ORM side: any root save a Jarvis tool call makes (here ``frappe.client.save``,
	what ``run_method`` reaches) honours the lock; Desk-like saves outside a tool call,
	nested saves and saves under ``apply_workflow`` do not change."""

	def _client_save(self, name, **values):
		doc = frappe.get_doc(NOTE, name).as_dict()
		doc.update(values)
		return frappe.client.save(doc)

	def _in_tool_call(self, fn, *args, **kwargs):
		with wr.guard_scope([]):
			return fn(*args, **kwargs)

	def test_handler_is_hooked_on_both_save_events(self):
		star = frappe.get_hooks("doc_events")["*"]
		for event in ("before_validate", "before_update_after_submit"):
			self.assertIn("jarvis.tools._workflow_edit.guard_doc_event", star[event])

	def test_client_save_in_a_tool_call_is_refused_and_unchanged(self):
		with self.assertRaises(PermissionDeniedError):
			self._in_tool_call(self._client_save, self.locked, content="after")
		self.assertEqual(self._content(self.locked), "before")

	def test_moving_the_state_in_the_same_save_does_not_unlock_it(self):
		with self.assertRaises(PermissionDeniedError):
			self._in_tool_call(self._client_save, self.locked, title="Draft", content="after")
		self.assertEqual(self._content(self.locked), "before")

	def test_editable_state_saves_in_a_tool_call(self):
		self._in_tool_call(self._client_save, self.editable, content="after")
		self.assertEqual(self._content(self.editable), "after")

	def test_a_user_with_an_edit_role_saves_in_a_tool_call(self):
		self.roles = ["Sales Manager"]
		self._in_tool_call(self._client_save, self.locked, content="after")
		self.assertEqual(self._content(self.locked), "after")

	def test_outside_a_tool_call_nothing_changes(self):
		# Desk, REST and scheduler saves: Frappe's own rules only, as before.
		self._client_save(self.locked, content="after")
		self.assertEqual(self._content(self.locked), "after")

	def test_save_under_apply_workflow_passes(self):
		# A role allowed a transition need not be one allowed to edit: Frappe's
		# apply_workflow has checked the transition and its role.
		from frappe.model import workflow

		def fake_apply_workflow(doc, action):
			doc = frappe.get_doc(NOTE, doc)
			doc.content = "after"
			doc.save()

		with patch.object(workflow, "apply_workflow", fake_apply_workflow):
			self._in_tool_call(workflow.apply_workflow, self.locked, "Approve")
		self.assertEqual(self._content(self.locked), "after")

	def test_nested_save_passes(self):
		# A document's own code saving another document while it is saved, as from Desk.
		locked = self.locked

		def on_update(note):
			if note.name != locked:
				other = frappe.get_doc(NOTE, locked)
				other.content = "after"
				other.save()

		with patch.object(Note, "on_update", on_update, create=True):
			self._in_tool_call(self._client_save, self.editable, content="after")
		self.assertEqual(self._content(self.locked), "after")
