"""admin-v2#629: moving a different model to FIRST on a serving pool restarts
the container, so the apply is held like a proxy switch. A same-first reorder or
a model added at the end keeps hot-reloading.

Site-free: the docs are plain namespaces and ``get_doc_before_save`` is faked.
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import JarvisSettings

_JS = "jarvis.jarvis.doctype.jarvis_settings.jarvis_settings"


def _row(model, order, provider="openai", credential_type="api_key", enabled=1):
	return SimpleNamespace(
		provider=provider, model=model, order=order, credential_type=credential_type, enabled=enabled
	)


def _doc(*rows, synced=True, before=None):
	return SimpleNamespace(
		models=list(rows),
		llm_pool_synced_at="2026-10-01 00:00:00" if synced else None,
		llm_direct_synced_at=None,
		get_doc_before_save=lambda: before,
	)


def _changed(before_rows, after_rows, **kw):
	before = _doc(*before_rows, **kw)
	after = _doc(*after_rows, before=before)
	return JarvisSettings._primary_changed(after)


class TestPrimaryChanged(TestCase):
	def test_first_model_change_is_a_primary_change(self):
		self.assertTrue(
			_changed(
				[_row("gpt-5.5", 0), _row("gpt-4o", 1)],
				[_row("gpt-4o", 0), _row("gpt-5.5", 1)],
			)
		)

	def test_same_first_reorder_is_not(self):
		self.assertFalse(
			_changed(
				[_row("a", 0), _row("b", 1), _row("c", 2)],
				[_row("a", 0), _row("c", 1), _row("b", 2)],
			)
		)

	def test_model_added_at_the_end_is_not(self):
		self.assertFalse(_changed([_row("a", 0)], [_row("a", 0), _row("b", 1)]))

	def test_claude_plan_moved_to_first_is_a_primary_change(self):
		plan = lambda order: _row("claude-sonnet-5", order, "anthropic", "subscription")  # noqa: E731
		self.assertTrue(_changed([_row("a", 0), plan(1)], [plan(0), _row("a", 1)]))

	def test_claude_plan_moved_out_of_first_is_a_primary_change(self):
		plan = lambda order: _row("claude-sonnet-5", order, "anthropic", "subscription")  # noqa: E731
		self.assertTrue(_changed([plan(0), _row("a", 1)], [_row("a", 0), plan(1)]))

	def test_claude_plan_and_claude_key_are_not_the_same_primary(self):
		self.assertTrue(
			_changed(
				[_row("claude-sonnet-5", 0, "anthropic", "api_key")],
				[_row("claude-sonnet-5", 0, "anthropic", "subscription")],
			)
		)

	def test_disabled_first_row_is_ignored(self):
		self.assertFalse(
			_changed(
				[_row("x", 0, enabled=0), _row("a", 1)],
				[_row("x", 5, enabled=0), _row("a", 1)],
			)
		)

	def test_never_synced_workspace_is_not_held(self):
		self.assertFalse(_changed([_row("a", 0), _row("b", 1)], [_row("b", 0), _row("a", 1)], synced=False))

	def test_first_ever_save_is_not_held(self):
		self.assertFalse(JarvisSettings._primary_changed(_doc(_row("a", 0), before=None)))


class TestEnqueuePoolSyncRoutesPrimaryChange(TestCase):
	def _is_switch(self, primary_changed, pool_switch):
		fake = SimpleNamespace(_is_pool_switch=lambda: pool_switch, _primary_changed=lambda: primary_changed)
		with (
			patch(f"{_JS}._stamp_pool_pending"),
			patch(f"{_JS}._switch_or_enqueue") as route,
		):
			JarvisSettings._enqueue_pool_sync(fake)
		return route.call_args.kwargs["is_switch"]

	def test_primary_change_alone_holds(self):
		self.assertTrue(self._is_switch(True, False))

	def test_proxy_flip_alone_holds(self):
		self.assertTrue(self._is_switch(False, True))

	def test_neither_does_not_hold(self):
		self.assertFalse(self._is_switch(False, False))
