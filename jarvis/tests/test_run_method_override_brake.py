"""run_method follows the site's whitelisted-method overrides, and a call that
deletes or cancels is braked like the delete / cancel tools.

No database writes: overrides are injected through the real hook lookup, and the
brake is a pure decision on the tool args (the gate that uses it is covered in
test_skill_approve_and_run.TestSkillAutorunGate).
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import PermissionDeniedError
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
