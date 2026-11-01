"""Server-bound Warehouse scope regressions for the negative-stock auditor."""

from __future__ import annotations

import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_scheduler, agent_scope, operator_review
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import record_agent_run as writeback

AGENT = agent_scope.NEGATIVE_STOCK_AGENT
DASHBOARD = "Jarvis Dashboard"
CONVERSATION = "Jarvis Conversation"
FINDING = "Jarvis Agent Finding"
BASE_SCOPE = {
	"company": "Scope Company",
	"fiscal_year": "2026-2027",
	"from_date": "2026-10-01",
	"to_date": "2026-10-31",
	"report_date": "2026-10-31",
}
_DEFAULT_WAREHOUSE = object()


class TestNegativeStockScope(FrappeTestCase):
	owner = "negative-stock-scope-test@example.invalid"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from jarvis.permissions import ensure_jarvis_user_role

		frappe.set_user("Administrator")
		ensure_jarvis_user_role()
		if not frappe.db.exists("User", cls.owner):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": cls.owner,
					"first_name": "Negative Stock Scope",
					"send_welcome_email": 0,
					"enabled": 1,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		if "Jarvis User" not in frappe.get_roles(cls.owner):
			frappe.get_doc("User", cls.owner).add_roles("Jarvis User")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._reset_live_fixture()
		if frappe.db.exists("User", cls.owner):
			frappe.delete_doc("User", cls.owner, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		self._reset_live_fixture()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._reset_live_fixture()
		super().tearDown()

	@classmethod
	def _reset_live_fixture(cls):
		for doctype in (DASHBOARD, FINDING, writeback.RUN):
			for name in frappe.get_all(doctype, filters={"owner": cls.owner}, pluck="name"):
				frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
		for name in frappe.get_all(CONVERSATION, filters={"owner": cls.owner}, pluck="name"):
			frappe.delete_doc(CONVERSATION, name, ignore_permissions=True, force=True)
		for name in frappe.get_all(writeback.INSTALLATION, filters={"owner": cls.owner}, pluck="name"):
			frappe.delete_doc(writeback.INSTALLATION, name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def live_run(
		self,
		*,
		session_key: str,
		warehouse_name: str = "Main Warehouse Group",
		reviewer: str | None = None,
		activation_state: str = "live",
	):
		"""Build the real save-dashboard/writeback path without a model turn."""
		from jarvis.chat import agent_catalog

		if not frappe.db.exists(writeback.LISTING, AGENT):
			agent_catalog.sync_agent_listings()
		installation = frappe.get_doc(
			{
				"doctype": writeback.INSTALLATION,
				"name": frappe.generate_hash(length=12),
				"agent": AGENT,
				"enabled": 1,
				"run_as_user": self.owner,
				"reviewer": reviewer or self.owner,
				"activation_state": activation_state,
				"schedule_enabled": 0,
				"config": frappe.as_json({}),
			}
		)
		installation.owner = self.owner
		installation.flags.ignore_permissions = True
		previous_user = frappe.session.user
		frappe.set_user(self.owner)
		try:
			installation.db_insert()
		finally:
			frappe.set_user(previous_user)
		conversation = frappe.get_doc(
			{
				"doctype": CONVERSATION,
				"title": "Negative-stock writeback scope regression",
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(CONVERSATION, conversation.name, "owner", self.owner, update_modified=False)
		scope = {
			**BASE_SCOPE,
			"record_scope": {"doctype": "Warehouse", "name": warehouse_name},
		}
		run = frappe.get_doc(
			{
				"doctype": writeback.RUN,
				"agent": AGENT,
				"installation": installation.name,
				"trigger": "manual",
				"status": "running",
				"preparation_mode": activation_state,
				"conversation": conversation.name,
				"session_key": session_key,
				"started_at": frappe.utils.now(),
				"scope_json": frappe.as_json(scope),
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(writeback.RUN, run.name, "owner", self.owner, update_modified=False)
		return installation, conversation, run, scope

	def run_for_installation(self, installation, *, session_key: str, scope: dict):
		"""Create another running snapshot without violating owner/agent uniqueness."""
		conversation = frappe.get_doc(
			{
				"doctype": CONVERSATION,
				"title": "Negative-stock writeback scope regression",
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(CONVERSATION, conversation.name, "owner", self.owner, update_modified=False)
		run = frappe.get_doc(
			{
				"doctype": writeback.RUN,
				"agent": AGENT,
				"installation": installation.name,
				"trigger": "manual",
				"status": "running",
				"preparation_mode": installation.get("activation_state") or "live",
				"conversation": conversation.name,
				"session_key": session_key,
				"started_at": frappe.utils.now(),
				"scope_json": frappe.as_json(scope),
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(writeback.RUN, run.name, "owner", self.owner, update_modified=False)
		return conversation, run

	def installation(self, config):
		return frappe._dict(
			name="NEGATIVE-STOCK-SCOPE-1",
			agent=AGENT,
			config=frappe.as_json(config),
		)

	def configuration(self, record_scope=None):
		negative_stock = {}
		if record_scope is not None:
			negative_stock["record_scope"] = record_scope
		return {
			"company": BASE_SCOPE["company"],
			"from_date": BASE_SCOPE["from_date"],
			"to_date": BASE_SCOPE["to_date"],
			"negative_stock": negative_stock,
		}

	def resolve(self, config, warehouse=_DEFAULT_WAREHOUSE, readable=True, company_readable=True):
		warehouse = (
			warehouse
			if warehouse is not _DEFAULT_WAREHOUSE
			else frappe._dict(company=BASE_SCOPE["company"], disabled=0, is_group=1)
		)
		with (
			mock.patch.object(agent_scope, "_resolve", return_value=dict(BASE_SCOPE)),
			mock.patch.object(frappe.db, "get_value", return_value=warehouse) as get_value,
			mock.patch.object(
				frappe, "has_permission", side_effect=(readable, company_readable)
			) as has_permission,
		):
			scope = agent_scope.resolve_scope(self.installation(config))
		return scope, get_value, has_permission

	def test_valid_group_warehouse_is_stamped_as_the_only_record_scope(self):
		scope, get_value, has_permission = self.resolve(
			self.configuration({"doctype": "Warehouse", "name": "Main Warehouse Group"})
		)

		self.assertEqual(scope["record_scope"], {"doctype": "Warehouse", "name": "Main Warehouse Group"})
		get_value.assert_called_once_with(
			"Warehouse", "Main Warehouse Group", ["company", "disabled"], as_dict=True
		)
		has_permission.assert_has_calls(
			[
				mock.call("Warehouse", "read", doc="Main Warehouse Group"),
				mock.call("Company", "read", doc=BASE_SCOPE["company"]),
			]
		)

	def test_missing_or_invalid_warehouse_never_becomes_an_unscoped_review(self):
		valid = {"doctype": "Warehouse", "name": "Main Warehouse Group"}
		cases = (
			self.configuration(),
			self.configuration({"doctype": "Warehouse"}),
			self.configuration({"doctype": "Company", "name": BASE_SCOPE["company"]}),
			self.configuration({"doctype": "Warehouse", "name": " Main Warehouse Group"}),
			self.configuration({**valid, "all": True}),
		)
		for config in cases:
			with self.subTest(config=config), self.assertRaises(InvalidArgumentError):
				self.resolve(config)

	def test_foreign_disabled_or_unreadable_warehouse_is_refused(self):
		config = self.configuration({"doctype": "Warehouse", "name": "Main Warehouse Group"})
		for warehouse, readable, company_readable in (
			(frappe._dict(company="Other Company", disabled=0), True, True),
			(frappe._dict(company=BASE_SCOPE["company"], disabled=1), True, True),
			(frappe._dict(company=BASE_SCOPE["company"], disabled=0), False, True),
			(frappe._dict(company=BASE_SCOPE["company"], disabled=0), True, False),
			(None, True, True),
		):
			with (
				self.subTest(warehouse=warehouse, readable=readable, company_readable=company_readable),
				self.assertRaises(InvalidArgumentError),
			):
				self.resolve(
					config,
					warehouse=warehouse,
					readable=readable,
					company_readable=company_readable,
				)

	def test_raw_owner_root_stays_out_of_snapshot_and_prompt(self):
		listing = frappe._dict(agent_slug=AGENT, config_keys='["negative_stock"]')
		inst = self.installation(
			{
				**self.configuration({"doctype": "Warehouse", "name": "Raw Owner Warehouse"}),
				"negative_stock": {
					"record_scope": {"doctype": "Warehouse", "name": "Raw Owner Warehouse"},
					"reviewer": "reviewer@example.invalid",
				},
			}
		)
		snapshot = agent_scheduler._explicit_config(listing, inst)
		snapshot = agent_scheduler._sanitize_delegated_engagement_config(listing, snapshot)
		self.assertEqual(snapshot, {"negative_stock": {"reviewer": "reviewer@example.invalid"}})

		prompt = agent_scheduler._audit_prompt(
			listing,
			inst,
			"manual",
			{**BASE_SCOPE, "record_scope": {"doctype": "Warehouse", "name": "Main Warehouse Group"}},
			engagement_config=snapshot,
		)
		self.assertIn('record_scope={"doctype": "Warehouse", "name": "Main Warehouse Group"}', prompt)
		self.assertNotIn('"record_scope"', prompt.split("EXPLICIT CONFIG", 1)[1])
		self.assertNotIn("Raw Owner Warehouse", prompt.split("EXPLICIT CONFIG", 1)[1])

	def test_scope_error_refuses_before_scope_or_permission_values_are_stamped(self):
		inst = self.installation(self.configuration())
		with (
			mock.patch.object(operator_review, "backend", return_value=None),
			mock.patch.object(
				agent_scope,
				"resolve_scope",
				side_effect=InvalidArgumentError("Negative stock record_scope must name one Warehouse"),
			),
			mock.patch.object(frappe, "log_error") as log_error,
			mock.patch.object(frappe.db, "set_value") as set_value,
		):
			with self.assertRaises(InvalidArgumentError):
				agent_scheduler._stamp_scope_and_watermark("NSV-RUN", inst, "run-as@example.invalid")

		set_value.assert_not_called()
		self.assertEqual(
			log_error.call_args.kwargs["title"],
			"jarvis agent: Negative stock scope resolution refused launch",
		)

	def test_every_invalid_warehouse_case_refuses_before_any_scope_stamp(self):
		valid = {"doctype": "Warehouse", "name": "Main Warehouse Group"}
		cases = (
			(self.configuration(), _DEFAULT_WAREHOUSE, True, True),
			(self.configuration({"doctype": "Warehouse"}), _DEFAULT_WAREHOUSE, True, True),
			(
				self.configuration({"doctype": "Company", "name": BASE_SCOPE["company"]}),
				_DEFAULT_WAREHOUSE,
				True,
				True,
			),
			(self.configuration({**valid, "all": True}), _DEFAULT_WAREHOUSE, True, True),
			(self.configuration(valid), frappe._dict(company="Other Company", disabled=0), True, True),
			(self.configuration(valid), frappe._dict(company=BASE_SCOPE["company"], disabled=1), True, True),
			(self.configuration(valid), frappe._dict(company=BASE_SCOPE["company"], disabled=0), False, True),
			(self.configuration(valid), frappe._dict(company=BASE_SCOPE["company"], disabled=0), True, False),
		)
		for config, warehouse, readable, company_readable in cases:
			warehouse = (
				frappe._dict(company=BASE_SCOPE["company"], disabled=0)
				if warehouse is _DEFAULT_WAREHOUSE
				else warehouse
			)
			with (
				self.subTest(config=config),
				mock.patch.object(agent_scope, "_resolve", return_value=dict(BASE_SCOPE)),
				mock.patch.object(frappe.db, "get_value", return_value=warehouse),
				mock.patch.object(frappe, "has_permission", side_effect=(readable, company_readable)),
				mock.patch.object(operator_review, "backend", return_value=None),
				mock.patch.object(frappe, "log_error"),
				mock.patch.object(frappe.db, "set_value") as set_value,
			):
				with self.assertRaises(InvalidArgumentError):
					agent_scheduler._stamp_scope_and_watermark(
						"NSV-RUN", self.installation(config), "run-as@example.invalid"
					)
				set_value.assert_not_called()

	def test_model_cannot_replace_the_server_stamped_warehouse_after_launch(self):
		"""A changed, missing, or malformed model scope lands as non-evaluable."""
		tokens = [
			"nsv-grade-e058",
			"nsv-integ-2a6f",
			"nsv-neg-2e73",
			"nsv-spike-4b1c",
			"nsv-tieout-7d92",
			"nsv-zero-8c45",
		]
		stamped_scope = {
			**BASE_SCOPE,
			"record_scope": {"doctype": "Warehouse", "name": "Main Warehouse Group"},
		}
		for changed in (
			{"record_scope": {"doctype": "Warehouse", "name": "Other Warehouse"}},
			{"company": "Other Company"},
			{"from_date": "2026-09-01"},
			{"to_date": "2026-11-30"},
			{"report_date": "2026-11-01"},
			{"record_scope": {"doctype": "Warehouse", "name": "Other Warehouse", "all": True}},
			{"record_scope": None},
		):
			with self.subTest(changed=changed):
				run_row = frappe._dict(
					name="NSV-RUN",
					status="running",
					installation="NEGATIVE-STOCK-SCOPE-1",
					agent=AGENT,
				)
				run_doc = frappe._dict(name="NSV-RUN", agent=AGENT, scope_json=frappe.as_json(stamped_scope))
				final_run = frappe._dict(
					name="NSV-RUN",
					status="partial",
					findings_count=0,
					blocker_count=0,
					coverage_note="scope did not match server-stamped Warehouse boundary",
					owner="reviewer@example.invalid",
				)

				def get_value(doctype, *_args, **_kwargs):
					if doctype == writeback.RUN:
						return run_row
					if doctype == writeback.LISTING:
						return frappe._dict(
							rule_tokens=frappe.as_json(tokens),
							doctypes_required=frappe.as_json(["Warehouse"]),
						)
					self.fail(f"unexpected get_value for {doctype}")

				model_scope = {
					**stamped_scope,
					**changed,
					"warehouse_set": ["Other Warehouse"],
					"stock_accounts": ["Unsafe Stock Account"],
					"owner_config": {"all_warehouses": True},
				}
				with (
					mock.patch(
						"jarvis.tools._agent_run_ctx.get_session_key",
						return_value="agent:negative-stock:NSV-RUN",
					),
					mock.patch.object(writeback.frappe.db, "get_value", side_effect=get_value),
					mock.patch.object(
						writeback.frappe,
						"get_doc",
						side_effect=(frappe._dict(name="NEGATIVE-STOCK-SCOPE-1"), run_doc),
					),
					mock.patch.object(operator_review, "backend", return_value=None),
					mock.patch("jarvis.chat.agent_runs.record_delegate_run", return_value=final_run) as land,
					mock.patch("jarvis.chat.agent_run_steps.record_step"),
					mock.patch.object(writeback.frappe, "logger"),
					mock.patch.object(writeback.frappe.db, "set_value") as set_value,
				):
					result = writeback.record_agent_run(
						findings=[
							{
								"token": "nsv-neg-2e73",
								"ref_doctype": "Warehouse",
								"ref_name": "OUT-OF-SCOPE",
								"severity": "warning",
							}
						],
						coverage={"nsv-neg-2e73": {"state": "evaluated"}},
						scope=model_scope,
						canvas_ref="unsafe-canvas",
						integrity_digest="unsafe-digest",
						dashboard="unsafe-dashboard",
						rows_consumed=99,
					)

				self.assertEqual(result["status"], "partial")
				args, kwargs = land.call_args
				self.assertEqual(args[2], [])
				self.assertEqual(kwargs["scope"], agent_scope.negative_stock_output_scope(stamped_scope))
				self.assertEqual(set(kwargs["coverage"]), set(tokens))
				self.assertTrue(all(row["state"] == "not_evaluable" for row in kwargs["coverage"].values()))
				self.assertTrue(
					all(
						row["reason_code"] == "unsupported_customisation"
						for row in kwargs["coverage"].values()
					)
				)
				self.assertIsNone(kwargs["integrity_digest"])
				self.assertIsNone(kwargs["rows_consumed"])
				self.assertIsNone(kwargs["canvas_ref"])
				self.assertIsNone(kwargs["dashboard"])
				self.assertTrue(kwargs["dropped"])
				self.assertNotIn("Other Warehouse", json.dumps(kwargs, sort_keys=True))
				self.assertNotIn("owner_config", json.dumps(kwargs, sort_keys=True))
				set_value.assert_called_once_with(
					writeback.RUN,
					"NSV-RUN",
					{"dashboard": None, "canvas_ref": None, "integrity_digest": None},
					update_modified=False,
				)

	def test_matching_scope_strips_model_only_keys_before_global_merge(self):
		stamped_scope = {
			**BASE_SCOPE,
			"record_scope": {"doctype": "Warehouse", "name": "Main Warehouse Group"},
		}
		run_row = frappe._dict(
			name="NSV-RUN",
			status="running",
			installation="NEGATIVE-STOCK-SCOPE-1",
			agent=AGENT,
		)
		run_doc = frappe._dict(name="NSV-RUN", agent=AGENT, scope_json=frappe.as_json(stamped_scope))
		final_run = frappe._dict(
			name="NSV-RUN",
			status="completed",
			findings_count=0,
			blocker_count=0,
			coverage_note="",
			owner="reviewer@example.invalid",
		)

		def get_value(doctype, *_args, **_kwargs):
			if doctype == writeback.RUN:
				return run_row
			if doctype == writeback.LISTING:
				return frappe._dict(rule_tokens="[]", doctypes_required="[]")
			self.fail(f"unexpected get_value for {doctype}")

		model_scope = {
			**stamped_scope,
			"fiscal_year": "model-must-not-merge",
			"warehouse_set": ["Other Warehouse"],
			"stock_accounts": ["Unsafe Stock Account"],
			"owner_config": {"all_warehouses": True},
		}
		with (
			mock.patch(
				"jarvis.tools._agent_run_ctx.get_session_key", return_value="agent:negative-stock:NSV-RUN"
			),
			mock.patch.object(writeback.frappe.db, "get_value", side_effect=get_value),
			mock.patch.object(
				writeback.frappe,
				"get_doc",
				side_effect=(frappe._dict(name="NEGATIVE-STOCK-SCOPE-1"), run_doc),
			),
			mock.patch.object(operator_review, "backend", return_value=None),
			mock.patch("jarvis.chat.agent_runs.record_delegate_run", return_value=final_run) as land,
			mock.patch("jarvis.chat.agent_run_steps.record_step"),
			mock.patch.object(writeback.frappe, "logger"),
			mock.patch.object(writeback.frappe.db, "set_value") as set_value,
		):
			writeback.record_agent_run(findings=[], coverage={}, scope=model_scope)

		_args, kwargs = land.call_args
		self.assertEqual(kwargs["scope"], agent_scope.negative_stock_output_scope(stamped_scope))
		self.assertNotIn("fiscal_year", kwargs["scope"])
		self.assertNotIn("warehouse_set", kwargs["scope"])
		self.assertNotIn("stock_accounts", kwargs["scope"])
		self.assertNotIn("owner_config", kwargs["scope"])
		set_value.assert_not_called()

	def test_scope_mismatch_removes_the_saved_dashboard_before_safe_fallback(self):
		"""A persisted wrong-scope dashboard must not survive in any read path."""
		from jarvis.chat.dashboards_api import dashboard_for_conversation, get_dashboard, list_dashboards_page
		from jarvis.tools import _agent_run_ctx
		from jarvis.tools.save_agent_dashboard import save_agent_dashboard

		session_key = f"agent:negative-stock:scope-dashboard-{frappe.generate_hash(length=8)}"
		_installation, conversation, run, stamped_scope = self.live_run(session_key=session_key)
		unsafe_title = f"Unsafe negative-stock dashboard {frappe.generate_hash(length=8)}"
		frappe.set_user(self.owner)
		_agent_run_ctx.set_session_key(session_key)
		try:
			authored = save_agent_dashboard(
				html="<!doctype html><html><body>wrong-scope dashboard</body></html>",
				title=unsafe_title,
			)
			self.assertEqual(frappe.db.get_value(writeback.RUN, run.name, "dashboard"), authored["dashboard"])
			with mock.patch.object(operator_review, "backend", return_value=None):
				result = writeback.record_agent_run(
					findings=[],
					coverage={},
					scope={
						**stamped_scope,
						"record_scope": {"doctype": "Warehouse", "name": "Other Warehouse"},
					},
				)
		finally:
			_agent_run_ctx.clear_session_key()

		self.assertEqual(result["status"], "partial")
		self.assertFalse(frappe.db.exists(DASHBOARD, authored["dashboard"]))
		self.assertTrue(result["dashboard"])
		self.assertNotEqual(result["dashboard"], authored["dashboard"])
		self.assertEqual(frappe.db.get_value(writeback.RUN, run.name, "dashboard"), result["dashboard"])
		self.assertNotIn(
			authored["dashboard"],
			[row["name"] for row in list_dashboards_page(search=unsafe_title)["data"]["rows"]],
		)
		with self.assertRaises(frappe.DoesNotExistError):
			get_dashboard(authored["dashboard"])
		self.assertEqual(dashboard_for_conversation(conversation.name)["data"]["name"], result["dashboard"])

	def test_scope_cleanup_refuses_an_unproven_dashboard(self):
		"""A stale link must not authorize deletion or a completed unsafe run."""
		from jarvis.chat import agent_runs

		run_doc = frappe._dict(name="NSV-RUN", dashboard="OTHER-DASHBOARD", conversation="NSV-CONVERSATION")
		with (
			mock.patch.object(
				writeback.frappe.db,
				"get_value",
				return_value=frappe._dict(
					name="OTHER-DASHBOARD",
					source_conversation="ANOTHER-CONVERSATION",
					owner=self.owner,
					target_user=self.owner,
				),
			),
			mock.patch.object(writeback.frappe.db, "set_value") as set_value,
			mock.patch.object(writeback.frappe, "delete_doc") as delete_doc,
			mock.patch.object(writeback.frappe, "logger"),
		):
			with self.assertRaises(frappe.ValidationError):
				agent_runs.clear_untrusted_negative_stock_artifacts(
					run_doc, frappe._dict(owner=self.owner, reviewer=self.owner)
				)

		delete_doc.assert_not_called()
		set_value.assert_not_called()

	def test_scope_cleanup_accepts_a_dashboard_saved_before_visibility_promotion(self):
		"""A shadow dashboard remains deletable after the installation becomes live."""
		from jarvis.tools import _agent_run_ctx
		from jarvis.tools.save_agent_dashboard import save_agent_dashboard

		session_key = f"agent:negative-stock:promoted-dashboard-{frappe.generate_hash(length=8)}"
		installation, _conversation, run, stamped_scope = self.live_run(
			session_key=session_key,
			reviewer="Administrator",
			activation_state="shadow",
		)
		frappe.set_user(self.owner)
		_agent_run_ctx.set_session_key(session_key)
		try:
			authored = save_agent_dashboard(
				html="<!doctype html><html><body>wrong-scope dashboard</body></html>",
				title="Promoted negative-stock dashboard",
			)
			self.assertEqual(frappe.db.get_value(DASHBOARD, authored["dashboard"], "owner"), "Administrator")
			frappe.db.set_value(
				writeback.INSTALLATION,
				installation.name,
				"activation_state",
				"live",
				update_modified=False,
			)
			with mock.patch.object(operator_review, "backend", return_value=None):
				result = writeback.record_agent_run(
					findings=[],
					coverage={},
					scope={
						**stamped_scope,
						"record_scope": {"doctype": "Warehouse", "name": "Other Warehouse"},
					},
				)
		finally:
			_agent_run_ctx.clear_session_key()

		self.assertEqual(result["status"], "partial")
		self.assertFalse(frappe.db.exists(DASHBOARD, authored["dashboard"]))

	def test_scope_cleanup_refuses_completion_when_dashboard_lookup_or_delete_fails(self):
		"""A cleanup failure cannot turn a rejected dashboard into a completed run."""
		from jarvis.chat import agent_runs

		run_doc = frappe._dict(name="NSV-RUN", dashboard="NSV-DASHBOARD", conversation="NSV-CONVERSATION")
		inst = frappe._dict(owner=self.owner, reviewer=self.owner)
		with (
			mock.patch.object(writeback.frappe.db, "get_value", side_effect=RuntimeError("lookup")),
			mock.patch.object(writeback.frappe, "log_error"),
			mock.patch.object(writeback.frappe.db, "set_value") as set_value,
			self.assertRaises(frappe.ValidationError),
		):
			agent_runs.clear_untrusted_negative_stock_artifacts(run_doc, inst)
		set_value.assert_not_called()

		with (
			mock.patch.object(
				writeback.frappe.db,
				"get_value",
				return_value=frappe._dict(
					name="NSV-DASHBOARD",
					source_conversation="NSV-CONVERSATION",
					owner=self.owner,
					target_user=self.owner,
				),
			),
			mock.patch.object(writeback.frappe, "delete_doc", side_effect=RuntimeError("delete")),
			mock.patch.object(writeback.frappe.db, "delete", side_effect=RuntimeError("fallback")),
			mock.patch.object(writeback.frappe, "log_error"),
			mock.patch.object(writeback.frappe.db, "set_value") as set_value,
			self.assertRaises(frappe.ValidationError),
		):
			agent_runs.clear_untrusted_negative_stock_artifacts(run_doc, inst)
		set_value.assert_not_called()

	def test_direct_record_delegate_run_cannot_replace_the_server_stamped_warehouse(self):
		"""The persistence API has the same scope fence as the public tool."""
		from jarvis.chat import agent_runs

		session_key = f"agent:negative-stock:direct-scope-{frappe.generate_hash(length=8)}"
		installation, _conversation, run, stamped_scope = self.live_run(session_key=session_key)
		with mock.patch.object(operator_review, "backend", return_value=None):
			agent_runs.record_delegate_run(
				run,
				installation,
				[],
				coverage={},
				scope={
					**stamped_scope,
					"record_scope": {"doctype": "Warehouse", "name": "Other Warehouse"},
					"warehouse_set": ["Other Warehouse"],
				},
			)

		stored_scope = frappe.parse_json(frappe.db.get_value(writeback.RUN, run.name, "scope_json"))
		self.assertEqual(stored_scope["record_scope"], stamped_scope["record_scope"])
		self.assertNotIn("Other Warehouse", frappe.as_json(stored_scope))

	def test_direct_record_delegate_run_drops_submitted_scope_when_stamp_is_malformed(self):
		"""A malformed legacy stamp must not let direct callers choose a Warehouse."""
		from jarvis.chat import agent_runs

		session_key = f"agent:negative-stock:malformed-scope-{frappe.generate_hash(length=8)}"
		installation, _conversation, run, stamped_scope = self.live_run(session_key=session_key)
		frappe.db.set_value(writeback.RUN, run.name, "scope_json", "{not-json", update_modified=False)
		with mock.patch.object(operator_review, "backend", return_value=None):
			agent_runs.record_delegate_run(
				run,
				installation,
				[],
				coverage={},
				scope={
					**stamped_scope,
					"record_scope": {"doctype": "Warehouse", "name": "Other Warehouse"},
					"warehouse_set": ["Other Warehouse"],
				},
			)

		self.assertEqual(frappe.db.get_value(writeback.RUN, run.name, "scope_json"), "{not-json")

	def test_direct_scope_mismatch_cannot_persist_or_auto_resolve_another_company(self):
		"""A raw direct scope must not drive finding or A16 company side effects."""
		from jarvis.chat import agent_runs

		first_inst, _conversation, first_run, first_scope = self.live_run(
			session_key=f"agent:negative-stock:first-company-{frappe.generate_hash(length=8)}"
		)
		token = frappe.parse_json(frappe.db.get_value(writeback.LISTING, AGENT, "rule_tokens"))[0]
		companies = frappe.get_all("Company", pluck="name")
		self.assertGreaterEqual(len(companies), 2)
		trusted_company, other_company = companies[:2]
		other_company_scope = {**first_scope, "company": other_company}
		frappe.db.set_value(
			writeback.RUN,
			first_run.name,
			"scope_json",
			frappe.as_json(other_company_scope),
			update_modified=False,
		)
		with mock.patch.object(operator_review, "backend", return_value=None):
			agent_runs.record_delegate_run(
				first_run,
				first_inst,
				[
					{
						"token": token,
						"ref_doctype": "Warehouse",
						"ref_name": "Other Company Warehouse",
						"severity": "warning",
						"result_class": "observed_fact",
					}
				],
				coverage={token: "evaluated"},
				scope=other_company_scope,
			)
		candidate = frappe.db.get_value(
			FINDING,
			{"last_seen_run": first_run.name},
			["name", "state", "company"],
			as_dict=True,
		)
		self.assertEqual(candidate.company, other_company)

		second_scope = {**first_scope, "company": trusted_company}
		_conversation, second_run = self.run_for_installation(
			first_inst,
			session_key=f"agent:negative-stock:second-company-{frappe.generate_hash(length=8)}",
			scope=second_scope,
		)
		with mock.patch.object(operator_review, "backend", return_value=None):
			agent_runs.record_delegate_run(
				second_run,
				first_inst,
				[
					{
						"token": token,
						"ref_doctype": "Warehouse",
						"ref_name": "Wrong Company Warehouse",
						"severity": "blocker",
						"result_class": "observed_fact",
					}
				],
				coverage={token: "evaluated"},
				scope={**second_scope, "company": other_company},
				integrity_digest="wrong-company-digest",
				dashboard="wrong-company-dashboard",
			)

		self.assertEqual(frappe.db.get_value(FINDING, candidate.name, "state"), "open")
		self.assertEqual(
			frappe.db.count(FINDING, {"last_seen_run": second_run.name}),
			0,
		)
		second_run.reload()
		self.assertEqual(second_run.result_state, "not_evaluable")
		self.assertFalse(second_run.integrity_digest)

	def test_auto_resolve_keeps_negative_stock_findings_from_another_warehouse_root(self):
		"""A clean root B run cannot resolve an unobserved root A finding."""
		from jarvis.chat import agent_runs

		first_inst, _conversation, first_run, first_scope = self.live_run(
			session_key=f"agent:negative-stock:first-root-{frappe.generate_hash(length=8)}",
			warehouse_name="Warehouse Root A",
		)
		token = frappe.parse_json(frappe.db.get_value(writeback.LISTING, AGENT, "rule_tokens"))[0]
		company = frappe.get_all("Company", pluck="name")[0]
		first_scope = {**first_scope, "company": company}
		frappe.db.set_value(
			writeback.RUN,
			first_run.name,
			"scope_json",
			frappe.as_json(first_scope),
			update_modified=False,
		)
		with mock.patch.object(operator_review, "backend", return_value=None):
			agent_runs.record_delegate_run(
				first_run,
				first_inst,
				[
					{
						"token": token,
						"ref_doctype": "Warehouse",
						"ref_name": "Warehouse A Finding",
						"severity": "warning",
						"result_class": "observed_fact",
					}
				],
				coverage={token: "evaluated"},
				scope=first_scope,
			)
		candidate = frappe.db.get_value(
			FINDING, {"last_seen_run": first_run.name}, ["name", "state"], as_dict=True
		)

		second_scope = {
			**first_scope,
			"record_scope": {"doctype": "Warehouse", "name": "Warehouse Root B"},
		}
		_conversation, second_run = self.run_for_installation(
			first_inst,
			session_key=f"agent:negative-stock:second-root-{frappe.generate_hash(length=8)}",
			scope=second_scope,
		)
		with mock.patch.object(operator_review, "backend", return_value=None):
			agent_runs.record_delegate_run(
				second_run,
				first_inst,
				[],
				coverage={token: "evaluated"},
				scope=second_scope,
			)

		self.assertEqual(frappe.db.get_value(FINDING, candidate.name, "state"), "open")
