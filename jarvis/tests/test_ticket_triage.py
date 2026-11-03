"""Ticket Triage private-state and typed-control tests.

The state examples here are deliberately the contract.  They contain ticket
references, controlled values and opaque HMACs only.  A test that adds a raw
subject/body/email-like field must fail before any approval or write can use it.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_scheduler, approvals_api
from jarvis.chat import ticket_triage as triage
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _private_output


def safe_state() -> dict:
	return {
		"kind": triage.STATE_KIND,
		"capture_id": "opaque-capture-id",
		"policy_version": triage.POLICY_VERSION,
		"config_hmac": "a" * 64,
		"source_hmac": "b" * 64,
		"captured_at": "2026-11-01T09:00:00Z",
		"execution_user": "Administrator",
		"reviewer": "reviewer@example.test",
		"scope": {
			"policy_version": triage.POLICY_VERSION,
			"from_at": "2026-10-01T00:00:00Z",
			"to_at": "2026-10-31T23:59:59Z",
			"eligible_team_names": ["TT Team"],
			"eligible_status_names": ["Open"],
			"allowed_ticket_types": ["Question"],
			"allowed_priorities": ["Medium"],
			"max_tickets": 5,
			"assignment_mode": "none",
		},
		"ticket_count": 1,
		"tickets": [
			{
				"ticket": "TT-0001",
				"modified": "2026-10-30T08:00:00Z",
				"evidence_hmac": "c" * 64,
				"status": "Open",
				"ticket_type": "Question",
				"priority": "Medium",
			}
		],
		"stage": "captured",
		"proposals": [],
	}


class TestTicketTriageStateContract(FrappeTestCase):
	def test_private_dispatch_refuses_ungranted_metadata_without_any_receipt(self):
		from jarvis import api

		marker = "TT-RAW-CALLER-MARKER"
		cap = {"agent": triage.AGENT, "status": "running", "legacy": False}
		with patch.object(api, "persist_tool_receipt") as persist:
			result = triage.dispatch_private_call("test-session", marker, {marker: marker}, cap=cap)
		self.assertFalse(result["ok"])
		self.assertNotIn(marker, json.dumps(result))
		persist.assert_not_called()

	def test_private_dispatch_rolls_back_and_redacts_handler_failures(self):
		from jarvis import api
		from jarvis.tools import _delegate_capability

		marker = "TT-RAW-CALLER-MARKER"
		cap = {"agent": triage.AGENT, "status": "running", "legacy": False}
		with (
			patch.object(_delegate_capability, "tool_denial", return_value=None),
			patch.object(triage, "run_from_session", side_effect=RuntimeError(marker)),
			patch.object(frappe.db, "get_value", return_value="TEST-CONVERSATION"),
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(api, "persist_tool_receipt") as persist,
		):
			result = triage.dispatch_private_call(
				"test-session", triage.STAGE_TOOL, {"capture_id": marker, "rows": []}, cap=cap
			)
		self.assertFalse(result["ok"])
		rollback.assert_called_once()
		persist.assert_called_once()
		self.assertNotIn(marker, json.dumps(persist.call_args.args))
		self.assertEqual(persist.call_args.args[1:3], ("private_ticket_triage", {}))

	def test_capture_state_contract_contains_safe_metadata_only(self):
		state = safe_state()
		triage._assert_safe_state(state)

		for raw_field in ("subject", "description", "raised_by", "summary", "similarity_text"):
			with self.subTest(raw_field=raw_field):
				unsafe = copy.deepcopy(state)
				unsafe["tickets"][0][raw_field] = "TT-RAW-MARKER"
				with self.assertRaises(frappe.ValidationError):
					triage._assert_safe_state(unsafe)

		for change in (
			lambda value: value["scope"].update({"company": "TT-Company"}),
			lambda value: value["scope"].__setitem__("from_at", "2026-10-01 00:00:00"),
			lambda value: value["tickets"][0].__setitem__("modified", "2026-10-30 08:00:00"),
		):
			with self.subTest(change=change):
				unsafe = copy.deepcopy(state)
				change(unsafe)
				with self.assertRaises(frappe.ValidationError):
					triage._assert_safe_state(unsafe)

	def test_configuration_requires_canonical_utc_and_rejects_company_scope(self):
		config = copy.deepcopy(safe_state()["scope"])
		with patch.object(triage, "_validate_controls") as validate_controls:
			self.assertEqual(triage.configuration(frappe._dict(config=json.dumps(config))), config)
		validate_controls.assert_called_once_with(config)

		for change in (
			lambda value: value.update({"company": "TT-Company"}),
			lambda value: value.__setitem__("from_at", "2026-10-01 00:00:00"),
			lambda value: value.__setitem__("to_at", "2026-10-31T23:59:59+00:00"),
			lambda value: value.__setitem__("to_at", "2999-10-31T23:59:59Z"),
			lambda value: value.__setitem__("assignment_mode", "assign"),
		):
			with self.subTest(change=change):
				invalid = copy.deepcopy(config)
				change(invalid)
				with patch.object(triage, "_validate_controls"), self.assertRaises(frappe.ValidationError):
					triage.configuration(frappe._dict(config=json.dumps(invalid)))

	def test_ticket_scope_caps_the_modified_source_timestamp_at_as_of(self):
		config = safe_state()["scope"]
		filters = triage._ticket_filters(config)
		self.assertIn("modified", filters)
		self.assertNotIn("creation", filters)
		within_scope = {
			"modified": "2026-10-30 08:00:00",
			"agent_group": "TT Team",
			"status": "Open",
			"status_category": "Open",
		}
		triage._assert_ticket_scope(within_scope, config)
		beyond_as_of = {**within_scope, "modified": "2026-11-01 08:00:00"}
		with self.assertRaises(frappe.ValidationError):
			triage._assert_ticket_scope(beyond_as_of, config)

	def test_ordinary_capture_refuses_before_any_source_collection(self):
		with (
			patch("jarvis.tools._private_output.is_private", return_value=False),
			patch.object(triage, "_collect") as collect,
		):
			with self.assertRaises(InvalidArgumentError):
				triage.capture_private_inputs(None)
		collect.assert_not_called()

	def test_stage_rows_reject_extra_keys_duplicate_or_missing_ticket(self):
		state = safe_state()
		valid = {"ticket": "TT-0001", "disposition": "propose", "ticket_type": "Question", "priority": None}
		for rows in (
			[{**valid, "summary": "model text"}],
			[valid, valid],
			[],
			[{**valid, "ticket": "TT-OTHER"}],
		):
			with self.subTest(rows=rows), self.assertRaises(frappe.ValidationError):
				triage._validate_stage_rows(state, state["scope"], rows)

	def test_stage_refuses_a_proposal_that_keeps_both_current_values(self):
		state = safe_state()
		for ticket_type, priority in (("Question", None), (None, "Medium"), ("Question", "Medium")):
			with self.subTest(ticket_type=ticket_type, priority=priority):
				with self.assertRaises(frappe.ValidationError):
					triage._validate_stage_rows(
						state,
						state["scope"],
						[
							{
								"ticket": "TT-0001",
								"disposition": "propose",
								"ticket_type": ticket_type,
								"priority": priority,
							}
						],
					)

	def test_stage_refuses_a_capture_id_from_another_run_before_any_approval(self):
		state = safe_state()
		run = frappe._dict(
			name="TT-RUN",
			status="running",
			assessment_output_json=json.dumps(state),
		)
		with (
			patch.object(triage, "_require_stage_run"),
			patch.object(triage, "_lock_run", return_value=run),
			patch.object(triage, "_approval_shell") as approval_shell,
			self.assertRaises(frappe.ValidationError),
		):
			triage.stage_proposals(run, "other-capture", [])
		approval_shell.assert_not_called()

	def test_stage_refuses_a_new_or_missing_scoped_ticket_before_any_ticket_lock(self):
		state = safe_state()
		with (
			patch.object(triage, "_candidate_names", return_value=["TT-NEW"]) as candidates,
			patch.object(triage, "_locked_ticket") as lock_ticket,
			self.assertRaises(frappe.ValidationError),
		):
			triage._fresh_capture_evidence(state, state["scope"])
		candidates.assert_called_once_with(state["scope"])
		lock_ticket.assert_not_called()

	def test_raw_marker_stays_out_of_state_approval_receipt_and_ticket_errors(self):
		marker = "TT-RAW-MARKER-DO-NOT-PERSIST"
		config = safe_state()["scope"]
		raw_ticket = {
			"name": "TT-0001",
			"modified": "2026-10-30 08:00:00",
			"creation": "2026-10-30 07:00:00",
			"status": "Open",
			"status_category": "Open",
			"ticket_type": "Question",
			"priority": "Medium",
			"agent_group": "TT Team",
			"_assign": "[]",
			"is_merged": 0,
			"subject": marker,
			"description": marker,
			"raised_by": marker,
		}
		snapshot = {
			"source_digest": "d" * 64,
			"captured_at": "2026-11-01T09:00:00Z",
			"tickets": [raw_ticket],
		}
		with patch("jarvis.chat.ticket_triage.get_encryption_key", return_value="test-key"):
			state = triage._capture_state(
				capture_id="opaque-capture-id",
				config=config,
				config_hmac="a" * 64,
				snapshot=snapshot,
				reviewer="reviewer@example.test",
			)
			proposal = {
				"ticket": "TT-0001",
				"disposition": "propose",
				"ticket_type": "Question",
				"priority": "High",
			}
			proposal_hmac = triage._proposal_hmac(state, proposal)
			evidence = triage._proposal_evidence(state, state["tickets"][0], proposal, proposal_hmac)
			values = triage._approval_values(
				frappe._dict(name="TT-RUN", preparation_mode="shadow"),
				evidence,
				triage._approval_key("TT-RUN", evidence),
			)
			wire, receipt = _private_output.prepare(
				{"ok": True, "data": {"rows": [{**snapshot, "subject": marker}]}},
				tool=triage.SOURCE_TOOL,
			)

		self.assertIn(marker, wire["data"])
		for safe_value in (state, evidence, values, receipt):
			self.assertNotIn(marker, json.dumps(safe_value, sort_keys=True))
		with patch.object(frappe.db, "set_value") as store:
			triage._store_state("TT-RUN", state)
		self.assertNotIn(marker, str(store.call_args))

		# Scheduler paths must not relay a raw provider reply/error for this agent.
		self.assertNotIn(
			marker,
			agent_scheduler._gateway_abort_error_message({"reply": marker}, "aborted", agent=triage.AGENT),
		)
		self.assertNotIn(
			marker,
			agent_scheduler._fleet_error_message({"error": {"message": marker}}, agent=triage.AGENT),
		)

	def test_generic_approval_path_refuses_only_the_exact_typed_contract(self):
		ticket_doc = frappe._dict(
			agent=triage.AGENT, source=triage.APPROVAL_SOURCE, assessment_key="opaque-key"
		)
		with self.assertRaises(frappe.PermissionError):
			approvals_api._refuse_if_ticket_triage(ticket_doc)
		# A normal Agent Review row remains on its existing route.
		approvals_api._refuse_if_ticket_triage(
			frappe._dict(
				agent="ap-3way-match-operator", source=triage.APPROVAL_SOURCE, assessment_key="opaque-key"
			)
		)

	def test_all_generic_approval_actions_and_list_row_refuse_the_typed_shell(self):
		typed = frappe._dict(
			name="TT-APPROVAL",
			agent=triage.AGENT,
			source=triage.APPROVAL_SOURCE,
			assessment_key="opaque-key",
			conversation="",
		)
		normal = frappe._dict(
			name="TT-NORMAL",
			agent=triage.AGENT,
			source=triage.APPROVAL_SOURCE,
			assessment_key="",
		)
		self.assertEqual(approvals_api._without_ticket_triage([typed, normal]), [normal])

		for endpoint, args in (
			(approvals_api.get_approval, (typed.name,)),
			(approvals_api.decide, (typed.name, "confirm")),
			(approvals_api.dismiss_approval, (typed.name,)),
			(approvals_api.restore_approval, (typed.name,)),
		):
			with (
				self.subTest(endpoint=endpoint.__name__),
				patch("jarvis.chat.approvals_api.frappe.get_doc", return_value=typed),
				patch("jarvis.chat.approvals_api.refuse_in_tool_dispatch"),
				patch("jarvis.chat.approvals_api._resume_conversation") as resume,
				self.assertRaises(frappe.PermissionError),
			):
				endpoint(*args)
			resume.assert_not_called()

	def test_typed_reviewer_view_is_safe_and_a_different_user_is_refused(self):
		state = safe_state()
		proposal = {
			"ticket": "TT-0001",
			"disposition": "propose",
			"ticket_type": "Question",
			"priority": "High",
		}
		with patch("jarvis.chat.ticket_triage.get_encryption_key", return_value="test-key"):
			proposal_hmac = triage._proposal_hmac(state, proposal)
			evidence = triage._proposal_evidence(state, state["tickets"][0], proposal, proposal_hmac)
		view = triage._safe_proposal_view(
			frappe._dict(name="TT-APPROVAL", status="Pending", apply_status="Pending"),
			evidence,
			can_confirm=True,
		)
		self.assertEqual(
			set(view), {"name", "ticket", "status", "apply_status", "current", "proposed", "can_confirm"}
		)
		self.assertNotIn("subject", json.dumps(view, sort_keys=True))
		self.assertNotIn("description", json.dumps(view, sort_keys=True))
		with self.assertRaises(frappe.PermissionError):
			triage._require_reviewer(frappe._dict(conversation=""), evidence, write=False)


class TestTicketTriageFakeRecords(FrappeTestCase):
	"""One complete journey with TT-only Helpdesk records and no shared fixtures."""

	def setUp(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("DocType", triage.TICKET):
			self.skipTest(
				"test_jarvis lacks the Helpdesk HD Ticket DocType; TT integration evidence is unavailable"
			)
		self.prefix = "TT-" + frappe.generate_hash(length=10).upper()
		self.reviewer = self.prefix.lower() + "@example.test"
		self.team = self.prefix + " Team"
		self.status = self.prefix + " Open"
		self.ticket_type = self.prefix + " Question"
		self.priority_before = self.prefix + " Medium"
		self.priority_after = self.prefix + " High"
		self.marker = self.prefix + " RAW CUSTOMER TEXT"
		self.session_key = f"agent:agent-{triage.AGENT}:{self.prefix}"
		self._make_fixture()

	def tearDown(self):
		frappe.set_user("Administrator")
		if getattr(self, "conversation_name", None):
			frappe.db.delete("Jarvis Chat Message", {"conversation": self.conversation_name})
			frappe.db.set_value("Jarvis Agent Run", self.run_name, "conversation", None)
			frappe.delete_doc(
				"Jarvis Conversation", self.conversation_name, force=True, ignore_permissions=True
			)
		for name in frappe.get_all(
			"Jarvis Approval Request", {"run": getattr(self, "run_name", "")}, pluck="name"
		):
			frappe.delete_doc("Jarvis Approval Request", name, force=True, ignore_permissions=True)
		for doctype, filters in (
			("Jarvis Chat Session", {"session_key": getattr(self, "session_key", "")}),
			("Jarvis Agent Run", {"name": getattr(self, "run_name", "")}),
			("Jarvis Agent Installation", {"name": getattr(self, "installation", "")}),
			("HD Ticket Activity", {"ticket": getattr(self, "ticket_name", "")}),
			("HD Ticket Comment", {"reference_ticket": getattr(self, "ticket_name", "")}),
			("HD Notification", {"reference_ticket": getattr(self, "ticket_name", "")}),
			("ToDo", {"reference_type": "HD Ticket", "reference_name": getattr(self, "ticket_name", "")}),
			(
				"Communication",
				{"reference_doctype": "HD Ticket", "reference_name": getattr(self, "ticket_name", "")},
			),
		):
			if frappe.db.exists("DocType", doctype):
				frappe.db.delete(doctype, filters)
		if getattr(self, "ticket_name", None) and frappe.db.exists("HD Ticket", self.ticket_name):
			frappe.delete_doc("HD Ticket", self.ticket_name, force=True, ignore_permissions=True)
		for doctype, name in (
			("HD Team", getattr(self, "team", "")),
			("HD Ticket Status", getattr(self, "status", "")),
			("HD Ticket Type", getattr(self, "ticket_type", "")),
			("HD Ticket Priority", getattr(self, "priority_before", "")),
			("HD Ticket Priority", getattr(self, "priority_after", "")),
			("User", getattr(self, "reviewer", "")),
		):
			if name and frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- remove only TT fake fixtures

	def _make_fixture(self):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": self.reviewer,
				"first_name": self.prefix,
				"send_welcome_email": 0,
			}
		)
		user.insert(ignore_permissions=True)
		# System Manager supplies the real Helpdesk write permission.  This test
		# site need not install the optional Helpdesk Agent role.
		user.add_roles("System Manager", "Jarvis User")
		frappe.get_doc(
			{
				"doctype": "HD Team",
				"team_name": self.team,
				"users": [{"user": self.reviewer}],
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "HD Ticket Status",
				"label_agent": self.status,
				"enabled": 1,
				"category": "Open",
			}
		).insert(ignore_permissions=True)
		for name in (self.priority_before, self.priority_after):
			frappe.get_doc({"doctype": "HD Ticket Priority", "name": name, "level": "Medium"}).insert(
				ignore_permissions=True
			)
		frappe.get_doc(
			{
				"doctype": "HD Ticket Type",
				"name": self.ticket_type,
				"priority": self.priority_before,
			}
		).insert(ignore_permissions=True)
		ticket = frappe.get_doc(
			{
				"doctype": "HD Ticket",
				"name": self.prefix + "-TICKET",
				"subject": self.marker,
				"description": self.marker,
				"raised_by": self.reviewer,
				"agent_group": self.team,
				"status": self.status,
				"ticket_type": self.ticket_type,
				"priority": self.priority_before,
			}
		)
		ticket.insert(ignore_permissions=True)
		ticket.reload()
		# Helpdesk selects its default Open status on insert. Save the owned
		# status through its normal update path before capturing the scope.
		ticket.status = self.status
		ticket.save(ignore_permissions=True)
		self.ticket_name = ticket.name
		# The saved scope has whole-second UTC bounds. Let the real clock pass
		# this ticket's fractional modified time; do not forge source timestamps.
		time.sleep(1.05)
		to_at = triage._server_now_utc(error=triage._CONFIG_INVALID)
		from_at = to_at - timedelta(minutes=5)
		config = {
			"policy_version": triage.POLICY_VERSION,
			"from_at": from_at.strftime(triage.UTC_TIMESTAMP_FORMAT),
			"to_at": to_at.strftime(triage.UTC_TIMESTAMP_FORMAT),
			"eligible_team_names": [self.team],
			"eligible_status_names": [self.status],
			"allowed_ticket_types": [self.ticket_type],
			"allowed_priorities": [self.priority_before, self.priority_after],
			"max_tickets": 5,
			"assignment_mode": "none",
		}
		config = triage.configuration(frappe._dict(config=json.dumps(config)))
		inst = frappe.get_doc(
			{
				"doctype": "Jarvis Agent Installation",
				"agent": triage.AGENT,
				"run_as_user": self.reviewer,
				"reviewer": self.reviewer,
				"activation_state": "shadow",
				"config": json.dumps(config),
			}
		)
		inst.owner = self.reviewer
		inst.insert(ignore_permissions=True)
		self.installation = inst.name
		run = frappe.get_doc(
			{
				"doctype": "Jarvis Agent Run",
				"agent": triage.AGENT,
				"installation": inst.name,
				"trigger": "manual",
				"status": "running",
				"preparation_mode": "live",
				"started_at": frappe.utils.now_datetime(),
				"session_key": self.session_key,
				"assessment_config_json": json.dumps(config),
				"bundle_version": "0.2.0",
				"capability_contract": "snapshot",
				"tools_allow_json": json.dumps(
					[f"jarvis__{triage.SOURCE_TOOL}", f"jarvis__{triage.STAGE_TOOL}"]
				),
				"capability_nature": "operator",
				"capability_writes_json": "[]",
			}
		)
		run.owner = self.reviewer
		run.insert(ignore_permissions=True)
		self.run_name = run.name
		frappe.get_doc(
			{"doctype": "Jarvis Chat Session", "session_key": self.session_key, "user": self.reviewer}
		).insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- isolate TT fixture before lock tests

	def _capture(self):
		frappe.set_user(self.reviewer)
		run = frappe.get_doc("Jarvis Agent Run", self.run_name)
		with _private_output.request(True):
			result = triage.capture_private_inputs(run)
		run.reload()
		return run, result["rows"][0]

	def _stage(self, run, capture_id):
		return triage.stage_proposals(
			run,
			capture_id,
			[
				{
					"ticket": self.ticket_name,
					"disposition": "propose",
					"ticket_type": None,
					"priority": self.priority_after,
				}
			],
		)

	def test_fake_tt_capture_stage_confirm_is_typed_private_and_idempotent(self):
		run, snapshot = self._capture()
		self.assertEqual(snapshot["tickets"][0]["subject"], self.marker)
		self.assertEqual(snapshot["tickets"][0]["description"], self.marker)
		self.assertEqual(run.input_snapshot_json or "", "")
		self.assertNotIn(self.marker, run.assessment_output_json)
		state = json.loads(run.assessment_output_json)
		self.assertEqual(state["ticket_count"], 1)
		self.assertNotIn("subject", state["tickets"][0])

		staged = self._stage(run, state["capture_id"])
		self.assertEqual(staged["proposed_count"], 1)
		approval = frappe.get_doc("Jarvis Approval Request", staged["approvals"][0])
		self.assertEqual(approval.ref_doctype or "", "")
		self.assertEqual(approval.ref_name or "", "")
		self.assertEqual(approval.context_md or "", "")
		self.assertNotIn(self.marker, approval.assessment_evidence)
		safe_view = triage.get_ticket_triage_proposal(approval.name)
		self.assertEqual(safe_view["ticket"], self.ticket_name)
		self.assertNotIn(self.marker, json.dumps(safe_view, sort_keys=True))
		for generic in (
			lambda: approvals_api.get_approval(approval.name),
			lambda: approvals_api.decide(approval.name, "not a typed decision"),
			lambda: approvals_api.dismiss_approval(approval.name),
			lambda: approvals_api.restore_approval(approval.name),
		):
			with self.subTest(generic=generic), self.assertRaises(frappe.PermissionError):
				generic()
		approval.reload()
		self.assertEqual(approval.status, "Pending")

		before = frappe.db.get_value(
			"HD Ticket",
			self.ticket_name,
			["status", "agent_group", "_assign", "subject", "description", "raised_by", "ticket_type"],
			as_dict=True,
		)
		guard_before = triage._write_guard(frappe.get_doc("HD Ticket", self.ticket_name))
		result = triage.confirm_ticket_triage_proposal(approval.name)
		self.assertEqual(result, {"ok": True, "status": "Approved", "idempotent": False})
		ticket = frappe.get_doc("HD Ticket", self.ticket_name)
		self.assertEqual(ticket.priority, self.priority_after)
		self.assertEqual(
			{field: ticket.get(field) for field in before},
			{field: before[field] for field in before},
		)
		self.assertEqual(triage.confirm_ticket_triage_proposal(approval.name)["idempotent"], True)
		approval.reload()
		self.assertEqual(approval.status, "Approved")
		self.assertEqual(approval.apply_status, "Applied")
		# Native ticket creation can already have a Communication. Confirmation
		# must add none and must preserve every prohibited field.
		self.assertEqual(triage._write_guard(ticket), guard_before)

	def test_fake_tt_source_drift_refuses_before_any_approval(self):
		run, snapshot = self._capture()
		frappe.db.set_value("HD Ticket", self.ticket_name, "description", self.marker + " changed")
		with self.assertRaises(frappe.ValidationError):
			self._stage(run, snapshot["capture_id"])
		self.assertFalse(frappe.db.exists("Jarvis Approval Request", {"run": self.run_name}))

	def test_shadow_staging_creates_no_approval_or_ticket_change(self):
		frappe.db.set_value("Jarvis Agent Run", self.run_name, "preparation_mode", "shadow")
		run, snapshot = self._capture()
		result = self._stage(run, snapshot["capture_id"])
		self.assertEqual(result["status"], "preview")
		self.assertEqual(result["approvals"], [])
		self.assertFalse(frappe.db.exists("Jarvis Approval Request", {"run": self.run_name}))
		self.assertEqual(frappe.db.get_value("HD Ticket", self.ticket_name, "priority"), self.priority_before)

	def test_rejection_is_idempotent_and_never_updates_the_ticket(self):
		run, snapshot = self._capture()
		staged = self._stage(run, snapshot["capture_id"])
		name = staged["approvals"][0]
		self.assertEqual(triage.reject_ticket_triage_proposal(name)["status"], "Rejected")
		self.assertTrue(triage.reject_ticket_triage_proposal(name)["idempotent"])
		self.assertEqual(frappe.db.get_value("HD Ticket", self.ticket_name, "priority"), self.priority_before)

	def test_unchanged_capture_and_stage_survive_a_later_clock_tick(self):
		run, first = self._capture()
		later = triage._server_now_utc(error=triage._CONFIG_INVALID) + timedelta(seconds=5)
		with patch.object(triage, "_server_now_utc", return_value=later):
			with _private_output.request(True):
				second = triage.capture_private_inputs(run)["rows"][0]
			self.assertNotEqual(first["captured_at"], second["captured_at"])
			self.assertEqual(first["source_digest"], second["source_digest"])
			self.assertEqual(self._stage(run, first["capture_id"])["proposed_count"], 1)

	def test_list_exposes_only_the_named_reviewers_safe_view(self):
		run, snapshot = self._capture()
		self._stage(run, snapshot["capture_id"])
		page = triage.list_ticket_triage_proposals()
		self.assertEqual(len(page["rows"]), 1)
		self.assertEqual(page["rows"][0]["execution_user"], self.reviewer)
		self.assertEqual(page["rows"][0]["reviewer"], self.reviewer)
		self.assertNotIn(self.marker, json.dumps(page))
		frappe.set_user("Administrator")
		self.assertEqual(triage.list_ticket_triage_proposals()["rows"], [])

	def test_bound_dispatch_keeps_source_and_malformed_arguments_out_of_chat(self):
		from jarvis import api

		frappe.set_user(self.reviewer)
		conversation = frappe.get_doc(
			{"doctype": "Jarvis Conversation", "title": "TT test", "session_key": self.session_key}
		).insert()
		self.conversation_name = conversation.name
		frappe.db.set_value("Jarvis Agent Run", self.run_name, "conversation", conversation.name)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- isolated test dispatch fixture
		with patch("jarvis.api.publish_realtime_tool_result") as published:
			result = api._dispatch_from_session(
				self.reviewer, self.session_key, triage.SOURCE_TOOL, {}, result_mode=_private_output.MODE
			)
			self.assertTrue(result["ok"], result)
			self.assertIn(self.marker, result["data"])
			self.assertNotIn(self.marker, str(published.call_args_list))
			result = api._dispatch_from_session(
				self.reviewer,
				self.session_key,
				triage.STAGE_TOOL,
				{"capture_id": self.marker, "rows": [], "description": self.marker},
			)
			self.assertFalse(result["ok"])
			self.assertNotIn(self.marker, str(published.call_args_list))
		for message in frappe.get_all(
			"Jarvis Chat Message",
			{"conversation": conversation.name},
			["tool_name", "tool_args", "tool_result", "tool_call_id"],
		):
			self.assertNotIn(self.marker, json.dumps(message))
			self.assertEqual(message.tool_name, "private_ticket_triage")
			self.assertIsNone(message.tool_call_id)
