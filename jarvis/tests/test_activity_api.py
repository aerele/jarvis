"""Tests for ``jarvis.activity_report.get_user_activity`` (plan task 12): the one POST
endpoint behind the in-app "User activity" page, and the notes it shares with the Desk
report.

Fixtures and the clean-table base come from ``test_activity_report`` (imported as a module
so its test classes are not collected a second time)."""

import datetime as dt
import json
from unittest import mock

import frappe

from jarvis import activity_report as ar
from jarvis.jarvis.report.jarvis_user_activity import jarvis_user_activity as report
from jarvis.tests import test_activity_report as base

ENDPOINT = "jarvis.activity_report.get_user_activity"


class TestGetUserActivity(base.ReportTestCase):
	def _range(self, **extra):
		return {"from_date": str(base.DAY), "to_date": str(base.DAY), **extra}

	def _plain_user(self, email="act-api-plain@example.com"):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": "Plain", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		return email

	def test_endpoint_is_whitelisted_post_only(self):
		self.assertIn(ar.get_user_activity, frappe.whitelisted)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[ar.get_user_activity], ["POST"])

	def test_summary_view_returns_the_summary_plus_notes(self):
		base.ev(rows=3)
		base.usage(base.A, tin=3, tout=4)
		res = ar.get_user_activity("summary", **self._range())
		self.assertEqual(set(res), set(ar.summary(self._range())) | {"notes"})
		self.assertEqual([p["user"] for p in res["people"]], [base.A])
		self.assertEqual(res["thresholds"], ar.THRESHOLDS)
		self.assertEqual(res["notes"][0], ar.scope_line(res["first_event_at"]))
		for p in res["people"]:
			self.assertEqual(set(p), set(ar.PERSON_KEYS))

	def test_summary_is_the_default_view(self):
		base.ev()
		self.assertIn("people", ar.get_user_activity(**self._range()))

	def test_events_view_returns_rows_cursor_and_notes(self):
		base.ev(rows=2)
		res = ar.get_user_activity("events", **self._range())
		self.assertEqual(
			set(res),
			{"rows", "next_cursor", "thresholds", "preparing", "notes", "first_event_at", "logging_health"},
		)
		self.assertEqual(set(res["first_event_at"]), {"agent", "app"})
		self.assertEqual(set(res["logging_health"]), set(ar.summary(self._range())["logging_health"]))
		self.assertEqual(len(res["rows"]), 1)
		self.assertEqual(set(res["rows"][0]), set(ar.EVENT_ROW_KEYS))
		self.assertIsNone(res["next_cursor"])
		self.assertFalse(res["preparing"])
		self.assertTrue(res["notes"])
		self.assertEqual(res["notes"][0], ar.scope_line(ar.first_event_at()))

	def test_events_pages_with_the_cursor_and_notes_ride_the_first_page_only(self):
		for i in range(ar.DEFAULT_LIMIT + 5):
			base.ev(base.at(10, 0, i % 60, base.DAY), actor=base.A)
		first = ar.get_user_activity("events", **self._range())
		self.assertEqual(len(first["rows"]), ar.DEFAULT_LIMIT)
		self.assertTrue(first["next_cursor"])
		self.assertTrue(first["notes"])
		second = ar.get_user_activity("events", **self._range(cursor=first["next_cursor"]))
		self.assertEqual(len(second["rows"]), 5)
		self.assertIsNone(second["next_cursor"])
		self.assertEqual(second["notes"], [])
		# a follow-up page carries neither: they ride the first page only
		self.assertNotIn("logging_health", second)
		self.assertNotIn("first_event_at", second)
		self.assertIn("logging_health", first)
		seen = {(r["at"], r["actor"], r["label"]) for r in first["rows"] + second["rows"]}
		self.assertTrue(len(seen) <= ar.DEFAULT_LIMIT + 5)

	def test_events_filters_reach_the_query(self):
		base.ev(actor=base.A, event="read")
		base.ev(actor=base.B, event="export", rows=9)
		by_user = ar.get_user_activity("events", **self._range(user=base.B))
		self.assertEqual([r["actor"] for r in by_user["rows"]], [base.B])
		by_activity = ar.get_user_activity("events", **self._range(activity="read"))
		self.assertEqual([r["activity"] for r in by_activity["rows"]], ["read"])
		by_outcome = ar.get_user_activity("events", **self._range(outcome="denied"))
		self.assertEqual(by_outcome["rows"], [])

	def test_blank_filters_mean_no_filter(self):
		base.ev()
		res = ar.get_user_activity("events", "", "", "", "", "", "")
		self.assertEqual(len(res["rows"]), 1)

	def test_a_plain_user_is_refused_on_both_views(self):
		base.ev()
		frappe.set_user(self._plain_user())
		for view in ("summary", "events"):
			with self.assertRaises(frappe.PermissionError):
				ar.get_user_activity(view, **self._range())

	def test_a_jarvis_admin_is_enough(self):
		email = self._plain_user("act-api-admin@example.com")
		frappe.get_doc("User", email).add_roles("Jarvis Admin")
		base.ev()
		frappe.set_user(email)
		self.assertTrue(ar.get_user_activity("summary", **self._range())["people"])
		self.assertEqual(len(ar.get_user_activity("events", **self._range())["rows"]), 1)

	def test_an_unknown_view_is_rejected(self):
		for bad in ("everything", "Summary", "events; drop", 7):
			with self.assertRaises(frappe.ValidationError, msg=repr(bad)):
				ar.get_user_activity(bad)

	def test_non_text_and_bad_dates_are_rejected_not_a_500(self):
		with self.assertRaises(frappe.ValidationError):
			ar.get_user_activity("events", user=["a@example.com"])
		with self.assertRaises(frappe.ValidationError):
			ar.get_user_activity("summary", from_date="not-a-date")
		with self.assertRaises(frappe.ValidationError):
			ar.get_user_activity("events", cursor="garbage")

	def test_a_missing_events_table_answers_preparing(self):
		real = frappe.db.table_exists
		with mock.patch.object(
			frappe.db, "table_exists", side_effect=lambda dt, *a, **k: dt != ar.ACTIVITY_EVENT and real(dt)
		):
			res = ar.get_user_activity("events", **self._range())
		self.assertTrue(res["preparing"])
		self.assertEqual(res["rows"], [])
		self.assertEqual(res["notes"], [ar.preparing_note()])
		self.assertEqual(res["first_event_at"], {"agent": None, "app": None})
		self.assertFalse(res["logging_health"]["app_warn"])

	def test_no_content_reaches_the_response(self):
		base.ev(rows=3, ref_name="r", ref_doctype="ToDo", channel="chat", code="X")
		base.wr(tool="delete_doc", bulk=2, ref_name="w", provenance_name="a free label")
		base.usage(base.A, tin=3, tout=4)
		blob = json.dumps(
			[ar.get_user_activity(v, **self._range()) for v in ("summary", "events")], default=str
		)
		for forbidden in ("tool_args", "tool_result", "content", "provenance_name", "a free label"):
			self.assertNotIn(forbidden, blob)


class TestSharedNotes(base.ReportTestCase):
	"""The Desk report and the page say the same thing about the same result."""

	def _stale_result(self):
		return {
			"people": [],
			"first_event_at": {"agent": "2026-10-01", "app": "2026-11-04"},
			"logging_health": {
				"warn": True,
				"rollup_lag_days": 3,
				"scheduler_disabled": True,
			},
			"stale_days": 2,
			"clamped": True,
			"partial": True,
			"excluded_days": 4,
			"preparing": False,
		}

	def test_summary_notes_cover_every_state_in_order(self):
		notes = ar.summary_notes(self._stale_result())
		self.assertEqual(
			notes,
			[
				ar.scope_line({"agent": "2026-10-01", "app": "2026-11-04"}),
				ar.kept_note(),
				ar.stale_note(2),
				ar.partial_note(4),
				*ar.health_notes({"warn": True, "rollup_lag_days": 3, "scheduler_disabled": True}),
			],
		)
		self.assertEqual(len(notes), 7)

	def test_a_truncated_people_list_is_noted_after_the_data_notes_and_before_health(self):
		res = {**self._stale_result(), "people_truncated": True}
		notes = ar.summary_notes(res)
		self.assertEqual(notes.index(ar.partial_note(4)) + 1, notes.index(ar.people_truncated_note()))
		self.assertEqual(
			notes.index(ar.people_truncated_note()) + 1,
			notes.index(ar.health_notes(res["logging_health"])[0]),
		)
		self.assertIn("500", ar.people_truncated_note())
		self.assertNotIn(ar.people_truncated_note(), ar.summary_notes(self._stale_result()))

	def test_the_app_action_warning_is_a_shared_health_note(self):
		health = {"warn": False, "app_warn": True, "rollup_lag_days": 0, "scheduler_disabled": False}
		self.assertEqual(ar.health_notes(health), [ar.app_warn_note()])
		res = {**self._stale_result(), "logging_health": health, "stale_days": 0, "clamped": False}
		res.update(partial=False, excluded_days=0)
		self.assertIn(ar.app_warn_note(), ar.summary_notes(res))
		# the agent warning keeps its own, separate sentence
		self.assertNotEqual(ar.app_warn_note(), ar.health_notes({"warn": True})[0])

	def test_the_api_summary_carries_the_truncation_flag_and_the_app_health(self):
		base.ev()
		res = ar.get_user_activity("summary", from_date=str(base.DAY), to_date=str(base.DAY))
		self.assertIs(res["people_truncated"], False)
		for key in ("app_warn", "app_seen_24h", "app_actions_24h", "app_failures_24h", "app_dropped_24h"):
			self.assertIn(key, res["logging_health"])

	def test_quiet_summary_has_only_the_scope_line(self):
		res = {**self._stale_result(), "stale_days": 0, "clamped": False, "partial": False}
		res["excluded_days"] = 0
		res["logging_health"] = {"warn": False, "rollup_lag_days": 1, "scheduler_disabled": False}
		self.assertEqual(len(ar.summary_notes(res)), 1)

	def test_preparing_summary_has_only_the_preparing_note(self):
		self.assertEqual(ar.summary_notes({"preparing": True}), [ar.preparing_note()])

	def test_the_desk_report_message_is_the_joined_notes(self):
		res = self._stale_result()
		with mock.patch("jarvis.activity_report.summary", return_value=res):
			_cols, _data, message = report._summary({})
		self.assertEqual(message, " ".join(ar.summary_notes(res)))

	def test_events_notes_add_the_kept_window_only_before_the_horizon(self):
		first = {"agent": "2026-10-01", "app": None}
		self.assertEqual(ar.events_notes(first, str(base.DAY)), [ar.scope_line(first)])
		old = str(ar.horizon_day() - dt.timedelta(days=5))
		self.assertEqual(ar.events_notes(first, old), [ar.scope_line(first), ar.kept_note()])
