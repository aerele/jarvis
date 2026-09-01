"""GAP 1 (Track B) — the bench heartbeat emitter (dead-man's-switch, bench-side half)."""

from __future__ import annotations

from unittest.mock import patch

import frappe

from jarvis.chat import heartbeat, pump
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
