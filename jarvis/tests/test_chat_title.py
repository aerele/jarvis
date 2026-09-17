"""jarvis.chat.title: a greeting / small-talk turn must NOT seed a chat title (it stays "New chat"
until a real topic lands); a real topic must. Greeting-vs-topic is decided by the LLM (SKIP), so it
works in ANY language; the opening messages are fed together so a topic that FOLLOWS a greeting is
still caught. Covers the LLM SKIP-sentinel + generic-title OUTPUT guard in maybe_autotitle."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import title


class TestGenericTitle(FrappeTestCase):
	def test_generic_meta_titles_rejected(self):
		for t in [
			"Greetings",
			"Greeting",
			"Casual Greeting Conversation",
			"Casual Conversation",
			"Casual Chat",
			"Small Talk",
			"Small-Talk",
			"Chit Chat",
			"Chit-Chat",
			"Hello",
			"Friendly Greeting",
		]:
			self.assertTrue(title._is_generic_title(t), f"expected generic: {t!r}")

	def test_real_titles_kept(self):
		for t in [
			"Paid Leave Balance",
			"GST Invoice Help",
			"Greeting Card Design",  # a real topic that merely starts with "Greeting"
			"Password Reset",
			"Monthly Sales Report",
		]:
			self.assertFalse(title._is_generic_title(t), f"expected NOT generic: {t!r}")


class TestMaybeAutotitle(FrappeTestCase):
	def _run(self, gateway_returns, *, existing="New chat", source="Reset my password", gen=None):
		gen = gen or (lambda *a, **k: gateway_returns)
		with (
			patch.object(title, "_opening_user_messages", return_value=source),
			patch.object(title, "_generate_via_gateway", side_effect=gen),
			patch.object(frappe.db, "get_value", return_value=existing),
			patch.object(frappe.db, "set_value") as setv,
			patch.object(frappe.db, "commit"),
			patch.object(title, "publish_to_user"),
		):
			title.maybe_autotitle("c1", "u1", gateway_url="ws://x", model="m", provider="p")
		return setv

	def test_skip_sentinel_leaves_new_chat(self):
		# the model says "no topic yet" (any language) -> stay "New chat", don't title the greeting
		self._run("SKIP").assert_not_called()

	def test_generic_title_is_rejected(self):
		# the model returned a meta-title instead of SKIP -> reject it as a backstop
		self._run("Casual Greeting Conversation").assert_not_called()

	def test_empty_result_leaves_new_chat(self):
		# a title-model failure (gateway returns "") must NOT deterministically guess a title:
		# the chat stays "New chat" and is retried next turn (no derive_title fallback)
		self._run("").assert_not_called()

	def test_real_title_is_set(self):
		setv = self._run("Password Reset Help")
		setv.assert_called_once()
		self.assertIn("Password Reset Help", setv.call_args[0])

	def test_opening_messages_are_fed_so_a_topic_after_a_greeting_is_titled(self):
		# A greeting followed by a real prompt (here a non-English greeting) is fed AS ONE source, so
		# the model can title the real topic instead of missing it behind the greeting.
		captured = {}

		def gen(gateway_url, source_text, *, model, provider):
			captured["source"] = source_text
			return "Password Reset"

		setv = self._run(None, source="வணக்கம்\nreset my password", gen=gen)
		self.assertIn("reset my password", captured["source"])  # the real message reached the model
		setv.assert_called_once()
		self.assertIn("Password Reset", setv.call_args[0])
