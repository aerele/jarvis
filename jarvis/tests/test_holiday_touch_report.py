"""Holiday touch error logging: one deferred row per kind per hour, safe on a GET request.

``frappe.db.rollback()`` stands in for the end of a GET request, which Frappe rolls back. A
deferred row goes through Redis, so ``frappe.deferred_insert.save_to_db`` (what the scheduler
runs) is called to write it.
"""

from __future__ import annotations

from unittest import mock

import frappe
from redis.exceptions import RedisError

from jarvis.holiday_touch import report, resolve
from jarvis.tests.test_holiday_touch_resolve import DIWALI, _Base, _now, clear_log_markers, flush_error_logs

NOW = _now(DIWALI)
ANSWER_TITLE = "Holiday touch: answer failed"


def _rows(title: str) -> int:
	return frappe.db.count("Error Log", {"method": title})


class TestReport(_Base):
	def setUp(self):
		super().setUp()
		frappe.cache().delete_value(report.PREFIX + "other")

	def _fail_once(self, user):
		with mock.patch.object(resolve, "occasion", side_effect=RuntimeError("boom")):
			return resolve.safe_answer(user, NOW)

	def test_the_row_survives_the_end_of_request_rollback(self):
		# On MariaDB the Error Log table is MyISAM, so a rollback cannot remove a row even when
		# it is inserted directly: this test pins the outcome, and
		# ``test_the_row_is_deferred_not_inserted_in_the_request`` pins the mechanism that keeps
		# it true on a transactional engine.
		user = self.person(dob="1990-11-08")
		flush_error_logs()
		before = _rows(ANSWER_TITLE)
		self.assertEqual(self._fail_once(user)["kind"], "none")
		frappe.db.rollback()  # a GET request ends in a rollback
		flush_error_logs()
		self.assertEqual(_rows(ANSWER_TITLE), before + 1)

	def test_the_row_is_deferred_not_inserted_in_the_request(self):
		user = self.person(dob="1990-11-08")
		flush_error_logs()
		before = _rows(ANSWER_TITLE)
		self._fail_once(user)
		self.assertEqual(_rows(ANSWER_TITLE), before)  # nothing inserted until the queue is flushed

	def test_at_most_one_row_an_hour_per_kind(self):
		user = self.person(dob="1990-11-08")
		other = self.person(dob="1990-11-08")
		flush_error_logs()
		before = _rows(ANSWER_TITLE)
		self._fail_once(user)
		self._fail_once(other)
		flush_error_logs()
		self.assertEqual(_rows(ANSWER_TITLE), before + 1)

	def test_the_marker_is_one_atomic_set_nx_ex_per_kind(self):
		instance = frappe.cache()
		with (
			mock.patch("frappe.log_error") as log,
			mock.patch.object(instance, "set", wraps=instance.set) as setter,
		):
			for _ in range(3):
				report.report("answer", "T")
		self.assertEqual(log.call_count, 1)
		for call in setter.call_args_list:
			self.assertEqual((call.kwargs["nx"], call.kwargs["ex"]), (True, 3600))
			self.assertEqual(call.kwargs["name"], instance.make_key(report.PREFIX + "answer"))

	def test_the_marker_survives_clear_cache(self):
		# The pump clears the cache about every five minutes; a cleared marker would log every time.
		with mock.patch("frappe.log_error") as log:
			report.report("answer", "T")
			frappe.clear_cache()
			report.report("answer", "T")
		self.assertEqual(log.call_count, 1)

	def test_kinds_are_independent(self):
		with mock.patch("frappe.log_error") as log:
			report.report("answer", "A")
			report.report("other", "B")
			report.report("answer", "A")
			report.report("other", "B")
		self.assertEqual([c.kwargs["title"] for c in log.call_args_list], ["A", "B"])

	def test_a_lost_race_does_not_log_and_an_unusable_marker_still_logs(self):
		instance = frappe.cache()
		with mock.patch("frappe.log_error") as log:
			with mock.patch.object(instance, "set", return_value=None):  # another worker won
				report.report("answer", "T")
			self.assertEqual(log.call_count, 0)
			with mock.patch.object(instance, "set", side_effect=RedisError("down")):
				report.report("answer", "T")
				report.report("answer", "T")
		self.assertEqual(log.call_count, 2)

	def test_the_call_is_deferred_with_a_message_and_never_raises(self):
		with mock.patch("frappe.log_error") as log:
			report.report("answer", "T")
		self.assertTrue(log.call_args.kwargs["defer_insert"])
		self.assertIn("message", log.call_args.kwargs)
		with mock.patch("frappe.log_error", side_effect=RuntimeError("log down")):
			report.report("other", "T")
