"""Record-scope capability boundaries for delegate agents."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_catalog, agent_scheduler, agent_scope, agents_api
from jarvis.exceptions import InvalidArgumentError


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
