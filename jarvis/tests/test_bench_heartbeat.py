"""GAP 1 (Track B) — the bench heartbeat emitter (dead-man's-switch, bench-side half)."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import admission, heartbeat, pump
from jarvis.chat import turn_state as ts
from jarvis.exceptions import AdminUnreachableError, AdminValidationError
from jarvis.tests.test_pump import _PumpTestCase


class TestBenchHeartbeatVector(_PumpTestCase):
	def test_b2_watchdog_age_none_when_unstamped(self):
		frappe.defaults.clear_default(pump.WATCHDOG_LAST_COMPLETED_KEY)
		frappe.db.commit()
		self.assertIsNone(heartbeat._watchdog_age_s(), "unstamped watchdog reads as unknown, not stale")

	def test_b2_watchdog_age_from_stamp(self):
		frappe.db.set_default(pump.WATCHDOG_LAST_COMPLETED_KEY, frappe.utils.add_to_date(None, seconds=-120))
		frappe.db.commit()
		age = heartbeat._watchdog_age_s()
		self.assertIsNotNone(age)
		self.assertGreaterEqual(age, 110)
		self.assertLessEqual(age, 200)

	def test_b2_oldest_turn_age_reflects_the_oldest(self):
		conv = self._mk_conv()
		seed = self._mk_msg(conv)
		self._mk_turn(conv, "hb_old", seed, "streaming", version=1)
		frappe.db.set_value(
			ts.TURN,
			"hb_old",
			"enqueued_at",
			frappe.utils.add_to_date(None, seconds=-300),
			update_modified=False,
		)
		frappe.db.commit()
		# MIN(enqueued_at) over nonterminal turns picks the oldest; any older stray only
		# raises the age, so >= 290 is robust.
		self.assertGreaterEqual(heartbeat._oldest_nonterminal_turn_age_s(), 290)

	def test_b2_vector_shape(self):
		v = heartbeat.bench_liveness_vector()
		self.assertIn("watchdog_last_completed_age_s", v)
		self.assertIn("oldest_nonterminal_turn_age_s", v)
		self.assertGreaterEqual(v["oldest_nonterminal_turn_age_s"], 0)


class TestBenchHeartbeatPush(_PumpTestCase):
	def test_b3_pushes_unconditionally_when_admin_configured(self):
		"""Fires even with zero errors AND zero usage — the exact hole in error/usage push."""
		with (
			patch.object(heartbeat, "_admin_configured", return_value=True),
			patch("jarvis.admin_client.push_bench_heartbeat") as push,
		):
			heartbeat.push_bench_heartbeat()
		push.assert_called_once()

	def test_b3_skips_when_admin_unconfigured(self):
		with (
			patch.object(heartbeat, "_admin_configured", return_value=False),
			patch("jarvis.admin_client.push_bench_heartbeat") as push,
		):
			heartbeat.push_bench_heartbeat()
		push.assert_not_called()

	def test_b3_silent_on_admin_unreachable(self):
		with (
			patch.object(heartbeat, "_admin_configured", return_value=True),
			patch("jarvis.admin_client.push_bench_heartbeat", side_effect=AdminUnreachableError("down")),
		):
			heartbeat.push_bench_heartbeat()  # must not raise

	def test_b3_silent_on_admin_validation_error(self):
		"""A 4xx from the backend — INCLUDING the ingest endpoint not existing yet (before Track
		C ships) — is silently swallowed, never logged fleet-wide."""
		with (
			patch.object(heartbeat, "_admin_configured", return_value=True),
			patch(
				"jarvis.admin_client.push_bench_heartbeat", side_effect=AdminValidationError("no endpoint")
			),
			patch.object(heartbeat, "_log_failure_throttled") as logged,
		):
			heartbeat.push_bench_heartbeat()  # must not raise
		logged.assert_not_called()

	def test_b3_never_raises_on_a_bug(self):
		with (
			patch.object(heartbeat, "_admin_configured", return_value=True),
			patch("jarvis.admin_client.push_bench_heartbeat", side_effect=RuntimeError("boom")),
			patch.object(heartbeat, "_log_failure_throttled") as logged,
		):
			heartbeat.push_bench_heartbeat()  # must not raise
		logged.assert_called_once()


class TestPushBenchHeartbeatClient(_PumpTestCase):
	def test_b4_posts_to_ingest_endpoint(self):
		from jarvis import admin_client

		vector = {"watchdog_last_completed_age_s": 5, "oldest_nonterminal_turn_age_s": 0}
		with patch.object(admin_client, "_post") as post:
			admin_client.push_bench_heartbeat(vector)
		post.assert_called_once()
		_, kwargs = post.call_args
		self.assertIn("ingest_bench_heartbeat", kwargs["path"])
		self.assertEqual(kwargs["body"], {"heartbeat": vector})


class _MaxChatsCase(FrappeTestCase):
	"""Writes Jarvis Settings.max_concurrent_chats via set_single_value INSIDE the test
	transaction only (never a commit, never a doc save); tearDown rolls it back and drops
	the document cache so nothing leaks onto the shared local site."""

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")

	def _store(self, value):
		frappe.db.set_single_value("Jarvis Settings", "max_concurrent_chats", value, update_modified=False)
		frappe.clear_document_cache("Jarvis Settings", "Jarvis Settings")

	def _stored(self):
		return frappe.db.get_single_value("Jarvis Settings", "max_concurrent_chats")


class TestHeartbeatAppliesMaxConcurrentChats(_MaxChatsCase):
	def _beat(self, reply=None, side_effect=None):
		with (
			patch.object(heartbeat, "_admin_configured", return_value=True),
			patch("jarvis.admin_client.push_bench_heartbeat", return_value=reply, side_effect=side_effect),
		):
			heartbeat.push_bench_heartbeat()  # must never raise

	def test_positive_int_is_stored(self):
		self._store(None)
		self._beat({"tenant": "t", "max_concurrent_chats": 8})
		self.assertEqual(self._stored(), 8)

	def test_positive_int_replaces_stored(self):
		self._store(4)
		self._beat({"tenant": "t", "max_concurrent_chats": 12})
		self.assertEqual(self._stored(), 12)

	def test_key_absent_keeps_stored(self):
		self._store(8)
		self._beat({"tenant": "t"})
		self.assertEqual(self._stored(), 8)

	def test_non_dict_reply_keeps_stored(self):
		self._store(8)
		self._beat(None)
		self._beat("ok")
		self.assertEqual(self._stored(), 8)

	def test_explicit_null_clears(self):
		self._store(8)
		self._beat({"tenant": "t", "max_concurrent_chats": None})
		self.assertFalse(self._stored())

	def test_bad_values_keep_stored(self):
		self._store(8)
		for bad in (0, -3, True, False, "8", 8.0, [8], {"n": 8}):
			with self.subTest(bad=bad):
				self._beat({"tenant": "t", "max_concurrent_chats": bad})
				self.assertEqual(self._stored(), 8)

	def test_unchanged_value_does_not_write(self):
		self._store(8)
		with patch.object(frappe.db, "set_single_value") as write:
			self._beat({"tenant": "t", "max_concurrent_chats": 8})
		write.assert_not_called()

	def test_null_with_nothing_stored_does_not_write(self):
		self._store(None)
		with patch.object(frappe.db, "set_single_value") as write:
			self._beat({"tenant": "t", "max_concurrent_chats": None})
		write.assert_not_called()

	def test_write_does_not_touch_modified(self):
		self._store(None)
		with patch.object(frappe.db, "set_single_value") as write:
			self._beat({"tenant": "t", "max_concurrent_chats": 8})
		write.assert_called_once_with("Jarvis Settings", "max_concurrent_chats", 8, update_modified=False)

	def test_admin_error_keeps_stored(self):
		self._store(8)
		self._beat(side_effect=AdminUnreachableError("down"))
		self.assertEqual(self._stored(), 8)

	def test_apply_failure_never_breaks_heartbeat(self):
		self._store(None)
		with (
			patch.object(frappe.db, "set_single_value", side_effect=RuntimeError("db")),
			patch.object(heartbeat, "_log_failure_throttled") as logged,
		):
			self._beat({"tenant": "t", "max_concurrent_chats": 8})
		logged.assert_called_once()


class TestMaxInflightFromSettings(_MaxChatsCase):
	def test_nothing_stored_is_default(self):
		self._store(None)
		self.assertEqual(admission._max_inflight(), admission.DEFAULT_MAX_INFLIGHT)
		self.assertEqual(admission.DEFAULT_MAX_INFLIGHT, 4)

	def test_stored_value_is_used(self):
		self._store(8)
		self.assertEqual(admission._max_inflight(), 8)

	def test_garbage_is_default(self):
		for bad in (0, -2, "x", True):
			with (
				self.subTest(bad=bad),
				patch("frappe.get_cached_value", return_value=bad),
			):
				self.assertEqual(admission._max_inflight(), admission.DEFAULT_MAX_INFLIGHT)

	def test_write_invalidates_cached_read(self):
		self._store(8)
		self.assertEqual(admission._max_inflight(), 8)
		self._store(6)
		self.assertEqual(admission._max_inflight(), 6)

	def test_site_config_key_is_ignored(self):
		self._store(None)
		with patch.dict(frappe.local.conf, {"jarvis_site_max_inflight_turns": 9}):
			self.assertEqual(admission._max_inflight(), admission.DEFAULT_MAX_INFLIGHT)
