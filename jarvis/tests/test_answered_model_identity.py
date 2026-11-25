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
