"""Daily / weekly / monthly windows for the per-user token cap
(``Jarvis User Settings.limit_period``).

The window is a lazily-rolled counter, same trick as the month buckets: the
accrual UPDATE compares the stored ``period_key`` with the current bucket key
and either adds or restarts, so no scheduler is involved. Switching a user's
period restarts the window at that moment (decision 2026-09-08). Fixtures and
commit/cleanup discipline come from ``test_user_settings``.
"""

from __future__ import annotations

from datetime import datetime

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import policy, usage, user_settings_api
from jarvis.tests.test_user_settings import USER_A, USETT, _make_session, _UsageTestBase


def _set_cap(limit: int, period: str | None = None) -> dict:
	kwargs = {"user": USER_A, "monthly_token_limit": limit}
	if period is not None:
		kwargs["limit_period"] = period
	return user_settings_api.admin_set_user_limit(**kwargs)


def _stamp(**fields) -> None:
	frappe.db.set_value(USETT, {"user": USER_A}, fields, update_modified=False)
	frappe.db.commit()


def _row(*fields):
	return frappe.db.get_value(USETT, {"user": USER_A}, list(fields), as_dict=True)


def _turn(session_key: str, tokens_in: int, tokens_out: int) -> None:
	usage.record_turn_usage(
		session_key,
		{
			"totalTokensFresh": True,
			"inputTokens": tokens_in,
			"outputTokens": tokens_out,
			"totalTokens": 100,
		},
	)


class TestPeriodKey(FrappeTestCase):
	def test_all_time_has_no_bucket(self):
		self.assertEqual(usage.current_period_key("All time"), "")
		self.assertEqual(usage.current_period_key(""), "")
		self.assertEqual(usage.current_period_key(None), "")

	def test_daily_key_is_the_site_day(self):
		self.assertEqual(usage.current_period_key("Daily", datetime(2026, 9, 8, 23, 59)), "D:2026-09-08")

	def test_weekly_key_is_the_weeks_sunday(self):
		# Frappe's weekly scheduler boundary is Sunday 00:00, so a week is keyed
		# by its Sunday: Tue 09-08 and Sat 09-12 share 09-06; Sun 09-13 starts anew.
		self.assertEqual(usage.current_period_key("Weekly", datetime(2026, 9, 8)), "W:2026-09-06")
		self.assertEqual(usage.current_period_key("Weekly", datetime(2026, 9, 12, 23, 59)), "W:2026-09-06")
		self.assertEqual(usage.current_period_key("Weekly", datetime(2026, 9, 13)), "W:2026-09-13")

	def test_monthly_key(self):
		self.assertEqual(usage.current_period_key("Monthly", datetime(2026, 9, 8)), "M:2026-09")

	def test_daily_and_weekly_keys_never_collide(self):
		sunday = datetime(2026, 9, 13)
		self.assertNotEqual(
			usage.current_period_key("Daily", sunday), usage.current_period_key("Weekly", sunday)
		)


class TestWindowAccrual(_UsageTestBase):
	def test_all_time_cap_accrues_no_window(self):
		_set_cap(1000)
		_make_session("agent:win-all", USER_A)
		_turn("agent:win-all", 10, 5)
		s = _row("limit_period", "period_key", "period_tokens", "total_tokens")
		self.assertEqual(s.limit_period, "All time")
		self.assertEqual(s.period_key or "", "")
		self.assertEqual(int(s.period_tokens or 0), 0)
		self.assertEqual(s.total_tokens, 15)

	def test_window_accrues_under_the_current_key(self):
		_set_cap(1000, "Daily")
		_make_session("agent:win-day", USER_A)
		_turn("agent:win-day", 10, 5)
		_turn("agent:win-day", 8, 12)
		s = _row("period_key", "period_tokens", "total_tokens")
		self.assertEqual(s.period_tokens, 35)
		self.assertEqual(s.total_tokens, 35)
		self.assertEqual(s.period_key, usage.current_period_key("Daily"))

	def test_stale_key_restarts_the_window(self):
		_set_cap(1000, "Weekly")
		_make_session("agent:win-week", USER_A)
		_turn("agent:win-week", 10, 5)
		_stamp(period_key="W:2020-01-05")
		_turn("agent:win-week", 12, 8)
		s = _row("period_key", "period_tokens", "total_tokens")
		self.assertEqual(s.period_tokens, 20)
		self.assertEqual(s.total_tokens, 35)
		self.assertEqual(s.period_key, usage.current_period_key("Weekly"))


class TestAdminSetsPeriod(_UsageTestBase):
	def test_switching_period_starts_the_window_now(self):
		_set_cap(100)
		_stamp(total_tokens=500, period_tokens=77, period_key="stale")
		out = _set_cap(100, "Weekly")
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["limit_period"], "Weekly")
		s = _row("limit_period", "period_key", "period_tokens", "total_tokens")
		self.assertEqual(s.limit_period, "Weekly")
		self.assertEqual(s.period_key, usage.current_period_key("Weekly"))
		self.assertEqual(s.period_tokens, 0)
		self.assertEqual(s.total_tokens, 500)

	def test_changing_only_the_number_keeps_the_window(self):
		_set_cap(100, "Daily")
		_stamp(period_tokens=40)
		_set_cap(200, "Daily")
		s = _row("monthly_token_limit", "period_tokens")
		self.assertEqual(s.monthly_token_limit, 200)
		self.assertEqual(s.period_tokens, 40)

	def test_omitted_period_leaves_it_unchanged(self):
		# The pre-period SPA shape (no limit_period arg) must not reset a user
		# back to All time.
		_set_cap(100, "Daily")
		_stamp(period_tokens=40)
		_set_cap(150)
		s = _row("limit_period", "period_tokens")
		self.assertEqual(s.limit_period, "Daily")
		self.assertEqual(s.period_tokens, 40)

	def test_unknown_period_rejected(self):
		out = _set_cap(100, "Fortnightly")
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "invalid_period")

	def test_admin_list_carries_the_window(self):
		_set_cap(100, "Daily")
		_stamp(period_tokens=40)
		rows = user_settings_api.admin_list_user_usage()["data"]
		row = next(r for r in rows if r["user"] == USER_A)
		self.assertEqual(row["limit_period"], "Daily")
		self.assertEqual(row["period_tokens"], 40)

	def test_own_settings_carry_the_window(self):
		_set_cap(100, "Monthly")
		_stamp(period_tokens=40)
		frappe.set_user(USER_A)
		data = user_settings_api.get_my_settings()["data"]
		frappe.set_user("Administrator")
		self.assertEqual(data["limit_period"], "Monthly")
		self.assertEqual(data["period_tokens"], 40)

	def test_stale_window_reads_as_zero(self):
		from jarvis.chat.api import _measured_usage

		_set_cap(100, "Daily")
		_stamp(period_tokens=40, period_key="D:2020-01-01")
		rows = user_settings_api.admin_list_user_usage()["data"]
		row = next(r for r in rows if r["user"] == USER_A)
		self.assertEqual(row["period_tokens"], 0)
		measured = _measured_usage(USER_A)
		self.assertEqual(measured["limit_period"], "Daily")
		self.assertEqual(measured["period_tokens"], 0)

	def test_no_row_measured_usage_defaults_to_all_time(self):
		from jarvis.chat.api import _measured_usage

		measured = _measured_usage(USER_A)
		self.assertEqual(measured["limit_period"], "All time")
		self.assertEqual(measured["period_tokens"], 0)


class TestWindowEnforcement(_UsageTestBase):
	def test_full_window_blocks(self):
		_set_cap(100, "Daily")
		_stamp(period_tokens=100, total_tokens=0)
		ok, reason = policy.validate_can_send(USER_A)
		self.assertFalse(ok)
		self.assertEqual(reason, "usage_limit")

	def test_window_cap_ignores_the_all_time_total(self):
		_set_cap(100, "Weekly")
		_stamp(period_tokens=10, total_tokens=999999)
		ok, reason = policy.validate_can_send(USER_A)
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_stale_window_key_allows(self):
		_set_cap(100, "Daily")
		_stamp(period_tokens=500, period_key="D:2020-01-01")
		ok, reason = policy.validate_can_send(USER_A)
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_all_time_cap_ignores_the_window_counter(self):
		_set_cap(100)
		_stamp(period_tokens=500, period_key=usage.current_period_key("Daily"), total_tokens=50)
		ok, reason = policy.validate_can_send(USER_A)
		self.assertTrue(ok)
		self.assertIsNone(reason)


class TestRejectionNamesTheWindow(_UsageTestBase):
	def test_send_message_carries_the_window_that_blocked(self):
		from jarvis.chat.api import send_message

		_set_cap(100, "Daily")
		_stamp(period_tokens=100)
		frappe.set_user(USER_A)
		out = send_message(conversation="JCONV-does-not-matter", message="hi")
		frappe.set_user("Administrator")
		self.assertEqual(out, {"ok": False, "reason": "usage_limit", "limit_period": "Daily"})

	def test_all_time_cap_carries_no_window(self):
		from jarvis.chat.api import _send_rejection

		_set_cap(100)
		_stamp(total_tokens=100)
		self.assertEqual(_send_rejection(USER_A, "usage_limit"), {"ok": False, "reason": "usage_limit"})

	def test_per_model_rejection_carries_no_window(self):
		# The aggregate window is NOT full, so a usage_limit rejection came from
		# the (still monthly) per-model cap; the toast must stay period-neutral.
		from jarvis.chat.api import _send_rejection

		_set_cap(100, "Weekly")
		_stamp(period_tokens=1)
		self.assertEqual(_send_rejection(USER_A, "usage_limit"), {"ok": False, "reason": "usage_limit"})

	def test_other_reasons_pass_through(self):
		from jarvis.chat.api import _send_rejection

		self.assertEqual(
			_send_rejection(USER_A, "subscription_suspended"),
			{"ok": False, "reason": "subscription_suspended"},
		)


class TestMigrateWindow(_UsageTestBase):
	"""Code can run ahead of ``bench migrate`` for a moment (dev server
	auto-reload, a worker restarted early). The read paths and the gate must
	then behave as if the window columns did not exist: All time, 0 used."""

	def test_read_paths_and_gate_fall_back_to_all_time(self):
		from unittest.mock import patch

		from jarvis.chat.api import _measured_usage

		_set_cap(100, "Daily")
		_stamp(period_tokens=100, total_tokens=10)
		with patch.object(frappe.db, "has_column", return_value=False):
			row = next(r for r in user_settings_api.admin_list_user_usage()["data"] if r["user"] == USER_A)
			self.assertEqual((row["limit_period"], row["period_tokens"]), ("All time", 0))
			measured = _measured_usage(USER_A)
			self.assertEqual((measured["limit_period"], measured["period_tokens"]), ("All time", 0))
			ok, reason = policy.validate_can_send(USER_A)
		self.assertTrue(ok)
		self.assertIsNone(reason)


class TestContextCarriesUsage(_UsageTestBase):
	"""The composer's usage pill rides the context-meter payload the SPA already
	fetches after every turn, so the pill costs no request of its own."""

	def tearDown(self):
		from jarvis.tests.test_chat_compaction import _cleanup

		frappe.set_user("Administrator")
		_cleanup()
		super().tearDown()

	def test_context_payload_carries_the_cap_reading(self):
		from jarvis.chat.api import get_conversation_context
		from jarvis.tests.test_chat_compaction import _mk_conversation

		_set_cap(100, "Daily")
		_stamp(period_tokens=40, total_tokens=900)
		frappe.set_user(USER_A)
		conv = _mk_conversation("agent:main:c-usage-pill")
		out = get_conversation_context(conv)
		self.assertEqual(
			out["usage"],
			{"monthly_token_limit": 100, "limit_period": "Daily", "period_tokens": 40, "total_tokens": 900},
		)
