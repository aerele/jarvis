"""Unit tests for jarvis.intake.client. ``_call_route`` is mocked: no DB, no network."""

import json
import unittest
from unittest.mock import patch

from jarvis.intake import client
from jarvis.triggers.llm_task_client import LLMTaskError, LLMTaskNotAvailableError

_CALL = "jarvis.triggers.llm_task_client._call_route"


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
				client.vision_task_complete("p", {}, [])

	def test_ok_false_raises_llm_task_error(self):
		body = json.dumps({"ok": False, "error": {"type": "timeout", "message": "too slow"}}).encode()
		with patch(_CALL, return_value=(504, body)):
			with self.assertRaises(LLMTaskError) as cm:
				client.vision_task_complete("p", {}, [])
		self.assertNotIsInstance(cm.exception, LLMTaskNotAvailableError)
		self.assertIn("too slow", str(cm.exception))

	def test_refused_raises_llm_task_error(self):
		with patch(_CALL, return_value=(401, b"{}")):
			with self.assertRaises(LLMTaskError):
				client.vision_task_complete("p", {}, [])

	def test_returns_details_json(self):
		with patch(_CALL, return_value=(200, _ok({"total": 5}))):
			self.assertEqual(client.vision_task_complete("p", {}, []), {"total": 5})

	def test_no_image_reply_is_returned_as_dict(self):
		with patch(_CALL, return_value=(200, _ok({"error": "NO_IMAGE"}))):
			self.assertEqual(client.vision_task_complete("p", {}, []), {"error": "NO_IMAGE"})

	def test_non_object_reply_raises(self):
		with patch(_CALL, return_value=(200, _ok("prose"))):
			with self.assertRaises(LLMTaskError):
				client.vision_task_complete("p", {}, [])
