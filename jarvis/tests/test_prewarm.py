# app/jarvis/tests/test_prewarm.py
import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import session_lifecycle
from jarvis.chat.openclaw_client import OpenclawSession


def _lapse_cooldown(prewarm):
	"""Put the bench where the NEXT warm really finds it: cooldown expired, and
	the remembered predecessor fired a cooldown ago.

	Both halves matter. Clearing the cooldown key alone would leave a pointer
	stamped microseconds ago, and reclaim_throwaway_session deliberately refuses
	to delete a session whose run may not have started yet (issue #535) - so such
	a test would be asserting the guard, not the reclaim. Backdating the stamp is
	what the four-minute cooldown does in production."""
	frappe.cache().delete_value(prewarm._warm_cooldown_key())
	pointer = frappe.cache().get_value(prewarm._warm_last_key())
	if isinstance(pointer, dict):
		frappe.cache().set_value(
			prewarm._warm_last_key(),
			dict(pointer, fired_at=time.time() - 600),
			expires_in_sec=3600,
		)


class TestFireAgent(FrappeTestCase):
	def test_fire_agent_sends_deliver_false_and_returns_runid(self):
		sess = OpenclawSession.__new__(OpenclawSession)  # bypass __init__/WS
		captured = {}

		def fake_request(method, params, *, timeout_s):
			captured["method"], captured["params"] = method, params
			return {"ok": True, "payload": {"runId": "run123"}}

		sess._request = fake_request
		rid = sess.fire_agent("sk_1", "/think off warmup", "idem1")

		self.assertEqual(rid, "run123")
		self.assertEqual(captured["method"], "agent")
		self.assertEqual(captured["params"]["deliver"], False)
		self.assertEqual(captured["params"]["sessionKey"], "sk_1")
		self.assertEqual(captured["params"]["message"], "/think off warmup")


class TestWarmPrefix(FrappeTestCase):
	def _settings_stub(self):
		s = MagicMock()
		s.agent_url = "https://gw.example"
		s.llm_model = "gpt-5.5"
		s.llm_auth_mode = "api_key"
		s.llm_provider = "OpenAI"
		return s

	def tearDown(self):
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		# Leaving this set would make the NEXT test's first warm reclaim a
		# session this one invented.
		frappe.cache().delete_value(prewarm._warm_last_key())

	def test_warm_prefix_throwaway_session_no_rows_best_effort(self):
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		fake_sess = MagicMock()
		fake_sess.create_session.return_value = "sk_throwaway"
		before = frappe.db.count("Jarvis Chat Message")

		with (
			patch("jarvis.chat.prewarm.OpenclawSession") as OC,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=self._settings_stub()),
		):
			OC.connect.return_value = fake_sess
			fired = prewarm.warm_prefix()

		self.assertTrue(fired)
		fake_sess.create_session.assert_called_once()
		msg = fake_sess.fire_agent.call_args.args[1]
		self.assertIn("/think off", msg)
		fake_sess.close.assert_called_once()
		self.assertEqual(frappe.db.count("Jarvis Chat Message"), before)

	def test_warm_prefix_reclaims_previous_throwaway(self):
		"""Each warm deletes its PREDECESSOR's session. fire_agent is
		fire-and-forget, so a warm cannot delete its own without blocking a
		worker until the turn lands; the cooldown usually guarantees the
		predecessor has long since finished. Steady state: one live prewarm
		session, not one per warm (which at a 4-minute cooldown leaked up to
		~350/day). "Usually" is why the reclaim now probes hasActiveRun first -
		see test_throwaway_session_reclaim.py."""
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		frappe.cache().delete_value(prewarm._warm_last_key())
		fake_sess = MagicMock()
		fake_sess.create_session.side_effect = ["sk_first", "sk_second"]
		fake_sess.is_run_active.return_value = False  # predecessor's warm turn is done

		with (
			patch("jarvis.chat.prewarm.OpenclawSession") as OC,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=self._settings_stub()),
			patch.object(session_lifecycle, "RECLAIM_PROBE_DELAY_S", 0),
		):
			OC.connect.return_value = fake_sess
			self.assertTrue(prewarm.warm_prefix())
			# Nothing to reclaim on the very first warm of a bench.
			fake_sess.delete_session.assert_not_called()
			_lapse_cooldown(prewarm)
			self.assertTrue(prewarm.warm_prefix())

		# The second warm reclaimed the first's session — and only that one.
		fake_sess.delete_session.assert_called_once_with("sk_first")
		pointer = frappe.cache().get_value(prewarm._warm_last_key())
		self.assertEqual(pointer["key"], "sk_second")

	def test_warm_prefix_survives_previous_delete_failure(self):
		"""A failed reclaim must never fail the warm — the orphan sweep is the
		backstop — and must still leave the NEW key tracked, or the next warm
		would reclaim nothing and the leak would quietly resume."""
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		frappe.cache().delete_value(prewarm._warm_last_key())
		fake_sess = MagicMock()
		fake_sess.create_session.side_effect = ["sk_first", "sk_second"]
		fake_sess.is_run_active.return_value = False
		fake_sess.delete_session.side_effect = RuntimeError("gateway blip")

		with (
			patch("jarvis.chat.prewarm.OpenclawSession") as OC,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=self._settings_stub()),
			patch.object(session_lifecycle, "RECLAIM_PROBE_DELAY_S", 0),
		):
			OC.connect.return_value = fake_sess
			self.assertTrue(prewarm.warm_prefix())
			_lapse_cooldown(prewarm)
			self.assertTrue(prewarm.warm_prefix())

		self.assertEqual(fake_sess.fire_agent.call_count, 2)
		pointer = frappe.cache().get_value(prewarm._warm_last_key())
		self.assertEqual(pointer["key"], "sk_second")

	def test_warm_prefix_remembers_when_it_fired(self):
		"""The pointer carries a fire time, not just a key. Without it the next
		warm cannot tell a predecessor whose run finished from one whose run has
		not started yet, and #535's guard has nothing to work with."""
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		frappe.cache().delete_value(prewarm._warm_last_key())
		fake_sess = MagicMock()
		fake_sess.create_session.return_value = "sk_only"

		before = time.time()
		with (
			patch("jarvis.chat.prewarm.OpenclawSession") as OC,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=self._settings_stub()),
		):
			OC.connect.return_value = fake_sess
			self.assertTrue(prewarm.warm_prefix())

		pointer = frappe.cache().get_value(prewarm._warm_last_key())
		self.assertEqual(pointer["key"], "sk_only")
		self.assertGreaterEqual(pointer["fired_at"], before)
		self.assertLessEqual(pointer["fired_at"], time.time())

	def test_a_predecessor_fired_moments_ago_is_left_for_the_sweep(self):
		"""THE #535 REGRESSION, at the level that produces it.

		Two warms can pass the get-then-set cooldown check in the same instant
		(observed 6ms apart on jarvis-pool-bf4097), which makes "the predecessor"
		a session whose fire-and-forget turn openclaw has ACCEPTED but not yet
		STARTED. sessions.list answers hasActiveRun=false for that state exactly
		as it does for a finished run - measured median 670ms wide - so the probe
		alone waves the delete through and it lands on a live run.

		Here the cooldown is cleared WITHOUT backdating the pointer, which is
		what that race looks like from prewarm's side.

		The grace is widened for the duration so the assertion cannot depend on
		how long a loaded CI box takes to get from the first warm to the second.
		The real 5s value is plenty in production and asserted directly in
		test_throwaway_session_reclaim.py; what this test is for is the wiring,
		which must not turn into a timing flake."""
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		frappe.cache().delete_value(prewarm._warm_last_key())
		fake_sess = MagicMock()
		fake_sess.create_session.side_effect = ["sk_first", "sk_second"]
		# The gateway cannot see the run yet, so it reports the session as idle.
		fake_sess.is_run_active.return_value = False

		with (
			patch("jarvis.chat.prewarm.OpenclawSession") as OC,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=self._settings_stub()),
			patch.object(session_lifecycle, "RECLAIM_PROBE_DELAY_S", 0),
			patch.object(session_lifecycle, "RUN_START_GRACE_S", 3600),
		):
			OC.connect.return_value = fake_sess
			self.assertTrue(prewarm.warm_prefix())
			frappe.cache().delete_value(prewarm._warm_cooldown_key())
			self.assertTrue(prewarm.warm_prefix())

		fake_sess.delete_session.assert_not_called()
		self.assertEqual(fake_sess.fire_agent.call_count, 2, "both warms must still fire")
		# No leak: the pointer moved on and the sweep collects jarvis-prewarm-*
		# on THROWAWAY_GRACE_HOURS, which is exactly what a skipped reclaim buys.
		self.assertEqual(frappe.cache().get_value(prewarm._warm_last_key())["key"], "sk_second")

	def test_a_pre_535_pointer_still_reclaims(self):
		"""A bare-string pointer written before this deploy can still be in the
		cache under its 24h TTL. It must reclaim as it always did, not crash and
		not silently stop collecting."""
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		frappe.cache().set_value(prewarm._warm_last_key(), "sk_legacy", expires_in_sec=3600)
		fake_sess = MagicMock()
		fake_sess.create_session.return_value = "sk_new"
		fake_sess.is_run_active.return_value = False

		with (
			patch("jarvis.chat.prewarm.OpenclawSession") as OC,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=self._settings_stub()),
			patch.object(session_lifecycle, "RECLAIM_PROBE_DELAY_S", 0),
		):
			OC.connect.return_value = fake_sess
			self.assertTrue(prewarm.warm_prefix())

		fake_sess.delete_session.assert_called_once_with("sk_legacy")

	def test_warm_prefix_debounced_second_call_is_noop(self):
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		with (
			patch("jarvis.chat.prewarm.OpenclawSession") as OC,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=self._settings_stub()),
		):
			OC.connect.return_value = MagicMock(create_session=MagicMock(return_value="sk"))
			self.assertTrue(prewarm.warm_prefix())
			self.assertFalse(prewarm.warm_prefix())  # within cooldown

	def test_warm_prefix_swallows_errors(self):
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		with (
			patch("jarvis.chat.prewarm.frappe.get_single", side_effect=RuntimeError("boom")),
		):
			self.assertFalse(prewarm.warm_prefix())  # no raise

	# --- adversarial tests ---

	def test_warm_prefix_fires_with_empty_agent_token(self):
		"""Warming must succeed even when agent_token is empty.
		connect() uses device-pairing auth, not agent_token - proves FIX A."""
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		stub = self._settings_stub()
		stub.get_password.return_value = ""  # empty agent_token (device-paired bench)
		fake_sess = MagicMock()
		fake_sess.create_session.return_value = "sk_throwaway"

		with (
			patch("jarvis.chat.prewarm.OpenclawSession") as OC,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=stub),
		):
			OC.connect.return_value = fake_sess
			fired = prewarm.warm_prefix()

		self.assertTrue(fired, "warm_prefix must fire even with empty agent_token")

	def test_warm_prefix_failure_does_not_arm_full_cooldown(self):
		"""A failed connect must set only the short in-progress marker,
		not the full cooldown. Proves FIX B: transient failures retry soon
		instead of being disabled for 25 min."""
		from jarvis.chat import prewarm

		cache_mock = MagicMock()
		cache_mock.get_value.return_value = None  # not debounced

		with (
			patch("jarvis.chat.prewarm.OpenclawSession") as OC,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=self._settings_stub()),
			patch("jarvis.chat.prewarm.frappe.cache", return_value=cache_mock),
			patch("jarvis.chat.prewarm.frappe.logger"),
		):
			OC.connect.side_effect = RuntimeError("gateway blip")
			fired = prewarm.warm_prefix()

		self.assertFalse(fired)
		set_calls = cache_mock.set_value.call_args_list
		# Exactly one set_value call: the short in-progress guard.
		# A second call (full cooldown) must not appear on failure.
		self.assertEqual(
			len(set_calls),
			1,
			"Only the short in-progress TTL should be set on failure, not the full cooldown",
		)
		_, call_kwargs = set_calls[0]
		self.assertEqual(
			call_kwargs.get("expires_in_sec"),
			prewarm._WARM_INPROGRESS_S,
			"The single cache set on failure must use the short in-progress TTL",
		)

	def test_warm_prefix_passes_default_model_to_fire_agent(self):
		"""warm_prefix resolves the Settings default model and passes it to
		fire_agent - proves FIX C (prevents container-default model drift)."""
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		fake_sess = MagicMock()
		fake_sess.create_session.return_value = "sk_throwaway"

		with (
			patch("jarvis.chat.prewarm.OpenclawSession") as OC,
			patch("jarvis.chat.prewarm.frappe.get_single", return_value=self._settings_stub()),
		):
			OC.connect.return_value = fake_sess
			prewarm.warm_prefix()

		fake_sess.fire_agent.assert_called_once()
		_, call_kwargs = fake_sess.fire_agent.call_args
		self.assertEqual(
			call_kwargs.get("model"), "gpt-5.5", "fire_agent must receive the Settings default model"
		)
		# api_key mode: no oauth provider
		self.assertIsNone(call_kwargs.get("provider"), "provider must be None in api_key mode")


class TestKeepWarm(FrappeTestCase):
	def tearDown(self):
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		# Leaving this set would make the NEXT test's first warm reclaim a
		# session this one invented.
		frappe.cache().delete_value(prewarm._warm_last_key())

	def test_keep_warm_warms_when_recent_activity(self):
		from jarvis.chat import prewarm

		with (
			patch("jarvis.chat.prewarm.frappe.db.exists", return_value="MSG-1"),
			patch("jarvis.chat.prewarm.warm_prefix") as wp,
		):
			prewarm.keep_warm_if_active()
		wp.assert_called_once()

	def test_keep_warm_noop_when_idle(self):
		from jarvis.chat import prewarm

		with (
			patch("jarvis.chat.prewarm.frappe.db.exists", return_value=None),
			patch("jarvis.chat.prewarm.warm_prefix") as wp,
		):
			prewarm.keep_warm_if_active()
		wp.assert_not_called()


class TestEnqueueWarmIfDue(FrappeTestCase):
	def tearDown(self):
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		# Leaving this set would make the NEXT test's first warm reclaim a
		# session this one invented.
		frappe.cache().delete_value(prewarm._warm_last_key())

	def test_enqueues_warm_when_not_on_cooldown(self):
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		with (
			patch("jarvis.chat.prewarm.frappe.enqueue") as enq,
		):
			prewarm.enqueue_warm_if_due()
		enq.assert_called_once()
		self.assertEqual(enq.call_args.args[0], "jarvis.chat.prewarm.warm_prefix")

	def test_skips_enqueue_when_on_cooldown(self):
		from jarvis.chat import prewarm

		frappe.cache().set_value(prewarm._warm_cooldown_key(), "1", expires_in_sec=60)
		with (
			patch("jarvis.chat.prewarm.frappe.enqueue") as enq,
		):
			prewarm.enqueue_warm_if_due()
		enq.assert_not_called()


class TestListConversationsWarms(FrappeTestCase):
	def tearDown(self):
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		# Leaving this set would make the NEXT test's first warm reclaim a
		# session this one invented.
		frappe.cache().delete_value(prewarm._warm_last_key())

	def test_list_conversations_triggers_warm_on_load(self):
		from jarvis.chat import api

		with patch("jarvis.chat.prewarm.enqueue_warm_if_due") as w:
			api.list_conversations()
		w.assert_called_once()
