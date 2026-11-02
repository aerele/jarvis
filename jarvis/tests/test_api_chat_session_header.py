"""Tests for the X-Jarvis-Session header support on jarvis.api.call_tool."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis._password_utils import set_settings_password
from jarvis.api import call_tool
from jarvis.tests.test_api import restore_agent_token, sign_plugin_headers

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
SESS = "Jarvis Chat Session"

PLUGIN_TOKEN = "test-plugin-token-xyz"


class _FakeRequest:
	def __init__(self, headers: dict, body: bytes = b""):
		self.headers = headers
		self._body = body

	def get_data(self, cache: bool = True) -> bytes:
		return self._body


def _plugin_headers(session_key: str, body: bytes = b"") -> dict:
	"""Bearer + session + the signature headers the bench requires since
	JF-014 (unsigned plugin requests are rejected by default)."""
	headers = {
		"X-Jarvis-Token": PLUGIN_TOKEN,
		"X-Jarvis-Session": session_key,
	}
	headers.update(sign_plugin_headers(PLUGIN_TOKEN, session_key, body))
	return headers


import contextlib


@contextlib.contextmanager
def _patch_request(req, *, request_ip: str = "127.0.0.1"):
	"""Patch frappe.request AND set frappe.local.request_ip so the
	C2 IP-allowlist check (default loopback+RFC1918) passes."""
	prior_ip = getattr(frappe.local, "request_ip", None)
	frappe.local.request_ip = request_ip
	with patch.object(frappe, "request", req, create=True):
		try:
			yield
		finally:
			if prior_ip is None:
				try:
					del frappe.local.request_ip
				except AttributeError:
					pass
			else:
				frappe.local.request_ip = prior_ip


def _cleanup_for_session(session_key: str):
	convs = frappe.get_all(CONV, filters={"session_key": session_key}, pluck="name")
	for conv in convs:
		for child in frappe.get_all(MSG, filters={"conversation": conv}, pluck="name"):
			frappe.delete_doc(MSG, child, ignore_permissions=True, force=True)
		frappe.delete_doc(CONV, conv, ignore_permissions=True, force=True)
	sess_rows = frappe.get_all(SESS, filters={"session_key": session_key}, pluck="name")
	for s in sess_rows:
		frappe.delete_doc(SESS, s, ignore_permissions=True, force=True)
	frappe.db.commit()


def _delete_turn(run_id: str):
	frappe.db.delete("Jarvis Chat Turn", {"name": run_id})
	frappe.db.commit()


class TestCallToolWithSessionHeader(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		cls._original_token = settings.get_password("agent_token", raise_exception=False) or ""
		set_settings_password(settings, "agent_token", PLUGIN_TOKEN)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		restore_agent_token(cls._original_token)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.session_key = "agent:test:abc-123"
		_cleanup_for_session(self.session_key)
		conv = frappe.get_doc(
			{
				"doctype": CONV,
				"title": "T",
				"session_key": self.session_key,
				"status": "Active",
			}
		)
		conv.insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": SESS,
				"session_key": self.session_key,
				"user": "Administrator",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		self.conv_name = conv.name

	def tearDown(self):
		_cleanup_for_session(self.session_key)

	def test_session_header_persists_tool_message(self):
		req = _FakeRequest(_plugin_headers(self.session_key))
		with _patch_request(req):
			with patch("jarvis.api.publish_realtime_tool_result"):
				result = call_tool("get_schema", args={"doctype": "Customer"})

		self.assertTrue(result["ok"])
		tools = frappe.get_all(
			MSG,
			filters={"conversation": self.conv_name, "role": "tool"},
			fields=["tool_name", "tool_args", "tool_result", "tool_status"],
		)
		self.assertEqual(len(tools), 1)
		self.assertEqual(tools[0]["tool_name"], "get_schema")
		self.assertEqual(tools[0]["tool_status"], "completed")

	def test_session_header_publishes_realtime_tool_result(self):
		req = _FakeRequest(_plugin_headers(self.session_key))
		with _patch_request(req):
			with patch("jarvis.api.publish_realtime_tool_result") as pub:
				call_tool("get_schema", args={"doctype": "Customer"})
		pub.assert_called_once()
		_, kwargs = pub.call_args
		self.assertEqual(kwargs["tool_name"], "get_schema")
		self.assertEqual(kwargs["status"], "completed")

	def test_realtime_tool_result_carries_the_saved_tool_call_id(self):
		# The SPA binds a live report to its reply through this id (the
		# "Reports consulted" card), the same way the saved rows do.
		headers = {**_plugin_headers(self.session_key), "X-Jarvis-Tool-Call-Id": "call_live_1"}
		with _patch_request(_FakeRequest(headers)):
			with patch("jarvis.api.publish_realtime_tool_result") as pub:
				call_tool("get_schema", args={"doctype": "Customer"})
		pub.assert_called_once()
		_, kwargs = pub.call_args
		self.assertEqual(kwargs["tool_call_id"], "call_live_1")
		saved = frappe.db.get_value(MSG, kwargs["tool_message_id"], "tool_call_id")
		self.assertEqual(saved, "call_live_1")

	def test_a_callback_tool_is_bound_to_the_running_reply(self):
		# A Claude CLI turn sends no tool events, so the callback binds its tool to the
		# running turn's reply, or "Reports consulted" loses the report on reload.
		def message(role, seq):
			return frappe.get_doc(
				{"doctype": MSG, "conversation": self.conv_name, "seq": seq, "role": role}
			).insert(ignore_permissions=True)

		seed, reply = message("user", 1), message("assistant", 2)
		run_id = frappe.generate_hash(length=12)
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Turn",
				"run_id": run_id,
				"conversation": self.conv_name,
				"relay_target_id": "test-target",
				"turn_class": "interactive",
				"state": "streaming",
				"seed_message": seed.name,
				"assistant_message": reply.name,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(_delete_turn, run_id)
		headers = {**_plugin_headers(self.session_key), "X-Jarvis-Tool-Call-Id": "mcp-live-1"}
		with _patch_request(_FakeRequest(headers)):
			with patch("jarvis.api.publish_realtime_tool_result"):
				call_tool("get_schema", args={"doctype": "Customer"})
		self.assertEqual(json.loads(frappe.db.get_value(MSG, reply.name, "tool_call_ids")), ["mcp-live-1"])

	def test_missing_session_header_now_rejected(self):
		"""Path A v2 requires X-Jarvis-Session - there is no user header any
		more, so without a session we cannot resolve identity."""
		req = _FakeRequest({"X-Jarvis-Token": PLUGIN_TOKEN})
		with _patch_request(req):
			result = call_tool("get_schema", args={"doctype": "Customer"})
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		self.assertIn("X-Jarvis-Session", result["error"]["message"])

	def test_unknown_session_now_rejected(self):
		"""Without a Chat Session row we have no user to dispatch as - fail
		fast rather than silently fall back to a different identity."""
		req = _FakeRequest(_plugin_headers("agent:nonexistent"))
		with _patch_request(req):
			result = call_tool("get_schema", args={"doctype": "Customer"})
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "InvalidArgumentError")
		self.assertIn("unknown session", result["error"]["message"])
