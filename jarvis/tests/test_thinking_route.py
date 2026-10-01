"""Thinking level vs the LLM proxy route (Aerele-RnD/jarvis-admin-v2#648).

The proxy's models carry no ``reasoning`` flag, so the agent accepts only "off"
there and answers any other explicit level with an error reply instead of running
the turn. These tests pin which turns drop the level and which tenants are offered
an effort choice at all.

Mock-only: nothing here reads or writes the site's Jarvis Settings, so the module is
safe on a live local site.
"""

import inspect
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from jarvis.chat import prepare, turn_handler

CLAUDE_PLAN_MODEL = "claude-opus-5"
PROXY_MODEL = "gpt-5.6-terra"


def _turn_thinking(level="medium", *, proxy=True, pinned=("", None), primary=PROXY_MODEL):
	"""Run ``_turn_thinking`` for one conversation on a faked tenant."""
	conv = SimpleNamespace(thinking_override=level)
	with (
		patch("frappe.get_cached_doc", return_value=SimpleNamespace(proxy_active=1 if proxy else 0)),
		patch.object(turn_handler, "_resolve_model_and_provider", return_value=pinned),
		patch.object(turn_handler, "pool_primary_model", return_value=primary),
		patch.object(
			turn_handler, "_is_native_claude_pick", side_effect=lambda _s, model: model == CLAUDE_PLAN_MODEL
		),
	):
		return turn_handler._turn_thinking(conv)


class TestTurnThinking(TestCase):
	def test_no_proxy_keeps_the_level(self):
		self.assertEqual(_turn_thinking(" high ", proxy=False), "high")

	def test_unset_level_stays_unset(self):
		self.assertIsNone(_turn_thinking(""))
		self.assertIsNone(_turn_thinking(None, proxy=False))

	def test_proxy_unpinned_drops_the_level(self):
		self.assertIsNone(_turn_thinking("medium"))

	def test_proxy_non_claude_pin_drops_the_level(self):
		self.assertIsNone(_turn_thinking("high", pinned=(PROXY_MODEL, None)))

	def test_proxy_claude_plan_pin_keeps_the_level(self):
		pinned = (CLAUDE_PLAN_MODEL, turn_handler.NATIVE_CLAUDE_PROVIDER)
		self.assertEqual(_turn_thinking("high", pinned=pinned), "high")

	def test_proxy_unpinned_with_claude_plan_primary_keeps_the_level(self):
		self.assertEqual(_turn_thinking("low", primary=CLAUDE_PLAN_MODEL), "low")


class TestOfferedThinkingLevels(TestCase):
	def _levels(self, *, proxy, claude_plan):
		settings = SimpleNamespace(proxy_active=1 if proxy else 0)
		with patch.object(turn_handler, "has_native_claude_subscription", return_value=claude_plan):
			return turn_handler.offered_thinking_levels(settings)

	def test_direct_tenant_gets_every_level(self):
		self.assertEqual(self._levels(proxy=False, claude_plan=False), ["low", "medium", "high"])

	def test_proxy_only_tenant_gets_none(self):
		self.assertEqual(self._levels(proxy=True, claude_plan=False), [])

	def test_proxy_tenant_with_claude_plan_keeps_levels(self):
		self.assertEqual(self._levels(proxy=True, claude_plan=True), ["low", "medium", "high"])


class TestDispatchSitesUseTurnThinking(TestCase):
	"""Both chat.send call paths must forward the guarded level, never the raw field."""

	def test_prepare_payload(self):
		src = inspect.getsource(prepare)
		self.assertIn('"thinking": turn_handler._turn_thinking(conv)', src)
		self.assertNotIn("conv.thinking_override", src)

	def test_handle_chat_send(self):
		src = inspect.getsource(turn_handler.handle_chat_send)
		self.assertIn("thinking=_turn_thinking(conv)", src)
		self.assertNotIn("conv.thinking_override", src)
