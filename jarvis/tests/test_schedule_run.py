"""schedule_run: work set from chat to run once, later, as the user.

The chat had no way to act on "remind me Monday" or "check this next week and
tell me": the tool makes a one-time macro (or puts one of the user's macros on a
one-time schedule) behind a confirmation card, never uncarded.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from jarvis import api
from jarvis.chat import confirm_card
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tests.test_confirm_gate import _spy_mint
from jarvis.tests.test_macro_scheduler import MACRO, OWNER_OK, PFX, MacroSchedulerBase
from jarvis.tools.schedule_run import schedule_run


def _at(days: int = 2, hour: int = 9, minute: int = 30) -> datetime.datetime:
	return add_to_date(now_datetime(), days=days).replace(hour=hour, minute=minute, second=0, microsecond=0)


class TestScheduleRun(MacroSchedulerBase):
	def setUp(self):
		super().setUp()
		frappe.set_user(OWNER_OK)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_a_prompt_becomes_a_one_time_macro(self):
		when = _at()
		out = schedule_run(
			run_at=when.strftime("%Y-%m-%d %H:%M"),
			prompt="Check the comments on HR-EMP-0001 and tell me about the review",
			title=f"{PFX}-review",
		)
		doc = frappe.get_doc(MACRO, out["macro"])
		self.assertEqual(doc.owner, OWNER_OK)
		self.assertEqual(doc.macro_name, f"{PFX}-review")
		self.assertEqual((doc.schedule_enabled, doc.schedule_frequency), (1, "once"))
		self.assertEqual(get_datetime(doc.next_run_at), when)
		self.assertEqual(doc.skip_confirmation, 0, "a macro from chat is never armed")
		self.assertEqual(
			[s.prompt for s in doc.steps], ["Check the comments on HR-EMP-0001 and tell me about the review"]
		)
		self.assertEqual(out["link"], f"/jarvis/macros/{doc.name}")
		self.assertEqual(get_datetime(out["run_at"]), when)

	def test_the_title_defaults_from_the_prompt_and_stays_unique(self):
		when = _at().strftime("%Y-%m-%d %H:%M")
		prompt = f"{PFX} follow up on the pending purchase orders with every supplier this week"
		first = schedule_run(run_at=when, prompt=prompt)
		second = schedule_run(run_at=when, prompt=prompt)
		a = frappe.db.get_value(MACRO, first["macro"], "macro_name")
		b = frappe.db.get_value(MACRO, second["macro"], "macro_name")
		self.assertTrue(a.startswith(f"{PFX} follow up on the pending purchase orders"))
		self.assertTrue(a.endswith("..."))
		self.assertEqual(b, f"{a} (2)")

	def test_a_past_or_bad_time_is_refused(self):
		with self.assertRaisesRegex(InvalidArgumentError, "already passed"):
			schedule_run(run_at=_at(days=-1).strftime("%Y-%m-%d %H:%M"), prompt="x")
		with self.assertRaisesRegex(InvalidArgumentError, "needs a time of day"):
			schedule_run(run_at="next monday", prompt="x")
		with self.assertRaisesRegex(InvalidArgumentError, "must be a date and time"):
			schedule_run(run_at="2030-13-45 10:00", prompt="x")
		with self.assertRaisesRegex(InvalidArgumentError, "run_at is required"):
			schedule_run(run_at="", prompt="x")

	def test_exactly_one_of_prompt_or_macro(self):
		when = _at().strftime("%Y-%m-%d %H:%M")
		with self.assertRaisesRegex(InvalidArgumentError, "exactly one"):
			schedule_run(run_at=when)
		with self.assertRaisesRegex(InvalidArgumentError, "exactly one"):
			schedule_run(run_at=when, prompt="x", macro="y")

	def test_an_owner_the_scheduler_refuses_is_told_why(self):
		frappe.set_user("Administrator")
		with self.assertRaisesRegex(PermissionDeniedError, "cannot run as Administrator"):
			schedule_run(run_at=_at().strftime("%Y-%m-%d %H:%M"), prompt="x")

	def _existing(self, tag: str, **values):
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-{tag}",
				"steps": [{"prompt": "month-end checks"}],
				**values,
			}
		)
		doc.insert()
		frappe.db.commit()
		return doc

	def test_an_unscheduled_macro_is_run_once(self):
		m = self._existing("month-end")
		when = _at(days=5)
		out = schedule_run(run_at=when.strftime("%Y-%m-%d %H:%M"), macro=f"{PFX}-month-end")
		self.assertEqual(out["macro"], m.name)
		row = frappe.db.get_value(
			MACRO, m.name, ["schedule_enabled", "schedule_frequency", "next_run_at"], as_dict=True
		)
		self.assertEqual((row.schedule_enabled, row.schedule_frequency), (1, "once"))
		self.assertEqual(get_datetime(row.next_run_at), when)

	def test_a_macro_it_must_not_change_is_left_alone(self):
		when = _at().strftime("%Y-%m-%d %H:%M")
		self._existing("daily", schedule_enabled=1, schedule_frequency="daily")
		with self.assertRaisesRegex(InvalidArgumentError, "already has a schedule"):
			schedule_run(run_at=when, macro=f"{PFX}-daily")
		self._existing("draft", enabled=0)
		with self.assertRaisesRegex(InvalidArgumentError, "switched off"):
			schedule_run(run_at=when, macro=f"{PFX}-draft")
		with self.assertRaisesRegex(InvalidArgumentError, "no macro of yours"):
			schedule_run(run_at=when, macro=f"{PFX}-nope")

	def test_an_armed_macro_is_refused(self):
		m = self._existing("armed")
		frappe.db.set_value(MACRO, m.name, "skip_confirmation", 1)
		with self.assertRaisesRegex(InvalidArgumentError, "armed"):
			schedule_run(run_at=_at().strftime("%Y-%m-%d %H:%M"), macro=m.name)
		self.assertEqual(frappe.db.get_value(MACRO, m.name, "schedule_enabled"), 0)

	def test_someone_elses_macro_is_not_found(self):
		frappe.set_user("Administrator")
		other = frappe.get_doc({"doctype": MACRO, "macro_name": f"{PFX}-theirs", "steps": [{"prompt": "x"}]})
		other.insert()
		frappe.set_user(OWNER_OK)
		with self.assertRaisesRegex(InvalidArgumentError, "no macro of yours"):
			schedule_run(run_at=_at().strftime("%Y-%m-%d %H:%M"), macro=other.name)


class TestScheduleRunGate(MacroSchedulerBase):
	def test_it_always_parks_a_card_with_its_own_shape(self):
		args = {"run_at": "2030-01-15 09:30", "prompt": "Remind me about the review", "title": "Review"}
		patcher, captured = _spy_mint()
		with patch("jarvis.api.dispatch") as disp, patcher:
			r = api._run_tool("schedule_run", args)
		self.assertFalse(disp.called, "scheduling ran before the user confirmed")
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		self.assertIn("schedule_run", api._BRAKE)
		self.assertNotIn("schedule_run", api._COVERED)
		card = confirm_card.build_card("schedule_run", args, None)
		self.assertEqual(
			card,
			{
				"kind": "schedule",
				"when": f"Tue, 15 Jan 2030 at 9:30 am ({frappe.utils.get_system_timezone()})",
				"name": "Review",
				"macro": "",
				"prompt": "Remind me about the review",
			},
		)
		self.assertTrue(captured.get("token"))

	def test_the_summary_floor_names_the_time(self):
		self.assertIn(
			"run_at=2030-01-15 09:30", api._describe_call("schedule_run", {"run_at": "2030-01-15 09:30"})
		)


class TestScheduleRunInputs(MacroSchedulerBase):
	def setUp(self):
		super().setUp()
		frappe.set_user(OWNER_OK)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_an_offset_is_converted_to_site_time(self):
		from zoneinfo import ZoneInfo

		when = _at(days=3, hour=4, minute=0)
		site = ZoneInfo(frappe.utils.get_system_timezone())
		as_utc = when.replace(tzinfo=site).astimezone(ZoneInfo("UTC"))
		out = schedule_run(run_at=as_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), prompt="x", title=f"{PFX}-utc")
		self.assertEqual(get_datetime(out["run_at"]), when)

	def test_a_date_without_a_time_is_refused(self):
		with self.assertRaisesRegex(InvalidArgumentError, "needs a time of day"):
			schedule_run(run_at=_at().strftime("%Y-%m-%d"), prompt="x")

	def test_seconds_round_up_to_the_next_minute(self):
		soon = add_to_date(now_datetime(), seconds=40)
		out = schedule_run(run_at=soon.strftime("%Y-%m-%d %H:%M:%S"), prompt="x", title=f"{PFX}-soon")
		slot = get_datetime(out["run_at"])
		self.assertGreater(slot, now_datetime())
		self.assertEqual(slot.second, 0)

	def test_arguments_must_be_text(self):
		with self.assertRaisesRegex(InvalidArgumentError, "title must be text"):
			schedule_run(run_at=_at().strftime("%Y-%m-%d %H:%M"), prompt="x", title=["x"])
		with self.assertRaisesRegex(InvalidArgumentError, "macro must be text"):
			schedule_run(run_at=_at().strftime("%Y-%m-%d %H:%M"), macro={"like": "%"})

	def test_an_over_long_prompt_is_refused(self):
		from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import MAX_PROMPT_LEN

		with self.assertRaisesRegex(InvalidArgumentError, "prompt must be at most"):
			schedule_run(run_at=_at().strftime("%Y-%m-%d %H:%M"), prompt="x" * (MAX_PROMPT_LEN + 1))

	def test_before_the_migrate_it_says_to_wait(self):
		with patch("jarvis.chat.macro_scheduler.once_ready", return_value=False):
			with self.assertRaisesRegex(InvalidArgumentError, "being set up"):
				schedule_run(run_at=_at().strftime("%Y-%m-%d %H:%M"), prompt="x")

	def test_a_held_macro_is_refused(self):
		from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import hold_fields_exist

		if not hold_fields_exist():
			self.skipTest("hold fields not migrated")
		frappe.set_user("Administrator")
		m = frappe.get_doc({"doctype": MACRO, "macro_name": f"{PFX}-held", "steps": [{"prompt": "x"}]})
		m.insert()
		frappe.db.set_value(MACRO, m.name, {"owner": OWNER_OK, "admin_hold": 1})
		frappe.db.commit()
		frappe.set_user(OWNER_OK)
		with self.assertRaisesRegex(InvalidArgumentError, "on hold"):
			schedule_run(run_at=_at().strftime("%Y-%m-%d %H:%M"), macro=m.name)

	def test_the_cap_does_not_count_spent_one_time_macros(self):
		from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import MAX_MACROS_PER_OWNER

		when = _at().strftime("%Y-%m-%d %H:%M")
		names = [
			schedule_run(run_at=when, prompt="x", title=f"{PFX}-cap-{i}")["macro"]
			for i in range(MAX_MACROS_PER_OWNER - frappe.db.count(MACRO, {"owner": OWNER_OK}))
		]
		with self.assertRaisesRegex(InvalidArgumentError, "most allowed"):
			schedule_run(run_at=when, prompt="x", title=f"{PFX}-cap-over")
		frappe.db.set_value(
			MACRO, names[0], {"schedule_enabled": 0, "next_run_at": None, "last_run_at": now_datetime()}
		)
		frappe.db.commit()
		self.assertTrue(schedule_run(run_at=when, prompt="x", title=f"{PFX}-cap-after")["macro"])
