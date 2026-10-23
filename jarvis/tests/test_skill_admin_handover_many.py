"""An admin hands a skill to several people at once, or gives copies and keeps it where
it is (``skills_admin_api.admin_handover`` with ``new_owners`` and ``keep``).

What is pinned here:

* several people: the first is handed the skill as a single hand-over does, each of the
  others gets a private copy, switched off, asking before every write, no File Box
  opt-in, no shares, with the skill's name, description and instructions;
* a Role or company skill: the first must be a reviewer, a copy is private and goes to
  anyone with the Jarvis User role;
* ``keep``: the skill and its owner are left as they were, and everyone named gets a
  copy;
* every person is checked before anything is written: one who cannot have it refuses
  the whole request, naming them;
* each copy is audited and commented; the old owner is told once and each person given
  a copy is told, never the admin.
"""

from __future__ import annotations

import frappe

from jarvis.chat import skills_admin_api, skills_handover
from jarvis.tests import test_skill_admin_handover as ho

SKILL = ho.SKILL
OWNER = ho.OWNER
OTHER = ho.OTHER
REVIEWER = ho.REVIEWER
ADMIN = ho.ADMIN
TARGET = ho.TARGET
DISABLED = ho.DISABLED
TAG = ho.TAG


class ManyBase(ho.HandoverBase):
	def _give(self, skill, people, *, keep=False, who=ADMIN):
		expected_owner = frappe.db.get_value(SKILL, skill, "owner")
		frappe.set_user(who)
		return skills_admin_api.admin_handover(
			skill, expected_owner=expected_owner, new_owners=people, keep=keep
		)

	def _copy_of(self, skill, user):
		name = frappe.db.get_value(SKILL, skill, "skill_name")
		return frappe.db.get_value(SKILL, {"owner": user, "skill_name": name}, "name")

	def _assert_private_copy(self, copy, user, src_tag):
		row = frappe.db.get_value(
			SKILL,
			copy,
			[
				"owner",
				"enabled",
				"allow_approve_run",
				"use_in_file_box",
				"scope",
				"description",
				"instructions",
				"source_skill",
			],
			as_dict=True,
		)
		self.assertEqual(
			(row.owner, row.enabled, row.allow_approve_run, row.use_in_file_box, row.scope),
			(user, 0, 0, 0, "User"),
		)
		self.assertEqual(
			(row.description, row.instructions), (f"{src_tag} description", f"{src_tag} instructions")
		)
		self.assertFalse(row.source_skill)
		self.assertFalse(frappe.db.exists("Jarvis Custom Skill Share", {"parent": copy}))


class TestSeveralPeople(ManyBase):
	def test_the_first_is_handed_it_and_the_others_get_a_private_copy(self):
		skill = self._full_skill("many")
		res = self._give(skill, [TARGET, OTHER])
		copy = self._copy_of(skill, OTHER)
		self.assertTrue(copy)
		self.assertTrue(res["handed_over"])
		self.assertEqual(res["new_owner"], TARGET)
		self.assertEqual(res["copies"], [{"user": OTHER, "skill": copy}])
		self.assertEqual(res["copy_failed"], [])
		self.assertEqual(self._state(skill).owner, TARGET)
		self._assert_private_copy(copy, OTHER, "many")
		(entry,) = self._handover_log(copy)
		self.assertEqual(
			(entry.verb, entry.admin, entry.skill_owner, entry.new_owner), ("handover", ADMIN, OWNER, OTHER)
		)
		self.assertIn(skill, entry.reason)
		(comment,) = self._comments(copy)
		self.assertIn(ADMIN, comment)

	def test_a_shared_skill_goes_to_a_reviewer_and_its_copies_are_private(self):
		skill = self._skill(OWNER, "org-many", scope="Org")
		res = self._give(skill, [REVIEWER, TARGET])
		self.assertEqual(res["new_owner"], REVIEWER)
		self.assertTrue(res["needs_apply"])
		self._assert_private_copy(self._copy_of(skill, TARGET), TARGET, "org-many")

	def test_a_shared_skill_whose_first_is_no_reviewer_is_refused_whole(self):
		skill2 = self._skill(OWNER, "org-first", scope="Org")
		before = self._state(skill2)
		with self.assertRaises(skills_handover.SkillHandoverError) as raised:
			self._give(skill2, [TARGET, REVIEWER])
		self.assertIn("not a skill reviewer", str(raised.exception))
		self._untouched(skill2, before)
		self.assertIsNone(self._copy_of(skill2, REVIEWER))

	def test_one_who_cannot_have_it_refuses_the_whole_request(self):
		skill = self._skill(OWNER, "refused")
		before = self._state(skill)
		for keep in (False, True):
			with self.subTest(keep=keep):
				with self.assertRaises(skills_handover.SkillHandoverError) as raised:
					self._give(skill, [TARGET, DISABLED], keep=keep)
				self.assertIn(DISABLED, str(raised.exception))
				self._untouched(skill, before)
				self.assertIsNone(self._copy_of(skill, TARGET))

	def test_a_name_clash_for_a_copy_refuses_it(self):
		skill = self._skill(OWNER, "clash")
		self._skill(OTHER, "clash")
		before = self._state(skill)
		with self.assertRaises(skills_handover.SkillHandoverError) as raised:
			self._give(skill, [TARGET, OTHER])
		self.assertIn("already has a skill named", str(raised.exception))
		self._untouched(skill, before)

	def test_the_old_owner_is_told_once_more_and_each_copy_owner_is_told(self):
		skill = self._skill(OWNER, "told")
		self._give(skill, [TARGET, OTHER])
		notes = self._notices(OWNER)
		self.assertEqual(len(notes), 2)  # the hand-over's, and the copies'
		self.assertTrue(any("copy" in n.email_content for n in notes))
		(copy_note,) = self._notices(OTHER)
		self.assertIn("a copy of the skill", copy_note.email_content)
		self.assertEqual(self._notices(ADMIN), [])

	def test_more_than_the_most_is_refused(self):
		skill = self._skill(OWNER, "most")
		people = [f"nobody-{i}@example.com" for i in range(skills_admin_api.HANDOVER_MAX * 2 + 1)]
		with self.assertRaises(skills_handover.SkillHandoverError):
			self._give(skill, people)
		self.assertEqual(self._state(skill).owner, OWNER)


class TestKeep(ManyBase):
	def test_the_skill_stays_as_it_is_and_everyone_gets_a_copy(self):
		skill = self._full_skill("keep")
		before = self._state(skill)
		res = self._give(skill, [TARGET, OTHER], keep=True)
		self.assertEqual((res["handed_over"], res["new_owner"]), (False, None))
		self.assertEqual([c["user"] for c in res["copies"]], [TARGET, OTHER])
		self.assertEqual(self._state(skill), before)
		self.assertTrue(frappe.db.exists("Jarvis Custom Skill Share", {"parent": skill}))
		for user in (TARGET, OTHER):
			self._assert_private_copy(self._copy_of(skill, user), user, "keep")
		(old,) = self._notices(OWNER)
		self.assertIn("Yours is unchanged", old.email_content)

	def test_the_owner_cannot_be_given_a_copy_of_their_own(self):
		skill = self._skill(OWNER, "own-copy")
		before = self._state(skill)
		with self.assertRaises(skills_handover.SkillHandoverError):
			self._give(skill, [OWNER], keep=True)
		self._untouched(skill, before)

	def test_a_single_person_without_keep_is_the_plain_handover(self):
		skill = self._skill(OWNER, "single")
		res = self._give(skill, [TARGET])
		self.assertEqual((res["handed_over"], res["new_owner"], res["copies"]), (True, TARGET, []))
		self.assertEqual(self._state(skill).owner, TARGET)
