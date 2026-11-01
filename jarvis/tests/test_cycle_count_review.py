"""Cycle-Count Planner custody and output regressions."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import cycle_count_review as review
from jarvis.chat import operator_review


def config(**over):
	value = {
		"company": "Example",
		"from_date": "2026-08-01",
		"to_date": "2026-10-05",
		"count_history_from": "2025-10-05",
		"warehouses": ["WH-A"],
		"line_limit": 10,
		"recount_after_days": 30,
		"basis": "current_records",
		"review_scope": "count_plan_only",
		"policy_version": "CYCLE-COUNT-v1",
	}
	value.update(over)
	return value


def launch_config(**over):
	value = {**config(), "plan_date": "2026-10-05", "reviewer": "reviewer@example.invalid"}
	value.update(over)
	return value


def snapshot():
	policy = launch_config()
	data = {
		"schema": "cycle-count-source-v2",
		"scope": review.scope_from_config(policy),
		"config": policy,
		"currency": "INR",
		"complete": True,
		"source_counts": {"Bin": 1, "Item": 1, "Stock Ledger Entry": 1, "Stock Entry": 0},
		"datasets": {
			"bins": [
				{
					"name": "BIN-A",
					"item_code": "ITEM-A",
					"warehouse": "WH-A",
					"actual_qty": "5",
					"stock_value": "100",
					"valuation_rate": "20",
				}
			],
			"items": [
				{
					"name": "ITEM-A",
					"is_stock_item": 1,
					"disabled": 0,
					"end_of_life": None,
					"stock_uom": "Nos",
					"has_batch_no": 0,
					"has_serial_no": 0,
				}
			],
			"outward_ledger": [
				{
					"name": "SLE-1",
					"item_code": "ITEM-A",
					"warehouse": "WH-A",
					"company": "Example",
					"posting_date": "2026-09-20",
					"posting_time": "09:00:00",
					"actual_qty": "-3",
					"voucher_type": "Delivery Note",
					"voucher_no": "DN-1",
					"is_cancelled": 0,
				}
			],
			"stock_entries": [],
			"physical_counts": [],
			"physical_count_items": [],
		},
	}
	data["source_digest"] = review.digest(data)
	return data


def result(data=None):
	data = data or snapshot()
	findings = [
		{
			"token": review.TOKEN,
			"ref_doctype": "Warehouse",
			"ref_name": "WH-A",
			"amount": 0,
			"severity": "note",
			"result_class": "derived_candidate",
			"confidence": "medium",
			"match_basis": "Current records and approved cadence",
			"false_positive_path": "The store lead can defer a count after review.",
			"note": "Plan one human physical count for this warehouse.",
			"proposal": {
				"outcome": "plan",
				"reason_code": "cadence_due",
				"lines_considered": 1,
				"lines_planned": 1,
				"lines_rolled": 0,
				"due_total": 1,
				"lines": [
					{
						"item_code": "ITEM-A",
						"stock_uom": "Nos",
						"abc": "A",
						"velocity": "FAST",
						"outward_qty": "3",
						"actual_qty": "5",
						"stock_value": "100",
						"last_physical_count_on": None,
						"capture_required": False,
					}
				],
			},
		}
	]
	coverage = {review.TOKEN: {"state": "evaluated"}}
	return {
		"findings": findings,
		"coverage": coverage,
		"scope": data["scope"],
		"integrity_digest": review.digest(
			{
				"input_digest": data["source_digest"],
				"scope": data["scope"],
				"findings": findings,
				"coverage": coverage,
			}
		),
	}


class TestCycleCountConfiguration(FrappeTestCase):
	def test_registered_as_operator_backend(self):
		self.assertIn(review.AGENT, operator_review.AGENTS)
		self.assertIs(operator_review.backend(review.AGENT), review)

	def test_configuration_stamps_an_enabled_reviewer(self):
		warehouse = MagicMock(company="Example", is_group=0, disabled=0)
		with (
			patch.object(review, "_warehouse", return_value=warehouse),
			patch.object(review.frappe, "get_doc", return_value=MagicMock()),
			patch.object(review.frappe.db, "get_value", return_value=1),
			patch.object(review.frappe.utils, "today", return_value="2026-10-05"),
		):
			self.assertEqual(
				review.configuration(
					frappe._dict(config=frappe.as_json(config()), reviewer="reviewer@example.invalid")
				),
				launch_config(),
			)

	def test_configuration_rejects_missing_or_disabled_reviewer(self):
		warehouse = MagicMock(company="Example", is_group=0, disabled=0)
		with (
			patch.object(review, "_warehouse", return_value=warehouse),
			patch.object(review.frappe, "get_doc", return_value=MagicMock()),
			patch.object(review.frappe.db, "get_value", return_value=None),
			patch.object(review.frappe.utils, "today", return_value="2026-10-05"),
			self.assertRaisesRegex(frappe.ValidationError, "enabled reviewer"),
		):
			review.configuration(frappe._dict(config=frappe.as_json(config()), reviewer=""))

	def test_configuration_closes_the_namespace_and_dates(self):
		warehouse = MagicMock(company="Example", is_group=0, disabled=0)
		with (
			patch.object(review, "_warehouse", return_value=warehouse),
			patch.object(review.frappe, "get_doc", return_value=MagicMock()),
			patch.object(review.frappe.utils, "today", return_value="2026-10-05"),
		):
			self.assertEqual(review.configuration_issues(config()), {})
			self.assertIn("config", review.configuration_issues(config(extra="no")))
			self.assertIn("to_date", review.configuration_issues(config(to_date="2026-10-06")))
			self.assertIn("warehouses", review.configuration_issues(config(warehouses=[])))
			self.assertIn("line_limit", review.configuration_issues(config(line_limit=41)))

	def test_future_item_end_of_life_is_not_a_boolean_unavailable_flag(self):
		with patch.object(review, "_site_date", return_value=frappe.utils.getdate("2026-10-05")):
			self.assertTrue(
				review.is_countable_item(
					frappe._dict(is_stock_item=1, disabled=0, end_of_life="2099-12-31")
				)
			)
			self.assertFalse(
				review.is_countable_item(
					frappe._dict(is_stock_item=1, disabled=0, end_of_life="2026-10-05")
				)
			)


class TestCycleCountOutput(FrappeTestCase):
	def check(self, output, data=None):
		data = data or snapshot()
		return review.validate_output(
			frappe._dict(input_snapshot_json=frappe.as_json(data), agent=review.AGENT),
			output["findings"],
			output["coverage"],
			output["scope"],
			output["integrity_digest"],
		)

	def test_valid_output_is_ready_and_never_mentions_stock_reconciliation(self):
		findings, coverage, ready = self.check(result())
		self.assertTrue(ready)
		self.assertEqual(coverage, {review.TOKEN: {"state": "evaluated"}})
		self.assertEqual(findings[0]["proposal"]["lines"][0]["item_code"], "ITEM-A")
		self.assertNotIn("Stock Reconciliation", review.REVIEW_QUESTION)

	def test_changed_book_values_are_rejected_even_with_a_matching_result_digest(self):
		data, out = snapshot(), result()
		out["findings"][0]["proposal"]["lines"][0]["actual_qty"] = "4"
		out["integrity_digest"] = review.digest(
			{"input_digest": data["source_digest"], "scope": out["scope"], "findings": out["findings"], "coverage": out["coverage"]}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "captured stock"):
			self.check(out, data)

	def test_forbidden_inventory_claims_are_rejected(self):
		data, out = snapshot(), result()
		out["findings"][0]["note"] = "A variance needs an inventory adjustment."
		out["integrity_digest"] = review.digest(
			{"input_digest": data["source_digest"], "scope": out["scope"], "findings": out["findings"], "coverage": out["coverage"]}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "cannot assert"):
			self.check(out, data)

	def test_complete_private_file_requires_all_source_rows(self):
		data = snapshot()
		run = frappe._dict(input_snapshot_json=frappe.as_json(data))
		expected = sum(data["source_counts"].values())
		self.assertIsNone(review.validate_envelope(run, True, False, expected))
		self.assertIn("did not read every", review.validate_envelope(run, True, False, expected - 1))


class TestCycleCountPrivateTool(FrappeTestCase):
	def test_tool_is_private_and_registered(self):
		from jarvis.exceptions import InvalidArgumentError
		from jarvis.tools import _private_output, get_cycle_count_inputs as tool, registry

		with patch.object(tool._private_output, "is_private", return_value=False):
			with self.assertRaises(InvalidArgumentError):
				tool.get_cycle_count_inputs()
		self.assertIn("get_cycle_count_inputs", registry._TOOL_NAMES)
		self.assertIn("get_cycle_count_inputs", _private_output.TOOLS)


class TestNativePhysicalCountEntry(FrappeTestCase):
	"""One normal Stock User records a fake observation without posting stock."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "erpnext" not in frappe.get_installed_apps():
			raise cls.skipException("ERPNext is not installed")
		from jarvis.tests import _erpnext_masters as masters

		frappe.set_user("Administrator")
		masters.clear_cache_after_rollback(cls)
		cls.company = masters.ensure_ledger_company()
		cls.item = cls._stock_item(masters)
		masters.ensure_stock_entry_type("Material Receipt")
		masters.ensure_fiscal_years(frappe.utils.today())
		cls.warehouse = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "warehouse_name": "Stores"}
		)
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"company": cls.company,
				"posting_date": frappe.utils.today(),
				"stock_entry_type": "Material Receipt",
				"items": [
					{
						"item_code": cls.item,
						"qty": 5,
						"basic_rate": 25,
						"t_warehouse": cls.warehouse,
					}
				],
			}
		).insert()
		receipt.submit()
		cls.counter = cls._user("cycle-counter@example.invalid", ("Jarvis User", "Stock User"))
		cls.reviewer = cls._user("cycle-reviewer@example.invalid", ("Jarvis User", "Stock User"))

	@staticmethod
	def _user(email, roles):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Cycle Test",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		user = frappe.get_doc("User", email)
		for role in roles:
			if role not in frappe.get_roles(email):
				user.add_roles(role)
		frappe.clear_cache(user=email)
		return email

	@staticmethod
	def _stock_item(masters):
		name = "_J2A Cycle Count Entry Item"
		if frappe.db.exists("Item", name):
			return name
		masters.ensure_item_group_and_uom()
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": name,
				"item_group": masters.ITEM_GROUP,
				"stock_uom": masters.UOM,
				"is_stock_item": 1,
				"is_sales_item": 0,
				"valuation_rate": 25,
			}
		).insert(ignore_permissions=True)
		return name

	def _plan(self, *, pending=False):
		from jarvis.chat.purchase_match import digest

		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "Cycle count entry test"}).insert(
			ignore_permissions=True
		)
		frappe.db.set_value("Jarvis Conversation", conv.name, "owner", self.counter, update_modified=False)
		input_digest = digest({"test": conv.name})
		evidence = {
			"run": "TEST-CYCLE-RUN",
			"input_digest": input_digest,
			"scope": {"company": self.company},
			"reviewer": self.reviewer,
			"finding": {
				"ref_name": self.warehouse,
				"proposal": {
					"outcome": "plan",
					"lines": [
						{
							"item_code": self.item,
							"stock_uom": frappe.db.get_value("Item", self.item, "stock_uom"),
							"capture_required": False,
						}
					],
				},
			},
		}
		doc = frappe.get_doc(
			{
				"doctype": "Jarvis Approval Request",
				"title": "Test physical count plan",
				"source": "Agent Review",
				"status": "Pending",
				"document_type": "Warehouse",
				"conversation": conv.name,
				"ref_doctype": "Warehouse",
				"ref_name": self.warehouse,
				"agent": review.AGENT,
				"preparation_mode": "live",
				"result_class": "derived_candidate",
				"assessment_key": frappe.generate_hash(length=32),
				"assessment_evidence": frappe.as_json(evidence),
				"assessment_input_digest": input_digest,
				"question": "Approve the test count plan.",
				"options": "[]",
			}
		)
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True)
		if not pending:
			frappe.db.set_value(
				"Jarvis Approval Request",
				doc.name,
				{
					"status": "Approved",
					"decision": "Approved for test",
					"decided_by": self.reviewer,
					"decided_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1),
				},
				update_modified=False,
			)
		return frappe.get_doc("Jarvis Approval Request", doc.name)

	def test_normal_stock_user_records_an_idempotent_fact_without_stock_or_ledger_write(self):
		from jarvis.chat import cycle_count_entry

		approved = self._plan()
		pending = self._plan(pending=True)
		pending_evidence = frappe.parse_json(pending.assessment_evidence)
		pending_evidence["input_digest"] = approved.assessment_input_digest
		frappe.db.set_value(
			"Jarvis Approval Request",
			pending.name,
			{
				"assessment_input_digest": approved.assessment_input_digest,
				"assessment_evidence": frappe.as_json(pending_evidence),
			},
			update_modified=False,
		)
		before_qty = frappe.db.get_value("Bin", {"warehouse": self.warehouse, "item_code": self.item}, "actual_qty")
		before_sle = frappe.db.count("Stock Ledger Entry", {"warehouse": self.warehouse, "item_code": self.item})
		entry_key = "cyclecount" + frappe.generate_hash(length=24)
		counted_at = frappe.utils.now()
		frappe.set_user(self.counter)
		try:
			with self.assertRaisesRegex(frappe.ValidationError, "still pending"):
				cycle_count_entry.record_counts(
					approved.name,
					counted_at,
					[{"item_code": self.item, "counted_qty": "0"}],
					entry_key,
				)
			frappe.db.set_value(
				"Jarvis Approval Request",
				pending.name,
				{"status": "Rejected", "decision": "Not this warehouse"},
				update_modified=False,
			)
			first = cycle_count_entry.record_counts(
				approved.name,
				counted_at,
				[{"item_code": self.item, "counted_qty": "0"}],
				entry_key,
			)
			second = cycle_count_entry.record_counts(
				approved.name,
				counted_at,
				[{"item_code": self.item, "counted_qty": "0"}],
				entry_key,
			)
			with self.assertRaisesRegex(frappe.ValidationError, "already have a submitted"):
				cycle_count_entry.record_counts(
					approved.name,
					counted_at,
					[{"item_code": self.item, "counted_qty": "0"}],
					"cyclecount" + frappe.generate_hash(length=24),
				)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(first["saved"][0]["idempotent"], 0)
		self.assertEqual(second["saved"][0]["idempotent"], 1)
		fact = frappe.get_doc("Jarvis Physical Count", first["saved"][0]["name"])
		self.assertEqual((fact.docstatus, fact.recorded_by, fact.items[0].counted_qty), (1, self.counter, "0"))
		self.assertEqual(before_qty, frappe.db.get_value("Bin", {"warehouse": self.warehouse, "item_code": self.item}, "actual_qty"))
		self.assertEqual(before_sle, frappe.db.count("Stock Ledger Entry", {"warehouse": self.warehouse, "item_code": self.item}))
