"""Unit tests for jarvis.intake.client. ``_call_route`` is mocked: no DB, no network."""

import json
import unittest
from unittest.mock import patch

from jarvis.intake import client
from jarvis.triggers.llm_task_client import LLMTaskError, LLMTaskNotAvailableError

_CALL = "jarvis.triggers.llm_task_client._call_route"
_IMG = [{"mime": "image/png", "data_b64": "AAA"}]


def _ok(json_val):
	return json.dumps({"ok": True, "result": {"content": [], "details": {"json": json_val}}}).encode()


class TestVisionTaskComplete(unittest.TestCase):
	def test_sends_images_and_schema(self):
		images = [{"mime": "image/png", "data_b64": "AAA", "file_name": "p1.png"}]
		schema = {"type": "object"}
		with patch(_CALL, return_value=(200, _ok({"a": 1}))) as call:
			client.vision_task_complete(
				"read it", schema, images, timeout=30, model_ref={"provider": "p", "model": "m"}
			)
		path, body, read_timeout = call.call_args.args
		self.assertEqual(path, "/jarvis/vision-task")
		args = body["args"]
		self.assertEqual(args["images"], [{"mime": "image/png", "data_b64": "AAA"}])
		self.assertEqual(args["schema"], schema)
		self.assertEqual(args["prompt"], "read it")
		self.assertEqual(args["timeoutMs"], 30000)
		self.assertEqual(args["maxTokens"], 4000)
		self.assertEqual((args["provider"], args["model"]), ("p", "m"))
		self.assertGreater(read_timeout, 30)

	def test_404_raises_not_available(self):
		with patch(_CALL, return_value=(404, b"<html>nope</html>")):
			with self.assertRaises(LLMTaskNotAvailableError):
				client.vision_task_complete("p", {}, _IMG)

	def test_ok_false_raises_llm_task_error(self):
		body = json.dumps({"ok": False, "error": {"type": "timeout", "message": "too slow"}}).encode()
		with patch(_CALL, return_value=(504, body)):
			with self.assertRaises(LLMTaskError) as cm:
				client.vision_task_complete("p", {}, _IMG)
		self.assertNotIsInstance(cm.exception, LLMTaskNotAvailableError)
		self.assertIn("too slow", str(cm.exception))

	def test_gateway_failure_says_vision_task_not_llm_task(self):
		with patch(_CALL, return_value=(500, b"{}")):
			with self.assertRaises(LLMTaskError) as cm:
				client.vision_task_complete("p", {}, _IMG)
		self.assertEqual(str(cm.exception), "vision task invocation failed (HTTP 500)")

	def test_404_message_says_vision_task(self):
		with patch(_CALL, return_value=(404, b"<html>nope</html>")):
			with self.assertRaises(LLMTaskNotAvailableError) as cm:
				client.vision_task_complete("p", {}, _IMG)
		self.assertEqual(str(cm.exception), "vision task invocation failed (HTTP 404)")

	def test_refused_raises_llm_task_error(self):
		with patch(_CALL, return_value=(401, b"{}")):
			with self.assertRaises(LLMTaskError):
				client.vision_task_complete("p", {}, _IMG)

	def test_returns_details_json(self):
		with patch(_CALL, return_value=(200, _ok({"total": 5}))):
			self.assertEqual(client.vision_task_complete("p", {}, _IMG), {"total": 5})

	def test_no_image_reply_is_returned_as_dict(self):
		with patch(_CALL, return_value=(200, _ok({"error": "NO_IMAGE"}))):
			self.assertEqual(client.vision_task_complete("p", {}, _IMG), {"error": "NO_IMAGE"})

	def test_non_object_reply_raises(self):
		with patch(_CALL, return_value=(200, _ok("prose"))):
			with self.assertRaises(LLMTaskError):
				client.vision_task_complete("p", {}, _IMG)

	def test_wait_is_longer_than_the_plugins_105_seconds(self):
		with patch(_CALL, return_value=(200, _ok({"a": 1}))) as call:
			client.vision_task_complete("p", {}, _IMG, timeout=90)
		self.assertGreaterEqual(call.call_args.args[2], 110)

	def test_request_over_the_shared_limits_is_refused_before_sending(self):
		from jarvis.intake import limits

		too_many = _IMG * (limits.MAX_IMAGES + 1)
		huge = [{"mime": "image/png", "data_b64": "A" * (limits.IMAGE_BYTES * 4 // 3 + 100)}]
		for images in ([], too_many, huge):
			with self.subTest(n=len(images)), patch(_CALL) as call:
				with self.assertRaises(LLMTaskError) as cm:
					client.vision_task_complete("p", {}, images)
				call.assert_not_called()
				self.assertFalse(client.reached_model(cm.exception))

	def test_reader_and_client_share_one_limit_module(self):
		from jarvis.intake import limits, reader

		self.assertIs(reader.limits, limits)
		self.assertEqual((limits.MAX_PAGES, limits.MAX_IMAGES, limits.IMAGE_BYTES), (10, 10, 200_000))

	# ---- billing state: did the request reach the model? (R23) ------------------------
	def _route(self, *, raises=None, status=200, body=b"{}", chunks=None):
		"""Run vision_task_complete through the real transport with ``route_request`` faked."""
		from unittest import mock

		class Response:
			status_code = status

			def __enter__(self):
				return self

			def __exit__(self, *a):
				return False

			def iter_content(self, size):
				return iter(chunks if chunks is not None else [body])

		patches = (
			mock.patch("jarvis.triggers.llm_task_client._agent_url", return_value="http://agent"),
			mock.patch("jarvis.triggers.llm_task_client.session_claims.claim"),
			mock.patch("jarvis.chat.site_pin.assert_not_copy"),
			mock.patch(
				"jarvis.chat.plugin_routes.route_request",
				side_effect=raises if raises else lambda *a, **kw: Response(),
			),
		)
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		try:
			client.vision_task_complete("p", {}, _IMG)
		except LLMTaskError as e:
			return e
		self.fail("expected an error")

	def _plugin_error(self, cause):
		from jarvis.chat import plugin_routes

		err = plugin_routes.PluginRouteError("The agent route request failed.")
		err.__cause__ = cause
		return err

	def test_failures_before_the_request_left_the_site_are_not_counted(self):
		import requests
		from urllib3.exceptions import NewConnectionError

		refused = requests.ConnectionError(NewConnectionError(None, "refused"))
		for cause in (requests.ConnectTimeout("slow connect"), refused):
			with self.subTest(cause=type(cause).__name__):
				err = self._route(raises=self._plugin_error(cause))
				self.assertFalse(client.reached_model(err))

	def test_no_agent_configured_and_a_missing_key_are_not_counted(self):
		from unittest import mock

		with mock.patch("jarvis.triggers.llm_task_client._agent_url", return_value=""):
			with self.assertRaises(LLMTaskError) as cm:
				client.vision_task_complete("p", {}, _IMG)
		self.assertFalse(client.reached_model(cm.exception))
		self.assertFalse(client.reached_model(self._route(raises=self._plugin_error(None))))

	def test_failures_after_the_request_was_sent_are_counted(self):
		import requests

		for cause in (
			requests.ReadTimeout("model took too long"),
			requests.ConnectionError("Connection aborted."),
			requests.exceptions.ChunkedEncodingError("cut off"),
		):
			with self.subTest(cause=type(cause).__name__):
				err = self._route(raises=self._plugin_error(cause))
				self.assertTrue(client.reached_model(err))

	def test_an_http_error_after_the_send_is_counted(self):
		body = json.dumps({"ok": False, "error": {"message": "bad gateway"}}).encode()
		self.assertTrue(client.reached_model(self._route(status=502, body=body)))
		self.assertTrue(client.reached_model(self._route(status=403)))
		self.assertTrue(client.reached_model(self._route(status=200, body=b"not json")))

	def test_an_oversized_response_is_counted(self):
		from jarvis.triggers import llm_task_client as lt

		huge = [b"x" * (lt._MAX_RESPONSE_BYTES + 1)]
		self.assertTrue(client.reached_model(self._route(chunks=huge)))

	def test_a_404_old_plugin_is_not_counted(self):
		err = self._route(status=404, body=b"<html>nope</html>")
		self.assertIsInstance(err, LLMTaskNotAvailableError)
		self.assertFalse(client.reached_model(err))
