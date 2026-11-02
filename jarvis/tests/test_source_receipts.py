import copy
import json
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.source_receipts import is_source_receipt, source_receipt


class TestSourceReceipts(unittest.TestCase):
	def test_source_values_and_sql_are_not_retained_but_live_values_are_unchanged(self):
		for tool in (
			"read_file",
			"query_file",
			"query_report",
			"jarvis__query_report",
			"get_file_pages",
			"jarvis__get_file_pages",
		):
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

	def test_file_page_receipt_keeps_only_safe_page_metadata(self):
		args = {
			"file_url": "/private/files/source.pdf",
			"filename": "source.pdf",
			"first_page": 1,
			"max_pages": 6,
		}
		live = {
			"ok": True,
			"data": {
				"kind": "file_pages",
				"file_name": "source.pdf",
				"page_count": 9,
				"has_more": True,
				"pages": [{"page": 1, "mime": "image/png", "data_b64": "PRIVATE-PAGE-BYTES"}],
				"note": "PRIVATE-PAGE-BYTES",
			},
		}
		before = copy.deepcopy((args, live))
		stored_args, stored = source_receipt("get_file_pages", args, live)
		self.assertEqual((args, live), before)
		self.assertEqual(stored_args, {"file_url": "/private/files/source.pdf", "filename": "source.pdf"})
		self.assertEqual(stored["data"]["file_name"], "source.pdf")
		self.assertEqual(stored["data"]["page_count"], 9)
		self.assertTrue(stored["data"]["has_more"])
		self.assertNotIn("pages", stored["data"])
		self.assertNotIn("PRIVATE-PAGE-BYTES", json.dumps([stored_args, stored]))

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

	def test_actual_page_receipt_and_realtime_event_do_not_contain_page_bytes(self):
		from jarvis import api
		from jarvis.tests._transport_helpers import provision_legacy_site

		provision_legacy_site(self)
		conversation = frappe.get_doc(
			{"doctype": "Jarvis Conversation", "title": "Page receipt retention test"}
		).insert(ignore_permissions=True)
		try:
			page_bytes = "PRIVATE-PAGE-BYTES"
			live = {
				"ok": True,
				"data": {
					"kind": "file_pages",
					"file_name": "statement.pdf",
					"page_count": 3,
					"has_more": True,
					"pages": [{"page": 1, "mime": "image/png", "data_b64": page_bytes}],
					"note": page_bytes,
				},
			}
			with patch.object(api, "publish_realtime_tool_result") as publish:
				api.persist_tool_receipt(
					conversation.name,
					"get_file_pages",
					{"file_url": "/private/files/statement.pdf", "first_page": 1},
					live,
					tool_call_id="source-page-read-1",
				)
			row = frappe.db.get_value(
				"Jarvis Chat Message",
				{"conversation": conversation.name, "tool_call_id": "source-page-read-1"},
				["tool_args", "tool_result", "tool_status"],
				as_dict=True,
			)
			self.assertNotIn(page_bytes, row.tool_args + row.tool_result)
			self.assertNotIn(page_bytes, json.dumps(publish.call_args.kwargs))
			stored = json.loads(row.tool_result)
			self.assertTrue(is_source_receipt(stored))
			self.assertEqual(stored["data"]["file_name"], "statement.pdf")
			self.assertEqual(stored["data"]["page_count"], 3)
			self.assertTrue(stored["data"]["has_more"])
			self.assertNotIn("pages", stored["data"])
			self.assertEqual(row.tool_status, "completed")
			self.assertEqual(live["data"]["pages"][0]["data_b64"], page_bytes)
		finally:
			frappe.delete_doc("Jarvis Conversation", conversation.name, force=True, ignore_permissions=True)
			frappe.db.commit()


class TestSharedSourceReceiptRetry(unittest.TestCase):
	def test_legacy_cached_source_results_are_never_replayed_to_a_shared_agent(self):
		from jarvis import api
		from jarvis.chat import collaboration_dispatch

		binding = SimpleNamespace(conversation="SHARED-CONVERSATION", name="SHARED-BINDING")
		permit = SimpleNamespace(turn=SimpleNamespace(binding=binding, run_id="SHARED-TURN"))

		@contextmanager
		def callback_scope(*_args, **_kwargs):
			yield permit

		for tool, args, stored_result in (
			(
				"jarvis__get_file_pages",
				{"file_url": "/private/files/source.pdf"},
				json.dumps({"ok": True, "data": {"pages": [{"data_b64": "PRIVATE-SHARED-PAGE-BYTES"}]}}),
			),
			(
				"jarvis__query_file",
				{"file_url": "/private/files/source.csv", "sql": "SELECT 1"},
				json.dumps({"ok": True, "data": {"rows": [["PRIVATE-SHARED-QUERY-ROWS"]]}}),
			),
			(
				"jarvis__read_file",
				{"file_url": "/private/files/source.txt"},
				json.dumps({"ok": True, "data": {"content": "PRIVATE-SHARED-FILE-CONTENT"}}),
			),
			(
				"jarvis__get_file_pages",
				{"file_url": "/private/files/unparseable.pdf"},
				"PRIVATE-SHARED-UNPARSEABLE-LEGACY-CACHE",
			),
		):
			with self.subTest(tool=tool):
				cached = frappe._dict(
					name="SOURCE-RECEIPT",
					# No retention marker: this is a raw receipt stored before the
					# source-reference projection was deployed.
					tool_result=stored_result,
				)
				with (
					patch.object(collaboration_dispatch, "callback_scope", callback_scope),
					patch.object(collaboration_dispatch, "receipt_id", return_value="shared-source-call"),
					patch.object(collaboration_dispatch, "complete") as complete,
					patch.object(frappe.db, "get_value", return_value=cached),
					patch.object(api, "_run_tool") as run_tool,
				):
					result = collaboration_dispatch.dispatch("shared-session", tool, args, "call-1")

				self.assertFalse(result["ok"])
				self.assertEqual(result["error"]["code"], "SourceReadRequired")
				self.assertNotIn("PRIVATE-SHARED", json.dumps(result))
				complete.assert_called_once_with(permit, "SOURCE-RECEIPT")
				run_tool.assert_not_called()
