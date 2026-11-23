"""Unit tests for ``jarvis.connectors.result_view`` - how a connector's answer is
shown to the agent. No bench."""

from __future__ import annotations

import json
import time
import unittest

from jarvis.connectors.result_view import bound_tool_result, payload_text, receipt_payload


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


class TestBoundedScrubbing(unittest.TestCase):
	"""An answer can be megabytes: the cost of scrubbing must not grow with it."""

	def _timed(self, text, limit=1000):
		started = time.perf_counter()
		out = receipt_payload({"content": [_text(text)]}, limit)
		return out, time.perf_counter() - started

	def test_many_unterminated_key_blocks_return_fast(self):
		# Each block start scans to the end of the text for its footer: quadratic unwindowed.
		text = "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n" * 60_000
		self.assertGreater(len(text), 2_000_000)
		out, seconds = self._timed(text)
		print(f"\n  adversarial key-block payload: {len(text)} chars in {seconds * 1000:.0f} ms")
		self.assertLess(seconds, 1.0)
		self.assertNotIn("AAAA", out)
		self.assertIn("of %d characters]" % len(text), out)

	def test_jwt_lookalike_run_returns_fast(self):
		text = "eyJaaaaa-" * 250_000
		self.assertGreater(len(text), 2_000_000)
		out, seconds = self._timed(text)
		print(f"\n  adversarial JWT-like payload: {len(text)} chars in {seconds * 1000:.0f} ms")
		self.assertLess(seconds, 1.0)
		self.assertTrue(out.endswith("characters]"))

	def test_key_block_whose_end_lies_past_the_window_is_masked(self):
		key = "-----BEGIN PRIVATE KEY-----\n" + "KEYBODY" * 5000 + "\n-----END PRIVATE KEY-----"
		out, _seconds = self._timed("note: " + key, limit=200)
		self.assertNotIn("KEYBODY", out)
		self.assertNotIn("BEGIN PRIVATE", out)
		self.assertIn("[REDACTED-SECRET]", out)

	def test_cut_marker_reports_the_full_length(self):
		out, _seconds = self._timed("x" * 100_000, limit=500)
		self.assertTrue(out.endswith("[cut: showed 500 of 100000 characters]"))


BUDGET = 34_800


def _answer(*content, **mcp):
	return {"ok": True, "result": {"content": list(content), **mcp}}


def _size(obj):
	return len(json.dumps(obj, default=str, ensure_ascii=False, separators=(",", ":")))


def _issues(n=500, width=300):
	return [{"id": i, "title": f"issue {i}", "body": "x" * width} for i in range(n)]


class TestBoundToolResult(unittest.TestCase):
	def test_a_small_answer_is_returned_unchanged(self):
		data = _answer(_text("created issue 7"), isError=False)
		out, cuts = bound_tool_result(data, BUDGET)
		self.assertIs(out, data)
		self.assertIsNone(cuts)
		self.assertEqual(json.dumps(out), json.dumps(data))

	def test_a_159kb_dict_answer_comes_back_within_the_budget_with_a_marker(self):
		blob = {"records": _issues(400, 380), "totalCount": 400}
		data = _answer(_text(json.dumps(blob, indent=2)))
		self.assertGreater(_size(data), 159_000)
		out, cuts = bound_tool_result(data, BUDGET)
		self.assertLessEqual(_size(out), BUDGET)
		self.assertTrue(out["_truncated"])
		self.assertEqual(out["total_items"], 400)
		self.assertLess(out["shown_items"], 400)
		self.assertIn("narrow the request", out["note"])
		self.assertEqual(cuts[0]["unit"], "items")
		last = out["result"]["content"][-1]
		self.assertEqual(last["type"], "text")
		self.assertIn("of 400 items", last["text"])

	def test_a_list_of_500_keeps_whole_items_and_reports_the_dropped_count(self):
		data = _answer(_text(json.dumps(_issues())))
		out, _cuts = bound_tool_result(data, BUDGET)
		kept = json.loads(out["result"]["content"][0]["text"])
		self.assertIsInstance(kept, list)
		self.assertEqual(out["shown_items"], len(kept))
		self.assertEqual(out["total_items"], 500)
		self.assertEqual(kept, _issues()[: len(kept)])
		self.assertIn(f"first {len(kept)} of 500 items", out["note"])
		self.assertLessEqual(_size(out), BUDGET)

	def test_a_dict_with_one_list_keeps_its_other_keys(self):
		blob = {"totalCount": 500, "page": 1, "issues": _issues()}
		out, _cuts = bound_tool_result(_answer(_text(json.dumps(blob))), BUDGET)
		kept = json.loads(out["result"]["content"][0]["text"])
		self.assertEqual((kept["totalCount"], kept["page"]), (500, 1))
		self.assertEqual(out["total_items"], 500)

	def test_plain_text_is_cut_by_characters(self):
		out, cuts = bound_tool_result(_answer(_text("word " * 60_000)), BUDGET)
		self.assertLessEqual(_size(out), BUDGET)
		self.assertEqual(cuts[0]["unit"], "characters")
		self.assertEqual(out["total_characters"], 300_000)
		self.assertGreater(out["shown_characters"], 20_000)

	def test_escaped_text_still_fits(self):
		out, _cuts = bound_tool_result(_answer(_text('say "hi"\n' * 30_000)), BUDGET)
		self.assertLessEqual(_size(out), BUDGET)

	def test_one_item_larger_than_the_budget_falls_back_to_characters(self):
		data = _answer(_text(json.dumps([{"id": 1, "body": "z" * 90_000}, {"id": 2}])))
		out, cuts = bound_tool_result(data, BUDGET)
		self.assertEqual(cuts[0]["unit"], "characters")
		self.assertLessEqual(_size(out), BUDGET)

	def test_shape_ok_and_iserror_survive(self):
		data = _answer(_text("e" * 100_000), isError=True)
		out, _cuts = bound_tool_result(data, BUDGET)
		self.assertIs(out["ok"], True)
		self.assertIs(out["result"]["isError"], True)
		self.assertEqual(out["result"]["content"][0]["type"], "text")
		self.assertEqual(list(out)[-1], "result")

	def test_the_input_is_not_mutated(self):
		data = _answer(_text(json.dumps(_issues())))
		before = json.dumps(data)
		bound_tool_result(data, BUDGET)
		self.assertEqual(json.dumps(data), before)

	def test_structured_content_that_repeats_the_text_is_dropped_without_a_cut(self):
		blob = {"issues": _issues(70, 300)}
		data = _answer(_text(json.dumps(blob)), structuredContent=blob)
		self.assertGreater(_size(data), BUDGET)
		out, cuts = bound_tool_result(data, BUDGET)
		self.assertEqual(cuts, [])
		self.assertNotIn("structuredContent", out["result"])
		self.assertNotIn("_truncated", out)
		self.assertEqual(json.loads(out["result"]["content"][0]["text"]), blob)

	def test_structured_content_alone_is_cut_by_items(self):
		data = _answer(_text("done"), structuredContent={"issues": _issues(), "next": "c1"})
		out, cuts = bound_tool_result(data, BUDGET)
		structured = out["result"]["structuredContent"]
		self.assertEqual(structured["next"], "c1")
		self.assertEqual(len(structured["issues"]), cuts[0]["shown"])
		self.assertEqual(cuts[0]["total"], 500)
		self.assertEqual(out["result"]["content"][0]["text"], "done")
		self.assertLessEqual(_size(out), BUDGET)

	def test_structured_content_without_a_list_is_dropped_and_said(self):
		data = _answer(_text("done"), structuredContent={"blob": "q" * 80_000})
		out, cuts = bound_tool_result(data, BUDGET)
		self.assertNotIn("structuredContent", out["result"])
		self.assertEqual((cuts[0]["shown"], cuts[0]["where"]), (0, "structured data"))
		self.assertLessEqual(_size(out), BUDGET)

	def test_a_big_image_item_becomes_a_text_line(self):
		data = _answer(_text("chart"), {"type": "image", "data": "A" * 90_000, "mimeType": "image/png"})
		out, _cuts = bound_tool_result(data, BUDGET)
		self.assertEqual(out["result"]["content"][0]["text"], "chart")
		self.assertEqual(out["result"]["content"][1]["text"], "[image omitted: too large]")
		self.assertLessEqual(_size(out), BUDGET)

	def test_two_big_text_items_are_both_bounded(self):
		data = _answer(_text("a" * 60_000), _text("b" * 60_000))
		out, cuts = bound_tool_result(data, BUDGET)
		self.assertLessEqual(_size(out), BUDGET)
		self.assertGreaterEqual(len(cuts), 1)

	def test_bulk_outside_content_still_gets_bounded(self):
		data = _answer(_text("hi"), extra="q" * 90_000, isError=False)
		out, cuts = bound_tool_result(data, BUDGET)
		self.assertLessEqual(_size(out), BUDGET)
		self.assertTrue(out["_truncated"])
		self.assertEqual(cuts[0]["shown"], 0)
		self.assertIs(out["result"]["isError"], False)

	def test_other_shapes_are_returned_unchanged(self):
		for data in ({"ok": False, "error": {"code": "x", "message": "m" * 90_000}}, ["x" * 90_000], None):
			out, cuts = bound_tool_result(data, BUDGET)
			self.assertIs(out, data)
			self.assertIsNone(cuts)
