"""One-shot readers (auto-title, suggestions, learning polish) fold assistant frames with
events.accumulate_assistant_text. openclaw 2026.10.x sends ``text`` only on the first frame and the
rest as ``delta`` with ``text=""``; 2026.9.x sends a cumulative ``text`` on every frame."""

import unittest

from jarvis.chat.events import accumulate_assistant_text


def fold(frames):
	text = ""
	for f in frames:
		text = accumulate_assistant_text(text, {"kind": "assistant", **f})
	return text


class TestAccumulateAssistantText(unittest.TestCase):
	def test_2026_9_cumulative_frames(self):
		frames = [
			{"text": "On", "delta": "On"},
			{"text": "On the", "delta": " the"},
			{"text": "On the record", "delta": " record"},
		]
		self.assertEqual(fold(frames), "On the record")

	def test_2026_10_delta_frames_after_the_first(self):
		# The exact shape seen live on 10.6: titles came out as "on" before this helper.
		frames = [
			{"text": "On", "delta": "On"},
			{"text": "", "delta": " the"},
			{"text": "", "delta": " record"},
		]
		self.assertEqual(fold(frames), "On the record")

	def test_a_replace_frame_resets_to_its_text(self):
		frames = [
			{"text": "Draft", "delta": "Draft"},
			{"text": "", "delta": " one"},
			{"text": "Final title", "replace": True},
		]
		self.assertEqual(fold(frames), "Final title")

	def test_empty_frames_change_nothing(self):
		self.assertEqual(fold([{"text": "Hi", "delta": "Hi"}, {"text": "", "delta": ""}, {}]), "Hi")
