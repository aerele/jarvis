"""Tests for jarvis.chat.session_pin_sweep.

The sweep classifies every PINNED gateway session - one whose resolved model
differs from the agent default the same sessions.list response reports - against
``Jarvis Conversation.model_override``, the bench's record of what the customer
actually chose. Empty means Auto, so a pin on such a conversation is one the
bench wrote itself and gets cleared; a matching explicit pick is left alone.

The gateway is reached only through OpenclawSession, so these patch it with a
fake exposing ``list_sessions_page`` / ``clear_session_model``. Conversations are
real rows on the test site.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import session_pin_sweep
from jarvis.chat.session_pin_sweep import (
	CLEAR,
	KEEP,
	REPORT,
	SKIP,
	SessionPinSweep,
	_is_agent_main_key,
	_model_ref,
)

CONV = "Jarvis Conversation"
SETTINGS = "Jarvis Settings"

# Every conversation fixture in this file keys off this prefix, so setUp can
# clear residue left by an interrupted run without touching real rows.
KEY_PREFIX = "agent:main:dashboard:pin-"

DEFAULT_PROVIDER = "anthropic-0"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULTS = {"modelProvider": DEFAULT_PROVIDER, "model": DEFAULT_MODEL}


def _row(key, *, provider=DEFAULT_PROVIDER, model=DEFAULT_MODEL, **extra):
	"""One sessions.list row. Defaults to an UNPINNED session (resolves to the
	agent default); pass provider/model to pin it."""
	row = {
		"key": key,
		"modelProvider": provider,
		"model": model,
		"sessionId": f"sid-{key[-8:]}",
		"label": None,
		"hasActiveRun": False,
	}
	row.update(extra)
	return row


def _page(rows, *, defaults=None, has_more=False, next_offset=None):
	return {
		"sessions": rows,
		"defaults": DEFAULTS if defaults is None else defaults,
		"hasMore": has_more,
		"nextOffset": next_offset,
	}


def _fake_sess(*pages):
	sess = MagicMock()
	sess.list_sessions_page.side_effect = list(pages)
	sess.clear_session_model.return_value = {}
	return sess


class TestSessionPinSweep(FrappeTestCase):
	def setUp(self):
		self._made = []
		# session_key is unique on the DocType and _conv commits, so a run killed
		# mid-test would leave a row that 1062s the next run's insert. Sweep any
		# residue from this file's own key namespace first.
		frappe.db.delete(CONV, {"session_key": ["like", f"{KEY_PREFIX}%"]})
		frappe.db.commit()

	def tearDown(self):
		for name in self._made:
			frappe.db.delete(CONV, {"name": name})
		frappe.db.commit()

	def _conv(self, session_key, model_override=None):
		doc = frappe.get_doc(
			{"doctype": CONV, "title": f"pin-{session_key[-8:]}", "status": "Active"}
		).insert(ignore_permissions=True)
		self._made.append(doc.name)
		frappe.db.set_value(CONV, doc.name, {"session_key": session_key, "model_override": model_override})
		frappe.db.commit()
		return doc.name

	def _sweep(self, sess, **kw):
		return SessionPinSweep(sess, **kw).run()

	def _verbs(self, summary):
		return {item.key: item.verb for item in summary.plan}

	# -- classification ---------------------------------------------------- #

	def test_auto_conversation_with_a_pin_is_cleared(self):
		key = "agent:main:dashboard:pin-auto-1"
		self._conv(key, model_override="")
		sess = _fake_sess(_page([_row(key, provider="openai_compat-0", model="claude-sonnet-5")]))
		summary = self._sweep(sess)
		self.assertEqual(self._verbs(summary), {key: CLEAR})
		self.assertEqual(summary.plan[0].pinned, "openai_compat-0/claude-sonnet-5")

	def test_null_model_override_reads_as_auto(self):
		key = "agent:main:dashboard:pin-auto-2"
		self._conv(key, model_override=None)
		sess = _fake_sess(_page([_row(key, provider="zai_coding-0", model="claude-sonnet-4-6")]))
		self.assertEqual(self._verbs(self._sweep(sess)), {key: CLEAR})

	def test_legacy_pool_placeholder_pin_is_cleared(self):
		key = "agent:main:dashboard:pin-legacy"
		self._conv(key, model_override="")
		sess = _fake_sess(_page([_row(key, provider="openai_compat-0", model="jarvis-pool")]))
		self.assertEqual(self._verbs(self._sweep(sess)), {key: CLEAR})

	def test_explicit_customer_pick_is_kept(self):
		key = "agent:main:dashboard:pin-pick"
		self._conv(key, model_override="gemini-3.6-flash")
		sess = _fake_sess(_page([_row(key, provider="gemini-1", model="gemini-3.6-flash")]))
		summary = self._sweep(sess, apply=True)
		self.assertEqual(self._verbs(summary), {key: KEEP})
		sess.clear_session_model.assert_not_called()

	def test_pick_mismatch_is_reported_not_touched(self):
		key = "agent:main:dashboard:pin-drift"
		self._conv(key, model_override="gemini-3.6-flash")
		sess = _fake_sess(_page([_row(key, provider="zai_coding-0", model="claude-sonnet-4-6")]))
		summary = self._sweep(sess, apply=True)
		self.assertEqual(self._verbs(summary), {key: REPORT})
		sess.clear_session_model.assert_not_called()

	def test_a_pick_the_tenant_no_longer_offers_still_holds(self):
		# The customer's model pill still reads this row, so the sweep does not
		# unpin the container behind it; repairing a dead pick is the
		# conversation's business.
		key = "agent:main:dashboard:pin-dead"
		self._conv(key, model_override="a-model-nobody-serves-now")
		sess = _fake_sess(_page([_row(key, provider="vllm", model="claude-sonnet-4-6")]))
		summary = self._sweep(sess, apply=True)
		self.assertEqual(self._verbs(summary), {key: REPORT})
		sess.clear_session_model.assert_not_called()

	def test_unpinned_sessions_are_not_planned(self):
		key = "agent:main:dashboard:pin-clean"
		self._conv(key, model_override="")
		sess = _fake_sess(_page([_row(key)]))
		summary = self._sweep(sess)
		self.assertEqual(summary.plan, [])
		self.assertEqual(summary.scanned, 1)

	def test_bench_throwaway_is_skipped(self):
		# Single-use and reaped by session_lifecycle within the hour; patching one
		# would only bump the updatedAt that reaper grades on.
		key = "agent:main:dashboard:pin-title"
		row = _row(key, provider="openai_compat-0", model="jarvis-pool", label="jarvis-title-abc123")
		sess = _fake_sess(_page([row]))
		summary = self._sweep(sess, apply=True)
		self.assertEqual(self._verbs(summary), {key: SKIP})
		sess.clear_session_model.assert_not_called()

	def test_agent_main_and_heartbeat_are_cleared(self):
		rows = [
			_row("agent:main:main", provider="openai_compat-0", model="jarvis-pool"),
			_row("agent:main:main:heartbeat", provider="openai_compat-0", model="jarvis-pool"),
		]
		verbs = self._verbs(self._sweep(_fake_sess(_page(rows))))
		self.assertEqual(set(verbs.values()), {CLEAR})
		self.assertEqual(len(verbs), 2)

	def test_unattributable_orphan_is_reported_not_touched(self):
		key = "agent:main:dashboard:pin-orphan"
		row = _row(key, provider="zai_coding-0", model="claude-sonnet-4-6", label="somebody-else")
		sess = _fake_sess(_page([row]))
		summary = self._sweep(sess, apply=True)
		self.assertEqual(self._verbs(summary), {key: REPORT})
		sess.clear_session_model.assert_not_called()

	def test_in_flight_run_is_skipped(self):
		key = "agent:main:dashboard:pin-busy"
		self._conv(key, model_override="")
		row = _row(key, provider="openai_compat-0", model="jarvis-pool", hasActiveRun=True)
		sess = _fake_sess(_page([row]))
		summary = self._sweep(sess, apply=True)
		self.assertEqual(self._verbs(summary), {key: SKIP})
		sess.clear_session_model.assert_not_called()

	def test_entry_without_session_id_is_skipped(self):
		# Patching one makes openclaw mint a fresh sessionId and drop the label.
		key = "agent:main:dashboard:pin-nosid"
		self._conv(key, model_override="")
		row = _row(key, provider="openai_compat-0", model="jarvis-pool", sessionId=None)
		sess = _fake_sess(_page([row]))
		summary = self._sweep(sess, apply=True)
		self.assertEqual(self._verbs(summary), {key: SKIP})
		sess.clear_session_model.assert_not_called()

	# -- apply / dry run --------------------------------------------------- #

	def test_dry_run_issues_no_rpc(self):
		key = "agent:main:dashboard:pin-dry"
		self._conv(key, model_override="")
		sess = _fake_sess(_page([_row(key, provider="openai_compat-0", model="jarvis-pool")]))
		summary = self._sweep(sess)
		self.assertEqual(summary.cleared, 0)
		self.assertFalse(summary.apply)
		sess.clear_session_model.assert_not_called()

	def test_apply_clears_only_the_clear_plans(self):
		auto = "agent:main:dashboard:pin-a1"
		pick = "agent:main:dashboard:pin-a2"
		self._conv(auto, model_override="")
		self._conv(pick, model_override="gemini-3.6-flash")
		rows = [
			_row(auto, provider="openai_compat-0", model="jarvis-pool"),
			_row(pick, provider="gemini-1", model="gemini-3.6-flash"),
		]
		sess = _fake_sess(_page(rows))
		summary = self._sweep(sess, apply=True)
		self.assertEqual(summary.cleared, 1)
		sess.clear_session_model.assert_called_once_with(auto)

	def test_max_clear_caps_the_run(self):
		keys = []
		for i in range(3):
			key = f"agent:main:dashboard:pin-cap{i}"
			self._conv(key, model_override="")
			keys.append(key)
		rows = [_row(k, provider="openai_compat-0", model="jarvis-pool") for k in keys]
		sess = _fake_sess(_page(rows))
		summary = self._sweep(sess, apply=True, max_clear=2)
		self.assertEqual(summary.cleared, 2)
		self.assertEqual(summary.capped, 1)
		self.assertEqual(sess.clear_session_model.call_count, 2)

	def test_a_surviving_override_counts_as_an_error(self):
		key = "agent:main:dashboard:pin-stuck"
		self._conv(key, model_override="")
		sess = _fake_sess(_page([_row(key, provider="openai_compat-0", model="jarvis-pool")]))
		sess.clear_session_model.return_value = {
			"modelOverride": "jarvis-pool",
			"modelOverrideSource": "user",
		}
		with patch("frappe.log_error"):
			summary = self._sweep(sess, apply=True)
		self.assertEqual(summary.cleared, 0)
		self.assertEqual(summary.errors, 1)

	def test_one_failed_clear_does_not_stop_the_sweep(self):
		keys = []
		for i in range(2):
			key = f"agent:main:dashboard:pin-err{i}"
			self._conv(key, model_override="")
			keys.append(key)
		rows = [_row(k, provider="openai_compat-0", model="jarvis-pool") for k in keys]
		sess = _fake_sess(_page(rows))
		sess.clear_session_model.side_effect = [RuntimeError("nope"), {}]
		with patch("frappe.log_error"):
			summary = self._sweep(sess, apply=True)
		self.assertEqual(summary.cleared, 1)
		self.assertEqual(summary.errors, 1)
		self.assertEqual(sess.clear_session_model.call_count, 2)

	# -- transport --------------------------------------------------------- #

	def test_missing_defaults_aborts_before_any_clear(self):
		key = "agent:main:dashboard:pin-nodef"
		self._conv(key, model_override="")
		sess = _fake_sess(_page([_row(key, provider="openai_compat-0", model="jarvis-pool")], defaults={}))
		summary = self._sweep(sess, apply=True)
		self.assertEqual(summary.aborted, "gateway reported no default model")
		self.assertEqual(summary.plan, [])
		sess.clear_session_model.assert_not_called()

	def test_pages_are_followed(self):
		first = "agent:main:dashboard:pin-p1"
		second = "agent:main:dashboard:pin-p2"
		self._conv(first, model_override="")
		self._conv(second, model_override="")
		sess = _fake_sess(
			_page(
				[_row(first, provider="openai_compat-0", model="jarvis-pool")],
				has_more=True,
				next_offset=1,
			),
			_page([_row(second, provider="openai_compat-0", model="jarvis-pool")]),
		)
		summary = self._sweep(sess)
		self.assertEqual(summary.scanned, 2)
		self.assertEqual(set(self._verbs(summary)), {first, second})
		self.assertEqual(sess.list_sessions_page.call_count, 2)

	def test_a_stalled_cursor_does_not_loop(self):
		key = "agent:main:dashboard:pin-stall"
		sess = _fake_sess(_page([_row(key)], has_more=True, next_offset=0))
		self.assertEqual(self._sweep(sess).scanned, 1)
		self.assertEqual(sess.list_sessions_page.call_count, 1)

	def test_list_failure_reports_what_it_has(self):
		key = "agent:main:dashboard:pin-half"
		self._conv(key, model_override="")
		sess = MagicMock()
		sess.list_sessions_page.side_effect = [
			_page(
				[_row(key, provider="openai_compat-0", model="jarvis-pool")],
				has_more=True,
				next_offset=1,
			),
			RuntimeError("gateway went away"),
		]
		with patch("frappe.log_error"):
			summary = self._sweep(sess)
		self.assertEqual(self._verbs(summary), {key: CLEAR})

	# -- entry point ------------------------------------------------------- #

	def test_run_defaults_to_dry_run(self):
		key = "agent:main:dashboard:pin-entry"
		self._conv(key, model_override="")
		sess = _fake_sess(_page([_row(key, provider="openai_compat-0", model="jarvis-pool")]))
		orig = frappe.db.get_single_value(SETTINGS, "agent_url")
		frappe.db.set_single_value(SETTINGS, "agent_url", "http://gw.test:18789")
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		try:
			with patch("jarvis.chat.openclaw_client.OpenclawSession.connect", return_value=sess):
				out = session_pin_sweep.run()
		finally:
			frappe.db.set_single_value(SETTINGS, "agent_url", orig or "")
			frappe.clear_document_cache(SETTINGS, SETTINGS)
			frappe.db.commit()
		self.assertFalse(out["apply"])
		self.assertEqual(out["cleared"], 0)
		self.assertEqual(out["counts"][CLEAR], 1)
		sess.clear_session_model.assert_not_called()
		sess.close.assert_called_once()

	def test_run_without_agent_url_aborts(self):
		orig = frappe.db.get_single_value(SETTINGS, "agent_url")
		frappe.db.set_single_value(SETTINGS, "agent_url", "")
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		try:
			out = session_pin_sweep.run()
		finally:
			frappe.db.set_single_value(SETTINGS, "agent_url", orig or "")
			frappe.clear_document_cache(SETTINGS, SETTINGS)
			frappe.db.commit()
		self.assertEqual(out["aborted"], "no agent_url")

	# -- races between the listing and the patch --------------------------- #

	def test_a_pick_made_after_planning_is_not_reverted(self):
		# The plan is built while the conversation is on Auto; the customer picks
		# a model before the patch goes out. The recheck re-reads the row, so the
		# fresh pick must survive.
		key = "agent:main:dashboard:pin-race"
		name = self._conv(key, model_override="")
		sess = _fake_sess(_page([_row(key, provider="openai_compat-0", model="jarvis-pool")]))
		sess.clear_session_model.side_effect = AssertionError("must not revert a fresh pick")
		real_get_value = frappe.db.get_value
		landed = []

		def pick_lands_first(*args, **kwargs):
			# Fires once, on the recheck's own read, and only then: set_value may
			# itself read through the patched get_value, so guard against reentry.
			if not landed and args[:2] == (CONV, name):
				landed.append(True)
				frappe.db.set_value(CONV, name, "model_override", "gemini-3.6-flash")
				frappe.db.commit()
			return real_get_value(*args, **kwargs)

		with patch("frappe.db.get_value", side_effect=pick_lands_first):
			summary = self._sweep(sess, apply=True)
		self.assertEqual(summary.cleared, 0)
		self.assertEqual(summary.raced, 1)
		self.assertEqual(summary.errors, 0)
		self.assertEqual(frappe.db.get_value(CONV, name, "model_override"), "gemini-3.6-flash")

	def test_a_replaced_session_is_an_error_not_a_fix(self):
		# The lifecycle sweep freed the session mid-run, so openclaw minted a
		# ghost entry under the key instead of patching the planned one.
		key = "agent:main:dashboard:pin-ghost"
		self._conv(key, model_override="")
		sess = _fake_sess(_page([_row(key, provider="openai_compat-0", model="jarvis-pool")]))
		sess.clear_session_model.return_value = {"sessionId": "some-other-session"}
		with patch("frappe.log_error"):
			summary = self._sweep(sess, apply=True)
		self.assertEqual(summary.cleared, 0)
		self.assertEqual(summary.errors, 1)

	def test_a_matching_session_id_verifies(self):
		key = "agent:main:dashboard:pin-samesid"
		self._conv(key, model_override="")
		row = _row(key, provider="openai_compat-0", model="jarvis-pool")
		sess = _fake_sess(_page([row]))
		sess.clear_session_model.return_value = {"sessionId": row["sessionId"]}
		summary = self._sweep(sess, apply=True)
		self.assertEqual(summary.cleared, 1)
		self.assertEqual(summary.errors, 0)

	def test_a_row_repeated_across_pages_is_cleared_once(self):
		key = "agent:main:dashboard:pin-dup"
		self._conv(key, model_override="")
		row = _row(key, provider="openai_compat-0", model="jarvis-pool")
		sess = _fake_sess(_page([row], has_more=True, next_offset=1), _page([row]))
		summary = self._sweep(sess, apply=True)
		self.assertEqual(summary.scanned, 1)
		self.assertEqual(sess.clear_session_model.call_count, 1)

	def test_an_endless_cursor_stops_at_max_pages(self):
		pages = [
			_page([_row(f"agent:main:dashboard:pin-endless{i}")], has_more=True, next_offset=i + 1)
			for i in range(session_pin_sweep.MAX_PAGES + 5)
		]
		sess = _fake_sess(*pages)
		self._sweep(sess)
		self.assertEqual(sess.list_sessions_page.call_count, session_pin_sweep.MAX_PAGES)

	# -- helpers ----------------------------------------------------------- #

	def test_model_ref_shapes(self):
		self.assertEqual(
			_model_ref({"modelProvider": "Anthropic-0", "model": "Claude-Sonnet-5"}),
			"anthropic-0/claude-sonnet-5",
		)
		self.assertEqual(_model_ref({"model": "claude-sonnet-5"}), "claude-sonnet-5")
		self.assertEqual(_model_ref({"modelProvider": "anthropic-0"}), "")
		self.assertEqual(_model_ref({}), "")

	def test_agent_main_key_shapes(self):
		for key in (
			"agent:main:main",
			"agent:main:main:heartbeat",
			"agent:main:main:anything-openclaw-adds",
			"agent:other:main",
		):
			self.assertTrue(_is_agent_main_key(key), key)
		for key in (
			"agent:",
			"agent:main",
			"agent:main:maintenance",
			"agent:main:dashboard:abc",
			"mainly:main",
			"",
		):
			self.assertFalse(_is_agent_main_key(key), key)
