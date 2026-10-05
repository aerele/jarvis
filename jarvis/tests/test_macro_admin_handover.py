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
  it off instead of a hold;
* the owner the dialog showed is matched as the user is stored; the old owner's
  Re-summarize afterwards is told the macro changed hands; a hand-over that failed
  leaves its own hold reason, which the next one replaces (an admin's is kept).
"""

from __future__ import annotations

import contextvars
import threading
import time
from unittest.mock import patch

import frappe
from frappe.handler import is_valid_http_method
from frappe.utils import set_request

from jarvis.chat import macro_scheduler, macros, macros_admin_api, macros_api
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
# Pass the Jarvis gate, but the macro's doctype permission (create, write, delete)
# is the Jarvis User role's alone: such a user could not switch the macro on.
SM_ONLY = "macro-handover-smonly@example.com"
JA_ONLY = "macro-handover-jaonly@example.com"
# A portal user who holds the Jarvis User role.
WEBSITE = "macro-handover-website@example.com"
# Created in this order, and sorted the other way by full name and by address.
ORDER_FIRST = "macro-handover-order-b@example.com"
ORDER_SECOND = "macro-handover-order-a@example.com"
SECOND_TARGET = "macro-handover-second@example.com"
HELPERS = (
	TARGET,
	DISABLED,
	NO_ACCESS,
	SM_ONLY,
	JA_ONLY,
	WEBSITE,
	ORDER_FIRST,
	ORDER_SECOND,
	SECOND_TARGET,
)
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
		# Desk users without the Jarvis User role (NO_ACCESS with no Jarvis role at
		# all): a desk role keeps each a System User, so the roles are the only thing
		# that tells them apart.
		for user, roles in (
			(NO_ACCESS, ("Report Manager",)),
			(SM_ONLY, ("System Manager",)),
			(JA_ONLY, ("Jarvis Admin",)),
		):
			ensure_user(user, roles=roles)
			for role in ("Jarvis User", "Jarvis Admin", "System Manager"):
				if role not in roles and role in frappe.get_roles(user):
					frappe.get_doc("User", user).remove_roles(role)
		ensure_user(WEBSITE)
		frappe.db.set_value("User", WEBSITE, "user_type", "Website User")
		for user, full_name in ((ORDER_FIRST, "Abe Order"), (ORDER_SECOND, "Zed Order")):
			ensure_user(user)
			frappe.db.set_value("User", user, "full_name", full_name)
		ensure_user(SECOND_TARGET)
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

	def _handover(self, macro, new_owner=TARGET, *, who=ADMIN, expected_owner=_MISSING):
		"""As the dialog sends it: with the owner it showed (the stored one, unless
		given)."""
		if expected_owner is _MISSING:
			expected_owner = frappe.db.get_value(MACRO, macro, "owner")
		frappe.set_user(who)
		return macros_admin_api.admin_handover(macro, new_owner, expected_owner)

	def _row(self, macro):
		fields = [
			"owner",
			"enabled",
			"schedule_enabled",
			"next_run_at",
			"last_run_at",
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
		"""OWNER's macro with everything a hand-over must not carry over: on, scheduled
		(and run on that schedule), armed, a summary, a skill on each step, a share, an
		open assignment and a follower. Also a closed assignment, which stays closed."""
		macro = self._macro(
			OWNER,
			tag,
			steps=("first prompt", "second prompt"),
			enabled=1,
			schedule_enabled=1,
			next_run_at=frappe.utils.add_days(frappe.utils.now_datetime(), 1),
			last_run_at=frappe.utils.add_days(frappe.utils.now_datetime(), -1),
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
			{
				"doctype": "ToDo",
				"allocated_to": OTHER,
				"reference_type": MACRO,
				"reference_name": macro,
				"description": "done already",
				"status": "Closed",
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
		# The old owner's schedule ran it, not the new owner's.
		self.assertIsNone(row.last_run_at)
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
		# The open assignment is cancelled; the closed one is history and stays closed.
		self.assertEqual(
			{
				t.description: t.status
				for t in frappe.get_all(
					"ToDo",
					filters={"reference_type": MACRO, "reference_name": macro},
					fields=["description", "status"],
				)
			},
			{"look at this": "Cancelled", "done already": "Closed"},
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

	def test_the_owner_the_dialog_showed_is_matched_as_the_user_is_stored(self):
		# An API caller may send the owner in another case or padded: that is the same
		# person, so not "changed hands".
		for i, shown in enumerate((OWNER.upper(), f"  {OWNER.title()} ")):
			with self.subTest(shown=shown):
				macro = self._macro(OWNER, f"shown{i}")
				self.assertTrue(self._handover(macro, expected_owner=shown)["handed_over"])
				self.assertEqual(self._row(macro).owner, TARGET)

	def test_the_old_owners_resummarize_after_it_says_it_changed_hands(self):
		# Their form was still open: the refusal says why, and starts nothing.
		macro = self._macro(OWNER, "resum", steps=("one", "two"))
		self._handover(macro)
		with patch("jarvis.chat.api._enqueue_turn", return_value={"ok": True}) as enqueue:
			frappe.set_user(OWNER)
			with self.assertRaises(frappe.PermissionError) as raised:
				macros_api.summarize_macro(macro, force=1)
			self.assertIn("changed hands", str(raised.exception))
			frappe.db.rollback()
			# Someone who never owned it is refused as before, without that sentence.
			frappe.set_user(OTHER)
			with self.assertRaises(frappe.PermissionError) as raised:
				macros_api.summarize_macro(macro, force=1)
			self.assertNotIn("changed hands", str(raised.exception))
			frappe.db.rollback()
		enqueue.assert_not_called()
		row = self._row(macro)
		self.assertEqual((row.owner, row.merge_status or ""), (TARGET, ""))

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

	def test_the_old_owners_scheduled_start_that_reaches_the_lock_late_is_dropped_quietly(self):
		# The sweep claimed the slot as the old owner; the hand-over committed before
		# its run_macro took the row lock. Not a failure of the old owner's: no run
		# row, no Error Log, no notice, and nothing started.
		macro = self._macro(OWNER, "late-sched", steps=("one", "two"), enabled=1)
		self._handover(macro)
		frappe.set_user(TARGET)
		macros_api.update_macro(macro, enabled=1)  # the new owner switched it on again
		for row_enabled in (1, 0):
			with self.subTest(enabled=row_enabled):
				frappe.db.set_value(MACRO, macro, "enabled", row_enabled, update_modified=False)
				frappe.db.commit()
				frappe.set_user(OWNER)
				with patch("jarvis.chat.api._enqueue_turn") as enqueue:
					out = macros.run_macro(macro, trigger="scheduled")
				enqueue.assert_not_called()
				self.assertFalse(out["ok"])
				self.assertEqual(out["reason"], macros.BLOCK_MACRO_CHANGED_OWNER)
				self.assertEqual(frappe.db.count(RUN, {"macro": macro}), 0)
		# A run by hand keeps its refusal: the person asked, and is told why not.
		frappe.set_user(OWNER)
		with self.assertRaises(frappe.PermissionError):
			macros.run_macro(macro)
		frappe.db.rollback()


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
class TestOnlyAJarvisUserReceivesIt(HandoverBase):
	"""Runs before the migrate too: the check does not depend on the hold fields."""

	def test_a_user_who_could_not_use_the_macro_is_refused_and_a_jarvis_user_is_not(self):
		# A System Manager passes the Jarvis gate and may read a macro; a Jarvis Admin
		# passes the gate alone. Neither could switch the macro on, edit or delete it:
		# the doctype's create, write and delete are the Jarvis User role's.
		for target in (SM_ONLY, JA_ONLY):
			with self.subTest(target=target):
				macro = self._macro(OWNER, f"use-{target[:20]}")
				before = self._row(macro)
				with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
					self._handover(macro, target)
				self.assertIn("does not have the Jarvis User role", str(raised.exception))
				self._untouched(macro, before)
		macro = self._macro(OWNER, "use-ju")
		self._handover(macro, SECOND_TARGET)
		self.assertEqual(self._row(macro).owner, SECOND_TARGET)


class TestRefusals(WithColumns):
	def test_a_target_that_cannot_own_it_is_refused_with_nothing_written(self):
		macro = self._macro(OWNER, "a")
		self._macro(TARGET, "a")  # TARGET already has one of that name
		before = self._row(macro)
		for target, words in (
			("nobody-here@example.com", "There is no user"),
			(DISABLED, "is disabled"),
			(NO_ACCESS, "does not have access to Jarvis"),
			(WEBSITE, "does not have access to Jarvis"),
			(SM_ONLY, "does not have the Jarvis User role"),
			(JA_ONLY, "does not have the Jarvis User role"),
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

	def test_a_name_that_is_no_user_comes_back_escaped(self):
		macro = self._macro(OWNER, "esc")
		with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
			self._handover(macro, "<b>x</b>@example.com")
		self.assertIn("&lt;b&gt;x&lt;/b&gt;", str(raised.exception))
		self.assertNotIn("<b>", str(raised.exception))

	def test_a_macro_that_changed_hands_since_the_dialog_showed_it_is_refused(self):
		# The pane showed OTHER as the owner (a stale list): the admin meant OTHER's
		# macro, not whoever owns it now.
		macro = self._macro(OWNER, "stale")
		before = self._row(macro)
		with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
			self._handover(macro, expected_owner=OTHER)
		self.assertIn("changed hands", str(raised.exception))
		self._untouched(macro, before)

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
					(
						macros_admin_api.admin_handover,
						{"macro": macro, "new_owner": TARGET, "expected_owner": OWNER},
					),
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
			for kwargs in (
				{"macro": bad, "new_owner": TARGET, "expected_owner": OWNER},
				{"macro": macro, "new_owner": bad, "expected_owner": OWNER},
				{"macro": macro, "new_owner": TARGET, "expected_owner": bad},
			):
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
			("admin_handover", {"macro": macro, "new_owner": TARGET, "expected_owner": OWNER}),
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

	def test_the_refusal_says_how_many_runs_were_stopped(self):
		macro = self._macro(OWNER, "some")
		stopped = self._run_row(macro, OWNER, "running")
		stuck = self._run_row(macro, OWNER, "running")
		real_stop = macros._stop_run

		def stop(name, **kwargs):
			if name == stuck:
				raise frappe.ValidationError("could not be stopped")
			return real_stop(name, **kwargs)

		with patch.object(macros, "_stop_run", side_effect=stop):
			with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
				self._handover(macro)
		self.assertIn("1 of its runs could not be stopped", str(raised.exception))
		self.assertIn("1 other was stopped", str(raised.exception))
		self.assertEqual(self._run(stopped).status, "stopped")
		frappe.db.set_value(RUN, stuck, "status", "stopped")
		frappe.db.commit()

	def test_a_held_macro_keeps_its_hold_and_reason_when_the_handover_fails(self):
		macro = self._macro(OWNER, "held-fail")
		frappe.set_user(ADMIN)
		macros_admin_api.admin_hold(macro, "Emails customers wrongly, do not release")
		run = self._run_row(macro, OWNER, "running")
		with patch.object(macros, "_stop_run", side_effect=frappe.ValidationError("stuck")):
			with self.assertRaises(macros_admin_api.MacroHandoverError):
				self._handover(macro)
		row = self._row(macro)
		self.assertEqual(
			(row.owner, row.admin_hold, row.admin_hold_reason),
			(OWNER, 1, "Emails customers wrongly, do not release"),
		)
		# The Comment says what is under way, and that the hold was already there.
		(note,) = [c.content for c in self._comments(macro) if "handing this macro over" in c.content]
		self.assertIn(TARGET, note)
		self.assertIn("already on hold", note)
		frappe.db.set_value(RUN, run, "status", "stopped")
		frappe.db.commit()

	def _fails(self, macro, new_owner):
		with patch.object(macros, "_stop_run", side_effect=frappe.ValidationError("stuck")):
			with self.assertRaises(macros_admin_api.MacroHandoverError):
				self._handover(macro, new_owner)

	def test_a_second_failed_handover_holds_it_for_itself_not_for_the_first(self):
		# The first hand-over's hold is its own, not an admin's reason to keep.
		macro = self._macro(OWNER, "twice")
		run = self._run_row(macro, OWNER, "running")
		self._fails(macro, TARGET)
		self.assertEqual(self._row(macro).admin_hold_reason, f"Being handed over to {TARGET}.")
		self._fails(macro, SECOND_TARGET)
		row = self._row(macro)
		self.assertEqual(
			(row.owner, row.admin_hold, row.admin_hold_reason),
			(OWNER, 1, f"Being handed over to {SECOND_TARGET}."),
		)
		self.assertFalse([c for c in self._comments(macro) if "already on hold" in c.content])
		# Once the run stops, the third goes through and names no other hold.
		frappe.db.set_value(RUN, run, "status", "stopped")
		frappe.db.commit()
		self._handover(macro, SECOND_TARGET)
		(note,) = [c.content for c in self._comments(macro) if "handed this macro over" in c.content]
		self.assertNotIn("also released", note)

	def test_an_admins_hold_reason_outlasts_two_failed_handovers(self):
		macro = self._macro(OWNER, "twice-held")
		frappe.set_user(ADMIN)
		macros_admin_api.admin_hold(macro, "Emails customers wrongly, do not release")
		run = self._run_row(macro, OWNER, "running")
		self._fails(macro, TARGET)
		self._fails(macro, SECOND_TARGET)
		self.assertEqual(self._row(macro).admin_hold_reason, "Emails customers wrongly, do not release")
		frappe.db.set_value(RUN, run, "status", "stopped")
		frappe.db.commit()

	def test_a_handover_that_lifts_an_earlier_hold_says_so(self):
		macro = self._macro(OWNER, "held-ok")
		frappe.set_user(ADMIN)
		macros_admin_api.admin_hold(macro, "Emails customers wrongly")
		self._handover(macro)
		row = self._row(macro)
		self.assertEqual((row.owner, row.admin_hold, row.admin_hold_reason), (TARGET, 0, None))
		(note,) = [c.content for c in self._comments(macro) if "handed this macro over" in c.content]
		self.assertIn("This also released the hold set for: Emails customers wrongly", note)

	def test_a_hold_another_admin_set_during_the_handover_is_named_when_lifted(self):
		macro = self._macro(OWNER, "held-mid")
		real_stop = macros_admin_api._stop_live_runs
		done = []

		def stop(name, **kwargs):
			if not done:  # admin_hold stops runs too
				done.append(1)
				macros_admin_api.admin_hold(name, "Paused for review")
			return real_stop(name, **kwargs)

		with patch.object(macros_admin_api, "_stop_live_runs", side_effect=stop):
			self._handover(macro)
		self.assertEqual(self._row(macro).owner, TARGET)
		(note,) = [c.content for c in self._comments(macro) if "handed this macro over" in c.content]
		self.assertIn("This also released the hold set for: Paused for review", note)

	def test_a_handover_of_a_macro_that_was_not_held_names_no_other_hold(self):
		macro = self._macro(OWNER, "not-held")
		self._handover(macro)
		(note,) = [c.content for c in self._comments(macro) if "handed this macro over" in c.content]
		self.assertNotIn("also released", note)

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
		self.assertEqual(
			sorted(u["user"] for u in res["users"]),
			sorted((TARGET, ORDER_FIRST, ORDER_SECOND, SECOND_TARGET)),
		)
		self.assertIn({"user": TARGET, "full_name": "macro-handover-target", "macros": 1}, res["users"])
		self.assertEqual(frappe.db.get_value("User", NO_ACCESS, "user_type"), "System User")
		# The first search above already left out DISABLED and NO_ACCESS (and the users
		# without the Jarvis User role), who match it.
		for absent in ("Administrator", "Guest"):
			names = [u["user"] for u in macros_admin_api.admin_handover_targets(search=absent)["users"]]
			self.assertNotIn(absent, names)

	def test_users_without_the_jarvis_user_role_are_not_offered(self):
		# They pass the Jarvis gate, but could not switch a macro on (see the refusal).
		frappe.set_user(ADMIN)
		for absent in (SM_ONLY, JA_ONLY):
			with self.subTest(user=absent):
				names = [u["user"] for u in macros_admin_api.admin_handover_targets(search=absent)["users"]]
				self.assertEqual(names, [])

	def test_a_portal_user_with_the_jarvis_user_role_is_not_offered(self):
		self.assertIn("Jarvis User", frappe.get_roles(WEBSITE))
		frappe.set_user(ADMIN)
		self.assertEqual(macros_admin_api.admin_handover_targets(search=WEBSITE)["users"], [])

	def test_percent_and_underscore_in_the_search_are_matched_as_typed(self):
		frappe.set_user(ADMIN)
		for search in ("macro-handover%target", "macro_handover-target"):
			with self.subTest(search=search):
				self.assertEqual(macros_admin_api.admin_handover_targets(search=search)["users"], [])

	def test_ordered_by_full_name_and_cut_at_the_cap_saying_so(self):
		# ORDER_FIRST was created first, and its address sorts after ORDER_SECOND's:
		# only the full name puts it first.
		frappe.set_user(ADMIN)
		res = macros_admin_api.admin_handover_targets(search="macro-handover-order")
		self.assertEqual([u["user"] for u in res["users"]], [ORDER_FIRST, ORDER_SECOND])
		self.assertFalse(res["more"])
		with patch.object(macros_admin_api, "TARGETS_MAX", 1):
			res = macros_admin_api.admin_handover_targets(search="macro-handover-order")
		self.assertEqual([u["user"] for u in res["users"]], [ORDER_FIRST])
		self.assertTrue(res["more"])
		with patch.object(macros_admin_api, "TARGETS_MAX", 2):
			res = macros_admin_api.admin_handover_targets(search="macro-handover-order")
		self.assertEqual(len(res["users"]), 2)
		self.assertFalse(res["more"])


# --------------------------------------------------------------------------- #
# Races with a second connection
# --------------------------------------------------------------------------- #
def _in_other_context(fn, out: dict) -> threading.Thread:
	"""Run ``fn`` in a thread with its own Frappe context and database connection
	(another request or worker), its result or error put in ``out``."""
	site = frappe.local.site
	sites_path = frappe.local.sites_path

	def body():
		frappe.init(site=site, sites_path=sites_path)
		frappe.connect()
		try:
			out["result"] = fn()
		except BaseException as e:
			out["error"] = e
			out["tb"] = frappe.get_traceback()
			frappe.db.rollback()
		finally:
			frappe.db.commit()
			frappe.destroy()

	t = threading.Thread(target=lambda: contextvars.Context().run(body))
	t.start()
	return t


class TestRaces(WithColumns):
	def _scheduled_macro(self, tag):
		return self._macro(
			OWNER,
			tag,
			steps=("one", "two"),
			enabled=1,
			schedule_enabled=1,
			schedule_frequency="Daily",
			schedule_time="03:00:00",
			next_run_at=frappe.utils.add_days(frappe.utils.now_datetime(), -1),
		)

	def _scheduler_noise(self, macro):
		"""What a failed scheduled start leaves: run rows, the old owner's notices
		about it, Error Logs."""
		frappe.db.rollback()
		frappe.set_user("Administrator")
		return (
			frappe.get_all(RUN, filters={"macro": macro}, fields=["status", "owner"]),
			frappe.get_all(
				"Notification Log",
				filters={"for_user": OWNER, "subject": ["like", "Scheduled macro%"]},
				pluck="subject",
			),
			frappe.get_all("Error Log", filters={"method": ["like", f"%{macro}%"]}, pluck="name"),
		)

	def _clean_scheduler_noise(self, macro):
		frappe.set_user("Administrator")
		frappe.db.delete("Error Log", {"method": ["like", f"%{macro}%"]})
		frappe.db.delete("Notification Log", {"for_user": OWNER, "subject": ["like", "Scheduled macro%"]})
		frappe.db.commit()

	def test_a_scheduled_start_that_holds_the_row_first_is_stopped(self):
		macro = self._scheduled_macro("sched-a")
		locked = threading.Event()
		real_snapshot = macros._steps_snapshot

		def slow_snapshot(doc, merged):
			locked.set()
			time.sleep(1.5)
			return real_snapshot(doc, merged)

		def start():
			frappe.set_user(OWNER)
			return macros.run_macro(macro, trigger="scheduled")

		out = {}
		with (
			patch.object(macros, "_steps_snapshot", side_effect=slow_snapshot),
			patch.object(macros, "_dispatch_step", return_value=None),
			patch.object(macros, "entitlement_block", return_value=None),
			patch.object(macros, "publish_to_user", return_value=None),
		):
			t = _in_other_context(start, out)
			self.assertTrue(locked.wait(10))
			res = self._handover(macro, expected_owner=OWNER)
			t.join(20)
		self.assertNotIn("error", out, out.get("tb"))
		run = out["result"]["data"]["macro_run"]
		self.assertEqual(res["stopped_runs"], 1)
		self.assertEqual(frappe.db.get_value(RUN, run, ["status", "owner"]), ("stopped", OWNER))
		row = self._row(macro)
		self.assertEqual((row.owner, row.admin_hold, row.enabled), (TARGET, 0, 0))

	def test_a_scheduled_start_that_waits_on_the_owner_write_is_dropped_quietly(self):
		# The sweep claimed the slot before the hold, and its run_macro (as the old
		# owner) reaches the row lock only after the owner change committed. Not a
		# failure: no run row, no notice to the old owner, no Error Log.
		macro = self._scheduled_macro("sched-b")
		self._clean_scheduler_noise(macro)
		frappe.set_user("Administrator")
		(m,) = frappe.get_all(
			MACRO,
			filters={"name": macro},
			fields=[
				"name",
				"macro_name",
				"owner",
				"enabled",
				"schedule_frequency",
				"schedule_time",
				"schedule_weekday",
				"schedule_day_of_month",
				"next_run_at",
				"admin_hold",
			],
		)
		now = frappe.utils.now_datetime()
		claimed = frappe.utils.add_days(now, 1)
		real_write = macros_admin_api._write_handover
		out, threads = {}, []

		def sweep():
			frappe.set_user("Administrator")
			with patch.object(macro_scheduler, "_claim_slot", return_value=claimed):
				macro_scheduler._sweep_one(m, now, "Administrator", set())
			return "swept"

		def write(row, new_owner):
			threads.append(_in_other_context(sweep, out))
			time.sleep(1.5)  # the scheduled run_macro now waits for this row lock
			return real_write(row, new_owner)

		with (
			patch.object(macros_admin_api, "_write_handover", side_effect=write),
			patch.object(macros, "_dispatch_step", return_value=None),
			patch.object(macros, "entitlement_block", return_value=None),
			patch.object(macros, "publish_to_user", return_value=None),
		):
			self._handover(macro)
			threads[0].join(20)
		self.assertNotIn("error", out, out.get("tb"))
		self.assertEqual(self._scheduler_noise(macro), ([], [], []))
		self.assertEqual(self._row(macro).owner, TARGET)
		self._clean_scheduler_noise(macro)

	def test_a_second_handover_from_a_stale_dialog_writes_nothing(self):
		# H2's dialog showed OWNER; H1 completed between H2's read and its hold. H2
		# must not hold the macro TARGET just received.
		macro = self._macro(OWNER, "twice")
		real_load = macros_admin_api._load_macro
		state = {}

		def load(name):
			row = real_load(name)
			if not state:
				state["done"] = True
				frappe.set_user(ADMIN)
				with patch.object(macros_admin_api, "_load_macro", side_effect=real_load):
					macros_admin_api.admin_handover(macro, TARGET, OWNER)
			return row

		with patch.object(macros_admin_api, "_load_macro", side_effect=load):
			with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
				self._handover(macro, SECOND_TARGET, expected_owner=OWNER)
		self.assertIn("changed hands", str(raised.exception))
		frappe.db.rollback()
		row = self._row(macro)
		self.assertEqual((row.owner, row.admin_hold, row.admin_hold_reason), (TARGET, 0, None))
		self.assertFalse([c for c in self._comments(macro) if SECOND_TARGET in c.content])
		self.assertEqual(self._notices(SECOND_TARGET), [])

	def test_the_old_owners_summarize_spanning_the_handover_starts_nothing(self):
		# The old owner's Re-summarize passed its checks before the hand-over, and
		# takes the row lock after it.
		macro = self._macro(OWNER, "sumrace", steps=("one", "two"))
		done = []

		def block(owner, **kw):
			if not done:
				done.append(1)
				me = frappe.session.user
				self._handover(macro, expected_owner=OWNER)
				frappe.set_user(me)
			return None

		frappe.set_user(OWNER)
		with (
			patch.object(macros, "entitlement_block", side_effect=block),
			patch("jarvis.chat.api._enqueue_turn", return_value={"ok": True}) as enqueue,
		):
			with self.assertRaises(frappe.PermissionError) as raised:
				macros_api.summarize_macro(macro)
		self.assertIn("changed hands", str(raised.exception))
		frappe.db.rollback()
		enqueue.assert_not_called()
		row = self._row(macro)
		self.assertEqual((row.owner, row.merge_status or "", row.merge_conversation or ""), (TARGET, "", ""))
		self.assertFalse(frappe.db.exists(CONV, {"owner": TARGET, "title": f"Merge: {TAG}-sumrace"}))

	def test_an_admin_delete_racing_a_handover_tells_the_owner_it_deleted(self):
		# The hand-over commits between the delete's read and its lock: the macro the
		# delete removes is TARGET's by then.
		macro = self._macro(OWNER, "del-race")
		real_stop = macros.stop_runs_of_macro
		done = []

		def stop(name, **kwargs):
			if not done:
				done.append(1)
				self._handover(macro, expected_owner=OWNER)
			return real_stop(name, **kwargs)

		frappe.set_user(ADMIN)
		with patch.object(macros, "stop_runs_of_macro", side_effect=stop):
			macros_admin_api.admin_delete(macro)
		self.assertFalse(frappe.db.exists(MACRO, macro))
		deleted = f"{macros_api.ADMIN_DELETE_NOTICE}:%"
		self.assertFalse(
			frappe.db.exists("Notification Log", {"for_user": OWNER, "subject": ["like", deleted]})
		)
		self.assertTrue(
			frappe.db.exists("Notification Log", {"for_user": TARGET, "subject": ["like", deleted]})
		)
