"""Keep record-scoped assessments apart across real writeback transactions."""

import json
from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_catalog, agent_runs, agent_scheduler, agent_scope, agents_api
from jarvis.chat.agent_visibility import make_visibility_receipt
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
			return agent_scope.resolve_scope(inst, allow_record_scope=True)

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
		listing = frappe._dict(agent_slug="payroll-prerun-auditor", config_keys="[]", supports_record_scope=1)
		inst = frappe._dict(name="fake-install", config="{}")
		scope = {
			"company": "Fake Company",
			"from_date": "2026-09-01",
			"to_date": "2026-09-30",
			"prior_period_from": "2026-08-01",
			"prior_period_to": "2026-08-31",
			"record_scope": {"doctype": "Payroll Entry", "name": "Fake Batch"},
			"scoped_visibility": True,
		}
		with patch.object(agent_scheduler, "_explicit_config", return_value={}):
			prompt = agent_scheduler._audit_prompt(
				listing, inst, "manual", scope, record_scope_supported=True
			)
		for value in (
			"prior_period_from",
			"2026-08-01",
			"prior_period_to",
			"2026-08-31",
			"record_scope",
			"Fake Batch",
			"scoped_visibility",
		):
			self.assertIn(value, prompt)

	def test_exact_false_scoped_visibility_is_delivered_but_malformed_values_are_omitted(self):
		listing = frappe._dict(agent_slug="payroll-prerun-auditor", config_keys="[]")
		inst = frappe._dict(name="fake-install", config="{}")
		with patch.object(agent_scheduler, "_explicit_config", return_value={}):
			prompt = agent_scheduler._audit_prompt(listing, inst, "manual", {"scoped_visibility": False})
		self.assertIn('"scoped_visibility": false', prompt)
		for malformed in (None, 0, 1, "false"):
			with (
				self.subTest(malformed=malformed),
				patch.object(agent_scheduler, "_explicit_config", return_value={}),
			):
				prompt = agent_scheduler._audit_prompt(
					listing, inst, "manual", {"scoped_visibility": malformed}
				)
				self.assertNotIn("scoped_visibility", prompt)

	def test_fresh_permission_profile_marks_scope_as_sliced(self):
		inst = frappe._dict(agent="payroll-prerun-auditor", config="{}", scoped_visibility=0)
		profile = json.dumps({"user_permissions": {"Employee": ["EMP-1"]}})
		resolved = {"company": "Fake Company", "to_date": "2026-09-30"}
		with (
			patch.object(agent_scope, "resolve_scope", return_value=resolved),
			patch.object(agent_scheduler, "_permission_profile", return_value=profile),
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch.object(frappe.db, "sql", return_value=[frappe._dict(n=0, m=None)]),
			patch.object(frappe.db, "set_value"),
		):
			stamped = agent_scheduler._stamp_scope_and_watermark("unused", inst, "test@example.invalid")
		self.assertTrue(stamped["scoped_visibility"])
		listing = frappe._dict(agent_slug="payroll-prerun-auditor", config_keys="[]")
		with patch.object(agent_scheduler, "_explicit_config", return_value={}):
			prompt = agent_scheduler._audit_prompt(listing, inst, "manual", stamped)
		self.assertIn("scoped_visibility", prompt)

	def test_unknown_payroll_profile_omits_the_visibility_flag(self):
		inst = frappe._dict(agent="payroll-prerun-auditor", config="{}", scoped_visibility=0)
		resolved = {"company": "Fake Company", "to_date": "2026-09-30"}
		with (
			patch.object(agent_scope, "resolve_scope", return_value=resolved),
			patch.object(agent_scheduler, "_permission_profile", return_value="not json"),
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch.object(frappe.db, "sql", return_value=[frappe._dict(n=0, m=None)]),
			patch.object(frappe.db, "set_value"),
		):
			stamped = agent_scheduler._stamp_scope_and_watermark("unused", inst, "test@example.invalid")
		self.assertNotIn("scoped_visibility", stamped)

	def test_non_payroll_keeps_the_legacy_installation_fallback(self):
		profile_without_a_gl_slice = json.dumps({"user_permissions": {}})
		legacy = frappe._dict(agent="not-a-bundled-agent", scoped_visibility=1)
		payroll = frappe._dict(agent="payroll-prerun-auditor", scoped_visibility=1)
		self.assertTrue(agent_scheduler._scope_has_scoped_visibility(profile_without_a_gl_slice, legacy))
		self.assertFalse(agent_scheduler._scope_has_scoped_visibility(profile_without_a_gl_slice, payroll))

	def test_scheduler_receipt_uses_its_canonical_permission_profile(self):
		"""The receipt is derived from the scheduler's real profile serializer."""
		inst = frappe._dict(agent="baseline-meter-auditor-hrms", config="{}", scoped_visibility=0)
		resolved = {"company": "Fake Company", "to_date": "2026-09-30"}
		with (
			patch.object(agent_scope, "resolve_scope", return_value=resolved),
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch.object(agent_catalog, "registry_visibility_domain", return_value="hrms-payroll"),
			patch.object(frappe, "get_roles", return_value=["HR User"]),
			patch(
				"frappe.permissions.get_user_permissions", return_value={"Department": [{"doc": "Finance"}]}
			),
			patch.object(frappe.db, "sql", return_value=[frappe._dict(n=0, m=None)]),
			patch.object(frappe.db, "set_value") as set_value,
		):
			agent_scheduler._stamp_scope_and_watermark("unused", inst, "test@example.invalid")
		values = set_value.call_args.args[2]
		profile = values["permission_profile"]
		receipt = json.loads(values["visibility_receipt_json"])
		self.assertEqual(receipt, make_visibility_receipt(profile, "hrms-payroll"))
		self.assertEqual(receipt["state"], "sliced")
		# The receipt and its state are absent from model-visible scope_json.
		self.assertNotIn("visibility_receipt", json.loads(values["scope_json"]))
		self.assertNotIn("scoped_visibility", json.loads(values["scope_json"]))

	def test_review_backend_receipt_stays_out_of_delegate_scope(self):
		"""The operator-review scheduler branch has the same server-only rule."""
		inst = frappe._dict(agent="baseline-meter-auditor-hrms", config="{}", scoped_visibility=0)
		backend = Mock()
		backend.scope_from_config.return_value = {"scoped_visibility": True, "review": "safe"}
		with (
			patch("jarvis.chat.operator_review.backend", return_value=backend),
			patch.object(agent_catalog, "registry_visibility_domain", return_value="hrms-payroll"),
			patch.object(frappe.db, "get_value", return_value="{}"),
			patch.object(frappe, "get_roles", return_value=["HR User"]),
			patch(
				"frappe.permissions.get_user_permissions", return_value={"Department": [{"doc": "Finance"}]}
			),
			patch.object(frappe.db, "set_value") as set_value,
		):
			agent_scheduler._stamp_scope_and_watermark("unused", inst, "test@example.invalid")
		values = set_value.call_args.args[2]
		scope = json.loads(values["scope_json"])
		self.assertEqual(scope, {"review": "safe"})
		self.assertEqual(json.loads(values["visibility_receipt_json"])["state"], "sliced")


class TestPayrollVisibilityAutoResolve(FrappeTestCase):
	def test_payroll_auto_resolve_needs_two_exact_false_values(self):
		inst = frappe._dict(agent="payroll-prerun-auditor", scoped_visibility=0)
		profile_false = json.dumps({"user_permissions": {}})

		def run(profile, persisted):
			scope = {} if persisted is None else {"scoped_visibility": persisted}
			return frappe._dict(
				agent="payroll-prerun-auditor",
				permission_profile=profile,
				scope_json=json.dumps(scope),
			)

		self.assertFalse(agent_runs._scoped_visibility(run(profile_false, False), inst))
		for profile, persisted in (
			(None, False),
			("not json", False),
			(profile_false, None),
			(profile_false, 0),
			(profile_false, True),
			(json.dumps({"user_permissions": {"Department": ["Finance"]}}), False),
		):
			with self.subTest(profile=profile, persisted=persisted):
				self.assertTrue(agent_runs._scoped_visibility(run(profile, persisted), inst))

	def test_reviewed_receipt_rechecks_the_same_profile_and_fails_closed_on_change(self):
		inst = frappe._dict(agent="baseline-meter-auditor-hrms", scoped_visibility=0)
		# Use the canonical scheduler serializer to avoid a test-only receipt shape.
		with (
			patch.object(frappe, "get_roles", return_value=["HR User"]),
			patch("frappe.permissions.get_user_permissions", return_value={}),
		):
			profile = agent_scheduler._permission_profile("test@example.invalid")
		with (
			patch.object(frappe, "get_roles", return_value=["HR User"]),
			patch(
				"frappe.permissions.get_user_permissions", return_value={"Department": [{"doc": "Finance"}]}
			),
		):
			changed_profile = agent_scheduler._permission_profile("test@example.invalid")
		receipt = make_visibility_receipt(profile, "hrms-payroll")
		run = frappe._dict(
			agent="baseline-meter-auditor-hrms",
			permission_profile=profile,
			visibility_receipt_json=json.dumps(receipt),
			session_key="agent:receipt:test",
		)
		with (
			patch.object(agent_catalog, "registry_visibility_domain", return_value="hrms-payroll"),
			patch.object(agent_runs, "_run_session_user", return_value="test@example.invalid"),
			patch.object(agent_scheduler, "_permission_profile", return_value=profile),
		):
			self.assertEqual(agent_runs.visibility_receipt_state(run, inst), "complete")
			self.assertFalse(agent_runs._scoped_visibility(run, inst))
		with (
			patch.object(agent_catalog, "registry_visibility_domain", return_value="hrms-payroll"),
			patch.object(agent_runs, "_run_session_user", return_value="test@example.invalid"),
			patch.object(agent_scheduler, "_permission_profile", return_value=changed_profile),
		):
			self.assertEqual(agent_runs.visibility_receipt_state(run, inst), "unknown")
			self.assertTrue(agent_runs._scoped_visibility(run, inst))
		# The policy selector is mutable catalog data.  Once a run has a receipt,
		# removing that selector must fail closed rather than revive the old
		# measurement-domain classifier.
		with patch.object(agent_catalog, "registry_visibility_domain", return_value=None):
			self.assertEqual(agent_runs.visibility_receipt_state(run, inst), "unknown")
			self.assertTrue(agent_runs._scoped_visibility(run, inst))


class TestRecordScopeCapability(FrappeTestCase):
	"""The bundle declaration must gate config, launch, resolution, and prompt delivery."""

	BASELINE = "baseline-meter-auditor"
	SCOPED = "payroll-prerun-auditor"

	@staticmethod
	def _installation(name, agent, config="{}"):
		doc = frappe._dict(
			name=name,
			agent=agent,
			owner="scope-owner@example.invalid",
			run_as_user="scope-runas@example.invalid",
			reviewer="scope-reviewer@example.invalid",
			config=config,
		)
		doc.check_permission = MagicMock()
		doc.save = MagicMock()
		return doc

	@staticmethod
	def _listing(slug, supports_record_scope):
		return frappe._dict(
			agent_slug=slug,
			status="Published",
			config_keys="[]",
			supports_record_scope=supports_record_scope,
		)

	def _config_docs(self, installation, listing):
		def get_doc(doctype, name=None, *args, **kwargs):
			if doctype == "Jarvis Agent Installation" and name == installation.name:
				return installation
			if doctype == "Jarvis Agent Listing" and name == installation.agent:
				return listing
			raise AssertionError(f"unexpected document lookup: {doctype} {name}")

		return get_doc

	def test_baseline_rejects_record_scope_before_config_save(self):
		original = frappe.as_json({"company": "Fake Company"})
		inst = self._installation("BASELINE-CONFIG", self.BASELINE, original)
		# A listing is mutable admin state. A manual true value must not override
		# the bundled Baseline declaration.
		listing = self._listing(self.BASELINE, supports_record_scope=1)
		with (
			patch.object(frappe, "get_doc", side_effect=self._config_docs(inst, listing)),
			patch.object(agents_api, "log_activity") as activity,
			patch.object(frappe.db, "commit") as commit,
		):
			with self.assertRaisesRegex(frappe.ValidationError, "does not support record-scoped"):
				agents_api.set_config(
					inst.name,
					frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}}),
				)
		self.assertEqual(inst.config, original)
		inst.save.assert_not_called()
		activity.assert_not_called()
		commit.assert_not_called()

	def test_baseline_schedule_enable_rejects_record_scope_before_save(self):
		inst = self._installation(
			"BASELINE-SCHEDULE",
			self.BASELINE,
			frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}}),
		)
		with patch.object(frappe, "get_doc", side_effect=self._config_docs(inst, None)):
			with self.assertRaisesRegex(frappe.ValidationError, "does not support record-scoped"):
				agents_api.set_schedule(inst.name, schedule_enabled=1)
		inst.save.assert_not_called()

	def test_baseline_rejects_stale_record_scope_before_launch_side_effects(self):
		inst = self._installation(
			"BASELINE-LAUNCH",
			self.BASELINE,
			frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}}),
		)
		listing = self._listing(self.BASELINE, supports_record_scope=1)
		before = frappe.db.count("Jarvis Agent Run")
		registry_tools = MagicMock(side_effect=AssertionError("must not reach capability gate"))
		with (
			patch.object(frappe, "get_doc", return_value=listing),
			patch("jarvis.chat.agent_catalog.registry_tools_allow", registry_tools),
			patch.object(frappe.db, "savepoint") as savepoint,
			patch("jarvis.admin_client.post_agent_run") as dispatch,
		):
			with self.assertRaisesRegex(frappe.ValidationError, "does not support record-scoped"):
				agent_scheduler._launch_audit(inst, trigger="scheduled")
		self.assertEqual(frappe.db.count("Jarvis Agent Run"), before)
		registry_tools.assert_not_called()
		savepoint.assert_not_called()
		dispatch.assert_not_called()

	def test_scheduled_baseline_scope_never_reaches_dispatch(self):
		row = frappe._dict(
			name="BASELINE-SCHEDULED",
			owner="scope-owner@example.invalid",
			run_as_user="scope-runas@example.invalid",
			agent=self.BASELINE,
			schedule_frequency="daily",
			schedule_time="00:00",
			installable=1,
			config=frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}}),
			activation_state="live",
		)
		listing = frappe._dict(nature="Auditor", status="Published", operator_visibility="available")
		with (
			patch.object(frappe.db, "get_value", return_value=listing),
			patch.object(agent_scheduler, "_record_failed") as record_failed,
			patch.object(agent_scheduler, "_advance") as advance,
			patch.object(agent_scheduler, "_dispatch") as dispatch,
		):
			agent_scheduler._sweep_one(row, object(), set())
		record_failed.assert_called_once()
		self.assertIn("does not support record-scoped", record_failed.call_args.args[1])
		advance.assert_called_once()
		dispatch.assert_not_called()

	def test_supported_agent_accepts_and_injects_record_scope(self):
		inst = self._installation("SCOPED-CONFIG", self.SCOPED)
		# A stale listing must not disable a bundle that explicitly supports scope.
		listing = self._listing(self.SCOPED, supports_record_scope=0)
		boundary = {"doctype": "Payroll Entry", "name": "Fake Payroll"}
		payload = {"company": "Fake Company", "record_scope": boundary}
		with (
			patch.object(frappe, "get_doc", side_effect=self._config_docs(inst, listing)),
			patch.object(agents_api, "log_activity"),
			patch.object(frappe.db, "commit"),
		):
			agents_api.set_config(inst.name, frappe.as_json(payload))
		self.assertEqual(frappe.parse_json(inst.config), payload)
		inst.save.assert_called_once_with()

		record = MagicMock()
		record.get.return_value = None
		with (
			patch.object(
				agent_scope,
				"_resolve",
				return_value={"company": "Fake Company", "from_date": "2026-09-01", "to_date": "2026-09-30"},
			),
			patch.object(frappe, "get_doc", return_value=record),
		):
			scope = agent_scope.resolve_scope(inst, allow_record_scope=True)
		self.assertEqual(scope["record_scope"], boundary)
		record.check_permission.assert_called_once_with("read")

	def test_prompt_never_delivers_record_scope_without_launch_capability(self):
		listing = self._listing(self.BASELINE, supports_record_scope=1)
		inst = frappe._dict(name="BASELINE-PROMPT", config="{}")
		scope = {
			"company": "Fake Company",
			"record_scope": {"doctype": "Company", "name": "Fake Company"},
		}
		with patch.object(agent_scheduler, "_explicit_config", return_value={}):
			prompt = agent_scheduler._audit_prompt(
				listing, inst, "manual", scope, record_scope_supported=False
			)
		self.assertNotIn("record_scope", prompt)

	def test_undeclared_scope_is_refused_by_the_generic_resolver(self):
		inst = frappe._dict(
			config=frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}})
		)
		with self.assertRaisesRegex(InvalidArgumentError, "not supported"):
			agent_scope.resolve_scope(inst)

	def test_normal_agent_config_without_record_scope_still_saves(self):
		inst = self._installation("NORMAL-CONFIG", "close-auditor")
		listing = self._listing("close-auditor", supports_record_scope=0)
		payload = {"company": "Fake Company", "materiality": {"percentage": 5}}
		with (
			patch.object(frappe, "get_doc", side_effect=self._config_docs(inst, listing)),
			patch.object(agents_api, "log_activity"),
			patch.object(frappe.db, "commit"),
		):
			agents_api.set_config(inst.name, frappe.as_json(payload))
		self.assertEqual(frappe.parse_json(inst.config), payload)
		inst.save.assert_called_once_with()


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
