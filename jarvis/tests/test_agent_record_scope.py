"""Keep record-scoped assessments apart across real writeback transactions."""

import json
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
		listing = frappe._dict(agent_slug="payroll-prerun-auditor", config_keys="[]")
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


class TestRecordScopeCapability(FrappeTestCase):
	"""A record boundary is accepted only from the bundled registry contract."""

	UNSUPPORTED = "baseline-meter-auditor"
	SUPPORTED = "payroll-prerun-auditor"

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
	def _listing(slug):
		return frappe._dict(agent_slug=slug, status="Published", title="Scope test", config_keys="[]")

	def test_registry_capability_reads_the_bundled_artifact_not_a_listing(self):
		with patch.object(
			agent_catalog,
			"_load_registry",
			return_value={
				"agents": [
					{"agent_slug": self.UNSUPPORTED, "supports_record_scope": False},
					{"agent_slug": self.SUPPORTED, "supports_record_scope": True},
				]
			},
		):
			self.assertFalse(agent_catalog.registry_supports_record_scope(self.UNSUPPORTED))
			self.assertTrue(agent_catalog.registry_supports_record_scope(self.SUPPORTED))
			self.assertFalse(agent_catalog.registry_supports_record_scope("missing-agent"))

	def test_unsupported_scope_is_refused_before_config_save(self):
		original = frappe.as_json({"company": "Fake Company"})
		inst = self._installation("SCOPE-CONFIG", self.UNSUPPORTED, original)
		with (
			patch.object(frappe, "get_doc", return_value=inst),
			patch.object(agents_api, "registry_supports_record_scope", return_value=False),
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

	def test_unsupported_scope_is_refused_before_schedule_save(self):
		inst = self._installation(
			"SCOPE-SCHEDULE",
			self.UNSUPPORTED,
			frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}}),
		)
		with (
			patch.object(frappe, "get_doc", return_value=inst),
			patch.object(agents_api, "registry_supports_record_scope", return_value=False),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "does not support record-scoped"):
				agents_api.set_schedule(inst.name, schedule_enabled=1)
		inst.save.assert_not_called()

	def test_unsupported_scope_is_refused_before_editing_an_enabled_schedule(self):
		inst = self._installation(
			"SCOPE-SCHEDULE-EDIT",
			self.UNSUPPORTED,
			frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}}),
		)
		inst.schedule_enabled = 1
		with (
			patch.object(frappe, "get_doc", return_value=inst),
			patch.object(agents_api, "registry_supports_record_scope", return_value=False),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "does not support record-scoped"):
				agents_api.set_schedule(inst.name, schedule_time="10:00")
		inst.save.assert_not_called()

	def test_unsupported_scope_can_disable_an_enabled_schedule_for_recovery(self):
		inst = self._installation(
			"SCOPE-SCHEDULE-DISABLE",
			self.UNSUPPORTED,
			frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}}),
		)
		inst.schedule_enabled = 1
		with (
			patch.object(frappe, "get_doc", return_value=inst),
			patch.object(agents_api, "log_activity"),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "get_value", return_value="Scope test"),
			patch.object(
				agents_api,
				"registry_supports_record_scope",
				side_effect=AssertionError("disabling must not need the registry"),
			) as supports,
		):
			result = agents_api.set_schedule(inst.name, schedule_enabled=0)
		self.assertEqual(result["data"]["next_run_at"], None)
		self.assertEqual(inst.schedule_enabled, 0)
		inst.save.assert_called_once_with()
		supports.assert_not_called()

	def test_unsupported_scope_is_refused_before_manual_launch_side_effects(self):
		inst = self._installation(
			"SCOPE-LAUNCH",
			self.UNSUPPORTED,
			frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}}),
		)
		listing = self._listing(self.UNSUPPORTED)
		registry_tools = MagicMock(side_effect=AssertionError("must not reach tool contract"))
		with (
			patch.object(frappe, "get_doc", return_value=listing),
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch.object(agent_scheduler, "_explicit_config", return_value={}),
			patch.object(agent_scheduler, "_resolve_initiating_human", return_value=None),
			patch("jarvis.chat.agent_catalog.registry_supports_record_scope", return_value=False),
			patch("jarvis.chat.agent_catalog.registry_tools_allow", registry_tools),
			patch.object(frappe.db, "savepoint") as savepoint,
			patch("jarvis.admin_client.post_agent_run") as dispatch,
		):
			with self.assertRaisesRegex(frappe.ValidationError, "does not support record-scoped"):
				agent_scheduler._launch_audit(inst, trigger="scheduled")
		registry_tools.assert_not_called()
		savepoint.assert_not_called()
		dispatch.assert_not_called()

	def test_unsupported_scope_is_refused_before_manual_entrypoint_persists_source_apps(self):
		inst = self._installation(
			"SCOPE-MANUAL-ENTRY",
			"custom-app-learning",
			frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}}),
		)
		inst.enabled = 1
		inst.activation_state = "live"
		listing = frappe._dict(nature="Scribe", status="Published")
		with (
			patch.object(frappe, "get_doc", return_value=inst),
			patch.object(agents_api, "_agent_is_withdrawn", return_value=False),
			patch.object(agents_api, "_user_allowed_for_agent", return_value=True),
			patch("jarvis.chat.agent_scheduler.run_as_mapping_refusal", return_value=None),
			patch("jarvis.chat.agent_installability.assert_installable") as installable,
			patch.object(frappe.db, "get_value", return_value=listing),
			patch("jarvis.chat.agent_models.gate_run", return_value={"ok": True}) as model_gate,
			patch.object(agents_api, "registry_supports_record_scope", return_value=False),
			patch.object(frappe.db, "set_value") as set_value,
			patch("jarvis.chat.agent_scheduler._launch_audit") as launch,
		):
			with self.assertRaisesRegex(frappe.ValidationError, "does not support record-scoped"):
				agents_api.run_agent_now(inst.name, options={"source_apps": ["example_app"]})
		installable.assert_not_called()
		model_gate.assert_not_called()
		set_value.assert_not_called()
		launch.assert_not_called()

	def test_unsupported_scope_is_refused_before_scheduled_dispatch(self):
		row = frappe._dict(
			name="SCOPE-SCHEDULED",
			owner="scope-owner@example.invalid",
			run_as_user="scope-runas@example.invalid",
			agent=self.UNSUPPORTED,
			schedule_frequency="daily",
			schedule_time="00:00",
			installable=1,
			config=frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}}),
			activation_state="live",
		)
		listing = frappe._dict(nature="Auditor", status="Published", operator_visibility="available")
		with (
			patch.object(frappe.db, "get_value", return_value=listing),
			patch("jarvis.chat.agent_catalog.registry_supports_record_scope", return_value=False),
			patch.object(agent_scheduler, "_record_failed") as record_failed,
			patch.object(agent_scheduler, "_advance") as advance,
			patch.object(agent_scheduler, "_dispatch") as dispatch,
		):
			agent_scheduler._sweep_one(row, object(), set())
		record_failed.assert_called_once()
		self.assertIn("does not support record-scoped", record_failed.call_args.args[1])
		advance.assert_called_once()
		dispatch.assert_not_called()

	def test_supported_scope_is_checked_and_injected(self):
		boundary = {"doctype": "Payroll Entry", "name": "Fake Payroll"}
		inst = self._installation(
			"SCOPE-SUPPORTED",
			self.SUPPORTED,
			frappe.as_json({"company": "Fake Company", "record_scope": boundary}),
		)
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

		with patch("jarvis.chat.agent_catalog.registry_tools_allow", return_value=[]):
			prompt = agent_scheduler._audit_prompt(
				self._listing(self.SUPPORTED),
				inst,
				"manual",
				scope,
				engagement_config={},
				record_scope_supported=True,
			)
		self.assertIn('"record_scope"', prompt)
		self.assertIn('"Payroll Entry"', prompt)

	def test_failed_supported_record_scope_does_not_fall_back_to_a_wider_run(self):
		inst = self._installation(
			"SCOPE-FAILED-BOUNDARY",
			self.SUPPORTED,
			frappe.as_json({"record_scope": {"doctype": "Payroll Entry", "name": "Unreadable Payroll"}}),
		)
		with (
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch.object(
				agent_scope,
				"resolve_scope",
				side_effect=InvalidArgumentError("record scope is not readable"),
			),
			patch.object(frappe, "log_error") as log_error,
			patch.object(frappe.db, "set_value") as set_value,
		):
			with self.assertRaisesRegex(InvalidArgumentError, "not readable"):
				agent_scheduler._stamp_scope_and_watermark(
					"SCOPE-RUN",
					inst,
					"scope-runas@example.invalid",
					record_scope_supported=True,
				)
		set_value.assert_not_called()
		self.assertEqual(
			log_error.call_args.kwargs["title"], "jarvis agent: explicit record scope resolution failed"
		)

	def test_generic_resolver_refuses_undeclared_scope_and_plain_config_still_saves(self):
		inst = self._installation(
			"SCOPE-RESOLVER",
			self.UNSUPPORTED,
			frappe.as_json({"record_scope": {"doctype": "Company", "name": "Fake Company"}}),
		)
		with self.assertRaisesRegex(InvalidArgumentError, "not supported"):
			agent_scope.resolve_scope(inst)

		plain = self._installation("SCOPE-PLAIN", self.UNSUPPORTED)
		payload = {"company": "Fake Company", "materiality": {"percentage": 5}}
		with (
			patch.object(frappe, "get_doc", return_value=plain),
			patch.object(agents_api, "registry_supports_record_scope", return_value=False),
			patch.object(agents_api, "log_activity"),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "get_value", return_value="Scope test"),
		):
			agents_api.set_config(plain.name, frappe.as_json(payload))
		self.assertEqual(frappe.parse_json(plain.config), payload)
		plain.save.assert_called_once_with()

	def test_unsupported_scope_stays_out_of_the_prompt_without_changing_snapshot_delivery(self):
		scope = {
			"company": "Fake Company",
			"record_scope": {"doctype": "Company", "name": "Fake Company"},
		}
		snapshot = {"depr_run_posted": False, "enable_capex_signal": False}
		with patch("jarvis.chat.agent_catalog.registry_tools_allow", return_value=[]):
			prompt = agent_scheduler._audit_prompt(
				self._listing("fixed-asset-depreciation-auditor"),
				self._installation("FA-SNAPSHOT", "fixed-asset-depreciation-auditor"),
				"manual",
				scope,
				engagement_config=snapshot,
				record_scope_supported=False,
			)
		self.assertNotIn('"record_scope"', prompt)
		self.assertIn('"depr_run_posted": false', prompt)
		self.assertIn('"enable_capex_signal": false', prompt)
