"""Unit tests for jarvis.triggers.llm_task_client - the tenant agent gateway
``llm-task`` HTTP client (jarvis-admin-v2#597). No DB, no network: ``requests``
and the Jarvis Settings lookup are mocked.
"""

import json
import unittest
from unittest.mock import Mock, patch

from jarvis.triggers import llm_task_client as lt


def _streamed_response(status=200, body=b"{}"):
	"""A Mock mimicking requests' streamed response context manager (same
	shape used by jarvis.chat.generated_media's fetch_media tests)."""
	resp = Mock()
	resp.status_code = status
	resp.iter_content = Mock(return_value=[body] if body else [b""])
	cm = Mock()
	cm.__enter__ = Mock(return_value=resp)
	cm.__exit__ = Mock(return_value=False)
	return cm


def _settings(agent_url="ws://agent.host:9000", token="tok123"):
	settings = Mock()
	settings.agent_url = agent_url
	settings.get_password = Mock(return_value=token)
	return settings


class TestLLMTaskComplete(unittest.TestCase):
	def test_success_prefers_string_details_json(self):
		body = json.dumps(
			{
				"ok": True,
				"result": {
					"content": [{"type": "text", "text": '"escaped text"'}],
					"details": {"json": "All good.", "provider": "anthropic", "model": "x"},
				},
			}
		).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.post", return_value=_streamed_response(body=body)) as post,
		):
			out = lt.llm_task_complete("do the thing", "<untrusted-data>{}</untrusted-data>")
		self.assertEqual(out, "All good.")
		args, kwargs = post.call_args
		self.assertEqual(args[0], "http://agent.host:9000/tools/invoke")
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok123")
		self.assertFalse(kwargs["allow_redirects"])
		self.assertTrue(kwargs["stream"])
		payload = kwargs["json"]
		self.assertEqual(payload["name"], "llm-task")
		self.assertEqual(
			payload["args"],
			{
				"prompt": "do the thing",
				"input": "<untrusted-data>{}</untrusted-data>",
				"schema": {"type": "string"},
				"maxTokens": lt._MAX_TOKENS,
				"timeoutMs": 60_000,
			},
		)
		self.assertEqual(payload["agentId"], "main")
		self.assertTrue(payload["sessionKey"].startswith("agent:main:trigger-"))
		# provider/model must never be sent - overriding the tenant's own
		# configured model must stay impossible from this client.
		self.assertNotIn("provider", payload["args"])
		self.assertNotIn("model", payload["args"])
		# the HTTP read timeout is kept above the runtime's timeoutMs so the
		# client never gives up before the gateway would.
		self.assertGreater(kwargs["timeout"][1], payload["args"]["timeoutMs"] / 1000)

	def test_custom_timeout_scales_timeoutms_and_stays_below_http_timeout(self):
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch(
				"requests.post", return_value=_streamed_response(body=b'{"ok": true, "result": {}}')
			) as post,
		):
			with self.assertRaises(lt.LLMTaskError):  # empty result -> raises, timing still checked below
				lt.llm_task_complete("p", "i", timeout=30)
		args, kwargs = post.call_args
		self.assertEqual(kwargs["json"]["args"]["timeoutMs"], 30_000)
		self.assertGreater(kwargs["timeout"][1], 30)

	def test_falls_back_to_content_text_when_json_not_a_string(self):
		body = json.dumps(
			{
				"ok": True,
				"result": {
					"content": [{"type": "text", "text": "prose reply"}],
					"details": {"json": {"nested": True}},
				},
			}
		).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.post", return_value=_streamed_response(body=body)),
		):
			out = lt.llm_task_complete("p", "i")
		self.assertEqual(out, "prose reply")

	def test_tool_not_available_raises_typed_error(self):
		body = json.dumps(
			{
				"ok": False,
				"status": 404,
				"error": {"type": "not_found", "message": "Tool not available: llm-task"},
			}
		).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.post", return_value=_streamed_response(status=404, body=body)),
		):
			with self.assertRaises(lt.LLMTaskNotAvailableError):
				lt.llm_task_complete("p", "i")

	def test_404_with_non_json_body_still_raises_not_available(self):
		# An older runtime that doesn't know the /tools/invoke route at all can
		# 404 with a plain-text/HTML body, not the {"error": {...}} envelope -
		# the status code alone must still resolve to "not available" so the
		# caller falls back instead of treating it as a generic parse failure.
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch(
				"requests.post", return_value=_streamed_response(status=404, body=b"<html>not found</html>")
			),
		):
			with self.assertRaises(lt.LLMTaskNotAvailableError):
				lt.llm_task_complete("p", "i")

	def test_other_gateway_error_raises_base_error_not_not_available(self):
		body = json.dumps(
			{"ok": False, "status": 400, "error": {"type": "invalid_request", "message": "bad args"}}
		).encode()
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.post", return_value=_streamed_response(status=400, body=body)),
		):
			with self.assertRaises(lt.LLMTaskError) as ctx:
				lt.llm_task_complete("p", "i")
		self.assertNotIsInstance(ctx.exception, lt.LLMTaskNotAvailableError)
		self.assertIn("bad args", str(ctx.exception))

	def test_missing_agent_url_or_token_raises_without_network_call(self):
		with (
			patch("frappe.get_cached_doc", return_value=_settings(agent_url="", token="tok")),
			patch("requests.post") as post,
		):
			with self.assertRaises(lt.LLMTaskError):
				lt.llm_task_complete("p", "i")
		post.assert_not_called()
		with (
			patch("frappe.get_cached_doc", return_value=_settings(agent_url="ws://h:1", token="")),
			patch("requests.post") as post,
		):
			with self.assertRaises(lt.LLMTaskError):
				lt.llm_task_complete("p", "i")
		post.assert_not_called()

	def test_oversize_response_raises(self):
		big = b"x" * (lt._MAX_RESPONSE_BYTES + 1)
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.post", return_value=_streamed_response(body=big)),
		):
			with self.assertRaises(lt.LLMTaskError):
				lt.llm_task_complete("p", "i")

	def test_non_json_response_raises(self):
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.post", return_value=_streamed_response(body=b"not json")),
		):
			with self.assertRaises(lt.LLMTaskError):
				lt.llm_task_complete("p", "i")

	def test_network_error_raises_base_error(self):
		with (
			patch("frappe.get_cached_doc", return_value=_settings()),
			patch("requests.post", side_effect=RuntimeError("boom")),
		):
			with self.assertRaises(lt.LLMTaskError):
				lt.llm_task_complete("p", "i")

	def test_settings_lookup_failure_propagates_untyped(self):
		# Deliberately NOT wrapped in LLMTaskError: frappe.get_cached_doc("Jarvis
		# Settings") failing (DoesNotExistError, a DB error) is outside this
		# client's HTTP-transport contract. jarvis.triggers.llm_action._complete
		# is the layer responsible for catching this into a Failed activity.
		with patch("frappe.get_cached_doc", side_effect=RuntimeError("db unavailable")):
			with self.assertRaises(RuntimeError):
				lt.llm_task_complete("p", "i")

	def test_headers_never_echoed_into_raised_error_message(self):
		# The client builds its own error messages from the parsed gateway
		# payload / a fixed literal - never from the request object (which
		# would carry the Authorization header) - so a raised error can never
		# echo the bearer token.
		with (
			patch("frappe.get_cached_doc", return_value=_settings(token="super-secret-token")),
			patch("requests.post", side_effect=RuntimeError("boom")),
		):
			with self.assertRaises(lt.LLMTaskError) as ctx:
				lt.llm_task_complete("p", "i")
		self.assertNotIn("super-secret-token", str(ctx.exception))


if __name__ == "__main__":
	unittest.main()
