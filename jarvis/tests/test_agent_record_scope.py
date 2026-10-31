"""Keep record-scoped assessments apart across real writeback transactions."""

import json
from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_runs, agent_scheduler, agent_scope
from jarvis.exceptions import InvalidArgumentError
from jarvis.tests.test_platform_false_clean import (
	FINDING,
	RUN,
	SLUG,
	TOK_A,
	TOK_B,
	_mk_installation,
	_mk_listing,
	_mk_run,
	_mk_user,
	_wipe_residue,
)


class TestComparisonPeriod(FrappeTestCase):
	def resolve(self, cfg):
		inst = frappe._dict(config=json.dumps(cfg))
		with patch.object(
			agent_scope,
			"_resolve",
			return_value={"company": "Fake Company", "from_date": "2026-09-01", "to_date": "2026-09-30"},
		):
			return agent_scope.resolve_scope(inst)

	def test_explicit_prior_period_is_preserved(self):
		scope = self.resolve({"prior_period_from": "2026-08-01", "prior_period_to": "2026-08-31"})
		self.assertEqual(scope["prior_period_from"], "2026-08-01")
		self.assertEqual(scope["prior_period_to"], "2026-08-31")

	def test_no_comparison_period_is_guessed(self):
		self.assertNotIn("prior_period_from", self.resolve({}))

	def test_invalid_or_overlapping_period_is_refused(self):
		for cfg in [
			{"prior_period_from": "2026-08-01"},
			{"prior_period_to": "2026-08-31"},
			{"prior_period_from": "bad", "prior_period_to": "2026-08-31"},
			{"prior_period_from": "2026-08-31", "prior_period_to": "2026-08-01"},
			{"prior_period_from": "2026-08-01", "prior_period_to": "2026-09-01"},
		]:
			with self.subTest(cfg=cfg), self.assertRaises(InvalidArgumentError):
				self.resolve(cfg)

	def test_record_scope_checks_real_record_permission(self):
		record = {"doctype": "Company", "name": "Fake Company"}
		with patch.object(frappe, "get_doc") as get_doc:
			get_doc.return_value.get.return_value = None
			self.assertEqual(self.resolve({"record_scope": record})["record_scope"], record)
			get_doc.return_value.check_permission.assert_called_once_with("read")

	def test_bad_explicit_scope_stops_the_launch_path(self):
		inst = frappe._dict(
			agent="fake-scope-agent",
			config=json.dumps({"record_scope": {"doctype": "Company", "name": "missing"}}),
		)
		with (
			patch.object(agent_scope, "resolve_scope", side_effect=InvalidArgumentError("bad scope")),
			patch.object(frappe, "log_error"),
		):
			with self.assertRaises(InvalidArgumentError):
				agent_scheduler._stamp_scope_and_watermark("unused", inst, "Administrator")

	def test_malformed_legacy_config_keeps_the_existing_fallback(self):
		inst = frappe._dict(agent="fake-scope-agent", config="{broken")
		with (
			patch.object(agent_scope, "resolve_scope", side_effect=ValueError("bad config")),
			patch.object(frappe, "log_error"),
			patch.object(frappe.db, "set_value"),
		):
			self.assertIsNone(agent_scheduler._stamp_scope_and_watermark("unused", inst, "Administrator"))

	def test_comparison_and_record_scope_are_delivered_in_the_prompt(self):
		listing = frappe._dict(agent_slug="payroll-prerun-auditor", config_keys="[]")
		inst = frappe._dict(name="fake-install", config="{}")
		scope = {
			"company": "Fake Company",
			"from_date": "2026-09-01",
			"to_date": "2026-09-30",
			"prior_period_from": "2026-08-01",
			"prior_period_to": "2026-08-31",
			"record_scope": {"doctype": "Payroll Entry", "name": "Fake Batch"},
		}
		with patch.object(agent_scheduler, "_explicit_config", return_value={}):
			prompt = agent_scheduler._audit_prompt(listing, inst, "manual", scope)
		for value in (
			"prior_period_from",
			"2026-08-01",
			"prior_period_to",
			"2026-08-31",
			"record_scope",
			"Fake Batch",
		):
			self.assertIn(value, prompt)


class TestRecordScopedWriteback(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("record-scope-test@example.invalid")
		_mk_listing()
		cls.inst = _mk_installation(cls.owner)
		cls.company = frappe.db.get_value("Company", {}, "name")
		cls.a = {"doctype": "Jarvis Agent Listing", "name": SLUG}
		cls.b = {"doctype": "Jarvis Agent Installation", "name": cls.inst.name}
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_residue()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete(FINDING, {"agent": SLUG})

	def record(
		self,
		boundary,
		findings=True,
		claimed_boundary=None,
		stamp_values=None,
		payload_values=None,
		omit_clock=False,
		stamp_raw=None,
	):
		run = _mk_run(self.owner)
		stamp = {"company": self.company, **(stamp_values or {})}
		if boundary:
			stamp["record_scope"] = boundary
		frappe.db.set_value(
			RUN, run.name, "scope_json", stamp_raw if stamp_raw is not None else json.dumps(stamp)
		)
		run.reload()
		payload = {**stamp, **(payload_values or {})}
		if omit_clock:
			payload.pop("report_datetime", None)
		if claimed_boundary:
			payload["record_scope"] = claimed_boundary
		rows = (
			[
				{
					"token": TOK_A,
					"ref_doctype": "Company",
					"ref_name": self.company,
					"result_class": "observed_fact",
					"severity": "note",
					"note": "Fake observation",
				}
			]
			if findings
			else []
		)
		agent_runs.record_delegate_run(
			run, self.inst, rows, coverage={TOK_A: "evaluated", TOK_B: "evaluated"}, scope=payload
		)
		run.reload()
		return run

	def test_distinct_records_keep_distinct_finding_identities(self):
		self.record(self.a)
		self.record(self.b)
		rows = frappe.get_all(FINDING, {"agent": SLUG, "state": "open"}, ["fingerprint"])
		self.assertEqual(len(rows), 2)
		self.assertEqual(len({r.fingerprint for r in rows}), 2)

	def test_completed_other_record_does_not_close_first_record(self):
		first = self.record(self.a)
		self.record(self.b, findings=False)
		self.assertEqual(frappe.db.get_value(FINDING, {"run": first.name}, "state"), "open")

	def test_same_record_can_resolve_its_own_finding(self):
		first = self.record(self.a)
		self.record(self.a, findings=False)
		self.assertEqual(frappe.db.get_value(FINDING, {"run": first.name}, "state"), "resolved")

	def test_unscoped_run_does_not_close_a_record_scoped_finding(self):
		first = self.record(self.a)
		self.record(None, findings=False)
		self.assertEqual(frappe.db.get_value(FINDING, {"run": first.name}, "state"), "open")

	def test_delegate_cannot_change_the_launcher_boundary(self):
		first = self.record(self.a)
		run = self.record(self.a, claimed_boundary=self.b)
		self.assertEqual(json.loads(run.scope_json)["record_scope"], self.a)
		self.assertEqual(run.status, "partial")
		self.assertEqual(run.findings_count, 0)
		self.assertEqual(frappe.db.get_value(FINDING, {"run": first.name}, "state"), "open")

	def test_clock_legacy_omission_keeps_the_stamped_value(self):
		for boundary in (None, self.a):
			with self.subTest(boundary=boundary):
				run = self.record(
					boundary, stamp_values={"report_datetime": "2026-10-31 12:00:00.123456"}, omit_clock=True
				)
				self.assertEqual(run.status, "completed")
				self.assertEqual(run.findings_count, 1)
				self.assertEqual(json.loads(run.scope_json)["report_datetime"], "2026-10-31 12:00:00.123456")

	def test_wrong_clock_with_or_without_findings_cannot_close_an_open_finding(self):
		for boundary in (None, self.a):
			for has_findings in (True, False):
				with self.subTest(boundary=boundary, has_findings=has_findings):
					frappe.db.delete(FINDING, {"agent": SLUG})
					first = self.record(boundary)
					run = self.record(
						boundary,
						findings=has_findings,
						stamp_values={"report_datetime": "2026-10-31 12:00:00"},
						payload_values={"report_datetime": "2026-11-01 12:00:00"},
					)
					self.assertEqual(run.status, "partial")
					self.assertEqual(run.findings_count, 0)
					self.assertEqual(json.loads(run.scope_json)["report_datetime"], "2026-10-31 12:00:00")
					self.assertEqual(frappe.db.get_value(FINDING, {"run": first.name}, "state"), "open")

	def test_delegate_cannot_add_an_unstamped_clock(self):
		for boundary in (None, self.a):
			with self.subTest(boundary=boundary):
				run = self.record(boundary, payload_values={"report_datetime": "2026-10-31 12:00:00"})
				self.assertEqual(run.status, "partial")
				self.assertEqual(run.findings_count, 0)
				self.assertNotIn("report_datetime", json.loads(run.scope_json))

	def test_new_run_clock_does_not_change_finding_identity(self):
		first = self.record(self.a, stamp_values={"report_datetime": "2026-10-30 12:00:00"})
		self.record(self.a, findings=False, stamp_values={"report_datetime": "2026-10-31 12:00:00"})
		self.assertEqual(frappe.db.get_value(FINDING, {"run": first.name}, "state"), "resolved")

	def test_wrong_execution_identity_cannot_create_findings(self):
		for boundary in (None, self.a):
			run = self.record(
				boundary,
				stamp_values={"execution_user": self.owner},
				payload_values={"execution_user": "other@example.invalid"},
			)
			self.assertEqual(run.status, "partial")
			self.assertEqual(run.findings_count, 0)
			self.assertEqual(json.loads(run.scope_json)["execution_user"], self.owner)

	def test_corrupt_scope_cannot_accept_a_delegate_execution_identity(self):
		for raw in ("[]", "{broken"):
			run = self.record(None, stamp_raw=raw, payload_values={"execution_user": self.owner})
			self.assertEqual(run.status, "partial")
			self.assertEqual(run.findings_count, 0)
			self.assertNotIn("execution_user", json.loads(run.scope_json))

	def test_ordinary_run_cannot_change_the_stamped_date(self):
		run = self.record(
			None, stamp_values={"report_date": "2026-10-31"}, payload_values={"report_date": "2026-11-01"}
		)
		self.assertEqual(run.status, "partial")
		self.assertEqual(json.loads(run.scope_json)["report_date"], "2026-10-31")


class TestLaunchClock(FrappeTestCase):
	def test_report_date_and_datetime_use_one_site_instant(self):
		instant = datetime(2026, 10, 31, 23, 59, 59, 123456)
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(agent_scope, "_prior_fiscal_year", return_value=None),
			patch.object(frappe.utils, "now_datetime", return_value=instant) as clock,
		):
			scope = agent_scope._resolve("Fake Company", None, "2026-01-01", "2026-12-31")
		self.assertEqual(scope["report_date"], "2026-10-31")
		self.assertEqual(scope["report_datetime"], "2026-10-31 23:59:59.123456")
		clock.assert_called_once_with()
		self.assertEqual(scope["execution_user"], frappe.session.user)

	def test_prompt_delivers_exact_site_datetime(self):
		listing = frappe._dict(agent_slug="fake-scope-agent", config_keys="[]")
		inst = frappe._dict(name="fake-install", config="{}")
		stamp = "2026-10-31 23:59:59.123456"
		prompt = agent_scheduler._audit_prompt(
			listing, inst, "manual", {"company": "Fake Company", "report_datetime": stamp}
		)
		self.assertIn('report_datetime="' + stamp + '"', prompt)

	def test_legacy_prompt_does_not_invent_a_clock(self):
		listing = frappe._dict(agent_slug="fake-scope-agent", config_keys="[]")
		inst = frappe._dict(name="fake-install", config="{}")
		for value in ({}, {"report_datetime": None}):
			prompt = agent_scheduler._audit_prompt(
				listing, inst, "manual", {"company": "Fake Company", **value}
			)
			self.assertNotIn("report_datetime=", prompt)
