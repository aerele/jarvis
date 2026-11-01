"""Server-bound Department scope regressions for the Attendance auditor."""

from __future__ import annotations

import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_scheduler, agent_scope, operator_review
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import record_agent_run as writeback

AGENT = agent_scope.ATTENDANCE_AGENT
BASE_SCOPE = {
	"company": "Scope Company",
	"fiscal_year": "2026-2027",
	"from_date": "2026-10-01",
	"to_date": "2026-10-31",
	"report_date": "2026-10-31",
}
POLICY = {
	"reviewer": "reviewer@example.invalid",
	"reviewer_capacity": "HR reviewer",
	"marking_model": "every_day",
	"checkin_policy": "required",
	"holiday_lists": ["Holiday List - Scope"],
	"leave_types": ["Casual Leave"],
}
_DEFAULT_DEPARTMENT = object()


class TestAttendanceScope(FrappeTestCase):
	def installation(self, config):
		return frappe._dict(
			name="ATTENDANCE-SCOPE-1",
			agent=AGENT,
			config=frappe.as_json(config),
		)

	def configuration(self, record_scope=None):
		return {
			"company": BASE_SCOPE["company"],
			"from_date": BASE_SCOPE["from_date"],
			"to_date": BASE_SCOPE["to_date"],
			"attendance": dict(POLICY),
			**({"record_scope": record_scope} if record_scope is not None else {}),
		}

	def resolve(self, config, department=_DEFAULT_DEPARTMENT, readable=True, company_readable=True):
		department = (
			department
			if department is not _DEFAULT_DEPARTMENT
			else frappe._dict(company=BASE_SCOPE["company"], is_group=0, disabled=0)
		)
		with (
			mock.patch.object(agent_scope, "_resolve", return_value=dict(BASE_SCOPE)),
			mock.patch.object(frappe.db, "get_value", return_value=department) as get_value,
			mock.patch.object(
				frappe, "has_permission", side_effect=(readable, company_readable)
			) as has_permission,
		):
			scope = agent_scope.resolve_scope(self.installation(config))
		return scope, get_value, has_permission

	def test_valid_leaf_department_is_stamped_as_the_only_record_scope(self):
		scope, get_value, has_permission = self.resolve(
			self.configuration({"doctype": "Department", "name": "HR Operations"})
		)

		self.assertEqual(scope["record_scope"], {"doctype": "Department", "name": "HR Operations"})
		get_value.assert_called_once_with(
			"Department", "HR Operations", ["company", "is_group", "disabled"], as_dict=True
		)
		has_permission.assert_has_calls(
			[
				mock.call("Department", "read", doc="HR Operations"),
				mock.call("Company", "read", doc=BASE_SCOPE["company"]),
			]
		)

	def test_missing_or_invalid_department_never_becomes_an_unscoped_review(self):
		valid = {"doctype": "Department", "name": "HR Operations"}
		cases = (
			self.configuration(),
			self.configuration(None),
			self.configuration({"doctype": "Department"}),
			self.configuration({"doctype": "Company", "name": "Scope Company"}),
			self.configuration({"doctype": "Department", "name": " HR Operations"}),
			self.configuration({**valid, "all": True}),
		)
		for config in cases:
			with self.subTest(config=config), self.assertRaises(InvalidArgumentError):
				self.resolve(config)

	def test_foreign_group_or_unreadable_department_is_refused(self):
		config = self.configuration({"doctype": "Department", "name": "HR Operations"})
		for department, readable, company_readable in (
			(frappe._dict(company="Other Company", is_group=0, disabled=0), True, True),
			(frappe._dict(company=BASE_SCOPE["company"], is_group=1, disabled=0), True, True),
			(frappe._dict(company=BASE_SCOPE["company"], is_group=0, disabled=1), True, True),
			(frappe._dict(company=BASE_SCOPE["company"], is_group=0, disabled=0), False, True),
			(frappe._dict(company=BASE_SCOPE["company"], is_group=0, disabled=0), True, False),
			(None, True, True),
		):
			with (
				self.subTest(department=department, readable=readable, company_readable=company_readable),
				self.assertRaises(InvalidArgumentError),
			):
				self.resolve(
					config,
					department=department,
					readable=readable,
					company_readable=company_readable,
				)

	def test_raw_owner_root_stays_out_of_snapshot_and_prompt(self):
		listing = frappe._dict(agent_slug=AGENT, config_keys='["attendance"]')
		inst = self.installation(
			self.configuration({"doctype": "Department", "name": "Untrusted Department", "all": True})
		)
		snapshot = agent_scheduler._explicit_config(listing, inst)
		self.assertEqual(snapshot, {"attendance": POLICY})

		prompt = agent_scheduler._audit_prompt(
			listing,
			inst,
			"manual",
			{**BASE_SCOPE, "record_scope": {"doctype": "Department", "name": "HR Operations"}},
			engagement_config=snapshot,
		)
		self.assertIn('record_scope={"doctype": "Department", "name": "HR Operations"}', prompt)
		self.assertNotIn("Untrusted Department", prompt)
		self.assertNotIn('"all"', prompt)

	def test_scope_error_refuses_before_scope_or_permission_values_are_stamped(self):
		inst = self.installation(self.configuration())
		with (
			mock.patch.object(operator_review, "backend", return_value=None),
			mock.patch.object(
				agent_scope,
				"resolve_scope",
				side_effect=InvalidArgumentError("Attendance record_scope must name one Department leaf"),
			),
			mock.patch.object(frappe, "log_error") as log_error,
			mock.patch.object(frappe.db, "set_value") as set_value,
		):
			with self.assertRaises(InvalidArgumentError):
				agent_scheduler._stamp_scope_and_watermark("ATT-RUN", inst, "run-as@example.invalid")

		set_value.assert_not_called()
		self.assertEqual(
			log_error.call_args.kwargs["title"], "jarvis agent: Attendance scope resolution refused launch"
		)

	def test_every_invalid_department_case_refuses_before_any_scope_stamp(self):
		valid = {"doctype": "Department", "name": "HR Operations"}
		cases = (
			(self.configuration(), _DEFAULT_DEPARTMENT, True, True),
			(self.configuration({"doctype": "Department"}), _DEFAULT_DEPARTMENT, True, True),
			(
				self.configuration({"doctype": "Company", "name": "Scope Company"}),
				_DEFAULT_DEPARTMENT,
				True,
				True,
			),
			(self.configuration({**valid, "all": True}), _DEFAULT_DEPARTMENT, True, True),
			(
				self.configuration(valid),
				frappe._dict(company="Other Company", is_group=0, disabled=0),
				True,
				True,
			),
			(
				self.configuration(valid),
				frappe._dict(company=BASE_SCOPE["company"], is_group=1, disabled=0),
				True,
				True,
			),
			(
				self.configuration(valid),
				frappe._dict(company=BASE_SCOPE["company"], is_group=0, disabled=1),
				True,
				True,
			),
			(
				self.configuration(valid),
				frappe._dict(company=BASE_SCOPE["company"], is_group=0, disabled=0),
				False,
				True,
			),
			(
				self.configuration(valid),
				frappe._dict(company=BASE_SCOPE["company"], is_group=0, disabled=0),
				True,
				False,
			),
		)
		for config, department, readable, company_readable in cases:
			department = (
				frappe._dict(company=BASE_SCOPE["company"], is_group=0, disabled=0)
				if department is _DEFAULT_DEPARTMENT
				else department
			)
			with (
				self.subTest(config=config),
				mock.patch.object(agent_scope, "_resolve", return_value=dict(BASE_SCOPE)),
				mock.patch.object(frappe.db, "get_value", return_value=department),
				mock.patch.object(frappe, "has_permission", side_effect=(readable, company_readable)),
				mock.patch.object(operator_review, "backend", return_value=None),
				mock.patch.object(frappe, "log_error"),
				mock.patch.object(frappe.db, "set_value") as set_value,
			):
				with self.assertRaises(InvalidArgumentError):
					agent_scheduler._stamp_scope_and_watermark(
						"ATT-RUN", self.installation(config), "run-as@example.invalid"
					)
				set_value.assert_not_called()

	def test_model_cannot_replace_the_server_stamped_department_after_launch(self):
		"""A widened model output lands as non-evaluable with no findings."""
		tokens = [
			"ali-bal-5e63",
			"ali-cal-6b48",
			"ali-cf-3f7a",
			"ali-lvmk-2a8f",
			"ali-lwp-9c21",
			"ali-punch-7d14",
		]
		stamped_scope = {
			**BASE_SCOPE,
			"record_scope": {"doctype": "Department", "name": "HR Operations"},
		}
		run_row = frappe._dict(
			name="ATT-RUN",
			status="running",
			installation="ATTENDANCE-SCOPE-1",
			agent=AGENT,
		)
		run_doc = frappe._dict(name="ATT-RUN", agent=AGENT, scope_json=frappe.as_json(stamped_scope))
		final_run = frappe._dict(
			name="ATT-RUN",
			status="partial",
			findings_count=0,
			blocker_count=0,
			coverage_note="scope did not match server-stamped Department boundary",
			owner="reviewer@example.invalid",
		)

		def get_value(doctype, *_args, **_kwargs):
			if doctype == writeback.RUN:
				return run_row
			if doctype == writeback.LISTING:
				return frappe._dict(
					rule_tokens=frappe.as_json(tokens), doctypes_required=frappe.as_json(["Employee"])
				)
			self.fail(f"unexpected get_value for {doctype}")

		model_scope = {
			**BASE_SCOPE,
			"record_scope": {"doctype": "Department", "name": "Other Department"},
			"owner_config": {"all_departments": True},
		}
		with (
			mock.patch(
				"jarvis.tools._agent_run_ctx.get_session_key", return_value="agent:attendance:ATT-RUN"
			),
			mock.patch.object(writeback.frappe.db, "get_value", side_effect=get_value),
			mock.patch.object(
				writeback.frappe,
				"get_doc",
				side_effect=(frappe._dict(name="ATTENDANCE-SCOPE-1"), run_doc),
			),
			mock.patch.object(operator_review, "backend", return_value=None),
			mock.patch("jarvis.chat.agent_runs.record_delegate_run", return_value=final_run) as land,
			mock.patch("jarvis.chat.agent_run_steps.record_step"),
			mock.patch.object(writeback.frappe, "logger"),
		):
			result = writeback.record_agent_run(
				findings=[
					{
						"token": "ali-punch-7d14",
						"ref_doctype": "Employee",
						"ref_name": "OUT-OF-SCOPE",
						"severity": "warning",
					}
				],
				coverage={"ali-punch-7d14": {"state": "evaluated"}},
				scope=model_scope,
			)

		self.assertEqual(result["status"], "partial")
		args, kwargs = land.call_args
		self.assertEqual(args[2], [])
		self.assertEqual(kwargs["scope"], agent_scope.attendance_output_scope(stamped_scope))
		self.assertTrue(kwargs["dropped"])
		self.assertTrue(all(row["state"] == "not_evaluable" for row in kwargs["coverage"].values()))
		self.assertNotIn("Other Department", json.dumps(kwargs, sort_keys=True))
		self.assertNotIn("owner_config", json.dumps(kwargs, sort_keys=True))
