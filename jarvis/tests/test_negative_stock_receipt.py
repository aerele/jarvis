"""Tests for the server-owned Negative Stock receipt gate.

These use synthetic fixtures and isolated database transactions. They do not
install or run an agent, and they do not read a customer company.
"""

from __future__ import annotations

import json
import time
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import negative_stock_receipt as receipt

COMPANY = "Receipt Test Company"
ROOT = "Receipt Test Root"
USER = "receipt-runner@example.invalid"
START = "2026-10-30 09:00:00"
FINISH = "2026-10-30 09:05:00"
PROFILE = {"hash": "a" * 64, "roles": ["Stock User"], "user_permissions": {}}
CONFIG = {"negative_stock": {"reviewer": "reviewer@example.invalid"}}
SCOPE_RAW = {
	"company": COMPANY,
	"from_date": "2026-10-01",
	"to_date": "2026-12-31",
	"report_date": "2026-10-30",
	"record_scope": {"doctype": "Warehouse", "name": ROOT},
}


def _coverage():
	return {
		"coverage": {token: {"state": "evaluated"} for token in receipt.REQUIRED_TOKENS},
		"required_tokens": sorted(receipt.REQUIRED_TOKENS),
		"result_state": "evaluated_clean",
		"truncated": False,
		"watermark_drift": False,
		"scoped_visibility": False,
		"row_shortfall": False,
		"dropped": [],
		"not_evaluable": [],
		"not_evaluable_scoped": [],
	}


def _run(**overrides):
	data = {
		"name": "NEG-RECEIPT-1",
		"agent": receipt.AGENT,
		"status": "completed",
		"result_state": "evaluated_clean",
		"bundle_version": receipt.SUPPORTED_BUNDLE_VERSION,
		"started_at": START,
		"finished_at": FINISH,
		"scope_json": json.dumps(SCOPE_RAW),
		"permission_profile": json.dumps(PROFILE),
		"engagement_config_json": json.dumps(CONFIG),
		"coverage_json": json.dumps(_coverage()),
		"integrity_digest": "b" * 64,
		"session_key": "negative-session",
		"findings_count": 0,
		"blocker_count": 0,
		"coverage_note": "",
		"owner": USER,
	}
	data.update(overrides)
	return frappe._dict(data)


def _capture(run):
	return {
		"run": run.name,
		"scope": receipt._scope(run.scope_json),
		"run_as_user": USER,
		"run_as_fingerprint": receipt._user_fingerprint(USER),
		"permission_profile_fingerprint": receipt._profile_fingerprint(run.permission_profile),
		"engagement_config_fingerprint": receipt._config_fingerprint(run),
	}


def _source(scope):
	state = {
		"schema": receipt.SOURCE_SCHEMA,
		"scope": scope,
		"tables": {"Stock Ledger Entry": {"count": 1, "digest": "c" * 64}},
	}
	state["digest"] = receipt._digest(state)
	return state


def _receipt_value(run, capture, source):
	body = receipt._receipt_body(run, capture, source)
	return {receipt.RECEIPT_KEY: {**body, "digest": receipt._digest(body)}}


class TestNegativeStockReceipt(unittest.TestCase):
	def test_receipt_accepts_the_current_published_negative_stock_bundle(self):
		with open(frappe.get_app_path("jarvis", "agents", "registry.json"), encoding="utf-8") as source:
			entry = next(row for row in json.load(source)["agents"] if row["agent_slug"] == receipt.AGENT)
		self.assertEqual(entry["status"], "Published")
		with patch.object(receipt.frappe.db, "exists", return_value=False):
			receipt._complete(_run(bundle_version=entry["version"]))

	def test_old_or_unknown_negative_stock_bundles_cannot_supply_a_receipt(self):
		for version in ("0.3.1", "0.3.3", "", None):
			with self.subTest(version=version), self.assertRaises(receipt.ReceiptRefused):
				receipt._complete(_run(bundle_version=version))

	def test_scope_binds_effective_as_of_date(self):
		scope = receipt._scope(SCOPE_RAW)
		self.assertEqual(scope["effective_as_of"], "2026-10-30")
		future = dict(SCOPE_RAW, report_date="2027-01-04")
		self.assertEqual(receipt._scope(future)["effective_as_of"], "2026-12-31")

	def test_complete_rejects_partial_coverage_and_grade_finding(self):
		partial = _coverage()
		partial["coverage"][receipt.GRADE_TOKEN] = {"state": "not_evaluable"}
		run = _run(coverage_json=json.dumps(partial), result_state="partial")
		with self.assertRaises(receipt.ReceiptRefused):
			receipt._complete(run)

		with patch.object(receipt.frappe.db, "exists", return_value=True) as exists:
			with self.assertRaises(receipt.ReceiptRefused):
				receipt._complete(_run())
		exists.assert_called_once_with(
			receipt.FINDING,
			{"agent": receipt.AGENT, "rule_id": receipt.GRADE_TOKEN, "last_seen_run": "NEG-RECEIPT-1"},
		)

	def test_source_identity_uses_hashes_only_and_rejects_fresh_source(self):
		scope = receipt._scope(SCOPE_RAW)
		old = "2026-10-29 08:00:00"

		def sql(query, values, as_dict):
			if "tabCompany" in query:
				return [frappe._dict(name=COMPANY, modified=old)]
			if "tabSingles" in query:
				return [
					frappe._dict(field="modified", value=old),
					frappe._dict(field="allow_negative_stock", value="0"),
					frappe._dict(field="valuation_method", value="FIFO"),
				]
			if "tabWarehouse" in query:
				return [frappe._dict(name=ROOT, modified=old)]
			if "tabItem" in query:
				return [frappe._dict(name="ITEM-SECRET", modified=old)]
			return []

		root = frappe._dict(name=ROOT, company=COMPANY, lft=1, rgt=2)
		with (
			patch.object(receipt.frappe.db, "get_value", return_value=root),
			patch.object(receipt.frappe.db, "sql", side_effect=sql),
		):
			state = receipt._source_state(scope, START)
		self.assertNotIn("ITEM-SECRET", json.dumps(state))
		self.assertEqual(state["tables"]["Item"]["count"], 1)

		def stale_sql(query, values, as_dict):
			if "tabCompany" in query:
				return [frappe._dict(name=COMPANY, modified=old)]
			if "tabSingles" in query:
				return [
					frappe._dict(field="modified", value=old),
					frappe._dict(field="allow_negative_stock", value="0"),
					frappe._dict(field="valuation_method", value="FIFO"),
				]
			if "tabWarehouse" in query:
				return [frappe._dict(name=ROOT, modified=old)]
			if "tabStock Ledger Entry" in query:
				return [frappe._dict(name="SLE-FRESH", modified=START)]
			return []

		with (
			patch.object(receipt.frappe.db, "get_value", return_value=root),
			patch.object(receipt.frappe.db, "sql", side_effect=stale_sql),
		):
			with self.assertRaises(receipt.ReceiptRefused):
				receipt._source_state(scope, START)

	def test_persist_stores_one_immutable_server_receipt(self):
		run = _run()
		capture = _capture(run)
		source = _source(capture["scope"])
		locked = frappe._dict(name=run.name, assessment_output_json=None)
		with (
			patch.object(receipt.frappe.db, "get_value", return_value=locked),
			patch.object(receipt.frappe, "get_doc", return_value=run),
			patch.object(receipt.frappe.db, "exists", return_value=False),
			patch.object(receipt.frappe.db, "set_value") as set_value,
			patch.object(receipt, "_source_state", return_value=source),
			patch.object(
				receipt, "_live_profile_fingerprint", return_value=capture["permission_profile_fingerprint"]
			),
		):
			out = receipt.persist(capture)

		self.assertIsNotNone(out)
		self.assertEqual(out["source"], source)
		stored = set_value.call_args.args[3]
		self.assertEqual(receipt._receipt(stored), out)
		self.assertEqual(set_value.call_args.kwargs["update_modified"], False)

	def test_reorder_verifier_requires_exact_stored_receipt_scope_and_profile(self):
		negative = _run()
		capture = _capture(negative)
		source = _source(capture["scope"])
		value = _receipt_value(negative, capture, source)
		reorder = _run(
			name="REORDER-1",
			agent=receipt.REORDER_AGENT,
			status="running",
			session_key="reorder-session",
		)

		def get_value(doctype, name, field, **kwargs):
			if doctype == receipt.SESSION:
				self.assertEqual(name, {"session_key": "reorder-session"})
				return USER
			if doctype == receipt.RUN:
				self.assertEqual(name, negative.name)
				return frappe.as_json(value)
			raise AssertionError(f"unexpected get_value: {doctype} {name} {field}")

		with (
			patch.object(receipt.frappe, "get_doc", return_value=negative),
			patch.object(receipt.frappe.db, "get_value", side_effect=get_value),
			patch.object(receipt.frappe.db, "exists", return_value=False),
			patch.object(receipt, "_source_state", return_value=source),
			patch.object(
				receipt, "_live_profile_fingerprint", return_value=capture["permission_profile_fingerprint"]
			),
		):
			self.assertEqual(
				receipt.verify_for_reorder(negative.name, reorder)["negative_run"], negative.name
			)

		different_scope = _run(
			name="REORDER-OTHER",
			agent=receipt.REORDER_AGENT,
			status="running",
			scope_json=json.dumps({**SCOPE_RAW, "report_date": "2026-10-29"}),
		)
		with (
			patch.object(receipt.frappe, "get_doc", return_value=negative),
			patch.object(receipt.frappe.db, "get_value", side_effect=get_value),
		):
			with self.assertRaises(receipt.ReceiptRefused):
				receipt.verify_for_reorder(negative.name, different_scope)

		changed_profile = _run(
			name="REORDER-PROFILE",
			agent=receipt.REORDER_AGENT,
			status="running",
			session_key="reorder-session",
			permission_profile=json.dumps({**PROFILE, "hash": "d" * 64}),
		)
		with (
			patch.object(receipt.frappe, "get_doc", return_value=negative),
			patch.object(receipt.frappe.db, "get_value", side_effect=get_value),
			patch.object(receipt.frappe.db, "exists", return_value=False),
		):
			with self.assertRaises(receipt.ReceiptRefused):
				receipt.verify_for_reorder(negative.name, changed_profile)

	def test_reorder_verifier_rejects_unstored_or_stale_receipt(self):
		negative = _run()
		capture = _capture(negative)
		source = _source(capture["scope"])
		value = _receipt_value(negative, capture, source)
		reorder = _run(
			name="REORDER-2", agent=receipt.REORDER_AGENT, status="running", session_key="reorder-session"
		)

		with (
			patch.object(receipt.frappe, "get_doc", return_value=negative),
			patch.object(receipt.frappe.db, "get_value", return_value=None),
		):
			with self.assertRaises(receipt.ReceiptRefused):
				receipt.verify_for_reorder(negative.name, reorder)

		tampered = _receipt_value(negative, capture, source)
		tampered[receipt.RECEIPT_KEY]["digest"] = "0" * 64
		with (
			patch.object(receipt.frappe, "get_doc", return_value=negative),
			patch.object(receipt.frappe.db, "get_value", return_value=frappe.as_json(tampered)),
		):
			with self.assertRaises(receipt.ReceiptRefused):
				receipt.verify_for_reorder(negative.name, reorder)

		def get_value(doctype, name, field, **kwargs):
			if doctype == receipt.SESSION:
				return USER
			if doctype == receipt.RUN:
				return frappe.as_json(value)
			raise AssertionError("unexpected read")

		stale = dict(source)
		stale["tables"] = {"Stock Ledger Entry": {"count": 2, "digest": "d" * 64}}
		stale["digest"] = receipt._digest({key: item for key, item in stale.items() if key != "digest"})
		with (
			patch.object(receipt.frappe, "get_doc", return_value=negative),
			patch.object(receipt.frappe.db, "get_value", side_effect=get_value),
			patch.object(receipt.frappe.db, "exists", return_value=False),
			patch.object(receipt, "_source_state", return_value=stale),
			patch.object(
				receipt, "_live_profile_fingerprint", return_value=capture["permission_profile_fingerprint"]
			),
		):
			with self.assertRaises(receipt.ReceiptRefused):
				receipt.verify_for_reorder(negative.name, reorder)


class TestNegativeStockReceiptDatabase(FrappeTestCase):
	"""Use real ERPNext tables and Singles storage on the isolated test site."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from jarvis.tests import _erpnext_masters as masters

		frappe.set_user("Administrator")
		masters.clear_cache_after_rollback(cls)
		cls.company = masters.ensure_ledger_company()
		cls.root = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 1}, "name")
		settings = frappe.get_single("Stock Settings")
		settings.allow_negative_stock = 0
		settings.valuation_method = "FIFO"
		settings.save()
		cls.scope = receipt._scope(
			{**SCOPE_RAW, "company": cls.company, "record_scope": {"doctype": "Warehouse", "name": cls.root}}
		)

	def setUp(self):
		time.sleep(1.05)
		self.started = frappe.utils.now_datetime()

	def test_real_single_settings_source_can_be_captured(self):
		self.assertFalse(frappe.db.table_exists("Stock Settings"))
		state = receipt._source_state(self.scope, self.started)
		self.assertEqual(state["tables"]["Stock Settings"]["count"], 1)
		self.assertNotIn("allow_negative_stock", json.dumps(state))

	def test_settings_value_change_without_modified_timestamp_invalidates_source(self):
		before = receipt._source_state(self.scope, self.started)
		frappe.db.sql(
			"update `tabSingles` set value='1' where doctype='Stock Settings' and field='allow_negative_stock'"
		)
		after = receipt._source_state(self.scope, self.started)
		self.assertNotEqual(before["digest"], after["digest"])

	def test_item_value_change_without_modified_timestamp_invalidates_source(self):
		from jarvis.tests import _erpnext_masters as masters

		item = masters.ensure_item("RECEIPT-DB-ONLY", is_stock_item=1, valuation_rate=25)
		time.sleep(1.05)
		started = frappe.utils.now_datetime()
		before = receipt._source_state(self.scope, started)
		frappe.db.set_value("Item", item, "allow_negative_stock", 1, update_modified=False)
		after = receipt._source_state(self.scope, started)
		self.assertNotEqual(before["digest"], after["digest"])
		self.assertNotIn(item, json.dumps(after))


class TestNegativeStockFreshSnapshot(FrappeTestCase):
	def test_fresh_snapshot_sees_a_commit_hidden_by_the_request_snapshot(self):
		primary = frappe.local.db
		conf = frappe.conf
		writer = receipt.get_db(
			socket=conf.db_socket,
			host=conf.db_host,
			port=conf.db_port,
			user=conf.db_user or conf.db_name,
			password=conf.db_password,
			cur_db_name=conf.db_name,
		)
		doctype = "Receipt snapshot test " + frappe.generate_hash(length=12)
		query = "select value from `tabSingles` where doctype=%s and field='test_marker'"
		try:
			self.assertFalse(primary.sql(query, (doctype,)))
			writer.sql(
				"insert into `tabSingles` (doctype, field, value) values (%s, 'test_marker', 'new')",
				(doctype,),
			)
			writer.commit()
			self.assertFalse(primary.sql(query, (doctype,)))
			with receipt.fresh_source_snapshot():
				self.assertEqual(frappe.db.sql(query, (doctype,))[0][0], "new")
			self.assertIs(frappe.local.db, primary)
		finally:
			writer.sql("delete from `tabSingles` where doctype=%s", (doctype,))
			writer.commit()
			writer.close()

	def test_snapshot_error_restores_the_request_connection(self):
		primary = frappe.local.db
		with self.assertRaisesRegex(RuntimeError, "receipt test"):
			with receipt.fresh_source_snapshot():
				raise RuntimeError("receipt test")
		self.assertIs(frappe.local.db, primary)


class TestNegativeStockReceiptWritebackHook(unittest.TestCase):
	def test_capture_precedes_finalization_and_persist_follows_it(self):
		from jarvis.tools import record_agent_run as writeback

		capture = {"run": "NEG-HOOK-1"}
		start_run = frappe._dict(
			name="NEG-HOOK-1",
			scope_json=frappe.as_json(SCOPE_RAW),
			agent=receipt.AGENT,
			status="running",
			findings_count=0,
			blocker_count=0,
			coverage_note="",
			owner=USER,
		)
		finished_run = frappe._dict(
			name=start_run.name,
			agent=receipt.AGENT,
			status="completed",
			findings_count=0,
			blocker_count=0,
			coverage_note="",
			owner=USER,
		)
		inst = frappe._dict(name="NEG-INSTALL", activation_state="shadow")
		run_row = frappe._dict(
			name=start_run.name,
			status="running",
			installation=inst.name,
			agent=receipt.AGENT,
		)

		def get_value(doctype, filters, fields, **kwargs):
			if doctype == writeback.RUN:
				return run_row
			if doctype == writeback.LISTING:
				return frappe._dict(rule_tokens="[]", doctypes_required="[]")
			raise AssertionError(f"unexpected get_value: {doctype} {filters} {fields}")

		with (
			patch("jarvis.tools._agent_run_ctx.get_session_key", return_value="negative-session"),
			patch.object(writeback.frappe.db, "get_value", side_effect=get_value),
			patch.object(writeback.frappe, "get_doc", side_effect=[inst, start_run]),
			patch.object(writeback, "_validate_findings", return_value=([], [])),
			patch("jarvis.chat.operator_review.backend", return_value=None),
			patch("jarvis.chat.agent_runs.record_delegate_run", return_value=finished_run) as finalize,
			patch("jarvis.chat.agent_run_steps.record_step"),
			patch(
				"jarvis.chat.negative_stock_receipt.capture_launch", return_value=capture
			) as capture_launch,
			patch("jarvis.chat.negative_stock_receipt.persist") as persist,
		):
			out = writeback.record_agent_run(findings=[], coverage={}, scope=SCOPE_RAW)

		self.assertEqual(out["status"], "completed")
		capture_launch.assert_called_once_with(start_run)
		finalize.assert_called_once()
		persist.assert_called_once_with(capture)
