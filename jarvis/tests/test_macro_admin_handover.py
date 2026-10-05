"""A Jarvis Admin hands another user's macro to someone else
(``jarvis.chat.macros_admin_api.admin_handover``).

What is pinned here:

* the macro arrives switched off, unscheduled, disarmed and not held, with no
  summary, no skills on its steps, no shares, no assignments and nobody following
  it; the new owner is written as the user's name is stored;
* it is refused, with nothing written, for a target that does not exist, is
  disabled, has no Jarvis access, is Administrator or Guest, is full, already has a
  macro of that name or already owns this one; and on the admin's own macro;
* only a Jarvis Admin, by POST, never from a tool call or ``run_method``, with every
  argument checked to be text before anything is written;
* a live run is stopped before the owner changes, while the macro is held; one that
  cannot be stopped keeps the macro where it was (held, old owner); a run that
  starts while the hand-over is under way does not survive it;
* past run rows keep their owner, so the old owner's month usage holds, also after
  the new owner deletes the macro;
* both owners are told (a notice their Macros list shows, naming no document), and
  a Comment names the admin and both owners;
* nothing else holds on to the old owner (design assumption): the new owner can
  open, edit and run the macro, the old owner none of it;
* before the migrate (no hold fields) it works, the macro stood down by switching
  it off instead of a hold.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import frappe
from frappe.handler import is_valid_http_method
from frappe.utils import set_request

from jarvis.chat import macros, macros_admin_api, macros_api
from jarvis.exceptions import PermissionDeniedError
from jarvis.jarvis.doctype.jarvis_macro import jarvis_macro as controller
from jarvis.tests import test_macro_admin_api as e1
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tests.race_harness import other_connection
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
STEP = "Jarvis Macro Step"
TARGET = "macro-handover-target@example.com"
DISABLED = "macro-handover-disabled@example.com"
NO_ACCESS = "macro-handover-noaccess@example.com"
HELPERS = (TARGET, DISABLED, NO_ACCESS)
NOTICE = macros_api.ADMIN_HANDOVER_NOTICE
STOPPED = macros_admin_api.STOPPED_BY_ADMIN
_MISSING = object()


def _columns() -> bool:
	return controller.hold_fields_exist()


class HandoverBase(e1.AdminBase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_user(TARGET)
		ensure_user(DISABLED)
		frappe.db.set_value("User", DISABLED, "enabled", 0)
		ensure_user(NO_ACCESS, roles=())
		for role in frappe.get_roles(NO_ACCESS):
			if role in ("Jarvis User", "Jarvis Admin", "System Manager"):
				frappe.get_doc("User", NO_ACCESS).remove_roles(role)
		frappe.db.commit()

	def _purge(self):
		super()._purge()
		frappe.set_user("Administrator")
		users = (OWNER, OTHER, ADMIN, *HELPERS)
		for owner in HELPERS:
			for dt in (RUN, MACRO, CONV):
				for name in frappe.get_all(dt, filters={"owner": owner}, pluck="name"):
					frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
			frappe.db.delete("Notification Log", {"for_user": owner})
		frappe.db.delete("DocShare", {"share_doctype": MACRO, "user": ["in", users]})
		frappe.db.delete("ToDo", {"reference_type": MACRO, "allocated_to": ["in", users]})
		frappe.db.delete("Document Follow", {"ref_doctype": MACRO, "user": ["in", users]})
		frappe.db.commit()

	def _handover(self, macro, new_owner=TARGET, *, who=ADMIN):
		frappe.set_user(who)
		return macros_admin_api.admin_handover(macro, new_owner)

	def _row(self, macro):
		fields = [
			"owner",
			"enabled",
			"schedule_enabled",
			"next_run_at",
			"skip_confirmation",
			"merged_prompt",
			"merge_status",
			"merge_conversation",
			"_assign",
		]
		if _columns():
			fields += ["admin_hold", "admin_hold_reason"]
		return frappe.db.get_value(MACRO, macro, fields, as_dict=True)

	def _full_macro(self, tag="a"):
		"""OWNER's macro with everything a hand-over must not carry over: on, scheduled,
		armed, a summary, a skill on each step, a share, an assignment and a follower."""
		macro = self._macro(
			OWNER,
			tag,
			steps=("first prompt", "second prompt"),
			enabled=1,
			schedule_enabled=1,
			next_run_at=frappe.utils.add_days(frappe.utils.now_datetime(), 1),
			skip_confirmation=1,
			merged_prompt="Do whatever the old owner wrote here.",
			merge_status="ready",
			_assign=frappe.as_json([OTHER]),
		)
		frappe.db.sql(
			f"UPDATE `tab{STEP}` SET skills=%s WHERE parent=%s",
			(frappe.as_json(["old-private-skill"]), macro),
		)
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "DocShare",
				"share_doctype": MACRO,
				"share_name": macro,
				"user": OTHER,
				"read": 1,
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "ToDo",
				"allocated_to": OTHER,
				"reference_type": MACRO,
				"reference_name": macro,
				"description": "look at this",
				"status": "Open",
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{"doctype": "Document Follow", "ref_doctype": MACRO, "ref_docname": macro, "user": OWNER}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return macro

	def _notices(self, user):
		return frappe.get_all(
			"Notification Log",
			filters={"for_user": user, "subject": ["like", f"{NOTICE}:%"]},
			fields=["name", "subject", "email_content", "document_type", "document_name"],
		)

	def _untouched(self, macro, before):
		"""Nothing of a refused hand-over was written."""
		self.assertEqual(self._row(macro), before)
		self.assertEqual(self._comments(macro), [])
		for user in (OWNER, TARGET):
			self.assertEqual(self._notices(user), [])


class WithColumns(HandoverBase):
	def setUp(self):
		if not _columns():
			self.skipTest("the site has not migrated the hold fields (bench migrate)")
		super().setUp()


class BeforeTheMigrate(HandoverBase):
	"""The hold fields are not in the doctype: patched to say so where they exist,
	as test_macro_admin_hold does."""

	def setUp(self):
		super().setUp()
		for target in (
			patch.object(controller, "hold_fields_exist", return_value=False),
			patch.object(macros_admin_api, "hold_fields_exist", return_value=False),
		):
			target.start()
			self.addCleanup(target.stop)


# --------------------------------------------------------------------------- #
# What arrives
# --------------------------------------------------------------------------- #
class ArrivesClean:
	"""Shared by the with-columns and before-the-migrate runs."""

	def test_it_arrives_off_unscheduled_disarmed_and_with_nothing_of_the_old_owners(self):
		macro = self._full_macro()
		res = self._handover(macro)
		self.assertEqual(res, {"ok": True, "handed_over": True, "new_owner": TARGET, "stopped_runs": 0})
		row = self._row(macro)
		self.assertEqual(row.owner, TARGET)
		self.assertEqual(
			(row.enabled, row.schedule_enabled, row.next_run_at, row.skip_confirmation), (0, 0, None, 0)
		)
		self.assertEqual(
			(row.merged_prompt or "", row.merge_status or "", row.merge_conversation or ""), ("", "", "")
		)
		self.assertFalse(row._assign)
		if _columns():
			self.assertEqual((row.admin_hold, row.admin_hold_reason), (0, None))
		steps = frappe.get_all(
			STEP, filters={"parent": macro}, fields=["prompt", "skills", "owner"], order_by="idx"
		)
		self.assertEqual([s.prompt for s in steps], ["first prompt", "second prompt"])
		self.assertEqual({s.skills for s in steps}, {"[]"})
		self.assertEqual({s.owner for s in steps}, {TARGET})
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": MACRO, "share_name": macro}))
		self.assertEqual(
			frappe.get_all(
				"ToDo", filters={"reference_type": MACRO, "reference_name": macro}, pluck="status"
			),
			["Cancelled"],
		)
		self.assertFalse(frappe.db.exists("Document Follow", {"ref_doctype": MACRO, "ref_docname": macro}))

	def test_the_owner_is_written_as_the_user_is_stored(self):
		# The database matches the name whatever its case; the session user's name is
		# the stored one, and an owner check in Python compares exactly.
		macro = self._macro(OWNER, "case")
		self.assertEqual(self._handover(macro, TARGET.upper())["new_owner"], TARGET)
		self.assertEqual(self._row(macro).owner, TARGET)
		frappe.set_user(TARGET)
		self.assertEqual(macros_api.get_macro(macro)["name"], macro)

	def test_the_new_owner_can_open_edit_and_run_it_and_the_old_owner_cannot(self):
		# Nothing but the macro row (and its steps) carries the macro's owner: the run
		# rows, chats and messages carry their own.
		macro = self._macro(OWNER, "use", steps=("one", "two"))
		self._handover(macro)
		frappe.set_user(TARGET)
		self.assertEqual(macros_api.get_macro(macro)["name"], macro)
		macros_api.update_macro(macro, description="mine now", enabled=1)
		self.assertEqual(frappe.db.get_value(MACRO, macro, ["description", "enabled"]), ("mine now", 1))
		with patch(
			"jarvis.chat.api._enqueue_turn", return_value={"run_id": "r", "message_id": "m"}
		) as enqueue:
			started = macros.run_macro(macro)
		enqueue.assert_called_once()
		run = started["data"]["macro_run"]
		self.assertEqual(frappe.db.get_value(RUN, run, "owner"), TARGET)
		self.assertEqual(frappe.db.get_value(CONV, started["data"]["conversation"], "owner"), TARGET)
		frappe.set_user(OWNER)
		for attempt in (
			lambda: macros_api.get_macro(macro),
			lambda: macros_api.update_macro(macro, description="still mine?"),
			lambda: macros.run_macro(macro),
			lambda: macros_api.delete_macro(macro),
		):
			with self.assertRaises(frappe.PermissionError):
				attempt()
			frappe.db.rollback()
		self.assertEqual(frappe.db.get_value(MACRO, macro, "description"), "mine now")


class TestWhatArrives(ArrivesClean, WithColumns):
	pass


class TestWhatArrivesBeforeTheMigrate(ArrivesClean, BeforeTheMigrate):
	def test_it_stands_the_macro_down_without_a_hold(self):
		# The first write switches it off, unschedules and disarms it: the scheduler
		# does not start it while its runs are stopped.
		macro = self._full_macro()
		seen = []
		real_stop = macros_admin_api._stop_live_runs

		def stop(name, **kwargs):
			seen.append(frappe.db.get_value(MACRO, name, ["owner", "enabled", "schedule_enabled"]))
			return real_stop(name, **kwargs)

		with patch.object(macros_admin_api, "_stop_live_runs", side_effect=stop):
			self._handover(macro)
		self.assertEqual(seen[0], (OWNER, 0, 0))
		self.assertEqual(self._row(macro).owner, TARGET)

	def test_a_run_started_by_hand_after_the_stop_is_caught_under_the_lock(self):
		# No hold: the old owner can still start the macro until the owner changes.
		# A run that commits between the stop and the lock is found under the lock,
		# stopped in the next round, and the hand-over goes through.
		macro = self._macro(OWNER, "late")
		frappe.set_user(OWNER)
		conv = frappe.get_doc({"doctype": CONV, "title": "late run"})
		conv.flags.ignore_permissions = True
		conv.insert()
		frappe.db.commit()
		late = frappe.generate_hash(length=10)
		real_lock = macros_admin_api._lock
		calls = []

		def lock(row):
			calls.append(row.name)
			if len(calls) == 2:  # the lock before the owner write, first round
				now = frappe.utils.now()
				with other_connection() as other:
					other.sql(
						f"""INSERT INTO `tab{RUN}`
							(name, creation, modified, owner, modified_by, docstatus, macro, conversation,
							 status, current_step, total_steps, run_mode, `trigger`)
						VALUES (%s, %s, %s, %s, %s, 0, %s, %s, 'running', 1, 2, 'stepped', 'manual')""",
						(late, now, now, OWNER, OWNER, macro, conv.name),
					)
					other.commit()
			return real_lock(row)

		with patch.object(macros_admin_api, "_lock", side_effect=lock):
			res = self._handover(macro)
		self.assertEqual(res["stopped_runs"], 1)
		self.assertEqual(self._run(late).status, "stopped")
		self.assertEqual(frappe.db.get_value(RUN, late, "owner"), OWNER)
		self.assertEqual(self._row(macro).owner, TARGET)

	def test_a_macro_that_keeps_starting_runs_is_not_handed_over(self):
		macro = self._macro(OWNER, "loop")
		with patch.object(macros, "live_runs_of", return_value=["phantom"]):
			with patch.object(macros, "_stop_run", return_value=False):
				with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
					self._handover(macro)
		self.assertIn("kept starting runs", str(raised.exception))
		self.assertEqual(self._row(macro).owner, OWNER)


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
class TestRefusals(WithColumns):
	def test_a_target_that_cannot_own_it_is_refused_with_nothing_written(self):
		macro = self._macro(OWNER, "a")
		self._macro(TARGET, "a")  # TARGET already has one of that name
		before = self._row(macro)
		for target, words in (
			("nobody-here@example.com", "There is no user"),
			(DISABLED, "is disabled"),
			(NO_ACCESS, "does not have access to Jarvis"),
			("Administrator", "Choose a named user"),
			("Guest", "Choose a named user"),
			(OWNER, "already owns this macro"),
			(TARGET, "already has a macro named"),
		):
			with self.subTest(target=target):
				with patch.object(macros, "_lock_macro_row") as lock:
					with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
						self._handover(macro, target)
				self.assertIn(words, str(raised.exception))
				lock.assert_not_called()
		self._untouched(macro, before)

	def test_a_full_target_is_refused(self):
		macro = self._macro(OWNER, "a")
		self._macro(TARGET, "other name")
		before = self._row(macro)
		with patch.object(controller, "MAX_MACROS_PER_OWNER", 1):
			with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
				self._handover(macro)
		self.assertIn("the most one user can have", str(raised.exception))
		self._untouched(macro, before)
		# One under the cap goes.
		with patch.object(controller, "MAX_MACROS_PER_OWNER", 2):
			self._handover(macro)
		self.assertEqual(self._row(macro).owner, TARGET)

	def test_the_admins_own_macro_is_refused_and_handing_one_to_oneself_is_not(self):
		own = self._macro(ADMIN, "own")
		before = self._row(own)
		with self.assertRaises(frappe.ValidationError) as raised:
			self._handover(own)
		self.assertIn("your own macro", str(raised.exception))
		self._untouched(own, before)
		# An admin takes over a leaver's macro: allowed, and they are not notified of
		# their own act.
		macro = self._macro(OWNER, "theirs")
		self._handover(macro, ADMIN)
		self.assertEqual(self._row(macro).owner, ADMIN)
		self.assertEqual(self._notices(ADMIN), [])
		self.assertEqual(len(self._notices(OWNER)), 1)

	def test_anyone_but_an_admin_is_refused(self):
		macro = self._macro(OWNER, "a")
		before = self._row(macro)
		for who in (PLAIN, OWNER, TARGET):
			with self.subTest(who=who):
				for fn, kwargs in (
					(macros_admin_api.admin_handover, {"macro": macro, "new_owner": TARGET}),
					(macros_admin_api.admin_handover_targets, {"search": ""}),
				):
					frappe.set_user(who)
					with self.assertRaises(frappe.PermissionError):
						fn(**kwargs)
		self._untouched(macro, before)

	def test_get_is_refused(self):
		prev = getattr(frappe.local, "request", _MISSING)
		self.addCleanup(e1.TestTheGates._restore_request, prev)
		for verb in ("admin_handover", "admin_handover_targets"):
			with self.subTest(verb=verb):
				fn = getattr(macros_admin_api, verb)
				self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])
				set_request(method="GET", path=f"/api/method/{PATH}{verb}")
				with self.assertRaises(frappe.PermissionError):
					is_valid_http_method(fn)

	def test_arguments_that_are_not_text_are_refused_before_anything_is_written(self):
		# A dict reaches the database layer as a filter and would match some other row.
		macro = self._macro(OWNER, "a")
		before = self._row(macro)
		frappe.set_user(ADMIN)
		for bad in (None, "", "  ", ["x"], {"owner": OWNER}, 7):
			for kwargs in ({"macro": bad, "new_owner": TARGET}, {"macro": macro, "new_owner": bad}):
				with self.subTest(kwargs=kwargs):
					with (
						patch.object(frappe.db, "set_value") as set_value,
						patch.object(macros, "_lock_macro_row") as lock,
					):
						with self.assertRaises(frappe.ValidationError):
							macros_admin_api.admin_handover(**kwargs)
					set_value.assert_not_called()
					lock.assert_not_called()
		with self.assertRaises(frappe.ValidationError):
			macros_admin_api.admin_handover_targets(search={"owner": OWNER})
		self._untouched(macro, before)

	def test_refused_inside_a_tool_call_and_through_run_method(self):
		macro = self._macro(OWNER, "a")
		before = self._row(macro)
		frappe.set_user(ADMIN)
		gates = e1.TestTheGates()
		for verb, kwargs in (
			("admin_handover", {"macro": macro, "new_owner": TARGET}),
			("admin_handover_targets", {"search": ""}),
		):
			with self.subTest(verb=verb):
				fn = getattr(macros_admin_api, verb)
				self.assertIsInstance(gates._raised_in_a_tool(fn, **kwargs), frappe.PermissionError)
				self.assertTrue(_is_denied(fn))
				with self.assertRaises(PermissionDeniedError):
					run_method(PATH + verb, kwargs)
		self._untouched(macro, before)


# --------------------------------------------------------------------------- #
# Live runs
# --------------------------------------------------------------------------- #
class TestLiveRuns(WithColumns):
	def test_a_live_run_is_stopped_first_while_the_macro_is_held(self):
		run, conv, macro = self._mk_run(steps=3, at_step=1, armed=True, tag=f"{TAG}-live")
		queued, _, _ = self._step(conv, "queued")
		seen = []
		real_stop = macros._stop_run

		def stop(name, **kwargs):
			seen.append(frappe.db.get_value(MACRO, macro, ["owner", "admin_hold"]))
			return real_stop(name, **kwargs)

		with patch.object(macros, "_stop_run", side_effect=stop):
			self.assertEqual(self._handover(macro)["stopped_runs"], 1)
		self.assertEqual(seen, [(OWNER, 1)])
		row = self._run(run)
		self.assertEqual((row.status, row.error), ("stopped", STOPPED))
		self.assertEqual(frappe.db.get_value(RUN, run, "owner"), OWNER)
		self.assertEqual(frappe.db.get_value(CONV, conv, ["skip_confirmation", "owner"]), (0, OWNER))
		self.assertEqual(self._turn_state(queued), "cancelled")
		(closing,) = self._closing(conv, run)
		self.assertIn(STOPPED, closing.content)
		self.assertEqual(self._row(macro).owner, TARGET)

	def test_a_run_that_will_not_stop_keeps_the_macro_held_and_where_it_was(self):
		run, _conv, macro = self._mk_run(steps=3, at_step=1, armed=True, tag=f"{TAG}-stuck")
		frappe.clear_messages()
		with patch.object(macros, "_stop_run", side_effect=frappe.ValidationError("could not be stopped")):
			with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
				self._handover(macro)
		self.assertIn("was not handed over", str(raised.exception))
		self.assertIn(run, str(raised.exception))
		self.assertIn("stays on hold", str(raised.exception))
		row = self._row(macro)
		self.assertEqual((row.owner, row.admin_hold, row.enabled), (OWNER, 1, 0))
		self.assertEqual(self._run(run).status, "running")
		self.assertEqual(self._notices(TARGET), [])
		# Sent again once the run can be stopped, it goes through.
		self.assertEqual(self._handover(macro)["stopped_runs"], 1)
		self.assertEqual(self._row(macro).owner, TARGET)

	def test_a_run_start_in_progress_when_the_handover_begins_is_stopped(self):
		# The run start is mid-insert on another connection (row locked, run row
		# written, not committed). The hand-over's hold waits for the row; the run it
		# then finds committed is stopped before the owner changes: no run of the old
		# owner survives armed.
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
			timer = threading.Timer(1.0, other._conn.commit)
			timer.start()
			try:
				res = self._handover(macro)
			finally:
				timer.join()
		self.assertEqual(res["stopped_runs"], 1)
		self.assertEqual(self._run(run).status, "stopped")
		self.assertEqual(frappe.db.get_value(CONV, conv.name, "skip_confirmation"), 0)
		self.assertEqual(self._row(macro).owner, TARGET)

	def test_the_old_owner_cannot_start_it_once_the_handover_has_begun(self):
		# Between the hold and the owner write the macro is held; after it, it is not
		# theirs. Either way their run start is refused and leaves no run.
		macro = self._macro(OWNER, "after")
		real_stop = macros_admin_api._stop_live_runs
		refused = []

		def stop(name, **kwargs):
			frappe.set_user(OWNER)
			try:
				with patch("jarvis.chat.api._enqueue_turn") as enqueue:
					macros.run_macro(name)
			except frappe.ValidationError as e:
				refused.append(type(e).__name__)
			frappe.db.rollback()
			enqueue.assert_not_called()
			frappe.set_user(ADMIN)
			return real_stop(name, **kwargs)

		with patch.object(macros_admin_api, "_stop_live_runs", side_effect=stop):
			self._handover(macro)
		self.assertEqual(refused, ["MacroOnHoldError"])
		frappe.set_user(OWNER)
		with self.assertRaises(frappe.PermissionError):
			macros.run_macro(macro)
		frappe.db.rollback()
		self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)

	def test_a_summary_in_progress_is_given_up_and_its_chat_removed(self):
		macro = self._macro(OWNER, "sum", steps=("one", "two"))
		frappe.set_user(OWNER)
		chat = frappe.get_doc({"doctype": CONV, "title": "summary", "status": "Archived"})
		chat.flags.ignore_permissions = True
		chat.insert()
		frappe.db.set_value(
			MACRO, macro, {"merge_status": "pending", "merge_conversation": chat.name}, update_modified=False
		)
		frappe.db.commit()
		self._handover(macro)
		row = self._row(macro)
		self.assertEqual((row.merge_status or "", row.merge_conversation or ""), ("", ""))
		self.assertFalse(frappe.db.exists(CONV, chat.name))


# --------------------------------------------------------------------------- #
# History, notices, the lists
# --------------------------------------------------------------------------- #
class TestHistoryAndNotices(WithColumns):
	def test_past_runs_keep_the_old_owner_and_their_month_usage_survives_a_delete(self):
		macro = self._macro(OWNER, "budget")
		kept = self._run_row(macro, OWNER, "completed", trigger="scheduled", current_step=3)
		manual = self._run_row(macro, OWNER, "completed", trigger="manual")
		before = macros._scheduled_steps_this_month(OWNER)
		self.assertGreaterEqual(before, 3)
		self._handover(macro)
		self.assertEqual(frappe.db.get_value(RUN, kept, "owner"), OWNER)
		self.assertEqual(frappe.db.get_value(RUN, manual, "owner"), OWNER)
		self.assertEqual(macros._scheduled_steps_this_month(OWNER), before)
		self.assertEqual(macros._scheduled_steps_this_month(TARGET), 0)
		# The new owner deletes it: the month's scheduled row stays, still the old owner's.
		frappe.set_user(TARGET)
		macros_api.delete_macro(macro)
		self.assertFalse(frappe.db.exists(MACRO, macro))
		self.assertEqual(frappe.db.get_value(RUN, kept, ["owner", "macro"]), (OWNER, None))
		self.assertEqual(macros._scheduled_steps_this_month(OWNER), before)

	def test_both_owners_are_told_with_no_document_named(self):
		macro = self._macro(OWNER, "told")
		self._handover(macro)
		(old,) = self._notices(OWNER)
		(new,) = self._notices(TARGET)
		for note in (old, new):
			self.assertEqual(note.subject, f"{NOTICE}: {TAG}-told")
			self.assertFalse(note.document_type or note.document_name)
		self.assertIn(TARGET, old.email_content)
		self.assertIn(OWNER, new.email_content)
		self.assertIn("switched off, unscheduled and not armed", new.email_content)
		self.assertEqual(self._notices(ADMIN), [])

	def test_a_comment_names_the_admin_and_both_owners(self):
		macro = self._macro(OWNER, "comment")
		self._handover(macro)
		texts = [c.content for c in self._comments(macro)]
		self.assertTrue(
			any(ADMIN in t and f"from {OWNER} to {TARGET}" in t for t in texts),
			texts,
		)
		self.assertTrue(any("on hold" in t and TARGET in t for t in texts), texts)
		self.assertEqual({c.comment_email for c in self._comments(macro)}, {ADMIN})

	def test_the_lists_follow_the_macro_and_show_the_notice_until_dismissed(self):
		macro = self._macro(OWNER, "lists", enabled=1, skip_confirmation=1)
		self._handover(macro)
		frappe.set_user(OWNER)
		page = macros_api.list_macros_page(search=TAG)
		self.assertNotIn(macro, [r["name"] for r in page["rows"]])
		(old_notice,) = page["notices"]
		self.assertIn(TARGET, old_notice["message"])
		frappe.set_user(TARGET)
		page = macros_api.list_macros_page(search=TAG)
		(row,) = [r for r in page["rows"] if r["name"] == macro]
		self.assertEqual(
			(row["enabled"], row["skip_confirmation"], row["schedule_enabled"], row["admin_hold"]),
			(0, 0, 0, 0),
		)
		(notice,) = page["notices"]
		self.assertEqual(macros_api.dismiss_macro_notices([notice["name"]])["dismissed"], 1)
		self.assertEqual(macros_api.list_macros_page(search=TAG)["notices"], [])
		# The old owner's is theirs to dismiss, not the new owner's.
		self.assertEqual(macros_api.dismiss_macro_notices([old_notice["name"]])["dismissed"], 0)
		(row,) = [r for r in self._list()["rows"] if r["name"] == macro]
		self.assertEqual(row["owner"], TARGET)


# --------------------------------------------------------------------------- #
# Who a macro can go to
# --------------------------------------------------------------------------- #
class TestTargets(HandoverBase):
	def test_only_enabled_jarvis_users_are_offered_with_their_macro_count(self):
		self._macro(TARGET, "counted")
		frappe.set_user(ADMIN)
		res = macros_admin_api.admin_handover_targets(search="macro-handover")
		self.assertEqual(res["max"], controller.MAX_MACROS_PER_OWNER)
		self.assertEqual(res["users"], [{"user": TARGET, "full_name": "macro-handover-target", "macros": 1}])
		names = [u["user"] for u in macros_admin_api.admin_handover_targets(search="")["users"]]
		for absent in (DISABLED, NO_ACCESS, "Administrator", "Guest"):
			self.assertNotIn(absent, names)
