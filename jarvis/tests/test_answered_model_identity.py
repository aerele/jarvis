"""The model a reply is credited to is the one that answered, not the one that was asked for."""

import unittest

from jarvis.chat import usage

PINNED = {"model": "claude-opus-5", "modelProvider": "anthropic"}


class TestAnsweredModelIdentity(unittest.TestCase):
	def test_requested_equals_answered(self):
		self.assertEqual(usage.resolved_model_identity(PINNED), ("claude-opus-5", "anthropic"))

	def test_a_fallback_answered_by_another_member_is_credited_to_it(self):
		row = {**PINNED, "activeModel": "gpt-5.6-terra", "activeModelProvider": "openai"}
		self.assertEqual(usage.resolved_model_identity(row), ("gpt-5.6-terra", "openai"))

	def test_a_sticky_fallback_on_a_later_turn_keeps_naming_the_active_model(self):
		row = {**PINNED, "activeModel": "gpt-5.6-terra", "activeModelProvider": "openai", "lastRunId": "r2"}
		self.assertEqual(usage.resolved_model_identity(row)[0], "gpt-5.6-terra")

	def test_a_recovered_model_clears_the_fallback_and_the_row_model_is_used_again(self):
		self.assertEqual(
			usage.resolved_model_identity({**PINNED, "activeModel": None}), ("claude-opus-5", "anthropic")
		)

	def test_an_old_runtime_row_without_the_fields_is_unchanged(self):
		self.assertEqual(
			usage.resolved_model_identity({"model": "gpt-5", "modelProvider": "openai"}), ("gpt-5", "openai")
		)
		self.assertEqual(usage.resolved_model_identity(None), ("", ""))

	def test_runtime_provider_ids_map_back_to_the_apps_ids(self):
		row = {**PINNED, "activeModel": "deepseek-chat", "activeModelProvider": "deepseek_api"}
		self.assertEqual(usage.resolved_model_identity(row), ("deepseek-chat", "deepseek"))

	def test_a_model_without_a_provider_does_not_pair_with_the_pinned_provider(self):
		row = {**PINNED, "activeModel": "gpt-5.6-terra"}
		self.assertEqual(usage.resolved_model_identity(row), ("claude-opus-5", "anthropic"))


def _msg(seq, model, provider, *, mirror=False, stop="stop", run="r"):
	"""A gateway transcript entry in the 2026.11.7 shape (captured live, sanitised)."""
	m = {
		"role": "assistant",
		"model": model,
		"provider": provider,
		"api": "openclaw-transcript" if mirror else "openai-chatgpt-responses",
		"stopReason": stop,
		"__openclaw": {"seq": seq, "runId": run},
	}
	if mirror:
		m["openclawDeliveryMirror"] = True
	else:
		m["responseModel"] = model
	return m


class TestAnsweredFromTheTranscript(unittest.TestCase):
	def setUp(self):
		from unittest.mock import patch

		import frappe

		p = patch(
			"jarvis.chat.runtime_profile.get_profile",
			return_value=frappe._dict(message_metadata_key="__openclaw"),
		)
		p.start()
		self.addCleanup(p.stop)

	def test_the_first_fallback_turn_is_credited_to_the_member_that_answered(self):
		# Asked for claude-opus-5 (the row still says so); ChatGPT answered at seq 2, mirrored at 3.
		msgs = [_msg(2, "gpt-5.6-terra", "openai"), _msg(3, "delivery-mirror", "openclaw", mirror=True)]
		ident = usage.run_answering_identity(msgs, 0, None)
		self.assertEqual(ident, {"answeredModel": "gpt-5.6-terra", "answeredModelProvider": "openai"})
		row = usage.with_run_identity(PINNED, ident)
		self.assertEqual(usage.resolved_model_identity(row), ("gpt-5.6-terra", "openai"))

	def test_a_sticky_fallback_turn_uses_the_last_real_answer_of_its_own_window(self):
		msgs = [
			_msg(2, "gpt-5.6-terra", "openai"),
			_msg(3, "delivery-mirror", "openclaw", mirror=True),
			_msg(5, "gpt-5.6-terra", "openai", stop="toolUse"),
			_msg(7, "gpt-5.6-terra", "openai"),
			_msg(8, "delivery-mirror", "openclaw", mirror=True),
		]
		self.assertEqual(usage.run_answering_identity(msgs, 3, None)["answeredModel"], "gpt-5.6-terra")

	def test_the_window_keeps_another_turns_answer_out(self):
		msgs = [_msg(2, "claude-opus-5", "anthropic"), _msg(5, "gpt-5.6-terra", "openai")]
		self.assertEqual(usage.run_answering_identity(msgs, 0, 3)["answeredModel"], "claude-opus-5")
		self.assertEqual(usage.run_answering_identity(msgs, 3, None)["answeredModel"], "gpt-5.6-terra")

	def test_requested_equals_answered_and_no_usable_entry_leave_the_row_alone(self):
		msgs = [_msg(2, "claude-opus-5", "anthropic")]
		row = usage.with_run_identity(PINNED, usage.run_answering_identity(msgs, 0, None))
		self.assertEqual(usage.resolved_model_identity(row), ("claude-opus-5", "anthropic"))
		self.assertIsNone(
			usage.run_answering_identity([_msg(3, "delivery-mirror", "openclaw", mirror=True)], 0)
		)
		self.assertIsNone(usage.run_answering_identity([{"role": "assistant", "__openclaw": {"seq": 2}}], 0))
		self.assertEqual(usage.with_run_identity(PINNED, None), PINNED)

	def test_the_transcript_answer_beats_the_row_even_with_an_active_fallback_field(self):
		row = {
			**PINNED,
			"activeModel": "x",
			"activeModelProvider": "y",
			"answeredModel": "gpt-5.6-terra",
			"answeredModelProvider": "openai",
		}
		self.assertEqual(usage.resolved_model_identity(row), ("gpt-5.6-terra", "openai"))


NOTICE = "Model switched from anthropic/claude-opus-5 to deepseek-0/deepseek-flash (unknown)."


def _text_msg(seq, model, provider, text, *, mirror=False):
	m = _msg(seq, model, provider, mirror=mirror)
	m["content"] = [{"type": "text", "text": text}]
	return m


class TestRuntimeModelSwitchNotice(unittest.TestCase):
	def setUp(self):
		from unittest.mock import patch

		import frappe

		p = patch(
			"jarvis.chat.runtime_profile.get_profile",
			return_value=frappe._dict(message_metadata_key="__openclaw"),
		)
		p.start()
		self.addCleanup(p.stop)

	def test_the_notice_text_is_recognised_exactly_and_a_real_answer_is_not(self):
		from jarvis.chat.runtime_notices import is_runtime_notice

		self.assertTrue(is_runtime_notice(NOTICE))
		self.assertFalse(is_runtime_notice("pong"))
		self.assertFalse(is_runtime_notice("I read: " + NOTICE))
		self.assertFalse(is_runtime_notice(None))

	def test_fallback_with_a_notice_entry_after_the_real_answer(self):
		# The captured shape: the answer at seq 2 (deepseek), the notice mirrored at seq 3.
		msgs = [
			_text_msg(2, "deepseek-flash", "deepseek-0", "pong"),
			_text_msg(3, "delivery-mirror", "openclaw", NOTICE, mirror=True),
		]
		ident = usage.run_answering_identity(msgs, 0, None)
		self.assertEqual(
			ident,
			{"answeredModel": "deepseek-flash", "answeredModelProvider": "deepseek-0", "answerText": "pong"},
		)

	def test_a_notice_entry_is_never_the_answerer_even_without_the_mirror_flag(self):
		msgs = [_text_msg(2, "deepseek-flash", "deepseek-0", "pong"), _text_msg(3, "x", "y", NOTICE)]
		self.assertEqual(usage.run_answering_identity(msgs, 0, None)["answeredModel"], "deepseek-flash")

	def test_notice_only_leaves_no_identity_so_nothing_is_invented(self):
		self.assertIsNone(
			usage.run_answering_identity([_text_msg(3, "x", "openclaw", NOTICE, mirror=True)], 0)
		)

	def test_no_fallback_keeps_the_answer_text_of_the_asked_model(self):
		ident = usage.run_answering_identity([_text_msg(2, "claude-opus-5", "anthropic", "pong")], 0, None)
		self.assertEqual(ident["answerText"], "pong")

	def test_the_relay_drops_a_notice_final_for_what_streamed(self):
		from unittest.mock import MagicMock

		from jarvis.chat import relay_mux

		lane = MagicMock(stream_text="pong", saw_media_tool_start=False, steps=[], preamble_steps=set())
		lane.loose_preambles = []
		lane.segment_tails = []
		lane.next_seq.return_value = 1
		mux = relay_mux.RelayMux.__new__(relay_mux.RelayMux)
		captured = {}
		mux._offer = lambda ln, ev: captured.update(ev.data)
		mux._bump = lambda *a, **k: None
		payload = {
			"state": "final",
			"message": {
				"role": "assistant",
				"content": [{"type": "text", "text": NOTICE}],
				"stopReason": "stop",
			},
		}
		mux._route_terminal(lane, payload)
		self.assertEqual(captured.get("text"), "pong")
		lane.stream_text = ""
		mux._route_terminal(lane, payload)
		self.assertFalse(captured.get("text"))
