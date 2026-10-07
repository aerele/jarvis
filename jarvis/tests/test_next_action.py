"""Tests for the next-step suggestion after a draft is created (#621).

``_suggest_next`` decides the one button the receipt chip offers; ``apply_action``
returns and persists it; ``propose_next_action`` parks the normal confirm card.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import actions_api
from jarvis.chat.actions_api import _suggest_next, apply_action, propose_next_action
from jarvis.exceptions import InvalidArgumentError
from jarvis.tests.test_actions_api import _make_conversation

GWT = "jarvis.tools.get_workflow_transitions.get_workflow_transitions"
WFN = "frappe.model.workflow.get_workflow_name"


def _meta(submittable):
	return frappe._dict(is_submittable=submittable)


class TestSuggestNext(FrappeTestCase):
	def _suggest(self, *, docstatus=0, workflow="", actions=(), submittable=True, can_submit=True):
		with (
			patch("frappe.db.get_value", return_value=docstatus),
			patch(WFN, return_value=workflow),
			patch(GWT, return_value={"available_actions": [{"action": a} for a in actions]}),
			patch("frappe.get_meta", return_value=_meta(submittable)),
			patch("frappe.has_permission", return_value=can_submit),
		):
			return _suggest_next("Sales Order", "SO-1")

	def test_submit_when_submittable_and_permitted(self):
		self.assertEqual(self._suggest()["kind"], "submit")

	def test_none_without_submit_permission(self):
		self.assertIsNone(self._suggest(can_submit=False))

	def test_none_for_non_submittable(self):
		self.assertIsNone(self._suggest(submittable=False))

	def test_none_once_submitted(self):
		self.assertIsNone(self._suggest(docstatus=1))

	def test_first_permitted_workflow_action(self):
		s = self._suggest(workflow="WF", actions=["Approve", "Reject"])
		self.assertEqual(s, {"kind": "workflow", "action": "Approve", "label": "Approve"})

	def test_workflow_without_permitted_action_offers_nothing_not_submit(self):
		self.assertIsNone(self._suggest(workflow="WF", actions=[]))


class TestApplyActionSuggestion(FrappeTestCase):
	def _create(self, doctype, values, **extra):
		conv = _make_conversation()
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Conversation", conv, force=True, ignore_permissions=True)
		)
		r = apply_action(
			frappe.as_json(
				{"verb": "create", "doctype": doctype, "values": values, "conversation": conv, **extra}
			)
		)
		self.addCleanup(lambda: frappe.delete_doc(doctype, r["name"], force=True, ignore_permissions=True))
		return conv, r

	def test_suggestion_returned_and_persisted_on_receipt(self):
		sug = {"kind": "submit", "action": None, "label": "Submit"}
		with patch.object(actions_api, "_suggest_next", return_value=sug):
			conv, r = self._create("ToDo", {"description": "next step"})
		self.assertEqual(r["suggested_next"], sug)
		row = frappe.get_all(
			"Jarvis Chat Message", {"conversation": conv, "role": "tool"}, ["tool_result"], limit=1
		)[0]
		self.assertEqual(frappe.parse_json(row.tool_result)["data"]["suggested_next"], sug)

	def test_non_submittable_create_has_no_suggestion(self):
		_, r = self._create("ToDo", {"description": "plain"})
		self.assertNotIn("suggested_next", r)

	def test_create_and_submit_has_no_suggestion(self):
		with patch.object(actions_api, "_suggest_next") as sn:
			from frappe.core.doctype.doctype.test_doctype import new_doctype

			dt = new_doctype(custom=1, is_submittable=1).insert()
			self.addCleanup(
				lambda: frappe.delete_doc("DocType", dt.name, force=True, ignore_permissions=True)
			)
			_, r = self._create(dt.name, {"some_fieldname": "x"}, submit=1)
		sn.assert_not_called()
		self.assertNotIn("suggested_next", r)


class TestProposeNextAction(FrappeTestCase):
	def setUp(self):
		self.conv = _make_conversation()
		self.addCleanup(
			lambda: frappe.delete_doc("Jarvis Conversation", self.conv, force=True, ignore_permissions=True)
		)

	def test_submit_parks_the_normal_card(self):
		sug = {"kind": "submit", "action": None, "label": "Submit"}
		with (
			patch.object(actions_api, "_suggest_next", return_value=sug),
			patch("jarvis.api._run_tool", return_value={"ok": True}) as run,
		):
			propose_next_action(self.conv, "Sales Order", "SO-1", "submit")
		run.assert_called_once_with(
			"submit_doc", {"doctype": "Sales Order", "name": "SO-1"}, conversation=self.conv
		)

	def test_workflow_parks_apply_workflow_action(self):
		sug = {"kind": "workflow", "action": "Approve", "label": "Approve"}
		with (
			patch.object(actions_api, "_suggest_next", return_value=sug),
			patch("jarvis.api._run_tool", return_value={"ok": True}) as run,
		):
			propose_next_action(self.conv, "Leave Application", "LA-1", "workflow", "Approve")
		run.assert_called_once_with(
			"apply_workflow_action",
			{"doctype": "Leave Application", "name": "LA-1", "action": "Approve"},
			conversation=self.conv,
		)

	def test_stale_or_unpermitted_step_is_refused(self):
		with patch.object(actions_api, "_suggest_next", return_value=None):
			with self.assertRaises(InvalidArgumentError):
				propose_next_action(self.conv, "Sales Order", "SO-1", "submit")

	def test_a_different_action_than_suggested_is_refused(self):
		sug = {"kind": "workflow", "action": "Approve", "label": "Approve"}
		with patch.object(actions_api, "_suggest_next", return_value=sug):
			with self.assertRaises(InvalidArgumentError):
				propose_next_action(self.conv, "Leave Application", "LA-1", "workflow", "Reject")
