"""Tests for the Workflow tools + the workflow-awareness guards.

Covers get_workflow_transitions (read) and apply_workflow_action (gated write),
plus the refuse-and-redirect guards added to submit_doc / cancel_doc, and the
api.py gating classification + confirm-card summary for the write tool.

Mock-based by design (mirrors test_submit_doc): a REAL active Workflow on a
submittable DocType needs heavy fixtures, and the engine these tools wrap
(frappe.model.workflow) is Frappe's own, already tested upstream. We verify what
these tools OWN: response shape, the docstatus/empty-state branches, the
has_approval_access filter, the no-workflow guard, the background-queue branch,
error propagation, the cancel-guard short-circuit order, the gating sets, and
the confirm card carrying the action. Nothing here does a real insert, so the
local-site global-search / user-creation gotchas never fire.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.model.workflow import WorkflowStateError
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.apply_workflow_action import apply_workflow_action
from jarvis.tools.cancel_doc import cancel_doc
from jarvis.tools.get_workflow_transitions import get_workflow_transitions
from jarvis.tools.submit_doc import submit_doc

GWT = "jarvis.tools.get_workflow_transitions"
AWA = "jarvis.tools.apply_workflow_action"


def _meta(submittable=True):
	m = MagicMock()
	m.is_submittable = submittable
	return m


class TestGetWorkflowTransitions(FrappeTestCase):
	def test_rejects_empty_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			get_workflow_transitions("", "X-1")

	def test_unknown_doc_raises(self):
		with patch("frappe.db.exists", return_value=False):
			with self.assertRaises(InvalidArgumentError):
				get_workflow_transitions("ToDo", "nope")

	def test_no_read_permission_raises(self):
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.has_permission", return_value=False),
		):
			with self.assertRaises(PermissionDeniedError):
				get_workflow_transitions("ToDo", "TODO-001")

	def test_no_workflow_returns_flag(self):
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.has_permission", return_value=True),
			patch(f"{GWT}.get_workflow_name", return_value=""),
		):
			result = get_workflow_transitions("ToDo", "TODO-001")
		self.assertEqual(result, {"doctype": "ToDo", "name": "TODO-001", "has_workflow": False})

	def test_lists_actions_filtered_by_approval_access(self):
		doc = frappe._dict({"owner": "a@b.c", "docstatus": 0, "workflow_state": "Applied"})
		transitions = [
			frappe._dict({"action": "Approve", "next_state": "Approved"}),
			frappe._dict({"action": "Reject", "next_state": "Rejected"}),
		]

		def _only_approve(user, d, t):
			return t.get("action") == "Approve"

		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.has_permission", return_value=True),
			patch(f"{GWT}.get_workflow_name", return_value="WF"),
			patch(f"{GWT}.get_workflow_state_field", return_value="workflow_state"),
			patch(f"{GWT}.get_transitions", return_value=transitions),
			patch(f"{GWT}.has_approval_access", side_effect=_only_approve),
			patch("frappe.get_doc", return_value=doc),
		):
			result = get_workflow_transitions("Leave Application", "LA-001")

		self.assertTrue(result["has_workflow"])
		self.assertEqual(result["workflow"], "WF")
		self.assertEqual(result["current_state"], "Applied")
		self.assertEqual(
			result["available_actions"],
			[{"action": "Approve", "next_state": "Approved"}],
		)

	def test_cancelled_doc_returns_no_actions(self):
		doc = frappe._dict({"owner": "a@b.c", "docstatus": 2, "workflow_state": "Approved"})
		gt = MagicMock()
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.has_permission", return_value=True),
			patch(f"{GWT}.get_workflow_name", return_value="WF"),
			patch(f"{GWT}.get_workflow_state_field", return_value="workflow_state"),
			patch(f"{GWT}.get_transitions", gt),
			patch("frappe.get_doc", return_value=doc),
		):
			result = get_workflow_transitions("Leave Application", "LA-001")
		self.assertEqual(result["available_actions"], [])
		self.assertEqual(result["current_state"], "Approved")
		gt.assert_not_called()  # never consult the engine for a cancelled doc

	def test_empty_state_returns_note_not_error(self):
		doc = frappe._dict({"owner": "a@b.c", "docstatus": 0, "workflow_state": None})
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.has_permission", return_value=True),
			patch(f"{GWT}.get_workflow_name", return_value="WF"),
			patch(f"{GWT}.get_workflow_state_field", return_value="workflow_state"),
			patch(f"{GWT}.get_transitions", side_effect=WorkflowStateError("no state")),
			patch("frappe.get_doc", return_value=doc),
		):
			result = get_workflow_transitions("Leave Application", "LA-001")
		self.assertIsNone(result["current_state"])
		self.assertEqual(result["available_actions"], [])
		self.assertIn("note", result)


class TestApplyWorkflowAction(FrappeTestCase):
	def test_rejects_empty_action(self):
		with self.assertRaises(InvalidArgumentError):
			apply_workflow_action("Leave Application", "LA-001", "")

	def test_rejects_non_workflow_doctype(self):
		with patch(f"{AWA}.get_workflow_name", return_value=""):
			with self.assertRaises(InvalidArgumentError) as ctx:
				apply_workflow_action("ToDo", "TODO-001", "Approve")
		self.assertIn("not governed by an active Workflow", str(ctx.exception))

	def test_applies_trimmed_action_and_returns_doc(self):
		doc = MagicMock()
		doc.as_dict.return_value = {"name": "LA-001", "workflow_state": "Approved", "docstatus": 1}
		with (
			patch(f"{AWA}.get_workflow_name", return_value="WF"),
			patch("frappe.get_doc", return_value=doc),
			patch(f"{AWA}.apply_workflow", return_value=doc) as aw,
		):
			result = apply_workflow_action("Leave Application", "LA-001", "  Approve  ")
		aw.assert_called_once_with(doc, "Approve")
		doc.apply_fieldlevel_read_permissions.assert_called_once()
		self.assertEqual(result["workflow_state"], "Approved")

	def test_queued_transition_returns_marker(self):
		doc = MagicMock()
		with (
			patch(f"{AWA}.get_workflow_name", return_value="WF"),
			patch("frappe.get_doc", return_value=doc),
			patch(f"{AWA}.apply_workflow", return_value=None),
		):
			result = apply_workflow_action("Leave Application", "LA-001", "Approve")
		self.assertEqual(
			result,
			{"doctype": "Leave Application", "name": "LA-001", "action": "Approve", "queued": True},
		)
		doc.apply_fieldlevel_read_permissions.assert_not_called()

	def test_propagates_validation_error(self):
		doc = MagicMock()
		with (
			patch(f"{AWA}.get_workflow_name", return_value="WF"),
			patch("frappe.get_doc", return_value=doc),
			patch(
				f"{AWA}.apply_workflow", side_effect=frappe.ValidationError("Self approval is not allowed")
			),
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				apply_workflow_action("Leave Application", "LA-001", "Approve")
		self.assertIn("Self approval", str(ctx.exception))


class TestSubmitCancelWorkflowGuard(FrappeTestCase):
	def test_submit_doc_refuses_workflow_doctype(self):
		with (
			patch("frappe.get_meta", return_value=_meta(True)),
			patch("jarvis.tools.submit_doc.get_workflow_name", return_value="WF"),
		):
			with self.assertRaises(InvalidArgumentError) as ctx:
				submit_doc("Leave Application", "LA-001")
		self.assertIn("governed by a Workflow", str(ctx.exception))

	def test_cancel_doc_refuses_when_cancel_is_modeled(self):
		with (
			patch("frappe.get_meta", return_value=_meta(True)),
			patch("jarvis.tools.cancel_doc.get_workflow_name", return_value="WF"),
			patch("jarvis.tools.cancel_doc.can_cancel_document", return_value=False),
		):
			with self.assertRaises(InvalidArgumentError) as ctx:
				cancel_doc("Leave Application", "LA-001")
		self.assertIn("apply_workflow_action", str(ctx.exception))

	def test_cancel_doc_allows_when_no_cancel_path(self):
		doc = MagicMock()
		doc.docstatus = 1
		doc.as_dict.return_value = {"name": "LA-001", "docstatus": 2}
		with (
			patch("frappe.get_meta", return_value=_meta(True)),
			patch("jarvis.tools.cancel_doc.get_workflow_name", return_value="WF"),
			patch("jarvis.tools.cancel_doc.can_cancel_document", return_value=True),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.get_doc", return_value=doc),
		):
			result = cancel_doc("Leave Application", "LA-001")
		doc.cancel.assert_called_once()
		self.assertEqual(result["docstatus"], 2)

	def test_cancel_doc_non_workflow_short_circuits_can_cancel(self):
		"""can_cancel_document crashes on a null workflow, so the guard must
		short-circuit on get_workflow_name first - assert it's never called."""
		doc = MagicMock()
		doc.docstatus = 1
		doc.as_dict.return_value = {"name": "SINV-1", "docstatus": 2}
		ccd = MagicMock()
		with (
			patch("frappe.get_meta", return_value=_meta(True)),
			patch("jarvis.tools.cancel_doc.get_workflow_name", return_value=""),
			patch("jarvis.tools.cancel_doc.can_cancel_document", ccd),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.get_doc", return_value=doc),
		):
			cancel_doc("Sales Invoice", "SINV-1")
		ccd.assert_not_called()


class TestWorkflowActionGating(FrappeTestCase):
	def test_classified_as_gated_destructive_write(self):
		self.assertIn("apply_workflow_action", api._WRITE_TOOLS)
		self.assertIn("apply_workflow_action", api._GATED_WRITES)
		self.assertIn("apply_workflow_action", api._DESTRUCTIVE)

	def test_not_dry_run_previewable(self):
		# on_submit/on_cancel hooks fire, so it must never be sandbox-dry-run.
		self.assertNotIn("apply_workflow_action", api._PREVIEWABLE)
		self.assertNotIn("apply_workflow_action", api._DRY_RUN_ON_PARK)
		self.assertNotIn("apply_workflow_action", api._AUTO_APPLYABLE)

	def test_parks_as_described_intent(self):
		pv = api._pending_preview(
			"apply_workflow_action",
			{"doctype": "Leave Application", "name": "LA-001", "action": "Approve"},
		)
		self.assertTrue(pv.get("described"))
		self.assertFalse(pv.get("preview"))

	def test_confirm_summary_names_the_action(self):
		summary = api._describe_call(
			"apply_workflow_action",
			{"doctype": "Leave Application", "name": "LA-001", "action": "Approve"},
		)
		self.assertIn("action=Approve", summary)
		self.assertIn("Leave Application", summary)


class TestWorkflowToolsRegistered(FrappeTestCase):
	def test_registered(self):
		from jarvis.tools.registry import _TOOL_NAMES

		self.assertIn("get_workflow_transitions", _TOOL_NAMES)
		self.assertIn("apply_workflow_action", _TOOL_NAMES)
