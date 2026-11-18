"""Tests for ``jarvis.activity_report`` (plan 5 task 5): the unified events stream
(keyset paging over Activity Event + Agent Write), the per-person summary and its
E3 flags, the applied-export exclusion rule, and the admin gate.

Runs on a test site: the three source tables are emptied in ``setUpClass`` (and per
test) so every count is exact. Fixture times sit on a fixed past day, at 10:00 unless
a test is about the clock, so they never trip the off-hours flag by accident."""

import datetime as dt
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis import activity_actions
from jarvis import activity_report as ar
from jarvis.activity_log import EXPORT_TOOLS
from jarvis.agent_audit import AGENT_WRITE_FIELDS

# Days are relative to today: the summary reads only the last 90 days (the rollup horizon),
# so a fixed past date would fall out of range as the calendar moves. Set per test by
# ``_set_days`` (setUp), never at import: a long-lived test runner crosses midnight.
DAY = NEXT_DAY = RANGE = None
A = "act-a@example.com"
B = "act-b@example.com"
C = "act-c@example.com"
_TABLES = ("Jarvis Activity Event", "Jarvis Agent Write", "Jarvis Turn Usage", "Jarvis Activity Daily")


def _set_days():
	global DAY, NEXT_DAY, RANGE
	DAY = dt.date.today() - dt.timedelta(days=10)
	NEXT_DAY = DAY + dt.timedelta(days=1)
	RANGE = {"from_date": str(DAY), "to_date": str(DAY)}


def at(h=10, m=0, s=0, day=None):
	return dt.datetime.combine(day or DAY, dt.time(h, m, s))


def ev(when=None, event="read", actor=A, outcome="ok", rows=0, **kw):
	d = {
		"doctype": "Jarvis Activity Event",
		"at": when or at(),
		"actor": actor,
		"actor_name": kw.pop("actor_name", actor.split("@")[0] if actor else ""),
		"event": event,
		"method": kw.pop("method", "get_list"),
		"outcome": outcome,
		"row_count": rows,
	}
	d.update(kw)
	return frappe.get_doc(d).insert(ignore_permissions=True, ignore_links=True)


def wr(when=None, tool="create_doc", outcome="applied", actor=A, bulk=0, **kw):
	d = {
		"doctype": "Jarvis Agent Write",
		"at": when or at(),
		"actor": actor,
		"actor_name": kw.pop("actor_name", actor.split("@")[0] if actor else ""),
		"tool": tool,
		"outcome": outcome,
		"provenance": kw.pop("provenance", "chat"),
		"bulk_count": bulk,
	}
	d.update(kw)
	return frappe.get_doc(d).insert(ignore_permissions=True, ignore_links=True)


def usage(user, day=None, tin=0, tout=0, calls=0):
	return frappe.get_doc(
		{
			"doctype": "Jarvis Turn Usage",
			"user": user,
			"day": day or DAY,
			"tokens_in": tin,
			"tokens_out": tout,
			"tool_calls": calls,
		}
	).insert(ignore_permissions=True)


def person(result, user):
	for p in result["people"]:
		if p["user"] == user:
			return p
	return None


def flags(result, user):
	p = person(result, user)
	return {f["key"]: f["value"] for f in (p["flags"] if p else [])}


class ReportTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._clear()

	@classmethod
	def _clear(cls):
		# Test site only; the class-level rollback restores whatever was there.
		for dtn in _TABLES:
			frappe.db.delete(dtn)
		frappe.db.delete("Singles", {"doctype": "Jarvis Activity Rollup"})  # nothing rolled

	def setUp(self):
		_set_days()
		frappe.set_user("Administrator")
		self._clear()
		# A summary over unrolled closed days enqueues the rollup: never onto the real queue.
		patcher = mock.patch("frappe.enqueue")
		self.enqueue = patcher.start()
		self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		self._clear()

	def summary(self, **extra):
		return ar.summary({**RANGE, **extra})


class TestEventsPaging(ReportTestCase):
	def test_keyset_paging_across_both_sources_with_equal_at(self):
		# Many rows share one `at` (and the two sources interleave on it), which is
		# exactly where an offset or an `at`-only cursor drops or repeats rows.
		same = at(11)
		for i in range(7):
			ev(same, ref_name=f"e{i}")
		for i in range(6):
			wr(same, ref_name=f"w{i}")
		for i in range(3):
			ev(at(9, i), ref_name=f"old-e{i}")
			wr(at(8, i), ref_name=f"old-w{i}")
		expected = {f"e{i}" for i in range(7)} | {f"w{i}" for i in range(6)}
		expected |= {f"old-e{i}" for i in range(3)} | {f"old-w{i}" for i in range(3)}

		for limit in (1, 4, 5, 7, 22, 100):
			seen, cursor, pages = [], None, 0
			while True:
				page = ar.events(RANGE, after=cursor, limit=limit)
				pages += 1
				self.assertLessEqual(len(page["rows"]), limit)
				seen += [r["ref_name"] for r in page["rows"]]
				cursor = page["next_cursor"]
				if cursor is None:
					break
				self.assertLess(pages, 100)
			self.assertEqual(len(seen), len(set(seen)), f"duplicates at limit={limit}")
			self.assertEqual(set(seen), expected, f"gaps at limit={limit}")

	def test_order_is_newest_first_and_cursor_none_when_exhausted(self):
		for i in range(5):
			ev(at(9, i), ref_name=f"x{i}")
		page = ar.events(RANGE, limit=5)
		self.assertIsNone(page["next_cursor"])  # exactly `limit` rows left: nothing more
		times = [r["at"] for r in page["rows"]]
		self.assertEqual(times, sorted(times, reverse=True))
		page = ar.events(RANGE, limit=4)
		self.assertIsNotNone(page["next_cursor"])
		self.assertEqual(len(ar.events(RANGE, after=page["next_cursor"], limit=4)["rows"]), 1)

	def test_row_shape_and_source_mapping(self):
		ev(
			at(9),
			event="export",
			method="export_query",
			label="Exported",
			ref_doctype="Sales Invoice",
			rows=12,
			channel="chat",
			code="",
		)
		wr(
			at(10),
			tool="delete_doc",
			outcome="refused",
			ref_doctype="ToDo",
			ref_name="T1",
			bulk=4,
			provenance="macro",
		)
		wr(at(11), tool="some_new_write_tool", provenance="request")
		rows = ar.events(RANGE)["rows"]
		self.assertEqual([r["source"] for r in rows], ["write", "write", "event"])
		for r in rows:
			self.assertEqual(set(r), set(ar.EVENT_ROW_KEYS))
		new, deleted, exported = rows
		self.assertEqual((new["activity"], new["channel"], new["rows"]), ("write", "", 1))
		self.assertEqual(
			(deleted["activity"], deleted["method"], deleted["rows"], deleted["outcome"], deleted["channel"]),
			("delete", "delete_doc", 4, "refused", "macro"),
		)
		self.assertEqual(
			(exported["activity"], exported["rows"], exported["channel"], exported["ref_doctype"]),
			("export", 12, "chat", "Sales Invoice"),
		)

	def test_limit_clamp(self):
		self.assertEqual(ar._clamp(None, ar.MAX_LIMIT), ar.DEFAULT_LIMIT)
		self.assertEqual(ar._clamp("junk", ar.MAX_LIMIT), ar.DEFAULT_LIMIT)
		self.assertEqual(ar._clamp(0, ar.MAX_LIMIT), 1)
		self.assertEqual(ar._clamp(10**9, ar.MAX_LIMIT), 5000)
		self.assertEqual(ar._clamp(10**9, ar.EXPORT_MAX_LIMIT), 50000)

	def test_export_path_reports_truncation(self):
		for i in range(3):
			ev(at(9, i))
		cut = ar.events(RANGE, limit=2, for_export=True)
		self.assertEqual((len(cut["rows"]), cut["truncated"]), (2, True))
		whole = ar.events(RANGE, limit=5, for_export=True)
		self.assertEqual((len(whole["rows"]), whole["truncated"]), (3, False))
		self.assertNotIn("truncated", ar.events(RANGE))

	def test_bad_cursor_is_a_validation_error(self):
		with self.assertRaises(frappe.ValidationError):
			ar.events(RANGE, after="not-a-cursor")
		with self.assertRaises(frappe.ValidationError):
			ar.events(RANGE, after="|abc")

	def test_a_zero_or_null_datetime_cursor_is_a_validation_error(self):
		# get_datetime turns these into None: ``at < NULL`` would silently page to nothing.
		for cursor in ("0000-00-00 00:00:00|abc", "0001-01-01 00:00:00|abc", " |abc", "2026-01-01 10:00:00|"):
			with self.subTest(cursor=cursor), self.assertRaises(frappe.ValidationError):
				ar.events(RANGE, after=cursor)
		self.assertEqual(ar._decode_cursor(None), None)
		self.assertEqual(ar._decode_cursor("2026-01-01 10:00:00|x")[1], "x")

	def test_to_date_is_clamped_to_today_so_the_far_future_cannot_overflow(self):
		ev(at(9), ref_name="kept")
		for far in ("9999-12-31", "9999-12-30", "2999-01-01"):
			with self.subTest(to_date=far):
				got = ar.events({"from_date": str(DAY), "to_date": far})
				self.assertEqual([r["ref_name"] for r in got["rows"]], ["kept"])
		got = ar.events({"to_date": "9999-12-31"}, for_export=True)
		self.assertEqual((len(got["rows"]), got["truncated"]), (1, False))

	def test_from_date_is_clamped_to_the_horizon(self):
		horizon = ar.horizon_day()
		ev(dt.datetime.combine(horizon, dt.time(1, 0)), ref_name="at-horizon")
		ev(dt.datetime.combine(horizon - dt.timedelta(days=1), dt.time(23, 0)), ref_name="past-retention")
		got = {r["ref_name"] for r in ar.events({"from_date": "2000-01-01"})["rows"]}
		self.assertEqual(got, {"at-horizon"})  # an old straggler the purge has not reached yet
		# A range wholly outside what is kept is empty, with the page's usual shape.
		self.assertEqual(
			ar.events({"from_date": "2000-01-01", "to_date": "2000-01-31"}), {"rows": [], "next_cursor": None}
		)
		self.assertEqual(
			ar.events({"from_date": "9999-01-01"}, for_export=True),
			{"rows": [], "next_cursor": None, "truncated": False},
		)


class TestEventsFilters(ReportTestCase):
	def test_date_range_is_inclusive_of_both_days(self):
		ev(dt.datetime.combine(DAY, dt.time(0, 0, 0)), ref_name="first-second")
		ev(dt.datetime.combine(DAY, dt.time(23, 59, 59)), ref_name="last-second")
		ev(dt.datetime.combine(DAY - dt.timedelta(days=1), dt.time(23, 59, 59)), ref_name="before")
		ev(dt.datetime.combine(NEXT_DAY, dt.time(0, 0, 0)), ref_name="after")
		got = {r["ref_name"] for r in ar.events(RANGE)["rows"]}
		self.assertEqual(got, {"first-second", "last-second"})
		got = {r["ref_name"] for r in ar.events({"from_date": str(DAY)})["rows"]}
		self.assertEqual(got, {"first-second", "last-second", "after"})
		got = {r["ref_name"] for r in ar.events({"to_date": DAY})["rows"]}
		self.assertEqual(got, {"first-second", "last-second", "before"})

	def test_user_filter(self):
		ev(actor=A, ref_name="a")
		wr(actor=A, ref_name="aw")
		ev(actor=B, ref_name="b")
		ev(actor="", ref_name="nobody")
		got = {r["ref_name"] for r in ar.events({**RANGE, "user": A})["rows"]}
		self.assertEqual(got, {"a", "aw"})

	def test_activity_filter_event_kinds_verbs_and_fallback(self):
		ev(event="read", ref_name="read")
		ev(event="action", ref_name="action", method="x.y")
		wr(tool="delete_doc", ref_name="del")
		wr(tool="update_doc", ref_name="upd")
		wr(tool="brand_new_write", ref_name="fallback")
		wr(tool="export_query", outcome="refused", ref_name="refused-export")

		def refs(activity):
			return {r["ref_name"] for r in ar.events({**RANGE, "activity": activity})["rows"]}

		self.assertEqual(refs("read"), {"read"})
		self.assertEqual(refs("action"), {"action"})
		self.assertEqual(refs("delete"), {"del"})
		self.assertEqual(refs("update"), {"upd"})
		self.assertEqual(refs("write"), {"fallback"})
		self.assertEqual(refs("export"), {"refused-export"})
		self.assertEqual(refs("nonsense"), set())

	def test_outcome_filter_is_as_stored_per_source(self):
		ev(outcome="ok", ref_name="ok")
		ev(outcome="denied", ref_name="denied")
		wr(outcome="applied", ref_name="applied")
		wr(outcome="refused", ref_name="refused")
		wr(outcome="failed", ref_name="wfailed")

		def refs(outcome):
			return {r["ref_name"] for r in ar.events({**RANGE, "outcome": outcome})["rows"]}

		self.assertEqual(refs("ok"), {"ok"})
		self.assertEqual(refs("denied"), {"denied"})
		self.assertEqual(refs("applied"), {"applied"})
		self.assertEqual(refs("refused"), {"refused"})
		self.assertEqual(refs("failed"), {"wfailed"})
		self.assertEqual(refs("bogus"), set())

	def test_sql_injection_in_filters_is_inert(self):
		ev(ref_name="keep")
		got = ar.events({**RANGE, "user": "x' OR '1'='1", "activity": "read' OR 1=1 --"})
		self.assertEqual(got["rows"], [])
		with self.assertRaises(Exception):
			ar.events({"from_date": "2026-03-10'; DROP TABLE tabUser; --"})


class TestExportExclusion(ReportTestCase):
	def test_applied_export_write_rows_hidden_but_refused_and_failed_visible(self):
		for tool in sorted(EXPORT_TOOLS):
			wr(tool=tool, outcome="applied", ref_name=f"hidden-{tool}")
		wr(tool="export_query", outcome="refused", ref_name="refused")
		wr(tool="export_excel", outcome="failed", ref_name="failed")
		wr(tool="update_doc", outcome="applied", ref_name="plain")
		ev(event="export", method="export_query", rows=100, ref_name="the-event")
		got = {r["ref_name"] for r in ar.events(RANGE)["rows"]}
		self.assertEqual(got, {"refused", "failed", "plain", "the-event"})

	def test_summary_counts_exports_once_and_refused_export_as_refused(self):
		wr(tool="export_query", outcome="applied")  # the immediate export's Agent Write twin
		ev(event="export", method="export_query", rows=100)
		ev(event="export", method="export_query", rows=50)  # e.g. a background export
		p = person(self.summary(), A)
		self.assertEqual((p["exports"], p["rows_exported"], p["writes"], p["refused_denied"]), (2, 150, 0, 0))

		wr(tool="export_query", outcome="refused")
		wr(tool="export_query", outcome="failed")
		p = person(self.summary(), A)
		self.assertEqual((p["writes"], p["refused_denied"], p["failed"]), (2, 1, 1))

	def test_exclusion_does_not_hide_applied_non_export_writes(self):
		wr(tool="create_doc", outcome="applied")
		wr(tool="submit_doc", outcome="applied")
		self.assertEqual(len(ar.events(RANGE)["rows"]), 2)
		self.assertEqual(person(self.summary(), A)["writes"], 2)


class TestSummaryCounts(ReportTestCase):
	def test_counts_per_person(self):
		ev(event="read", rows=10)
		ev(event="read", rows=5)
		ev(event="read", outcome="denied")
		ev(event="export", rows=7)
		ev(event="action", method="m.a")
		ev(event="denied_call", outcome="denied", code="PermissionError")
		ev(event="read", outcome="failed")
		wr(tool="create_doc")
		wr(tool="delete_doc", bulk=3)
		wr(tool="update_doc", outcome="refused")
		wr(tool="update_doc", outcome="failed")
		wr(tool="update_doc", outcome="discarded")
		ev(actor=B, event="read", rows=1)
		p = person(self.summary(), A)
		self.assertEqual(
			{k: p[k] for k in p if k not in ("flags", "name", "user")},
			{
				"turns": 0,
				"tokens": 0,
				"reads": 2,
				"rows_read": 15,
				"exports": 1,
				"rows_exported": 7,
				"writes": 5,
				"erp_deletes": 3,
				"refused_denied": 3,  # denied read + denied_call + refused write
				"failed": 2,  # failed read + failed write
				"actions": 1,
			},
		)
		self.assertEqual(person(self.summary(), B)["reads"], 1)
		self.assertEqual(self.summary(user=B)["people"][0]["user"], B)
		self.assertEqual(len(self.summary(user=B)["people"]), 1)

	def test_turns_and_tokens_use_the_day_range(self):
		usage(A, DAY, tin=100, tout=50)
		usage(A, DAY, tin=10, tout=5)
		usage(A, NEXT_DAY, tin=999, tout=999)
		usage(B, DAY, tin=1, tout=1)
		p = person(self.summary(), A)
		self.assertEqual((p["turns"], p["tokens"]), (2, 165))
		self.assertEqual(person(self.summary(), B)["turns"], 1)

	def test_chat_history_action_deletes_are_not_erp_deletes(self):
		for i in range(60):
			ev(at(10, 0, i), event="action", method="jarvis.chat.delete_conversation", label="Deleted a chat")
		p = person(self.summary(), A)
		self.assertEqual((p["actions"], p["erp_deletes"]), (60, 0))
		self.assertNotIn("delete_burst", flags(self.summary(), A))

	def test_erp_deletes_only_count_applied_delete_rows(self):
		wr(tool="delete_doc", bulk=40)
		wr(tool="delete_doc")  # single: bulk_count 0 means one
		wr(tool="delete_doc", outcome="refused", bulk=500)
		wr(tool="update_doc", bulk=500)
		self.assertEqual(person(self.summary(), A)["erp_deletes"], 41)

	def test_no_person_bucket(self):
		ev(actor="", event="foreign_session", outcome="denied", actor_name="")
		ev(actor="", event="auth_rejected", outcome="denied", actor_name="")
		ev(actor=A)
		res = self.summary()
		nobody = person(res, "")
		self.assertEqual((nobody["name"], nobody["refused_denied"]), ("No person", 2))
		self.assertEqual(person(res, A)["name"], "act-a")

	def test_name_falls_back_to_user_record_then_id(self):
		if not frappe.db.exists("User", C):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": C,
					"first_name": "Carol",
					"last_name": "Audit",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		usage(C, tin=1)
		usage("ghost@example.com", tin=1)
		res = self.summary()
		self.assertEqual(person(res, "ghost@example.com")["name"], "ghost@example.com")
		self.assertEqual(person(res, C)["name"], "Carol Audit")

	def test_sort_flagged_first_then_by_activity(self):
		for _ in range(5):
			ev(actor=A)  # busiest, unflagged
		ev(actor=B)
		ev(at(3), actor=C)  # one off-hours event: flagged, least busy
		order = [p["user"] for p in self.summary()["people"]]
		self.assertEqual(order, [C, A, B])

	def test_people_are_capped_flagged_first_and_say_so(self):
		# D8: a site with thousands of users must not ship (and render) all of them.
		self.assertEqual(ar.PEOPLE_CAP, 500)
		for _ in range(5):
			ev(actor=A)  # busiest, unflagged
		ev(actor=B)
		ev(at(3), actor=C)  # flagged, least busy
		self.assertFalse(self.summary()["people_truncated"])
		with mock.patch.object(ar, "PEOPLE_CAP", 2):
			res = self.summary()
		self.assertEqual([p["user"] for p in res["people"]], [C, A])  # the flagged person is kept
		self.assertTrue(res["people_truncated"])
		with mock.patch.object(ar, "PEOPLE_CAP", 3):
			res = self.summary()
		self.assertEqual(len(res["people"]), 3)
		self.assertFalse(res["people_truncated"])  # exactly at the cap is not truncated

	def test_first_event_at_per_source(self):
		self.assertEqual(ar.summary(RANGE)["first_event_at"], {"agent": None, "app": None})
		ev(at(9), event="action", method="m.a")
		ev(at(10, 5), event="denied_call", outcome="denied")
		ev(at(10, 1), event="read")
		got = ar.summary(RANGE)["first_event_at"]
		self.assertEqual((got["agent"], got["app"]), (at(10, 1), at(9)))
		ev(at(7), event="export")
		ev(at(6), event="foreign_session", actor="", outcome="denied")
		self.assertEqual(ar.summary(RANGE)["first_event_at"]["agent"], at(6))

	def test_first_agent_event_is_the_earlier_of_the_events_and_the_agent_writes(self):
		# Pruning the (newer) event log must not move "recorded from" later than the writes
		# that outlive it, and an app action is not agent activity.
		ev(at(10), event="read")
		wr(at(8))
		ev(at(7), event="action", method="m.a")
		got = ar.summary(RANGE)["first_event_at"]
		self.assertEqual((got["agent"], got["app"]), (at(8), at(7)))
		frappe.db.delete("Jarvis Activity Event")
		got = ar.summary(RANGE)["first_event_at"]
		self.assertEqual((got["agent"], got["app"]), (at(8), None))
		ev(at(6), event="read")
		self.assertEqual(ar.summary(RANGE)["first_event_at"]["agent"], at(6))

	def test_logging_health(self):
		now = now_datetime()
		health = lambda: ar.summary(RANGE)["logging_health"]  # noqa: E731
		self.assertFalse(health()["warn"])  # nothing ran

		usage(A, day=now.date(), calls=0)
		self.assertFalse(health()["warn"])  # turns but no tool calls

		row = usage(A, day=now.date(), calls=3)
		h = health()
		self.assertTrue(h["warn"])
		self.assertEqual((h["tool_calls_24h"], h["events_24h"]), (3, 0))

		ev(now, event="read")
		self.assertFalse(health()["warn"])  # tool calls AND events: healthy

		frappe.db.delete("Jarvis Activity Event")
		ev(add_to_date(now, hours=-30), event="read")
		self.assertTrue(health()["warn"])  # only a stale event

		frappe.db.set_value("Jarvis Turn Usage", row.name, "creation", add_to_date(now, hours=-30))
		self.assertFalse(health()["warn"])  # tool calls too old to matter

	def test_logging_health_counts_agent_events_not_app_actions(self):
		# A broken tool-path logger must not hide behind an admin clicking around the app.
		now = now_datetime()
		usage(A, day=now.date(), calls=4)
		ev(now, event="action", method="m.a")
		h = ar.summary(RANGE)["logging_health"]
		self.assertTrue(h["warn"])
		self.assertEqual((h["tool_calls_24h"], h["events_24h"]), (4, 0))
		for kind in sorted(ar._AGENT_EVENTS):
			frappe.db.delete("Jarvis Activity Event")
			ev(now, event=kind, outcome="denied" if kind != "read" else "ok")
			h = ar.summary(RANGE)["logging_health"]
			self.assertEqual((h["warn"], h["events_24h"]), (False, 1), kind)

	# ---- D6: the app-action logger has its own check (the agent check above stays separate:
	# an admin clicking around writes app rows and must not mask a broken tool-path logger).
	def _app_health(self):
		frappe.cache().delete_keys(activity_actions.SEEN_PREFIX)
		self.addCleanup(frappe.cache().delete_keys, activity_actions.SEEN_PREFIX)

	def test_app_actions_seen_but_none_written_warns(self):
		self._app_health()
		activity_actions._note("seen")
		h = ar.summary(RANGE)["logging_health"]
		self.assertTrue(h["app_warn"])
		self.assertEqual((h["app_seen_24h"], h["app_actions_24h"], h["app_failures_24h"]), (1, 0, 0))
		self.assertIn(ar.app_warn_note(), ar.health_notes(h))
		self.assertEqual(
			ar.app_warn_note(),
			"Jarvis app actions are not being recorded. Ask your system administrator to check the activity log.",
		)

	def test_app_actions_seen_and_written_is_healthy(self):
		self._app_health()
		activity_actions._note("seen")
		ev(now_datetime(), event="action", method="jarvis.chat.macros_api.create_macro")
		h = ar.summary(RANGE)["logging_health"]
		self.assertFalse(h["app_warn"])
		self.assertEqual((h["app_seen_24h"], h["app_actions_24h"]), (1, 1))
		self.assertNotIn(ar.app_warn_note(), ar.health_notes(h))

	def test_the_app_rows_check_is_an_existence_check(self):
		# N5: "any app row in 24 h?" must not read a pile of rows to answer.
		self._app_health()
		activity_actions._note("seen")
		for i in range(3):
			ev(now_datetime(), event="action", method=f"jarvis.x.y{i}")
		h = ar.summary(RANGE)["logging_health"]
		self.assertEqual(h["app_actions_24h"], 1)
		self.assertFalse(h["app_warn"])

	def test_dropped_rows_are_reported_as_data_and_raise_no_warning(self):
		self._app_health()
		activity_actions._note("dropped")
		activity_actions._note("dropped")
		ev(now_datetime(), event="action", method="jarvis.x.y")
		h = ar.summary(RANGE)["logging_health"]
		self.assertEqual(h["app_dropped_24h"], 2)
		self.assertFalse(h["app_warn"])
		self.assertNotIn(ar.app_warn_note(), ar.health_notes(h))  # data only: no note of its own

	def test_a_quiet_app_is_not_a_warning(self):
		self._app_health()
		h = ar.summary(RANGE)["logging_health"]
		self.assertEqual((h["app_warn"], h["app_seen_24h"], h["app_failures_24h"]), (False, 0, 0))

	def test_failures_with_nothing_written_warn_even_if_nothing_was_seen(self):
		# a snapshot failure happens before a request is "seen": it must still raise the alarm
		self._app_health()
		activity_actions._note("failures")
		h = ar.summary(RANGE)["logging_health"]
		self.assertTrue(h["app_warn"])
		self.assertEqual(h["app_failures_24h"], 1)

	def test_tool_path_rows_do_not_count_as_app_actions(self):
		self._app_health()
		activity_actions._note("seen")
		ev(now_datetime(), event="read", method="get_list")  # a tool name has no dot
		self.assertTrue(ar.summary(RANGE)["logging_health"]["app_warn"])

	def test_an_old_app_row_does_not_count(self):
		self._app_health()
		activity_actions._note("seen")
		ev(add_to_date(now_datetime(), hours=-30), event="action", method="jarvis.x.y")
		self.assertTrue(ar.summary(RANGE)["logging_health"]["app_warn"])

	def test_an_unreadable_counter_store_never_fails_the_summary_or_warns(self):
		self._app_health()
		with mock.patch.object(activity_actions, "_cache", side_effect=ConnectionError("down")):
			h = ar.summary(RANGE)["logging_health"]
		self.assertEqual((h["app_warn"], h["app_seen_24h"], h["app_failures_24h"]), (False, 0, 0))

	def test_horizon_days_matches_the_retention_hooks(self):
		# The report reaches back as far as the two logs are kept: one number, three places.
		from jarvis import hooks

		kept = hooks.default_log_clearing_doctypes
		self.assertEqual(ar.HORIZON_DAYS, kept["Jarvis Activity Event"])
		self.assertEqual(ar.HORIZON_DAYS, kept["Jarvis Activity Daily"])

	def test_thresholds_exposed(self):
		t = ar.summary(RANGE)["thresholds"]
		self.assertEqual(
			t,
			{
				"delete_burst_count": 50,
				"delete_burst_minutes": 60,
				"big_export_rows": 50000,
				"daily_export_rows": 100000,
				"refusals_per_day": 5,
				"business_hours_start": 8,
				"business_hours_end": 20,
			},
		)


class TestFlagBoundaries(ReportTestCase):
	def deletes(self, n, start=None, step_s=10, actor=A, tool="delete_doc"):
		start = start or at(10)
		for i in range(n):
			wr(start + dt.timedelta(seconds=i * step_s), tool=tool, actor=actor)

	def test_delete_burst_49_vs_50(self):
		self.deletes(49)
		self.assertNotIn("delete_burst", flags(self.summary(), A))
		self.deletes(1, start=at(10, 30))
		got = flags(self.summary(), A)
		self.assertEqual(got["delete_burst"], 50)
		label = next(f["label"] for f in person(self.summary(), A)["flags"] if f["key"] == "delete_burst")
		self.assertEqual(label, "50 deletes in 1 hour")

	def test_delete_burst_sums_bulk_count(self):
		wr(at(10), tool="delete_doc", bulk=49)
		self.assertNotIn("delete_burst", flags(self.summary(), A))
		wr(at(10, 20), tool="delete_doc")
		self.assertEqual(flags(self.summary(), A)["delete_burst"], 50)
		wr(at(10, 40), tool="delete_doc", bulk=12)
		self.assertEqual(flags(self.summary(), A)["delete_burst"], 62)

	def test_delete_burst_window_edges(self):
		self.deletes(25, start=at(10, 0, 0), step_s=0)
		self.deletes(25, start=at(11, 0, 1), step_s=0)  # 60 min 1 s later: outside the window
		self.assertNotIn("delete_burst", flags(self.summary(), A))
		frappe.db.delete("Jarvis Agent Write")
		self.deletes(25, start=at(10, 0, 0), step_s=0)
		self.deletes(25, start=at(11, 0, 0), step_s=0)  # exactly 60 min later: inside
		self.assertEqual(flags(self.summary(), A)["delete_burst"], 50)

	def test_delete_burst_ignores_other_people_refused_and_non_delete(self):
		self.deletes(30, actor=A)
		self.deletes(30, actor=B, start=at(10, 5))
		for i in range(30):
			wr(at(10, 0, i), tool="delete_doc", outcome="refused")
			wr(at(10, 0, i), tool="update_doc")
		res = self.summary()
		self.assertNotIn("delete_burst", flags(res, A))
		self.assertNotIn("delete_burst", flags(res, B))

	def test_delete_burst_straddling_from_date_counts_rows_before_it(self):
		before = dt.datetime.combine(DAY, dt.time(0, 0)) - dt.timedelta(minutes=30)
		self.deletes(30, start=before, step_s=1)  # 23:30 the day before
		self.deletes(20, start=at(0, 10), step_s=1)  # 00:10 on from_date
		got = self.summary()  # from_date = DAY
		self.assertEqual(flags(got, A)["delete_burst"], 50)
		# but the 30 early rows alone are not in range and never flag by themselves
		frappe.db.delete("Jarvis Agent Write")
		self.deletes(30, start=before, step_s=1)
		self.deletes(19, start=at(0, 10), step_s=1)
		self.assertNotIn("delete_burst", flags(self.summary(), A))
		frappe.db.delete("Jarvis Agent Write")
		self.deletes(60, start=before - dt.timedelta(hours=3), step_s=1)  # burst entirely before the range
		self.assertNotIn("delete_burst", flags(self.summary(), A))

	def test_delete_burst_beyond_to_date_is_ignored(self):
		self.deletes(60, start=dt.datetime.combine(NEXT_DAY, dt.time(10, 0)))
		self.assertNotIn("delete_burst", flags(self.summary(), A))

	def test_big_export_49999_vs_50000(self):
		ev(event="export", rows=49_999)
		self.assertNotIn("big_export", flags(self.summary(), A))
		ev(event="export", rows=50_000)
		got = flags(self.summary(), A)
		self.assertEqual(got["big_export"], 50_000)
		label = next(f["label"] for f in person(self.summary(), A)["flags"] if f["key"] == "big_export")
		self.assertEqual(label, "One export of 50,000 rows")

	def test_big_export_ignores_non_ok_and_reads(self):
		ev(event="export", rows=60_000, outcome="failed")
		ev(event="read", rows=60_000)
		self.assertNotIn("big_export", flags(self.summary(), A))

	def test_daily_export_99999_vs_100000(self):
		ev(at(9), event="export", rows=40_000)
		ev(at(10), event="export", rows=40_000)
		ev(at(11), event="export", rows=19_999)
		self.assertNotIn("daily_export", flags(self.summary(), A))
		frappe.db.delete("Jarvis Activity Event")
		ev(at(9), event="export", rows=40_000)
		ev(at(10), event="export", rows=40_000)
		ev(at(11), event="export", rows=20_000)
		got = flags(self.summary(), A)
		self.assertEqual(got["daily_export"], 100_000)
		self.assertNotIn("big_export", got)

	def test_daily_export_is_per_calendar_day(self):
		ev(at(23, 0), event="export", rows=45_000)
		ev(at(23, 30), event="export", rows=45_000)
		ev(at(0, 30, day=NEXT_DAY), event="export", rows=45_000)
		ev(at(1, 30, day=NEXT_DAY), event="export", rows=45_000)
		res = ar.summary({"from_date": str(DAY), "to_date": str(NEXT_DAY)})
		self.assertNotIn("daily_export", flags(res, A))  # 90,000 + 90,000 across midnight

	def test_refusals_4_vs_5_per_day_across_both_sources(self):
		ev(at(9), event="denied_call", outcome="denied")
		ev(at(9, 1), event="read", outcome="denied")
		wr(at(9, 2), tool="update_doc", outcome="refused")
		wr(at(9, 3), tool="export_query", outcome="refused")
		self.assertNotIn("refusals", flags(self.summary(), A))
		wr(at(17), tool="delete_doc", outcome="refused")
		got = flags(self.summary(), A)
		self.assertEqual(got["refusals"], 5)
		label = next(f["label"] for f in person(self.summary(), A)["flags"] if f["key"] == "refusals")
		self.assertEqual(label, "5 refused in one day")

	def test_refusals_split_over_two_days_do_not_flag(self):
		for i in range(3):
			ev(at(9, i), event="denied_call", outcome="denied")
			ev(at(9, i, day=NEXT_DAY), event="denied_call", outcome="denied")
		res = ar.summary({"from_date": str(DAY), "to_date": str(NEXT_DAY)})
		self.assertNotIn("refusals", flags(res, A))
		self.assertEqual(person(res, A)["refused_denied"], 6)

	def test_failures_are_not_refusals(self):
		for i in range(6):
			ev(at(9, i), outcome="failed")
			wr(at(9, i), outcome="failed")
		self.assertNotIn("refusals", flags(self.summary(), A))

	def test_off_hours_boundaries(self):
		cases = {
			A: (at(7, 59), True),
			B: (at(8, 0), False),
			C: (at(19, 59), False),
			"act-d@example.com": (at(20, 0), True),
			"act-e@example.com": (at(0, 0), True),
			"act-f@example.com": (at(23, 59, 59), True),
		}
		for user, (when, _flagged) in cases.items():
			ev(when, actor=user)
		res = self.summary()
		for user, (when, flagged) in cases.items():
			self.assertEqual("off_hours" in flags(res, user), flagged, f"{user} at {when.time()}")

	def test_off_hours_counts_events_and_writes_and_labels_the_number(self):
		ev(at(2), event="read")
		ev(at(21), event="action", method="m.a")
		wr(at(22), tool="update_doc")
		wr(at(22), tool="export_query")  # applied export twin: not counted again
		ev(at(10))
		got = flags(self.summary(), A)
		self.assertEqual(got["off_hours"], 3)
		label = next(f["label"] for f in person(self.summary(), A)["flags"] if f["key"] == "off_hours")
		self.assertEqual(label, "3 events outside 08:00 to 20:00")

	def test_flag_labels_are_translated_as_whole_sentence_templates(self):
		# Each label is ONE ``_()`` template with the numbers as arguments (a translator
		# must be able to reorder), and the singular/plural are separate templates.
		def marked(template):
			return f"[{template}]"

		self.deletes(60)
		ev(at(3), event="export", rows=120_000)
		ev(at(4), event="export", rows=1_000)  # a 2nd off-hours event: plural template
		for i in range(5):
			ev(at(11, i), event="denied_call", outcome="denied")
		with mock.patch.object(ar, "_", marked):
			labels = {f["key"]: f["label"] for f in person(self.summary(), A)["flags"]}
		self.assertEqual(labels["delete_burst"], "[{0} deletes in 1 hour]".format("60"))
		self.assertEqual(labels["big_export"], "[One export of {0} rows]".format("120,000"))
		self.assertEqual(labels["daily_export"], "[{0} rows exported in one day]".format("121,000"))
		self.assertEqual(labels["refusals"], "[{0} refused in one day]".format(5))
		self.assertEqual(labels["off_hours"], "[{0} events outside {1} to {2}]".format(2, "08:00", "20:00"))
		frappe.db.delete("Jarvis Activity Event")
		ev(at(3), event="read")
		with mock.patch.object(ar, "_", marked):
			labels = {f["key"]: f["label"] for f in person(self.summary(), A)["flags"]}
		self.assertEqual(labels["off_hours"], "[{0} event outside {1} to {2}]".format(1, "08:00", "20:00"))
		self.assertNotIn("1 events", labels["off_hours"])

	def test_flag_labels_have_no_em_dash(self):
		self.deletes(60)
		ev(at(3), event="export", rows=120_000)
		for p in self.summary()["people"]:
			for f in p["flags"]:
				self.assertEqual(set(f), set(ar.FLAG_KEYS))
				self.assertNotIn("\u2014", f["label"])


class TestGateAndAllowlists(ReportTestCase):
	def _user(self, email="act-plain@example.com"):
		# One address per test: roles added in one test must not leak into another
		# (the class-level rollback is the only isolation).
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": "Plain", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		return email

	def test_non_admin_is_refused_everywhere(self):
		ev()
		frappe.set_user(self._user())
		with self.assertRaises(frappe.PermissionError):
			ar.events(RANGE)
		with self.assertRaises(frappe.PermissionError):
			ar.events(RANGE, for_export=True)
		with self.assertRaises(frappe.PermissionError):
			ar.summary(RANGE)

	def test_jarvis_admin_role_is_enough(self):
		email = self._user("act-admin@example.com")
		frappe.get_doc("User", email).add_roles("Jarvis Admin")
		ev()
		frappe.set_user(email)
		self.assertEqual(len(ar.events(RANGE)["rows"]), 1)
		self.assertTrue(ar.summary(RANGE)["people"])

	def test_output_contains_only_allowlisted_keys(self):
		ev(rows=3, ref_name="r", ref_doctype="ToDo", channel="chat", code="X")
		wr(tool="delete_doc", bulk=2, ref_name="w", provenance_name="a free label")
		usage(A, tin=3, tout=4)
		for r in ar.events(RANGE)["rows"]:
			self.assertEqual(set(r), set(ar.EVENT_ROW_KEYS))
		res = self.summary()
		self.assertEqual(
			set(res),
			{
				"people",
				"people_truncated",
				"first_event_at",
				"logging_health",
				"thresholds",
				"stale_days",
				"clamped",
				"partial",
				"excluded_days",
				"preparing",
			},
		)
		for p in res["people"]:
			self.assertEqual(set(p), set(ar.PERSON_KEYS))
		self.assertEqual(set(res["first_event_at"]), {"agent", "app"})
		self.assertEqual(
			set(res["logging_health"]),
			{
				"warn",
				"tool_calls_24h",
				"events_24h",
				"rollup_lag_days",
				"scheduler_disabled",
				"app_warn",
				"app_seen_24h",
				"app_actions_24h",
				"app_failures_24h",
				"app_dropped_24h",
			},
		)
		blob = repr(res) + repr(ar.events(RANGE))
		for forbidden in (
			"tool_args",
			"tool_result",
			"content",
			"message",
			"provenance_name",
			"a free label",
		):
			self.assertNotIn(forbidden, blob)

	def test_source_field_lists_are_metadata_only(self):
		forbidden = {"tool_args", "tool_result", "content", "error", "message", "text", "args", "result"}
		self.assertFalse(forbidden & set(ar._EVENT_FIELDS))
		self.assertFalse(forbidden & set(ar._WRITE_FIELDS))
		self.assertTrue(set(ar._WRITE_FIELDS) <= set(AGENT_WRITE_FIELDS))
		meta = {f.fieldname for f in frappe.get_meta("Jarvis Activity Event").fields}
		self.assertTrue(set(ar._EVENT_FIELDS) - {"name"} <= meta)

	def test_verb_map_names_real_write_tools(self):
		from jarvis.api import _WRITE_TOOLS

		self.assertEqual(set(ar._TOOL_VERBS) - set(_WRITE_TOOLS), set())
		self.assertTrue(EXPORT_TOOLS <= set(ar._TOOL_VERBS))
		self.assertEqual(ar._DELETE_TOOLS, ("delete_doc",))
