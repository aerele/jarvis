import copy
import json
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.source_receipts import is_source_receipt, source_receipt


class TestSourceReceipts(unittest.TestCase):
	def test_source_values_and_sql_are_not_retained_but_live_values_are_unchanged(self):
		for tool in ("read_file", "query_file", "query_report", "jarvis__query_report"):
			with self.subTest(tool=tool):
				args = {"file_id": "FILE-1", "version": "a" * 64, "sql": "SELECT 'SECRET-INVOICE'"}
				result = {
					"ok": True,
					"data": {
						"version": "a" * 64,
						"file_id": "FILE-1",
						"source_rows": 6000,
						"coverage": {"complete": True},
						"rows": [["SECRET-INVOICE"]],
						"sheets": [{"sample": [["SECRET-INVOICE"]]}],
						"text": "SECRET-INVOICE",
					},
				}
				before = copy.deepcopy((args, result))
				stored_args, stored_result = source_receipt(tool, args, result)
				self.assertNotIn("SECRET-INVOICE", json.dumps([stored_args, stored_result]))
				self.assertEqual((args, result), before)
				self.assertEqual(stored_args["file_id"], "FILE-1")
				self.assertEqual(stored_result["data"]["source_rows"], 6000)
				self.assertTrue(is_source_receipt(stored_result))

	def test_errors_and_unknown_result_fields_cannot_retain_source_values(self):
		args, result = source_receipt(
			"query_file",
			{"sql": "SECRET"},
			{
				"ok": False,
				"error": {"code": "SECRET", "message": "SECRET"},
			},
		)
		self.assertNotIn("SECRET", json.dumps([args, result]))
		self.assertFalse(result["ok"])

	def test_other_tool_receipts_are_unchanged(self):
		args, result = {"name": "INV-1"}, {"ok": True, "data": {"name": "INV-1"}}
		self.assertEqual(source_receipt("update_doc", args, result), (args, result))


class TestStoredSourceReceipt(FrappeTestCase):
	def test_actual_receipt_and_realtime_event_do_not_contain_source_values(self):
		from jarvis import api
		from jarvis.tests._transport_helpers import provision_legacy_site

		provision_legacy_site(self)
		conversation = frappe.get_doc(
			{"doctype": "Jarvis Conversation", "title": "Source retention test"}
		).insert(ignore_permissions=True)
		try:
			args = {"file_id": "FILE-1", "sql": "SELECT 'PRIVATE-INVOICE'"}
			live = {
				"ok": True,
				"data": {
					"file_id": "FILE-1",
					"source_rows": 6000,
					"coverage": {"complete": True},
					"rows": [["PRIVATE-INVOICE"]],
				},
			}
			with patch.object(api, "publish_realtime_tool_result") as publish:
				api.persist_tool_receipt(
					conversation.name, "query_file", args, live, tool_call_id="source-read-1"
				)
			row = frappe.db.get_value(
				"Jarvis Chat Message",
				{"conversation": conversation.name, "tool_call_id": "source-read-1"},
				["tool_args", "tool_result", "tool_status"],
				as_dict=True,
			)
			self.assertNotIn("PRIVATE-INVOICE", row.tool_args + row.tool_result)
			self.assertNotIn("PRIVATE-INVOICE", json.dumps(publish.call_args.kwargs))
			self.assertTrue(json.loads(row.tool_args)["source_query_executed"])
			self.assertEqual(row.tool_status, "completed")
			self.assertEqual(live["data"]["rows"], [["PRIVATE-INVOICE"]])
		finally:
			frappe.delete_doc("Jarvis Conversation", conversation.name, force=True, ignore_permissions=True)
			frappe.db.commit()
