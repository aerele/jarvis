"""Unit tests for ``jarvis.connectors.mcp_client`` - the synchronous MCP
Streamable-HTTP client. Plain ``unittest`` (no bench): the SSRF/transport seam
``ssrf.open_pinned_request`` is mocked, so no socket is ever opened. Covers SSE
frame parsing, JSON vs SSE response paths, session-id + protocol-version header
wiring, version-negotiation rejection, and the 404 session-expiry retry.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from jarvis.connectors import mcp_client


class _FakeResp:
	def __init__(self, status=200, headers=None, chunks=None):
		self.status = status
		self.headers = headers or {}
		self._chunks = chunks or []

	def stream(self, amt=8192, decode_content=True):
		yield from self._chunks

	def close(self):
		pass


def _json_resp(obj, status=200, headers=None):
	h = {"Content-Type": "application/json"}
	h.update(headers or {})
	return _FakeResp(status, h, [json.dumps(obj).encode("utf-8")])


def _sse_resp(frames, status=200, headers=None):
	h = {"Content-Type": "text/event-stream"}
	h.update(headers or {})
	chunks = [f.encode("utf-8") for f in frames]
	return _FakeResp(status, h, chunks)


class _Seam:
	"""Scriptable stand-in for ``ssrf.open_pinned_request``: pops the next
	response per call and records the headers/body it was called with. When the
	script is exhausted it returns a 405 (used by best-effort session DELETE)."""

	def __init__(self, responses):
		self._responses = list(responses)
		self.calls = []

	def __call__(self, url, *, method, headers, body, connect_timeout, read_timeout, egress_allowed):
		self.calls.append({"url": url, "method": method, "headers": dict(headers), "body": body})
		resp = self._responses.pop(0) if self._responses else _FakeResp(405)
		return resp, mock.MagicMock(), url

	def request_calls(self):
		# Calls that carried a JSON-RPC body (i.e. not the DELETE teardown).
		return [c for c in self.calls if c["body"]]


class TestSseParsing(unittest.TestCase):
	def test_multiline_data_is_joined(self):
		raw = b'data: {"a":\ndata: 1}\n\n'
		self.assertEqual(list(mcp_client.iter_sse_messages(raw)), [{"a": 1}])

	def test_comments_and_other_fields_ignored(self):
		raw = b': keep-alive\nevent: message\nid: 42\ndata: {"x":true}\n\n'
		self.assertEqual(list(mcp_client.iter_sse_messages(raw)), [{"x": True}])

	def test_non_json_data_is_skipped(self):
		raw = b'data: not json\n\ndata: {"ok":1}\n\n'
		self.assertEqual(list(mcp_client.iter_sse_messages(raw)), [{"ok": 1}])

	def test_multiple_events(self):
		raw = b'data: {"n":1}\n\ndata: {"n":2}\n\n'
		self.assertEqual([m["n"] for m in mcp_client.iter_sse_messages(raw)], [1, 2])


class TestResponseMatching(unittest.TestCase):
	def test_notification_is_not_a_response(self):
		self.assertFalse(mcp_client._matches_response({"jsonrpc": "2.0", "method": "notifications/x"}, 1))

	def test_server_request_is_not_a_response(self):
		self.assertFalse(mcp_client._matches_response({"jsonrpc": "2.0", "id": 1, "method": "sampling/x"}, 1))

	def test_matching_id_with_result(self):
		self.assertTrue(mcp_client._matches_response({"jsonrpc": "2.0", "id": 2, "result": {}}, 2))

	def test_wrong_id_is_not_matched(self):
		self.assertFalse(mcp_client._matches_response({"jsonrpc": "2.0", "id": 9, "result": {}}, 2))


def _init_ok(session_id="sess-123", version="2025-06-18"):
	return _json_resp(
		{
			"jsonrpc": "2.0",
			"id": 1,
			"result": {
				"protocolVersion": version,
				"capabilities": {},
				"serverInfo": {"name": "s", "version": "1"},
			},
		},
		headers={"Mcp-Session-Id": session_id},
	)


def _initialized_202():
	return _FakeResp(202, {})


class TestSessionFlow(unittest.TestCase):
	def test_fetch_tools_echoes_session_and_protocol_headers(self):
		seam = _Seam(
			[
				_init_ok(),
				_initialized_202(),
				_json_resp(
					{"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "get_x", "inputSchema": {}}]}}
				),
			]
		)
		with mock.patch.object(mcp_client.ssrf, "open_pinned_request", seam):
			tools = mcp_client.fetch_tools("https://api.example.com/mcp", "PAT-TOKEN")
		self.assertEqual(tools, [{"name": "get_x", "inputSchema": {}}])

		reqs = seam.request_calls()
		# initialize carries Authorization but NOT the protocol header (no version yet)
		self.assertEqual(reqs[0]["headers"].get("Authorization"), "Bearer PAT-TOKEN")
		self.assertNotIn("MCP-Protocol-Version", reqs[0]["headers"])
		self.assertIn("application/json", reqs[0]["headers"]["Accept"])
		self.assertIn("text/event-stream", reqs[0]["headers"]["Accept"])
		# every request after initialize echoes session id + negotiated version
		for c in reqs[1:]:
			self.assertEqual(c["headers"].get("Mcp-Session-Id"), "sess-123")
			self.assertEqual(c["headers"].get("MCP-Protocol-Version"), "2025-06-18")

	def test_paginated_tools_list(self):
		seam = _Seam(
			[
				_init_ok(),
				_initialized_202(),
				_json_resp(
					{"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "a"}], "nextCursor": "c1"}}
				),
				_json_resp({"jsonrpc": "2.0", "id": 3, "result": {"tools": [{"name": "b"}]}}),
			]
		)
		with mock.patch.object(mcp_client.ssrf, "open_pinned_request", seam):
			tools = mcp_client.fetch_tools("https://api.example.com/mcp", None)
		self.assertEqual([t["name"] for t in tools], ["a", "b"])

	def test_call_tool_over_sse(self):
		seam = _Seam(
			[
				_init_ok(),
				_initialized_202(),
				_sse_resp(
					[
						"data: "
						+ json.dumps(
							{
								"jsonrpc": "2.0",
								"id": 2,
								"result": {"content": [{"type": "text", "text": "hi"}], "isError": False},
							}
						)
						+ "\n\n"
					]
				),
			]
		)
		with mock.patch.object(mcp_client.ssrf, "open_pinned_request", seam):
			result = mcp_client.run_tool("https://api.example.com/mcp", "t", "get_x", {"q": 1})
		self.assertEqual(result["content"][0]["text"], "hi")

	def test_unsupported_protocol_version_rejected(self):
		seam = _Seam([_init_ok(version="1999-01-01"), _initialized_202()])
		with mock.patch.object(mcp_client.ssrf, "open_pinned_request", seam):
			with self.assertRaises(mcp_client.McpError) as cm:
				mcp_client.fetch_tools("https://api.example.com/mcp", None)
		self.assertEqual(cm.exception.kind, mcp_client.ERR_PROTOCOL)

	def test_json_rpc_error_raises_rpc_kind(self):
		seam = _Seam(
			[
				_init_ok(),
				_initialized_202(),
				_json_resp({"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "Unknown tool"}}),
			]
		)
		with mock.patch.object(mcp_client.ssrf, "open_pinned_request", seam):
			with self.assertRaises(mcp_client.McpError) as cm:
				mcp_client.run_tool("https://api.example.com/mcp", None, "nope", {})
		self.assertEqual(cm.exception.kind, mcp_client.ERR_RPC)
		self.assertEqual(cm.exception.code, -32602)

	def test_404_session_expiry_triggers_one_retry(self):
		# First session: init OK, initialized, then tools/call 404 (expired).
		# Retry: fresh init, initialized, tools/call success.
		seam = _Seam(
			[
				_init_ok(session_id="s1"),
				_initialized_202(),
				_FakeResp(404, {"Content-Type": "application/json"}, [b"{}"]),
				_init_ok(session_id="s2"),
				_initialized_202(),
				_json_resp(
					{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "ok"}]}}
				),
			]
		)
		with mock.patch.object(mcp_client.ssrf, "open_pinned_request", seam):
			result = mcp_client.run_tool("https://api.example.com/mcp", None, "get_x", {})
		self.assertEqual(result["content"][0]["text"], "ok")

	def test_http_500_raises_http_kind_with_code(self):
		seam = _Seam(
			[
				_init_ok(),
				_initialized_202(),
				_FakeResp(503, {"Content-Type": "application/json"}, [b"{}"]),
			]
		)
		with mock.patch.object(mcp_client.ssrf, "open_pinned_request", seam):
			with self.assertRaises(mcp_client.McpError) as cm:
				mcp_client.run_tool("https://api.example.com/mcp", None, "get_x", {})
		self.assertEqual(cm.exception.kind, mcp_client.ERR_HTTP)
		self.assertEqual(cm.exception.code, 503)

	def test_total_timeout_budget_trips(self):
		# A clock that jumps past the budget once the session is under way. An
		# infinite generator (0 for the first few calls, then 100) is robust to the
		# exact number of clock reads (_run_session now reads it too, to span the
		# retry deadline).
		def _ticks():
			for _ in range(4):
				yield 0.0
			while True:
				yield 100.0

		gen = _ticks()
		seam = _Seam([_init_ok(), _initialized_202()])
		with mock.patch.object(mcp_client.ssrf, "open_pinned_request", seam):
			with self.assertRaises(mcp_client.McpError) as cm:
				mcp_client.fetch_tools(
					"https://api.example.com/mcp", None, total_timeout=5.0, clock=lambda: next(gen)
				)
		self.assertEqual(cm.exception.kind, mcp_client.ERR_TRANSPORT)

	def test_session_expired_retry_carries_remaining_budget(self):
		# A naive retry gave the second session a fresh full total_timeout, so a
		# slow first session + retry could reach ~2x the budget (past the plugin's
		# 30s AbortController). A single deadline must span both attempts: the retry
		# gets only what remained, never a second full budget.
		constructed_budgets: list[float] = []
		t = {"now": 1000.0}

		class _FakeClient:
			def __init__(self, base_url, token, **kw):
				constructed_budgets.append(kw.get("total_timeout"))
				self._first = len(constructed_budgets) == 1

			def initialize(self):
				# Each session burns 5s of wall-clock before its op runs.
				t["now"] += 5.0

			def list_tools(self, **kw):
				if self._first:
					raise mcp_client.McpError("expired", kind=mcp_client.ERR_SESSION_EXPIRED)
				return [{"name": "x"}]

			def close(self):
				pass

		with mock.patch.object(mcp_client, "McpClient", _FakeClient):
			tools = mcp_client.fetch_tools(
				"https://api.example.com/mcp", None, total_timeout=20.0, clock=lambda: t["now"]
			)
		self.assertEqual(tools, [{"name": "x"}])
		self.assertEqual(len(constructed_budgets), 2)
		self.assertAlmostEqual(constructed_budgets[0], 20.0)
		# First session burned 5s, so the retry gets ~15s, not another full 20s.
		self.assertAlmostEqual(constructed_budgets[1], 15.0)
		self.assertLess(constructed_budgets[1], constructed_budgets[0])

	def test_session_expired_not_retried_when_budget_exhausted(self):
		# If the first session used up the whole budget, the expired-session retry
		# must NOT fire (it would add a fresh call past the deadline); the original
		# session_expired error surfaces instead.
		t = {"now": 1000.0}

		class _FakeClient:
			def __init__(self, base_url, token, **kw):
				pass

			def initialize(self):
				t["now"] += 25.0  # blow the whole 20s budget

			def list_tools(self, **kw):
				raise mcp_client.McpError("expired", kind=mcp_client.ERR_SESSION_EXPIRED)

			def close(self):
				pass

		with mock.patch.object(mcp_client, "McpClient", _FakeClient):
			with self.assertRaises(mcp_client.McpError) as cm:
				mcp_client.fetch_tools(
					"https://api.example.com/mcp", None, total_timeout=20.0, clock=lambda: t["now"]
				)
		self.assertEqual(cm.exception.kind, mcp_client.ERR_SESSION_EXPIRED)


if __name__ == "__main__":
	unittest.main()
