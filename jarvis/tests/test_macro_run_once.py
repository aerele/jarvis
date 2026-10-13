"""One-time macro schedules (``schedule_frequency = "once"``).

A schedule could only repeat (daily / weekly / monthly), so "run this on Monday at
9" left a macro firing every Monday after. A one-time schedule fires on
``schedule_date`` at ``schedule_time`` and switches itself off in the same locked
write that claims the slot, so it can never fire twice. A slot that failed to start
is retried once, 55 minutes later; then it stays off and the owner is told.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from jarvis.chat import macro_scheduler, macros, macros_api
from jarvis.tests.test_macro_scheduler import (
	MACRO,
	OWNER_OK,
	PFX,
	RUN,
	MacroSchedulerBase,
	_Killed,
	_runs_for,
)


def _slot(minutes_from_now: int) -> datetime.datetime:
	return add_to_date(now_datetime(), minutes=minutes_from_now).replace(second=0, microsecond=0)


def _mk_once(tag: str, *, minutes_from_now: int = -60, enabled: int = 1):
	"""A one-time macro owned by OWNER_OK whose slot is ``minutes_from_now`` away
	(negative: already due). Saved with a future slot, then moved by raw writes,
	since a save refuses a slot in the past."""
	tomorrow = add_to_date(now_datetime(), days=1)
	doc = frappe.get_doc(
		{
			"doctype": MACRO,
			"macro_name": f"{PFX}-{tag}",
			"enabled": enabled,
			"schedule_enabled": 1,
			"schedule_frequency": "once",
			"schedule_date": tomorrow.date(),
			"schedule_time": "09:00:00",
			"steps": [{"prompt": "check the review comments"}],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	slot = _slot(minutes_from_now)
	frappe.db.set_value(
		MACRO,
		doc.name,
		{
			"owner": OWNER_OK,
			"schedule_date": slot.date(),
			"schedule_time": slot.strftime("%H:%M:00"),
			"next_run_at": slot,
		},
		update_modified=False,
	)
	frappe.db.commit()
	return doc


def _state(name: str):
	return frappe.db.get_value(
		MACRO, name, ["schedule_enabled", "next_run_at", "last_run_at", "schedule_date"], as_dict=True
	)


def _due_now(name: str) -> None:
	"""Make the row's current ``next_run_at`` (a lease) come due, as time would."""
	frappe.db.set_value(MACRO, name, "next_run_at", _slot(-1), update_modified=False)
	frappe.db.commit()


def _leased(test, name: str) -> None:
	"""The slot is held by a lease about _RETRY_AFTER_S out, the schedule still on."""
	state = _state(name)
	test.assertEqual(state.schedule_enabled, 1, "the one-time slot was switched off before it ran")
	wait = macro_scheduler._RETRY_AFTER_S
	test.assertGreater(get_datetime(state.next_run_at), add_to_date(now_datetime(), seconds=wait - 120))
	test.assertLessEqual(get_datetime(state.next_run_at), add_to_date(now_datetime(), seconds=wait))


def _notes(tag: str) -> list:
	return frappe.get_all(
		"Notification Log",
		filters={"for_user": OWNER_OK, "subject": ["like", f"%{PFX}-{tag}%"]},
		fields=["subject", "email_content", "link"],
		order_by="creation asc",
	)


class TestComputeOnce(FrappeTestCase):
	def test_future_slot_is_returned(self):
		day = datetime.date(2030, 1, 15)
		self.assertEqual(
			macro_scheduler.compute_next_run("once", "14:30:00", schedule_date=day),
			datetime.datetime(2030, 1, 15, 14, 30),
		)

	def test_past_missing_or_garbage_date_is_none(self):
		self.assertIsNone(
			macro_scheduler.compute_next_run("once", "09:00", schedule_date=datetime.date(2001, 1, 1))
		)
		self.assertIsNone(macro_scheduler.compute_next_run("once", "09:00"))
		self.assertIsNone(macro_scheduler.compute_next_run("once", "09:00", schedule_date="not-a-date"))

	def test_unset_time_runs_at_nine(self):
		self.assertEqual(
			macro_scheduler.compute_next_run("once", None, schedule_date="2030-01-15"),
			datetime.datetime(2030, 1, 15, 9, 0),
		)

	def test_recurring_frequencies_are_untouched(self):
		base = datetime.datetime(2030, 1, 15, 8, 0)
		self.assertEqual(
			macro_scheduler.compute_next_run("daily", "09:00", from_dt=base, schedule_date="2030-02-01"),
			datetime.datetime(2030, 1, 15, 9, 0),
		)


class TestOnceSave(MacroSchedulerBase):
	def _new(self, **schedule):
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-save-{frappe.generate_hash(length=6)}",
				"schedule_enabled": 1,
				"schedule_frequency": "once",
				"steps": [{"prompt": "x"}],
				**schedule,
			}
		)
		doc.flags.ignore_permissions = True
		return doc

	def test_a_future_slot_sets_next_run(self):
		day = add_to_date(now_datetime(), days=3).date()
		doc = self._new(schedule_date=day, schedule_time="10:15:00")
		doc.insert()
		self.assertEqual(get_datetime(doc.next_run_at), datetime.datetime.combine(day, datetime.time(10, 15)))

	def test_a_missing_date_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "date a one-time schedule"):
			self._new(schedule_time="10:00:00").insert()

	def test_a_past_slot_is_refused(self):
		yesterday = add_to_date(now_datetime(), days=-1).date()
		with self.assertRaisesRegex(frappe.ValidationError, "in the future"):
			self._new(schedule_date=yesterday, schedule_time="10:00:00").insert()

	def test_an_unrelated_save_of_a_due_slot_is_allowed(self):
		m = _mk_once("rename")
		doc = frappe.get_doc(MACRO, m.name)
		doc.description = "renamed while due"
		doc.flags.ignore_permissions = True
		doc.save()  # the slot is in the past, but the schedule did not change
		self.assertEqual(frappe.db.get_value(MACRO, m.name, "schedule_enabled"), 1)

	def test_switching_back_on_after_it_ran_needs_a_new_date(self):
		m = _mk_once("again")
		frappe.db.set_value(MACRO, m.name, {"schedule_enabled": 0, "next_run_at": None})
		doc = frappe.get_doc(MACRO, m.name)
		doc.schedule_enabled = 1
		doc.flags.ignore_permissions = True
		with self.assertRaisesRegex(frappe.ValidationError, "in the future"):
			doc.save()
		doc = frappe.get_doc(MACRO, m.name)  # the refused save stamped this copy
		doc.schedule_enabled = 1
		doc.schedule_date = add_to_date(now_datetime(), days=2).date()
		doc.flags.ignore_permissions = True
		doc.save()
		self.assertTrue(frappe.db.get_value(MACRO, m.name, "next_run_at"))


class TestOnceSweep(MacroSchedulerBase):
	def test_runs_once_then_switches_itself_off(self):
		m = _mk_once("ran")
		self.assertEqual(self._run_due(), [m.name])
		state = _state(m.name)
		self.assertEqual(state.schedule_enabled, 0)
		self.assertIsNone(state.next_run_at)
		self.assertTrue(state.last_run_at)
		self.assertNotIn(m.name, self._run_due(), "a one-time schedule fired twice")

	def test_a_future_slot_is_not_run(self):
		m = _mk_once("later", minutes_from_now=120)
		self.assertNotIn(m.name, self._run_due())
		self.assertEqual(_state(m.name).schedule_enabled, 1)

	def test_a_failed_start_is_retried_once_then_left_off(self):
		m = _mk_once("retry")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		state = _state(m.name)
		self.assertEqual(state.schedule_enabled, 1, "the one retry was not scheduled")
		wait = macro_scheduler._RETRY_AFTER_S
		self.assertLessEqual(get_datetime(state.next_run_at), add_to_date(now_datetime(), seconds=wait))
		self.assertTrue(macro_scheduler.is_retry_pending(frappe.get_doc(MACRO, m.name)))
		self.assertIn("retry within the hour", _notes("retry")[-1].email_content)

		# The retry comes due and fails too: no second retry, the schedule stays off.
		frappe.db.set_value(MACRO, m.name, "next_run_at", _slot(-1), update_modified=False)
		frappe.db.commit()
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		state = _state(m.name)
		self.assertEqual(state.schedule_enabled, 0)
		self.assertIsNone(state.next_run_at)
		self.assertIn("will not run again", _notes("retry")[-1].email_content)
		self.assertEqual([r.status for r in _runs_for(m.name)], ["failed", "failed"])

	def test_an_owner_save_during_the_run_is_kept(self):
		m = _mk_once("owner-edit")
		later = add_to_date(now_datetime(), days=3).date()

		def owner_moves_it(*a, **k):
			# While the run is being dispatched, the owner moves the schedule.
			doc = frappe.get_doc(MACRO, m.name)
			doc.schedule_date = later
			doc.flags.ignore_permissions = True
			doc.save()
			frappe.db.commit()
			return {"ok": True}

		with patch("jarvis.chat.macros.run_macro", side_effect=owner_moves_it):
			macro_scheduler.run_due_macros()
		state = _state(m.name)
		self.assertEqual(
			state.schedule_enabled, 1, "the end of the run switched off the owner's new schedule"
		)
		self.assertEqual(get_datetime(state.next_run_at).date(), later)

	def test_a_worker_killed_before_the_run_leaves_the_slot_to_come_due(self):
		m = _mk_once("killed")
		with patch("jarvis.chat.macros.run_macro", side_effect=_Killed()):
			with self.assertRaises(_Killed):
				macro_scheduler.run_due_macros()
		frappe.db.rollback()
		_leased(self, m.name)
		# The lease comes due: no run was started, so this is the one retry, and it runs.
		_due_now(m.name)
		self.assertEqual(self._run_due(), [m.name])
		self.assertEqual(_state(m.name).schedule_enabled, 0)

	def test_a_worker_killed_after_the_run_started_does_not_run_it_twice(self):
		m = _mk_once("killed-late")
		with patch("jarvis.chat.macros.run_macro", side_effect=_Killed()):
			with self.assertRaises(_Killed):
				macro_scheduler.run_due_macros()
		frappe.db.rollback()
		# The run had started (its row and conversation exist) when the worker died.
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "x"}).insert(
			ignore_permissions=True
		)
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"macro": m.name,
				"status": "running",
				"trigger": "scheduled",
				"conversation": conv.name,
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		frappe.db.commit()
		_due_now(m.name)
		self.assertEqual(self._run_due(), [], "the run that already started was dispatched again")
		state = _state(m.name)
		self.assertEqual(state.schedule_enabled, 0)
		self.assertTrue(state.last_run_at)

	def test_a_refusal_for_good_ends_it_without_a_resume_promise(self):
		m = _mk_once("refused")
		with patch("jarvis.chat.macros.run_macro", return_value={"ok": False, "reason": "usage_limit"}):
			macro_scheduler.run_due_macros()
		state = _state(m.name)
		self.assertEqual(state.schedule_enabled, 0)
		self.assertFalse(state.last_run_at, "a refused one-time slot was stamped as run")
		note = _notes("refused")[-1].email_content
		self.assertIn("One-time run skipped", note)
		self.assertIn("will not run again", note)
		self.assertNotIn("resume", note)

	def test_a_dispatch_that_did_not_start_keeps_the_lease_as_the_retry(self):
		m = _mk_once("not-dispatched")
		with patch(
			"jarvis.chat.macros.run_macro",
			return_value={"ok": False, "reason": macros.BLOCK_DISPATCH_FAILED},
		):
			macro_scheduler.run_due_macros()
		_leased(self, m.name)
		self.assertIn("retry within the hour", _notes("not-dispatched")[-1].email_content)

	def test_a_failure_before_the_claim_moves_the_slot_once(self):
		m = _mk_once("pre-claim")
		with patch.object(macro_scheduler, "_claim_slot", side_effect=RuntimeError("db blip")):
			macro_scheduler.run_due_macros()
		_leased(self, m.name)
		self.assertIn("retry within the hour", _notes("pre-claim")[-1].email_content)

	def test_a_switched_off_macro_loses_the_slot_and_the_owner_is_told(self):
		m = _mk_once("off", enabled=0)
		self.assertNotIn(m.name, self._run_due())
		state = _state(m.name)
		self.assertEqual(state.schedule_enabled, 0)
		self.assertIsNone(state.next_run_at)
		self.assertFalse(state.last_run_at)
		self.assertIn("switched off at its time", _notes("off")[-1].email_content)
		self.assertEqual([r.status for r in _runs_for(m.name)], ["failed"])


class TestOnceFinishedNotice(MacroSchedulerBase):
	def _announce(self, m, status, error="", trigger="scheduled"):
		run = frappe._dict(name="RUN-1", trigger=trigger, conversation="conv-once-1")
		with patch.object(macros, "_post_closing_message"), patch.object(macros, "_publish_done"):
			macros._announce(run, frappe.get_doc(MACRO, m.name), status, error)

	def test_a_finished_one_time_run_tells_the_owner_with_a_link(self):
		m = _mk_once("done")
		self._announce(m, "completed")
		[note] = _notes("done")
		self.assertIn("Your scheduled run finished", note.subject)
		self.assertEqual(note.link, "/jarvis/c/conv-once-1")

	def test_recurring_and_manual_successes_stay_quiet(self):
		m = _mk_once("quiet")
		self._announce(m, "completed", trigger="manual")
		frappe.db.set_value(MACRO, m.name, "schedule_frequency", "daily")
		self._announce(m, "completed")
		self.assertEqual(_notes("quiet"), [])


class TestOnceApi(MacroSchedulerBase):
	def test_create_and_read_back(self):
		day = add_to_date(now_datetime(), days=4).date()
		frappe.set_user(OWNER_OK)
		try:
			made = macros_api.create_macro(
				macro_name=f"{PFX}-api",
				steps=[{"prompt": "remind me"}],
				schedule_enabled=1,
				schedule_frequency="once",
				schedule_date=str(day),
				schedule_time="11:00",
			)
			got = macros_api.get_macro(made["data"]["name"])
			page = macros_api.list_macros_page()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(got["schedule_frequency"], "once")
		self.assertEqual(got["schedule_date"], str(day))
		self.assertEqual(get_datetime(got["next_run_at"]), datetime.datetime.combine(day, datetime.time(11)))
		[row] = [r for r in page["rows"] if r["name"] == made["data"]["name"]]
		self.assertEqual(row["schedule_date"], str(day))


class TestOnceSaveThroughTheForm(MacroSchedulerBase):
	"""The form posts the date as text ("2026-11-02"); the row holds a date."""

	def test_saving_the_same_date_is_not_a_change(self):
		m = _mk_once("same-date")
		before = _state(m.name)
		frappe.set_user(OWNER_OK)
		try:
			macros_api.update_macro(m.name, description="noted", schedule_date=str(before.schedule_date))
		finally:
			frappe.set_user("Administrator")
		after = _state(m.name)
		self.assertEqual(after.next_run_at, before.next_run_at, "an unrelated save moved the due slot")
		self.assertEqual(after.schedule_enabled, 1)

	def test_a_new_date_moves_the_run(self):
		m = _mk_once("new-date")
		day = add_to_date(now_datetime(), days=6).date()
		frappe.set_user(OWNER_OK)
		try:
			macros_api.update_macro(m.name, schedule_date=str(day))
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(get_datetime(_state(m.name).next_run_at).date(), day)


class TestBeforeTheMigrate(MacroSchedulerBase):
	"""Code served before the migrate adds schedule_date: nothing reads the column."""

	def test_the_sweep_and_the_lists_work_without_the_column(self):
		_mk_once("pre-migrate", minutes_from_now=120)
		with patch.object(macro_scheduler, "once_ready", return_value=False):
			macro_scheduler.run_due_macros()
			frappe.set_user(OWNER_OK)
			try:
				page = macros_api.list_macros_page()
				macros_api.list_macros()
			finally:
				frappe.set_user("Administrator")
		self.assertTrue(all("schedule_date" not in r for r in page["rows"]))


class TestSpentOneTimeMacrosDoNotFillTheCap(MacroSchedulerBase):
	def test_a_spent_one_time_macro_does_not_count(self):
		from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import (
			MAX_MACROS_PER_OWNER,
			macros_counted_toward_cap,
		)

		spent = _mk_once("spent")
		frappe.db.set_value(
			MACRO, spent.name, {"schedule_enabled": 0, "next_run_at": None, "last_run_at": now_datetime()}
		)
		frappe.db.commit()
		total = frappe.db.count(MACRO, {"owner": OWNER_OK})
		self.assertEqual(macros_counted_toward_cap(OWNER_OK), total - 1)
		self.assertLess(macros_counted_toward_cap(OWNER_OK), MAX_MACROS_PER_OWNER)
