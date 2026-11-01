"""Cycle-Count Planner custody and output regressions."""

from contextlib import contextmanager
from types import SimpleNamespace
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
		"source_counts": {
			"Bin": 1,
			"Item": 1,
			"Stock Ledger Entry": 1,
			"Stock Entry": 0,
			"Jarvis Physical Count": 0,
			"Jarvis Physical Count Item": 0,
		},
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
			"ledger": [
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


def reseal_snapshot(data):
	data["source_digest"] = review.digest(
		{key: value for key, value in data.items() if key != "source_digest"}
	)
	return data


def reseal_result(data, output):
	output["integrity_digest"] = review.digest(
		{
			"input_digest": data["source_digest"],
			"scope": output["scope"],
			"findings": output["findings"],
			"coverage": output["coverage"],
		}
	)
	return output


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
				review.is_countable_item(frappe._dict(is_stock_item=1, disabled=0, end_of_life="2099-12-31"))
			)
			self.assertFalse(
				review.is_countable_item(frappe._dict(is_stock_item=1, disabled=0, end_of_life="2026-10-05"))
			)


class TestCycleCountCollection(FrappeTestCase):
	def test_collector_keeps_raw_negative_movement_and_exact_stock_entry_headers_private(self):
		company = MagicMock(default_currency="INR")
		company.apply_fieldlevel_read_permissions.return_value = None
		warehouse = MagicMock(company="Example", is_group=0, disabled=0)
		bin_row = {
			"name": "BIN-A",
			"item_code": "ITEM-A",
			"warehouse": "WH-A",
			"actual_qty": 5,
			"stock_value": 100,
			"valuation_rate": 20,
		}
		item_row = {
			"name": "ITEM-A",
			"is_stock_item": 1,
			"disabled": 0,
			"end_of_life": None,
			"stock_uom": "Nos",
			"has_batch_no": 0,
			"has_serial_no": 0,
		}
		ledger_rows = [
			{
				"name": "SLE-DN",
				"item_code": "ITEM-A",
				"warehouse": "WH-A",
				"company": "Example",
				"posting_date": "2026-09-20",
				"posting_time": "09:00:00",
				"actual_qty": -2,
				"voucher_type": "Delivery Note",
				"voucher_no": "DN-1",
				"is_cancelled": 0,
			},
			{
				"name": "SLE-TRANSFER",
				"item_code": "ITEM-A",
				"warehouse": "WH-A",
				"company": "Example",
				"posting_date": "2026-09-21",
				"posting_time": "09:00:00",
				"actual_qty": -3,
				"voucher_type": "Stock Entry",
				"voucher_no": "STE-TRANSFER",
				"is_cancelled": 0,
			},
		]
		entry_row = {
			"name": "STE-TRANSFER",
			"company": "Example",
			"purpose": "Material Transfer",
			"docstatus": 1,
		}

		def get_list(doctype, **_kwargs):
			return {
				"Bin": [bin_row],
				"Item": [item_row],
				"Stock Ledger Entry": ledger_rows,
				"Stock Entry": [entry_row],
				"Jarvis Physical Count": [],
			}[doctype]

		scope = review.scope_from_config(launch_config())
		with (
			patch.object(review.frappe, "get_doc", return_value=company),
			patch.object(review, "_warehouse", return_value=warehouse),
			patch.object(review.frappe, "has_permission", return_value=True),
			patch.object(review.frappe, "get_list", side_effect=get_list),
		):
			captured = review.collect(scope, launch_config())

		self.assertTrue(captured["complete"])
		self.assertEqual(
			set(captured["datasets"]),
			{"bins", "items", "ledger", "stock_entries", "physical_counts", "physical_count_items"},
		)
		self.assertEqual([row["name"] for row in captured["datasets"]["ledger"]], ["SLE-DN", "SLE-TRANSFER"])
		self.assertEqual(captured["datasets"]["stock_entries"], [entry_row])
		self.assertEqual(
			captured["source_counts"],
			{"Bin": 1, "Item": 1, "Stock Ledger Entry": 2, "Stock Entry": 1, "Jarvis Physical Count": 0},
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
			{
				"input_digest": data["source_digest"],
				"scope": out["scope"],
				"findings": out["findings"],
				"coverage": out["coverage"],
			}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "captured stock"):
			self.check(out, data)

	def test_forbidden_inventory_claims_are_rejected(self):
		data, out = snapshot(), result()
		out["findings"][0]["note"] = "A variance needs an inventory adjustment."
		out["integrity_digest"] = review.digest(
			{
				"input_digest": data["source_digest"],
				"scope": out["scope"],
				"findings": out["findings"],
				"coverage": out["coverage"],
			}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "cannot assert"):
			self.check(out, data)

	def test_complete_private_file_requires_all_source_rows(self):
		data = snapshot()
		run = frappe._dict(input_snapshot_json=frappe.as_json(data))
		expected = sum(data["source_counts"].values())
		self.assertIsNone(review.validate_envelope(run, True, False, expected))
		self.assertIn("did not read every", review.validate_envelope(run, True, False, expected - 1))

	def test_invalid_private_file_can_close_only_with_the_fixed_partial_sentinel(self):
		data = snapshot()
		coverage = {
			review.TOKEN: {
				"state": "not_evaluable",
				"reason_code": "record_coverage_insufficient",
				"detail": review.PRIVATE_INPUT_FAILURE_DETAIL,
			}
		}
		output = {
			"findings": [],
			"coverage": coverage,
			"scope": {},
			"integrity_digest": review.digest(
				{"input_digest": None, "scope": {}, "findings": [], "coverage": coverage}
			),
		}
		findings, returned_coverage, ready = self.check(output, data)
		self.assertEqual(findings, [])
		self.assertEqual(returned_coverage, coverage)
		self.assertFalse(ready)

		output["integrity_digest"] = "0" * 64
		with self.assertRaisesRegex(frappe.ValidationError, "captured scope"):
			self.check(output, data)

	def test_private_movement_policy_can_exclude_a_transfer_without_public_reclassification(self):
		data = snapshot()
		data["datasets"]["ledger"][0].update(voucher_type="Stock Entry", voucher_no="STE-TRANSFER")
		data["datasets"]["stock_entries"] = [
			{"name": "STE-TRANSFER", "company": "Example", "purpose": "Material Transfer", "docstatus": 1}
		]
		data["source_counts"]["Stock Entry"] = 1
		reseal_snapshot(data)
		output = result(data)
		line = output["findings"][0]["proposal"]["lines"][0]
		line["outward_qty"] = "0"
		line["velocity"] = ""
		reseal_result(data, output)

		self.assertTrue(self.check(output, data)[2])

	def test_private_movement_value_cannot_exceed_all_captured_negative_movement(self):
		data = snapshot()
		output = result(data)
		output["findings"][0]["proposal"]["lines"][0]["outward_qty"] = "4"
		reseal_result(data, output)

		with self.assertRaisesRegex(frappe.ValidationError, "exceeds the captured negative"):
			self.check(output, data)

	def test_old_outward_ledger_snapshot_finishes_as_partial_during_a_rolling_update(self):
		data = snapshot()
		data["datasets"]["outward_ledger"] = data["datasets"].pop("ledger")
		reseal_snapshot(data)
		output = result(data)

		findings, coverage, ready = self.check(output, data)
		self.assertEqual(findings, [])
		self.assertEqual(coverage[review.TOKEN]["state"], "not_evaluable")
		self.assertEqual(coverage[review.TOKEN]["reason_code"], "record_coverage_insufficient")
		self.assertFalse(ready)

	def test_invalid_private_frame_writeback_ends_partial_without_staging_a_review(self):
		from jarvis.tools import record_agent_run as writeback

		data = snapshot()
		coverage = {
			review.TOKEN: {
				"state": "not_evaluable",
				"reason_code": "record_coverage_insufficient",
				"detail": review.PRIVATE_INPUT_FAILURE_DETAIL,
			}
		}
		output = {
			"findings": [],
			"coverage": coverage,
			"scope": {},
			"truncated": False,
			"rows_consumed": 0,
			"integrity_digest": review.digest(
				{"input_digest": None, "scope": {}, "findings": [], "coverage": coverage}
			),
		}
		run = frappe._dict(
			name="RUN-CYCLE-PRIVATE-FAILURE",
			agent=review.AGENT,
			installation="INST-CYCLE-PRIVATE-FAILURE",
			status="completed",
			input_snapshot_json=frappe.as_json(data),
			preparation_mode="live",
			owner="Administrator",
			findings_count=0,
			blocker_count=0,
			coverage_note="",
			dashboard=None,
		)
		run_row = frappe._dict(
			name=run.name,
			status="running",
			installation=run.installation,
			agent=run.agent,
		)
		state = frappe._dict(status="running", input_snapshot_json=run.input_snapshot_json)
		inst = frappe._dict(name=run.installation, reviewer="Administrator")
		listing = frappe._dict(rule_tokens=frappe.as_json([review.TOKEN]), doctypes_required="[]")

		def get_value(doctype, selector, *_args, **_kwargs):
			if doctype == writeback.RUN:
				return run_row if isinstance(selector, dict) else state
			if doctype == writeback.LISTING:
				return listing
			return None

		with (
			patch("jarvis.tools._agent_run_ctx.get_session_key", return_value="cycle-private-failure"),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(frappe.db, "set_value") as save_output,
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda doctype, _name: run if doctype == writeback.RUN else inst,
			),
			patch("jarvis.chat.agent_runs.record_delegate_run", return_value=run) as persist,
			patch.object(review, "stage_reviews") as stage,
			patch("jarvis.chat.agent_run_steps.record_step"),
		):
			writeback.record_agent_run(**output)

		stage.assert_not_called()
		self.assertEqual(persist.call_args.args[2], [])
		self.assertEqual(persist.call_args.kwargs["coverage"], coverage)
		save_output.assert_called_once()


class TestCycleCountPrivateTool(FrappeTestCase):
	def test_tool_is_private_and_registered(self):
		from jarvis.exceptions import InvalidArgumentError
		from jarvis.tools import _private_output, registry
		from jarvis.tools import get_cycle_count_inputs as tool

		with patch.object(tool._private_output, "is_private", return_value=False):
			with self.assertRaises(InvalidArgumentError):
				tool.get_cycle_count_inputs()
		self.assertIn("get_cycle_count_inputs", registry._TOOL_NAMES)
		self.assertIn("get_cycle_count_inputs", _private_output.TOOLS)

	def test_normal_mode_refuses_before_source_collection(self):
		from jarvis.exceptions import InvalidArgumentError
		from jarvis.tools import _private_output
		from jarvis.tools import get_cycle_count_inputs as tool

		with _private_output.request(False), patch.object(tool.cycle_count_review, "get_inputs") as collect:
			with self.assertRaises(InvalidArgumentError):
				tool.get_cycle_count_inputs()
			collect.assert_not_called()


class TestCycleCountPrivateDispatch(FrappeTestCase):
	"""Exercise the real Cycle source tool through the bound delegate path."""

	def setUp(self):
		from jarvis.tests.test_platform_capability_contract import _mk_listing, _mk_run, _mk_user, _snapshot

		frappe.set_user("Administrator")
		self.slug = "cycle-private-" + frappe.generate_hash(length=10)
		self.key = f"agent:agent-{self.slug}:fake-test"
		self.actor = _mk_user(self.slug + "@example.invalid", roles=("System Manager", "Jarvis User"))
		_mk_listing(self.slug, "Operator", [])
		self.run = _mk_run(
			self.slug,
			self.actor,
			self.key,
			_snapshot(["jarvis__get_cycle_count_inputs"], "operator", []),
		)
		self.conv = frappe.get_doc(
			{"doctype": "Jarvis Conversation", "title": "Cycle private output", "session_key": self.key}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{"doctype": "Jarvis Chat Session", "session_key": self.key, "user": self.actor}
		).insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- private receipt uses a separate transaction

	def tearDown(self):
		from jarvis.tools import _agent_run_ctx

		frappe.set_user("Administrator")
		_agent_run_ctx.clear_session_key()
		for doctype in ("Jarvis Agent Run Step", "Jarvis Agent Activity"):
			field = "run" if doctype == "Jarvis Agent Run Step" else "agent"
			if frappe.db.exists("DocType", doctype):
				frappe.db.delete(doctype, {field: self.run if field == "run" else self.slug})
		frappe.db.delete("Jarvis Chat Message", {"conversation": self.conv.name})
		frappe.delete_doc("Jarvis Conversation", self.conv.name, force=True, ignore_permissions=True)
		frappe.db.delete("Jarvis Chat Session", {"session_key": self.key})
		for doctype in ("Jarvis Agent Run", "Jarvis Agent Installation"):
			for name in frappe.get_all(doctype, {"agent": self.slug}, pluck="name"):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.delete_doc("Jarvis Agent Listing", self.slug, force=True, ignore_permissions=True)
		frappe.delete_doc("User", self.actor, force=True, ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- remove committed test fixtures

	def test_normal_and_malformed_modes_refuse_before_collection(self):
		from jarvis import api
		from jarvis.tools import _private_output

		with patch("jarvis.chat.cycle_count_review.get_inputs") as collect:
			normal = api._dispatch_from_session(
				self.actor, self.key, "get_cycle_count_inputs", {}, result_mode=None
			)
			malformed = api._dispatch_from_session(
				self.actor,
				self.key,
				"get_cycle_count_inputs",
				{"company": "model-selected"},
				result_mode=_private_output.MODE,
			)
		self.assertFalse(normal["ok"], normal)
		self.assertFalse(malformed["ok"], malformed)
		collect.assert_not_called()

	def test_shared_session_refuses_before_cycle_source_collection(self):
		from jarvis import api
		from jarvis.tools import _private_output

		headers = {"X-Jarvis-Token": "test-token"}
		with (
			patch("jarvis.api._get_header", side_effect=headers.get),
			patch("jarvis.api.validate_plugin_request", return_value=self.key),
			patch("jarvis.chat.collaboration.is_collaborative_session", return_value=True),
			patch(
				"jarvis.chat.collaboration.resolve_binding", return_value=SimpleNamespace(actor=self.actor)
			),
			patch("jarvis.chat.collaboration_dispatch.dispatch") as dispatch,
			patch("jarvis.chat.cycle_count_review.get_inputs") as collect,
		):
			result = api.call_tool("get_cycle_count_inputs", {}, result_mode=_private_output.MODE)
		self.assertFalse(result["ok"], result)
		dispatch.assert_not_called()
		collect.assert_not_called()

	def test_private_source_uses_server_canonical_bytes_and_safe_receipt(self):
		import hashlib
		import json

		from jarvis import api
		from jarvis.tools import _private_output

		snapshot = {
			"z_key": "CYCLE-RAW-SOURCE-MARKER",
			"source_digest": "a" * 64,
			"a_key": "first",
		}
		expected = json.dumps(
			{"rows": [snapshot]}, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
		).encode()
		with patch("jarvis.chat.cycle_count_review.get_inputs", return_value=snapshot):
			result = api._dispatch_from_session(
				self.actor,
				self.key,
				"get_cycle_count_inputs",
				{},
				result_mode=_private_output.MODE,
			)
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["data"].encode(), expected)
		self.assertEqual(result["private_output"]["bytes"], len(expected))
		self.assertEqual(result["private_output"]["sha256"], hashlib.sha256(expected).hexdigest())
		message = frappe.get_last_doc("Jarvis Chat Message", {"conversation": self.conv.name})
		self.assertNotIn("CYCLE-RAW-SOURCE-MARKER", message.tool_result)


class TestNativePhysicalCountEntry(FrappeTestCase):
	"""One normal Stock User records a fake observation without posting stock."""

	def setUp(self):
		super().setUp()
		# Most native lifecycle tests construct a minimal sealed approval directly.
		# The dedicated stale-evidence regression below exercises the reviewer
		# re-collection guard with its own controlled source result.
		self._current_evidence = patch("jarvis.chat.cycle_count_entry._assert_current_evidence")
		self._current_evidence.start()
		self.addCleanup(self._current_evidence.stop)

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
		cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "warehouse_name": "Stores"})
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
		cls.stock_variance_user = cls._user(
			"cycle-stock-variance-run@example.invalid", ("Jarvis User", "Stock User")
		)
		cls.other_warehouse = cls._other_warehouse()
		if not frappe.db.exists("Jarvis Agent Listing", review.AGENT):
			from jarvis.chat import agent_catalog

			agent_catalog.sync_agent_listings()

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

	@classmethod
	def _other_warehouse(cls):
		"""A distinct leaf that cannot be an ancestor of the Stores warehouse."""
		warehouse_name = "_J2A Cycle Count Other Scope"
		existing = frappe.db.get_value(
			"Warehouse",
			{"company": cls.company, "warehouse_name": warehouse_name},
			"name",
		)
		if existing:
			return existing
		return (
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": warehouse_name,
					"company": cls.company,
					"is_group": 0,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	@classmethod
	def _review_run(cls, conversation, *, status="completed"):
		"""Create the real run row that fences this sealed approval cohort."""
		run = frappe.get_doc(
			{
				"doctype": "Jarvis Agent Run",
				"agent": review.AGENT,
				"status": status,
				"started_at": frappe.utils.now(),
				"finished_at": frappe.utils.now(),
				"conversation": conversation,
				"preparation_mode": "live",
			}
		)
		run.flags.ignore_validate = True
		return run.insert(ignore_permissions=True, ignore_mandatory=True)

	@contextmanager
	def _delegate_run(self, agent, scope):
		"""Create the server-side rows that an active delegate dispatch owns."""
		from jarvis.tools import _agent_run_ctx

		user = self.stock_variance_user
		if not frappe.db.exists("Jarvis Agent Listing", agent):
			from jarvis.chat import agent_catalog

			agent_catalog.sync_agent_listings()
		session_key = f"agent:agent-{agent}:{frappe.generate_hash(length=16)}"
		original_user = frappe.session.user
		frappe.set_user(user)
		try:
			installation = frappe.get_doc(
				{
					"doctype": "Jarvis Agent Installation",
					"agent": agent,
					"enabled": 1,
					"run_as_user": user,
					"installable": 1,
					"activation_state": "shadow",
					"reviewer": user,
					# This deliberately has no scope. The permission exception must
					# use only the server-stamped Run.scope_json below.
					"config": "{}",
				}
			)
			installation.flags.ignore_validate = True
			installation.insert(ignore_permissions=True, ignore_mandatory=True)
			frappe.get_doc(
				{
					"doctype": "Jarvis Chat Session",
					"session_key": session_key,
					"user": user,
				}
			).insert(ignore_permissions=True)
			run = frappe.get_doc(
				{
					"doctype": "Jarvis Agent Run",
					"agent": agent,
					"installation": installation.name,
					"status": "running",
					"started_at": frappe.utils.now(),
					"session_key": session_key,
					"scope_json": frappe.as_json(scope),
				}
			)
			run.flags.ignore_validate = True
			run.insert(ignore_permissions=True, ignore_mandatory=True)
			_agent_run_ctx.set_session_key(session_key)
			yield frappe._dict(installation=installation.name, run=run.name, session_key=session_key)
		finally:
			_agent_run_ctx.clear_session_key()
			frappe.set_user("Administrator")
			if "run" in locals():
				frappe.db.delete("Jarvis Agent Run", {"name": run.name})
			frappe.db.delete("Jarvis Chat Session", {"session_key": session_key})
			if "installation" in locals():
				frappe.db.delete("Jarvis Agent Installation", {"name": installation.name})
			frappe.set_user(original_user)

	def _assert_physical_count_readable(self, user, fact_name, expected, message):
		"""Exercise the real list and single-document permission paths."""
		frappe.set_user(user)
		self.assertEqual(
			frappe.get_list("Jarvis Physical Count", filters={"name": fact_name}, pluck="name"),
			[fact_name] if expected else [],
			message,
		)
		self.assertEqual(
			frappe.has_permission("Jarvis Physical Count", "read", doc=fact_name, user=user),
			expected,
			message,
		)

	def _assert_physical_count_items_readable(self, user, fact_name, expected, message):
		"""Exercise both the ORM and generic API child-list paths."""
		frappe.set_user(user)
		expected_parents = [fact_name] if expected else []
		self.assertEqual(
			frappe.get_list(
				"Jarvis Physical Count Item",
				filters={"parent": fact_name},
				pluck="parent",
				parent_doctype="Jarvis Physical Count",
			),
			expected_parents,
			message,
		)
		from frappe import client

		self.assertEqual(
			[
				row["parent"]
				for row in client.get_list(
					"Jarvis Physical Count Item",
					fields=["parent"],
					filters={"parent": fact_name},
					parent="Jarvis Physical Count",
				)
			],
			expected_parents,
			"generic child-list API: " + message,
		)

	def _plan(self, *, pending=False):
		from jarvis.chat.purchase_match import digest

		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "Cycle count entry test"}).insert(
			ignore_permissions=True
		)
		frappe.db.set_value("Jarvis Conversation", conv.name, "owner", self.counter, update_modified=False)
		run = self._review_run(conv.name)
		input_digest = digest({"test": conv.name})
		evidence = {
			"run": run.name,
			"input_digest": input_digest,
			"scope": {"company": self.company},
			"policy": {"test": "cycle-count-entry"},
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
				"run": run.name,
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

	def test_cycle_count_staging_locks_a_running_run_before_creating_reviews(self):
		"""The run row fences all sealed-cohort membership writes."""
		conversation = frappe.get_doc(
			{"doctype": "Jarvis Conversation", "title": "Cycle count staging fence"}
		).insert(ignore_permissions=True)
		run = self._review_run(conversation.name, status="running")
		events = []
		real_get_value = frappe.db.get_value

		def tracked_get_value(doctype, *args, **kwargs):
			if doctype == "Jarvis Agent Run" and kwargs.get("for_update"):
				events.append("run-fence")
			return real_get_value(doctype, *args, **kwargs)

		with (
			patch.object(review.frappe.db, "get_value", side_effect=tracked_get_value),
			patch.object(
				operator_review, "stage_reviews", side_effect=lambda *_: events.append("stage") or []
			),
		):
			self.assertEqual(
				review.stage_reviews(run, frappe._dict(), [{"proposal": {"outcome": "plan"}}]), []
			)
		self.assertEqual(events, ["run-fence", "stage"])

		terminal = self._review_run(conversation.name, status="completed")
		with patch.object(operator_review, "stage_reviews") as stage:
			with self.assertRaisesRegex(frappe.PermissionError, "valid Agent Run"):
				review.stage_reviews(terminal, frappe._dict(), [{"proposal": {"outcome": "plan"}}])
			stage.assert_not_called()

	def test_cohort_gate_uses_a_current_run_scan_and_refuses_missing_run(self):
		"""A writer never decides sibling membership from its old RR snapshot."""
		from jarvis.chat import cycle_count_entry

		approved = self._plan()
		ar, evidence = cycle_count_entry._approval_context(approved.name)
		key = cycle_count_entry._approval_cohort_key(ar, evidence)
		calls = []
		real_get_values = frappe.db.get_values

		def tracked_get_values(doctype, *args, **kwargs):
			if doctype == "Jarvis Approval Request":
				calls.append((args, kwargs))
			return real_get_values(doctype, *args, **kwargs)

		with patch.object(cycle_count_entry.frappe.db, "get_values", side_effect=tracked_get_values):
			cohort = cycle_count_entry._lock_approval_cohort(key)
		self.assertIn(approved.name, cohort.names)
		self.assertEqual(len(calls), 1)
		args, kwargs = calls[0]
		self.assertEqual(kwargs["filters"], {"run": approved.run})
		self.assertEqual(kwargs["order_by"], "name")
		self.assertTrue(kwargs["for_update"])

		missing_run = "missing-cycle-run-" + frappe.generate_hash(length=16)
		missing_evidence = frappe.parse_json(approved.assessment_evidence)
		missing_evidence["run"] = missing_run
		frappe.db.set_value(
			"Jarvis Approval Request",
			approved.name,
			{"run": missing_run, "assessment_evidence": frappe.as_json(missing_evidence)},
			update_modified=False,
		)
		ar, evidence = cycle_count_entry._approval_context(approved.name)
		with self.assertRaisesRegex(frappe.PermissionError, "valid sealed review run"):
			cycle_count_entry._lock_approval_cohort(cycle_count_entry._approval_cohort_key(ar, evidence))

	def test_cohort_scan_includes_a_sibling_visible_after_the_run_fence(self):
		"""Membership comes from the current post-fence scan, not preflight state."""
		from jarvis.chat import cycle_count_entry

		approved = self._plan()
		ar, evidence = cycle_count_entry._approval_context(approved.name)
		key = cycle_count_entry._approval_cohort_key(ar, evidence)
		row = frappe._dict(
			name=approved.name,
			run=ar.run,
			conversation=ar.conversation,
			agent=ar.agent,
			source=ar.source,
			result_class=ar.result_class,
			ref_doctype=ar.ref_doctype,
			ref_name=ar.ref_name,
			assessment_input_digest=ar.assessment_input_digest,
			assessment_evidence=ar.assessment_evidence,
			status=ar.status,
		)
		late_sibling = frappe._dict({**row, "name": "late-cycle-sibling", "status": "Pending"})
		events = []
		real_get_values = frappe.db.get_values

		def fenced(actual_key):
			self.assertEqual(actual_key, key)
			events.append("run-fence")

		def current_scan(doctype, *args, **kwargs):
			if doctype != "Jarvis Approval Request":
				return real_get_values(doctype, *args, **kwargs)
			self.assertEqual(doctype, "Jarvis Approval Request")
			self.assertEqual(events, ["run-fence"])
			self.assertEqual(kwargs["filters"], {"run": ar.run})
			self.assertEqual(kwargs["order_by"], "name")
			self.assertTrue(kwargs["for_update"])
			events.append("current-scan")
			return [row, late_sibling]

		with (
			patch.object(cycle_count_entry, "_lock_approval_cohort_run", side_effect=fenced),
			patch.object(cycle_count_entry.frappe.db, "get_values", side_effect=current_scan),
		):
			cohort = cycle_count_entry._lock_approval_cohort(key)
		self.assertEqual(events, ["run-fence", "current-scan"])
		self.assertEqual(set(cohort.names), {approved.name, late_sibling.name})
		with self.assertRaisesRegex(frappe.ValidationError, "still pending"):
			cycle_count_entry._assert_no_pending_sibling(cohort)

	def test_normal_stock_user_records_an_idempotent_fact_without_stock_or_ledger_write(self):
		from jarvis.chat import cycle_count_entry

		approved = self._plan()
		pending = self._plan(pending=True)
		pending_evidence = frappe.parse_json(pending.assessment_evidence)
		pending_evidence["run"] = approved.run
		pending_evidence["input_digest"] = approved.assessment_input_digest
		frappe.db.set_value(
			"Jarvis Approval Request",
			pending.name,
			{
				"conversation": approved.conversation,
				"run": approved.run,
				"assessment_input_digest": approved.assessment_input_digest,
				"assessment_evidence": frappe.as_json(pending_evidence),
			},
			update_modified=False,
		)
		before_qty = frappe.db.get_value(
			"Bin", {"warehouse": self.warehouse, "item_code": self.item}, "actual_qty"
		)
		before_sle = frappe.db.count(
			"Stock Ledger Entry", {"warehouse": self.warehouse, "item_code": self.item}
		)
		before_reconciliation = frappe.db.count("Stock Reconciliation", {"company": self.company})
		entry_key = "cyclecount" + frappe.generate_hash(length=24)
		counted_at = frappe.utils.now()
		frappe.set_user(self.counter)
		try:
			self.assertTrue(frappe.has_permission("Jarvis Physical Count", "create"))
			self.assertTrue(frappe.has_permission("Jarvis Physical Count", "submit"))
			self.assertTrue(frappe.has_permission("Warehouse", "read", doc=self.warehouse))
			self.assertTrue(frappe.has_permission("Item", "read", doc=self.item))
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
		self.assertEqual(
			(fact.docstatus, fact.recorded_by, fact.items[0].counted_qty), (1, self.counter, "0")
		)
		self.assertEqual(
			before_qty,
			frappe.db.get_value("Bin", {"warehouse": self.warehouse, "item_code": self.item}, "actual_qty"),
		)
		self.assertEqual(
			before_sle,
			frappe.db.count("Stock Ledger Entry", {"warehouse": self.warehouse, "item_code": self.item}),
		)
		self.assertEqual(
			before_reconciliation,
			frappe.db.count("Stock Reconciliation", {"company": self.company}),
		)

	def test_pending_plan_with_the_same_digest_from_another_run_does_not_block(self):
		"""A digest collision does not make two independently sealed runs siblings."""
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
		frappe.set_user(self.counter)
		try:
			result = cycle_count_entry.record_counts(
				approved.name,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				"cyclecount" + frappe.generate_hash(length=24),
			)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(result["saved"][0]["idempotent"], 0)

	def test_foreign_entry_key_never_enters_a_plans_locked_retry_map(self):
		from jarvis.chat import cycle_count_entry

		first, second = self._plan(), self._plan()
		entry_key = "cyclecount" + frappe.generate_hash(length=24)
		frappe.set_user(self.counter)
		try:
			cycle_count_entry.record_counts(
				first.name,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				entry_key,
			)
			seen_maps = []
			real_locked_entry = cycle_count_entry._locked_plan_entry

			def track_locked_entry(entry_keys, key):
				seen_maps.append(dict(entry_keys))
				return real_locked_entry(entry_keys, key)

			with (
				patch.object(cycle_count_entry, "_locked_plan_entry", side_effect=track_locked_entry),
				self.assertRaisesRegex(frappe.ValidationError, "count-entry key was already used"),
			):
				cycle_count_entry.record_counts(
					second.name,
					frappe.utils.now(),
					[{"item_code": self.item, "counted_qty": "0"}],
					entry_key,
				)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(len(seen_maps), 1)
		self.assertNotIn(entry_key, seen_maps[0])

	def test_foreign_correction_source_fails_before_any_current_count_lock(self):
		from jarvis.chat import cycle_count_entry

		target, foreign = self._plan(), self._plan()
		frappe.set_user(self.counter)
		try:
			foreign_fact = cycle_count_entry.record_counts(
				foreign.name,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				"cyclecount" + frappe.generate_hash(length=24),
			)["saved"][0]["name"]
			with (
				patch.object(cycle_count_entry, "_lock_plan_count_facts", side_effect=AssertionError),
				patch.object(
					cycle_count_entry.frappe,
					"get_doc",
					wraps=cycle_count_entry.frappe.get_doc,
				) as get_doc,
				self.assertRaises(frappe.PermissionError),
			):
				cycle_count_entry.correct_counts(
					target.name,
					foreign_fact,
					frappe.utils.now(),
					[{"item_code": self.item, "counted_qty": "1"}],
					"cyclecount" + frappe.generate_hash(length=24),
				)
		finally:
			frappe.set_user("Administrator")
		self.assertFalse(
			any(
				call.args[:2] == (cycle_count_entry.COUNT, foreign_fact) and call.kwargs.get("for_update")
				for call in get_doc.call_args_list
			)
		)

	def test_controlled_endpoint_locks_plan_facts_before_the_shared_plan_gate(self):
		from jarvis.chat import cycle_count_entry

		approved = self._plan()
		events = []
		real_plan_facts = cycle_count_entry._lock_plan_count_facts
		real_gate = cycle_count_entry._lock_approval_cohort

		def tracked_plan_facts(request):
			events.append("fact")
			return real_plan_facts(request)

		def tracked_gate(key):
			events.append("review")
			return real_gate(key)

		frappe.set_user(self.counter)
		try:
			with (
				patch.object(cycle_count_entry, "_lock_plan_count_facts", side_effect=tracked_plan_facts),
				patch.object(cycle_count_entry, "_lock_approval_cohort", side_effect=tracked_gate),
				patch.object(cycle_count_entry, "_active_counted_items", side_effect=AssertionError),
			):
				cycle_count_entry.record_counts(
					approved.name,
					frappe.utils.now(),
					[{"item_code": self.item, "counted_qty": "0"}],
					"cyclecount" + frappe.generate_hash(length=24),
				)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(events[0], "fact")
		first_review = events.index("review")
		self.assertNotIn("fact", events[first_review + 1 :])

		class StopAtReviewGate(Exception):
			pass

		correction_events = []
		ar = frappe._dict(name="AR-CYCLE-LOCK", status="Approved")
		evidence = {"run": "TEST-LOCK"}
		key = (
			"TEST-LOCK",
			"CONV-LOCK",
			cycle_count_entry.AGENT,
			"Agent Review",
			"derived_candidate",
			"Warehouse",
			"d" * 64,
		)

		def correction_plan_facts(request):
			self.assertEqual(request, ar.name)
			correction_events.append("plan-fact")
			return {}, set(), {}

		def stop_at_review_gate(actual_key):
			self.assertEqual(actual_key, key)
			correction_events.append("review")
			raise StopAtReviewGate

		with (
			patch.object(cycle_count_entry, "_preliminary_target_context", return_value=(ar, evidence)),
			patch.object(cycle_count_entry, "_approval_cohort_key", return_value=key),
			patch.object(cycle_count_entry, "_preliminary_source_matches_plan"),
			patch.object(cycle_count_entry, "_lock_plan_count_facts", side_effect=correction_plan_facts),
			patch.object(cycle_count_entry, "_lock_approval_cohort", side_effect=stop_at_review_gate),
			self.assertRaises(StopAtReviewGate),
		):
			cycle_count_entry.correct_counts(
				ar.name,
				"COUNT-LOCK",
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				"cyclecount" + frappe.generate_hash(length=24),
			)
		self.assertEqual(correction_events, ["plan-fact", "review"])

	def test_native_mutations_fail_before_any_count_lock(self):
		from jarvis.chat import cycle_count_entry

		approved = self._plan()

		def raw_doc(**values):
			return frappe.get_doc(
				{
					"doctype": "Jarvis Physical Count",
					"approval_request": approved.name,
					"counted_at": frappe.utils.now(),
					"entry_key": "cyclecount" + frappe.generate_hash(length=24),
					"items": [{"item_code": self.item, "counted_qty": "0"}],
					**values,
				}
			)

		def assert_no_count_lock(operation):
			events = []
			with (
				patch.object(
					cycle_count_entry, "_lock_plan_count_facts", side_effect=lambda *_: events.append("fact")
				),
				patch.object(
					cycle_count_entry, "_lock_approval_cohort", side_effect=lambda *_: events.append("cohort")
				),
				self.assertRaises(frappe.PermissionError),
			):
				operation()
			self.assertEqual(events, [])

		frappe.set_user(self.counter)
		try:
			assert_no_count_lock(lambda: raw_doc().insert())
			for forged in (True, "endpoint-token"):
				forged_doc = raw_doc()
				forged_doc.flags.cycle_count_controlled_mutation_token = forged
				assert_no_count_lock(forged_doc.insert)
			fact_name = cycle_count_entry.record_counts(
				approved.name,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				"cyclecount" + frappe.generate_hash(length=24),
			)["saved"][0]["name"]
			assert_no_count_lock(lambda: raw_doc(amended_from=fact_name).insert())
			assert_no_count_lock(lambda: frappe.get_doc("Jarvis Physical Count", fact_name).cancel())
			draft = raw_doc()
			draft.flags.ignore_validate = True
			draft.insert(ignore_permissions=True, ignore_mandatory=True)
			assert_no_count_lock(lambda: frappe.get_doc("Jarvis Physical Count", draft.name).submit())
			assert_no_count_lock(lambda: frappe.get_doc("Jarvis Physical Count", draft.name).discard())
		finally:
			frappe.set_user("Administrator")
		try:
			# Frappe normally bypasses custom permission hooks for Administrator.
			# The DocType gate must still refuse before `check_if_latest` can lock.
			assert_no_count_lock(lambda: raw_doc().insert())
			assert_no_count_lock(lambda: frappe.get_doc("Jarvis Physical Count", fact_name).cancel())
		finally:
			frappe.set_user("Administrator")

	def test_endpoints_keep_the_server_token_through_insert_submit_and_cancel(self):
		from jarvis.chat import cycle_count_entry

		approved = self._plan()
		seen = []
		token_checks = []
		real_validate = cycle_count_entry.validate_count_document
		real_token = cycle_count_entry.is_controlled_count_mutation

		def tracked_validate(doc, *, action="record"):
			seen.append((action, cycle_count_entry.is_controlled_count_mutation(doc)))
			return real_validate(doc, action=action)

		def tracked_token(doc):
			value = real_token(doc)
			token_checks.append(value)
			return value

		frappe.set_user(self.counter)
		try:
			with (
				patch.object(cycle_count_entry, "is_controlled_count_mutation", side_effect=tracked_token),
				patch.object(cycle_count_entry, "validate_count_document", side_effect=tracked_validate),
			):
				source_name = cycle_count_entry.record_counts(
					approved.name,
					frappe.utils.now(),
					[{"item_code": self.item, "counted_qty": "0"}],
					"cyclecount" + frappe.generate_hash(length=24),
				)["saved"][0]["name"]
				cycle_count_entry.correct_counts(
					approved.name,
					source_name,
					frappe.utils.now(),
					[{"item_code": self.item, "counted_qty": "1"}],
					"cyclecount" + frappe.generate_hash(length=24),
				)
		finally:
			frappe.set_user("Administrator")
		self.assertTrue(seen)
		self.assertTrue(all(token for _, token in seen))
		self.assertIn("cancel", [action for action, _ in seen])
		self.assertGreaterEqual(len(token_checks), 5)
		self.assertTrue(all(token_checks))

	def test_locked_plan_facts_reject_a_t1_commit_after_a_pre_gate_read(self):
		"""A current fact lock must beat an older repeatable-read projection."""
		from jarvis.chat import cycle_count_entry

		approved = self._plan()
		# T2 makes an ordinary read before it reaches the entry code. In production
		# this can happen in the Jarvis access gate or a Desk permission hook.
		self.assertEqual(cycle_count_entry._active_counted_items(approved.name), {})
		events = ["t2-pre-gate-read"]

		def current_plan_facts_after_t1(request):
			self.assertEqual(request, approved.name)
			events.append("t1-commit-then-t2-current-fact-lock")
			return {self.item: "COUNT-T1"}, set(), {}

		frappe.set_user(self.counter)
		try:
			with (
				patch.object(
					cycle_count_entry,
					"_lock_plan_count_facts",
					side_effect=current_plan_facts_after_t1,
				),
				patch.object(cycle_count_entry, "_active_counted_items", side_effect=AssertionError),
			):
				with self.assertRaisesRegex(frappe.ValidationError, "already have a submitted"):
					cycle_count_entry.record_counts(
						approved.name,
						frappe.utils.now(),
						[{"item_code": self.item, "counted_qty": "0"}],
						"cyclecount" + frappe.generate_hash(length=24),
					)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(events, ["t2-pre-gate-read", "t1-commit-then-t2-current-fact-lock"])

	def test_counter_and_reviewer_can_correct_only_the_current_count_fact(self):
		from jarvis.chat import cycle_count_entry

		if not frappe.db.has_column("Jarvis Physical Count", "amended_from"):
			self.skipTest("The current test site has not synchronized the new correction column.")
		approved = self._plan()
		first_key = "cyclecount" + frappe.generate_hash(length=24)
		counter_key = "cyclecount" + frappe.generate_hash(length=24)
		reviewer_key = "cyclecount" + frappe.generate_hash(length=24)
		counter_at = frappe.utils.now()
		frappe.set_user(self.counter)
		try:
			first = cycle_count_entry.record_counts(
				approved.name,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				first_key,
			)["saved"][0]["name"]
			counter = cycle_count_entry.correct_counts(
				approved.name,
				first,
				counter_at,
				[{"item_code": self.item, "counted_qty": "1"}],
				counter_key,
			)
			self.assertEqual(counter["saved"][0]["idempotent"], 0)
			self.assertEqual(
				cycle_count_entry.correct_counts(
					approved.name,
					first,
					counter_at,
					[{"item_code": self.item, "counted_qty": "1"}],
					counter_key,
				)["saved"][0]["idempotent"],
				1,
			)
		finally:
			frappe.set_user("Administrator")
		second = counter["saved"][0]["name"]
		self.assertEqual(frappe.get_doc("Jarvis Physical Count", first).docstatus, 2)
		second_doc = frappe.get_doc("Jarvis Physical Count", second)
		self.assertEqual(
			(
				second_doc.docstatus,
				second_doc.amended_from,
				second_doc.recorded_by,
				second_doc.items[0].counted_qty,
			),
			(1, first, self.counter, "1"),
		)

		frappe.set_user(self.reviewer)
		try:
			third = cycle_count_entry.correct_counts(
				approved.name,
				second,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "2"}],
				reviewer_key,
			)["saved"][0]["name"]
		finally:
			frappe.set_user("Administrator")
		third_doc = frappe.get_doc("Jarvis Physical Count", third)
		self.assertEqual(
			(third_doc.docstatus, third_doc.amended_from, third_doc.recorded_by), (1, second, self.reviewer)
		)
		self.assertEqual(frappe.get_doc("Jarvis Physical Count", second).docstatus, 2)
		# The controlled endpoint owns both cancellation and the replacement. It
		# stamps a fresh fact instead of inheriting server-owned predecessor fields.
		frappe.set_user(self.reviewer)
		try:
			fourth = cycle_count_entry.correct_counts(
				approved.name,
				third,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "3"}],
				"cyclecount" + frappe.generate_hash(length=24),
			)["saved"][0]["name"]
		finally:
			frappe.set_user("Administrator")
		fourth_doc = frappe.get_doc("Jarvis Physical Count", fourth)
		self.assertEqual(
			(fourth_doc.docstatus, fourth_doc.amended_from, fourth_doc.recorded_by),
			(1, third, self.reviewer),
		)

		frappe.set_user(self.counter)
		try:
			with self.assertRaisesRegex(frappe.PermissionError, "not current"):
				cycle_count_entry.correct_counts(
					approved.name,
					first,
					frappe.utils.now(),
					[{"item_code": self.item, "counted_qty": "4"}],
					"cyclecount" + frappe.generate_hash(length=24),
				)
		finally:
			frappe.set_user("Administrator")

		outsider = self._user("cycle-correct-outsider@example.invalid", ("Jarvis User", "Stock User"))
		frappe.set_user(outsider)
		try:
			with self.assertRaises(frappe.PermissionError):
				cycle_count_entry.correct_counts(
					approved.name,
					fourth_doc.name,
					frappe.utils.now(),
					[{"item_code": self.item, "counted_qty": "4"}],
					"cyclecount" + frappe.generate_hash(length=24),
				)
		finally:
			frappe.set_user("Administrator")

	def test_new_count_refuses_changed_evidence_for_controlled_endpoint(self):
		from jarvis.chat import cycle_count_entry

		approved = self._plan()
		self._current_evidence.stop()
		seen_users = []

		def changed_source(scope, policy):
			seen_users.append(frappe.session.user)
			return {"source_digest": "changed-source-digest"}

		frappe.set_user(self.counter)
		try:
			with patch.object(cycle_count_entry.cycle_count_review, "collect", side_effect=changed_source):
				with self.assertRaisesRegex(frappe.ValidationError, "stale or no longer fully readable"):
					cycle_count_entry.record_counts(
						approved.name,
						frappe.utils.now(),
						[{"item_code": self.item, "counted_qty": "0"}],
						"cyclecount" + frappe.generate_hash(length=24),
					)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(seen_users, [self.reviewer])

	def test_fresh_reviewer_snapshot_rejects_a_commit_after_the_primary_read(self):
		"""A source commit before the fresh snapshot must not reuse the request RR view."""
		from decimal import Decimal

		from frappe.database import get_db

		from jarvis.chat import cycle_count_entry, cycle_count_review

		self._current_evidence.stop()
		record_plan, correction_plan = self._plan(), self._plan()
		baseline_digest = "fresh-cycle-source-" + frappe.generate_hash(length=20)
		for plan in (record_plan, correction_plan):
			evidence = frappe.parse_json(plan.assessment_evidence)
			evidence["input_digest"] = baseline_digest
			frappe.db.set_value(
				"Jarvis Approval Request",
				plan.name,
				{
					"assessment_input_digest": baseline_digest,
					"assessment_evidence": frappe.as_json(evidence),
				},
				update_modified=False,
			)
		record_plan = frappe.get_doc("Jarvis Approval Request", record_plan.name)
		correction_plan = frappe.get_doc("Jarvis Approval Request", correction_plan.name)
		marker_bin = "fresh-cycle-bin-" + frappe.generate_hash(length=20)
		original_qty = "0"
		changed_qty = str(Decimal(original_qty) + Decimal("1"))
		primary_db = frappe.local.db
		collector_connections = []

		def collect_after_current_read(scope, policy):
			collector_connections.append(frappe.local.db)
			current_qty = Decimal(str(frappe.db.get_value("Bin", marker_bin, "actual_qty")))
			return {
				"source_digest": baseline_digest if current_qty == Decimal(original_qty) else "changed-source"
			}

		def with_other(sql, values):
			conf = frappe.conf
			other = get_db(
				socket=conf.db_socket,
				host=conf.db_host,
				port=conf.db_port,
				user=conf.db_user or conf.db_name,
				password=conf.db_password,
				cur_db_name=conf.db_name,
			)
			other.connect()
			try:
				other.sql(sql, values)
				other.commit()
			finally:
				other.close()

		with_other(
			"""
			INSERT INTO `tabBin`
				(`name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`, `idx`,
				 `item_code`, `warehouse`, `actual_qty`, `stock_uom`, `company`)
			VALUES (%s, NOW(6), NOW(6), %s, %s, 0, 0, %s, %s, %s, %s, %s)
			""",
			(
				marker_bin,
				"Administrator",
				"Administrator",
				"fresh-cycle-item-" + frappe.generate_hash(length=20),
				self.warehouse,
				original_qty,
				"Nos",
				self.company,
			),
		)

		frappe.set_user(self.counter)
		try:
			with (
				patch.object(cycle_count_review, "collect", side_effect=collect_after_current_read),
				patch.object(
					cycle_count_entry,
					"_source_digest_excluding_cohort_facts",
					side_effect=lambda current, _: current["source_digest"],
				),
			):
				source_name = cycle_count_entry.record_counts(
					correction_plan.name,
					frappe.utils.now(),
					[{"item_code": self.item, "counted_qty": "0"}],
					"cyclecount" + frappe.generate_hash(length=24),
				)["saved"][0]["name"]
				# This ordinary read pins the primary request snapshot before T1's
				# independent commit. The endpoint must collect through another one.
				# The test proves the before-snapshot guard only; the contract does not
				# claim source-write serialization after the fresh snapshot starts.
				frappe.db.sql("SELECT actual_qty FROM `tabBin` WHERE name = %s", marker_bin)
				with_other("UPDATE `tabBin` SET actual_qty = %s WHERE name = %s", (changed_qty, marker_bin))
				with self.assertRaisesRegex(frappe.ValidationError, "stale or no longer fully readable"):
					cycle_count_entry.record_counts(
						record_plan.name,
						frappe.utils.now(),
						[{"item_code": self.item, "counted_qty": "1"}],
						"cyclecount" + frappe.generate_hash(length=24),
					)
				with self.assertRaisesRegex(frappe.ValidationError, "stale or no longer fully readable"):
					cycle_count_entry.correct_counts(
						correction_plan.name,
						source_name,
						frappe.utils.now(),
						[{"item_code": self.item, "counted_qty": "1"}],
						"cyclecount" + frappe.generate_hash(length=24),
					)
		finally:
			with_other("DELETE FROM `tabBin` WHERE name = %s", (marker_bin,))
			frappe.set_user("Administrator")
		self.assertTrue(collector_connections)
		self.assertTrue(all(connection is not primary_db for connection in collector_connections))
		self.assertEqual(frappe.db.get_value("Jarvis Physical Count", source_name, "docstatus"), 1)

	def test_correction_refuses_changed_evidence_for_controlled_endpoint(self):
		from jarvis.chat import cycle_count_entry

		if not frappe.db.has_column("Jarvis Physical Count", "amended_from"):
			self.skipTest("The current test site has not synchronized the new correction column.")
		approved = self._plan()
		frappe.set_user(self.counter)
		try:
			source_name = cycle_count_entry.record_counts(
				approved.name,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				"cyclecount" + frappe.generate_hash(length=24),
			)["saved"][0]["name"]
		finally:
			frappe.set_user("Administrator")

		self._current_evidence.stop()
		seen_users = []

		def changed_source(scope, policy):
			seen_users.append(frappe.session.user)
			return {"source_digest": "changed-source-digest"}

		frappe.set_user(self.counter)
		try:
			with patch.object(cycle_count_entry.cycle_count_review, "collect", side_effect=changed_source):
				with self.assertRaisesRegex(frappe.ValidationError, "stale or no longer fully readable"):
					cycle_count_entry.correct_counts(
						approved.name,
						source_name,
						frappe.utils.now(),
						[{"item_code": self.item, "counted_qty": "1"}],
						"cyclecount" + frappe.generate_hash(length=24),
					)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(seen_users, [self.reviewer])

	def test_cancelled_fact_cannot_be_replaced_by_a_new_count(self):
		from jarvis.chat import cycle_count_entry

		if not frappe.db.has_column("Jarvis Physical Count", "amended_from"):
			self.skipTest("The current test site has not synchronized the new correction column.")
		owner = self._user("cycle-plan-owner@example.invalid", ("Jarvis User", "Stock User"))
		approved = self._plan()
		frappe.db.set_value(
			"Jarvis Conversation", approved.conversation, "owner", owner, update_modified=False
		)
		frappe.set_user(owner)
		try:
			with (
				patch.object(cycle_count_entry, "_lock_plan_count_facts", return_value=({}, {self.item}, {})),
				self.assertRaisesRegex(frappe.PermissionError, "controlled amendment"),
			):
				cycle_count_entry.record_counts(
					approved.name,
					frappe.utils.now(),
					[{"item_code": self.item, "counted_qty": "1"}],
					"cyclecount" + frappe.generate_hash(length=24),
				)
		finally:
			frappe.set_user("Administrator")

	def test_savedocs_cancel_cannot_forge_the_recorder(self):
		from frappe.desk.form.save import savedocs

		from jarvis.chat import cycle_count_entry

		approved = self._plan()
		frappe.set_user(self.counter)
		try:
			fact_name = cycle_count_entry.record_counts(
				approved.name,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				"cyclecount" + frappe.generate_hash(length=24),
			)["saved"][0]["name"]
		finally:
			frappe.set_user("Administrator")
		viewer = self._user("cycle-cancel-viewer@example.invalid", ("Jarvis User", "Stock User"))
		frappe.db.set_value(
			"Jarvis Conversation", approved.conversation, "owner", viewer, update_modified=False
		)
		payload = frappe.get_doc("Jarvis Physical Count", fact_name).as_dict()
		payload["recorded_by"] = viewer
		frappe.set_user(viewer)
		try:
			with self.assertRaises(frappe.PermissionError):
				savedocs(frappe.as_json(payload), "Cancel")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Jarvis Physical Count", fact_name, "docstatus"), 1)
		payload = frappe.get_doc("Jarvis Physical Count", fact_name).as_dict()
		payload["items"][0]["counted_qty"] = "999"
		frappe.set_user(viewer)
		try:
			with self.assertRaises(frappe.PermissionError):
				savedocs(frappe.as_json(payload), "Cancel")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Jarvis Physical Count", fact_name, "docstatus"), 1)

	def test_malformed_count_time_is_a_validation_error(self):
		from jarvis.chat import cycle_count_entry

		with patch.object(cycle_count_entry.frappe.utils, "get_datetime", return_value=None):
			with self.assertRaisesRegex(frappe.ValidationError, "valid site date and time"):
				cycle_count_entry._counted_at("2026-11-01 10:00:00")

	def test_draft_amendment_successor_does_not_block_the_current_fact(self):
		from jarvis.chat import cycle_count_entry

		ar = frappe._dict(name="AR-CYCLE-SUCCESSOR", ref_name=self.warehouse)
		evidence = {"scope": {"company": self.company}}
		source = frappe._dict(
			name="COUNT-SOURCE",
			docstatus=2,
			approval_request=ar.name,
			company=self.company,
			warehouse=self.warehouse,
		)
		draft = frappe._dict(name="COUNT-DRAFT", amended_from=source.name)
		observed_filters = []

		def no_finished_successor(doctype, filters, fieldname, *, for_update=False):
			observed_filters.append(filters)
			self.assertEqual((doctype, fieldname, for_update), (cycle_count_entry.COUNT, "name", True))
			return None

		with (
			patch.object(cycle_count_entry.frappe, "get_doc", return_value=source),
			patch.object(cycle_count_entry.frappe.db, "get_value", side_effect=no_finished_successor),
		):
			self.assertIs(cycle_count_entry._amendment_source(draft, ar, evidence), source)
		self.assertEqual(observed_filters[0]["docstatus"], ["!=", 0])

		with (
			patch.object(cycle_count_entry.frappe, "get_doc", return_value=source),
			patch.object(cycle_count_entry.frappe.db, "get_value", return_value="COUNT-CANCELLED"),
			self.assertRaisesRegex(frappe.ValidationError, "newer correction"),
		):
			cycle_count_entry._amendment_source(draft, ar, evidence)

	def test_amendment_draft_discard_is_refused(self):
		from jarvis.jarvis.doctype.jarvis_physical_count.jarvis_physical_count import JarvisPhysicalCount

		draft = frappe._dict(amended_from="")
		draft.get_doc_before_save = lambda: frappe._dict(amended_from="COUNT-DRAFT-SOURCE")
		with self.assertRaisesRegex(frappe.PermissionError, "cannot be discarded"):
			JarvisPhysicalCount.before_discard(draft)

	def test_saved_amendment_draft_cannot_clear_its_lineage(self):
		from jarvis.jarvis.doctype.jarvis_physical_count.jarvis_physical_count import JarvisPhysicalCount

		draft = frappe._dict(docstatus=0, amended_from="")
		draft.is_new = lambda: False
		draft.get_doc_before_save = lambda: frappe._dict(docstatus=0, amended_from="COUNT-DRAFT-SOURCE")
		with self.assertRaisesRegex(frappe.PermissionError, "must keep its original"):
			JarvisPhysicalCount._guard_draft_amendment_lineage(draft)

	def test_source_freshness_excludes_only_this_sealed_cohorts_observations(self):
		from jarvis.chat import cycle_count_entry

		baseline = snapshot()
		baseline["source_counts"]["Jarvis Physical Count"] = 0
		baseline["source_digest"] = review.digest(
			{key: value for key, value in baseline.items() if key != "source_digest"}
		)
		current = frappe.parse_json(frappe.as_json(baseline))
		current["datasets"]["physical_counts"] = [
			{
				"name": "COUNT-SELF",
				"company": "Example",
				"warehouse": "WH-A",
				"counted_at": "2026-10-05 10:00:00",
				"approval_request": "AR-SELF",
			}
		]
		current["datasets"]["physical_count_items"] = [
			{
				"name": "COUNT-ITEM-SELF",
				"parent": "COUNT-SELF",
				"item_code": "ITEM-A",
				"counted_qty": "1",
				"stock_uom": "Nos",
				"batch_detail_recorded": 0,
			}
		]
		current["source_counts"].update({"Jarvis Physical Count": 1, "Jarvis Physical Count Item": 1})
		self.assertEqual(
			cycle_count_entry._source_digest_excluding_cohort_facts(current, {"AR-SELF"}),
			baseline["source_digest"],
		)
		current["datasets"]["physical_counts"].append(
			{
				"name": "COUNT-OTHER",
				"company": "Example",
				"warehouse": "WH-A",
				"counted_at": "2026-10-05 11:00:00",
				"approval_request": "AR-SIBLING",
			}
		)
		current["datasets"]["physical_count_items"].append(
			{
				"name": "COUNT-ITEM-OTHER",
				"parent": "COUNT-OTHER",
				"item_code": "ITEM-A",
				"counted_qty": "2",
				"stock_uom": "Nos",
				"batch_detail_recorded": 0,
			}
		)
		current["source_counts"].update({"Jarvis Physical Count": 2, "Jarvis Physical Count Item": 2})
		self.assertEqual(
			cycle_count_entry._source_digest_excluding_cohort_facts(current, {"AR-SELF", "AR-SIBLING"}),
			baseline["source_digest"],
		)
		self.assertNotEqual(
			cycle_count_entry._source_digest_excluding_cohort_facts(current, {"AR-SELF"}),
			baseline["source_digest"],
		)

	def test_two_approved_siblings_can_record_against_one_sealed_source_baseline(self):
		from jarvis.chat import cycle_count_entry

		first, second = self._plan(), self._plan()
		frappe.db.set_value("Jarvis Approval Request", second.name, "conversation", first.conversation)
		baseline = snapshot()
		baseline["source_counts"]["Jarvis Physical Count"] = 0
		baseline["source_digest"] = review.digest(
			{key: value for key, value in baseline.items() if key != "source_digest"}
		)
		shared_run = first.run
		for approved in (first, second):
			evidence = frappe.parse_json(approved.assessment_evidence)
			evidence.update({"run": shared_run, "input_digest": baseline["source_digest"]})
			frappe.db.set_value(
				"Jarvis Approval Request",
				approved.name,
				{
					"conversation": first.conversation,
					"run": shared_run,
					"assessment_evidence": frappe.as_json(evidence),
					"assessment_input_digest": baseline["source_digest"],
				},
				update_modified=False,
			)
		first = frappe.get_doc("Jarvis Approval Request", first.name)
		second = frappe.get_doc("Jarvis Approval Request", second.name)
		with_first = frappe.parse_json(frappe.as_json(baseline))
		with_first["datasets"]["physical_counts"] = [
			{
				"name": "COUNT-SIBLING-FIRST",
				"company": "Example",
				"warehouse": "WH-A",
				"counted_at": "2026-10-05 10:00:00",
				"approval_request": first.name,
			}
		]
		with_first["datasets"]["physical_count_items"] = [
			{
				"name": "COUNT-ITEM-SIBLING-FIRST",
				"parent": "COUNT-SIBLING-FIRST",
				"item_code": "ITEM-A",
				"counted_qty": "1",
				"stock_uom": "Nos",
				"batch_detail_recorded": 0,
			}
		]
		with_first["source_counts"].update({"Jarvis Physical Count": 1, "Jarvis Physical Count Item": 1})
		self._current_evidence.stop()
		seen_users = []
		collections = []

		def source_for_cohort(scope, policy):
			seen_users.append(frappe.session.user)
			collections.append(1)
			# The source collector is intentionally on a fresh secondary reader, so
			# it cannot see this test transaction's uncommitted first observation.
			# Model the sealed snapshots directly instead of leaking the primary DB
			# into the assertion.
			return baseline if len(collections) == 1 else with_first

		frappe.set_user(self.counter)
		try:
			with patch.object(cycle_count_entry.cycle_count_review, "collect", side_effect=source_for_cohort):
				first_result = cycle_count_entry.record_counts(
					first.name,
					frappe.utils.now(),
					[{"item_code": self.item, "counted_qty": "0"}],
					"cyclecount" + frappe.generate_hash(length=24),
				)
				second_result = cycle_count_entry.record_counts(
					second.name,
					frappe.utils.now(),
					[{"item_code": self.item, "counted_qty": "1"}],
					"cyclecount" + frappe.generate_hash(length=24),
				)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(
			(first_result["saved"][0]["idempotent"], second_result["saved"][0]["idempotent"]), (0, 0)
		)
		self.assertTrue(seen_users)
		self.assertTrue(all(user == self.reviewer for user in seen_users))
		self.assertGreaterEqual(len(collections), 2)

	def test_count_plan_list_skips_malformed_approved_rows(self):
		from jarvis.chat import cycle_count_entry

		approved = self._plan()
		malformed = frappe.get_doc(
			{
				"doctype": "Jarvis Approval Request",
				"title": "Malformed test physical count plan",
				"source": "Agent Review",
				"status": "Approved",
				"decision": "Approved test fixture.",
				"document_type": "Warehouse",
				"conversation": approved.conversation,
				"ref_doctype": "Warehouse",
				"ref_name": self.warehouse,
				"agent": review.AGENT,
				"result_class": "derived_candidate",
				"assessment_key": frappe.generate_hash(length=32),
				"assessment_evidence": frappe.as_json({"reviewer": self.reviewer}),
				"assessment_input_digest": "malformed-test",
				"question": "Malformed test count plan.",
				"options": "[]",
			}
		)
		malformed.flags.jarvis_server_write = True
		malformed.insert(ignore_permissions=True)
		frappe.set_user(self.counter)
		try:
			listed = cycle_count_entry.list_count_plans(page_length=20)
		finally:
			frappe.set_user("Administrator")
		self.assertIn(approved.name, [row["request"] for row in listed["rows"]])
		self.assertGreaterEqual(listed["next_start"], 2)

	def test_physical_count_visibility_follows_the_approved_plan(self):
		"""Owner, reviewer, or exact Approval DocShare reader can use one plan."""
		from jarvis.chat import cycle_count_entry

		viewer = self._user("cycle-plan-viewer@example.invalid", ("Jarvis User", "Stock User"))
		outsider = self._user("cycle-plan-outsider@example.invalid", ("Jarvis User", "Stock User"))
		manager = self._user(
			"cycle-plan-manager@example.invalid", ("Jarvis User", "Stock User", "System Manager")
		)
		approved = self._plan()
		frappe.share.add(
			"Jarvis Approval Request",
			approved.name,
			user=viewer,
			read=1,
			flags={"ignore_share_permission": True},
		)
		frappe.clear_cache(user=viewer)
		entry_key = "cyclecount" + frappe.generate_hash(length=24)
		frappe.set_user(viewer)
		try:
			self.assertIn(
				approved.name, [row["request"] for row in cycle_count_entry.list_count_plans()["rows"]]
			)
			self.assertEqual(cycle_count_entry.get_count_plan(approved.name)["request"], approved.name)
			self.assertTrue(cycle_count_entry.can_record_count_plan(frappe._dict(name=approved.name)))
			fact_name = cycle_count_entry.record_counts(
				approved.name,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				entry_key,
			)["saved"][0]["name"]
		finally:
			frappe.set_user("Administrator")

		# The list and detail projections give the browser enough safe state to
		# distinguish a completed plan from an unrecorded plan and to describe a
		# correction target without loading source evidence.
		frappe.set_user(viewer)
		listed = cycle_count_entry.list_count_plans()["rows"]
		listed_row = next(row for row in listed if row["request"] == approved.name)
		self.assertEqual(
			(
				listed_row["line_count"],
				listed_row["recorded_line_count"],
				listed_row["remaining_line_count"],
			),
			(1, 1, 0),
		)
		detail = cycle_count_entry.get_count_plan(approved.name)
		self.assertEqual(len(detail["active_counts"]), 1)
		active_count = detail["active_counts"][0]
		self.assertEqual(active_count["name"], fact_name)
		self.assertEqual(active_count["recorded_by"], viewer)
		self.assertTrue(active_count["counted_at"])
		self.assertTrue(active_count["recorded_at"])
		self.assertEqual(
			active_count["items"],
			[
				{
					"item_code": self.item,
					"counted_qty": "0",
					"stock_uom": frappe.db.get_value("Item", self.item, "stock_uom"),
				}
			],
		)
		frappe.set_user("Administrator")

		for allowed in (self.counter, self.reviewer, viewer):
			frappe.set_user(allowed)
			self.assertIn(
				approved.name,
				[row["request"] for row in cycle_count_entry.list_count_plans()["rows"]],
				allowed,
			)
			self.assertEqual(cycle_count_entry.get_count_plan(approved.name)["request"], approved.name)
			self.assertTrue(cycle_count_entry.can_record_count_plan(frappe._dict(name=approved.name)))
			self.assertEqual(
				frappe.get_list("Jarvis Physical Count", filters={"name": fact_name}, pluck="name"),
				[fact_name],
				allowed,
			)
			self.assertTrue(
				frappe.has_permission("Jarvis Physical Count", "read", doc=fact_name, user=allowed), allowed
			)
			self._assert_physical_count_items_readable(
				allowed,
				fact_name,
				True,
				f"{allowed} can read physical-count items through the approved plan",
			)

		for denied in (outsider, manager):
			frappe.set_user(denied)
			self.assertEqual(
				frappe.get_list("Jarvis Physical Count", filters={"name": fact_name}, pluck="name"),
				[],
				denied,
			)
			self.assertFalse(
				frappe.has_permission("Jarvis Physical Count", "read", doc=fact_name, user=denied), denied
			)
			self._assert_physical_count_items_readable(
				denied,
				fact_name,
				False,
				f"{denied} cannot read physical-count items outside the approved plan",
			)

		frappe.set_user("Administrator")
		frappe.db.delete(
			"DocShare",
			{
				"share_doctype": "Jarvis Approval Request",
				"share_name": approved.name,
				"user": viewer,
			},
		)
		frappe.clear_cache(user=viewer)
		frappe.set_user(viewer)
		self.assertEqual(cycle_count_entry.list_count_plans()["rows"], [])
		self.assertFalse(cycle_count_entry.can_record_count_plan(frappe._dict(name=approved.name)))
		with self.assertRaises(frappe.PermissionError):
			cycle_count_entry.get_count_plan(approved.name)
		with self.assertRaises(frappe.PermissionError):
			cycle_count_entry.record_counts(
				approved.name,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				"cyclecount" + frappe.generate_hash(length=24),
			)
		self._assert_physical_count_readable(
			viewer,
			fact_name,
			False,
			"a revoked Approval DocShare reader cannot read the physical-count parent",
		)
		self._assert_physical_count_items_readable(
			viewer,
			fact_name,
			False,
			"a revoked Approval DocShare reader cannot read physical-count items",
		)
		frappe.set_user("Administrator")

	def test_stock_variance_reads_only_inside_its_active_server_bound_scope(self):
		"""A running delegate can read its source, but never gains Desk access."""
		from jarvis.chat import cycle_count_entry
		from jarvis.tools import _agent_run_ctx

		approved = self._plan()
		frappe.set_user(self.counter)
		try:
			fact_name = cycle_count_entry.record_counts(
				approved.name,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				"cyclecount" + frappe.generate_hash(length=24),
			)["saved"][0]["name"]
		finally:
			frappe.set_user("Administrator")

		# A DocType-level check must keep working for installation validation. It
		# does not itself grant a record: the following real Desk-like list and
		# single-doc checks deny this unrelated Stock User without a run context.
		self.assertTrue(frappe.has_permission("Jarvis Physical Count", "read", user=self.stock_variance_user))
		_agent_run_ctx.clear_session_key()
		self._assert_physical_count_readable(
			self.stock_variance_user,
			fact_name,
			False,
			"an ordinary Stock Variance run-as user can read a private count outside a dispatch",
		)

		today = frappe.utils.getdate(frappe.utils.today()).isoformat()
		scope = {
			"company": self.company,
			"from_date": today,
			"to_date": today,
			"record_scope": {"doctype": "Warehouse", "name": self.warehouse},
		}
		try:
			with self._delegate_run("stock-variance-auditor", scope) as delegate:
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					True,
					"a matching active Stock Variance run cannot read its submitted source count",
				)
				for ptype in ("write", "submit"):
					self.assertFalse(
						frappe.has_permission(
							"Jarvis Physical Count",
							ptype,
							doc=fact_name,
							user=self.stock_variance_user,
						),
						f"active source scope incorrectly grants {ptype}",
					)

				def set_scope(value):
					frappe.db.set_value(
						"Jarvis Agent Run",
						delegate.run,
						"scope_json",
						frappe.as_json(value),
						update_modified=False,
					)

				set_scope({**scope, "company": f"{self.company}-other"})
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a run scope for a different company leaked this count",
				)
				set_scope({**scope, "from_date": "2000-01-01", "to_date": "2000-01-01"})
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a run scope for a different date leaked this count",
				)
				set_scope(
					{
						**scope,
						"record_scope": {"doctype": "Warehouse", "name": self.other_warehouse},
					}
				)
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a run scope for a different Warehouse subtree leaked this count",
				)
				set_scope({"company": self.company, "from_date": today, "to_date": today})
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a malformed server scope leaked this count",
				)

				set_scope(scope)
				session = frappe.db.get_value(
					"Jarvis Chat Session",
					{"session_key": delegate.session_key},
					"name",
				)
				frappe.db.set_value(
					"Jarvis Chat Session",
					session,
					"user",
					self.counter,
					update_modified=False,
				)
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a session mapped to a different user leaked this count",
				)
				frappe.db.set_value(
					"Jarvis Chat Session",
					session,
					"user",
					self.stock_variance_user,
					update_modified=False,
				)
				frappe.db.set_value(
					"Jarvis Agent Run",
					delegate.run,
					"status",
					"completed",
					update_modified=False,
				)
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a terminal run leaked this count",
				)
				frappe.db.set_value(
					"Jarvis Agent Run",
					delegate.run,
					"status",
					"running",
					update_modified=False,
				)
				frappe.db.set_value(
					"Jarvis Agent Installation",
					delegate.installation,
					"enabled",
					0,
					update_modified=False,
				)
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a disabled installation leaked this count",
				)
				frappe.db.set_value(
					"Jarvis Agent Installation",
					delegate.installation,
					"enabled",
					1,
					update_modified=False,
				)
				frappe.db.set_value(
					"Jarvis Agent Installation",
					delegate.installation,
					"run_as_user",
					self.counter,
					update_modified=False,
				)
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a remapped installation leaked this count",
				)
				frappe.db.set_value(
					"Jarvis Agent Installation",
					delegate.installation,
					"run_as_user",
					self.stock_variance_user,
					update_modified=False,
				)
				frappe.db.set_value(
					"Jarvis Physical Count",
					fact_name,
					"docstatus",
					0,
					update_modified=False,
				)
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a draft count leaked to the source run",
				)
				frappe.db.set_value(
					"Jarvis Physical Count",
					fact_name,
					"docstatus",
					1,
					update_modified=False,
				)
		finally:
			frappe.set_user("Administrator")

	def test_cycle_count_planner_history_is_session_bound_and_read_only(self):
		"""The planner reads only its stamped history set while its run is active."""
		from jarvis.chat import cycle_count_entry
		from jarvis.tools import _agent_run_ctx

		approved = self._plan()
		frappe.set_user(self.counter)
		try:
			fact_name = cycle_count_entry.record_counts(
				approved.name,
				frappe.utils.now(),
				[{"item_code": self.item, "counted_qty": "0"}],
				"cyclecount" + frappe.generate_hash(length=24),
			)["saved"][0]["name"]
		finally:
			frappe.set_user("Administrator")

		_agent_run_ctx.clear_session_key()
		self._assert_physical_count_readable(
			self.stock_variance_user,
			fact_name,
			False,
			"a planner run-as user can read private history outside a dispatch",
		)
		self._assert_physical_count_items_readable(
			self.stock_variance_user,
			fact_name,
			False,
			"a planner run-as user can read private count items outside a dispatch",
		)

		today = frappe.utils.getdate(frappe.utils.today()).isoformat()
		scope = {
			"company": self.company,
			"from_date": today,
			"to_date": today,
			"count_history_from": today,
			"warehouses": [self.warehouse],
		}
		try:
			with self._delegate_run("cycle-count-planner-operator", scope) as delegate:
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					True,
					"a matching active planner run cannot read its submitted history",
				)
				self._assert_physical_count_items_readable(
					self.stock_variance_user,
					fact_name,
					True,
					"a matching active planner run cannot read its count-item history",
				)
				for ptype in ("write", "submit"):
					self.assertFalse(
						frappe.has_permission(
							"Jarvis Physical Count",
							ptype,
							doc=fact_name,
							user=self.stock_variance_user,
						),
						f"active planner source scope incorrectly grants {ptype}",
					)

				def set_scope(value):
					frappe.db.set_value(
						"Jarvis Agent Run",
						delegate.run,
						"scope_json",
						frappe.as_json(value),
						update_modified=False,
					)

				set_scope({**scope, "company": f"{self.company}-other"})
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a planner scope for another company leaked count history",
				)
				set_scope({**scope, "count_history_from": "2099-01-01"})
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a planner scope after the count date leaked history",
				)
				set_scope({**scope, "warehouses": [self.other_warehouse]})
				self._assert_physical_count_readable(
					self.stock_variance_user,
					fact_name,
					False,
					"a planner scope for another leaf warehouse leaked history",
				)
		finally:
			frappe.set_user("Administrator")
