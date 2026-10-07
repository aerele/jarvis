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
		self.assertEqual(s, {"kind": "workflow", "action": "Approve"})

	def test_negative_actions_are_skipped(self):
		s = self._suggest(workflow="WF", actions=["Reject", "Cancel Request", "Approve"])
		self.assertEqual(s["action"], "Approve")

	def test_only_negative_actions_offer_nothing(self):
		self.assertIsNone(self._suggest(workflow="WF", actions=["Reject", "Withdraw", "Decline"]))

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

		def _remove():
			# A submitted record cannot be deleted until it is cancelled.
			if frappe.db.get_value(doctype, r["name"], "docstatus") == 1:
				frappe.get_doc(doctype, r["name"]).cancel()
			frappe.delete_doc(doctype, r["name"], force=True, ignore_permissions=True)

		self.addCleanup(_remove)
		return conv, r

	def test_suggestion_returned_and_persisted_on_receipt(self):
		sug = {"kind": "submit", "action": None}
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

	def _receipt(self, conv, doctype="Sales Order", name="SO-1", outcome="confirmed"):
		receipt = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": conv,
				"seq": 1 + frappe.db.count("Jarvis Chat Message", {"conversation": conv}),
				"role": "tool",
				"streaming": 0,
				"tool_name": "create_doc",
				"tool_args": "{}",
				"tool_result": frappe.as_json({"ok": True, "data": {"doctype": doctype, "name": name}}),
				"tool_status": "completed",
				"action_outcome": outcome,
			}
		)
		# tool_status / action_outcome / tool_args are server-owned fields.
		receipt.flags.jarvis_server_write = True
		receipt.insert(ignore_permissions=True)

	def _propose(self, *a, sug=None, run=None, perm=True, **kw):
		with (
			patch.object(actions_api, "_suggest_next", return_value=sug),
			patch("frappe.db.exists", return_value=True),
			patch("frappe.has_permission", return_value=perm),
			patch("jarvis.api._run_tool", return_value={"ok": True}) as rt,
		):
			out = propose_next_action(*a, **kw)
		return out, rt

	def test_submit_parks_the_normal_card(self):
		self._receipt(self.conv)
		_, run = self._propose(
			self.conv, "Sales Order", "SO-1", "submit", sug={"kind": "submit", "action": None}
		)
		run.assert_called_once_with(
			"submit_doc", {"doctype": "Sales Order", "name": "SO-1"}, conversation=self.conv
		)

	def test_workflow_parks_apply_workflow_action(self):
		self._receipt(self.conv, "Leave Application", "LA-1")
		_, run = self._propose(
			self.conv,
			"Leave Application",
			"LA-1",
			"workflow",
			"Approve",
			sug={"kind": "workflow", "action": "Approve"},
		)
		run.assert_called_once_with(
			"apply_workflow_action",
			{"doctype": "Leave Application", "name": "LA-1", "action": "Approve"},
			conversation=self.conv,
		)

	def test_stale_step_is_refused(self):
		self._receipt(self.conv)
		with self.assertRaises(InvalidArgumentError):
			self._propose(self.conv, "Sales Order", "SO-1", "submit", sug=None)

	def test_a_different_action_than_suggested_is_refused(self):
		self._receipt(self.conv, "Leave Application", "LA-1")
		with self.assertRaises(InvalidArgumentError):
			self._propose(
				self.conv,
				"Leave Application",
				"LA-1",
				"workflow",
				"Reject",
				sug={"kind": "workflow", "action": "Approve"},
			)

	def test_record_not_created_in_this_conversation_is_refused(self):
		with self.assertRaises(InvalidArgumentError):
			self._propose(self.conv, "Sales Order", "SO-9", "submit", sug={"kind": "submit", "action": None})

	def test_unconfirmed_create_receipt_does_not_count(self):
		self._receipt(self.conv, outcome="discarded")
		with self.assertRaises(InvalidArgumentError):
			self._propose(self.conv, "Sales Order", "SO-1", "submit", sug={"kind": "submit", "action": None})

	def test_another_users_conversation_is_refused(self):
		self._receipt(self.conv)
		with patch.object(actions_api, "_owns_conversation", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				self._propose(
					self.conv, "Sales Order", "SO-1", "submit", sug={"kind": "submit", "action": None}
				)

	def test_unreadable_and_missing_records_read_the_same(self):
		self._receipt(self.conv)
		sug = {"kind": "submit", "action": None}
		with self.assertRaises(InvalidArgumentError) as unreadable:
			self._propose(self.conv, "Sales Order", "SO-1", "submit", sug=sug, perm=False)
		with self.assertRaises(InvalidArgumentError) as missing:
			self._propose(self.conv, "Sales Order", "SO-404", "submit", sug=sug)
		self.assertEqual(str(unreadable.exception), str(missing.exception))

	def test_unknown_doctype_is_refused_cleanly(self):
		with self.assertRaises(InvalidArgumentError):
			propose_next_action(self.conv, "No Such DocType", "X-1", "submit")

	def test_auto_mode_still_parks_and_never_runs(self):
		"""The force-card flag is set while the tool runs, so every uncarded branch
		(auto mode, armed macro, autorun) steps aside."""
		self._receipt(self.conv)
		frappe.db.set_value("Jarvis Conversation", self.conv, "auto_mode", 1)
		seen = {}

		def fake_run(tool, args, conversation=None):
			seen["flag"] = frappe.flags.get("jarvis_force_card")
			return {"ok": True, "data": {"status": "pending_confirmation"}}

		self._receipt(self.conv, "ToDo", "TD-1")
		with (
			patch.object(actions_api, "_suggest_next", return_value={"kind": "submit", "action": None}),
			patch("frappe.has_permission", return_value=True),
			patch("jarvis.api._run_tool", side_effect=fake_run),
		):
			out = propose_next_action(self.conv, "ToDo", "TD-1", "submit")
		self.assertTrue(seen["flag"])
		self.assertEqual(out["data"]["status"], "pending_confirmation")
		self.assertIsNone(frappe.flags.get("jarvis_force_card"))

	def test_force_card_overrides_auto_mode_in_the_gate(self):
		"""Through the real gate: auto mode on, submit_doc parks and nothing is submitted."""
		from jarvis import api

		frappe.db.set_value("Jarvis Conversation", self.conv, "auto_mode", 1)
		frappe.flags["jarvis_force_card"] = True
		self.addCleanup(lambda: frappe.flags.pop("jarvis_force_card", None))
		with patch("jarvis.tools.submit_doc.submit_doc") as submit:
			res = api._run_tool("submit_doc", {"doctype": "ToDo", "name": "nope"}, conversation=self.conv)
		submit.assert_not_called()
		self.assertNotEqual((res.get("data") or {}).get("status"), "applied")
