"""Scheduled-macro dispatch: identity, caps and failure handling.

Covers the three defects the hourly ``run_due_macros`` cron shipped with:

* **#469** — it gated only on ``has_jarvis_access``, which returns True for
  ``Administrator`` and never reads ``User.enabled``, so an unattended turn could
  bind to a fully perm-bypassing identity or to an offboarded employee.
* **#468** — macro steps never passed the entitlement gate (``validate_can_send``)
  and had no run budget of any kind, so they drained the owner's quota without
  ever being refused by it.
* **#471** — a failed run advanced its schedule anyway, recorded nothing the owner
  could see, and could strand a run in ``running`` forever (no reaper).

Nothing here reaches a live gateway. Tests about the SCHEDULER's own decisions stub
``macros.run_macro`` wholesale and assert on which macros it dispatched; tests about
the #468 gate, which lives INSIDE ``run_macro``, stub only the turn dispatch
(``api._enqueue_turn``) so the real gate executes, and assert on the run rows it
did or did not leave behind. ``run_due_macros`` sweeps every due macro on the site,
so assertions are always scoped to this suite's own rows.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import frappe
from frappe.desk.doctype.notification_log.notification_log import NotificationLog
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from jarvis.chat import macro_scheduler, macros, macros_api

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"

PFX = "msched"
OWNER_OK = "msched-owner@example.com"
OWNER_OFF = "msched-disabled@example.com"


def _ensure_user(email: str, *, enabled: int = 1) -> str:
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
	# enabled is set LAST and with a raw write: User.validate refuses some edits on
	# a disabled row, and add_roles on a disabled user is a no-op.
	frappe.db.set_value("User", email, {"enabled": enabled, "user_type": "System User"})
	return email


def _mk_macro(owner: str, tag: str, *, due: bool = True, enabled: int = 1, steps: int = 1):
	doc = frappe.get_doc(
		{
			"doctype": MACRO,
			"macro_name": f"{PFX}-{tag}",
			"enabled": enabled,
			"schedule_enabled": 1,
			"schedule_frequency": "daily",
			"steps": [{"prompt": f"step {i}"} for i in range(1, steps + 1)],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.set_value(MACRO, doc.name, "owner", owner, update_modified=False)
	frappe.db.set_value(
		MACRO,
		doc.name,
		"next_run_at",
		add_to_date(now_datetime(), hours=-1) if due else add_to_date(now_datetime(), days=1),
		update_modified=False,
	)
	frappe.db.commit()
	return doc


def _purge() -> None:
	"""Drop this suite's macros + their runs. Several paths under test COMMIT, so
	the per-test rollback does not clean up after them, and leaked macros count
	against ``MAX_MACROS_PER_OWNER`` (25) until every later insert throws."""
	for n in frappe.get_all(MACRO, filters={"macro_name": ["like", f"{PFX}-%"]}, pluck="name"):
		for run in frappe.get_all(RUN, filters={"macro": n}, pluck="name"):
			frappe.delete_doc(RUN, run, force=True, ignore_permissions=True)
		frappe.delete_doc(MACRO, n, force=True, ignore_permissions=True)
	for n in frappe.get_all("Notification Log", filters={"subject": ["like", f"%{PFX}-%"]}, pluck="name"):
		frappe.delete_doc("Notification Log", n, force=True, ignore_permissions=True)
	frappe.db.commit()


_SWITCH_OFF_NOTICE = "Macro schedules switched off:%"


def _switch_off_notices() -> set:
	return set(
		frappe.get_all("Notification Log", filters={"subject": ["like", _SWITCH_OFF_NOTICE]}, pluck="name")
	)


def _runs_for(macro_name: str) -> list:
	return frappe.get_all(
		RUN,
		filters={"macro": macro_name},
		fields=["name", "status", "error", "owner", "trigger"],
		order_by="creation asc",
	)


def _due_names(macros_: list, *, days_ahead: int = 0) -> list:
	"""Which of these macros a sweep would pick up, by the sweep's own filter. With
	``days_ahead`` it answers "would ANY sweep in that time", i.e. is it scheduled at all."""
	return frappe.get_all(
		MACRO,
		filters={
			"name": ["in", [m.name for m in macros_]],
			"schedule_enabled": 1,
			"next_run_at": ["<=", add_to_date(now_datetime(), days=days_ahead)],
		},
		pluck="name",
	)


class MacroSchedulerBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(OWNER_OK, enabled=1)
		_ensure_user(OWNER_OFF, enabled=0)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		_purge()
		# A sweep that meets a barred owner tells EVERY Jarvis Admin on the site, other
		# suites' users included, and the notice about Administrator carries nothing
		# `_purge` can find it by. Remember what was there, so tearDown removes exactly
		# what this test caused and nothing a real admin was sent.
		self._notices_before = _switch_off_notices()

	def tearDown(self):
		frappe.db.rollback()
		_purge()
		for n in _switch_off_notices() - self._notices_before:
			frappe.delete_doc("Notification Log", n, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def _run_due(self):
		"""Run the cron with dispatch stubbed; returns the macro names it dispatched."""
		with patch("jarvis.chat.macros.run_macro", return_value={"ok": True}) as mock_run:
			macro_scheduler.run_due_macros()
		return [c.args[0] for c in mock_run.call_args_list]

	def _mk_retryable(self, tag: str, **kw):
		"""A due macro whose NEXT natural slot is half a day away, so "a retry was
		scheduled" cannot be confused with "the slot was consumed". With the fixture's
		default 09:00 the two looked the same to a test that ran between 08:00 and 09:00."""
		m = _mk_macro(OWNER_OK, tag, **kw)
		far = add_to_date(now_datetime(), hours=12).strftime("%H:%M:00")
		frappe.db.set_value(MACRO, m.name, "schedule_time", far, update_modified=False)
		frappe.db.commit()
		return m

	def _assert_retry_scheduled(self, m):
		"""The slot was not consumed: it comes due again ``_RETRY_AFTER_S`` from the
		sweep, and nothing claims a run happened. It is not left due either, or the
		five-minute sweep would retry it twelve times an hour. Use with ``_mk_retryable``."""
		now = now_datetime()
		wait = macro_scheduler._RETRY_AFTER_S
		row = frappe.db.get_value(MACRO, m.name, ["next_run_at", "last_run_at"], as_dict=True)
		nxt = get_datetime(row.next_run_at)
		self.assertGreater(nxt, add_to_date(now, seconds=wait - 120), "the retry is far sooner than the wait")
		self.assertLessEqual(
			nxt, add_to_date(now, seconds=wait), "a failed run consumed its slot instead of retrying"
		)
		self.assertFalse(row.last_run_at, "last_run_at was stamped for a run that never happened")


# --------------------------------------------------------------------------- #
# #469 — the unattended-identity guard
# --------------------------------------------------------------------------- #
class TestScheduledMacroIdentity(MacroSchedulerBase):
	def test_administrator_owned_macro_is_refused(self):
		m = _mk_macro("Administrator", "admin-owned")
		self.assertNotIn(m.name, self._run_due(), "scheduler bound an unattended turn to Administrator")

	def test_disabled_owner_macro_is_refused(self):
		m = _mk_macro(OWNER_OFF, "disabled-owner")
		# The premise of the defect: the OLD gate still says this identity is fine.
		from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

		self.assertTrue(has_jarvis_access(OWNER_OFF), "premise changed: has_jarvis_access now filters")
		self.assertFalse(is_valid_unattended_owner(OWNER_OFF))
		self.assertNotIn(m.name, self._run_due(), "offboarding did not revoke scheduled ERP access")

	def test_enabled_jarvis_owner_still_runs(self):
		m = _mk_macro(OWNER_OK, "good-owner")
		self.assertIn(m.name, self._run_due(), "the guard over-reached and refused a legitimate owner")

	def test_refused_macro_does_not_busy_refire(self):
		# Was "the slot is moved on". The schedule is now switched OFF instead (see
		# TestLeaverSchedulesAreSwitchedOff), and that is what stops the re-fire.
		m = _mk_macro("Administrator", "admin-advance")
		self._run_due()
		self.assertEqual(_due_names([m]), [], "a refused macro stayed due and would re-fire every sweep")

	def test_refusal_is_recorded_as_a_failed_run(self):
		m = _mk_macro(OWNER_OFF, "disabled-recorded")
		self._run_due()
		runs = _runs_for(m.name)
		self.assertEqual([r.status for r in runs], ["failed"])
		self.assertEqual(runs[0].owner, OWNER_OFF, "the failed row is not visible to the owner")
		self.assertIn("unattended", runs[0].error)


# --------------------------------------------------------------------------- #
# Leavers: the first sweep that meets a barred owner switches ALL of that owner's
# schedules off and tells the site's admins once, instead of failing every slot
# forever and telling nobody
# --------------------------------------------------------------------------- #
ADMIN_A = "msched-admin-a@example.com"
ADMIN_B = "msched-admin-b@example.com"
ADMIN_OFF = "msched-admin-off@example.com"
LEAVER = "msched-leaver@example.com"
NO_ROLE = "msched-norole@example.com"


class _Killed(BaseException):
	"""A worker dying mid-sweep (a deploy restart, the OOM killer, a job timeout). Not
	an ``Exception``, so no handler in the sweep gets to tidy up after it."""


def _comments_on(macro_name: str) -> list:
	return frappe.get_all(
		"Comment",
		filters={"reference_doctype": MACRO, "reference_name": macro_name, "comment_type": "Info"},
		pluck="content",
	)


def _admin_notes(user: str, about: str) -> list:
	return frappe.get_all(
		"Notification Log",
		filters={"for_user": user, "subject": ["like", f"%{about}%"]},
		fields=["subject", "email_content", "document_type", "document_name"],
	)


def _schedule_state(macro_name: str):
	return frappe.db.get_value(
		MACRO,
		macro_name,
		["schedule_enabled", "next_run_at", "last_run_at", "enabled", "modified"],
		as_dict=True,
	)


class TestLeaverSchedulesAreSwitchedOff(MacroSchedulerBase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from jarvis.permissions import JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE, ensure_jarvis_admin_role

		ensure_jarvis_admin_role()
		for admin in (ADMIN_A, ADMIN_B, ADMIN_OFF):
			_ensure_user(admin, enabled=1)
			frappe.get_doc("User", admin).add_roles(JARVIS_ADMIN_ROLE)
		# A Jarvis Admin whose own account is disabled is nobody to tell.
		frappe.db.set_value("User", ADMIN_OFF, "enabled", 0)
		_ensure_user(LEAVER, enabled=1)
		# Enabled, a System User, and holding no Jarvis role: the "lost the role" leaver.
		_ensure_user(NO_ROLE, enabled=1)
		frappe.get_doc("User", NO_ROLE).remove_roles(JARVIS_USER_ROLE)
		frappe.db.set_value("User", NO_ROLE, {"enabled": 1, "user_type": "System User"})
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# These users are this class's own. Left behind, the three admins stay holders
		# of the Jarvis Admin role on a shared site, and every other suite that sweeps a
		# barred owner then notifies them.
		frappe.db.rollback()
		for user in (ADMIN_A, ADMIN_B, ADMIN_OFF, LEAVER, NO_ROLE):
			frappe.db.delete("Notification Log", {"for_user": user})
			frappe.delete_doc("User", user, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		self._enable_leaver()

	def tearDown(self):
		frappe.db.rollback()
		self._enable_leaver()
		super().tearDown()

	def _enable_leaver(self):
		frappe.db.set_value("User", LEAVER, "enabled", 1)
		frappe.db.commit()

	def _assert_sweep_order(self, *groups):
		"""The sweep handles the due macros in the order its query returns them, newest
		first. A test about what happens AFTER a barred owner was met proves nothing if
		the macro it watches is handled before that owner, so pin the order: every macro
		of one group comes before every macro of the next."""
		names = [m.name for group in groups for m in group]
		order = frappe.get_all(
			MACRO,
			filters={"name": ["in", names], "schedule_enabled": 1, "next_run_at": ["<=", now_datetime()]},
			pluck="name",
		)
		self.assertEqual(order, names, "the fixture no longer puts these macros in the order the test needs")

	def _assert_untouched(self, m):
		row = _schedule_state(m.name)
		self.assertEqual(cint(row.schedule_enabled), 1, f"{m.macro_name}: the schedule went off")
		self.assertIsNotNone(row.next_run_at)
		self.assertIsNone(row.last_run_at)
		self.assertEqual(_comments_on(m.name), [])
		self.assertEqual(_runs_for(m.name), [])

	def _assert_switched_off(self, m, *, by: str = OWNER_OFF):
		row = _schedule_state(m.name)
		self.assertEqual(cint(row.schedule_enabled), 0, f"{m.macro_name}: the schedule is still on")
		self.assertIsNone(
			row.next_run_at,
			f"{m.macro_name}: not the state the form leaves when the schedule is switched off",
		)
		self.assertEqual(
			_comments_on(m.name),
			[f"Schedule switched off: {by} can no longer use Jarvis"],
			f"{m.macro_name}: expected exactly one Comment saying why",
		)

	def test_first_encounter_switches_off_every_schedule_of_that_owner(self):
		due = _mk_macro(OWNER_OFF, "leave-due")
		later = _mk_macro(OWNER_OFF, "leave-later", due=False)
		paused = _mk_macro(OWNER_OFF, "leave-paused", due=False, enabled=0)
		before = _schedule_state(later.name)

		self.assertEqual(self._run_due(), [], "a barred owner's macro was dispatched")

		self.assertEqual([r.status for r in _runs_for(due.name)], ["failed"])
		self.assertIn("unattended", _runs_for(due.name)[0].error)
		for m in (due, later, paused):
			self._assert_switched_off(m)
		# Only the macro the sweep actually met has anything recorded against it.
		self.assertEqual(_runs_for(later.name), [])
		self.assertEqual(_runs_for(paused.name), [])
		after = _schedule_state(later.name)
		self.assertEqual(after.modified, before.modified, "the engine never saves a macro document")
		self.assertIsNone(after.last_run_at, "a macro that was not even due reads as having run")
		self.assertEqual(cint(_schedule_state(paused.name).enabled), 0)
		self.assertEqual(cint(_schedule_state(due.name).enabled), 1, "only the schedule goes off")

		for admin in (ADMIN_A, ADMIN_B):
			notes = _admin_notes(admin, OWNER_OFF)
			self.assertEqual(len(notes), 1, f"{admin} should be told exactly once")
			self.assertIn("3 macro schedules", notes[0].email_content)
			self.assertIn(OWNER_OFF, notes[0].email_content)
			self.assertFalse(notes[0].document_type or notes[0].document_name)
		self.assertEqual(_admin_notes(ADMIN_OFF, OWNER_OFF), [], "a disabled admin was notified")
		self.assertEqual(_admin_notes(OWNER_OFF, OWNER_OFF), [], "the leaver was notified")

	def test_an_unscheduled_macro_of_that_owner_is_not_touched(self):
		_mk_macro(OWNER_OFF, "leave-sched")
		manual = _mk_macro(OWNER_OFF, "leave-manual", due=False)
		frappe.db.set_value(
			MACRO, manual.name, {"schedule_enabled": 0, "next_run_at": None}, update_modified=False
		)
		frappe.db.commit()
		before = _schedule_state(manual.name)

		self._run_due()

		self.assertEqual(_schedule_state(manual.name), before)
		self.assertEqual(_comments_on(manual.name), [], "a macro with no schedule got a switch-off Comment")
		self.assertEqual(_runs_for(manual.name), [])
		self.assertIn("1 macro schedule ", _admin_notes(ADMIN_A, OWNER_OFF)[0].email_content)

	def test_three_due_macros_of_one_owner_run_the_branch_once(self):
		three = [_mk_macro(OWNER_OFF, f"leave-three-{i}") for i in range(3)]

		self._run_due()

		runs = [r for m in three for r in _runs_for(m.name)]
		self.assertEqual(len(runs), 1, "one failed run for the owner, not one per due macro")
		for m in three:
			self._assert_switched_off(m)
		for admin in (ADMIN_A, ADMIN_B):
			notes = _admin_notes(admin, OWNER_OFF)
			self.assertEqual(len(notes), 1, "one notification per admin, not one per due macro")
			self.assertIn("3 macro schedules", notes[0].email_content)

	def test_the_next_sweep_does_nothing_for_that_owner(self):
		three = [_mk_macro(OWNER_OFF, f"leave-next-{i}") for i in range(3)]
		self._run_due()
		self.assertEqual(_due_names(three, days_ahead=400), [], "a switched-off macro can still come due")

		self._run_due()

		self.assertEqual(len([r for m in three for r in _runs_for(m.name)]), 1)
		self.assertEqual(len(_admin_notes(ADMIN_A, OWNER_OFF)), 1)
		for m in three:
			self._assert_switched_off(m)

	def test_a_second_sweep_holding_the_same_stale_row_does_not_repeat_it(self):
		# Two sweeps overlapping: each read `due` before either switched anything off,
		# and neither has the other's memory of barred owners.
		m = _mk_macro(OWNER_OFF, "leave-overlap")
		stale = frappe.get_all(MACRO, filters={"name": m.name}, fields=["*"])[0]

		macro_scheduler._sweep_one(stale, now_datetime(), set())
		macro_scheduler._sweep_one(stale, now_datetime(), set())

		self.assertEqual(len(_runs_for(m.name)), 1, "the second sweep recorded the failure again")
		self._assert_switched_off(m)
		self.assertEqual(len(_admin_notes(ADMIN_A, OWNER_OFF)), 1, "the admins were told twice")

	def test_a_paused_macro_due_in_the_same_pass_is_not_given_a_next_run(self):
		# The sweep lists newest first, so `paused` is handled AFTER `live` switched the
		# owner's schedules off. Moving its slot on then would write a "Next run" back
		# onto a macro whose schedule is off.
		paused = _mk_macro(OWNER_OFF, "leave-paused-due", enabled=0)
		live = _mk_macro(OWNER_OFF, "leave-live-due")

		self._run_due()

		for m in (paused, live):
			self._assert_switched_off(m)
		self.assertEqual(_runs_for(paused.name), [], "a paused macro is not a failure")
		self.assertIsNone(_schedule_state(paused.name).last_run_at)

	def test_another_owners_macros_in_the_same_pass_still_run(self):
		# Created FIRST, so handled LAST: after the sweep has met the barred owner.
		mine = [_mk_macro(OWNER_OK, f"stay-mixed-{i}") for i in range(2)]
		gone = [_mk_macro(OWNER_OFF, f"leave-mixed-{i}") for i in range(2)]
		self._assert_sweep_order(gone[::-1], mine[::-1])

		ran = self._run_due()

		self.assertEqual(sorted(ran), sorted(m.name for m in mine))
		for m in mine:
			row = _schedule_state(m.name)
			self.assertEqual(cint(row.schedule_enabled), 1, "another owner's schedule was switched off")
			self.assertGreater(get_datetime(row.next_run_at), now_datetime())
			self.assertEqual(_comments_on(m.name), [])
			self.assertEqual(_runs_for(m.name), [], "run_macro is stubbed: nothing should be recorded")
		for m in gone:
			self._assert_switched_off(m)

	def test_two_barred_owners_and_a_healthy_one_in_the_same_pass(self):
		# The first sweeps after a deploy: several owners are already barred. What the
		# sweep remembers is WHICH owners it met, not that it met one.
		mine_0 = _mk_macro(OWNER_OK, "stay-two-0")
		gone = [_mk_macro(OWNER_OFF, f"leave-two-gone-{i}") for i in range(2)]
		mine_1 = _mk_macro(OWNER_OK, "stay-two-1")
		norole = [_mk_macro(NO_ROLE, f"leave-two-norole-{i}") for i in range(2)]
		self._assert_sweep_order(norole[::-1], [mine_1], gone[::-1], [mine_0])

		ran = self._run_due()

		self.assertEqual(
			sorted(ran), sorted([mine_0.name, mine_1.name]), "a healthy owner's macro was skipped"
		)
		for group, owner in ((gone, OWNER_OFF), (norole, NO_ROLE)):
			for m in group:
				self._assert_switched_off(m, by=owner)
			self.assertEqual(
				len([r for m in group for r in _runs_for(m.name)]), 1, f"{owner}: one failed run"
			)
			for admin in (ADMIN_A, ADMIN_B):
				notes = _admin_notes(admin, owner)
				self.assertEqual(len(notes), 1, f"{admin} should be told once about {owner}")
				self.assertIn("2 macro schedules", notes[0].email_content)
		for m in (mine_0, mine_1):
			self.assertEqual(cint(_schedule_state(m.name).schedule_enabled), 1)
			self.assertEqual(_comments_on(m.name), [])

	def test_an_owner_whose_only_due_macros_are_paused_is_not_switched_off_yet(self):
		# Stated, not fixed: a paused macro is handled before the owner is looked at
		# (its slot is moved on, nothing is recorded), so the branch is not reached
		# until a LIVE macro of that owner comes due.
		paused = _mk_macro(OWNER_OFF, "leave-only-paused", enabled=0)
		live = _mk_macro(OWNER_OFF, "leave-only-paused-live", due=False)

		self._run_due()

		row = _schedule_state(paused.name)
		self.assertEqual(cint(row.schedule_enabled), 1)
		self.assertGreater(get_datetime(row.next_run_at), now_datetime(), "the paused slot was not moved on")
		self.assertEqual(cint(_schedule_state(live.name).schedule_enabled), 1)
		for m in (paused, live):
			self.assertEqual(_comments_on(m.name), [])
			self.assertEqual(_runs_for(m.name), [])
		self.assertEqual(_admin_notes(ADMIN_A, OWNER_OFF), [])

		frappe.db.set_value(
			MACRO, live.name, "next_run_at", add_to_date(now_datetime(), hours=-1), update_modified=False
		)
		frappe.db.commit()
		self._run_due()

		for m in (paused, live):
			self._assert_switched_off(m)
		self.assertIn("2 macro schedules", _admin_notes(ADMIN_A, OWNER_OFF)[0].email_content)

	def test_a_stale_paused_row_does_not_put_a_next_run_back_on_a_switched_off_schedule(self):
		# An overlapping sweep that read this paused macro as due before the owner's
		# schedules were switched off, and has no memory of that owner.
		paused = _mk_macro(OWNER_OFF, "leave-stale-paused", enabled=0)
		_mk_macro(OWNER_OFF, "leave-stale-live")
		stale = frappe.get_all(MACRO, filters={"name": paused.name}, fields=["*"])[0]
		self._run_due()

		macro_scheduler._sweep_one(stale, now_datetime(), set())

		self._assert_switched_off(paused)
		self.assertIsNone(_schedule_state(paused.name).last_run_at)

	def test_a_worker_killed_before_the_failed_run_is_written_leaves_nothing_half_done(self):
		self._assert_a_kill_is_atomic(
			patch.object(macro_scheduler, "_insert_failed_run", side_effect=_Killed())
		)

	def test_a_worker_killed_while_writing_the_notices_leaves_nothing_half_done(self):
		# The first admin's notice is written, and the worker dies inside the second.
		self._assert_a_kill_is_atomic(
			patch.object(NotificationLog, "after_insert", side_effect=[None, _Killed()]), notices_written=2
		)

	def _assert_a_kill_is_atomic(self, kill, *, notices_written: int = 0):
		"""Once the schedules are off these macros are never due again, so nothing
		comes back for a failed run or a notice that was lost. Either all of it lands
		or none of it does, and "none" leaves the macro due for the next sweep."""
		due = _mk_macro(OWNER_OFF, "leave-killed-due")
		later = _mk_macro(OWNER_OFF, "leave-killed-later", due=False)
		before = len(_switch_off_notices())

		with kill, self.assertRaises(_Killed):
			self._run_due()
		# The kill landed where the test means it to: inside the open transaction.
		self.assertEqual(len(_switch_off_notices()) - before, notices_written)
		# A dead worker's connection goes away, and the database rolls back whatever
		# that connection had not committed.
		frappe.db.rollback()

		for m in (due, later):
			self._assert_untouched(m)
		self.assertEqual(
			len(_switch_off_notices()), before, "an admin was told about a switch-off that did not land"
		)
		self.assertEqual(_due_names([due]), [due.name], "nothing will come back to finish this")

		self._run_due()

		for m in (due, later):
			self._assert_switched_off(m)
		self.assertEqual([r.status for r in _runs_for(due.name)], ["failed"])
		for admin in (ADMIN_A, ADMIN_B):
			self.assertEqual(len(_admin_notes(admin, OWNER_OFF)), 1)

	def test_the_leaver_is_taken_out_before_system_managers_are_considered(self):
		# The leaver is the only holder of the Jarvis Admin role: there is nobody in
		# that role to tell, so the System Managers are.
		_mk_macro(OWNER_OFF, "leave-only-admin")
		holders = {"Jarvis Admin": [OWNER_OFF], "System Manager": [ADMIN_B]}
		with patch.object(macro_scheduler, "get_users_with_role", side_effect=lambda role: holders[role]):
			self._run_due()
		self.assertEqual(len(_admin_notes(ADMIN_B, OWNER_OFF)), 1, "nobody was told")
		self.assertEqual(_admin_notes(OWNER_OFF, OWNER_OFF), [], "the leaver was notified about themselves")

	def test_a_barred_owner_who_is_a_jarvis_admin_is_not_told_about_themselves(self):
		_mk_macro(OWNER_OFF, "leave-is-admin")
		holders = {"Jarvis Admin": [OWNER_OFF, ADMIN_A], "System Manager": [ADMIN_B]}
		with patch.object(macro_scheduler, "get_users_with_role", side_effect=lambda role: holders[role]):
			self._run_due()
		self.assertEqual(len(_admin_notes(ADMIN_A, OWNER_OFF)), 1)
		self.assertEqual(_admin_notes(OWNER_OFF, OWNER_OFF), [], "the leaver was notified about themselves")
		self.assertEqual(_admin_notes(ADMIN_B, OWNER_OFF), [], "another Jarvis Admin exists: no fallback")

	def test_with_nobody_to_tell_the_switch_off_still_leaves_a_trace(self):
		gone = _mk_macro(OWNER_OFF, "leave-nobody")
		with (
			patch.object(macro_scheduler, "get_users_with_role", return_value=[]),
			patch.object(frappe, "log_error") as logged,
		):
			self._run_due()
		self._assert_switched_off(gone)
		self.assertEqual([r.status for r in _runs_for(gone.name)], ["failed"])
		self.assertEqual(logged.call_count, 1)
		self.assertIn("not every admin was told", logged.call_args.kwargs["title"])
		self.assertIn("Nobody was told", logged.call_args.kwargs["message"])
		self._assert_names_no_user(logged)

	def _assert_names_no_user(self, logged):
		"""An Error Log leaves the site (the vendor's error rollup): no user in it."""
		for call in logged.call_args_list:
			text = f"{call.kwargs.get('title')}\n{call.kwargs.get('message')}"
			for user in (OWNER_OFF, ADMIN_A, ADMIN_B):
				self.assertNotIn(user, text)

	def test_system_managers_are_told_when_nobody_holds_the_admin_role(self):
		_mk_macro(OWNER_OFF, "leave-no-admins")
		holders = {"Jarvis Admin": [], "System Manager": [ADMIN_B]}
		with patch.object(macro_scheduler, "get_users_with_role", side_effect=lambda role: holders[role]):
			self._run_due()
		self.assertEqual(len(_admin_notes(ADMIN_B, OWNER_OFF)), 1)
		self.assertEqual(_admin_notes(ADMIN_A, OWNER_OFF), [])

	def test_system_managers_are_not_told_when_an_admin_exists(self):
		_mk_macro(OWNER_OFF, "leave-admins")
		holders = {"Jarvis Admin": [ADMIN_A], "System Manager": [ADMIN_B]}
		with patch.object(macro_scheduler, "get_users_with_role", side_effect=lambda role: holders[role]):
			self._run_due()
		self.assertEqual(len(_admin_notes(ADMIN_A, OWNER_OFF)), 1)
		self.assertEqual(_admin_notes(ADMIN_B, OWNER_OFF), [])

	def test_a_notification_that_cannot_be_written_does_not_undo_the_switch_off(self):
		# The real insert, failing for one admin AFTER its row was written (the hook
		# that marks the bell unseen raises). That notice alone is undone.
		gone = _mk_macro(OWNER_OFF, "leave-notify-fails")
		mine = _mk_macro(OWNER_OK, "stay-notify-fails")
		real = NotificationLog.after_insert

		def after_insert(doc):
			if doc.for_user == ADMIN_A:
				raise frappe.ValidationError(f"could not notify {doc.for_user} about {OWNER_OFF}")
			real(doc)

		with (
			patch.object(NotificationLog, "after_insert", after_insert),
			patch.object(frappe, "log_error") as logged,
		):
			ran = self._run_due()

		self.assertEqual(ran, [mine.name], "a failed notification aborted the sweep")
		self._assert_switched_off(gone)
		self.assertEqual([r.status for r in _runs_for(gone.name)], ["failed"])
		self.assertEqual(_admin_notes(ADMIN_A, OWNER_OFF), [], "a half-written notice was kept")
		self.assertEqual(len(_admin_notes(ADMIN_B, OWNER_OFF)), 1, "one failed notice cost the others")
		# Logged as what it is, and not left to the sweep's catch-all, which would
		# report the whole macro as a failed sweep.
		self.assertEqual(logged.call_count, 1, logged.call_args_list)
		self.assertIn("not every admin was told", logged.call_args.kwargs["title"])
		self.assertIn("1 of ", logged.call_args.kwargs["message"])
		self.assertIn("ValidationError", logged.call_args.kwargs["message"])
		self._assert_names_no_user(logged)

	def test_failing_to_find_the_admins_does_not_undo_the_switch_off(self):
		gone = _mk_macro(OWNER_OFF, "leave-lookup-fails")
		mine = _mk_macro(OWNER_OK, "stay-lookup-fails")
		with (
			patch.object(
				macro_scheduler, "get_users_with_role", side_effect=RuntimeError(f"no roles for {OWNER_OFF}")
			),
			patch.object(frappe, "log_error") as logged,
		):
			ran = self._run_due()

		self.assertEqual(ran, [mine.name], "a failed lookup aborted the sweep")
		self._assert_switched_off(gone)
		self.assertEqual([r.status for r in _runs_for(gone.name)], ["failed"])
		self.assertEqual(logged.call_count, 1, logged.call_args_list)
		self.assertIn("not every admin was told", logged.call_args.kwargs["title"])
		self.assertIn("could not be looked up", logged.call_args.kwargs["message"])
		self._assert_names_no_user(logged)

	def test_a_user_who_returns_does_not_get_their_schedules_back(self):
		m = _mk_macro(LEAVER, "leave-returns")
		frappe.db.set_value("User", LEAVER, "enabled", 0)
		frappe.db.commit()
		self._run_due()
		self._assert_switched_off(m, by=LEAVER)

		frappe.db.set_value("User", LEAVER, "enabled", 1)
		frappe.db.commit()
		from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

		self.assertTrue(is_valid_unattended_owner(LEAVER) and has_jarvis_access(LEAVER))

		self.assertNotIn(m.name, self._run_due(), "a returning user's macro ran without being switched on")
		self._assert_switched_off(m, by=LEAVER)
		self.assertEqual(_due_names([m], days_ahead=400), [])
		self.assertEqual(len(_runs_for(m.name)), 1)

		# The way back is the owner switching it on, which is an ordinary save.
		frappe.set_user(LEAVER)
		try:
			macros_api.update_macro(m.name, schedule_enabled=1)
		finally:
			frappe.set_user("Administrator")
		row = _schedule_state(m.name)
		self.assertEqual(cint(row.schedule_enabled), 1)
		self.assertGreater(get_datetime(row.next_run_at), now_datetime())

	def test_an_owner_who_lost_the_jarvis_role_is_a_leaver_too(self):
		from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

		self.assertTrue(is_valid_unattended_owner(NO_ROLE), "premise: the account itself is fine")
		self.assertFalse(has_jarvis_access(NO_ROLE), "premise: it holds no Jarvis role")
		m = _mk_macro(NO_ROLE, "leave-norole")

		self.assertEqual(self._run_due(), [])

		self._assert_switched_off(m, by=NO_ROLE)
		self.assertEqual(len(_admin_notes(ADMIN_A, NO_ROLE)), 1)

	def test_an_administrator_owned_macro_is_switched_off_with_wording_that_fits(self):
		# The save gate (admin-v2#675) refuses such a schedule, so only a row from before
		# it can get here. Administrator has not "left"; the text must not say so.
		m = _mk_macro("Administrator", "leave-root")

		self.assertEqual(self._run_due(), [])

		row = _schedule_state(m.name)
		self.assertEqual(cint(row.schedule_enabled), 0)
		self.assertIsNone(row.next_run_at)
		self.assertEqual([r.status for r in _runs_for(m.name)], ["failed"])
		self.assertEqual(
			_comments_on(m.name), ["Schedule switched off: Administrator may not run scheduled macros"]
		)
		notes = _admin_notes(ADMIN_A, "Administrator may not run scheduled macros")
		self.assertEqual(len(notes), 1)
		self.assertNotIn("no longer", notes[0].subject + notes[0].email_content)

	def test_when_the_switch_off_itself_fails_the_slot_is_still_consumed_once_per_owner(self):
		# The fallback is what the sweep did before: one failed run, the slot moved on.
		# The owner is still remembered for the pass, so three due macros do not become
		# three failed runs; the other two stay due and the next sweep tries again.
		three = [_mk_macro(OWNER_OFF, f"leave-broken-{i}") for i in range(3)]
		mine = _mk_macro(OWNER_OK, "stay-broken")

		with (
			patch.object(
				macro_scheduler, "_switch_off_and_record", side_effect=RuntimeError(f"no db for {OWNER_OFF}")
			),
			patch.object(frappe, "log_error") as logged,
		):
			ran = self._run_due()

		self.assertEqual(logged.call_count, 1, logged.call_args_list)
		self.assertIn("could not switch off", logged.call_args.kwargs["title"])
		self._assert_names_no_user(logged)
		self.assertEqual(ran, [mine.name], "a failed switch-off aborted the sweep")
		self.assertEqual(len([r for m in three for r in _runs_for(m.name)]), 1)
		self.assertEqual(len(_due_names(three)), 2, "the macros the sweep skipped should still be due")
		self.assertEqual(
			_admin_notes(ADMIN_A, OWNER_OFF), [], "admins were told about a switch-off that failed"
		)

		self._run_due()
		for m in three:
			row = _schedule_state(m.name)
			self.assertEqual(cint(row.schedule_enabled), 0)
			self.assertIsNone(row.next_run_at)
		self.assertEqual(len(_admin_notes(ADMIN_A, OWNER_OFF)), 1)

	def test_a_failed_run_that_cannot_be_written_leaves_the_schedule_on(self):
		# The failed run is part of the unit. If it cannot be written the schedules stay
		# on, and the sweep falls back to consuming the slot and tries again next time.
		m = _mk_macro(OWNER_OFF, "leave-run-fails")
		with (
			patch.object(macro_scheduler, "_insert_failed_run", side_effect=RuntimeError("no run table")),
			patch.object(frappe, "log_error"),
		):
			self._run_due()

		row = _schedule_state(m.name)
		self.assertEqual(
			cint(row.schedule_enabled), 1, "the schedule went off with no failed run to show for it"
		)
		self.assertGreater(get_datetime(row.next_run_at), now_datetime(), "the slot was left due")
		self.assertEqual(_comments_on(m.name), [])
		self.assertEqual(_admin_notes(ADMIN_A, OWNER_OFF), [])

	def test_the_fallback_writes_nothing_onto_a_schedule_another_sweep_switched_off(self):
		# The switch-off failed because this sweep lost a wait on another one that was
		# switching the same owner off. That one recorded the run and told the admins.
		m = _mk_macro(OWNER_OFF, "leave-lost-the-wait")

		def switched_off_elsewhere(row, now):
			frappe.db.set_value(
				MACRO, row.name, {"schedule_enabled": 0, "next_run_at": None}, update_modified=False
			)
			frappe.db.commit()
			raise RuntimeError("lock wait timeout exceeded")

		with (
			patch.object(macro_scheduler, "_switch_off_and_record", side_effect=switched_off_elsewhere),
			patch.object(frappe, "log_error"),
		):
			self._run_due()

		row = _schedule_state(m.name)
		self.assertEqual(cint(row.schedule_enabled), 0)
		self.assertIsNone(row.next_run_at, "a next run was written onto a schedule that is off")
		self.assertIsNone(row.last_run_at)
		self.assertEqual(_runs_for(m.name), [], "a second failed run for the same switch-off")


# --------------------------------------------------------------------------- #
# admin-v2#675 — say at SAVE that this owner's schedule will never run, instead of
# accepting it and refusing every slot later
# --------------------------------------------------------------------------- #
class TestScheduleIsRefusedAtSaveForABarredOwner(MacroSchedulerBase):
	"""The scheduler has always refused Administrator (#469). The form accepted the
	schedule anyway and showed a "Next run", so the macro looked scheduled and simply
	never ran."""

	def _create(self, tag: str, **kw):
		return macros_api.create_macro(
			macro_name=f"{PFX}-{tag}", steps=[{"prompt": "p1"}], schedule_time="09:00", **kw
		)

	def test_administrator_cannot_put_a_new_macro_on_a_schedule(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._create("admin-new", schedule_enabled=1)
		self.assertIn("Administrator", str(ctx.exception))
		self.assertFalse(frappe.db.exists(MACRO, {"macro_name": f"{PFX}-admin-new"}))

	def test_administrator_cannot_switch_a_schedule_on(self):
		name = self._create("admin-later", schedule_enabled=0)["data"]["name"]
		with self.assertRaises(frappe.ValidationError):
			macros_api.update_macro(name, schedule_enabled=1)
		self.assertFalse(frappe.db.get_value(MACRO, name, "schedule_enabled"))

	def test_administrator_can_still_save_and_edit_an_unscheduled_macro(self):
		name = self._create("admin-manual", schedule_enabled=0)["data"]["name"]
		macros_api.update_macro(name, description="still editable")
		self.assertEqual(frappe.db.get_value(MACRO, name, "description"), "still editable")

	def test_a_schedule_that_is_already_on_can_be_switched_off(self):
		# A row scheduled before this gate existed must not be trapped: every save
		# would be refused until the schedule is off, so switching it off has to work.
		m = _mk_macro("Administrator", "admin-legacy")
		macros_api.update_macro(m.name, schedule_enabled=0)
		self.assertFalse(frappe.db.get_value(MACRO, m.name, "schedule_enabled"))

	def test_a_named_user_can_still_schedule(self):
		frappe.set_user(OWNER_OK)
		try:
			name = self._create("named", schedule_enabled=1)["data"]["name"]
		finally:
			frappe.set_user("Administrator")
		self.assertTrue(frappe.db.get_value(MACRO, name, "next_run_at"))

	def test_the_gate_follows_the_macros_owner_not_whoever_is_saving(self):
		# The scheduler runs a macro as its OWNER. Administrator saving a named user's
		# scheduled macro is saving a schedule that will run, so it must be accepted.
		m = _mk_macro(OWNER_OK, "named-owner")
		macros_api.update_macro(m.name, description="edited by Administrator", schedule_enabled=1)
		self.assertEqual(frappe.db.get_value(MACRO, m.name, "description"), "edited by Administrator")

	def test_an_already_scheduled_macro_says_how_to_get_unstuck(self):
		# Every save of such a macro is refused, whatever was edited. The refusal has to
		# name the way out, or the owner cannot tell why a rename failed.
		m = _mk_macro("Administrator", "admin-legacy-edit")
		with self.assertRaises(frappe.ValidationError) as ctx:
			macros_api.update_macro(m.name, description="just a rename")
		self.assertIn("switch the schedule off", str(ctx.exception))

	def test_the_form_is_told_why_from_the_macros_owner(self):
		# The form used to decide this from the logged-in user, which disagrees with
		# the server whenever someone opens a macro they do not own.
		blocked = _mk_macro("Administrator", "reason-admin")
		fine = _mk_macro(OWNER_OK, "reason-named")
		self.assertIn("Administrator", macros_api.get_macro(blocked.name)["schedule_blocked_reason"])
		self.assertEqual(macros_api.get_macro(fine.name)["schedule_blocked_reason"], "")

	def test_a_disabled_owner_is_not_told_to_sign_in(self):
		reason = macros_api._schedule_block_reason(OWNER_OFF)
		self.assertIn("disabled", reason)
		self.assertNotIn("Sign in", reason)

	def test_an_owner_without_jarvis_access_is_blocked_too(self):
		# The scheduler refuses on EITHER check; the save gate used to apply only one,
		# so this owner's schedule was accepted and then skipped on every slot.
		with patch("jarvis.permissions.has_jarvis_access", return_value=False):
			reason = macros_api._schedule_block_reason(OWNER_OK)
		self.assertIn("access", reason)
		self.assertEqual(macros_api._schedule_block_reason(OWNER_OK), "")


# --------------------------------------------------------------------------- #
# #471 — failures are durable, honest, and terminalized
# --------------------------------------------------------------------------- #
class TestScheduledMacroFailures(MacroSchedulerBase):
	def test_dispatch_failure_does_not_consume_the_slot(self):
		# Was "does not advance the schedule" (the slot stayed due for the next HOURLY
		# tick). On the five-minute sweep a slot left due retries twelve times an hour,
		# so the retry is now scheduled about an hour out. The #471 guarantee is the
		# same: a run that never happened is retried, not counted as done.
		m = self._mk_retryable("raises")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		self._assert_retry_scheduled(m)

	def test_dispatch_failure_is_visible_as_failed_not_successful(self):
		m = _mk_macro(OWNER_OK, "raises-visible")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		runs = _runs_for(m.name)
		self.assertEqual([r.status for r in runs], ["failed"], "the failure left no owner-visible trace")
		self.assertEqual(runs[0].trigger, "scheduled")
		self.assertEqual(runs[0].owner, OWNER_OK)
		# The UI reads last_run_at as "last run"; a run that never happened must not
		# stamp it (the "actively misleading" limb of #471).
		self.assertFalse(
			frappe.db.get_value(MACRO, m.name, "last_run_at"),
			"last_run_at was stamped for a run that never executed",
		)

	def test_dispatch_failure_notifies_the_owner(self):
		_mk_macro(OWNER_OK, "raises-notify")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		notes = frappe.get_all(
			"Notification Log",
			filters={"for_user": OWNER_OK, "subject": ["like", f"%{PFX}-raises-notify%"]},
			pluck="name",
		)
		self.assertTrue(notes, "the owner got no signal at all")

	def test_dispatch_raising_after_the_run_row_exists_leaves_no_orphan(self):
		# run_macro COMMITS the conversation and the run row, then dispatches. When the
		# dispatch raises (gateway down, _ensure_session_key throws) no turn exists, so
		# the chaining hook that terminalizes a run never fires. Without the fix that
		# row sat `running` for three hours until the sweep, AND the scheduler recorded
		# a second failed row, showing two failures for one missed slot.
		m = self._mk_retryable("orphan", steps=1)
		with patch("jarvis.chat.api._enqueue_turn", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		runs = _runs_for(m.name)
		self.assertEqual(len(runs), 1, f"expected exactly one run row for one slot, got {runs}")
		self.assertEqual(runs[0].status, "failed", "the run row was left stranded in `running`")
		self.assertIn("dispatch", runs[0].error.lower())
		# It is the row that carries the conversation, not a bare scheduler record.
		self.assertTrue(frappe.db.get_value(RUN, runs[0].name, "conversation"))
		# Transient: the slot is not consumed; it is retried about an hour later.
		self._assert_retry_scheduled(m)

	def test_notify_owner_never_escapes_and_aborts_the_sweep(self):
		# The scheduler's per-macro loop has no outer guard, so an escape from
		# notify_owner would abort the sweep for every REMAINING due macro.
		with patch("jarvis.permissions.is_valid_unattended_owner", side_effect=RuntimeError("db blip")):
			macros.notify_owner(OWNER_OK, subject="x", body="y")  # must not raise

	def test_successful_run_stamps_last_run_at(self):
		m = _mk_macro(OWNER_OK, "success")
		self._run_due()
		self.assertTrue(frappe.db.get_value(MACRO, m.name, "last_run_at"))
		self.assertEqual(_runs_for(m.name), [], "a successful dispatch wrote a spurious failed row")

	def test_disabled_macro_consumes_the_slot_without_claiming_a_run(self):
		m = _mk_macro(OWNER_OK, "switched-off", enabled=0)
		self.assertNotIn(m.name, self._run_due(), "a disabled macro was dispatched")
		nxt = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertGreater(nxt, now_datetime(), "a disabled macro stayed due and re-fires hourly forever")
		self.assertFalse(
			frappe.db.get_value(MACRO, m.name, "last_run_at"),
			"a disabled macro stamped last_run_at, so the UI claims it ran",
		)
		self.assertEqual(_runs_for(m.name), [], "a deliberately disabled macro is not a failure")


# --------------------------------------------------------------------------- #
# #468 — the entitlement gate and the unattended step budget
# --------------------------------------------------------------------------- #
class TestScheduledMacroCaps(MacroSchedulerBase):
	def _run_due_gated(self):
		"""Run the cron with only the TURN dispatch stubbed, so ``run_macro``'s own
		gate really executes. A refused run leaves exactly one ``failed`` row and no
		conversation; a dispatched one leaves a ``running`` row."""
		with patch("jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}):
			macro_scheduler.run_due_macros()

	def _mk_consumed_run(self, macro_name: str, steps: int, *, trigger="scheduled", status="completed"):
		"""A prior run that already spent ``steps`` turns this month."""
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"macro": macro_name,
				"status": status,
				"current_step": steps,
				"total_steps": steps,
				"trigger": trigger,
				"started_at": frappe.utils.now(),
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		frappe.db.set_value(RUN, run.name, "owner", OWNER_OK, update_modified=False)
		frappe.db.commit()
		return run.name

	def test_over_cap_run_is_refused_recorded_and_consumes_the_slot(self):
		m = _mk_macro(OWNER_OK, "over-cap")
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "usage_limit")):
			self._run_due_gated()
		runs = _runs_for(m.name)
		self.assertEqual([r.status for r in runs], ["failed"], "a macro ran straight through the cap")
		self.assertIn("usage limit", runs[0].error)
		self.assertEqual(runs[0].owner, OWNER_OK)
		# usage_limit does not clear inside the hour, so the slot is consumed rather
		# than relogging the same refusal 24 times a day.
		nxt = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertGreater(nxt, now_datetime())

	def test_ungated_run_still_dispatches(self):
		m = _mk_macro(OWNER_OK, "under-cap")
		self._run_due_gated()
		self.assertEqual([r.status for r in _runs_for(m.name)], ["running"], "the gate over-reached")

	def test_suspended_subscription_refuses_the_run(self):
		m = _mk_macro(OWNER_OK, "suspended")
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "subscription_suspended")):
			self._run_due_gated()
		runs = _runs_for(m.name)
		self.assertEqual([r.status for r in runs], ["failed"])
		self.assertIn("subscription", runs[0].error)

	def test_transient_block_keeps_the_slot_for_a_retry(self):
		m = self._mk_retryable("rolling-out")
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "release_update_required")):
			self._run_due_gated()
		self.assertEqual([r.status for r in _runs_for(m.name)], ["failed"])
		self._assert_retry_scheduled(m)

	def test_a_zero_worker_reading_is_not_a_block_reason_any_more(self):
		# The RQ registry can misreport zero workers while they are alive, so chat
		# no longer refuses sends for it; the scheduler must not expect that reason.
		self.assertNotIn("insufficient_workers", macro_scheduler._TRANSIENT_BLOCKS)

	def test_the_gate_creates_nothing_before_refusing(self):
		# A refused run must leave no conversation and no intro message behind —
		# only the scheduler's own failed record.
		m = _mk_macro(OWNER_OK, "no-residue")
		convs_before = frappe.db.count("Jarvis Conversation")
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "usage_limit")):
			self._run_due_gated()
		self.assertEqual(frappe.db.count("Jarvis Conversation"), convs_before)
		self.assertFalse(_runs_for(m.name)[0].get("conversation"))

	def test_step_budget_refuses_once_the_month_is_spent(self):
		m = _mk_macro(OWNER_OK, "budget", steps=3)
		frappe.db.set_single_value("Jarvis Settings", "macro_step_budget_monthly", 40)
		self.addCleanup(frappe.db.set_single_value, "Jarvis Settings", "macro_step_budget_monthly", None)
		spent = self._mk_consumed_run(m.name, 40 - macros._scheduled_steps_this_month(OWNER_OK) - 1)
		self.addCleanup(frappe.delete_doc, RUN, spent, force=True, ignore_permissions=True)
		# One step of headroom left, and this macro wants three.
		self.assertFalse(macros._over_step_budget(OWNER_OK, 1))
		self.assertTrue(macros._over_step_budget(OWNER_OK, 3))
		self._run_due_gated()
		runs = _runs_for(m.name)
		failed = [r for r in runs if r.status == "failed"]
		self.assertTrue(failed, "the unattended step budget did not bind")
		self.assertNotIn("running", [r.status for r in runs])
		self.assertIn("budget", failed[0].error)

	def test_meter_counts_steps_not_runs_and_excludes_refusals(self):
		m = _mk_macro(OWNER_OK, "meter", steps=3)
		base = macros._scheduled_steps_this_month(OWNER_OK)
		spent = self._mk_consumed_run(m.name, 7)
		self.addCleanup(frappe.delete_doc, RUN, spent, force=True, ignore_permissions=True)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER_OK), base + 7)
		# A manual run is not unattended work, so it does not spend the budget.
		manual = self._mk_consumed_run(m.name, 5, trigger="manual")
		self.addCleanup(frappe.delete_doc, RUN, manual, force=True, ignore_permissions=True)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER_OK), base + 7)
		# And a refusal must never make the cap self-perpetuating. It carries
		# current_step=0, so it contributes nothing WITHOUT filtering on status.
		refused = self._mk_consumed_run(m.name, 0, status="failed")
		self.addCleanup(frappe.delete_doc, RUN, refused, force=True, ignore_permissions=True)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER_OK), base + 7)

	def test_partly_failed_run_still_counts_the_steps_it_billed(self):
		# A run that failed HALFWAY really dispatched and billed its completed steps
		# (stop_on_error, the capacity-attempt cap, and the stale sweep all produce
		# exactly that). Excluding failed rows wholesale would erase those turns, so an
		# owner whose macros keep failing could spend past the budget indefinitely.
		m = _mk_macro(OWNER_OK, "part-failed", due=False, steps=3)
		base = macros._scheduled_steps_this_month(OWNER_OK)
		partial = self._mk_consumed_run(m.name, 2, status="failed")
		self.addCleanup(frappe.delete_doc, RUN, partial, force=True, ignore_permissions=True)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER_OK), base + 2)

	def test_stopped_run_still_counts_the_steps_it_billed(self):
		m = _mk_macro(OWNER_OK, "stopped-run", due=False, steps=3)
		base = macros._scheduled_steps_this_month(OWNER_OK)
		stopped = self._mk_consumed_run(m.name, 2, status="stopped")
		self.addCleanup(frappe.delete_doc, RUN, stopped, force=True, ignore_permissions=True)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER_OK), base + 2)

	def test_budget_floor_clamps_up_and_never_widens(self):
		self.addCleanup(frappe.db.set_single_value, "Jarvis Settings", "macro_step_budget_monthly", None)
		# Unset/blank means "no opinion" and takes the default.
		frappe.db.set_single_value("Jarvis Settings", "macro_step_budget_monthly", 0)
		self.assertEqual(
			macros._scheduled_step_budget_monthly(), macros.DEFAULT_SCHEDULED_STEP_BUDGET_MONTHLY
		)
		# A deliberately tight cap is clamped UP to the floor, never widened to the
		# default: an admin who configures 10 must not silently get 500.
		frappe.db.set_single_value("Jarvis Settings", "macro_step_budget_monthly", 10)
		self.assertEqual(macros._scheduled_step_budget_monthly(), macros.MIN_SCHEDULED_STEP_BUDGET_MONTHLY)
		# A value above the floor is honoured exactly.
		frappe.db.set_single_value("Jarvis Settings", "macro_step_budget_monthly", 77)
		self.assertEqual(macros._scheduled_step_budget_monthly(), 77)

	def test_manual_run_is_entitlement_gated_but_not_budget_gated(self):
		m = _mk_macro(OWNER_OK, "manual-gate", due=False)
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "usage_limit")):
			with self.assertRaises(frappe.ValidationError):
				macros.run_macro(m.name, trigger="manual")
		# The budget is scheduled-only: an attended click is never refused by it.
		with patch.object(macros, "_over_step_budget", return_value=True):
			self.assertIsNone(macros.entitlement_block(OWNER_OK, steps=3, trigger="manual"))
			self.assertEqual(
				macros.entitlement_block(OWNER_OK, steps=3, trigger="scheduled"),
				macros.BLOCK_STEP_BUDGET,
			)


# --------------------------------------------------------------------------- #
# admin-v2#675 — the sweep runs every five minutes so a macro starts near the time
# its owner set, while a slot that FAILED is still retried only about hourly
# --------------------------------------------------------------------------- #
SWEEP = "jarvis.chat.macro_scheduler.run_due_macros"


class TestScheduledMacroCadence(MacroSchedulerBase):
	def _dispatches(self, mock_run, m) -> int:
		return [c.args[0] for c in mock_run.call_args_list].count(m.name)

	def _an_hour_passes(self, m) -> None:
		frappe.db.set_value(
			MACRO, m.name, "next_run_at", add_to_date(now_datetime(), minutes=-1), update_modified=False
		)
		frappe.db.commit()

	def _notifications(self, tag: str) -> list:
		return frappe.get_all(
			"Notification Log",
			filters={"for_user": OWNER_OK, "subject": ["like", f"%{PFX}-{tag}%"]},
			pluck="name",
		)

	def test_the_sweep_is_registered_every_five_minutes_not_hourly(self):
		from jarvis import hooks

		self.assertIn(SWEEP, hooks.scheduler_events["cron"]["*/5 * * * *"])
		self.assertNotIn(
			SWEEP,
			hooks.scheduler_events["hourly"],
			"an hourly sweep starts a 10:15 macro at 11:00, which reads as 'never triggered'",
		)

	# --- a failed slot is retried about hourly, not on every sweep ------------- #

	def test_a_failed_slot_is_not_retried_by_the_next_sweep(self):
		m = _mk_macro(OWNER_OK, "retry")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")) as mock_run:
			macro_scheduler.run_due_macros()
			macro_scheduler.run_due_macros()
		self.assertEqual(self._dispatches(mock_run, m), 1, "a failing macro was retried every sweep")
		self.assertEqual(len(_runs_for(m.name)), 1, "one missed slot left more than one failed run")
		self.assertEqual(len(self._notifications("retry")), 1, "the next sweep notified the owner again")

	def test_the_wait_before_a_retry_survives_a_cache_clear(self):
		# pump.watchdog runs on the same five-minute cron and ends each cycle with a
		# full frappe.clear_cache() (see persistent_cache_keys in hooks.py). A retry
		# time held in the cache was gone before the next sweep ever read it.
		m = _mk_macro(OWNER_OK, "retry-cache")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")) as mock_run:
			macro_scheduler.run_due_macros()
			frappe.clear_cache()
			macro_scheduler.run_due_macros()
		self.assertEqual(self._dispatches(mock_run, m), 1, "a cache clear made the next sweep retry")

	def test_a_failed_slot_is_retried_once_its_wait_is_over(self):
		m = _mk_macro(OWNER_OK, "retry-later")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")) as mock_run:
			macro_scheduler.run_due_macros()
			self._an_hour_passes(m)
			macro_scheduler.run_due_macros()
		self.assertEqual(self._dispatches(mock_run, m), 2, "a failed slot was never retried")

	def test_a_transient_block_is_not_relogged_by_the_next_sweep(self):
		m = _mk_macro(OWNER_OK, "retry-block")
		with (
			patch("jarvis.chat.policy.validate_can_send", return_value=(False, "maintenance")),
			patch("jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}),
		):
			macro_scheduler.run_due_macros()
			macro_scheduler.run_due_macros()
		self.assertEqual([r.status for r in _runs_for(m.name)], ["failed"])

	def test_a_retry_is_never_later_than_the_next_natural_slot(self):
		# A slot that is due again in twenty minutes anyway must not be pushed out to
		# an hour: the retry would then run instead of the next occurrence, not before it.
		m = _mk_macro(OWNER_OK, "retry-soon")
		soon = add_to_date(now_datetime(), minutes=20)
		frappe.db.set_value(MACRO, m.name, "schedule_time", soon.strftime("%H:%M:00"), update_modified=False)
		frappe.db.commit()
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		nxt = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertLessEqual(nxt, add_to_date(now_datetime(), minutes=21))

	def test_a_failure_before_the_slot_is_claimed_is_recorded_and_retried_later(self):
		# Anything that raises ahead of the claim (here the identity guard) used to
		# leave only an Error Log the owner cannot read, and a slot due on every sweep.
		m = self._mk_retryable("retry-early")
		with (
			patch("jarvis.chat.macros.run_macro", return_value={"ok": True}) as mock_run,
			patch("jarvis.permissions.has_jarvis_access", side_effect=RuntimeError("db blip")),
		):
			macro_scheduler.run_due_macros()
			macro_scheduler.run_due_macros()
		self.assertEqual(self._dispatches(mock_run, m), 0)
		self.assertEqual([r.status for r in _runs_for(m.name)], ["failed"], "the owner saw nothing")
		self._assert_retry_scheduled(m)

	# --- the slot is claimed before the macro is dispatched --------------------- #

	def test_the_slot_is_claimed_before_the_macro_is_dispatched(self):
		# A worker killed after the dispatch must not leave the slot due, or the next
		# sweep runs the macro a second time (duplicate writes when it is armed).
		m = _mk_macro(OWNER_OK, "claim-first")
		seen = {}

		def dispatch(name, **kw):
			if name == m.name:
				seen["next_run_at"] = get_datetime(frappe.db.get_value(MACRO, name, "next_run_at"))
			return {"ok": True}

		with patch("jarvis.chat.macros.run_macro", side_effect=dispatch):
			macro_scheduler.run_due_macros()
		self.assertGreater(
			seen["next_run_at"], now_datetime(), "the slot was still due while the macro was dispatching"
		)

	def test_a_slot_another_dispatcher_already_took_is_not_run_again(self):
		m = _mk_macro(OWNER_OK, "claimed-elsewhere")
		stale = frappe.get_all(MACRO, filters={"name": m.name}, fields=["*"])[0]
		with patch("jarvis.chat.macros.run_macro", return_value={"ok": True}) as mock_run:
			macro_scheduler._sweep_one(stale, now_datetime(), set())
			macro_scheduler._sweep_one(stale, now_datetime(), set())
		self.assertEqual(self._dispatches(mock_run, m), 1, "a stale read of a taken slot ran it twice")

	def test_a_run_whose_bookkeeping_blew_up_is_neither_rerun_nor_reported_failed(self):
		# The dispatch SUCCEEDED; only the bookkeeping after it raised. The macro is
		# running, so the slot must not be retried and no failed run may be recorded.
		m = _mk_macro(OWNER_OK, "claim-boom")
		with (
			patch("jarvis.chat.macros.run_macro", return_value={"ok": True}) as mock_run,
			patch.object(macro_scheduler, "_settle", side_effect=RuntimeError("bookkeeping blew up")),
		):
			macro_scheduler.run_due_macros()
			macro_scheduler.run_due_macros()
		self.assertEqual(self._dispatches(mock_run, m), 1)
		self.assertEqual(_runs_for(m.name), [])

	def test_a_late_bookkeeping_failure_is_not_reported_when_the_next_slot_is_near(self):
		# The case the row count exists for. With the next natural slot under the retry
		# wait away, the retry time IS that slot, which the claim already wrote. Judging
		# "did the retry land" by re-reading the row then said yes although the
		# compare-and-set matched nothing, and a dispatched macro was reported failed.
		m = _mk_macro(OWNER_OK, "claim-boom-near")
		soon = add_to_date(now_datetime(), minutes=20)
		frappe.db.set_value(MACRO, m.name, "schedule_time", soon.strftime("%H:%M:00"), update_modified=False)
		frappe.db.commit()
		with (
			patch("jarvis.chat.macros.run_macro", return_value={"ok": True}),
			patch.object(macro_scheduler, "_settle", side_effect=RuntimeError("bookkeeping blew up")),
		):
			macro_scheduler.run_due_macros()
		self.assertEqual(_runs_for(m.name), [], "a macro that WAS dispatched was reported as failed")

	def test_retry_later_only_claims_a_write_that_landed(self):
		m = self._mk_retryable("retry-cas")
		row = frappe.get_all(MACRO, filters={"name": m.name}, fields=["*"])[0]
		now = now_datetime()
		elsewhere = add_to_date(now, days=2).replace(microsecond=0)
		self.assertFalse(macro_scheduler._retry_later(row, now, expected=elsewhere))
		self.assertEqual(
			get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at")), get_datetime(row.next_run_at)
		)
		self.assertTrue(macro_scheduler._retry_later(row, now, expected=row.next_run_at))

	# --- the owner is told the truth about the retry --------------------------- #

	def test_the_failure_says_it_will_retry_when_the_retry_was_scheduled(self):
		m = self._mk_retryable("retry-says")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		self.assertIn("retry within the hour", _runs_for(m.name)[0].error)

	def test_the_notification_says_it_will_retry_too(self):
		# The notification is the only thing an owner sees at 03:00; the retry clause
		# must not live on the failed-run row alone.
		m = self._mk_retryable("retry-notified")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		bodies = frappe.get_all(
			"Notification Log",
			filters={"for_user": OWNER_OK, "subject": ["like", f"%{m.macro_name}%"]},
			pluck="email_content",
		)
		self.assertEqual(len(bodies), 1, bodies)
		self.assertIn("retry within the hour", bodies[0])

	def test_the_failure_does_not_promise_a_retry_that_was_not_scheduled(self):
		m = self._mk_retryable("retry-not-promised")
		with (
			patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")),
			patch.object(macro_scheduler, "_retry_later", return_value=False),
		):
			macro_scheduler.run_due_macros()
		error = _runs_for(m.name)[0].error
		self.assertNotIn("retry", error)
		self.assertIn("next scheduled time", error)

	def test_a_pending_retry_is_reported_as_a_retry_not_as_the_schedule(self):
		from jarvis.chat import macros_api

		m = self._mk_retryable("retry-flag")
		frappe.set_user(OWNER_OK)
		try:
			self.assertEqual(
				macros_api.get_macro(m.name)["next_run_is_retry"], 0, "a due slot is not a retry"
			)
		finally:
			frappe.set_user("Administrator")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		frappe.set_user(OWNER_OK)
		try:
			self.assertEqual(macros_api.get_macro(m.name)["next_run_is_retry"], 1)
		finally:
			frappe.set_user("Administrator")

	def test_an_ordinary_upcoming_slot_is_not_reported_as_a_retry(self):
		m = _mk_macro(OWNER_OK, "retry-flag-normal")
		self._run_due()  # dispatched fine: next_run_at is now the schedule's own next slot
		row = frappe.get_all(MACRO, filters={"name": m.name}, fields=["*"])[0]
		self.assertFalse(macro_scheduler.is_retry_pending(row))

	def test_a_weekly_or_monthly_macro_saved_without_a_day_is_not_reported_as_a_retry(self):
		# Rows scheduled before the day picker (#653) carry no weekday / day of month.
		# For them "the next occurrence from now" is seven days (or a month) from TODAY
		# once today's time has passed, so it overtakes the stored slot every afternoon.
		# Judged against that, a healthy macro read "The last scheduled run failed".
		after_todays_time = datetime.datetime(2026, 10, 7, 11, 0)  # a Wednesday
		for frequency, slot in (
			("weekly", datetime.datetime(2026, 10, 12, 10, 15)),
			("monthly", datetime.datetime(2026, 11, 5, 10, 15)),
			# Less than the retry wait away, and still the schedule's own slot.
			("weekly", datetime.datetime(2026, 10, 7, 11, 30)),
		):
			row = frappe._dict(
				schedule_enabled=1,
				schedule_frequency=frequency,
				schedule_time=slot.strftime("%H:%M:00"),
				schedule_weekday=None,
				schedule_day_of_month=None,
				next_run_at=slot,
			)
			self.assertFalse(macro_scheduler.is_retry_pending(row, after_todays_time), f"{frequency} {slot}")

	def test_only_a_time_within_the_retry_wait_and_off_the_schedule_minute_is_a_retry(self):
		now = datetime.datetime(2026, 10, 7, 11, 0, 4, 250)
		row = frappe._dict(schedule_enabled=1, schedule_frequency="daily", schedule_time="09:00:00")
		wait = macro_scheduler._RETRY_AFTER_S
		row.next_run_at = add_to_date(now, seconds=wait)
		self.assertTrue(macro_scheduler.is_retry_pending(row, now))
		# The commonest retry: the 09:00 run fails and is retried at 09:55, inside the
		# schedule's own HOUR. Only the minute tells it from the slot.
		at_the_slot = datetime.datetime(2026, 10, 7, 9, 0, 4, 250)
		row.next_run_at = add_to_date(at_the_slot, seconds=wait)
		self.assertTrue(macro_scheduler.is_retry_pending(row, at_the_slot))
		row.next_run_at = add_to_date(now, seconds=wait)
		# Further out than any retry can be: a hand-set or seeded time, not a failure.
		row.next_run_at = add_to_date(now, seconds=wait + 1)
		self.assertFalse(macro_scheduler.is_retry_pending(row, now))
		row.next_run_at = add_to_date(now, seconds=-1)
		self.assertFalse(macro_scheduler.is_retry_pending(row, now), "a due slot is not a retry")
		row.schedule_enabled = 0
		row.next_run_at = add_to_date(now, seconds=wait)
		self.assertFalse(macro_scheduler.is_retry_pending(row, now), "a switched-off schedule")

	def test_the_retry_wait_fits_inside_the_hour_the_owner_is_told(self):
		# The failed-run text says "within the hour", and until a site migrates the retry
		# is picked up by the old HOURLY job: an hour or more would wait for two ticks.
		# Twelve retries an hour is what the wait exists to prevent.
		self.assertLess(macro_scheduler._RETRY_AFTER_S, 60 * 60)
		self.assertGreaterEqual(macro_scheduler._RETRY_AFTER_S, 30 * 60)

	def test_the_macros_list_reports_a_pending_retry_too(self):
		from jarvis.chat import macros_api

		m = self._mk_retryable("retry-flag-list")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		frappe.set_user(OWNER_OK)
		try:
			rows = macros_api.list_macros_page(page_length=100)["rows"]
		finally:
			frappe.set_user("Administrator")
		flags = {r["name"]: r["next_run_is_retry"] for r in rows}
		self.assertEqual(flags[m.name], 1)

	# --- the sweep never overwrites a schedule the owner saved meanwhile -------- #

	def _owner_saves_during_dispatch(self, m, outcome):
		saved = add_to_date(now_datetime(), days=3).replace(microsecond=0)

		def dispatch(name, **kw):
			if name == m.name:
				frappe.db.set_value(MACRO, name, "next_run_at", saved, update_modified=False)
				frappe.db.commit()
				if isinstance(outcome, Exception):
					raise outcome
			return outcome if isinstance(outcome, dict) else {"ok": True}

		with patch("jarvis.chat.macros.run_macro", side_effect=dispatch):
			macro_scheduler.run_due_macros()
		return saved

	def test_a_retry_does_not_overwrite_a_schedule_saved_during_the_dispatch(self):
		m = _mk_macro(OWNER_OK, "save-race-fail")
		saved = self._owner_saves_during_dispatch(m, RuntimeError("gateway down"))
		self.assertEqual(get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at")), saved)

	def test_a_successful_run_does_not_overwrite_a_schedule_saved_during_the_dispatch(self):
		m = _mk_macro(OWNER_OK, "save-race-ok")
		saved = self._owner_saves_during_dispatch(m, {"ok": True})
		self.assertEqual(get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at")), saved)
		self.assertTrue(frappe.db.get_value(MACRO, m.name, "last_run_at"))


# --------------------------------------------------------------------------- #
# #471 — the stale-run reaper, and what it must NOT reap
# --------------------------------------------------------------------------- #
class TestStaleMacroRunReaper(MacroSchedulerBase):
	def _mk_run(self, tag: str, status: str, *, age_s: int):
		m = _mk_macro(OWNER_OK, tag, due=False)
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"macro": m.name,
				"status": status,
				"current_step": 0,
				"total_steps": 1,
				"trigger": "scheduled",
				"started_at": frappe.utils.now(),
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		stamp = add_to_date(now_datetime(), seconds=-age_s)
		frappe.db.sql(
			"UPDATE `tabJarvis Macro Run` SET modified=%(t)s, owner=%(o)s WHERE name=%(n)s",
			{"t": stamp, "o": OWNER_OK, "n": run.name},
		)
		frappe.db.commit()
		return run.name

	def test_stranded_running_run_is_terminalized(self):
		name = self._mk_run("stranded", "running", age_s=macros.STALE_RUN_AFTER_SECONDS + 600)
		self.assertEqual(macros.reap_stale_macro_runs(), 1)
		row = frappe.db.get_value(RUN, name, ["status", "error", "finished_at"], as_dict=True)
		self.assertEqual(row.status, "failed")
		self.assertTrue(row.error, "a reaped run must say WHY, not carry an empty error")
		self.assertTrue(row.finished_at)

	def test_parked_waiting_capacity_run_is_never_reaped(self):
		# #470 lets a run sit in waiting_capacity for ~100 min legitimately; the
		# reaper must not confuse deliberately parked with stranded. Aged far past
		# the cutoff so only the STATUS allowlist can be saving it.
		name = self._mk_run("parked", "waiting_capacity", age_s=macros.STALE_RUN_AFTER_SECONDS * 4)
		self.assertEqual(macros.reap_stale_macro_runs(), 0)
		self.assertEqual(frappe.db.get_value(RUN, name, "status"), "waiting_capacity")

	def test_recently_progressing_run_is_never_reaped(self):
		name = self._mk_run("progressing", "running", age_s=macros.STALE_RUN_AFTER_SECONDS // 2)
		self.assertEqual(macros.reap_stale_macro_runs(), 0)
		self.assertEqual(frappe.db.get_value(RUN, name, "status"), "running")

	def test_run_that_advanced_after_the_scan_is_left_alone(self):
		# The candidate snapshot is inherently stale: a step can advance between the
		# scan and the transition. The re-read under the lock must catch that.
		name = self._mk_run("raced", "running", age_s=macros.STALE_RUN_AFTER_SECONDS + 600)
		with patch.object(macros, "_stale_run_candidates", return_value=[name]):
			frappe.db.sql(
				"UPDATE `tabJarvis Macro Run` SET modified=%(t)s WHERE name=%(n)s",
				{"t": now_datetime(), "n": name},
			)
			frappe.db.commit()
			self.assertEqual(macros.reap_stale_macro_runs(), 0)
		self.assertEqual(frappe.db.get_value(RUN, name, "status"), "running")


# --------------------------------------------------------------------------- #
# admin-v2#675 — saving a macro must not move a slot that is already due, and a
# schedule must read back as the time that was saved
# --------------------------------------------------------------------------- #
class TestScheduleSurvivesASave(MacroSchedulerBase):
	"""The schedule form posts every field on every save. For a daily macro that
	includes day-of-month ``0`` and weekday ``""``, which the API stores as ``None``
	while the row holds ``0`` / ``""``, so the controller read every save as a
	schedule change and recomputed ``next_run_at`` from now: a run that was due but
	not swept yet moved to tomorrow, with nothing recorded."""

	def _due_daily_macro(self, tag: str):
		m = _mk_macro(OWNER_OK, tag)
		frappe.db.set_value(MACRO, m.name, "schedule_time", "10:15:00", update_modified=False)
		frappe.db.commit()
		return m

	def _save_from_the_form(self, m, **changes):
		payload = {
			"macro_name": m.macro_name,
			"schedule_enabled": 1,
			"schedule_frequency": "daily",
			"schedule_time": "10:15",
			"schedule_weekday": "",
			"schedule_day_of_month": 0,
			**changes,
		}
		frappe.set_user(OWNER_OK)
		try:
			macros_api.update_macro(m.name, **payload)
		finally:
			frappe.set_user("Administrator")

	def _next_run(self, m):
		return get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))

	def test_a_save_that_changes_no_schedule_field_keeps_the_due_slot(self):
		m = self._due_daily_macro("resave")
		before = self._next_run(m)
		self._save_from_the_form(m, description="only the description changed")
		self.assertEqual(self._next_run(m), before, "re-saving a macro dropped the run that was due")

	def test_changing_the_time_still_reschedules(self):
		m = self._due_daily_macro("retime")
		self._save_from_the_form(m, schedule_time="11:30")
		after = self._next_run(m)
		self.assertEqual((after.hour, after.minute), (11, 30))
		self.assertGreater(after, now_datetime())

	# The two anchor tests change ONLY the anchor. Changing the frequency as well would
	# reschedule through `schedule_frequency` and prove nothing about the anchor compare.

	def test_changing_only_the_day_of_month_still_reschedules(self):
		m = self._due_daily_macro("reanchor-day")
		frappe.db.set_value(
			MACRO,
			m.name,
			{"schedule_frequency": "monthly", "schedule_day_of_month": 10},
			update_modified=False,
		)
		frappe.db.commit()
		self._save_from_the_form(m, schedule_frequency="monthly", schedule_day_of_month=15)
		after = self._next_run(m)
		self.assertEqual(after.day, 15)
		self.assertGreater(after, now_datetime())

	def test_changing_only_the_weekday_still_reschedules(self):
		m = self._due_daily_macro("reanchor-weekday")
		frappe.db.set_value(
			MACRO,
			m.name,
			{"schedule_frequency": "weekly", "schedule_weekday": "Monday"},
			update_modified=False,
		)
		frappe.db.commit()
		self._save_from_the_form(m, schedule_frequency="weekly", schedule_weekday="Thursday")
		after = self._next_run(m)
		self.assertEqual(after.strftime("%A"), "Thursday")
		self.assertGreater(after, now_datetime())

	def test_an_unchanged_anchor_keeps_the_due_slot(self):
		m = self._due_daily_macro("same-anchor")
		frappe.db.set_value(
			MACRO,
			m.name,
			{"schedule_frequency": "weekly", "schedule_weekday": "Monday"},
			update_modified=False,
		)
		frappe.db.commit()
		before = self._next_run(m)
		self._save_from_the_form(m, schedule_frequency="weekly", schedule_weekday="Monday")
		self.assertEqual(self._next_run(m), before)

	def test_a_row_with_no_stored_time_keeps_its_due_slot_when_the_form_sends_the_default(self):
		# A row written without a time runs at the 09:00 default. The form always posts
		# a time, "09:00" for such a row. That is the same schedule, not a change.
		m = _mk_macro(OWNER_OK, "no-time")
		# Emptied by hand: Frappe 15 stamps every Time field of a new document with the
		# current time, so there the fixture's row is NOT time-less on its own, and a
		# form posting "09:00" over it is a real change that rightly reschedules.
		frappe.db.set_value(MACRO, m.name, "schedule_time", None, update_modified=False)
		frappe.db.commit()
		before = self._next_run(m)
		self._save_from_the_form(m, schedule_time="09:00")
		self.assertEqual(self._next_run(m), before, "the first save of a time-less macro dropped its due run")

	def test_a_midnight_schedule_reads_back_as_midnight(self):
		# timedelta(0) is falsy, so `schedule_time or ""` returned "" for 00:00 and the
		# form fell back to its 09:00 default, which the next save then stored.
		m = _mk_macro(OWNER_OK, "midnight", due=False)
		frappe.db.set_value(MACRO, m.name, "schedule_time", "00:00:00", update_modified=False)
		frappe.db.commit()
		frappe.set_user(OWNER_OK)
		try:
			shown = macros_api.get_macro(m.name)["schedule_time"]
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(macro_scheduler.parse_schedule_seconds(shown), 0, f"midnight read back as {shown!r}")


# --------------------------------------------------------------------------- #
# #472 — schedule_time is validated, and one bad row cannot abort the sweep
# --------------------------------------------------------------------------- #
class TestScheduleTimeValidation(MacroSchedulerBase):
	"""A ``schedule_time`` Frappe never range-checks reached
	``datetime.replace(hour=...)`` inside ``validate()``, so the save 500'd, and a
	value already on a row aborted the WHOLE hourly sweep."""

	def _save(self, tag: str, schedule_time):
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-{tag}",
				"enabled": 1,
				"schedule_enabled": 1,
				"schedule_frequency": "daily",
				"schedule_time": schedule_time,
				"steps": [{"prompt": "p1"}],
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc

	def test_out_of_range_schedule_time_is_refused_cleanly(self):
		# frappe.ValidationError is what the SPA renders as a field error; a bare
		# ValueError from the arithmetic is the 500 this closes.
		for bad in ("99:00:00", "-01:00:00", "24:00:00", "12:99:00", "not-a-time", "1:2:3:4"):
			with self.subTest(bad=bad):
				with self.assertRaises(frappe.ValidationError):
					self._save(f"bad-{abs(hash(bad))}", bad)

	def test_a_bad_time_is_refused_even_with_the_schedule_off(self):
		# The latent limb: with schedule_enabled=0 the controller never computed a
		# next run, so the garbage persisted and armed the sweep for whenever the
		# schedule was turned on.
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-off-but-bad",
				"enabled": 1,
				"schedule_enabled": 0,
				"schedule_time": "99:00:00",
				"steps": [{"prompt": "p1"}],
			}
		)
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_valid_schedule_times_are_accepted_and_scheduled(self):
		for good in ("00:00:00", "09:30:00", "23:59:59"):
			with self.subTest(good=good):
				doc = self._save(f"ok-{good.replace(':', '')}", good)
				self.assertTrue(doc.next_run_at, "a valid time must still produce a next_run_at")

	def test_bad_persisted_time_does_not_abort_the_sweep(self):
		# Raw-write past the new validation to recreate a row saved before it existed,
		# then force it to the FRONT of the sweep (Jarvis Macro sorts modified DESC) so
		# a sweep that dies on it cannot reach the healthy macro behind it.
		bad = _mk_macro(OWNER_OK, "poisoned")
		good = _mk_macro(OWNER_OK, "healthy")
		frappe.db.sql(
			"UPDATE `tabJarvis Macro` SET schedule_time='99:00:00', modified=%(t)s WHERE name=%(n)s",
			{"t": add_to_date(now_datetime(), days=1), "n": bad.name},
		)
		frappe.db.commit()

		dispatched = self._run_due()

		self.assertIn(good.name, dispatched, "one bad row aborted the sweep for every other macro")
		self.assertIn(bad.name, dispatched, "the bad row should fall back, not be skipped")
		# The poisoned row's slot still advanced, on the 09:00 fallback an unset time gets.
		nxt = get_datetime(frappe.db.get_value(MACRO, bad.name, "next_run_at"))
		self.assertEqual((nxt.hour, nxt.minute), (9, 0))

	def test_one_exploding_macro_does_not_abort_the_sweep(self):
		# The structural guarantee, independent of schedule_time: whatever a single
		# row raises anywhere in its handling, every OTHER due macro still runs.
		first = _mk_macro(OWNER_OK, "explodes")
		second = _mk_macro(OWNER_OK, "survivor")
		frappe.db.sql(
			"UPDATE `tabJarvis Macro` SET modified=%(t)s WHERE name=%(n)s",
			{"t": add_to_date(now_datetime(), days=1), "n": first.name},
		)
		frappe.db.commit()

		def boom(m, *a, **kw):
			if m.name == first.name:
				raise RuntimeError("bookkeeping blew up")

		with (
			patch("jarvis.chat.macros.run_macro", return_value={"ok": True}) as mock_run,
			patch.object(macro_scheduler, "_settle", side_effect=boom),
		):
			macro_scheduler.run_due_macros()
		self.assertIn(second.name, [c.args[0] for c in mock_run.call_args_list])

	def test_compute_next_run_never_raises_on_a_stored_value(self):
		for bad in ("99:00:00", "-01:00:00", "838:59:59", "garbage"):
			with self.subTest(bad=bad):
				nxt = macro_scheduler.compute_next_run("daily", bad)
				self.assertEqual((nxt.hour, nxt.minute), (9, 0))


class TestScheduleDayOfMonthValidation(MacroSchedulerBase):
	"""The #653 twin of ``TestScheduleTimeValidation`` above: ``schedule_day_of_month``
	is an Int field Frappe never range-checks on its own, so a bad value must be
	refused the same way an out-of-range ``schedule_time`` is."""

	def _save(self, tag: str, day_of_month, frequency: str = "monthly"):
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-{tag}",
				"enabled": 1,
				"schedule_enabled": 1,
				"schedule_frequency": frequency,
				"schedule_day_of_month": day_of_month,
				"steps": [{"prompt": "p1"}],
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc

	def test_out_of_range_day_of_month_is_refused_cleanly(self):
		# 0 is deliberately NOT here: Frappe coerces a blank Int field to 0 (not
		# None), so the validator exempts it as "unset", not "the 0th day" - see
		# test_unset_day_of_month_is_exempt below.
		for bad in (32, -1, 100):
			with self.subTest(bad=bad):
				with self.assertRaises(frappe.ValidationError):
					self._save(f"bad-{bad}", bad)

	def test_valid_days_of_month_are_accepted_and_scheduled(self):
		for good in (1, 15, 31):
			with self.subTest(good=good):
				doc = self._save(f"ok-{good}", good)
				self.assertTrue(doc.next_run_at, "a valid day must still produce a next_run_at")

	def test_unset_day_of_month_is_exempt(self):
		# An Int field left blank reaches validate() as 0, not None - "not set",
		# never "the 0th day" (#653's exemption, mirroring the empty-string exemption
		# schedule_time gets). Both the None a caller might pass and the literal 0
		# Frappe coerces it to must be accepted, not refused.
		for unset in (None, 0):
			with self.subTest(unset=unset):
				doc = self._save(f"unset-{unset}", unset)
				self.assertTrue(doc.next_run_at)

	def test_out_of_range_weekday_select_is_refused_by_the_framework(self):
		# schedule_weekday is a Select field - Frappe's own _validate_selects()
		# refuses a value outside its options list, so no bespoke throw is needed
		# here (unlike the Int field above).
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{PFX}-bad-weekday",
				"enabled": 1,
				"schedule_enabled": 1,
				"schedule_frequency": "weekly",
				"schedule_weekday": "Blursday",
				"steps": [{"prompt": "p1"}],
			}
		)
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			doc.insert()


class TestComputeNextRunAnchors(FrappeTestCase):
	"""Pure arithmetic, no DB writes needed - the anchors compute_next_run gained
	for #653 (weekly weekday, monthly day-of-month), and the TOTAL guarantee
	(#472) extended to cover them: a missing, None, or garbage anchor value must
	fall back to the plain +7-days / +1-month advance and never raise."""

	def test_weekly_anchor_skips_to_next_occurrence_when_todays_has_passed(self):
		# Monday 10:00, target Monday 09:00 -> already passed today, so next Monday.
		nxt = macro_scheduler.compute_next_run(
			"weekly", "09:00:00", from_dt="2026-09-07 10:00:00", weekday="Monday"
		)
		self.assertEqual(nxt.strftime("%A"), "Monday")
		self.assertEqual(nxt.date().isoformat(), "2026-09-14")

	def test_weekly_anchor_same_day_when_the_time_has_not_passed_yet(self):
		nxt = macro_scheduler.compute_next_run(
			"weekly", "09:00:00", from_dt="2026-09-07 08:00:00", weekday="Monday"
		)
		self.assertEqual(nxt.date().isoformat(), "2026-09-07")

	def test_weekly_anchor_picks_a_different_weekday(self):
		nxt = macro_scheduler.compute_next_run(
			"weekly", "09:00:00", from_dt="2026-09-07 08:00:00", weekday="Friday"
		)
		self.assertEqual(nxt.strftime("%A"), "Friday")
		self.assertEqual(nxt.date().isoformat(), "2026-09-11")

	def test_monthly_anchor_clamps_day_31_in_february_then_returns_to_31_in_march(self):
		# 2026 is not a leap year: Feb has 28 days.
		feb = macro_scheduler.compute_next_run(
			"monthly", "09:00:00", from_dt="2026-01-31 09:00:01", day_of_month=31
		)
		self.assertEqual(feb.date().isoformat(), "2026-02-28")

		mar = macro_scheduler.compute_next_run("monthly", "09:00:00", from_dt=str(feb), day_of_month=31)
		self.assertEqual(mar.date().isoformat(), "2026-03-31")

	def test_monthly_anchor_clamps_to_29_in_a_leap_february(self):
		nxt = macro_scheduler.compute_next_run(
			"monthly", "09:00:00", from_dt="2028-01-31 09:00:01", day_of_month=31
		)
		self.assertEqual(nxt.date().isoformat(), "2028-02-29")

	def test_no_anchor_falls_back_to_the_plain_advance(self):
		# Same shape every pre-#653 caller still uses: weekday/day_of_month omitted.
		weekly = macro_scheduler.compute_next_run("weekly", "09:00:00", from_dt="2026-09-07 10:00:00")
		self.assertEqual(weekly.date().isoformat(), "2026-09-14")
		monthly = macro_scheduler.compute_next_run("monthly", "09:00:00", from_dt="2026-01-31 10:00:00")
		self.assertEqual(monthly.date().isoformat(), "2026-02-28")

	def test_garbage_anchors_never_raise_and_fall_back_to_the_plain_advance(self):
		for weekday in ("Blursday", 0, 8, "", [], {}):
			with self.subTest(weekday=weekday):
				nxt = macro_scheduler.compute_next_run(
					"weekly", "09:00:00", from_dt="2026-09-07 10:00:00", weekday=weekday
				)
				self.assertEqual(nxt.date().isoformat(), "2026-09-14")
		for day in (0, 32, -1, "abc", None, [], {}):
			with self.subTest(day=day):
				nxt = macro_scheduler.compute_next_run(
					"monthly", "09:00:00", from_dt="2026-01-31 10:00:00", day_of_month=day
				)
				self.assertEqual(nxt.date().isoformat(), "2026-02-28")

	def test_a_garbage_schedule_time_still_never_raises_alongside_a_valid_anchor(self):
		# #472's TOTAL guarantee and #653's anchors compose: a bad time AND a good
		# anchor together still fall back cleanly (09:00 default time).
		nxt = macro_scheduler.compute_next_run(
			"weekly", "not-a-time", from_dt="2026-09-07 10:00:00", weekday="Monday"
		)
		self.assertEqual((nxt.hour, nxt.minute), (9, 0))


class TestDefaultScheduleAnchors(FrappeTestCase):
	"""``agents_api.install_agent`` resolves a listing's ``default_schedule`` JSON
	into the two anchors a fresh installation is born with. A first version of
	this reimplemented weekday normalization as a bespoke string-only check
	instead of reusing ``macro_scheduler._normalize_weekday`` (the reader every
	OTHER weekday input - the SPA, ``set_schedule``, both DocType controllers -
	goes through), so a listing authored with the ISO-int form (e.g. 3 for
	Wednesday) silently landed with no weekday at all. These call the exact
	functions ``install_agent`` calls, no DB fixture needed."""

	def test_int_weekday_in_default_schedule_resolves_to_its_name(self):
		from jarvis.chat.agents_api import _default_schedule_weekday

		# ISO weekday: 1=Monday .. 7=Sunday.
		cases = {1: "Monday", 3: "Wednesday", 7: "Sunday"}
		for iso, name in cases.items():
			with self.subTest(iso=iso):
				self.assertEqual(_default_schedule_weekday({"schedule_weekday": iso}), name)

	def test_weekday_name_in_default_schedule_still_works(self):
		from jarvis.chat.agents_api import _default_schedule_weekday

		self.assertEqual(_default_schedule_weekday({"schedule_weekday": "Friday"}), "Friday")
		self.assertEqual(_default_schedule_weekday({"schedule_weekday": "friday"}), "Friday")

	def test_garbage_weekday_in_default_schedule_resolves_to_none(self):
		from jarvis.chat.agents_api import _default_schedule_weekday

		for bad in ("Blursday", 0, 8, "", None, [], {}):
			with self.subTest(bad=bad):
				self.assertIsNone(_default_schedule_weekday({"schedule_weekday": bad}))

	def test_day_of_month_in_default_schedule(self):
		from jarvis.chat.agents_api import _default_schedule_day_of_month

		self.assertEqual(_default_schedule_day_of_month({"schedule_day_of_month": 15}), 15)
		for bad in (0, 32, -1, "abc", None):
			with self.subTest(bad=bad):
				self.assertIsNone(_default_schedule_day_of_month({"schedule_day_of_month": bad}))

	def test_install_agent_applies_an_int_weekday_default_schedule(self):
		# End-to-end through install_agent itself: a listing whose default_schedule
		# carries an ISO-int weekday must land on the installation AS ITS NAME, not
		# be silently dropped.
		from jarvis.chat import agents_api

		listing_name = frappe.db.get_value("Jarvis Agent Listing", {"agent_slug": "close-auditor"}, "name")
		if not listing_name:
			self.skipTest("close-auditor listing not present on this site")
		owner = _ensure_user("msched-int-weekday@example.com", enabled=1)
		# close-auditor declares doctypes_required (GL Entry / Account / Company);
		# JarvisAgentInstallation._validate_run_as_user refuses to install unless
		# the run-as user already holds read on those - the same A12 gate
		# test_agents_marketplace.py and test_platform_agents_api_hardening.py
		# satisfy for their own fixtures. Accounts User grants them. This is a
		# real-access grant, not a bypass of the check itself.
		if frappe.db.exists("Role", "Accounts User"):
			if "Accounts User" not in set(frappe.get_roles(owner)):
				frappe.get_doc("User", owner).add_roles("Accounts User")
		original_schedule = frappe.db.get_value("Jarvis Agent Listing", listing_name, "default_schedule")
		frappe.db.set_value(
			"Jarvis Agent Listing",
			listing_name,
			"default_schedule",
			frappe.as_json({"schedule_enabled": 0, "schedule_frequency": "weekly", "schedule_weekday": 3}),
			update_modified=False,
		)
		frappe.db.commit()
		# On this line a listing with allowed_roles rows refuses installs from
		# users outside those roles (empty = open). close-auditor may ship seeded
		# roles, so grant Jarvis User for the duration of the test, or
		# install_agent refuses with a PermissionError before ever reaching the
		# schedule handling this test is about. _ensure_user already grants the
		# Jarvis User role. Only the row added here is removed afterwards.
		allowed_role_filters = {
			"parenttype": "Jarvis Agent Listing",
			"parentfield": "allowed_roles",
			"parent": listing_name,
			"role": "Jarvis User",
		}
		added_allowed_role = None
		if not frappe.db.exists("Jarvis Agent Allowed Role", allowed_role_filters):
			added_allowed_role = frappe.get_doc(
				{"doctype": "Jarvis Agent Allowed Role", **allowed_role_filters}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		original_user = frappe.session.user
		try:
			frappe.set_user(owner)
			frappe.db.delete("Jarvis Agent Installation", {"owner": owner, "agent": listing_name})
			frappe.db.commit()
			agents_api.install_agent("close-auditor")
			weekday = frappe.db.get_value(
				"Jarvis Agent Installation", {"owner": owner, "agent": listing_name}, "schedule_weekday"
			)
			self.assertEqual(weekday, "Wednesday")
		finally:
			frappe.set_user(original_user)
			if added_allowed_role is not None:
				frappe.db.delete("Jarvis Agent Allowed Role", {"name": added_allowed_role.name})
			frappe.db.delete("Jarvis Agent Installation", {"owner": owner, "agent": listing_name})
			frappe.db.set_value(
				"Jarvis Agent Listing",
				listing_name,
				"default_schedule",
				original_schedule,
				update_modified=False,
			)
			frappe.db.commit()
