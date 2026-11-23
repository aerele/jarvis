"""Unit tests for ``jarvis.connectors.result_view`` - how a connector's answer is
shown to the agent. No bench."""

from __future__ import annotations

import json
import unittest

from jarvis.connectors.result_view import payload_text, receipt_payload


def _text(text):
	return {"type": "text", "text": text}


class TestPayloadText(unittest.TestCase):
	def test_text_items_are_joined(self):
		result = {"content": [_text("first"), _text("second")]}
		self.assertEqual(payload_text(result), "first second")

	def test_structured_content_comes_first_as_compact_json(self):
		result = {"structuredContent": {"id": 7, "url": "https://x.test/7"}, "content": []}
		self.assertEqual(payload_text(result), '{"id":7,"url":"https://x.test/7"}')

	def test_text_that_repeats_structured_content_is_dropped(self):
		data = {"id": 7, "items": [1, 2]}
		result = {"structuredContent": data, "content": [_text(json.dumps(data, indent=2))]}
		self.assertEqual(payload_text(result), '{"id":7,"items":[1,2]}')

	def test_text_that_differs_from_structured_content_is_kept(self):
		result = {"structuredContent": {"id": 7}, "content": [_text("Created issue 7")]}
		self.assertEqual(payload_text(result), '{"id":7} Created issue 7')

	def test_other_content_types_become_markers(self):
		result = {
			"content": [
				{"type": "image", "data": "AAAA", "mimeType": "image/png"},
				{"type": "audio", "data": "BBBB"},
				{"type": "resource_link", "uri": "https://x.test/f?X-Amz-Signature=abc123def456"},
				{"type": "resource", "resource": {"uri": "file:///a.txt", "text": "body"}},
			]
		}
		out = payload_text(result)
		self.assertIn("[image omitted]", out)
		self.assertIn("[audio omitted]", out)
		self.assertIn("[resource: file:///a.txt]", out)
		self.assertNotIn("AAAA", out)
		self.assertNotIn("body", out)
		# The marker's uri is scrubbed by receipt_payload, not here.
		self.assertNotIn("abc123def456", receipt_payload(result, 1000))

	def test_empty_and_malformed_results(self):
		for result in (None, {}, [], "text", {"content": []}, {"content": [None, 3, {}]}):
			self.assertIn(payload_text(result), ("", "[content omitted]"))


class TestReceiptPayload(unittest.TestCase):
	def test_short_payload_is_returned_whole(self):
		self.assertEqual(receipt_payload({"content": [_text("done")]}, 100), "done")

	def test_secrets_are_scrubbed(self):
		text = "link https://x.test/f?X-Amz-Signature=abcdef0123456789 key AKIAIOSFODNN7EXAMPLE"
		out = receipt_payload({"content": [_text(text)]}, 1000)
		self.assertNotIn("abcdef0123456789", out)
		self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)
		self.assertIn("[REDACTED-SECRET]", out)

	def test_long_payload_is_cut_with_a_suffix(self):
		out = receipt_payload({"content": [_text("x" * 5000)]}, 100)
		self.assertTrue(out.startswith("x" * 100))
		self.assertTrue(out.endswith("[cut: showed 100 of 5000 characters]"))

	def test_a_secret_across_the_cut_is_never_half_shown(self):
		secret = "ghp_" + "a1B2c3D4e5" * 4
		text = "y" * 89 + " " + secret
		out = receipt_payload({"content": [_text(text)]}, 100)
		self.assertNotIn("ghp_", out)

	def test_no_content_gives_empty_string(self):
		self.assertEqual(receipt_payload({"content": []}, 100), "")
		self.assertEqual(receipt_payload(None, 100), "")
