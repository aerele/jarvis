"""Heartbeat: the three scheduled-macro health integers (counts only, no names)."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis.chat import heartbeat
from jarvis.tests._pending_action_helpers import ensure_user

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"
OWNER = "hbmacro-owner@example.com"
KEYS = {"sched_macros_enabled", "sched_macros_overdue", "sched_last_ok_age_s"}


def _mk(tag, *, enabled=1, sched=1, next_run=None, hold=0):
	doc = frappe.get_doc(
		{
			"doctype": MACRO,
			"macro_name": f"hbmacro-{tag}",
			"enabled": enabled,
			"schedule_enabled": sched,
			"schedule_frequency": "daily",
			"steps": [{"prompt": "x"}],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	values = {"owner": OWNER, "next_run_at": next_run}
	if hold and frappe.get_meta(MACRO).has_field("admin_hold"):
		values["admin_hold"] = 1
	frappe.db.set_value(MACRO, doc.name, values, update_modified=False)
	return doc.name


def _mk_run(macro, *, trigger="scheduled", status="completed", finished_at=None):
	doc = frappe.get_doc({"doctype": RUN, "macro": macro, "trigger": trigger, "status": status})
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.set_value(RUN, doc.name, "finished_at", finished_at, update_modified=False)
	return doc.name


class TestHeartbeatMacroHealth(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		ensure_user(OWNER)
		# The hourly fault-log marker is a persistent key: clear it so the fault test sees
		# a fresh hour whatever ran before it.
		frappe.cache().delete_value(heartbeat._MACRO_HEALTH_LOG_KEY)
		# Isolate from rows other suites left behind: this transaction is rolled back.
		frappe.db.delete(RUN, {"trigger": "scheduled", "status": "completed"})
		self.base = heartbeat._scheduled_macro_health()
		self.base_enabled = self.base["sched_macros_enabled"]
		self.base_overdue = self.base["sched_macros_overdue"]

	def tearDown(self):
		frappe.db.rollback()

	def _health(self):
		return heartbeat._scheduled_macro_health()

	def test_counts_only_enabled_scheduled_unheld(self):
		_mk("a", next_run=add_to_date(now_datetime(), hours=1))
		_mk("off", enabled=0, next_run=add_to_date(now_datetime(), hours=1))
		_mk("nosched", sched=0)
		h = self._health()
		self.assertEqual(h["sched_macros_enabled"], self.base_enabled + 1)

	def test_held_macro_not_counted(self):
		if not frappe.get_meta(MACRO).has_field("admin_hold"):
			self.skipTest("admin_hold not on this schema")
		_mk("held", hold=1, next_run=add_to_date(now_datetime(), hours=-5))
		h = self._health()
		self.assertEqual(h["sched_macros_enabled"], self.base_enabled)
		self.assertEqual(h["sched_macros_overdue"], self.base_overdue)

	def test_overdue_only_beyond_two_hours(self):
		now = now_datetime()
		_mk("fresh", next_run=add_to_date(now, minutes=-119))
		_mk("late", next_run=add_to_date(now, minutes=-121))
		_mk("future", next_run=add_to_date(now, hours=3))
		h = self._health()
		self.assertEqual(h["sched_macros_enabled"], self.base_enabled + 3)
		self.assertEqual(h["sched_macros_overdue"], self.base_overdue + 1)

	def test_null_next_run_not_overdue(self):
		_mk("null", next_run=None)
		h = self._health()
		self.assertEqual(h["sched_macros_enabled"], self.base_enabled + 1)
		self.assertEqual(h["sched_macros_overdue"], self.base_overdue)

	def test_last_ok_absent_when_no_completed_scheduled_run(self):
		m = _mk("r", next_run=None)
		_mk_run(m, trigger="manual", status="completed")
		_mk_run(m, trigger="scheduled", status="failed")
		self.assertNotIn("sched_last_ok_age_s", self._health())

	def test_last_ok_age_from_newest_completed_scheduled_run(self):
		m = _mk("r", next_run=None)
		old = _mk_run(m, finished_at=add_to_date(now_datetime(), hours=-9))
		newest = _mk_run(m, finished_at=add_to_date(now_datetime(), seconds=-600))
		# the newest by creation wins; a failed or manual one never counts
		frappe.db.set_value(
			RUN, old, "creation", add_to_date(now_datetime(), hours=-9), update_modified=False
		)
		frappe.db.set_value(
			RUN, newest, "creation", add_to_date(now_datetime(), minutes=-10), update_modified=False
		)
		_mk_run(m, trigger="manual", status="completed", finished_at=now_datetime())
		age = self._health()["sched_last_ok_age_s"]
		self.assertGreaterEqual(age, 590)
		self.assertLess(age, 700)

	def test_last_ok_falls_back_to_modified_without_finished_at(self):
		m = _mk("r", next_run=None)
		run = _mk_run(m, finished_at=None)
		frappe.db.set_value(
			RUN, run, "modified", add_to_date(now_datetime(), seconds=-300), update_modified=False
		)
		age = self._health()["sched_last_ok_age_s"]
		self.assertGreaterEqual(age, 290)
		self.assertLess(age, 400)

	def test_last_ok_ignores_runs_older_than_the_window(self):
		m = _mk("r", next_run=None)
		old = _mk_run(m, finished_at=add_to_date(now_datetime(), days=-35))
		frappe.db.set_value(
			RUN, old, "creation", add_to_date(now_datetime(), days=-35), update_modified=False
		)
		self.assertNotIn("sched_last_ok_age_s", self._health(), "a 35-day-old success is outside the window")

	def test_last_ok_age_never_negative_on_a_future_stamp(self):
		# Clock skew: a finished_at ahead of the site clock reads as 0, never a negative age
		# (the control plane drops a negative value as "not reported").
		m = _mk("r", next_run=None)
		_mk_run(m, finished_at=add_to_date(now_datetime(), minutes=10))
		self.assertEqual(self._health()["sched_last_ok_age_s"], 0)

	def test_vector_keeps_liveness_keys_and_adds_only_counts(self):
		v = heartbeat.bench_liveness_vector()
		self.assertTrue({"watchdog_last_completed_age_s", "oldest_nonterminal_turn_age_s"} <= set(v))
		self.assertTrue(set(v) <= {"watchdog_last_completed_age_s", "oldest_nonterminal_turn_age_s"} | KEYS)
		for k in KEYS & set(v):
			self.assertIsInstance(v[k], int)

	def test_failing_query_keeps_liveness_and_omits_the_three(self):
		with (
			patch.object(heartbeat.frappe.db, "count", side_effect=RuntimeError("db down")),
			patch.object(heartbeat.frappe, "log_error") as logged,
		):
			v = heartbeat.bench_liveness_vector()
			heartbeat.bench_liveness_vector()
		self.assertEqual(set(v), {"watchdog_last_completed_age_s", "oldest_nonterminal_turn_age_s"})
		self.assertEqual(logged.call_count, 1, "logged once, and only once an hour")

	def test_fault_marker_is_covered_by_persistent_cache_keys(self):
		from jarvis import hooks

		self.assertTrue(
			any(heartbeat._MACRO_HEALTH_LOG_KEY.startswith(k) for k in hooks.persistent_cache_keys)
		)

	def test_hold_filter_added_only_when_the_field_exists(self):
		# Pins the query shape on a schema (like the shared test site) that lags the field.
		for has_field in (True, False):
			meta = frappe._dict(has_field=lambda f, _h=has_field: _h and f == "admin_hold")
			with (
				patch.object(heartbeat.frappe, "get_meta", return_value=meta),
				patch.object(heartbeat.frappe.db, "count", return_value=0) as count,
				patch.object(heartbeat.frappe.db, "get_value", return_value=None),
			):
				heartbeat._scheduled_macro_health()
			for call in count.call_args_list:
				self.assertEqual("admin_hold" in call.args[1], has_field)
				if has_field:
					self.assertEqual(call.args[1]["admin_hold"], 0)
