"""Native Reorder evidence, authority and non-posting regressions."""

import copy
import json
import time
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_catalog, agent_scheduler
from jarvis.chat import negative_stock_receipt as trust
from jarvis.chat import reorder_review as review
from jarvis.tests.test_negative_stock_receipt import _coverage


def source():
	d = frappe._dict
	return {
		"company": [
			d(
				name="C",
				enable_perpetual_inventory=1,
				default_currency="INR",
				default_inventory_account="Stock",
			)
		],
		"warehouses": [
			d(name="Root", is_group=1, lft=1, rgt=4),
			d(name="Stores", is_group=0, lft=2, rgt=3, disabled=0, account="Stock"),
		],
		"accounts": [d(name="Stock", account_type="Stock", is_group=0, disabled=0, account_currency="INR")],
		"items": [
			d(
				name="ITEM",
				is_stock_item=1,
				has_variants=0,
				disabled=0,
				end_of_life=None,
				variant_of=None,
				stock_uom="Nos",
				min_order_qty=0,
				allow_negative_stock=0,
			)
		],
		"ledger": [
			d(
				name="SLE",
				item_code="ITEM",
				warehouse="Stores",
				actual_qty=5,
				qty_after_transaction=5,
				valuation_rate=25,
				incoming_rate=25,
				stock_value=125,
				stock_value_difference=125,
				posting_date=frappe.utils.today(),
				posting_time="09:00:00",
				creation="2026-01-01 09:00:00",
				voucher_type="Stock Entry",
				is_adjustment_entry=0,
				is_cancelled=0,
			)
		],
		"gl": [
			d(
				name="GL",
				account="Stock",
				posting_date=frappe.utils.today(),
				debit=125,
				credit=0,
				is_cancelled=0,
			)
		],
		"bins": [
			d(
				name="BIN",
				item_code="ITEM",
				warehouse="Stores",
				actual_qty=5,
				stock_value=125,
				valuation_rate=25,
				projected_qty=5,
				ordered_qty=0,
				indented_qty=0,
				planned_qty=0,
				reserved_qty=0,
				reserved_qty_for_production=0,
				reserved_qty_for_sub_contract=0,
				reserved_qty_for_production_plan=0,
			)
		],
		"reposts": [],
		"landed_costs": [],
		"requests": [],
		"orders": [],
		"request_items": [],
		"order_items": [],
		"uoms": [d(name="Nos", must_be_whole_number=1)],
		"policies": [
			d(
				name="P",
				parent="ITEM",
				warehouse="Stores",
				warehouse_group="Stores",
				warehouse_reorder_level=10,
				warehouse_reorder_qty=8,
				material_request_type="Purchase",
			)
		],
	}


class TestReorderRules(unittest.TestCase):
	def setUp(self):
		self.data = source()
		self.scope = {
			"record_scope": {"doctype": "Warehouse", "name": "Root"},
			"report_date": frappe.utils.today(),
		}
		self.config = {"max_proposals": 50}

	def test_clean_source_and_customer_policy_size_one_suggestion(self):
		leaf = review.native_stock_guard(self.data, self.scope)
		rows = review.derive(self.data, leaf, self.config)
		self.assertEqual(rows[0]["proposed_qty"], "8")
		self.assertEqual(rows[0]["status"], "Pending")
		self.assertEqual(rows[0]["projected_qty"], "5")

	def test_native_guard_refuses_poisoned_source_even_with_no_model_findings(self):
		for family, field, value in [
			("ledger", "qty_after_transaction", -1),
			("ledger", "incoming_rate", 0),
			("ledger", "stock_value", 124),
			("gl", "debit", 120),
			("bins", "actual_qty", 6),
			("bins", "projected_qty", 10),
		]:
			data = copy.deepcopy(self.data)
			data[family][0][field] = value
			with self.subTest(family=family, field=field), self.assertRaises(frappe.ValidationError):
				review.native_stock_guard(data, self.scope)

	def test_variable_incoming_cost_is_held_without_a_private_threshold(self):
		data = self.data
		data["ledger"].append(
			frappe._dict(
				**{
					**data["ledger"][0],
					"name": "SLE2",
					"actual_qty": 1,
					"qty_after_transaction": 6,
					"stock_value": 150,
					"stock_value_difference": 25,
					"incoming_rate": 30,
				}
			)
		)
		with self.assertRaises(frappe.ValidationError):
			review.native_stock_guard(data, self.scope)

	def test_open_supply_is_not_subtracted_twice(self):
		self.data["bins"][0].projected_qty = 20
		self.assertEqual(review.derive(self.data, {"Stores": {}}, self.config), [])

	def test_draft_supply_and_conflicting_policy_are_manual_work(self):
		self.data["requests"] = [frappe._dict(name="MR-DRAFT", docstatus=0)]
		self.data["request_items"] = [frappe._dict(parent="MR-DRAFT", item_code="ITEM", warehouse="Stores")]
		row = review.derive(self.data, {"Stores": {}}, self.config)[0]
		self.assertEqual(
			(row["status"], row["proposed_qty"], row["reason"]), ("Held", None, "draft_supply_exists")
		)
		self.data["policies"].append(copy.deepcopy(self.data["policies"][0]))
		self.assertEqual(
			review.derive(self.data, {"Stores": {}}, self.config)[0]["reason"], "conflicting_policy"
		)

	def test_minimum_quantity_whole_units_and_limit(self):
		self.data["items"][0].min_order_qty = 9.5
		self.assertEqual(review.derive(self.data, {"Stores": {}}, self.config)[0]["proposed_qty"], "10")
		with self.assertRaises(frappe.ValidationError):
			review.derive(self.data, {"Stores": {}}, {"max_proposals": 0})

	def test_group_target_is_visible_as_manual_work_and_not_a_zero_stock_claim(self):
		self.data["policies"][0].warehouse = "Root"
		self.data["policies"][0].warehouse_group = "Root"
		row = review.derive(self.data, {"Stores": {}}, self.config)[0]
		self.assertEqual(row["status"], "Held")
		self.assertIsNone(row["projected_qty"])
		self.assertIsNone(row["proposed_qty"])

	def test_model_scope_and_text_cannot_enter_the_dispatch_result(self):
		result = review.dispatch(
			"missing", review.TOOL, {"company": "PRIVATE-MODEL-MARKER"}, cap={"run": "missing"}
		)
		self.assertFalse(result["ok"])
		self.assertNotIn("PRIVATE-MODEL-MARKER", json.dumps(result))

	def test_native_release_can_be_withdrawn_without_reopening_legacy_writes(self):
		from jarvis.chat import reorder_replenishment_gate as gate

		gate.assert_listing_status(review.AGENT, "Deprecated")
		for tool in ("create_doc", "update_doc", "submit_doc", review.TOOL):
			denial = gate.delegate_tool_denial({"agent": review.AGENT, "run": "old", "legacy": True}, tool)
			self.assertTrue(denial["fatal"])


class TestNativeReorderJourney(FrappeTestCase):
	"""Committed, uniquely named fake fixtures allow the real read-only connection.

	The class runs only on a test site. It makes no production connection and no
	fleet call. Source fixtures need a commit because freshness must be proved
	through another database connection. The test site's lifecycle owns cleanup.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.local.site.startswith("test_"):
			raise unittest.SkipTest("A disposable test site is required")
		from jarvis.tests import _erpnext_masters as masters

		frappe.set_user("Administrator")
		masters.clear_cache_after_rollback(cls)
		cls.prefix = "ZZ-RRO-" + frappe.generate_hash(length=6)
		cls.company = cls.prefix + " Company"
		cls.runner = cls.prefix.lower() + "-runner@example.invalid"
		cls.reviewer = cls.prefix.lower() + "-reviewer@example.invalid"
		for email in (cls.runner, cls.reviewer):
			doc = frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": "Reorder test", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
			doc.add_roles("Jarvis User", "Stock Manager", "Accounts Manager", "Purchase Manager")
		masters.ensure_ledger_company()
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": cls.company,
				"abbr": "R" + frappe.generate_hash(length=4).upper(),
				"default_currency": "INR",
				"country": "Maldives",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": "Standard",
			}
		).insert(ignore_permissions=True)
		cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "warehouse_name": "Stores"})
		cls.root = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 1})
		settings = frappe.get_single("Stock Settings")
		settings.allow_negative_stock = 0
		settings.save()
		cls.item = masters.ensure_item(cls.prefix + "-ITEM", is_stock_item=1, valuation_rate=25)
		item = frappe.get_doc("Item", cls.item)
		item.append(
			"reorder_levels",
			{
				"warehouse": cls.warehouse,
				"warehouse_group": cls.warehouse,
				"warehouse_reorder_level": 10,
				"warehouse_reorder_qty": 8,
				"material_request_type": "Purchase",
			},
		)
		item.save()
		masters.ensure_stock_entry_type("Material Receipt")
		masters.ensure_fiscal_years(frappe.utils.today())
		if not masters._active_fiscal_year_covers(frappe.utils.getdate(), cls.company):
			frappe.get_doc(
				{
					"doctype": "Fiscal Year",
					"year": cls.prefix + " FY",
					"year_start_date": frappe.utils.getdate().replace(month=1, day=1),
					"year_end_date": frappe.utils.getdate().replace(month=12, day=31),
					"companies": [{"company": cls.company}],
				}
			).insert(ignore_permissions=True)
		stock = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"company": cls.company,
				"posting_date": frappe.utils.today(),
				"stock_entry_type": "Material Receipt",
				"items": [{"item_code": cls.item, "qty": 5, "basic_rate": 25, "t_warehouse": cls.warehouse}],
			}
		).insert()
		stock.submit()
		cls.stock = stock.name
		agent_catalog.sync_agent_listings()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		time.sleep(1.05)
		self.scope = {
			"company": self.company,
			"from_date": frappe.utils.add_days(frappe.utils.today(), -30),
			"to_date": frappe.utils.today(),
			"report_date": frappe.utils.today(),
			"record_scope": {"doctype": "Warehouse", "name": self.root},
		}
		profile = agent_scheduler._permission_profile(self.runner)
		negative = frappe.get_doc(
			{
				"doctype": review.RUN,
				"agent": trust.AGENT,
				"status": "running",
				"started_at": frappe.utils.now(),
				"bundle_version": trust.SUPPORTED_BUNDLE_VERSION,
				"preparation_mode": "live",
				"scope_json": json.dumps(self.scope),
				"permission_profile": profile,
				"engagement_config_json": json.dumps({"negative_stock": {"reviewer": self.reviewer}}),
			}
		).insert(ignore_permissions=True)
		negative_key = "agent:agent-" + trust.AGENT + ":" + negative.name
		frappe.get_doc(
			{"doctype": "Jarvis Chat Session", "session_key": negative_key, "user": self.runner}
		).insert(ignore_permissions=True)
		frappe.db.set_value(review.RUN, negative.name, "session_key", negative_key)
		negative.reload()
		capture = trust.capture_launch(negative)
		frappe.db.set_value(
			review.RUN,
			negative.name,
			{
				"status": "completed",
				"result_state": "evaluated_clean",
				"finished_at": frappe.utils.now(),
				"coverage_json": json.dumps(_coverage()),
				"integrity_digest": "b" * 64,
			},
		)
		self.assertIsNotNone(trust.persist(capture))
		self.config = {
			"policy_version": review.POLICY,
			"negative_stock_run": negative.name,
			"max_proposals": 50,
			"scope": self.scope,
			"reviewer": self.reviewer,
		}
		self.run = frappe.get_doc(
			{
				"doctype": review.RUN,
				"agent": review.AGENT,
				"status": "running",
				"started_at": frappe.utils.now(),
				"bundle_version": review.VERSION,
				"preparation_mode": "live",
				"scope_json": json.dumps(self.scope),
				"permission_profile": profile,
				"assessment_config_json": json.dumps(self.config),
				"capability_contract": "snapshot",
				"tools_allow_json": json.dumps(["jarvis__" + review.TOOL]),
				"capability_nature": "operator",
				"capability_writes_json": "[]",
			}
		).insert(ignore_permissions=True)
		self.key = "agent:agent-" + review.AGENT + ":" + self.run.name
		frappe.get_doc(
			{"doctype": "Jarvis Chat Session", "session_key": self.key, "user": self.runner}
		).insert(ignore_permissions=True)
		frappe.db.set_value(review.RUN, self.run.name, "session_key", self.key)
		self.run.reload()
		frappe.db.commit()
		frappe.set_user(self.runner)

	def guard(self):
		return {
			d: frappe.db.count(d, {"company": self.company})
			for d in ("Material Request", "Purchase Order", "Stock Entry", "Stock Ledger Entry", "GL Entry")
		} | {d: frappe.db.count(d) for d in ("Jarvis Approval Request", "Communication", "ToDo")}

	def test_live_prepare_review_and_retry_never_post_erp(self):
		from jarvis import api

		before = self.guard()
		response = api._dispatch_from_session(self.runner, self.key, review.TOOL, {})
		self.assertTrue(response["ok"], response)
		out = response["data"]
		self.assertEqual(out["proposal_count"], 1)
		self.assertEqual(frappe.db.get_value(review.RUN, self.run.name, "coverage_note"), "")
		self.assertNotIn(self.item, json.dumps(out))
		self.assertEqual(review.prepare(self.run), out)
		self.assertEqual(
			frappe.db.count("Jarvis Agent Run Step", {"run": self.run.name, "kind": "writeback"}), 1
		)
		frappe.db.commit()
		self.assertFalse(frappe.db.exists("Jarvis Chat Session", self.key))
		frappe.set_user(self.reviewer)
		view = review.get_review(self.run.name)
		self.assertEqual(view["execution_user"], self.runner)
		self.assertEqual(view["rows"][0]["proposed_qty"], "8")
		result = review.decide(self.run.name, view["rows"][0]["id"], 1)
		self.assertEqual(result["status"], "Approved")
		self.assertTrue(review.decide(self.run.name, view["rows"][0]["id"], 1)["idempotent"])
		self.assertEqual(self.guard(), before)

	def test_dispatch_refuses_generic_writes_and_model_arguments_without_raw_retention(self):
		from jarvis import api

		before = self.guard()
		marker = "RRO-PRIVATE-MODEL-MARKER"
		result = api._dispatch_from_session(
			self.runner,
			self.key,
			"create_doc",
			{"doctype": "Material Request", "values": {"description": marker}},
		)
		self.assertFalse(result["ok"])
		result = api._dispatch_from_session(self.runner, self.key, review.TOOL, {"source": marker})
		self.assertFalse(result["ok"])
		self.assertNotIn(marker, json.dumps(result))
		self.assertEqual(frappe.db.get_value(review.RUN, self.run.name, "status"), "failed")
		self.assertNotIn(marker, frappe.db.get_value(review.RUN, self.run.name, "error") or "")
		self.assertEqual(self.guard(), before)

	def test_dispatch_redacts_source_failures_in_both_response_channels(self):
		from jarvis import api

		marker = "RRO-PRIVATE-SOURCE-ERROR"

		def broken(*args, **kwargs):
			frappe.msgprint(marker)
			raise RuntimeError(marker)

		with patch.object(review, "collect", side_effect=broken):
			result = api._dispatch_from_session(self.runner, self.key, review.TOOL, {})
		self.assertFalse(result["ok"])
		self.assertNotIn(marker, json.dumps(result))
		self.assertNotIn(marker, json.dumps(frappe.local.message_log))
		self.assertNotIn(marker, frappe.db.get_value(review.RUN, self.run.name, "error") or "")

	def test_changed_policy_blocks_approval_but_rejection_still_works(self):
		review.prepare(self.run)
		frappe.db.commit()
		frappe.set_user("Administrator")
		frappe.db.set_value("Item", self.item, "min_order_qty", 1)
		frappe.db.commit()
		frappe.set_user(self.reviewer)
		row = review.get_review(self.run.name)["rows"][0]
		with self.assertRaises((frappe.ValidationError, trust.ReceiptRefused)):
			review.decide(self.run.name, row["id"], 1)
		self.assertEqual(review.get_review(self.run.name)["rows"][0]["status"], "Pending")
		self.assertEqual(review.decide(self.run.name, row["id"], 0)["status"], "Rejected")

	def test_old_request_snapshot_cannot_replace_a_committed_buyer_decision(self):
		review.prepare(self.run)
		frappe.db.commit()
		frappe.set_user(self.reviewer)
		primary = frappe.local.db
		conf = frappe.conf
		observer = trust.get_db(
			socket=conf.db_socket,
			host=conf.db_host,
			port=conf.db_port,
			user=conf.db_user or conf.db_name,
			password=conf.db_password,
			cur_db_name=conf.db_name,
		)
		try:
			observer.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			observer.sql("START TRANSACTION WITH CONSISTENT SNAPSHOT")
			query = "select assessment_output_json from `tabJarvis Agent Run` where name=%s"
			old = observer.sql(query, (self.run.name,))[0][0]
			self.assertEqual(json.loads(old)["state"]["rows"][0]["status"], "Pending")
			row = review.get_review(self.run.name)["rows"][0]
			self.assertEqual(review.decide(self.run.name, row["id"], 1)["status"], "Approved")
			frappe.db.commit()
			self.assertEqual(observer.sql(query, (self.run.name,))[0][0], old)
			frappe.local.db = observer
			with self.assertRaises(frappe.ValidationError):
				review.decide(self.run.name, row["id"], 0)
		finally:
			frappe.local.db = primary
			observer.rollback()
			observer.close()
		self.assertEqual(review.get_review(self.run.name)["rows"][0]["status"], "Approved")

	def test_lost_quantity_field_access_hides_an_earlier_review(self):
		from frappe.model import get_permitted_fields

		review.prepare(self.run)
		frappe.set_user(self.reviewer)

		def permitted(doctype, *args, **kwargs):
			fields = get_permitted_fields(doctype, *args, **kwargs)
			return [f for f in fields if not (doctype == "Bin" and f == "projected_qty")]

		with patch("frappe.model.get_permitted_fields", side_effect=permitted):
			with self.assertRaises(frappe.ValidationError):
				review.get_review(self.run.name)

	def test_shadow_is_visible_but_cannot_be_approved(self):
		frappe.db.set_value(review.RUN, self.run.name, "preparation_mode", "shadow")
		self.run.reload()
		before = self.guard()
		self.assertEqual(review.prepare(self.run)["state"], "preview")
		frappe.set_user(self.reviewer)
		row = review.get_review(self.run.name)["rows"][0]
		with self.assertRaises(frappe.ValidationError):
			review.decide(self.run.name, row["id"], 1)
		self.assertEqual(self.guard(), before)

	def test_install_enable_catalog_and_launch_use_the_native_contract(self):
		from jarvis.chat import agents_api

		frappe.set_user("Administrator")
		listing = frappe.get_doc("Jarvis Agent Listing", review.AGENT)
		listing.append("allowed_users", {"user": self.runner})
		listing.save()
		frappe.db.commit()
		frappe.set_user(self.runner)
		with patch("jarvis.chat.agent_models.plan_install", return_value=None):
			installed = agents_api.install_agent(review.AGENT)["data"]["name"]
		public_config = {
			key: self.config[key] for key in ("policy_version", "negative_stock_run", "max_proposals")
		}
		agents_api.set_config(installed, json.dumps(public_config))
		agents_api.set_enabled(installed, 1)
		agents_api.set_schedule(installed, schedule_enabled=0)
		self.assertEqual(frappe.db.get_value("Jarvis Agent Installation", installed, "enabled"), 1)
		payload = agent_catalog.build_agent_push_payload(owner=self.runner)
		self.assertTrue(
			any(
				r.get("slug") == "agent-" + review.AGENT
				and r.get("tools_allow") == ["jarvis__" + review.TOOL]
				for r in payload
			),
			payload,
		)
		installation = frappe.get_doc("Jarvis Agent Installation", installed)
		with patch("jarvis.admin_client.post_agent_run", return_value={"status": "accepted"}) as post:
			result = agent_scheduler._launch_audit(
				installation, "manual", initiating_human=self.runner, model_gate={"ok": True}
			)
		post.assert_called_once()
		self.assertNotIn(self.company, post.call_args.kwargs["message"])
		launched = frappe.get_doc(review.RUN, result["run"])
		self.assertTrue(review.modern_run(launched))
		self.assertEqual(json.loads(launched.assessment_config_json)["scope"], self.scope)
		self.assertEqual(review.prepare(launched)["state"], "preview")
		frappe.db.commit()
		frappe.set_user("Administrator")
		agents_api.promote_installation(installed, justification="Isolated fake-record review")
		frappe.set_user(self.runner)
		installation.reload()
		with patch("jarvis.admin_client.post_agent_run", return_value={"status": "accepted"}):
			live = agent_scheduler._launch_audit(
				installation, "manual", initiating_human=self.runner, model_gate={"ok": True}
			)
		live_run = frappe.get_doc(review.RUN, live["run"])
		self.assertEqual(review.prepare(live_run)["state"], "prepared")

	def test_other_reviewer_and_forged_state_are_refused(self):
		review.prepare(self.run)
		with self.assertRaises(frappe.PermissionError):
			review.get_review(self.run.name)
		frappe.set_user(self.reviewer)
		self.assertIn(self.run.name, [r["run"] for r in review.list_reviews()["rows"]])
		raw = json.loads(frappe.db.get_value(review.RUN, self.run.name, "assessment_output_json"))
		raw["state"]["rows"][0]["proposed_qty"] = "99999"
		frappe.db.set_value(review.RUN, self.run.name, "assessment_output_json", json.dumps(raw))
		with self.assertRaises(frappe.ValidationError):
			review.get_review(self.run.name)
