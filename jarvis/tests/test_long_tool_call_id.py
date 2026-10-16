"""Long OpenClaw execution ids must survive receipt storage and correlation."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.api import _dispatch_from_session

MSG = "Jarvis Chat Message"


class TestLongToolCallId(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.session_key = "agent:test:long-id:" + frappe.generate_hash(length=12)
		self.conversation = (
			frappe.get_doc(
				{
					"doctype": "Jarvis Conversation",
					"title": "Long tool-call id regression",
					"session_key": self.session_key,
					"status": "Active",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		frappe.db.commit()

	def tearDown(self):
		# Receipt persistence commits; explicitly remove only this test's rows.
		frappe.db.rollback()
		for name in frappe.get_all(MSG, filters={"conversation": self.conversation}, pluck="name"):
			frappe.delete_doc(MSG, name, force=True, ignore_permissions=True)
		frappe.delete_doc("Jarvis Conversation", self.conversation, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def _call(self, call_id):
		# Start after authentication: exercise the real dispatch, receipt insert,
		# dedupe and result publication without changing the site's agent token.
		with patch("jarvis.api._get_header", return_value=call_id):
			return _dispatch_from_session(
				frappe.session.user, self.session_key, "get_schema", {"doctype": "User"}
			)

	def test_nested_id_survives_receipt_and_realtime(self):
		# Synthetic version of the 139-character nested id observed in the incident.
		call_id = f"tool_search_code:call_{'a' * 32}_fc_{'b' * 50}:jarvis__get_creation_context:1"
		self.assertEqual(len(call_id), 139)
		with patch("jarvis.api.publish_realtime_tool_result") as publish:
			result = self._call(call_id)
		self.assertTrue(result["ok"])
		publish.assert_called_once()
		kwargs = publish.call_args.kwargs
		self.assertEqual(kwargs["tool_call_id"], call_id)
		self.assertEqual(frappe.db.get_value(MSG, kwargs["tool_message_id"], "tool_call_id"), call_id)

	def test_long_ids_dedupe_without_truncating_distinct_calls(self):
		prefix = "tool_search_code:" + "a" * 494
		first, second = prefix + "1", prefix + "2"
		self.assertEqual(len(first), 512)
		with patch("jarvis.api.publish_realtime_tool_result") as publish:
			for call_id in (first, first, second):
				self.assertTrue(self._call(call_id)["ok"])
		self.assertEqual(publish.call_count, 2)
		rows = frappe.get_all(
			MSG,
			filters={"conversation": self.conversation, "role": "tool"},
			fields=["tool_call_id", "tool_status"],
		)
		self.assertEqual(len(rows), 2)
		self.assertEqual({row.tool_call_id for row in rows}, {first, second})
		self.assertTrue(all(row.tool_status == "completed" for row in rows))
