"""run_method follows the site's whitelisted-method overrides, and a call that
deletes or cancels is braked like the delete / cancel tools.

Nothing is committed: overrides are injected through the real hook lookup, the
brake is a pure decision on the tool args (the gate that uses it is covered in
test_skill_approve_and_run.TestSkillAutorunGate). The ORM backstop is exercised
inside a guard scope directly, and everything it touches rolls back.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.exceptions import BrakeRefusedError, PermissionDeniedError
from jarvis.tools import _write_risk
from jarvis.tools import run_method as run_method_mod
from jarvis.tools.run_method import needs_brake, run_method


def _overrides(mapping: dict):
	"""Serve ``mapping`` as the override_whitelisted_methods hook, the way a
	site app's hooks.py would, leaving every other hook untouched."""
	real = frappe.get_hooks

	def fake(hook=None, *args, **kwargs):
		if hook == "override_whitelisted_methods":
			return {k: [v] for k, v in mapping.items()}
		return real(hook, *args, **kwargs)

	return patch("frappe.get_hooks", side_effect=fake)


class TestRunMethodFollowsOverrides(FrappeTestCase):
	def test_the_overriding_method_runs_not_the_original(self):
		with _overrides({"frappe.ping": "frappe.auth.get_logged_user"}):
			self.assertEqual(run_method("frappe.ping"), frappe.session.user)
		self.assertEqual(run_method("frappe.ping"), "pong")

	def test_an_override_into_a_denied_endpoint_is_refused(self):
		with _overrides({"frappe.ping": "jarvis.api.call_tool"}), self.assertRaises(PermissionDeniedError):
			run_method("frappe.ping")

	def test_the_blocklist_applies_to_the_override_too(self):
		with (
			_overrides({"frappe.ping": "frappe.auth.get_logged_user"}),
			patch.object(run_method_mod, "_blocklist", return_value=("frappe.auth.*",)),
			self.assertRaises(PermissionDeniedError),
		):
			run_method("frappe.ping")


class TestRunMethodBrake(FrappeTestCase):
	def test_delete_and_cancel_calls_are_braked(self):
		for tool_args in (
			{"method": "frappe.client.delete", "args": {"doctype": "ToDo", "name": "x"}},
			{"method": "frappe.client.cancel", "args": {"doctype": "ToDo", "name": "x"}},
			{"method": "frappe.desk.form.save.cancel", "args": {"doctype": "ToDo", "name": "x"}},
			{"method": "frappe.desk.reportview.delete_items"},
			{"method": "frappe.desk.form.save.savedocs", "args": {"doc": "{}", "action": "Cancel"}},
			{
				"method": "frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs",
				"args": {"doctype": "ToDo", "docnames": ["x"], "action": "delete"},
			},
			{"method": "cancel", "doctype": "ToDo", "name": "x"},
			{"method": "discard", "doctype": "ToDo", "name": "x"},
		):
			with self.subTest(tool_args=tool_args):
				self.assertTrue(needs_brake(tool_args))

	def test_other_calls_are_not_braked(self):
		for tool_args in (
			{"method": "frappe.ping"},
			{"method": "frappe.client.get_value", "args": {"doctype": "ToDo"}},
			{"method": "frappe.desk.form.save.savedocs", "args": {"doc": "{}", "action": "Submit"}},
			{"method": "submit", "doctype": "ToDo", "name": "x"},
			{"method": "no.such.module.fn"},
			{},
			None,
		):
			with self.subTest(tool_args=tool_args):
				self.assertFalse(needs_brake(tool_args))

	def test_an_override_into_delete_is_braked(self):
		with _overrides({"frappe.ping": "frappe.client.delete"}):
			self.assertTrue(needs_brake({"method": "frappe.ping"}))


class TestUncardedRunMethodCannotDeleteOrCancel(FrappeTestCase):
	"""The backstop for what a name list can't see: inside a run_method that runs
	without a card, no route may delete or cancel a root document."""

	def setUp(self):
		self.todo_meta = frappe.get_meta("ToDo")

	def _submitted_todo(self):
		with patch.object(self.todo_meta, "is_submittable", 1):
			todo = frappe.get_doc({"doctype": "ToDo", "description": "brake probe"}).insert(
				ignore_permissions=True
			)
			todo.submit()
		return todo

	def test_cancel_by_saving_docstatus_2_is_refused(self):
		todo = self._submitted_todo()
		payload = {**todo.as_dict(), "docstatus": 2}
		with (
			patch.object(self.todo_meta, "is_submittable", 1),
			_write_risk.guard_scope([], brake=True),
			self.assertRaises(BrakeRefusedError),
		):
			frappe.call("frappe.client.save", doc=payload)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "docstatus"), 1)

	def test_the_brake_runs_before_the_documents_own_cancel_handler(self):
		# Nothing a cancel handler does outside the database (an e-invoice cancel,
		# a mail) may happen before the refusal.
		todo = self._submitted_todo()
		handler = MagicMock()
		with (
			patch.object(type(todo), "before_cancel", handler, create=True),
			patch.object(self.todo_meta, "is_submittable", 1),
			_write_risk.guard_scope([], brake=True),
			self.assertRaises(BrakeRefusedError),
		):
			todo.cancel()
		handler.assert_not_called()

	def test_cancel_by_db_set_is_refused(self):
		todo = self._submitted_todo()
		with _write_risk.guard_scope([], brake=True), self.assertRaises(BrakeRefusedError):
			todo.db_set("docstatus", 2)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "docstatus"), 1)

	def test_delete_is_refused(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "brake probe"}).insert(
			ignore_permissions=True
		)
		with _write_risk.guard_scope([], brake=True), self.assertRaises(BrakeRefusedError):
			frappe.delete_doc("ToDo", todo.name, ignore_permissions=True)
		self.assertTrue(frappe.db.exists("ToDo", todo.name))

	def test_discard_is_refused(self):
		if not hasattr(Document, "discard"):
			self.skipTest("this Frappe version has no discard")
		with patch.object(self.todo_meta, "is_submittable", 1):
			todo = frappe.get_doc({"doctype": "ToDo", "description": "brake probe"}).insert(
				ignore_permissions=True
			)
			with _write_risk.guard_scope([], brake=True), self.assertRaises(BrakeRefusedError):
				todo.discard()
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "docstatus"), 0)

	def test_without_the_brake_the_same_delete_runs(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "brake probe"}).insert(
			ignore_permissions=True
		)
		with _write_risk.guard_scope([]):
			frappe.delete_doc("ToDo", todo.name, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("ToDo", todo.name))

	def test_a_cancel_nested_in_another_save_is_refused_inside_a_run_method(self):
		# A method can't hide the cancel inside a harmless save: the nesting only
		# exempts another document's own cascade, and only outside a run_method.
		victim = self._submitted_todo()
		with patch.object(self.todo_meta, "is_submittable", 1):
			carrier = frappe.get_doc({"doctype": "ToDo", "description": "carrier"}).insert(
				ignore_permissions=True
			)

			real_validate = type(carrier).validate

			def cancel_victim(doc, *args, **kwargs):
				real_validate(doc, *args, **kwargs)
				frappe.get_doc("ToDo", victim.name).cancel()

			with patch.object(type(carrier), "validate", cancel_victim):
				with (
					_write_risk.guard_scope([], brake=True, brake_nested=True),
					self.assertRaises(BrakeRefusedError),
				):
					carrier.save()
				self.assertEqual(frappe.db.get_value("ToDo", victim.name, "docstatus"), 1)
				with _write_risk.guard_scope([], brake=True):
					frappe.get_doc("ToDo", carrier.name).save()  # the save's own cascade, as from Desk
		self.assertEqual(frappe.db.get_value("ToDo", victim.name, "docstatus"), 2)

	def test_a_root_document_posing_as_a_child_row_is_still_braked(self):
		todo = self._submitted_todo()
		payload = {**todo.as_dict(), "docstatus": 2, "parenttype": "ToDo", "parentfield": "x"}
		with (
			patch.object(self.todo_meta, "is_submittable", 1),
			_write_risk.guard_scope([], brake=True),
			self.assertRaises(BrakeRefusedError),
		):
			frappe.call("frappe.client.save", doc=payload)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "docstatus"), 1)

	def test_db_set_with_a_string_docstatus_is_refused(self):
		todo = self._submitted_todo()
		with _write_risk.guard_scope([], brake=True), self.assertRaises(BrakeRefusedError):
			todo.db_set("docstatus", "2")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "docstatus"), 1)

	def test_a_raw_set_value_to_docstatus_2_is_refused(self):
		todo = self._submitted_todo()
		for field, value in (
			("docstatus", 2),
			({"docstatus": 2}, None),
			("DOCSTATUS", "2 "),
			("`docstatus`", 2.0),
			({"DocStatus": "2"}, None),
		):
			with (
				self.subTest(field=field),
				_write_risk.guard_scope([], brake=True),
				self.assertRaises(BrakeRefusedError),
			):
				frappe.db.set_value("ToDo", todo.name, field, value)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "docstatus"), 1)

	def test_a_queued_job_carries_the_brake(self):
		with _write_risk.guard_scope([], brake=True, brake_nested=True):
			queue = _write_risk.guard_queue(MagicMock())
		_, _, queue_args = queue._guard("frappe.ping", (), {})
		self.assertIs(queue_args["_jarvis_brake"], True)
		self.assertIs(queue_args["_jarvis_brake_nested"], True)

	def test_every_uncarded_write_gets_the_brake_and_run_method_the_nested_one(self):
		seen = {}

		def capture(tool, *args, **kwargs):
			state = _write_risk._active_state()
			seen[tool] = (state.brake, state.brake_nested)
			return {"ok": True, "data": {}}

		with patch.object(api, "_dispatch_and_wrap", side_effect=capture):
			api.dispatch_confirmed("run_method", {"method": "frappe.ping"}, uncarded=True)
			self.assertEqual(seen.pop("run_method"), (True, True))
			api.dispatch_confirmed(
				"apply_workflow_action", {"doctype": "ToDo", "name": "x", "action": "Cancel"}, uncarded=True
			)
			self.assertEqual(seen.pop("apply_workflow_action"), (True, False))
			api.dispatch_confirmed("run_method", {"method": "frappe.ping"})
			self.assertEqual(seen.pop("run_method"), (False, False))
