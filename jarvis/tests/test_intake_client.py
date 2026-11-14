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

	def test_reached_model_distinguishes_sent_from_unsent(self):
		import requests

		def transport(cause):
			err = LLMTaskError("llm-task request failed")
			err.__cause__ = cause
			return err

		cases = (
			(patch(_CALL, side_effect=transport(requests.ConnectionError("refused"))), False),
			(patch(_CALL, side_effect=transport(requests.Timeout("slow"))), True),
			(patch(_CALL, return_value=(404, b"nope")), False),
			(patch(_CALL, return_value=(403, b"")), False),
			(patch(_CALL, return_value=(200, b"not json")), True),
			(
				patch(
					_CALL,
					return_value=(500, json.dumps({"ok": False, "error": {"message": "boom"}}).encode()),
				),
				True,
			),
		)
		for mocked, expected in cases:
			with self.subTest(expected=expected), mocked:
				with self.assertRaises(LLMTaskError) as cm:
					client.vision_task_complete("p", {}, _IMG)
				self.assertIs(client.reached_model(cm.exception), expected)
