"""Thinking level on every route (Aerele-RnD/jarvis-admin-v2#648 follow-up).

The fleet agent renders every model, proxy models included, so it accepts low,
medium and high everywhere and treats a turn with no level as medium. These tests
pin that the stored level is always forwarded (never dropped by route) and that
every tenant is offered all three levels.

Mock-only: nothing here reads or writes the site's Jarvis Settings, so the module is
safe on a live local site.
"""

import inspect
from types import SimpleNamespace
from unittest import TestCase

from jarvis.chat import prepare, turn_handler


class TestTurnThinking(TestCase):
	def test_stored_level_is_stripped_and_forwarded_unset_sends_none(self):
		for stored, sent in (
			(" high ", "high"),
			("low", "low"),
			("medium", "medium"),
			("", None),
			(None, None),
		):
			with self.subTest(stored=stored):
				conv = SimpleNamespace(thinking_override=stored)
				self.assertEqual(turn_handler._turn_thinking(conv), sent)


class TestOfferedThinkingLevels(TestCase):
	def test_every_tenant_gets_every_level(self):
		for proxy_active in (0, 1):
			with self.subTest(proxy_active=proxy_active):
				settings = SimpleNamespace(proxy_active=proxy_active)
				self.assertEqual(turn_handler.offered_thinking_levels(settings), ["low", "medium", "high"])


class TestDispatchSitesUseTurnThinking(TestCase):
	"""Both chat.send call paths must forward ``_turn_thinking``, never the raw field."""

	def test_prepare_payload(self):
		src = inspect.getsource(prepare)
		self.assertIn('"thinking": turn_handler._turn_thinking(conv)', src)
		self.assertNotIn("conv.thinking_override", src)

	def test_handle_chat_send(self):
		src = inspect.getsource(turn_handler.handle_chat_send)
		self.assertIn("thinking=_turn_thinking(conv)", src)
		self.assertNotIn("conv.thinking_override", src)
