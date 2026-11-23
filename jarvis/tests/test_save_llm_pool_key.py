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
