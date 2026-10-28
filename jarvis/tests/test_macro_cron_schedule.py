"""Cron schedules on macros: hourly, custom, and daily/weekly/monthly with several
times or days.

``schedule_cron`` holds one cron line per line; when set on a recurring schedule it
decides the next slot (``macro_scheduler.macro_next_run``), and a macro without one
keeps the daily/weekly/monthly fields exactly as before. The sweep's own decisions
are covered in ``test_macro_scheduler``; here only what cron changes.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from jarvis.chat import macro_scheduler

MACRO = "Jarvis Macro"
PFX = "mcron"
OWNER = "mcron-owner@example.com"


def _ensure_user(email: str) -> str:
	from jarvis.permissions import JARVIS_USER_ROLE, ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": PFX,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	if JARVIS_USER_ROLE not in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).add_roles(JARVIS_USER_ROLE)
	return email


def _purge() -> None:
	for n in frappe.get_all(MACRO, filters={"macro_name": ["like", f"{PFX}-%"]}, pluck="name"):
		for run in frappe.get_all("Jarvis Macro Run", filters={"macro": n}, pluck="name"):
			frappe.delete_doc("Jarvis Macro Run", run, force=True, ignore_permissions=True)
		frappe.delete_doc(MACRO, n, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestCronHelpers(FrappeTestCase):
	BASE = datetime.datetime(2026, 10, 22, 10, 0)  # a Thursday

	def test_the_earliest_slot_of_several_lines_wins(self):
		nxt = macro_scheduler.next_cron_run(["0 18 * * *", "30 11 * * *"], self.BASE)
		self.assertEqual(nxt, datetime.datetime(2026, 10, 22, 11, 30))

	def test_a_slot_exactly_now_is_not_the_next_one(self):
		nxt = macro_scheduler.next_cron_run(["0 10 * * *"], self.BASE)
		self.assertEqual(nxt, datetime.datetime(2026, 10, 23, 10, 0))

	def test_lines_are_read_one_per_line_single_spaced(self):
		self.assertEqual(
			macro_scheduler.cron_lines("  0  9 * * 1-5 \n\n30 17 * * 5\n"),
			["0 9 * * 1-5", "30 17 * * 5"],
		)

	def test_a_line_croniter_cannot_read_is_skipped_never_raised(self):
		nxt = macro_scheduler.next_cron_run(["not a cron", "0 12 * * *"], self.BASE)
		self.assertEqual(nxt, datetime.datetime(2026, 10, 22, 12, 0))
		self.assertIsNone(macro_scheduler.next_cron_run(["0 0 30 2 *"], self.BASE))

	def test_what_is_refused_and_why(self):
		problem = macro_scheduler.cron_problem
		self.assertEqual(problem("0 9 * * 1-5"), "")
		self.assertEqual(problem("0 9,18 * * *\n30 13 * * *"), "")
		self.assertIn("5 parts", problem("0 9 * * * *"))
		self.assertIn("5 parts", problem("@daily"))
		self.assertIn("5 parts", problem("61 9 * * *"))
		self.assertIn("never comes round", problem("0 0 30 2 *"))
		self.assertIn("at most", problem("\n".join(f"{m} 9 * * *" for m in range(7))))
		self.assertTrue(problem(""))

	def test_a_schedule_with_runs_under_five_minutes_apart_is_refused(self):
		refused = {
			"* * * * *": 1,
			"* 9 * * *": 1,
			"*/3 9 * * *": 3,
			"*/7 * * * *": 4,  # 56 -> 0 of the next hour
			"0,2 9 * * *": 2,
			"0 9 * * *\n2 9 * * *": 2,
			"58 23 * * *\n0 0 * * *": 2,  # across midnight
		}
		for expr, gap in refused.items():
			with self.subTest(expr=expr):
				self.assertEqual(macro_scheduler.shortest_gap_minutes(macro_scheduler.cron_lines(expr)), gap)
				problem = macro_scheduler.cron_problem(expr)
				self.assertIn(f"{gap} minute(s) apart", problem)
				self.assertIn("at least 5 minutes", problem)
		for expr in ("*/5 * * * *", "0 9 * * MON-FRI", "0 * * * *", "15 */2 * * *", "0 9 * * *"):
			with self.subTest(expr=expr):
				self.assertEqual(macro_scheduler.cron_problem(expr), "")

	def test_only_a_star_in_the_minute_part_gets_the_hint(self):
		self.assertIn("0 9 * * *", macro_scheduler.cron_problem("* 9 * * *"))
		self.assertNotIn("every minute", macro_scheduler.cron_problem("0,2 9 * * *"))

	def test_one_run_a_day_is_the_widest_gap(self):
		self.assertEqual(macro_scheduler.shortest_gap_minutes(["30 9 * * *"]), 1440)

	def test_cron_parts_the_form_cannot_read_are_refused(self):
		for expr in ("0 9 L * *", "0 9 l * *", "5-1 * * * *", "0 9 * * fri-mon", "0 9 * * 1#2", "0 9 ? * *"):
			with self.subTest(expr=expr):
				self.assertIn("is not a cron schedule", macro_scheduler.cron_problem(expr))
		for expr in ("0 9 * jan-mar *", "0 9 * * sun-sat", "0 9 * * MON-FRI", "0-30/10 9 * * *"):
			with self.subTest(expr=expr):
				self.assertEqual(macro_scheduler.cron_problem(expr), "")

	def test_matches(self):
		self.assertTrue(macro_scheduler.cron_matches(["30 9 * * 1,3,5"], "2026-10-23 09:30:00"))
		self.assertFalse(macro_scheduler.cron_matches(["30 9 * * 1,3,5"], "2026-10-22 09:30:00"))

	def test_a_macro_without_cron_keeps_the_old_fields(self):
		m = frappe._dict(schedule_frequency="daily", schedule_time="14:00:00", schedule_cron="")
		self.assertEqual(macro_scheduler.macro_next_run(m, self.BASE), datetime.datetime(2026, 10, 22, 14, 0))

	def test_cron_wins_over_the_old_fields_and_once_ignores_it(self):
		m = frappe._dict(schedule_frequency="weekly", schedule_time="14:00:00", schedule_cron="0 8 * * 1")
		self.assertEqual(macro_scheduler.macro_next_run(m, self.BASE), datetime.datetime(2026, 10, 26, 8, 0))
		once = frappe._dict(
			schedule_frequency="once",
			schedule_date="2026-10-30",
			schedule_time="11:00:00",
			schedule_cron="0 8 * * 1",
		)
		self.assertEqual(
			macro_scheduler.macro_next_run(once, self.BASE), datetime.datetime(2026, 10, 30, 11, 0)
		)


class TestCronOnTheMacro(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(OWNER)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		_purge()
		frappe.set_user(OWNER)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		_purge()
		super().tearDown()

	def _macro(self, tag: str, **fields):
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-{tag}",
				"schedule_enabled": 1,
				"steps": [{"prompt": "step one"}],
				**fields,
			}
		)
		doc.insert()
		return doc

	def test_hourly_needs_a_cron_once_switched_on(self):
		with self.assertRaises(frappe.ValidationError):
			self._macro("bare-hourly", schedule_frequency="hourly")
		off = self._macro("off-hourly", schedule_frequency="hourly", schedule_enabled=0)
		self.assertIsNone(off.next_run_at)

	def test_next_run_comes_from_the_cron(self):
		doc = self._macro("hourly", schedule_frequency="hourly", schedule_cron="15 */2 * * *")
		self.assertEqual(
			get_datetime(doc.next_run_at),
			macro_scheduler.next_cron_run(["15 */2 * * *"], now_datetime()),
		)
		self.assertEqual(get_datetime(doc.next_run_at).minute, 15)

	def test_the_cron_is_stored_tidy(self):
		doc = self._macro(
			"tidy", schedule_frequency="daily", schedule_cron="  0   9,18 * * *\n\n30 13 * * * \n"
		)
		self.assertEqual(doc.schedule_cron, "0 9,18 * * *\n30 13 * * *")

	def test_a_bad_cron_is_refused_even_with_the_schedule_off(self):
		with self.assertRaises(frappe.ValidationError):
			self._macro("bad", schedule_frequency="custom", schedule_enabled=0, schedule_cron="61 * * * *")

	def test_saving_a_macro_with_a_one_minute_cron_throws(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._macro("tight", schedule_frequency="custom", schedule_cron="* 9 * * *")
		self.assertIn("1 minute(s) apart", str(ctx.exception))

	def test_changing_the_cron_moves_the_next_run(self):
		doc = self._macro("move", schedule_frequency="custom", schedule_cron="0 9 * * 1")
		doc.schedule_cron = "0 9 1 * *"
		doc.save()
		self.assertEqual(get_datetime(doc.next_run_at).day, 1)

	def test_writing_an_old_daily_schedule_as_cron_keeps_a_slot_that_is_due(self):
		# A macro from before cron schedules, due an hour ago and not swept yet: saved
		# from the new form, its 9:00 daily becomes "0 9 * * *". The slot stays.
		doc = self._macro("legacy", schedule_frequency="daily", schedule_time="09:00:00")
		due = add_to_date(now_datetime(), hours=-1).replace(hour=9, minute=0, second=0, microsecond=0)
		if due > now_datetime():
			due = add_to_date(due, days=-1)
		frappe.db.set_value(MACRO, doc.name, "next_run_at", due, update_modified=False)
		doc.reload()
		doc.schedule_cron = "0 9 * * *"
		doc.save()
		self.assertEqual(get_datetime(doc.next_run_at), due)

	def test_a_kept_slot_never_hides_an_earlier_one(self):
		doc = self._macro("earlier", schedule_frequency="daily", schedule_time="09:00:00")
		later = add_to_date(now_datetime(), days=3).replace(hour=9, minute=0, second=0, microsecond=0)
		frappe.db.set_value(MACRO, doc.name, "next_run_at", later, update_modified=False)
		doc.reload()
		doc.schedule_cron = "0 9 * * *"
		doc.save()
		self.assertLess(get_datetime(doc.next_run_at), later)

	def test_a_retry_is_told_apart_from_a_cron_slot(self):
		doc = self._macro("retry", schedule_frequency="custom", schedule_cron="0 * * * *")
		row = frappe._dict(
			schedule_enabled=1,
			schedule_frequency="custom",
			schedule_cron="0 * * * *",
			next_run_at=add_to_date(now_datetime(), minutes=30).replace(second=17, microsecond=0),
		)
		self.assertTrue(macro_scheduler.is_retry_pending(row))
		row.next_run_at = macro_scheduler.next_cron_run(["0 * * * *"], now_datetime())
		self.assertFalse(macro_scheduler.is_retry_pending(row))
		self.assertTrue(doc.name)

	def test_the_sweep_moves_a_cron_macro_to_its_next_slot(self):
		doc = self._macro("sweep", schedule_frequency="custom", schedule_cron="0 9 * * *")
		frappe.db.set_value(
			MACRO, doc.name, "next_run_at", add_to_date(now_datetime(), minutes=-3), update_modified=False
		)
		frappe.db.commit()
		frappe.set_user("Administrator")
		with patch("jarvis.chat.macros.run_macro", return_value={"ok": True}) as run:
			macro_scheduler.run_due_macros()
		self.assertIn(doc.name, [c.args[0] for c in run.call_args_list])
		nxt = get_datetime(frappe.db.get_value(MACRO, doc.name, "next_run_at"))
		self.assertEqual((nxt.hour, nxt.minute), (9, 0))
		self.assertGreater(nxt, now_datetime())

	def test_the_api_saves_and_returns_the_cron(self):
		from jarvis.chat import macros_api

		out = macros_api.create_macro(
			macro_name=f"{PFX}-api",
			steps=[{"prompt": "step one"}],
			schedule_enabled=1,
			schedule_frequency="weekly",
			schedule_time="09:30",
			schedule_weekday="Monday",
			schedule_cron="30 9 * * 1,3,5",
		)
		got = macros_api.get_macro(out["data"]["name"])
		self.assertEqual(got["schedule_cron"], "30 9 * * 1,3,5")
		self.assertEqual(got["schedule_frequency"], "weekly")
		self.assertIn("scheduled_step_budget", got)
		macros_api.update_macro(out["data"]["name"], schedule_frequency="hourly", schedule_cron="0 * * * *")
		self.assertEqual(frappe.db.get_value(MACRO, out["data"]["name"], "schedule_cron"), "0 * * * *")
		page = macros_api.list_macros_page(filters={"schedule_frequency": "hourly"})
		self.assertIn(out["data"]["name"], [r["name"] for r in page["rows"]])

	def test_the_admin_list_and_view_carry_the_cron(self):
		from jarvis.chat import macros_admin_api

		doc = self._macro("admin", schedule_frequency="hourly", schedule_cron="0 * * * *")
		frappe.set_user("Administrator")
		listed = macros_admin_api.admin_list_macros(search=f"{PFX}-admin")
		row = next(r for r in listed["rows"] if r["name"] == doc.name)
		self.assertEqual(row["schedule_cron"], "0 * * * *")
		self.assertEqual(macros_admin_api.admin_get_macro(doc.name)["schedule_cron"], "0 * * * *")
