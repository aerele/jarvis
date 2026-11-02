"""A Jarvis Admin hands a macro to several people at once, or gives copies and keeps it
where it is (``jarvis.chat.macros_admin_api.admin_handover`` with ``new_owners`` and
``keep``).

What is pinned here:

* several people: the first is handed the macro as a single hand-over does, each of
  the others gets a copy that arrives as a handed macro does (off, unscheduled, not
  armed, no summary, no skills on its steps), owned by them, steps and all;
* ``keep``: the macro, its owner, its arm, its schedule and its live run are left as
  they were, and everyone named gets a copy;
* every person is checked before anything is written: one who cannot have it refuses
  the whole request, naming them, with nothing written for the others;
* the people come as a list, a JSON list or a comma separated text, are named once
  each, and at most ``HANDOVER_MAX``;
* a copy refused by a change made in the meantime is reported, the others stand;
* the old owner is told once, naming where everything went, and each person given a
  copy is told; never the admin about their own act;
* the admin's own macro can be copied with ``keep`` (a copy holds nothing), and is
  still refused a move.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from jarvis.chat import macros_admin_api
from jarvis.tests import test_macro_admin_handover as ho

MACRO = ho.MACRO
RUN = ho.RUN
STEP = ho.STEP
OWNER = ho.OWNER
ADMIN = ho.ADMIN
TARGET = ho.TARGET
SECOND = ho.SECOND_TARGET
THIRD = ho.ORDER_FIRST
COPY_FAILED = "macro handover: copy failed"
DISABLED = ho.DISABLED
TAG = ho.TAG


class ManyBase(ho.WithColumns):
	def _give(self, macro, people, *, keep=False, who=ADMIN):
		expected_owner = frappe.db.get_value(MACRO, macro, "owner")
		frappe.set_user(who)
		return macros_admin_api.admin_handover(
			macro, expected_owner=expected_owner, new_owners=people, keep=keep
		)

	def _copy_of(self, macro, user):
		name = frappe.db.get_value(MACRO, macro, "macro_name")
		return frappe.db.get_value(MACRO, {"owner": user, "macro_name": name}, "name")

	def _assert_clean_copy(self, copy, user, prompts):
		row = self._row(copy)
		self.assertEqual(row.owner, user)
		self.assertEqual(
			(row.enabled, row.schedule_enabled, row.next_run_at, row.skip_confirmation), (0, 0, None, 0)
		)
		self.assertEqual((row.merged_prompt or "", row.merge_status or ""), ("", ""))
		steps = frappe.get_all(
			STEP, filters={"parent": copy}, fields=["prompt", "skills", "owner"], order_by="idx"
		)
		self.assertEqual([s.prompt for s in steps], prompts)
		self.assertEqual({s.skills for s in steps}, {"[]"})
		self.assertEqual({s.owner for s in steps}, {user})
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": MACRO, "share_name": copy}))


class TestSeveralPeople(ManyBase):
	def test_the_first_is_handed_it_and_the_others_get_a_copy(self):
		macro = self._full_macro("many")
		res = self._give(macro, [TARGET, SECOND])
		copy = self._copy_of(macro, SECOND)
		self.assertTrue(copy)
		self.assertEqual(
			res,
			{
				"ok": True,
				"handed_over": True,
				"new_owner": TARGET,
				"stopped_runs": 0,
				"copies": [{"user": SECOND, "macro": copy}],
				"copy_failed": [],
			},
		)
		self.assertEqual(self._row(macro).owner, TARGET)
		self._assert_clean_copy(macro, TARGET, ["first prompt", "second prompt"])
		self._assert_clean_copy(copy, SECOND, ["first prompt", "second prompt"])
		# The copy starts with no runs and no history of the old owner's.
		self.assertEqual(frappe.get_all(RUN, filters={"macro": copy}), [])
		(comment,) = self._comments(copy)
		self.assertIn(ADMIN, comment.content)
		self.assertIn(OWNER, comment.content)

	def test_the_people_come_as_text_and_are_named_once(self):
		macro = self._macro(OWNER, "text")
		res = self._give(macro, f"{TARGET}, {SECOND},{TARGET}")
		self.assertEqual(res["new_owner"], TARGET)
		self.assertEqual([c["user"] for c in res["copies"]], [SECOND])
		macro2 = self._macro(OWNER, "json")
		res = self._give(macro2, frappe.as_json([SECOND, TARGET]))
		self.assertEqual(res["new_owner"], SECOND)
		self.assertEqual([c["user"] for c in res["copies"]], [TARGET])

	def test_more_than_the_most_is_refused_with_nothing_written(self):
		macro = self._macro(OWNER, "most")
		before = self._row(macro)
		people = [f"nobody-{i}@example.com" for i in range(macros_admin_api.HANDOVER_MAX * 2 + 1)]
		with self.assertRaises(macros_admin_api.MacroHandoverError):
			self._give(macro, people)
		with patch.object(macros_admin_api, "_new_owner_arg", side_effect=lambda v: v):
			with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
				self._give(macro, [f"p{i}@example.com" for i in range(macros_admin_api.HANDOVER_MAX + 1)])
		self.assertIn(str(macros_admin_api.HANDOVER_MAX), str(raised.exception))
		self._untouched(macro, before)

	def test_one_who_cannot_have_it_refuses_the_whole_request(self):
		macro = self._macro(OWNER, "refused", enabled=1, skip_confirmation=1)
		before = self._row(macro)
		for people, keep in (([TARGET, DISABLED], False), ([TARGET, DISABLED], True), ([DISABLED], True)):
			with self.subTest(people=people, keep=keep):
				with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
					self._give(macro, people, keep=keep)
				self.assertIn(DISABLED, str(raised.exception))
				frappe.db.rollback()
				self.assertIsNone(self._copy_of(macro, TARGET))
		self._untouched(macro, before)

	def test_a_name_clash_for_any_of_them_refuses_it(self):
		macro = self._macro(OWNER, "clash")
		self._macro(SECOND, "clash")
		before = self._row(macro)
		with self.assertRaises(macros_admin_api.MacroHandoverError) as raised:
			self._give(macro, [TARGET, SECOND])
		self.assertIn(SECOND, str(raised.exception))
		self.assertIn("already has a macro named", str(raised.exception))
		self._untouched(macro, before)

	def test_a_copy_refused_in_the_meantime_is_reported_and_the_rest_stand(self):
		macro = self._macro(OWNER, "meantime")
		real = macros_admin_api._handover_refusal
		calls = []

		def refusal(row, user):
			calls.append(user)
			# The checks before anything is written pass; SECOND fills up after them.
			if user == SECOND and calls.count(SECOND) > 1:
				return f"{SECOND} already has 25 macros, the most one user can have."
			return real(row, user)

		with patch.object(macros_admin_api, "_handover_refusal", side_effect=refusal):
			res = self._give(macro, [TARGET, SECOND], keep=True)
		self.assertEqual([c["user"] for c in res["copies"]], [TARGET])
		self.assertEqual(len(res["copy_failed"]), 1)
		self.assertEqual(res["copy_failed"][0]["user"], SECOND)
		self.assertIn("already has 25 macros", res["copy_failed"][0]["reason"])
		self.assertTrue(self._copy_of(macro, TARGET))
		self.assertIsNone(self._copy_of(macro, SECOND))
		self.assertEqual(frappe.local.message_log, [])

	def test_the_old_owner_is_told_once_and_each_copy_owner_is_told(self):
		macro = self._macro(OWNER, "told")
		self._give(macro, [TARGET, SECOND])
		(old,) = self._notices(OWNER)
		self.assertIn(TARGET, old.email_content)
		self.assertIn(SECOND, old.email_content)
		self.assertFalse(old.document_type or old.document_name)
		(copy_note,) = self._notices(SECOND)
		self.assertIn("a copy of the macro", copy_note.email_content)
		self.assertIn(OWNER, copy_note.email_content)
		self.assertEqual(len(self._notices(TARGET)), 1)
		self.assertEqual(self._notices(ADMIN), [])


class TestKeep(ManyBase):
	def test_the_macro_stays_as_it_is_and_everyone_gets_a_copy(self):
		macro = self._full_macro("keep")
		run = self._run_row(macro, OWNER, "running")
		before = self._row(macro)
		res = self._give(macro, [TARGET, SECOND], keep=True)
		self.assertEqual((res["handed_over"], res["new_owner"], res["stopped_runs"]), (False, None, 0))
		self.assertEqual([c["user"] for c in res["copies"]], [TARGET, SECOND])
		# Owner, arm, schedule, summary, hold: all as they were; the run goes on.
		self.assertEqual(self._row(macro), before)
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "running")
		for user in (TARGET, SECOND):
			self._assert_clean_copy(self._copy_of(macro, user), user, ["first prompt", "second prompt"])
		(old,) = self._notices(OWNER)
		self.assertIn("Yours is unchanged", old.email_content)

	def test_the_owner_cannot_be_given_a_copy_of_their_own(self):
		macro = self._macro(OWNER, "own-copy")
		before = self._row(macro)
		with self.assertRaises(macros_admin_api.MacroHandoverError):
			self._give(macro, [OWNER], keep=True)
		self._untouched(macro, before)

	def test_the_admins_own_macro_can_be_copied_but_not_moved(self):
		macro = self._macro(ADMIN, "admins-own")
		res = self._give(macro, [TARGET], keep=True)
		self.assertEqual([c["user"] for c in res["copies"]], [TARGET])
		self.assertEqual(self._row(macro).owner, ADMIN)
		with self.assertRaises(frappe.ValidationError):
			self._give(macro, [SECOND])
		self.assertEqual(self._row(macro).owner, ADMIN)

	def test_keep_must_be_true_or_false(self):
		macro = self._macro(OWNER, "keep-arg")
		for bad in ("maybe", 7, ["1"]):
			with self.subTest(keep=bad):
				with self.assertRaises(frappe.ValidationError):
					self._give(macro, [TARGET], keep=bad)
		self.assertEqual(self._row(macro).owner, OWNER)


class TestACopyThatBreaks(ManyBase):
	"""Anything a copy throws, not only a refusal, is that copy's failure: the others
	stand, the notices go out, and the error is logged."""

	def _error_logs(self):
		return frappe.db.count("Error Log", {"method": COPY_FAILED})

	def _assert_second_alone_failed(self, macro, res, logs_before):
		self.assertEqual([c["user"] for c in res["copies"]], [TARGET, THIRD])
		(failed,) = res["copy_failed"]
		self.assertEqual(failed["user"], SECOND)
		self.assertIn("something went wrong", failed["reason"])
		self.assertTrue(self._copy_of(macro, TARGET))
		self.assertTrue(self._copy_of(macro, THIRD))
		self.assertIsNone(self._copy_of(macro, SECOND))
		self.assertEqual(self._error_logs(), logs_before + 1)
		# The row says which macro and which copy, with where it failed, and names no one:
		# Error Logs are forwarded off-site.
		log = frappe.get_last_doc("Error Log", {"method": COPY_FAILED})
		self.assertIn(f"{macro}: copy 2 of 3 failed", log.error)
		self.assertIn("_give_copies", log.error)
		for user in (TARGET, SECOND, THIRD, OWNER):
			self.assertNotIn(user, log.error)
		for user in (OWNER, TARGET, THIRD):
			self.assertTrue(self._notices(user), user)
		self.assertEqual(self._notices(SECOND), [])

	def test_an_unexpected_error_on_one_copy_is_reported_and_the_rest_go_through(self):
		macro = self._macro(OWNER, "boom")
		real = macros_admin_api._leave_comment

		def comment(target, text, **kwargs):
			if f"gave {SECOND} this copy" in text:
				raise frappe.QueryDeadlockError
			return real(target, text, **kwargs)

		logs_before = self._error_logs()
		with patch.object(macros_admin_api, "_leave_comment", side_effect=comment):
			res = self._give(macro, [TARGET, SECOND, THIRD], keep=True)
		self._assert_second_alone_failed(macro, res, logs_before)

	def test_a_duplicate_from_the_recipients_insert_is_reported_the_same_way(self):
		macro = self._macro(OWNER, "dupe")
		real = frappe.model.document.Document.insert

		def insert(doc, *args, **kwargs):
			if doc.doctype == MACRO and frappe.session.user == SECOND:
				# The message quotes the recipient, as a failed insert can.
				raise frappe.DuplicateEntryError(MACRO, doc.macro_name, frappe.session.user)
			return real(doc, *args, **kwargs)

		logs_before = self._error_logs()
		with patch.object(frappe.model.document.Document, "insert", insert):
			res = self._give(macro, [TARGET, SECOND, THIRD], keep=True)
		self._assert_second_alone_failed(macro, res, logs_before)

	def test_a_copy_refusal_reads_as_plain_text(self):
		macro = self._macro(OWNER, "R&D sweep")
		real = macros_admin_api._handover_refusal
		calls = []

		def refusal(row, user):
			calls.append(user)
			# The checks before anything is written pass; SECOND takes the name after them.
			if user == SECOND and calls.count(SECOND) == 2:
				self._macro(SECOND, "R&D sweep")
			return real(row, user)

		with patch.object(macros_admin_api, "_handover_refusal", side_effect=refusal):
			res = self._give(macro, [TARGET, SECOND], keep=True)
		(failed,) = res["copy_failed"]
		self.assertIn("R&D sweep", failed["reason"])
		self.assertNotIn("&amp;", failed["reason"])
		self.assertNotIn("something went wrong", failed["reason"])


class TestTheNoticeSubject(ManyBase):
	def test_the_handover_notice_subject_escapes_the_macro_name(self):
		macro = self._macro(OWNER, "<b>Q4</b>")
		self._give(macro, [TARGET])
		for user in (OWNER, TARGET):
			(note,) = self._notices(user)
			self.assertIn("&lt;b&gt;", note.subject)
			self.assertNotIn("<b>", note.subject)
			# The body is shown as text by the Macros list, so it stays as written.
			self.assertIn("<b>Q4</b>", note.email_content)

	def _owner_subjects(self):
		return frappe.get_all("Notification Log", filters={"for_user": OWNER}, pluck="subject")

	def test_the_stopped_by_an_admin_notice_escapes_the_macro_name(self):
		macro = self._macro(OWNER, "<b>Q4</b>")
		macros_admin_api._tell_the_owner(macro, ADMIN)
		(subject,) = self._owner_subjects()
		self.assertIn("&lt;b&gt;", subject)
		self.assertNotIn("<b>", subject)

	def test_the_deleted_by_an_admin_notice_escapes_the_macro_name(self):
		macro = self._macro(OWNER, "<b>Q4</b>")
		frappe.set_user(ADMIN)
		macros_admin_api.admin_delete(macro)
		(subject,) = self._owner_subjects()
		self.assertIn("&lt;b&gt;", subject)
		self.assertNotIn("<b>", subject)
