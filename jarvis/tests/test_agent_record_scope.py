"""Keep record-scoped assessments apart across real writeback transactions."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_runs, agent_scheduler, agent_scope, agents_api
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

	def record(self, boundary, findings=True, claimed_boundary=None):
		run = _mk_run(self.owner)
		stamp = {"company": self.company}
		if boundary:
			stamp["record_scope"] = boundary
		frappe.db.set_value(RUN, run.name, "scope_json", json.dumps(stamp))
		run.reload()
		payload = dict(stamp)
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

	def test_run_drilldown_does_not_include_a_different_record_between_detections(self):
		self.record(self.a)
		middle = self.record(self.b, findings=False)
		last = self.record(self.a)
		frappe.get_doc("User", self.owner).add_roles("Jarvis User")
		frappe.set_user(self.owner)
		try:
			self.assertEqual(agents_api.list_findings(run=middle.name)["total"], 0)
			self.assertEqual(agents_api.list_findings(run=last.name)["total"], 1)
		finally:
			frappe.set_user("Administrator")

	def test_delegate_cannot_change_the_launcher_boundary(self):
		first = self.record(self.a)
		run = self.record(self.a, claimed_boundary=self.b)
		self.assertEqual(json.loads(run.scope_json)["record_scope"], self.a)
		self.assertEqual(run.status, "partial")
		self.assertEqual(run.findings_count, 0)
		self.assertEqual(frappe.db.get_value(FINDING, {"run": first.name}, "state"), "open")
