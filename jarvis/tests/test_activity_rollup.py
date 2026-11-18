"""Tests for the user-activity rollup (plan rev 4, D13): ``jarvis.activity_rollup``,
the ``Jarvis Activity Daily`` / ``Jarvis Activity Rollup`` doctypes, and the summary's
split read path (rolled days from the daily rows, later days live).

The load-bearing test is the equivalence one: for every (from, to) sub-range of a
10-day fixture, with a mix of rolled and unrolled days and with and without a user
filter, the split summary must equal the all-live summary. Everything else pins one
guard each (watermark order, horizon, grace window, ...) so a mutation of it fails
a named test.

Isolation: the rollup commits per day in production; here ``frappe.db.commit`` is a
mock (a real commit would leak fixtures past the class-level rollback) and a failing
day rolls back to its own savepoint. The clock the rollup and the summary share
(``activity_report.now``) is pinned to noon today unless a test moves it."""

import datetime as dt
import itertools
import unittest
from unittest import mock

import frappe
from frappe.utils import add_days, getdate

from jarvis import activity_report as ar
from jarvis import activity_rollup as ru
from jarvis.jarvis.doctype.jarvis_activity_daily.jarvis_activity_daily import JarvisActivityDaily
from jarvis.jarvis.doctype.jarvis_trigger.jarvis_trigger import DENYLISTED_DOCTYPES
from jarvis.tests import test_activity_report as base

DAILY = "Jarvis Activity Daily"
ROLLUP = "Jarvis Activity Rollup"
A, B = base.A, base.B
GHOST = "act-ghost@example.com"  # a user with no User record: named from the snapshot
GHOST2 = "act-ghost2@example.com"
TODAY = None  # set per test by RollupCase.setUp (ar.today()), never at import


def d(n):
	"""``n`` days before today."""
	return TODAY - dt.timedelta(days=n)


def tm(day, h=10, m=0, s=0, us=0):
	return dt.datetime.combine(day, dt.time(h, m, s, us))


# Independent literal (never imported from the code under test): the only columns the
# daily table may carry. Counters and keys, no content.
_DAILY_FIELDS = {
	"day",
	"actor",
	"actor_name",
	"turns",
	"tokens",
	"reads",
	"rows_read",
	"exports",
	"rows_exported",
	"max_export_rows",
	"actions",
	"denied",
	"failed",
	"writes",
	"refused",
	"erp_deletes",
	"off_hours",
	"through",
}


def usage_at(user, when, tin=0, tout=0):
	"""A Turn Usage row for ``when``'s day with its ``creation`` set to ``when`` (the real
	row is stamped with both at insert, chat/usage.py; the test clock is not the DB's)."""
	doc = base.usage(user, when.date(), tin=tin, tout=tout)
	frappe.db.set_value("Jarvis Turn Usage", doc.name, "creation", when, update_modified=False)
	return doc


def today_fixture():
	"""Today's rows spread over the whole day, with rows either side of every cut-off the
	tests use, every counter kind, the boundary instants and a delete burst."""
	ev, wr = base.ev, base.wr
	ev(tm(TODAY, 0, 0, 0), event="read", actor=A, rows=3)  # off hours, the first instant
	ev(tm(TODAY, 6, 30), event="read", actor=A, rows=4)  # off hours
	ev(tm(TODAY, 9, 0), event="export", actor=B, rows=60_000)  # big export (max, not sum)
	ev(tm(TODAY, 9, 30), event="export", actor=B, rows=10)
	ev(tm(TODAY, 10, 30), event="action", actor=A, method="m.act")
	ev(tm(TODAY, 10, 59, 59, 999999), event="read", actor=GHOST, actor_name="Ghost Person", rows=2)
	ev(tm(TODAY, 11, 0, 0), event="read", actor=A, rows=5)  # exactly H = 11:00: live side
	ev(tm(TODAY, 11, 30), event="export", actor=B, rows=70_000)  # a bigger export after H
	ev(tm(TODAY, 11, 40), event="foreign_session", actor="", outcome="denied", actor_name="")
	ev(tm(TODAY, 13, 0), event="denied_call", actor=A, outcome="denied", code="PermissionError")
	ev(tm(TODAY, 20, 30), event="read", actor=B, rows=1)  # off hours, late
	ev(tm(TODAY, 23, 59, 59, 999999), event="read", actor=A, rows=1)  # the last instant
	wr(tm(TODAY, 8, 15), tool="delete_doc", actor=A, bulk=30)  # a burst across the cut-off
	wr(tm(TODAY, 11, 20), tool="delete_doc", actor=A, bulk=30)
	wr(tm(TODAY, 11, 45), tool="update_doc", outcome="refused", actor=A)
	wr(tm(TODAY, 12, 0), tool="create_doc", actor=GHOST2, actor_name="Ghost Two")
	wr(tm(TODAY, 3, 0), tool="export_query", outcome="applied", actor=B)  # hidden by its event
	usage_at(A, tm(TODAY, 0, 5), tin=100, tout=50)
	usage_at(A, tm(TODAY, 10, 59, 59), tin=10, tout=5)
	usage_at(A, tm(TODAY, 11, 0), tin=1, tout=2)  # exactly H: live side
	usage_at(B, tm(TODAY, 15), tin=7, tout=3)
	usage_at("", tm(TODAY, 3), tin=4, tout=4)


def stored_days():
	return frappe.db.sql(f"select distinct `day` from `tab{DAILY}` order by `day`", pluck=True)


def stored_watermark():
	return ar.watermark()


def set_watermark(day):
	frappe.db.delete("Singles", {"doctype": ROLLUP})
	if day:
		frappe.db.set_single_value(ROLLUP, "rolled_through", day, update_modified=False)


def daily_rows(**where):
	return frappe.get_all(DAILY, filters=where, fields=["*"], order_by="day asc, actor asc")


class RollupCase(base.ReportTestCase):
	"""Pins the clock to noon today and makes commit a no-op (see the module docstring)."""

	def setUp(self):
		super().setUp()
		global TODAY
		TODAY = ar.today()  # read per test, not at import (a long-lived runner crosses midnight)
		self.clock = self._patch("jarvis.activity_report.now", return_value=tm(TODAY, 12))
		self.commit = self._patch("frappe.db.commit")
		# A failing day writes an Error Log (once a day): never a real row in these tests.
		self.log_error = self._patch("frappe.log_error")
		self.cleanup_locks()
		self.addCleanup(self.cleanup_locks)

	def _patch(self, target, **kw):
		p = mock.patch(target, **kw)
		m = p.start()
		self.addCleanup(p.stop)
		return m

	def lift_cap(self):
		"""For tests about something else than the cap: compute any number of unrolled days
		live (the production cap would leave the older ones out of the totals)."""
		p = mock.patch.object(ar, "LIVE_DAY_CAP", 10_000)
		p.start()
		self.addCleanup(p.stop)

	def short_horizon(self, days=12):
		p = mock.patch.object(ar, "HORIZON_DAYS", days)
		p.start()
		self.addCleanup(p.stop)

	@staticmethod
	def cleanup_locks():
		cache = frappe.cache()
		cache.delete(cache.make_key(ru.LOCK_KEY))
		for n in range(-2, 3):  # the once-a-day failure throttle, for the days a test may use
			cache.delete(cache.make_key(f"{ru.FAILURE_KEY}:{dt.date.today() + dt.timedelta(days=n)}"))

	def at_time(self, when):
		self.clock.return_value = when

	def counted(self):
		"""Wrap ``day_counters`` so a test can see (and drive) every day the rollup computes."""
		calls = []
		real = ar.day_counters

		def spy(first, last, user=None, **kw):
			calls.append(first)
			return real(first, last, user, **kw)

		p = mock.patch.object(ar, "day_counters", spy)
		p.start()
		self.addCleanup(p.stop)
		return calls

	def people(self, frm, to, user=None):
		filters = {"from_date": str(frm), "to_date": str(to)}
		if user:
			filters["user"] = user
		return ar.summary(filters)


# --------------------------------------------------------------------------- doctypes


class TestDailyDoctype(RollupCase):
	def test_schema_is_counters_and_keys_only(self):
		fields = {f.fieldname for f in frappe.get_meta(DAILY).fields}
		self.assertEqual(fields, _DAILY_FIELDS)
		meta = frappe.get_meta(DAILY)
		self.assertEqual(meta.get_field("actor").fieldtype, "Link")
		self.assertEqual(meta.get_field("actor").options, "User")
		self.assertEqual(meta.get_field("day").fieldtype, "Date")
		self.assertTrue(meta.get_field("day").search_index)

	def test_rollup_single_is_one_date_field_and_sm_read_only(self):
		meta = frappe.get_meta(ROLLUP)
		self.assertTrue(meta.issingle)
		self.assertEqual([(f.fieldname, f.fieldtype) for f in meta.fields], [("rolled_through", "Date")])
		self.assertEqual(
			[(p.role, p.read, p.write, p.create) for p in meta.permissions], [("System Manager", 1, 0, 0)]
		)

	def test_permissions_are_read_only_for_every_role(self):
		perms = frappe.get_meta(DAILY).permissions
		self.assertEqual({p.role for p in perms}, {"System Manager", "Jarvis Admin"})
		for p in perms:
			self.assertTrue(p.read and p.report and p.export)
			for right in (
				"create",
				"write",
				"delete",
				"submit",
				"cancel",
				"amend",
				"import",
				"share",
				"email",
			):
				self.assertFalse(p.get(right), msg=f"{p.role} holds {right}")

	def test_unique_day_actor(self):
		idx = frappe.db.sql(f"SHOW INDEX FROM `tab{DAILY}` WHERE Key_name = 'day_actor'", as_dict=True)
		self.assertEqual(
			[r["Column_name"] for r in sorted(idx, key=lambda r: r["Seq_in_index"])], ["day", "actor"]
		)
		self.assertTrue(all(r["Non_unique"] == 0 for r in idx))
		frappe.db.bulk_insert(DAILY, ["name", "day", "actor"], [("dup-1", d(3), A)])
		frappe.db.savepoint("dup_check")
		with self.assertRaises(Exception):
			frappe.db.bulk_insert(DAILY, ["name", "day", "actor"], [("dup-2", d(3), A)])
		frappe.db.rollback(save_point="dup_check")
		self.assertEqual(len(daily_rows(day=d(3), actor=A)), 1)

	def test_summary_cover_index_is_gone_from_the_doctype_hook(self):
		import inspect

		from jarvis.jarvis.doctype.jarvis_activity_event import jarvis_activity_event as mod

		source = inspect.getsource(mod.on_doctype_update)
		self.assertIn("actor_at_index", source)
		self.assertIn("event_at_index", source)
		self.assertNotIn("summary_cover_index", source)
		self.assertNotIn('"outcome", "row_count"', source)

	def test_clear_old_logs_prunes_days_past_retention(self):
		rows = [(f"old-{n}", d(n), A) for n in (91, 90, 89)] + [("new", TODAY, A)]
		frappe.db.bulk_insert(DAILY, ["name", "day", "actor"], rows)
		JarvisActivityDaily.clear_old_logs(days=90)
		kept = {r.name for r in daily_rows()}
		self.assertEqual(kept, {"old-90", "old-89", "new"})  # only a day older than today - 90 goes

	def test_hook_registration(self):
		hooks = frappe.get_hooks
		self.assertEqual(hooks("default_log_clearing_doctypes").get(DAILY), [90])
		self.assertTrue(callable(JarvisActivityDaily.clear_old_logs))
		self.assertIn(DAILY, hooks("ignore_links_on_delete"))
		self.assertIn("jarvis.activity_rollup.rollup_days", hooks("scheduler_events")["hourly_long"])
		self.assertTrue(callable(frappe.get_attr("jarvis.activity_rollup.rollup_days")))
		self.assertIn(DAILY, DENYLISTED_DOCTYPES)

	def test_lock_key_is_persistent_and_site_prefixed(self):
		cache = frappe.cache()
		key = cache.make_key(ru.LOCK_KEY)
		self.assertTrue(key.startswith(f"{frappe.local.conf.get('db_name')}|".encode()))
		persistent = [cache.make_key(k) for k in frappe.get_hooks("persistent_cache_keys")]
		self.assertTrue(any(key.startswith(p) for p in persistent), "the claim key must be persistent")
		# ...and it really survives a full cache clear, which is what the hook is for.
		self.assertTrue(ru._redis_lock.claim(ru.LOCK_KEY, 60))
		frappe.clear_cache()
		self.assertTrue(cache.exists(ru.LOCK_KEY))
		self.assertFalse(ru._redis_lock.claim(ru.LOCK_KEY, 60))

	def test_seed_patch_inserts_an_empty_row_once(self):
		from jarvis.patches import v2_32_seed_activity_rollup as patch

		frappe.db.delete("Singles", {"doctype": ROLLUP})
		patch.execute()
		patch.execute()
		rows = frappe.db.sql(
			"select `value` from `tabSingles` where `doctype` = %s and `field` = 'rolled_through'", (ROLLUP,)
		)
		self.assertEqual(len(rows), 1)
		self.assertFalse(rows[0][0])
		self.assertIsNone(stored_watermark())
		set_watermark(d(2))
		patch.execute()  # never clobbers a real watermark
		self.assertEqual(stored_watermark(), d(2))

	def test_patch_is_registered_post_model_sync(self):
		import pathlib

		text = pathlib.Path(frappe.get_app_path("jarvis", "patches.txt")).read_text()
		pre, _, post = text.partition("[post_model_sync]")
		self.assertIn("jarvis.patches.v2_32_seed_activity_rollup", post)
		self.assertNotIn("v2_32_seed_activity_rollup", pre)


# --------------------------------------------------------------------------- the rollup


def build_fixture():
	"""Ten days (d(9) .. today) with every edge the plan lists. Returns nothing: the
	rows are the fixture. Days are named by their age so a failure message reads well."""
	ev, wr, usage = base.ev, base.wr, base.usage

	# d(9): reads; events at the very first and very last instant of the day (off hours both)
	for rows in (10, 20, 30):
		ev(tm(d(9), 10, 0, rows), event="read", actor=A, rows=rows)
	ev(tm(d(9), 0, 0, 0), event="action", actor=A, method="m.act")
	ev(tm(d(9), 23, 59, 59, 999999), event="read", actor=A, rows=5)
	ev(tm(d(8), 0, 0, 0), event="read", actor=A, rows=7)  # belongs to d(8), not d(9)

	# d(8): a big export (flag), five denied calls (refusal flag), a refused write
	ev(tm(d(8), 9), event="export", actor=B, rows=50_000)
	for i in range(5):
		ev(tm(d(8), 9, i), event="denied_call", actor=A, outcome="denied", code="PermissionError")
	wr(tm(d(8), 11), tool="update_doc", outcome="refused", actor=A)

	# d(7): two exports adding up to the daily threshold
	ev(tm(d(7), 9), event="export", actor=A, rows=45_000)
	ev(tm(d(7), 10), event="export", actor=A, rows=55_000)

	# d(6) / d(5): a delete burst crossing midnight between the two days
	wr(tm(d(6), 23, 40), tool="delete_doc", actor=A, bulk=20)
	wr(tm(d(6), 23, 50), tool="delete_doc", actor=A, bulk=20)
	wr(tm(d(5), 0, 10), tool="delete_doc", actor=A, bulk=20)
	wr(tm(d(5), 11), tool="delete_doc", actor=A, outcome="refused", bulk=500)

	# d(4): applied / refused / failed exports (only the applied Agent Write row is hidden),
	# a deleted user named from the Activity Event snapshot, another named from a write
	wr(tm(d(4), 10), tool="export_query", outcome="applied", actor=B)
	wr(tm(d(4), 10, 1), tool="export_query", outcome="refused", actor=B)
	wr(tm(d(4), 10, 2), tool="export_query", outcome="failed", actor=B)
	ev(tm(d(4), 10, 3), event="export", actor=B, rows=10)
	ev(tm(d(4), 11), event="read", actor=GHOST, actor_name="Ghost Person", rows=2)
	wr(tm(d(4), 12), tool="create_doc", actor=GHOST2, actor_name="Ghost Two")

	# d(3): Turn Usage whose `day` differs from DATE(creation) (creation is now), and the
	# No-person bucket's usage
	usage(A, d(3), tin=100, tout=50, calls=2)
	usage(A, d(3), tin=10, tout=5)
	usage(B, d(3), tin=1, tout=1)
	usage("", d(3), tin=7, tout=3)

	# d(2): No-person events, failures, write outcomes
	ev(tm(d(2), 9), event="foreign_session", actor="", outcome="denied", actor_name="")
	ev(tm(d(2), 9, 1), event="auth_rejected", actor="", outcome="denied", actor_name="")
	ev(tm(d(2), 9, 2), event="read", actor=A, outcome="failed")
	for outcome in ("failed", "discarded", "applied", "partial"):
		wr(tm(d(2), 12), tool="update_doc", outcome=outcome, actor=A)
	wr(tm(d(2), 13), tool="delete_doc", outcome="applied", actor=A)  # single: bulk_count 0 means one

	# d(1): the off-hours clock boundaries, and chat-history deletes (actions, not ERP deletes)
	for h, m, s in ((7, 59, 59), (8, 0, 0), (19, 59, 59), (20, 0, 0)):
		ev(tm(d(1), h, m, s), event="read", actor=B, rows=1)
	for i in range(60):
		ev(tm(d(1), 14, 0, i), event="action", actor=A, method="jarvis.chat.delete_conversation")

	# today (never rolled: always live)
	ev(tm(TODAY, 10), event="read", actor=A, rows=4)
	ev(tm(TODAY, 10, 5), event="action", actor=B, method="m.act")
	usage(A, TODAY, tin=2, tout=2)


class TestRollup(RollupCase):
	def test_rolls_the_fixture_into_exact_rows(self):
		self.short_horizon()
		build_fixture()
		written = ru.rollup_days()
		self.assertEqual(written, [d(n) for n in range(11, 0, -1)])
		self.assertEqual(stored_watermark(), d(1))
		self.assertEqual(self.commit.call_count, 12)  # one commit per day (the watermark moves in it) + today
		self.assertEqual(len(frappe.db.sql(f"select 1 from `tab{DAILY}` where actor is null")), 0)

		row = daily_rows(day=d(9), actor=A)[0]
		self.assertEqual((row.reads, row.rows_read, row.actions, row.off_hours), (4, 65, 1, 2))
		self.assertEqual(row.actor_name, "act-a")
		# The two clock-edge events belong to their own day, not the neighbour's.
		self.assertEqual(daily_rows(day=d(8), actor=A)[0].reads, 1)

		self.assertEqual(daily_rows(day=d(7), actor=A)[0].rows_exported, 100_000)
		self.assertEqual(daily_rows(day=d(7), actor=A)[0].max_export_rows, 55_000)
		self.assertEqual(daily_rows(day=d(8), actor=A)[0].denied, 5)
		self.assertEqual(daily_rows(day=d(8), actor=A)[0].refused, 1)
		self.assertEqual(daily_rows(day=d(6), actor=A)[0].erp_deletes, 40)
		self.assertEqual(daily_rows(day=d(5), actor=A)[0].erp_deletes, 20)  # the refused 500 is not counted

		b4 = daily_rows(day=d(4), actor=B)[0]  # applied export write hidden; refused + failed visible
		self.assertEqual((b4.writes, b4.refused, b4.failed, b4.exports), (2, 1, 1, 1))
		self.assertEqual(daily_rows(day=d(4), actor=GHOST)[0].actor_name, "Ghost Person")
		self.assertEqual(daily_rows(day=d(4), actor=GHOST2)[0].actor_name, "Ghost Two")

		a3 = daily_rows(day=d(3), actor=A)[0]
		self.assertEqual((a3.turns, a3.tokens), (2, 165))
		nobody = daily_rows(day=d(3), actor="")[0]
		self.assertEqual((nobody.turns, nobody.tokens), (1, 10))
		self.assertEqual(daily_rows(day=d(2), actor="")[0].denied, 2)

		a2 = daily_rows(day=d(2), actor=A)[0]
		self.assertEqual((a2.writes, a2.failed, a2.erp_deletes), (5, 2, 1))
		self.assertEqual(daily_rows(day=d(1), actor=B)[0].off_hours, 2)  # 07:59:59 and 20:00:00
		self.assertEqual(daily_rows(day=d(1), actor=A)[0].actions, 60)
		self.assertEqual(daily_rows(day=d(1), actor=A)[0].erp_deletes, 0)
		# Today is rolled only through the last full hour (clock 12:00 -> 11:00), and the
		# watermark never moves for it; closed days carry their own end as `through`.
		self.assertEqual({r.through for r in daily_rows(day=TODAY)}, {tm(TODAY, 11)})
		self.assertEqual({r.through for r in daily_rows(day=d(9))}, {tm(d(8), 0)})
		self.assertEqual({r.through for r in daily_rows(day=d(1))}, {tm(TODAY, 0)})

	def test_rerun_is_idempotent(self):
		self.short_horizon()
		build_fixture()
		ru.rollup_days()
		first = [(r.day, r.actor, r.actor_name, *(r[k] for k in ar.COUNTER_KEYS)) for r in daily_rows()]
		self.assertTrue(first)
		calls = self.counted()
		self.assertEqual(ru.rollup_days(), [d(1)])  # caught up: yesterday re-rolled (late rows), nothing new
		self.assertEqual(calls, [d(1), TODAY])  # then today's partial roll (rolls every run)
		# Force a full re-roll of every day (watermark reset): same rows, no duplicates.
		set_watermark(None)
		self.assertEqual(len(ru.rollup_days()), 11)
		again = [(r.day, r.actor, r.actor_name, *(r[k] for k in ar.COUNTER_KEYS)) for r in daily_rows()]
		self.assertEqual(again, first)
		self.assertEqual(stored_watermark(), d(1))

	def test_days_roll_in_order_and_a_failure_stops_the_run(self):
		self.short_horizon(8)  # d(7) .. d(1)
		build_fixture()
		bad = d(4)
		real = ar.day_counters
		seen = []

		def flaky(first, last, user=None, **kw):
			seen.append(first)
			if first == bad:
				raise RuntimeError("secret row text")
			return real(first, last, user, **kw)

		logger = self._patch("frappe.logger")
		with mock.patch.object(ar, "day_counters", flaky):
			written = ru.rollup_days()
		self.assertEqual(seen, [d(7), d(6), d(5), d(4)])  # oldest first, stopped at the failure
		self.assertEqual(written, [d(7), d(6), d(5)])
		self.assertEqual(stored_watermark(), d(5))  # D-1: the failing day did not advance it
		self.assertEqual(stored_days(), [d(7), d(6), d(5)])
		message = logger.return_value.error.call_args.args[0]
		self.assertIn("RuntimeError", message)
		self.assertNotIn("secret row text", message)
		self.assertNotIn(A, message)

		# The next run retries D and carries on to yesterday.
		self.assertEqual(ru.rollup_days(), [d(4), d(3), d(2), d(1)])
		self.assertEqual(stored_watermark(), d(1))

	def test_a_failure_after_the_delete_leaves_the_days_old_rows_alone(self):
		self.short_horizon(5)
		build_fixture()
		bad = d(3)
		set_watermark(d(4))
		frappe.db.bulk_insert(DAILY, ["name", "day", "actor", "reads"], [("junk", bad, A, 999)])
		real = frappe.db.bulk_insert

		def boom(doctype, fields, values, *a, **kw):
			if doctype == DAILY and any(v[7] == bad for v in values if len(v) > 7):
				raise RuntimeError("insert failed")
			return real(doctype, fields, values, *a, **kw)

		with mock.patch.object(frappe.db, "bulk_insert", boom):
			self.assertEqual(ru.rollup_days(), [])
		self.assertEqual(stored_watermark(), d(4))
		self.assertEqual(daily_rows(day=bad, actor=A)[0].reads, 999)  # the DELETE was rolled back with it
		self.assertEqual(ru.rollup_days(), [d(3), d(2), d(1)])
		replaced = daily_rows(day=bad, actor=A)[0]  # the junk row was replaced, not added to
		self.assertEqual((replaced.reads, replaced.turns), (0, 2))
		self.assertEqual(len(daily_rows(day=bad, actor=A)), 1)

	def test_time_budget_stops_new_days_cleanly(self):
		self.short_horizon(8)
		build_fixture()
		clock = {"t": 0.0}
		real = ar.day_counters

		def slow(first, last, user=None, **kw):
			clock["t"] += 100  # every day "takes" 100 s
			return real(first, last, user, **kw)

		logger = self._patch("frappe.logger")
		with mock.patch.object(ru, "_clock", lambda: clock["t"]), mock.patch.object(ar, "day_counters", slow):
			written = ru.rollup_days()
		self.assertEqual(written, [d(7), d(6), d(5)])  # 0 s, 100 s, 200 s start; 300 s does not
		# The run that the budget cut short says so, with how many days are still to roll.
		logger.assert_called_with("jarvis.activity_log")
		warning = logger.return_value.warning.call_args.args[0]
		self.assertIn("time budget", warning)
		self.assertIn("4 day(s)", warning)
		self.assertEqual(stored_watermark(), d(5))
		self.assertEqual(stored_days(), [d(7), d(6), d(5)])
		clock["t"] = 0
		with mock.patch.object(ru, "_clock", lambda: clock["t"]):
			self.assertEqual(ru.rollup_days(), [d(4), d(3), d(2), d(1)])

	def test_a_failed_savepoint_rollback_falls_back_to_a_full_rollback(self):
		# A deadlock / lock-wait abort drops the transaction and the savepoint with it.
		self.short_horizon(3)
		build_fixture()
		rollbacks = []

		def rollback(*args, **kwargs):
			if kwargs.get("save_point"):
				raise RuntimeError("savepoint does not exist")
			rollbacks.append("full")  # not performed: the fixtures must survive the test

		def failing(first, last, user=None, **kw):
			raise ValueError("secret row text")

		logger = self._patch("frappe.logger")
		with (
			mock.patch.object(ar, "day_counters", failing),
			mock.patch.object(frappe.db, "rollback", rollback),
		):
			self.assertEqual(ru.rollup_days(), [])  # stops, does not raise
		self.assertEqual(rollbacks, ["full"])
		message = logger.return_value.error.call_args.args[0]
		self.assertIn("ValueError", message)  # the original failure, class only
		self.assertNotIn("secret row text", message)
		self.assertNotIn("savepoint", message)

	def test_the_watermark_is_locked_only_after_the_counting_queries(self):
		self.short_horizon(3)
		build_fixture()
		order = []
		real_counters, real_lock = ar.day_counters, ru._read_watermark_locked

		def counters(*a, **kw):
			order.append("count")
			return real_counters(*a, **kw)

		def lock():
			order.append("lock")
			return real_lock()

		with (
			mock.patch.object(ar, "day_counters", counters),
			mock.patch.object(ru, "_read_watermark_locked", lock),
		):
			ru.rollup_days()
		# Every lock directly follows a count (days that advance the watermark); the late-row
		# re-roll and today's partial roll count without locking.
		self.assertTrue(order)
		self.assertGreater(order.count("lock"), 0)
		for i, step in enumerate(order):
			if step == "lock":
				self.assertEqual(order[i - 1], "count")
		self.assertNotIn("lock", order[-1:])  # today's partial roll comes last, lock-free

	def test_a_partial_roll_and_a_late_row_reroll_take_no_for_update(self):
		self.short_horizon(3)
		build_fixture()
		ru.rollup_days()
		sqls = []
		real = frappe.db.sql

		def spy(query, *a, **kw):
			sqls.append(" ".join(str(query).split()).lower())
			return real(query, *a, **kw)

		def locks():
			return [q for q in sqls if "tabsingles" in q and "for update" in q]

		with mock.patch.object(frappe.db, "sql", spy):
			self.assertEqual(ru._roll_day(TODAY, advance=False, through=tm(TODAY, 11)), "rolled")
			self.assertEqual(ru._roll_day(d(1), advance=False), "rolled")
			self.assertEqual(locks(), [])
			self.assertEqual(ru._roll_day(d(1), advance=True), "skipped")  # an advancing roll does lock
			self.assertEqual(len(locks()), 1)

	def test_yesterday_is_rerolled_by_every_run_of_the_following_day(self):
		# A row can commit hours after its ``at`` (a long transaction): yesterday stays open
		# for the whole next day, not for a 2 h grace window that closed it undercounted.
		self.short_horizon(5)
		base.ev(tm(d(1), 10), event="read", actor=A, rows=1)
		ru.rollup_days()
		self.assertEqual(daily_rows(day=d(1), actor=A)[0].reads, 1)

		for hour, minute in ((0, 5), (2, 0), (5, 30), (12, 0), (20, 15), (23, 59)):
			base.ev(tm(d(1), 23, 59, hour), event="read", actor=A, rows=1)  # a straggler
			calls = self.counted()
			self.at_time(tm(TODAY, hour, minute))
			with self.subTest(clock=f"{hour:02d}:{minute:02d}"):
				before = daily_rows(day=d(1), actor=A)[0].reads
				self.assertEqual(ru.rollup_days(), [d(1)])
				self.assertEqual(daily_rows(day=d(1), actor=A)[0].reads, before + 1)
				self.assertEqual(stored_watermark(), d(1))  # a re-roll never moves the watermark
				self.assertEqual(calls[0], d(1))  # yesterday first ...
				self.assertEqual(calls.count(d(1)), 1)  # ... and once
			self.cleanup_locks()

		# After the next midnight "yesterday" is another day: the old one is final.
		base.ev(tm(d(1), 23, 59, 30), event="read", actor=A, rows=1)
		final = daily_rows(day=d(1), actor=A)[0].reads
		self.at_time(tm(TODAY + dt.timedelta(days=1), 12))
		ru.rollup_days()
		self.assertEqual(daily_rows(day=d(1), actor=A)[0].reads, final)

	def test_a_day_just_rolled_in_the_grace_window_is_not_rolled_twice(self):
		self.short_horizon(5)
		base.ev(tm(d(1), 10), event="read", actor=A, rows=1)
		self.at_time(tm(TODAY, 0, 30))
		calls = self.counted()
		ru.rollup_days()
		self.assertEqual(calls.count(d(1)), 1)

	def test_a_lagging_run_never_moves_the_watermark_backwards(self):
		self.short_horizon(8)
		build_fixture()
		ru.rollup_days()
		self.assertEqual(stored_watermark(), d(1))
		before = [(r.day, r.actor, r.reads) for r in daily_rows()]
		writes = []
		real = frappe.db.set_single_value

		def spy(doctype, field, value, **kw):
			writes.append(value)
			return real(doctype, field, value, **kw)

		calls = self.counted()
		# This run started from an out-of-date read of the watermark (d(6)) while the
		# stored one is already d(1): every day it believes is pending is already final.
		with (
			mock.patch.object(ar, "watermark", return_value=d(6)),
			mock.patch.object(frappe.db, "set_single_value", spy),
		):
			# Skipped under the lock, one by one; yesterday is then re-rolled in place (every
			# run does, advance=False), which is idempotent and writes no watermark.
			self.assertEqual(ru.rollup_days(), [d(1)])
		self.assertEqual(writes, [])
		self.assertEqual(stored_watermark(), d(1))
		self.assertEqual([(r.day, r.actor, r.reads) for r in daily_rows()], before)
		# today's partial roll never advances anything
		self.assertEqual(calls, [d(n) for n in range(5, 0, -1)] + [d(1), TODAY])

	def test_no_full_cache_clear_during_a_rollup(self):
		self.short_horizon(5)
		build_fixture()
		spy = self._patch("frappe.clear_cache")
		spy2 = self._patch("frappe.cache_manager.clear_cache")
		ru.rollup_days()
		self.assertEqual(stored_watermark(), d(1))
		spy.assert_not_called()
		spy2.assert_not_called()

	def test_watermark_is_read_for_update_and_written_with_set_single_value(self):
		self.short_horizon(3)
		sqls = []
		real = frappe.db.sql

		def spy(query, *a, **kw):
			sqls.append(" ".join(str(query).split()))
			return real(query, *a, **kw)

		with mock.patch.object(frappe.db, "sql", spy):
			ru.rollup_days()
		locked = [q for q in sqls if "tabSingles" in q and "for update" in q.lower()]
		self.assertTrue(locked)

	def test_first_deploy_rolls_every_horizon_day_over_successive_runs(self):
		# Only Agent Write and Turn Usage history exists (no Activity Event at all).
		for n in (88, 60, 30, 3):
			base.wr(tm(d(n), 11), tool="create_doc", actor=A)
			base.usage(A, d(n), tin=1, tout=1)
		for n in (95, 90):  # before the horizon: never rolled, never counted
			base.wr(tm(d(n), 11), tool="create_doc", actor=A)
			base.usage(A, d(n), tin=100, tout=100)
		horizon = ar.horizon_day(TODAY)
		self.assertEqual(horizon, d(89))

		first = ar.summary({})
		self.assertEqual((first["stale_days"], first["clamped"]), (89, False))
		# Nothing rolled yet: only the 7 most recent days are computed live (d(3)'s write); the
		# 82 older ones, with the three older writes, are left out and the result says so.
		self.assertEqual((first["partial"], first["excluded_days"]), (True, 82))
		self.assertEqual(person(first, A)["writes"], 1)
		self.enqueue.assert_called_with(
			"jarvis.activity_rollup.rollup_days",
			queue="long",
			job_id="jarvis_activity_rollup",
			deduplicate=True,
		)

		clock = {"t": 0.0}
		real = ar.day_counters

		def slow(first, last, user=None, **kw):
			clock["t"] += 100
			return real(first, last, user, **kw)

		runs = []
		with mock.patch.object(ar, "day_counters", slow):
			for _ in range(60):
				clock["t"] = 0.0
				written = ru.rollup_days()
				runs.append(written)
				if stored_watermark() == d(1):
					break  # caught up (a later run only re-rolls yesterday)
		self.assertEqual(sum(len(r) for r in runs), 89)  # d(89) .. d(1), once each
		self.assertEqual([x for r in runs for x in r], [d(n) for n in range(89, 0, -1)])
		self.assertTrue(all(len(r) == 3 for r in runs[:-1]))
		self.assertEqual(stored_watermark(), d(1))
		self.assertGreaterEqual(min(stored_days()), horizon)  # nothing before the horizon

		self.enqueue.reset_mock()
		done = ar.summary({})
		self.assertEqual(done["stale_days"], 0)  # no banner
		self.enqueue.assert_not_called()
		self.assertEqual(person(done, A)["writes"], 4)
		self.assertEqual(person(done, A)["turns"], 4)
		# Same totals computed all-live (the cap lifted: it would leave the old days out).
		self.lift_cap()
		set_watermark(None)
		self.assertEqual(ar.summary({})["people"], done["people"])

	def test_two_sites_progress_independently(self):
		cache = frappe.cache()
		mine = cache.make_key(ru.LOCK_KEY)
		with mock.patch.dict(frappe.local.conf, {"db_name": "some_other_site_db"}):
			theirs = cache.make_key(ru.LOCK_KEY)
			self.assertTrue(ru._redis_lock.claim(ru.LOCK_KEY, 60))  # the other site is mid-run
		self.addCleanup(cache.delete, theirs)
		self.assertNotEqual(mine, theirs)
		self.assertTrue(mine.startswith(f"{frappe.local.conf.get('db_name')}|".encode()))

		self.short_horizon(3)
		seen = []

		def probe():
			seen.append(bool(cache.exists(ru.LOCK_KEY)))
			return []

		with mock.patch.object(ru, "_run", probe):
			self.assertEqual(ru.rollup_days(), [])
		self.assertEqual(seen, [True])  # this site ran, holding ITS key, despite the other's
		self.assertFalse(cache.exists(ru.LOCK_KEY))  # released when done
		with mock.patch.dict(frappe.local.conf, {"db_name": "some_other_site_db"}):
			self.assertTrue(cache.exists(ru.LOCK_KEY))  # the other site's claim is untouched

	def test_a_held_claim_makes_the_run_a_no_op(self):
		self.short_horizon(3)
		calls = self.counted()
		self.assertTrue(ru._redis_lock.claim(ru.LOCK_KEY, 60))
		self.assertEqual(ru.rollup_days(), [])
		self.assertEqual(calls, [])
		self.assertIsNone(stored_watermark())

	def test_the_claim_uses_the_documented_key_and_ttl(self):
		cache = frappe.cache()
		lock = mock.MagicMock()
		lock.acquire.return_value = False
		with mock.patch.object(type(cache), "lock", return_value=lock) as make:
			self.assertEqual(ru.rollup_days(), [])
		make.assert_called_once_with(cache.make_key("jarvis:activity_rollup"), timeout=600)
		lock.acquire.assert_called_once_with(blocking=False)
		lock.release.assert_not_called()  # never took it, never frees it

	def test_a_run_frees_only_its_own_claim(self):
		# A run that outlives the TTL must not delete the claim another run took after it
		# expired: release is token-checked (redis-py ``Lock.release`` compares the stored
		# token with the one this run wrote), a plain DELETE would free the stranger's.
		cache = frappe.cache()
		key = cache.make_key(ru.LOCK_KEY)

		def outlives_the_ttl():
			cache.delete(key)  # expired ...
			cache.set(key, b"another-run-token", px=60_000)  # ... and taken by the next run
			return []

		with mock.patch.object(ru, "_run", outlives_the_ttl):
			self.assertEqual(ru.rollup_days(), [])
		self.assertEqual(cache.get(key), b"another-run-token")  # untouched
		self.assertGreater(cache.pttl(key), 0)

	def test_a_run_frees_its_claim_when_it_fails(self):
		with mock.patch.object(ru, "_run", side_effect=RuntimeError("boom")):
			with self.assertRaises(RuntimeError):
				ru.rollup_days()
		self.assertFalse(frappe.cache().exists(ru.LOCK_KEY))
		lock = ru._claim()  # claimable again
		self.assertIsNotNone(lock)
		ru._release(lock)


def person(result, user):
	return base.person(result, user)


# --------------------------------------------------------------------------- read path


def _strip(res):
	return res["people"]


class TestEquivalence(RollupCase):
	def test_split_summary_equals_all_live_for_every_subrange(self):
		self.lift_cap()
		self.short_horizon()
		build_fixture()
		today_fixture()  # rows on both sides of the partial cut-off H = 11:00 (clock 12:00)
		ru.rollup_days()
		self.assertEqual({r.through for r in daily_rows(day=TODAY)}, {tm(TODAY, 11)})
		days = [d(n) for n in range(9, -1, -1)]  # oldest first, ending today
		ranges = [(days[i], days[j]) for i in range(len(days)) for j in range(i, len(days))]
		self.assertEqual(len(ranges), 55)

		set_watermark(None)  # reference: nothing rolled, everything computed live, today too
		frappe.db.delete(DAILY, {"day": TODAY})
		reference = {}
		for (frm, to), user in itertools.product(ranges, (None, A)):
			res = self.people(frm, to, user)
			self.assertEqual(res["stale_days"], len([x for x in days if frm <= x <= min(to, d(1))]))
			reference[(frm, to, user)] = _strip(res)

		# Sanity: the reference is not vacuous (every kind of flag and bucket is present).
		full = reference[(days[0], days[-1], None)]
		got_flags = {f["key"] for p in full for f in p["flags"]}
		self.assertEqual(got_flags, {"delete_burst", "big_export", "daily_export", "refusals", "off_hours"})
		self.assertEqual({p["user"] for p in full}, {A, B, GHOST, GHOST2, ""})
		self.assertEqual({p["user"]: p["name"] for p in full}[GHOST], "Ghost Person")
		self.assertEqual({p["user"]: p["name"] for p in full}[GHOST2], "Ghost Two")
		self.assertEqual({p["user"]: p["name"] for p in full}[""], "No person")

		# Today again as rolled partial rows + a live tail: every range ending today must be
		# identical to the all-live reference.
		self.assertEqual(ru._roll_day(TODAY, advance=False, through=tm(TODAY, 11)), "rolled")
		self.assertTrue(daily_rows(day=TODAY))
		for watermark in (d(6), d(2), d(1)):  # mixed ranges, then everything closed rolled
			set_watermark(watermark)
			for (frm, to), user in itertools.product(ranges, (None, A)):
				res = self.people(frm, to, user)
				with self.subTest(watermark=str(watermark), frm=str(frm), to=str(to), user=user):
					self.assertEqual(_strip(res), reference[(frm, to, user)])
					live_from = max(frm, watermark + dt.timedelta(days=1))
					self.assertEqual(
						res["stale_days"],
						max(0, (min(to, d(1)) - live_from).days + 1) if live_from <= to else 0,
					)

	def test_rolled_days_come_from_the_daily_rows_and_later_days_are_live(self):
		self.short_horizon()
		build_fixture()
		ru.rollup_days()
		set_watermark(d(9))
		# Tamper with the stored row of the watermark day and of the day after it: only the
		# watermark day (and earlier) may be read from the table, the next one is live.
		frappe.db.set_value(DAILY, daily_rows(day=d(9), actor=A)[0].name, "reads", 1000)
		frappe.db.set_value(DAILY, daily_rows(day=d(8), actor=A)[0].name, "reads", 1000)
		self.assertEqual(person(self.people(d(9), d(9)), A)["reads"], 1000)  # <= watermark: stored
		self.assertEqual(person(self.people(d(8), d(8)), A)["reads"], 1)  # > watermark: live
		self.assertEqual(person(self.people(d(9), d(8)), A)["reads"], 1001)  # a range straddling both

	def test_a_user_rename_after_the_rollup_leaves_the_summary_unchanged(self):
		from jarvis.tests.test_part3_security import _ensure_user

		self.short_horizon(6)
		old = _ensure_user(f"ru-{frappe.generate_hash(length=8)}@example.com", ["Jarvis User"])
		new = f"ru-{frappe.generate_hash(length=8)}@example.com"
		base.ev(tm(d(3), 10), event="read", actor=old, rows=5, actor_name="Old Name")
		base.wr(tm(d(3), 11), tool="create_doc", actor=old)
		base.ev(tm(TODAY, 10), event="read", actor=old, rows=2)
		ru.rollup_days()
		before = self.people(d(5), TODAY)["people"]
		self.assertEqual([p["user"] for p in before], [old])

		frappe.rename_doc("User", old, new, force=True)
		after = self.people(d(5), TODAY)["people"]
		self.assertEqual([p["user"] for p in after], [new])  # the stored rows followed the Link
		self.assertEqual({**after[0], "user": old}, before[0])
		self.assertEqual(daily_rows(actor=old), [])

	def test_deleted_users_keep_their_rolled_totals_and_name(self):
		self.short_horizon(6)
		base.ev(tm(d(3), 10), event="read", actor=GHOST, actor_name="Ghost Person", rows=3)
		ru.rollup_days()
		res = self.people(d(4), d(2))
		self.assertEqual((person(res, GHOST)["name"], person(res, GHOST)["reads"]), ("Ghost Person", 1))


class TestClamping(RollupCase):
	def setUp(self):
		super().setUp()
		self.lift_cap()  # these ranges span up to 89 unrolled days; the cap has its own tests

	def test_no_from_date_means_the_horizon(self):
		base.ev(tm(d(89), 10), event="read", actor=A)
		base.ev(tm(d(90), 10), event="read", actor=A)  # one day too old
		res = ar.summary({})
		self.assertEqual(person(res, A)["reads"], 1)
		self.assertFalse(res["clamped"])

	def test_an_older_from_date_is_raised_and_flagged(self):
		base.ev(tm(d(89), 10), event="read", actor=A)
		base.ev(tm(d(90), 10), event="read", actor=A)
		res = ar.summary({"from_date": str(d(200)), "to_date": str(TODAY)})
		self.assertEqual(person(res, A)["reads"], 1)
		self.assertTrue(res["clamped"])
		exact = ar.summary({"from_date": str(d(89))})
		self.assertFalse(exact["clamped"])  # exactly the horizon: nothing was cut
		self.assertTrue(ar.summary({"from_date": str(d(90))})["clamped"])

	def test_a_to_date_past_today_is_cut_to_today(self):
		base.ev(tm(TODAY, 10), event="read", actor=A)
		res = ar.summary({"from_date": str(d(2)), "to_date": str(TODAY + dt.timedelta(days=30))})
		self.assertEqual(person(res, A)["reads"], 1)
		self.assertFalse(res["clamped"])
		self.assertEqual(res["stale_days"], 2)  # d(2), d(1) closed and unrolled; today is not "stale"

	def test_ranges_wholly_outside_are_empty_with_no_banner_and_no_enqueue(self):
		base.ev(tm(d(95), 10), event="read", actor=A)
		base.ev(tm(d(1), 10), event="read", actor=A)
		for frm, to in (
			(d(100), d(91)),  # entirely before the horizon
			(TODAY + dt.timedelta(days=1), TODAY + dt.timedelta(days=9)),  # entirely after today
		):
			res = ar.summary({"from_date": str(frm), "to_date": str(to)})
			self.assertEqual((res["people"], res["stale_days"], res["clamped"]), ([], 0, False))
		self.enqueue.assert_not_called()

	def test_no_day_before_the_horizon_is_rolled(self):
		base.wr(tm(d(95), 11), tool="create_doc", actor=A)
		base.wr(tm(d(89), 11), tool="create_doc", actor=A)
		with mock.patch.object(ar, "day_counters", wraps=ar.day_counters) as counters:
			ru.rollup_days()
		firsts = [c.args[0] for c in counters.call_args_list]
		self.assertEqual(min(firsts), d(89))
		self.assertGreaterEqual(min(stored_days()), d(89))
		self.assertEqual(daily_rows(day=d(89), actor=A)[0].writes, 1)


class TestStaleAndEnqueue(RollupCase):
	def test_enqueues_the_rollup_when_closed_days_are_computed_live(self):
		base.ev(tm(d(3), 10), event="read", actor=A)
		res = ar.summary({"from_date": str(d(5)), "to_date": str(d(1))})
		self.assertEqual(res["stale_days"], 5)
		self.enqueue.assert_called_once_with(
			"jarvis.activity_rollup.rollup_days",
			queue="long",
			job_id="jarvis_activity_rollup",
			deduplicate=True,
		)

	def test_no_enqueue_when_nothing_closed_is_unrolled(self):
		set_watermark(d(1))
		base.ev(tm(d(3), 10), event="read", actor=A)
		self.assertEqual(ar.summary({"from_date": str(d(5)), "to_date": str(d(1))})["stale_days"], 0)
		self.assertEqual(ar.summary({"from_date": str(TODAY), "to_date": str(TODAY)})["stale_days"], 0)
		self.enqueue.assert_not_called()

	def test_the_enqueue_kwargs_exist_in_the_framework(self):
		import inspect

		from frappe.utils import background_jobs

		params = inspect.signature(background_jobs.enqueue).parameters
		for name in ("queue", "job_id", "deduplicate"):
			self.assertIn(name, params)

	def test_an_enqueue_failure_never_fails_the_summary(self):
		self.enqueue.side_effect = RuntimeError("redis down")
		base.ev(tm(d(3), 10), event="read", actor=A)
		res = ar.summary({"from_date": str(d(5)), "to_date": str(d(1))})
		self.assertEqual(person(res, A)["reads"], 1)

	def test_non_admin_is_refused_before_anything_is_read_or_enqueued(self):
		email = "ru-plain@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": "Plain", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		self.enqueue.reset_mock()  # creating the User enqueued its own job
		frappe.set_user(email)
		with self.assertRaises(frappe.PermissionError):
			ar.summary({"from_date": str(d(5)), "to_date": str(d(1))})
		with self.assertRaises(frappe.PermissionError):
			ar.summary({})
		self.enqueue.assert_not_called()


class TestHealth(RollupCase):
	def health(self):
		return ar.summary({"from_date": str(d(1)), "to_date": str(d(1))})["logging_health"]

	def test_rollup_lag_days(self):
		self.assertEqual(self.health()["rollup_lag_days"], 89)  # nothing rolled: the whole horizon
		set_watermark(d(4))
		self.assertEqual(self.health()["rollup_lag_days"], 3)
		set_watermark(d(1))
		self.assertEqual(self.health()["rollup_lag_days"], 0)
		set_watermark(d(200))  # a watermark older than the horizon cannot report more than the horizon
		self.assertEqual(self.health()["rollup_lag_days"], 89)

	def test_scheduler_disabled_reports_pause_maintenance_and_the_system_switch(self):
		with (
			mock.patch.object(ar, "is_scheduler_inactive", return_value=False),
			mock.patch.object(ar, "is_scheduler_disabled", return_value=False),
		):
			self.assertIs(self.health()["scheduler_disabled"], False)
			for key in ("pause_scheduler", "maintenance_mode"):  # config alone is not read here
				with mock.patch.dict(frappe.local.conf, {key: 1}):
					self.assertIs(self.health()["scheduler_disabled"], False)
		with (
			mock.patch.object(ar, "is_scheduler_inactive", return_value=False),
			mock.patch.object(ar, "is_scheduler_disabled", return_value=True),
		):
			self.assertIs(self.health()["scheduler_disabled"], True)
		# The real checks, driven by site config.
		with (
			mock.patch.object(ar, "is_scheduler_disabled", return_value=False),
			mock.patch("frappe.utils.scheduler.is_scheduler_disabled", return_value=False),
		):
			for key in ("pause_scheduler", "maintenance_mode"):
				with mock.patch.dict(frappe.local.conf, {key: 1}):
					self.assertIs(self.health()["scheduler_disabled"], True, msg=key)
			with mock.patch.dict(frappe.local.conf, {"pause_scheduler": 0, "maintenance_mode": 0}):
				self.assertIs(self.health()["scheduler_disabled"], False)


# --------------------------------------------------------------------------- today (D14)


class TestTodayPartial(RollupCase):
	"""Plan rev 4.3 (D14): the hourly job also rolls today through H = floor_hour(now - 5
	min); the summary sums those rows with a live pass over everything after their own
	``through``."""

	def setUp(self):
		super().setUp()
		self.short_horizon(5)

	def roll_today(self, now):
		self.at_time(now)
		ru.rollup_days()

	def all_live(self, frm=None, user=None):
		"""The reference: today's rows removed, so today is computed whole and live."""
		frappe.db.delete(DAILY, {"day": TODAY})
		return self.people(frm or TODAY, TODAY, user)["people"]

	def today_through(self):
		return {r.through for r in daily_rows(day=TODAY)}

	def test_cutoff_is_the_last_hour_that_ended_five_minutes_ago(self):
		self.assertEqual(ru.PARTIAL_MARGIN, dt.timedelta(minutes=5))
		for now, expected in (
			(tm(TODAY, 10, 3), tm(TODAY, 9)),
			(tm(TODAY, 10, 4, 59, 999999), tm(TODAY, 9)),
			(tm(TODAY, 10, 5), tm(TODAY, 10)),  # an hour that ended exactly 5 minutes ago counts
			(tm(TODAY, 10, 6), tm(TODAY, 10)),
			(tm(TODAY, 0, 4), tm(d(1), 23)),
			(tm(TODAY, 1, 5), tm(TODAY, 1)),
			(tm(TODAY, 23, 59, 59), tm(TODAY, 23)),
		):
			self.assertEqual(ru.cutoff_hour(now), expected, msg=str(now))

	def test_partial_plus_live_equals_all_live_at_several_cutoffs(self):
		today_fixture()
		# 01:03 -> H = midnight (nothing to roll); 01:06 -> 01:00; 12:00 -> 11:00 (rows either
		# side of it); 23:58 -> 23:00, the last hour.
		for now, h in (
			(tm(TODAY, 1, 3), None),
			(tm(TODAY, 1, 6), tm(TODAY, 1)),
			(tm(TODAY, 12, 0), tm(TODAY, 11)),
			(tm(TODAY, 18, 30), tm(TODAY, 18)),
			(tm(TODAY, 23, 58), tm(TODAY, 23)),
		):
			for user in (None, A, B, GHOST, ""):
				with self.subTest(now=str(now), user=user):
					self.at_time(now)
					reference = self.all_live(user=user)
					self.assertTrue(reference or user in (GHOST, ""))
					ru.rollup_days()
					if h is None:
						self.assertEqual(daily_rows(day=TODAY), [])  # H = midnight: no partial roll
					else:
						self.assertEqual(self.today_through(), {h})
					self.assertEqual(self.people(TODAY, TODAY, user)["people"], reference)
			self.assertEqual(ar.summary({})["stale_days"], 0)

	def test_the_live_pass_starts_at_the_rows_through_and_today_is_never_stale(self):
		today_fixture()
		calls = []
		real = ar.day_counters

		def spy(first, last, user=None, **kw):
			calls.append((first, last, kw))
			return real(first, last, user, **kw)

		with mock.patch.object(ar, "day_counters", spy):
			res = self.people(TODAY, TODAY)  # nothing rolled: the whole of today live
			self.assertEqual(calls, [(TODAY, TODAY, {"since": None})])
			self.assertEqual((res["stale_days"], res["clamped"]), (0, False))
			self.enqueue.assert_not_called()  # today is not a "stale" closed day

			self.roll_today(tm(TODAY, 12))
			calls.clear()
			res = self.people(TODAY, TODAY)
			self.assertEqual(calls, [(TODAY, TODAY, {"since": tm(TODAY, 11)})])
			self.assertEqual(res["stale_days"], 0)
			self.enqueue.assert_not_called()

	def test_the_rows_through_drives_the_live_start_not_a_clock_or_cached_value(self):
		base.ev(tm(TODAY, 10, 30), event="read", actor=A, rows=1)
		base.ev(tm(TODAY, 11, 30), event="read", actor=A, rows=1)  # after H, before the next roll
		self.roll_today(tm(TODAY, 12))  # H = 11:00: only the 10:30 event is in the rows
		self.assertEqual(self.today_through(), {tm(TODAY, 11)})
		# Hours later, nothing has re-rolled: a live start derived from the clock (13:00 ->
		# 13:00, or 12:00) would drop the 11:30 event; the rows' own stamp counts it once.
		for later in (tm(TODAY, 13, 0), tm(TODAY, 13, 4), tm(TODAY, 23, 59)):
			self.at_time(later)
			self.assertEqual(person(self.people(TODAY, TODAY), A)["reads"], 2, msg=str(later))
		# And the stamp, not any other value, decides: move it and the live start moves.
		frappe.db.sql(f"update `tab{DAILY}` set `through` = %s where `day` = %s", (tm(TODAY, 11, 15), TODAY))
		self.assertEqual(person(self.people(TODAY, TODAY), A)["reads"], 2)  # 11:30 still after it
		frappe.db.sql(f"update `tab{DAILY}` set `through` = %s where `day` = %s", (tm(TODAY, 12), TODAY))
		self.assertEqual(person(self.people(TODAY, TODAY), A)["reads"], 1)  # 11:30 now "rolled"

	def test_a_stamp_that_is_missing_mixed_or_not_today_is_not_trusted(self):
		today_fixture()
		self.at_time(tm(TODAY, 12))
		reference = self.all_live()
		ru.rollup_days()
		rows = daily_rows(day=TODAY)
		self.assertGreater(len(rows), 2)
		self.assertEqual(self.people(TODAY, TODAY)["people"], reference)

		frappe.db.sql(
			f"update `tab{DAILY}` set `through` = %s where `name` = %s", (tm(TODAY, 5), rows[0].name)
		)
		self.assertEqual(self.people(TODAY, TODAY)["people"], reference)  # two stamps: not stitchable
		frappe.db.sql(f"update `tab{DAILY}` set `through` = NULL where `day` = %s", TODAY)
		self.assertEqual(self.people(TODAY, TODAY)["people"], reference)  # no stamp at all
		frappe.db.sql(f"update `tab{DAILY}` set `through` = %s where `day` = %s", (tm(d(1), 11), TODAY))
		self.assertEqual(self.people(TODAY, TODAY)["people"], reference)  # a stamp from another day

	def test_a_row_stamped_before_h_inserted_after_the_roll_is_included_by_the_next_roll(self):
		base.ev(tm(TODAY, 8, 0), event="read", actor=A, rows=1)
		self.roll_today(tm(TODAY, 10, 6))
		self.assertEqual(self.today_through(), {tm(TODAY, 10)})
		base.ev(tm(TODAY, 9, 59, 30), event="read", actor=A, rows=1)  # stamped < H, committed late
		# Neither the rows nor the live tail covers it until the next roll (the margin exists
		# to make this window negligible)...
		self.assertEqual(person(self.people(TODAY, TODAY), A)["reads"], 1)
		# ... which recomputes [00:00, H) whole, even when H has not moved.
		self.roll_today(tm(TODAY, 10, 20))
		self.assertEqual(self.today_through(), {tm(TODAY, 10)})
		self.assertEqual(person(self.people(TODAY, TODAY), A)["reads"], 2)

	def test_rolling_today_twice_leaves_one_row_per_person(self):
		today_fixture()
		self.roll_today(tm(TODAY, 12))
		first = [(r.actor, r.reads, r.turns) for r in daily_rows(day=TODAY)]
		self.roll_today(tm(TODAY, 12, 4))
		self.assertEqual([(r.actor, r.reads, r.turns) for r in daily_rows(day=TODAY)], first)
		self.assertEqual(len({r.actor for r in daily_rows(day=TODAY)}), len(first))

	def test_the_partial_roll_never_moves_the_watermark_and_is_not_a_closed_day(self):
		today_fixture()
		self.roll_today(tm(TODAY, 12))
		self.assertEqual(stored_watermark(), d(1))
		self.assertNotIn(TODAY, ru.rollup_days())
		self.assertEqual(stored_watermark(), d(1))
		self.assertEqual(self.today_through(), {tm(TODAY, 11)})

	def test_the_partial_roll_waits_for_the_closed_days(self):
		today_fixture()
		calls = self.counted()
		real = ar.day_counters

		def failing(first, last, user=None, **kw):
			if first == d(3):
				raise RuntimeError("boom")
			return real(first, last, user, **kw)

		self._patch("frappe.logger")
		with mock.patch.object(ar, "day_counters", failing):
			self.assertEqual(ru.rollup_days(), [d(4)])
		self.assertNotIn(TODAY, calls)
		self.assertEqual(daily_rows(day=TODAY), [])

	def test_the_partial_roll_obeys_the_time_budget(self):
		today_fixture()
		clock = {"t": 0.0}
		real = ar.day_counters

		def slow(first, last, user=None, **kw):
			clock["t"] += 100
			return real(first, last, user, **kw)

		# Four closed days (d(4)..d(1)) at 100 s each: the third pushes past 240 s.
		with mock.patch.object(ru, "_clock", lambda: clock["t"]), mock.patch.object(ar, "day_counters", slow):
			written = ru.rollup_days()
		self.assertEqual(written, [d(4), d(3), d(2)])
		self.assertEqual(daily_rows(day=TODAY), [])

	def test_a_failure_in_the_partial_roll_keeps_the_previous_partial_rows(self):
		today_fixture()
		self.roll_today(tm(TODAY, 12))
		before = [(r.actor, r.reads, r.through) for r in daily_rows(day=TODAY)]
		real = frappe.db.bulk_insert

		def boom(doctype, *a, **kw):
			if doctype == DAILY:
				raise RuntimeError("insert failed")
			return real(doctype, *a, **kw)

		self._patch("frappe.logger")
		self.at_time(tm(TODAY, 15))
		with mock.patch.object(frappe.db, "bulk_insert", boom):
			ru.rollup_days()
		self.assertEqual([(r.actor, r.reads, r.through) for r in daily_rows(day=TODAY)], before)

	def test_midnight_handover_replaces_yesterdays_partial_rows_without_double_counting(self):
		yesterday = d(1)
		base.ev(tm(yesterday, 10), event="read", actor=A, rows=1)
		base.ev(tm(yesterday, 22, 10), event="read", actor=A, rows=1)
		base.ev(tm(yesterday, 23, 50), event="read", actor=A, rows=1)  # after yesterday's last H
		base.ev(tm(TODAY, 0, 2), event="read", actor=A, rows=1)

		# Yesterday 23:30: "today" is yesterday; its partial rows run through 23:00.
		self.at_time(tm(yesterday, 23, 30))
		ru.rollup_days()
		partial = daily_rows(day=yesterday)
		self.assertEqual({r.through for r in partial}, {tm(yesterday, 23)})
		self.assertEqual(partial[0].reads, 2)

		# Just after midnight, before the job: yesterday's partial rows are NOT read (it is
		# an unrolled closed day, computed live), so nothing counts twice.
		self.at_time(tm(TODAY, 0, 2, 30))
		res = self.people(yesterday, TODAY)
		self.assertEqual(person(res, A)["reads"], 4)
		self.assertEqual(res["stale_days"], 1)

		# The job's closed-day roll replaces them: one row per person, through = midnight.
		ru.rollup_days()
		rows = daily_rows(day=yesterday)
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].reads, rows[0].through), (3, tm(TODAY, 0)))
		self.assertEqual(stored_watermark(), yesterday)
		res = self.people(yesterday, TODAY)
		self.assertEqual((person(res, A)["reads"], res["stale_days"]), (4, 0))

	def test_the_job_behind_means_a_full_live_today(self):
		today_fixture()
		self.at_time(tm(TODAY, 12))
		self.assertEqual(daily_rows(day=TODAY), [])  # the job has not run yet today
		behind = self.people(TODAY, TODAY)
		self.assertEqual(behind["stale_days"], 0)  # still exact: not a banner
		self.enqueue.assert_not_called()
		reference = behind["people"]
		self.roll_today(tm(TODAY, 12))
		self.assertEqual(self.people(TODAY, TODAY)["people"], reference)

	def test_day_counters_halves_add_up_to_the_whole_day(self):
		today_fixture()
		whole, _ = ar.day_counters(TODAY, TODAY)
		for h in (tm(TODAY, 0), tm(TODAY, 1), tm(TODAY, 11), tm(TODAY, 23), tm(TODAY, 23, 59, 59, 999999)):
			for user in (None, A):
				with self.subTest(h=str(h), user=user):
					want, _ = ar.day_counters(TODAY, TODAY, user)
					early, _ = ar.day_counters(TODAY, TODAY, user, until=h)
					late, _ = ar.day_counters(TODAY, TODAY, user, since=h)
					merged = {}
					for part in (early, late):
						for key, c in part.items():
							if key in merged:
								ar._add_counters(merged[key], c)
							else:
								merged[key] = dict(c)
					self.assertEqual({k: dict(v) for k, v in want.items()}, merged)
		self.assertTrue(whole)
		# max_export_rows is a maximum, not a sum: 60,000 before H and 70,000 after.
		early, _ = ar.day_counters(TODAY, TODAY, until=tm(TODAY, 11))
		late, _ = ar.day_counters(TODAY, TODAY, since=tm(TODAY, 11))
		self.assertEqual(
			(early[(B, TODAY)]["max_export_rows"], late[(B, TODAY)]["max_export_rows"]), (60_000, 70_000)
		)
		self.assertEqual(whole[(B, TODAY)]["max_export_rows"], 70_000)
		self.assertEqual(whole[(B, TODAY)]["rows_exported"], 130_010)

	def test_a_user_filter_with_no_rows_still_uses_the_stamp(self):
		# Rows exist for others only: the filtered person's live pass must still start at the
		# stamp (not re-count their whole day on top of rows that are not theirs).
		base.ev(tm(TODAY, 9, 0), event="read", actor=A, rows=1)
		base.ev(tm(TODAY, 11, 30), event="read", actor=B, rows=1)
		self.roll_today(tm(TODAY, 12))
		calls = []
		real = ar.day_counters

		def spy(first, last, user=None, **kw):
			calls.append((user, kw))
			return real(first, last, user, **kw)

		with mock.patch.object(ar, "day_counters", spy):
			res = self.people(TODAY, TODAY, user=B)
		self.assertEqual(calls, [(B, {"since": tm(TODAY, 11)})])
		self.assertEqual([(p["user"], p["reads"]) for p in res["people"]], [(B, 1)])


# --------------------------------------------------------------------------- B1: the live cap


class TestLiveCap(RollupCase):
	"""The summary computes at most ``LIVE_DAY_CAP`` unrolled closed days per request and
	asks for the catch-up BEFORE it computes anything."""

	def setUp(self):
		super().setUp()
		self.short_horizon(20)
		for n in (9, 8, 7, 4, 1):
			base.ev(tm(d(n), 10), event="read", actor=A, rows=n)

	def reads(self, frm, to):
		res = self.people(frm, to)
		p = person(res, A)
		return res, (p["reads"] if p else 0)

	def test_only_the_seven_most_recent_unrolled_days_are_computed_live(self):
		calls = self.counted()
		res, reads = self.reads(d(9), d(1))
		self.assertEqual(calls, [d(7)])  # one pass: d(7) .. d(1), nothing older
		self.assertEqual(reads, 3)  # d(7), d(4), d(1); the d(9) and d(8) events are not in
		self.assertEqual((res["stale_days"], res["partial"], res["excluded_days"]), (9, True, 2))
		self.assertFalse(res["preparing"])
		self.enqueue.assert_called_once()

	def test_exactly_seven_unrolled_days_is_not_partial_and_eight_is(self):
		res, reads = self.reads(d(7), d(1))
		self.assertEqual((res["stale_days"], res["partial"], res["excluded_days"], reads), (7, False, 0, 3))
		res, reads = self.reads(d(8), d(1))
		self.assertEqual((res["stale_days"], res["partial"], res["excluded_days"], reads), (8, True, 1, 3))

	def test_rolled_days_are_read_in_full_and_only_unrolled_ones_count_toward_the_cap(self):
		for n in (9, 8):
			self.assertEqual(ru._roll_day(d(n), advance=True), "rolled")
		self.assertEqual(stored_watermark(), d(8))
		calls = self.counted()
		res, reads = self.reads(d(9), d(1))
		self.assertEqual(calls, [d(7)])  # d(7) .. d(1) is exactly 7 unrolled days
		self.assertEqual((res["stale_days"], res["partial"], res["excluded_days"]), (7, False, 0))
		self.assertEqual(reads, 5)  # the two rolled days (from the table) plus d(7), d(4), d(1)

	def test_a_partial_result_keeps_rolled_days_and_drops_the_oldest_unrolled_ones(self):
		self.assertEqual(ru._roll_day(d(9), advance=True), "rolled")  # d(8) .. d(1) are behind
		base.ev(tm(d(8), 11), event="read", actor=A, rows=1)
		res, reads = self.reads(d(9), d(1))
		# 8 days behind: the oldest, d(8), is out (both of its events); d(9) is read from the table.
		self.assertEqual((res["stale_days"], res["excluded_days"], res["partial"]), (8, 1, True))
		self.assertEqual(reads, 1 + 3)  # d(9) rolled; d(7), d(4), d(1) live

	def test_a_range_inside_the_rolled_days_is_never_partial(self):
		for n in (9, 8, 7):
			ru._roll_day(d(n), advance=True)
		res, reads = self.reads(d(9), d(7))
		self.assertEqual((res["stale_days"], res["partial"], res["excluded_days"]), (0, False, 0))
		self.assertEqual(reads, 3)
		self.enqueue.assert_not_called()

	def test_the_rollup_is_enqueued_before_any_live_computation(self):
		order = []
		self.enqueue.side_effect = lambda *a, **kw: order.append("enqueue")
		real = ar.day_counters

		def spy(*a, **kw):
			order.append("count")
			return real(*a, **kw)

		with mock.patch.object(ar, "day_counters", spy):
			self.people(d(9), d(1))
		self.assertEqual(order[0], "enqueue")
		self.assertIn("count", order)
		self.assertEqual(order.count("enqueue"), 1)

	def test_a_failing_live_pass_still_asked_for_the_catch_up(self):
		with mock.patch.object(ar, "day_counters", side_effect=RuntimeError("slow query killed")):
			with self.assertRaises(RuntimeError):
				self.people(d(9), d(1))
		self.enqueue.assert_called_once()

	def test_within_the_cap_the_split_summary_equals_the_all_live_one(self):
		# (TestEquivalence lifts the cap to compare the longer ranges too.)
		live = _strip(self.people(d(6), d(1)))
		for n in (6, 5, 4):
			ru._roll_day(d(n), advance=True)
		self.assertEqual(stored_watermark(), d(4))
		self.assertEqual(_strip(self.people(d(6), d(1))), live)


class TestTablesNotMigrated(RollupCase):
	"""Code deployed, ``bench migrate`` not run: an admin opening the page gets an empty
	"being prepared" answer, not a 500."""

	def missing(self, *names):
		real = frappe.db.table_exists
		return mock.patch.object(
			frappe.db,
			"table_exists",
			lambda doctype, *a, **kw: doctype not in names and real(doctype, *a, **kw),
		)

	def test_a_missing_daily_table_answers_empty_and_prepared(self):
		base.ev(tm(d(3), 10), event="read", actor=A)
		with self.missing(DAILY), mock.patch.object(ar, "day_counters", side_effect=AssertionError("ran")):
			res = ar.summary({})
		self.enqueue.assert_not_called()
		self.assertEqual(res["people"], [])
		self.assertTrue(res["preparing"])
		self.assertEqual(
			(res["stale_days"], res["excluded_days"], res["partial"], res["clamped"]), (0, 0, False, False)
		)
		normal = ar.summary({})
		self.assertEqual(set(res), set(normal))  # the same keys as a normal answer
		self.assertEqual(set(res["logging_health"]), set(normal["logging_health"]))

	def test_a_missing_event_table_answers_the_same(self):
		with self.missing("Jarvis Activity Event"):
			self.assertTrue(ar.summary({})["preparing"])

	def test_the_admin_gate_still_comes_first(self):
		email = "ru-plain@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": "Plain", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		frappe.set_user(email)
		with self.missing(DAILY), self.assertRaises(frappe.PermissionError):
			ar.summary({})


# --------------------------------------------------------------------------- B3: visibility


class TestFailureVisibility(RollupCase):
	def failing(self, message="boom", bad=None):
		real = ar.day_counters

		def flaky(first, last, user=None, **kw):
			if bad is None or first == bad:
				raise RuntimeError(message)
			return real(first, last, user, **kw)

		return mock.patch.object(ar, "day_counters", flaky)

	def test_a_failing_day_writes_an_error_log_with_class_day_and_watermark(self):
		self.short_horizon(4)
		build_fixture()
		set_watermark(d(3))
		with self.failing(bad=d(2)):
			self.assertEqual(ru.rollup_days(), [])
		self.log_error.assert_called_once()
		kw = self.log_error.call_args.kwargs
		self.assertEqual(kw["title"], f"Jarvis activity rollup failed for {d(2)}")
		for part in ("RuntimeError", "boom", str(d(2)), f"watermark: {d(3)}"):
			self.assertIn(part, kw["message"])

	def test_the_message_is_cut_at_200_characters(self):
		self.short_horizon(3)
		with self.failing("x" * 500):
			ru.rollup_days()
		message = self.log_error.call_args.kwargs["message"]
		self.assertIn("x" * 200, message)
		self.assertNotIn("x" * 201, message)

	def test_at_most_one_error_log_per_site_per_day(self):
		self.short_horizon(3)
		with self.failing():
			for _ in range(3):  # a stuck day fails every hour
				ru.rollup_days()
		self.assertEqual(self.log_error.call_count, 1)
		# The next day it may speak again.
		self.at_time(tm(TODAY + dt.timedelta(days=1), 12))
		with self.failing():
			ru.rollup_days()
		self.assertEqual(self.log_error.call_count, 2)

	def test_the_throttle_is_per_site(self):
		self.short_horizon(3)
		with self.failing():
			ru.rollup_days()
			with mock.patch.dict(frappe.local.conf, {"db_name": "some_other_site_db"}):
				theirs = frappe.cache().make_key(f"{ru.FAILURE_KEY}:{TODAY}")
				ru.rollup_days()
		self.addCleanup(frappe.cache().delete, theirs)
		self.assertEqual(self.log_error.call_count, 2)

	def test_a_failing_error_log_never_breaks_the_run(self):
		self.short_horizon(3)
		self.log_error.side_effect = RuntimeError("Error Log insert failed")
		with self.failing():
			self.assertEqual(ru.rollup_days(), [])
		self.log_error.assert_called_once()  # it tried; the failure stayed inside

	def test_a_failed_error_log_insert_does_not_spend_the_days_entry(self):
		self.short_horizon(3)
		self.log_error.side_effect = [RuntimeError("Error Log insert failed"), None]
		with self.failing():
			ru.rollup_days()  # the insert fails: nothing written, the claim is given back
			ru.rollup_days()  # the next hourly run may still write the day's entry
			ru.rollup_days()  # and now it is spent
		self.assertEqual(self.log_error.call_count, 2)

	def test_the_error_log_is_written_after_the_rollback(self):
		# An Error Log insert before the rollback would be undone with the failed day.
		self.short_horizon(3)
		order = []
		self.log_error.side_effect = lambda **kw: order.append("log_error")
		real = frappe.db.rollback

		def rollback(**kw):
			order.append("rollback")
			return real(**kw)

		with self.failing(), mock.patch.object(frappe.db, "rollback", rollback):
			ru.rollup_days()
		self.assertEqual(order, ["rollback", "log_error"])


# --------------------------------------------------------------------------- B11: pins


class TestPartialRollStart(RollupCase):
	def test_no_partial_roll_before_the_first_full_hour_has_ended_five_minutes_ago(self):
		self.short_horizon(3)
		base.ev(tm(TODAY, 0, 10), event="read", actor=A)
		for now_dt in (tm(TODAY, 0, 0, 1), tm(TODAY, 0, 30), tm(TODAY, 1, 4, 59)):
			with self.subTest(now=str(now_dt)):
				self.at_time(now_dt)
				calls = self.counted()
				ru.rollup_days()
				self.assertNotIn(TODAY, calls)  # H would be midnight: nothing of today to roll
				self.assertEqual(daily_rows(day=TODAY), [])
		self.at_time(tm(TODAY, 1, 5))
		calls = self.counted()
		ru.rollup_days()
		self.assertIn(TODAY, calls)  # H = 01:00: [00:00, 01:00) is rolled
		self.assertEqual({r.through for r in daily_rows(day=TODAY)}, {tm(TODAY, 1)})


class TestClockIsNotReadAtImport(unittest.TestCase):
	"""D10: a runner that stays up across midnight imports this module once; a day computed
	at import would then be wrong for every later run."""

	def test_no_module_level_assignment_calls_the_clock(self):
		import ast
		import inspect

		tree = ast.parse(inspect.getsource(inspect.getmodule(self)))
		clock_calls = []
		for node in tree.body:
			if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
				for sub in ast.walk(node.value):
					if (
						isinstance(sub, ast.Call)
						and isinstance(sub.func, ast.Attribute)
						and sub.func.attr in ("today", "now", "utcnow")
					):
						clock_calls.append(ast.unparse(node))
		self.assertEqual(clock_calls, [])
