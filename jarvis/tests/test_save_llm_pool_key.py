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
