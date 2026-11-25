"""A save carries one idempotency key, so the worker hand-off cannot allocate a second operation."""

import inspect

from frappe.tests.utils import FrappeTestCase


class TestEnsureIdempotencyKey(FrappeTestCase):
	def test_a_blank_key_is_replaced_by_a_fresh_one_each_time(self):
		from jarvis.onboarding import _ensure_idempotency_key

		first, second = _ensure_idempotency_key(""), _ensure_idempotency_key(None)
		self.assertTrue(first.startswith("app_") and second.startswith("app_"))
		self.assertNotEqual(first, second)

	def test_a_caller_key_is_kept_trimmed(self):
		from jarvis.onboarding import _ensure_idempotency_key

		self.assertEqual(_ensure_idempotency_key("  abc "), "abc")

	def test_save_llm_pool_hands_the_same_key_to_the_sync(self):
		from jarvis import onboarding

		src = inspect.getsource(onboarding.save_llm_pool)
		self.assertIn("idempotency_key = _ensure_idempotency_key(idempotency_key)", src)
		self.assertIn("sync_pool_now(idempotency_key=idempotency_key or None", src)


class TestSyncJobsRideTheShortQueue(FrappeTestCase):
	def test_the_non_switch_enqueue_uses_short_with_the_sync_timeout(self):
		from unittest.mock import patch

		from jarvis.jarvis.doctype.jarvis_settings import jarvis_settings as js

		with (
			patch("jarvis.chat.llm_switch.is_active", return_value=False),
			patch.object(js.frappe, "enqueue") as enq,
		):
			js._switch_or_enqueue(object(), "some.job", is_switch=False, job_id="x")
		self.assertEqual(enq.call_args.kwargs["queue"], "short")
		self.assertEqual(enq.call_args.kwargs["timeout"], js.ADMIN_SYNC_RQ_TIMEOUT_S)


class TestRefusedSendIsLogged(FrappeTestCase):
	def test_a_send_rejection_leaves_a_latency_log_line_with_its_reason(self):
		from unittest.mock import MagicMock, patch

		from jarvis.chat import api

		logger = MagicMock()
		with patch("jarvis.chat.latency.get_logger", return_value=logger):
			out = api._send_rejection("u@x", "maintenance")
		self.assertEqual(out, {"ok": False, "reason": "maintenance"})
		self.assertIn("send_rejected", logger.info.call_args.args[0])
		self.assertEqual(logger.info.call_args.args[1], "maintenance")


class TestNativeNeverOpensASwitchHold(FrappeTestCase):
	def _hold(self, native, switch=True, primary=True):
		from unittest.mock import MagicMock, patch

		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import JarvisSettings

		doc = MagicMock()
		doc._is_pool_switch.return_value = switch
		doc._primary_changed.return_value = primary
		with patch("jarvis.native_runtime.is_native_pool", return_value=native):
			return JarvisSettings._needs_switch_hold(doc)

	def test_a_native_tenant_holds_nothing_for_a_reorder_or_a_new_primary(self):
		self.assertFalse(self._hold(True))

	def test_a_proxied_tenant_keeps_the_hold_exactly_as_before(self):
		self.assertTrue(self._hold(False, True, False))
		self.assertTrue(self._hold(False, False, True))
		self.assertFalse(self._hold(False, False, False))

	def test_both_save_paths_ask_the_one_predicate(self):
		import inspect

		from jarvis import onboarding
		from jarvis.jarvis.doctype.jarvis_settings import jarvis_settings as js

		self.assertIn("_needs_switch_hold()", inspect.getsource(onboarding.save_llm_pool))
		self.assertIn(
			"is_switch=self._needs_switch_hold()", inspect.getsource(js.JarvisSettings._enqueue_pool_sync)
		)
