"""A Jarvis Admin's hold, release and delete of another user's macro
(``jarvis.chat.macros_admin_api.admin_hold`` / ``admin_release`` / ``admin_delete``).

What is pinned here:

* each verb is for a Jarvis Admin only, by POST only, never from inside a tool call
  and never through ``run_method``, and refuses a macro name that is not text before
  it writes anything; the hold verbs are refused on the admin's own macro;
* the owner cannot set or clear the hold: not by a form save, REST ``set_value``, a
  Data Import or a Duplicate, and the fields' permlevel alone already resets an
  owner's write on this framework;
* a held macro does not run: ``run_macro`` refuses it for every trigger, the
  scheduler consumes its slot with no failed run, the capacity resume and the five
  minute check end its run, a run in flight is stopped by the hold, and the owner
  cannot switch it on, schedule it or arm it;
* a hold and a run start take turns on the macro's row lock, in both orders;
* a release clears the hold and nothing else;
* a delete is the owner's own (runs stopped first, the month's rows kept) and leaves
  a notice the owner's Macros list shows until it is dismissed;
* before the migrate (the fields missing from the doctype) the sweep, the lists,
  ``get_macro`` and ``run_macro`` work and nothing is held.

The tests that need the two columns skip on a site that has not migrated them; the
ones that are about their absence run everywhere (see ``TestBeforeTheMigrate``).
"""

from __future__ import annotations

import threading
from collections import defaultdict
from unittest.mock import patch

import frappe
from frappe.handler import is_valid_http_method
from frappe.utils import add_to_date, now_datetime, set_request

from jarvis.chat import macro_reconcile, macro_scheduler, macros, macros_admin_api, macros_api
from jarvis.exceptions import PermissionDeniedError
from jarvis.jarvis.doctype.jarvis_macro import jarvis_macro as controller
from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import MacroOnHoldError
from jarvis.tests import test_macro_admin_api as e1
from jarvis.tests.race_harness import (
	assert_fix_closes_race,
	open_read_view,
	other_connection,
	snapshot_isolation_on,
)
from jarvis.tools.run_method import _is_denied, run_method

CONV = e1.CONV
MACRO = e1.MACRO
RUN = e1.RUN
OWNER = e1.OWNER
OTHER = e1.OTHER
ADMIN = e1.ADMIN
PLAIN = e1.PLAIN
TAG = e1.TAG
PATH = e1.PATH
VERBS = ("admin_hold", "admin_release", "admin_delete")
HELD = macros._MACRO_HELD_ERROR
REASON = "Sends too many emails"
NOTICE = macros_api.ADMIN_DELETE_NOTICE
_MISSING = object()


def _columns() -> bool:
	return controller.hold_fields_exist()


class HoldBase(e1.AdminBase):
	def _purge(self):
		super()._purge()
		frappe.db.delete("Deleted Document", {"deleted_doctype": MACRO, "owner": ["in", (ADMIN, OWNER)]})
		frappe.db.commit()

	def _hold(self, macro, reason=REASON, *, who=ADMIN):
		frappe.set_user(who)
		return macros_admin_api.admin_hold(macro, reason=reason)

	def _state(self, macro):
		fields = ["enabled", "schedule_enabled", "skip_confirmation", "next_run_at"]
		if _columns():
			fields += ["admin_hold", "admin_hold_reason"]
		return frappe.db.get_value(MACRO, macro, fields, as_dict=True)

	def _held_raw(self, macro, reason=REASON, **fields):
		"""The hold as the verb writes it, without its stop of live runs: what a run
		finds when the hold landed and its own stop did not reach it."""
		frappe.db.set_value(
			MACRO, macro, {"admin_hold": 1, "admin_hold_reason": reason, "enabled": 0, **fields}
		)
		frappe.db.commit()

	def _scheduled_macro(self, tag, **fields):
		due = add_to_date(now_datetime(), minutes=-5)
		return self._macro(OWNER, tag, schedule_enabled=1, next_run_at=due, **fields)


class WithColumns(HoldBase):
	def setUp(self):
		if not _columns():
			self.skipTest("the site has not migrated the hold fields (bench migrate)")
		super().setUp()


# --------------------------------------------------------------------------- #
# Who may call them, and how
# --------------------------------------------------------------------------- #
class TestTheGates(WithColumns):
	def test_a_jarvis_admin_holds_releases_and_deletes_another_users_macro(self):
		macro = self._macro(OWNER, "a")
		self.assertEqual(self._hold(macro), {"ok": True, "held": True, "stopped_runs": 0})
		self.assertEqual(self._state(macro).admin_hold, 1)
		self.assertEqual(macros_admin_api.admin_release(macro), {"ok": True, "released": True})
		self.assertEqual(self._state(macro).admin_hold, 0)
		self.assertEqual(
			macros_admin_api.admin_delete(macro), {"ok": True, "deleted": True, "stopped_runs": 0}
		)
		self.assertFalse(frappe.db.exists(MACRO, macro))

	def test_anyone_but_an_admin_is_refused(self):
		macro = self._macro(OWNER, "a")
		self._hold(macro)
		for who in (PLAIN, OWNER):
			for verb, kwargs in (
				("admin_hold", {"macro": macro, "reason": "mine now"}),
				("admin_release", {"macro": macro}),
				("admin_delete", {"macro": macro}),
			):
				with self.subTest(who=who, verb=verb):
					frappe.set_user(who)
					with self.assertRaises(frappe.PermissionError):
						getattr(macros_admin_api, verb)(**kwargs)
		state = self._state(macro)
		self.assertEqual((state.admin_hold, state.admin_hold_reason), (1, REASON))

	def test_every_verb_is_post_only(self):
		prev = getattr(frappe.local, "request", _MISSING)
		self.addCleanup(e1.TestTheGates._restore_request, prev)
		for verb in VERBS:
			with self.subTest(verb=verb):
				fn = getattr(macros_admin_api, verb)
				self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])
				set_request(method="GET", path=f"/api/method/{PATH}{verb}")
				with self.assertRaises(frappe.PermissionError):
					is_valid_http_method(fn)

	def test_a_name_that_is_not_text_is_refused_before_anything_is_written(self):
		# A dict reaches the database layer as a filter and would match some other
		# row: `{"owner": ...}` would hold or delete whichever macro it found.
		macro = self._macro(OWNER, "a")
		frappe.set_user(ADMIN)
		for verb in VERBS:
			for bad in (None, "", "  ", ["x"], {"owner": OWNER}, 7):
				with self.subTest(verb=verb, value=bad):
					kwargs = {"macro": bad, **({"reason": REASON} if verb == "admin_hold" else {})}
					with (
						patch.object(frappe.db, "set_value") as set_value,
						patch.object(macros, "_lock_macro_row") as lock,
						patch.object(macros_api, "delete_loaded_macro") as delete,
					):
						with self.assertRaises(frappe.ValidationError):
							getattr(macros_admin_api, verb)(**kwargs)
					for untouched in (set_value, lock, delete):
						untouched.assert_not_called()
		self.assertEqual(self._state(macro).admin_hold, 0)
		self.assertTrue(frappe.db.exists(MACRO, macro))

	def test_refused_inside_a_tool_call_and_through_run_method(self):
		macro = self._macro(OWNER, "a")
		frappe.set_user(ADMIN)
		gates = e1.TestTheGates()
		for verb in VERBS:
			kwargs = {"macro": macro, **({"reason": REASON} if verb == "admin_hold" else {})}
			with self.subTest(verb=verb):
				fn = getattr(macros_admin_api, verb)
				err = gates._raised_in_a_tool(fn, **kwargs)
				self.assertIsInstance(err, frappe.PermissionError)
				self.assertTrue(_is_denied(fn))
				with self.assertRaises(PermissionDeniedError):
					run_method(PATH + verb, kwargs)
		self.assertEqual(self._state(macro).admin_hold, 0)
		self.assertTrue(frappe.db.exists(MACRO, macro))

	def test_the_hold_verbs_are_refused_on_the_admins_own_macro(self):
		# An admin switches their own macro off on its form. A hold they could lift
		# themselves would hold nothing another admin set.
		own = self._macro(ADMIN, "own")
		with self.assertRaises(frappe.ValidationError) as raised:
			self._hold(own)
		self.assertIn("your own macro", str(raised.exception))
		self.assertEqual(self._state(own).admin_hold, 0)
		self._held_raw(own)  # another admin's hold
		frappe.set_user(ADMIN)
		with self.assertRaises(frappe.ValidationError):
			macros_admin_api.admin_release(own)
		self.assertEqual(self._state(own).admin_hold, 1)

	def test_a_hold_needs_a_reason_the_owner_can_read(self):
		macro = self._macro(OWNER, "a")
		for bad in ("", "   ", None, ["x"], "x" * (macros_admin_api.HOLD_REASON_MAX + 1)):
			with self.subTest(reason=bad):
				with self.assertRaises(frappe.ValidationError):
					self._hold(macro, reason=bad)
		self.assertEqual(self._state(macro).admin_hold, 0)


# --------------------------------------------------------------------------- #
# The hold
# --------------------------------------------------------------------------- #
class TestTheHold(WithColumns):
	def test_it_switches_the_macro_off_unschedules_and_disarms_it(self):
		macro = self._scheduled_macro("a", skip_confirmation=1)
		self._hold(macro)
		state = self._state(macro)
		self.assertEqual(
			(state.admin_hold, state.admin_hold_reason, state.enabled, state.schedule_enabled),
			(1, REASON, 0, 0),
		)
		self.assertEqual((state.skip_confirmation, state.next_run_at), (0, None))

	def test_it_works_on_a_macro_already_off_and_again_on_one_already_held(self):
		macro = self._macro(OWNER, "a", enabled=0)
		self._hold(macro)
		self.assertEqual(self._hold(macro, reason="A second look")["held"], True)
		self.assertEqual(self._state(macro).admin_hold_reason, "A second look")

	def test_it_leaves_a_comment_naming_the_admin_and_the_reason(self):
		macro = self._macro(OWNER, "a")
		self._hold(macro)
		frappe.set_user(ADMIN)
		macros_admin_api.admin_release(macro)
		texts = [c.content for c in self._comments(macro)]
		self.assertEqual(len(texts), 2)
		self.assertTrue(any(ADMIN in t and "on hold" in t and REASON in t for t in texts), texts)
		self.assertTrue(any(ADMIN in t and "released" in t for t in texts), texts)
		self.assertEqual({c.comment_email for c in self._comments(macro)}, {ADMIN})

	def test_a_run_in_flight_is_stopped(self):
		run, conv, macro = self._mk_run(steps=3, at_step=1, armed=True, tag=f"{TAG}-flight")
		queued, _, _ = self._step(conv, "queued")
		res = self._hold(macro)
		self.assertEqual(res["stopped_runs"], 1)
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("stopped", HELD))
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)
		self.assertEqual(self._turn_state(queued), "cancelled")
		(closing,) = self._closing(conv, run)
		self.assertIn(HELD, closing.content)

	def test_a_parked_run_is_stopped_too(self):
		run, _conv, macro = self._mk_run(steps=2, at_step=0, tag=f"{TAG}-parked")
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		frappe.db.commit()
		self.assertEqual(self._hold(macro)["stopped_runs"], 1)
		self.assertEqual(self._run(run).status, "stopped")

	def test_the_admin_list_says_a_held_runs_stop_was_an_admins(self):
		run, _conv, macro = self._mk_run(tag=f"{TAG}-flag")
		self._hold(macro)
		(row,) = [r for r in self._list()["rows"] if r["name"] == macro]
		self.assertEqual((row["admin_hold"], row["admin_hold_reason"]), (1, REASON))
		self.assertEqual(row["last_run"]["stopped_by_admin"], 1)
		self.assertEqual(self._run(run).error, HELD)

	def test_one_run_that_will_not_stop_leaves_the_others_stopped(self):
		# Every live run is tried; the one that failed is named in the error, which
		# comes after the rest were stopped.
		first, _c1, macro = self._mk_run(steps=3, at_step=1, armed=True, tag=f"{TAG}-two")
		frappe.set_user(OWNER)
		conv = frappe.get_doc({"doctype": CONV, "title": "second run"})
		conv.flags.ignore_permissions = True
		conv.insert()
		second = self._run_row(macro, OWNER, "running", conversation=conv.name)
		real_stop = macros._stop_run
		calls = []

		def stop(run, **kwargs):
			calls.append(run)
			if run == first:
				frappe.throw("This run could not be stopped. Try again.")
			return real_stop(run, **kwargs)

		frappe.clear_messages()
		with patch.object(macros, "_stop_run", side_effect=stop):
			with self.assertRaises(frappe.ValidationError) as raised:
				self._hold(macro)
		self.assertEqual(sorted(calls), sorted([first, second]))
		self.assertIn(first, str(raised.exception))
		# The admin is shown the hold's own message, not the failed stop's: a client
		# shows the first message of the request.
		shown = [frappe.parse_json(m).get("message", "") for m in frappe.local.message_log]
		self.assertEqual(len(shown), 1, shown)
		self.assertIn("could not be stopped (", shown[0])
		self.assertEqual(self._run(second).status, "stopped")
		self.assertEqual(self._run(first).status, "running")
		self.assertEqual(self._state(macro).admin_hold, 1)  # the hold itself stands

	def test_the_admin_list_shows_a_reason_only_on_a_held_row(self):
		macro = self._macro(OWNER, "a", admin_hold=0, admin_hold_reason="left behind")
		(row,) = [r for r in self._list()["rows"] if r["name"] == macro]
		self.assertEqual((row["admin_hold"], row["admin_hold_reason"]), (0, ""))

	def test_the_admin_list_filters_on_hold(self):
		held = self._macro(OWNER, "held")
		free = self._macro(OTHER, "free")
		self._hold(held)
		self.assertEqual(self._names(self._list(filters={"on_hold": 1})), [held])
		self.assertEqual(self._names(self._list(filters={"on_hold": 0})), [free])
		frappe.set_user(ADMIN)
		opened = macros_admin_api.admin_get_macro(held)
		self.assertEqual((opened["admin_hold"], opened["admin_hold_reason"]), (1, REASON))


class TestAHeldMacroDoesNotRun(WithColumns):
	def test_run_macro_refuses_it_by_hand_and_on_a_schedule(self):
		macro = self._macro(OWNER, "a")
		self._hold(macro)
		frappe.set_user(OWNER)
		with patch("jarvis.chat.api._enqueue_turn") as enqueue:
			with self.assertRaises(MacroOnHoldError) as raised:
				macros.run_macro(macro)
			self.assertIn(REASON, str(raised.exception))
			self.assertIn("will not run until an admin releases it", str(raised.exception))
			frappe.db.rollback()
			with self.assertRaises(MacroOnHoldError):
				macros_api.run_macro(macro)
			frappe.db.rollback()
			self.assertEqual(
				macros.run_macro(macro, trigger="scheduled"),
				{"ok": False, "reason": macros.BLOCK_MACRO_HELD},
			)
		enqueue.assert_not_called()
		self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)
		self.assertEqual(frappe.db.count(CONV, {"owner": OWNER}), 0)

	def test_it_is_refused_even_when_still_switched_on(self):
		# The hold alone is the refusal, not the `enabled` it also switches off.
		macro = self._macro(OWNER, "a")
		self._held_raw(macro, enabled=1)
		frappe.set_user(OWNER)
		with patch("jarvis.chat.api._enqueue_turn") as enqueue:
			with self.assertRaises(MacroOnHoldError):
				macros.run_macro(macro)
			frappe.db.rollback()
			self.assertEqual(macros.run_macro(macro, trigger="scheduled")["reason"], macros.BLOCK_MACRO_HELD)
		enqueue.assert_not_called()

	def test_the_scheduler_consumes_its_slot_with_no_failed_run(self):
		# The hold switches the schedule off; a sweep that read its due list before
		# the hold landed still holds the macro, scheduled.
		macro = self._scheduled_macro("a")
		self._held_raw(macro, enabled=1)
		frappe.set_user("Administrator")
		with (
			patch("jarvis.chat.api._enqueue_turn") as enqueue,
			patch.object(macros, "run_macro", wraps=macros.run_macro) as run,
			patch.object(macro_scheduler, "_record_failed") as record,
			patch.object(macro_scheduler, "_notify_owner") as notify,
		):
			macro_scheduler.run_due_macros()
		# Consumed before any dispatch, through the switched-off path.
		run.assert_not_called()
		enqueue.assert_not_called()
		record.assert_not_called()
		notify.assert_not_called()
		self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)
		next_run = frappe.db.get_value(MACRO, macro, ["next_run_at", "last_run_at"], as_dict=True)
		self.assertGreater(next_run.next_run_at, now_datetime())  # the slot moved on
		self.assertIsNone(next_run.last_run_at)  # and is not counted as run

	def test_a_hold_that_lands_after_the_due_list_is_quiet_too(self):
		# The sweep's row says "not held" and the dispatch finds it held: run_macro's
		# refusal is settled the way a disabled macro's is.
		macro = self._scheduled_macro("a")
		row = frappe.db.get_value(
			MACRO,
			macro,
			[
				"name",
				"macro_name",
				"owner",
				"enabled",
				"schedule_frequency",
				"schedule_time",
				"schedule_weekday",
				"schedule_day_of_month",
				"next_run_at",
			],
			as_dict=True,
		)
		self._held_raw(macro, enabled=1, schedule_enabled=1)
		frappe.set_user("Administrator")
		with (
			patch("jarvis.chat.api._enqueue_turn") as enqueue,
			patch.object(macro_scheduler, "_record_failed") as record,
			patch.object(macro_scheduler, "_notify_owner") as notify,
			patch.object(macro_scheduler, "_retry_later") as retry,
		):
			macro_scheduler._sweep_one(row, now_datetime(), set())
		for untouched in (enqueue, record, notify, retry):
			untouched.assert_not_called()
		self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)

	def test_the_capacity_resume_ends_a_held_macros_parked_run(self):
		run, conv, macro = self._mk_run(steps=2, at_step=0, armed=True, tag=f"{TAG}-resume")
		frappe.db.set_value(RUN, run, "status", "waiting_capacity")
		self._held_raw(macro)
		frappe.set_user("Administrator")
		with patch.object(macros, "_dispatch_step") as dispatch:
			macros.resume_waiting_capacity_runs()
		dispatch.assert_not_called()
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("stopped", HELD))
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)

	def test_a_step_end_ends_a_held_macros_run_and_sends_nothing(self):
		# The hold committed and its own stop of this run failed: the run's next turn
		# end stops it, with the reason the resume and the check use.
		run, conv, macro = self._mk_run(steps=3, at_step=1, armed=True, tag=f"{TAG}-hook")
		step_turn, _, _ = self._turn(conv)
		with patch.object(macros, "_stop_run", side_effect=frappe.ValidationError("lost")):
			with self.assertRaises(frappe.ValidationError):
				self._hold(macro)
		self.assertEqual(self._run(run).status, "running")
		frappe.set_user("Administrator")
		with patch("jarvis.chat.api._enqueue_turn") as enqueue:
			macros.advance_after_turn(conv, errored=False, run_id=step_turn)
		enqueue.assert_not_called()
		row = self._run(run)
		self.assertEqual((row.status, row.error, row.current_step), ("stopped", HELD, 1))
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)

	def test_a_held_macro_is_not_summarized(self):
		macro = self._macro(OWNER, "a", steps=("one", "two"))
		self._hold(macro)
		frappe.set_user(OWNER)
		with patch("jarvis.chat.api._enqueue_turn") as enqueue:
			for force in (0, 1):
				with self.subTest(force=force):
					with self.assertRaises(MacroOnHoldError) as raised:
						macros_api.summarize_macro(macro, force=force)
					self.assertIn(REASON, str(raised.exception))
					frappe.db.rollback()
		enqueue.assert_not_called()
		self.assertEqual(frappe.db.get_value(MACRO, macro, "merge_status") or "", "")
		self.assertEqual(frappe.db.count(CONV, {"owner": OWNER}), 0)

	def test_a_step_edit_while_held_does_not_ask_for_a_summary(self):
		macro = self._macro(OWNER, "a", steps=("one", "two"))
		self._hold(macro)
		frappe.set_user(OWNER)
		res = macros_api.update_macro(macro, steps=[{"prompt": "one"}, {"prompt": "two, edited"}])
		self.assertFalse(res["data"]["summarize"])
		self.assertEqual(
			frappe.db.get_value("Jarvis Macro Step", {"parent": macro, "idx": 2}, "prompt"), "two, edited"
		)

	def test_the_five_minute_check_ends_a_held_macros_run(self):
		run, conv, macro = self._mk_run(steps=2, at_step=1, armed=True, tag=f"{TAG}-check")
		self._turn(conv)  # step 1 ended; its hook is lost
		old = add_to_date(now_datetime(), minutes=-30)
		frappe.db.set_value(RUN, run, "modified", old, update_modified=False)
		self._held_raw(macro)
		frappe.set_user("Administrator")
		summary = defaultdict(int)
		with (
			patch("jarvis.chat.api._enqueue_turn") as enqueue,
			patch.object(macros, "_apply_step_end") as apply_end,
		):
			macro_reconcile._RunCheck(run, summary).run()
		enqueue.assert_not_called()
		apply_end.assert_not_called()
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("stopped", HELD))
		self.assertEqual(summary["acted"], 1)
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)


class TestTheOwnerCannotUndoIt(WithColumns):
	def _as_owner_doc(self, macro):
		frappe.set_user(OWNER)
		return frappe.get_doc(MACRO, macro)

	def test_the_owner_cannot_switch_it_on_schedule_or_arm_it(self):
		macro = self._macro(OWNER, "a")
		self._hold(macro)
		frappe.set_user(OWNER)
		for change in ({"enabled": 1}, {"schedule_enabled": 1}, {"skip_confirmation": 1}):
			with self.subTest(change=change):
				with self.assertRaises(MacroOnHoldError) as raised:
					macros_api.update_macro(macro, **change)
				self.assertIn(REASON, str(raised.exception))
				frappe.db.rollback()
		state = self._state(macro)
		self.assertEqual((state.enabled, state.schedule_enabled, state.skip_confirmation), (0, 0, 0))
		# Anything else is still theirs to change, and switching things off is free.
		macros_api.update_macro(macro, description="still mine", enabled=0, schedule_enabled=0)
		self.assertEqual(frappe.db.get_value(MACRO, macro, "description"), "still mine")
		self.assertEqual(self._state(macro).admin_hold, 1)

	def test_switching_off_a_held_macro_that_is_still_on_is_never_blocked(self):
		# A row held while still on (a raw write; the verb switches all three off):
		# each switch goes off on its own save, and the hold stays.
		macro = self._macro(OWNER, "a")
		self._held_raw(macro, enabled=1, schedule_enabled=1, skip_confirmation=1)
		frappe.set_user(OWNER)
		for field in ("enabled", "schedule_enabled", "skip_confirmation"):
			with self.subTest(field=field):
				macros_api.update_macro(macro, **{field: 0})
				self.assertEqual(frappe.db.get_value(MACRO, macro, field), 0)
		self.assertEqual(self._state(macro).admin_hold, 1)

	def test_the_reason_is_escaped_in_the_message(self):
		# The refusal is a message, and messages are HTML (Frappe 15's Desk dialog
		# inserts one as it is). What the SPA reads stays the admin's own text.
		reason = "<b>x</b> <img src=x onerror=alert(1)>"
		macro = self._macro(OWNER, "a")
		self._hold(macro, reason=reason)
		frappe.set_user(OWNER)
		with self.assertRaises(MacroOnHoldError) as switched_on:
			macros_api.update_macro(macro, enabled=1)
		frappe.db.rollback()
		with self.assertRaises(MacroOnHoldError) as by_hand:
			macros.run_macro(macro)
		frappe.db.rollback()
		for message in (str(switched_on.exception), str(by_hand.exception)):
			self.assertIn("&lt;b&gt;x&lt;/b&gt; &lt;img src=x onerror=alert(1)&gt;", message)
			self.assertNotIn("<", message)
		self.assertEqual(macros_api.get_macro(macro)["admin_hold_reason"], reason)
		(row,) = [r for r in self._list()["rows"] if r["name"] == macro]
		self.assertEqual(row["admin_hold_reason"], reason)

	def test_a_form_loaded_before_the_hold_cannot_switch_it_back_on(self):
		macro = self._macro(OWNER, "a")
		doc = self._as_owner_doc(macro)  # loaded while it was on
		self._hold(macro)
		frappe.set_user(OWNER)
		doc.reload()
		doc.enabled = 1
		with self.assertRaises(MacroOnHoldError):
			doc.save()
		frappe.db.rollback()
		self.assertEqual(self._state(macro).enabled, 0)

	def test_the_owner_cannot_clear_or_set_the_hold_by_a_form_save(self):
		held = self._macro(OWNER, "held")
		self._hold(held)
		doc = self._as_owner_doc(held)
		doc.admin_hold = 0
		doc.admin_hold_reason = "lifted"
		doc.description = "edited"
		doc.save()
		frappe.db.commit()
		state = self._state(held)
		self.assertEqual((state.admin_hold, state.admin_hold_reason), (1, REASON))
		self.assertEqual(frappe.db.get_value(MACRO, held, "description"), "edited")

		free = self._macro(OWNER, "free")
		doc = self._as_owner_doc(free)
		doc.admin_hold = 1
		doc.admin_hold_reason = "self-imposed"
		doc.save()
		frappe.db.commit()
		state = self._state(free)
		self.assertEqual((state.admin_hold, state.admin_hold_reason or ""), (0, ""))

	def test_nor_by_a_save_that_skips_the_permission_check(self):
		# The fields' permlevel resets a plain user's write, but not Administrator's
		# nor server code saving with permissions ignored. The controller does.
		held = self._macro(OWNER, "held")
		self._hold(held)
		frappe.set_user("Administrator")
		frappe.client.set_value(MACRO, held, {"admin_hold": 0, "admin_hold_reason": ""})
		doc = frappe.get_doc(MACRO, held)
		doc.admin_hold = 0
		doc.flags.ignore_permissions = True
		doc.save()
		frappe.db.commit()
		state = self._state(held)
		self.assertEqual((state.admin_hold, state.admin_hold_reason), (1, REASON))

	def test_nor_by_rest_set_value(self):
		held = self._macro(OWNER, "held")
		self._hold(held)
		frappe.set_user(OWNER)
		frappe.client.set_value(MACRO, held, "admin_hold", 0)
		frappe.client.set_value(MACRO, held, {"admin_hold_reason": "", "description": "x"})
		frappe.db.commit()
		state = self._state(held)
		self.assertEqual((state.admin_hold, state.admin_hold_reason), (1, REASON))
		free = self._macro(OWNER, "free")
		frappe.set_user(OWNER)
		frappe.client.set_value(MACRO, free, "admin_hold", 1)
		frappe.db.commit()
		self.assertEqual(self._state(free).admin_hold, 0)

	def test_nor_by_a_data_import(self):
		# The importer's own update path (``Importer.update_record``), as a Data Import
		# of "Update Existing Records" runs it for each row, with its import flag set.
		from frappe.core.doctype.data_import.importer import Importer

		held = self._macro(OWNER, "held")
		self._hold(held)
		importer = Importer.__new__(Importer)
		importer.doctype = MACRO
		importer.data_import = frappe._dict(doctype="Data Import", name="macro-hold-test")
		# Built without ``__init__``: what ``update_record`` reads from it on newer Frappe 16.
		importer._uses_tree_aliases = False
		importer._tree_parent_field = None
		frappe.set_user(OWNER)
		frappe.flags.in_import = True
		try:
			Importer.update_record(
				importer,
				frappe._dict(
					{"name": held, "admin_hold": 0, "admin_hold_reason": "", "description": "imported"}
				),
			)
		finally:
			frappe.flags.in_import = False
		frappe.db.commit()
		state = self._state(held)
		self.assertEqual((state.admin_hold, state.admin_hold_reason), (1, REASON))
		self.assertEqual(frappe.db.get_value(MACRO, held, "description"), "imported")

	def test_nor_by_a_duplicate_or_an_insert_that_carries_it(self):
		held = self._macro(OWNER, "held")
		self._hold(held)
		frappe.set_user(OWNER)
		copy = frappe.copy_doc(frappe.get_doc(MACRO, held))
		copy.macro_name = f"{TAG}-copy"
		copy.insert()
		crafted = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"{TAG}-crafted",
				"admin_hold": 1,
				"admin_hold_reason": "self-imposed",
				"steps": [{"prompt": "p"}],
			}
		).insert()
		frappe.db.commit()
		for name in (copy.name, crafted.name):
			state = self._state(name)
			self.assertEqual((state.admin_hold, state.admin_hold_reason or ""), (0, ""), name)

	def test_the_reason_is_never_drawn_by_the_desk_form(self):
		# The reason is stored as the admin typed it, and Desk draws a read-only text
		# field as HTML. Only the SPA shows it, as plain text.
		self.assertTrue(frappe.get_meta(MACRO).get_field("admin_hold_reason").hidden)

	def test_the_fields_permlevel_alone_resets_an_owners_write(self):
		# Spec assumption 7 on this framework (Frappe 16): a permlevel 1 field that no
		# role may write is put back on an owner's save, with the controller's own
		# guard switched off. Frappe 15 is first proven in the backport's CI.
		held = self._macro(OWNER, "held")
		self._hold(held)
		with patch.object(controller.JarvisMacro, "_guard_admin_hold", lambda self: None):
			frappe.set_user(OWNER)
			frappe.client.set_value(MACRO, held, {"admin_hold": 0, "admin_hold_reason": ""})
			crafted = frappe.get_doc(
				{
					"doctype": MACRO,
					"macro_name": f"{TAG}-crafted",
					"admin_hold": 1,
					"steps": [{"prompt": "p"}],
				}
			).insert()
		frappe.db.commit()
		self.assertEqual(self._state(held).admin_hold, 1)
		self.assertEqual(self._state(crafted.name).admin_hold, 0)


# --------------------------------------------------------------------------- #
# The hold and a run start take turns on the macro's row
# --------------------------------------------------------------------------- #
class TestHoldAndRunStartTakeTurns(WithColumns):
	def test_a_hold_committed_after_the_runs_snapshot_is_seen(self):
		# The request's read view is from before the hold. Read on it, the run would
		# start for a held macro. The lock's fresh snapshot is what sees the hold.
		def scenario():
			self._purge()
			macro = self._macro(OWNER, "race")
			frappe.set_user(OWNER)
			try:
				with snapshot_isolation_on():
					open_read_view()
					frappe.db.get_value(MACRO, macro, "admin_hold")
					with other_connection() as other:
						other.sql(
							f"UPDATE `tab{MACRO}` SET admin_hold=1, admin_hold_reason=%s WHERE name=%s",
							(REASON, macro),
						)
						other.commit()
					with patch(
						"jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}
					):
						try:
							macros.run_macro(macro)
						except MacroOnHoldError:
							return "refused"
				return "ran"
			finally:
				frappe.db.rollback()

		assert_fix_closes_race(self, scenario, lambda outcome: self.assertEqual(outcome, "refused"))

	def _commit_later(self, connection, seconds=1.0):
		"""Commit ``connection`` from another thread after ``seconds``: the other
		request finishing while this one waits for the row it holds."""
		timer = threading.Timer(seconds, connection._conn.commit)
		timer.start()
		return timer

	def test_a_run_start_waiting_on_a_hold_in_progress_is_refused(self):
		# The hold is mid-write on another connection (row locked, hold written, not
		# committed). The run start waits for the row and then sees the hold.
		macro = self._macro(OWNER, "race")
		frappe.set_user(OWNER)
		with other_connection() as other:
			other.sql(f"SELECT name FROM `tab{MACRO}` WHERE name=%s FOR UPDATE", macro)
			other.sql(
				f"UPDATE `tab{MACRO}` SET admin_hold=1, admin_hold_reason=%s, enabled=0 WHERE name=%s",
				(REASON, macro),
			)
			timer = self._commit_later(other)
			try:
				with patch("jarvis.chat.api._enqueue_turn") as enqueue:
					with self.assertRaises(MacroOnHoldError):
						macros.run_macro(macro)
			finally:
				timer.join()
				frappe.db.rollback()
		enqueue.assert_not_called()
		self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)

	def test_a_hold_waiting_on_a_run_start_in_progress_stops_that_run(self):
		# The run start is mid-insert on another connection (row locked, run row
		# written, not committed). The hold waits for the row, and the run it then
		# finds committed is stopped: no run of a held macro survives.
		macro = self._macro(OWNER, "race")
		frappe.set_user(OWNER)
		conv = frappe.get_doc({"doctype": CONV, "title": "race run"})
		conv.flags.ignore_permissions = True
		conv.insert()
		frappe.db.set_value(CONV, conv.name, "skip_confirmation", 1, update_modified=False)
		frappe.db.commit()
		run = frappe.generate_hash(length=10)
		now = frappe.utils.now()
		with other_connection() as other:
			other.sql(f"SELECT name FROM `tab{MACRO}` WHERE name=%s FOR UPDATE", macro)
			other.sql(
				f"""INSERT INTO `tab{RUN}`
					(name, creation, modified, owner, modified_by, docstatus, macro, conversation,
					 status, current_step, total_steps, run_mode, `trigger`)
				VALUES (%s, %s, %s, %s, %s, 0, %s, %s, 'running', 1, 2, 'stepped', 'manual')""",
				(run, now, now, OWNER, OWNER, macro, conv.name),
			)
			timer = self._commit_later(other)
			try:
				res = self._hold(macro)
			finally:
				timer.join()
		self.assertEqual(res["stopped_runs"], 1)
		self.assertEqual(self._run(run).status, "stopped")
		self.assertEqual(frappe.db.get_value(CONV, conv.name, "skip_confirmation"), 0)


# --------------------------------------------------------------------------- #
# Release
# --------------------------------------------------------------------------- #
class TestRelease(WithColumns):
	def test_a_release_clears_the_hold_and_turns_nothing_back_on(self):
		macro = self._scheduled_macro("a", skip_confirmation=1)
		self._hold(macro)
		frappe.set_user(ADMIN)
		self.assertTrue(macros_admin_api.admin_release(macro)["released"])
		state = self._state(macro)
		self.assertEqual((state.admin_hold, state.admin_hold_reason or ""), (0, ""))
		self.assertEqual(
			(state.enabled, state.schedule_enabled, state.skip_confirmation, state.next_run_at),
			(0, 0, 0, None),
		)
		# The owner turns it back on, and it runs.
		frappe.set_user(OWNER)
		macros_api.update_macro(macro, enabled=1)
		with patch(
			"jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}
		) as enqueue:
			self.assertTrue(macros.run_macro(macro)["ok"])
		enqueue.assert_called_once()

	def test_releasing_a_macro_that_is_not_held_changes_nothing(self):
		macro = self._macro(OWNER, "a")
		modified = frappe.db.get_value(MACRO, macro, "modified")
		frappe.set_user(ADMIN)
		self.assertEqual(macros_admin_api.admin_release(macro), {"ok": True, "released": False})
		self.assertEqual(frappe.db.get_value(MACRO, macro, "modified"), modified)
		self.assertEqual(self._comments(macro), [])


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #
class TestAdminDelete(HoldBase):
	"""Needs no hold column: the delete is the owner's own path."""

	def _notices(self, user=OWNER):
		return frappe.get_all(
			"Notification Log",
			filters={"for_user": user, "subject": ["like", f"{NOTICE}:%"]},
			fields=["name", "subject", "email_content", "document_type", "document_name", "read"],
		)

	def test_it_stops_the_runs_first_and_deletes(self):
		run, conv, macro = self._mk_run(steps=3, at_step=1, armed=True, tag=f"{TAG}-del")
		queued, _, _ = self._step(conv, "queued")
		frappe.set_user(ADMIN)
		self.assertEqual(macros_admin_api.admin_delete(macro)["stopped_runs"], 1)
		self.assertFalse(frappe.db.exists(MACRO, macro))
		self.assertEqual(frappe.db.get_value(CONV, conv, "skip_confirmation"), 0)
		self.assertEqual(self._turn_state(queued), "cancelled")

	def test_it_is_the_owners_own_delete(self):
		macro = self._macro(OWNER, "a")
		frappe.set_user(ADMIN)
		with patch.object(macros_api, "delete_loaded_macro", wraps=macros_api.delete_loaded_macro) as delete:
			macros_admin_api.admin_delete(macro)
		((doc,), kwargs) = delete.call_args
		self.assertEqual((doc.name, kwargs), (macro, {"ignore_permissions": True}))

	def test_it_keeps_the_months_scheduled_rows(self):
		macro = self._macro(OWNER, "a")
		kept = self._run_row(macro, OWNER, "completed", trigger="scheduled", current_step=2)
		dropped = self._run_row(macro, OWNER, "completed", trigger="manual")
		frappe.set_user(ADMIN)
		macros_admin_api.admin_delete(macro)
		self.assertTrue(frappe.db.exists(RUN, kept))
		self.assertIsNone(frappe.db.get_value(RUN, kept, "macro"))
		self.assertFalse(frappe.db.exists(RUN, dropped))

	def test_it_leaves_a_deleted_document_and_a_notice_naming_no_document(self):
		macro = self._macro(OWNER, "a")
		frappe.set_user(ADMIN)
		macros_admin_api.admin_delete(macro)
		self.assertTrue(
			frappe.db.exists(
				"Deleted Document", {"deleted_doctype": MACRO, "deleted_name": macro, "owner": ADMIN}
			)
		)
		(note,) = self._notices()
		self.assertEqual(note.subject, f"{NOTICE}: {TAG}-a")
		self.assertIn(f"{TAG}-a", note.email_content)
		self.assertFalse(note.document_type or note.document_name)

	def test_the_owners_list_shows_the_notice_until_it_is_dismissed(self):
		macro = self._macro(OWNER, "a")
		frappe.set_user(ADMIN)
		macros_admin_api.admin_delete(macro)
		# Another kind of notice is not one of these, and is not dismissed with them.
		macros.notify_owner(OWNER, subject="Macro run failed: x", body="boom")
		frappe.set_user(OWNER)
		(notice,) = macros_api.list_macros_page()["notices"]
		self.assertIn(f"{TAG}-a", notice["message"])
		# Another user's list does not show it, nor can they dismiss it.
		frappe.set_user(OTHER)
		self.assertEqual(macros_api.list_macros_page()["notices"], [])
		self.assertEqual(macros_api.dismiss_macro_notices([notice["name"]])["dismissed"], 0)
		frappe.set_user(OWNER)
		self.assertEqual(macros_api.dismiss_macro_notices([notice["name"]])["dismissed"], 1)
		self.assertEqual(macros_api.list_macros_page()["notices"], [])
		self.assertEqual(frappe.db.get_value("Notification Log", notice["name"], "read"), 1)
		other = frappe.get_all(
			"Notification Log", {"for_user": OWNER, "subject": "Macro run failed: x"}, ["read"]
		)
		self.assertEqual([o.read for o in other], [0])

	def test_dismiss_is_post_only_and_takes_a_list(self):
		fn = macros_api.dismiss_macro_notices
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])
		frappe.set_user(OWNER)
		# Not JSON, not a list, or a list of anything but names: a ValidationError, not a 500.
		for names in ({"owner": OWNER}, "NL-0001", '{"a": 1}', "[1, 2]", [None], ["NL-1", 3]):
			with self.subTest(names=names):
				with self.assertRaises(frappe.ValidationError):
					fn(names)
		self.assertEqual(fn("[]")["dismissed"], 0)

	def test_an_admin_deleting_their_own_macro_is_not_notified(self):
		own = self._macro(ADMIN, "own")
		frappe.set_user(ADMIN)
		macros_admin_api.admin_delete(own)
		self.assertFalse(frappe.db.exists(MACRO, own))
		self.assertEqual(self._notices(ADMIN), [])

	def test_a_macro_already_deleted_is_not_found(self):
		macro = self._macro(OWNER, "a")
		frappe.set_user(ADMIN)
		macros_admin_api.admin_delete(macro)
		with self.assertRaises(frappe.DoesNotExistError):
			macros_admin_api.admin_delete(macro)
		self.assertEqual(len(self._notices()), 1)


# --------------------------------------------------------------------------- #
# Before the migrate: the fields are not in the doctype
# --------------------------------------------------------------------------- #
class TestBeforeTheMigrate(HoldBase):
	"""Runs on every site. Where the columns are really missing (a site between deploy
	and migrate) nothing is patched in effect; where they exist, the one question every
	reader asks (``hold_fields_exist``) is answered "no", which is what such a site
	answers."""

	def setUp(self):
		super().setUp()
		for target in (
			patch.object(controller, "hold_fields_exist", return_value=False),
			patch.object(macros_admin_api, "hold_fields_exist", return_value=False),
		):
			target.start()
			self.addCleanup(target.stop)

	def test_the_sweep_runs_a_due_macro(self):
		macro = self._scheduled_macro("a")
		frappe.set_user("Administrator")
		with patch(
			"jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}
		) as enqueue:
			macro_scheduler.run_due_macros()
		self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 1)
		enqueue.assert_called_once()

	def test_the_lists_and_get_macro_work_and_nothing_is_held(self):
		macro = self._macro(OWNER, "a")
		frappe.set_user(OWNER)
		(row,) = [r for r in macros_api.list_macros_page()["rows"] if r["name"] == macro]
		self.assertEqual((row["admin_hold"], row["admin_hold_reason"]), (0, ""))
		got = macros_api.get_macro(macro)
		self.assertEqual((got["admin_hold"], got["admin_hold_reason"]), (0, ""))
		(row,) = self._list()["rows"]
		self.assertEqual(row["admin_hold"], 0)
		self.assertEqual(self._names(self._list(filters={"on_hold": 1})), [])
		self.assertEqual(self._names(self._list(filters={"on_hold": 0})), [macro])
		self.assertEqual(macros_admin_api.admin_get_macro(macro)["admin_hold"], 0)

	def test_run_macro_and_a_save_work(self):
		macro = self._macro(OWNER, "a")
		frappe.set_user(OWNER)
		macros_api.update_macro(macro, description="saved", enabled=1, schedule_enabled=1)
		with patch("jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}):
			self.assertTrue(macros.run_macro(macro)["ok"])

	def test_the_hold_verbs_say_the_site_needs_its_update(self):
		macro = self._macro(OWNER, "a")
		frappe.set_user(ADMIN)
		for verb, kwargs in (("admin_hold", {"reason": REASON}), ("admin_release", {})):
			with self.subTest(verb=verb):
				with self.assertRaises(frappe.ValidationError) as raised:
					getattr(macros_admin_api, verb)(macro, **kwargs)
				self.assertIn("bench migrate", str(raised.exception))
		self.assertEqual(self._state(macro).enabled, 1)
