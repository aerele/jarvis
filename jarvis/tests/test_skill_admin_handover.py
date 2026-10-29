"""An admin hands a skill to another owner (``jarvis.chat.skills_handover``, through
``skills_admin_api.admin_handover`` and ``admin_handover_targets``).

What is pinned here:

* who a skill can go to: enabled desk users with the Jarvis User ROLE, never
  Administrator, Guest, a disabled or a portal user, nor the current owner; who is at
  the per-owner cap; the search bounded with ``more``;
* each refusal, with nothing written: a learned row, a published company copy, a Role
  or company skill to a non-reviewer, a target at the cap or without the role, a skill
  that changed hands since the dialog showed it, a same-name skill of the target's;
* the hand-over itself, one transaction: switched off and disarmed, its shares, File Box
  opt-in, assignments and follows cleared, a hold lifted and named, the old owner's
  Pending request rejected, the audit row and the Comment, both owners told, the
  history carrying the new owner;
* an admin may hand a skill to themselves and their own skill away;
* a private source of a published copy takes the copy's lineage with it;
* the races: a same-name skill, or the skill that brings the target to the cap, saved
  between the first read and the lock refuses the hand-over (the locking read of the
  target's rows under the lock).

The endpoints commit, so fixtures are committed and purged around each test, as the
5a tests do (``test_skill_admin_api``).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from jarvis.chat import custom_skills_api, skills_admin, skills_admin_api, skills_handover
from jarvis.jarvis.doctype.jarvis_custom_skill import jarvis_custom_skill as skill_doctype
from jarvis.tests import test_skill_admin_api as base
from jarvis.tests import test_skill_admin_hold as hold_tests
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tests.race_harness import other_connection

SKILL = base.SKILL
LOG = base.LOG
TAG = base.TAG
OWNER = base.OWNER
OTHER = base.OTHER
PLAIN = base.PLAIN
REVIEWER = base.REVIEWER
ADMIN = base.ADMIN
SYSMGR = base.SYSMGR
TARGET = "skadm-target@example.com"
DISABLED = "skadm-disabled@example.com"
WEBSITE = "skadm-website@example.com"
NOTICE = skills_admin.ADMIN_HANDOVER_NOTICE
REJECTED_NOTE = "Rejected: the skill was handed to another owner."
_MISSING = object()


class HandoverBase(hold_tests.HoldBase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_user(TARGET)
		ensure_user(DISABLED)
		frappe.db.set_value("User", DISABLED, "enabled", 0)
		ensure_user(WEBSITE)
		frappe.db.set_value("User", WEBSITE, "user_type", "Website User")
		frappe.db.commit()

	def _purge(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		names = frappe.get_all(SKILL, filters={"skill_name": ["like", f"%{TAG}%"]}, pluck="name")
		if names:
			frappe.db.delete("DocShare", {"share_doctype": SKILL, "share_name": ["in", names]})
			frappe.db.delete("ToDo", {"reference_type": SKILL, "reference_name": ["in", names]})
			frappe.db.delete("Document Follow", {"ref_doctype": SKILL, "ref_docname": ["in", names]})
			frappe.db.delete("Notification Log", {"document_type": SKILL, "document_name": ["in", names]})
		frappe.db.commit()
		super()._purge()

	def _handover(self, skill, new_owner=TARGET, *, who=ADMIN, expected_owner=_MISSING):
		"""As the dialog sends it: with the owner it showed (the stored one, unless given)."""
		if expected_owner is _MISSING:
			expected_owner = frappe.db.get_value(SKILL, skill, "owner")
		frappe.set_user(who)
		return skills_admin_api.admin_handover(skill, new_owner, expected_owner)

	def _targets(self, search=TAG, **kwargs):
		frappe.set_user(ADMIN)
		return skills_admin_api.admin_handover_targets(search=search, **kwargs)

	def _full_skill(self, tag="full"):
		"""OWNER's private skill with everything a hand-over must not carry over: on,
		armed, in File Box, shared with OTHER (the share list and a document share), an
		open assignment and a follower. Also a closed assignment, which stays closed."""
		skill = self._skill(OWNER, tag, allow_approve_run=1, shared_with=[{"user": OTHER}])
		frappe.set_user("Administrator")
		frappe.get_doc(
			{"doctype": "DocShare", "share_doctype": SKILL, "share_name": skill, "user": OTHER, "read": 1}
		).insert(ignore_permissions=True)
		for description, status in (("look at this", "Open"), ("done already", "Closed")):
			frappe.get_doc(
				{
					"doctype": "ToDo",
					"allocated_to": OTHER,
					"reference_type": SKILL,
					"reference_name": skill,
					"description": description,
					"status": status,
				}
			).insert(ignore_permissions=True)
		frappe.get_doc(
			{"doctype": "Document Follow", "ref_doctype": SKILL, "ref_docname": skill, "user": OWNER}
		).insert(ignore_permissions=True)
		# What the open assignment's ToDo writes on the skill, written here so the test
		# does not rest on that.
		frappe.db.set_value(SKILL, skill, "_assign", frappe.as_json([OTHER]), update_modified=False)
		frappe.db.commit()  # the act commits; the fixture must be there for it
		return skill

	def _state(self, skill):
		return self._row(
			skill,
			"owner",
			"enabled",
			"allow_approve_run",
			"use_in_file_box",
			"admin_hold",
			"admin_hold_reason",
			"modified",
		)

	def _handover_log(self, skill):
		return frappe.get_all(
			LOG,
			filters={"skill": skill},
			fields=["verb", "admin", "skill_owner", "new_owner", "reason", "needs_apply"],
			order_by="creation asc",
		)

	def _notices(self, user):
		return frappe.get_all(
			"Notification Log",
			filters={"for_user": user, "subject": ["like", f"{NOTICE}: %{TAG}%"]},
			fields=["subject", "email_content", "document_type", "document_name"],
		)

	def _comments(self, skill):
		return frappe.get_all(
			"Comment",
			filters={"reference_doctype": SKILL, "reference_name": skill, "comment_type": "Info"},
			pluck="content",
		)

	def _untouched(self, skill, before):
		"""Nothing of a refused hand-over was written."""
		frappe.db.rollback()  # what the request does with the error
		self.assertEqual(self._state(skill), before)
		self.assertEqual(self._handover_log(skill), [])
		self.assertEqual(self._comments(skill), [])
		for user in (OWNER, TARGET, REVIEWER, ADMIN):
			self.assertEqual(self._notices(user), [])


# --------------------------------------------------------------------------- #
# What arrives
# --------------------------------------------------------------------------- #
class TestWhatArrives(HandoverBase):
	def test_it_arrives_switched_off_with_nothing_of_the_old_owners(self):
		skill = self._full_skill()
		before = self._state(skill)
		shares = frappe.db.count("Jarvis Custom Skill Share", {"parent": skill}) + frappe.db.count(
			"DocShare", {"share_doctype": SKILL, "share_name": skill}
		)
		assignments = frappe.db.count(
			"ToDo", {"reference_type": SKILL, "reference_name": skill, "status": "Open"}
		) + frappe.db.count("Document Follow", {"ref_doctype": SKILL, "ref_docname": skill})
		self.assertGreaterEqual(shares, 2)
		self.assertGreaterEqual(assignments, 2)
		self.assertTrue(frappe.db.get_value(SKILL, skill, "_assign"))

		res = self._handover(skill)

		self.assertEqual(
			res,
			{
				"ok": True,
				"new_owner": TARGET,
				"cleared": {"shares": shares, "armed": 1, "file_box": 1, "assignments": assignments},
				"hold_lifted": False,
				"requests_cancelled": 0,
				"needs_apply": False,
				"handed_over": True,
				"copies": [],
				"copy_failed": [],
			},
		)
		row = self._state(skill)
		self.assertEqual(
			(row.owner, row.enabled, row.allow_approve_run, row.use_in_file_box), (TARGET, 0, 0, 0)
		)
		self.assertGreater(row.modified, before.modified)  # a form loaded earlier fails its save
		self.assertEqual(frappe.db.get_value(SKILL, skill, "modified_by"), ADMIN)
		self.assertFalse(frappe.db.get_value(SKILL, skill, "_assign"))
		self.assertFalse(frappe.db.exists("Jarvis Custom Skill Share", {"parent": skill}))
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": SKILL, "share_name": skill}))
		self.assertFalse(frappe.db.exists("Document Follow", {"ref_doctype": SKILL, "ref_docname": skill}))
		todos = frappe.get_all(
			"ToDo",
			filters={"reference_type": SKILL, "reference_name": skill},
			fields=["description", "status"],
		)
		self.assertEqual(
			{t.description: t.status for t in todos}, {"look at this": "Cancelled", "done already": "Closed"}
		)

	def test_the_audit_row_the_comment_and_the_history(self):
		skill = self._skill(OWNER, "audit")
		self._handover(skill)
		self.assertEqual(
			self._handover_log(skill),
			[
				{
					"verb": "handover",
					"admin": ADMIN,
					"skill_owner": OWNER,
					"new_owner": TARGET,
					"reason": None,
					"needs_apply": 0,
				}
			],
		)
		(comment,) = self._comments(skill)
		self.assertIn(f"{ADMIN} handed this skill over from {OWNER} to {TARGET}.", comment)
		(entry,) = skills_admin_api.admin_get_skill(skill)["history"]
		self.assertEqual(
			(entry["verb"], entry["new_owner"], entry["new_owner_name"]), ("handover", TARGET, "skadm-target")
		)

	def test_both_owners_are_told_with_no_document_named(self):
		skill = self._skill(OWNER, "told")
		self._handover(skill)
		(old,) = self._notices(OWNER)
		self.assertEqual(old.email_content, f"An admin handed your skill {TAG}-told to skadm-target.")
		(new,) = self._notices(TARGET)
		self.assertEqual(
			new.email_content, f"An admin handed you the skill {TAG}-told. It arrives switched off."
		)
		for note in (old, new):
			self.assertFalse(note.document_name)
		self.assertEqual(self._notices(ADMIN), [])
		# The owners' Skills lists carry it until dismissed.
		frappe.set_user(TARGET)
		notices = custom_skills_api.list_custom_skills_page(search=TAG)["notices"]
		self.assertEqual([n["message"] for n in notices], [new.email_content])

	def test_a_hold_is_lifted_and_named(self):
		skill = self._skill(OWNER, "held", enabled=0, admin_hold=1, admin_hold_reason="Earlier hold")
		res = self._handover(skill)
		self.assertTrue(res["hold_lifted"])
		row = self._state(skill)
		self.assertEqual((row.owner, row.admin_hold, row.admin_hold_reason), (TARGET, 0, None))
		(entry,) = self._handover_log(skill)
		self.assertEqual(entry.reason, "Lifted the hold set for: Earlier hold")
		(comment,) = self._comments(skill)
		self.assertIn("Earlier hold", comment)
		# Both owners are told it was lifted (never the reason: Desk renders it as HTML).
		for user in (OWNER, TARGET):
			(note,) = self._notices(user)
			self.assertTrue(note.email_content.endswith(" The hold on it was lifted."))
			self.assertNotIn("Earlier hold", note.email_content)

	def test_the_old_owners_pending_request_is_rejected(self):
		skill = self._skill(OWNER, "requested")
		request = self._request(skill)
		with patch.object(custom_skills_api, "_stamp_decision") as stamp:
			res = self._handover(skill)
		stamp.assert_not_called()  # it commits: the act would no longer be one transaction
		self.assertEqual(res["requests_cancelled"], 1)
		status = self._status(request)
		self.assertEqual(
			(status.status, status.reviewer, status.decision_note), ("Rejected", ADMIN, REJECTED_NOTE)
		)

	def test_a_company_skill_goes_to_a_reviewer_and_needs_an_apply(self):
		skill = self._skill(OWNER, "org", scope="Org")
		res = self._handover(skill, REVIEWER)
		self.assertTrue(res["needs_apply"])
		# A company skill's File Box setting is its reviewed audience's, and stays.
		self.assertEqual(res["cleared"]["file_box"], 0)
		row = self._state(skill)
		self.assertEqual((row.owner, row.enabled, row.use_in_file_box), (REVIEWER, 0, 1))
		self.assertEqual(self._handover_log(skill)[0].needs_apply, 1)

	def test_nothing_survives_a_failure_before_the_commit(self):
		skill = self._skill(OWNER, "atomic", shared_with=[{"user": OTHER}])
		request = self._request(skill)
		before = self._state(skill)
		with patch.object(skills_admin.admin_log, "write", side_effect=RuntimeError("audit down")):
			with self.assertRaises(RuntimeError):
				self._handover(skill)
		frappe.db.rollback()
		self.assertEqual(self._state(skill), before)
		self.assertTrue(frappe.db.exists("Jarvis Custom Skill Share", {"parent": skill}))
		self.assertEqual(self._status(request).status, "Pending")


# --------------------------------------------------------------------------- #
# Who acts
# --------------------------------------------------------------------------- #
class TestWhoActs(HandoverBase):
	def test_an_admin_may_hand_a_skill_to_themselves(self):
		skill = self._skill(OWNER, "to-self")
		self.assertEqual(self._handover(skill, ADMIN)["new_owner"], ADMIN)
		self.assertEqual(self._state(skill).owner, ADMIN)
		(entry,) = self._handover_log(skill)
		self.assertEqual((entry.admin, entry.skill_owner, entry.new_owner), (ADMIN, OWNER, ADMIN))
		self.assertEqual(len(self._notices(OWNER)), 1)
		self.assertEqual(self._notices(ADMIN), [])  # never told of their own act

	def test_an_admin_may_hand_their_own_skill_away(self):
		skill = self._skill(ADMIN, "own-away")
		self.assertTrue(self._handover(skill)["ok"])
		self.assertEqual(self._state(skill).owner, TARGET)
		(entry,) = self._handover_log(skill)
		self.assertEqual((entry.skill_owner, entry.new_owner), (ADMIN, TARGET))
		self.assertEqual(len(self._notices(TARGET)), 1)
		self.assertEqual(self._notices(ADMIN), [])

	def test_the_owner_is_written_as_the_user_is_stored(self):
		skill = self._skill(OWNER, "case")
		self.assertEqual(
			self._handover(skill, TARGET.upper(), expected_owner=OWNER.upper())["new_owner"], TARGET
		)
		self.assertEqual(self._state(skill).owner, TARGET)

	def test_a_private_source_takes_its_published_copys_lineage(self):
		source = self._skill(OWNER, "lineage-src")
		copy = self._skill("Administrator", "lineage-copy", scope="Role", source_skill=source)
		self._handover(source)
		self.assertEqual(frappe.db.get_value(SKILL, copy, "source_skill"), source)
		row = frappe.db.get_value(SKILL, copy, list(skills_admin.ROW_FIELDS), as_dict=True)
		self.assertEqual(skills_admin.lineage_author(row), TARGET)
		frappe.set_user(ADMIN)
		self.assertEqual(skills_admin_api.admin_get_skill(copy)["lineage"]["author"], TARGET)


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
class TestRefusals(HandoverBase):
	def _refused(self, skill, new_owner=TARGET, *, text=None, error=frappe.ValidationError, **kwargs):
		before = self._state(skill)
		with self.assertRaises(error) as ctx:
			self._handover(skill, new_owner, **kwargs)
		if text:
			self.assertIn(text, str(ctx.exception))
		self._untouched(skill, before)

	def test_a_learned_skill(self):
		skill = self._skill("Administrator", "learned", managed_by_learning=1)
		self._refused(skill, text="learning board")

	def test_a_published_copy(self):
		source = self._skill(OWNER, "pub-src")
		copy = self._skill("Administrator", "pub-copy", scope="Role", source_skill=source)
		self._refused(copy, text="published company copy")

	def test_a_shared_skill_to_someone_who_is_not_a_reviewer(self):
		for scope in ("Role", "Org"):
			with self.subTest(scope=scope):
				skill = self._skill(OWNER, f"shared-{scope.lower()}", scope=scope)
				self._refused(skill, TARGET, text="not a skill reviewer")
		self.assertTrue(self._handover(skill, REVIEWER)["ok"])

	def test_a_target_at_the_cap(self):
		self._skill(TARGET, "already-owned")
		skill = self._skill(OWNER, "over-cap")
		with patch.object(skill_doctype, "MAX_SKILLS_PER_OWNER", 1):
			self._refused(skill, text="the most one user can have")

	def test_a_skill_that_changed_hands_since_the_dialog_showed_it(self):
		# Its own class: the dialog offers Reload by the response's exc_type.
		skill = self._skill(OWNER, "stale")
		self._refused(
			skill,
			expected_owner=OTHER,
			text="changed while you acted on it. Reload.",
			error=skills_handover.SkillOwnerChangedError,
		)

	def test_no_other_refusal_reads_as_a_changed_owner(self):
		skill = self._skill(OWNER, "changed")
		self._skill(TARGET, "changed")
		with self.assertRaises(skills_handover.SkillHandoverError) as ctx:
			self._handover(skill)
		self.assertNotIsInstance(ctx.exception, skills_handover.SkillOwnerChangedError)
		frappe.db.rollback()

	def test_a_same_name_skill_of_the_targets(self):
		skill = self._skill(OWNER, "twin")
		self._skill(TARGET, "twin")
		self._refused(skill, text=f"already has a skill named {TAG}-twin")

	def test_targets_who_could_not_own_it(self):
		skill = self._skill(OWNER, "targets")
		for user, text in (
			("Administrator", "Choose a named user"),
			("Guest", "Choose a named user"),
			(DISABLED, "is disabled"),
			(WEBSITE, "does not have access to Jarvis"),
			(SYSMGR, "Jarvis User role"),  # passes the Jarvis gate, but holds no skill permission
			(OWNER, "already owns this skill"),
			("zz-nobody@example.com", "There is no user"),
		):
			with self.subTest(user=user):
				self._refused(skill, user, text=text, expected_owner=OWNER)

	def test_arguments_that_are_not_names(self):
		skill = self._skill(OWNER, "args")
		for kwargs in (
			{"new_owner": None},
			{"new_owner": {"name": TARGET}},
			{"expected_owner": None},
			{"expected_owner": ["x"]},
		):
			with self.subTest(**{k: repr(v) for k, v in kwargs.items()}):
				new_owner = kwargs.get("new_owner", TARGET)
				expected = kwargs.get("expected_owner", OWNER)
				self._refused(skill, new_owner, expected_owner=expected)

	def test_anyone_but_an_admin(self):
		skill = self._skill(OWNER, "not-admin")
		for user in (PLAIN, REVIEWER, OWNER):
			with self.subTest(user=user):
				self._refused(skill, who=user, error=frappe.PermissionError)
				frappe.set_user(user)
				with self.assertRaises(frappe.PermissionError):
					skills_admin_api.admin_handover_targets(search=TAG)

	def test_before_the_migrate_it_is_not_available(self):
		skill = self._skill(OWNER, "premigrate")
		with patch.object(skills_handover.admin_log, "handover_available", return_value=False):
			self._refused(skill, text="not available yet")
			frappe.set_user(ADMIN)
			res = skills_admin_api.admin_list_skills(search=TAG)
			self.assertFalse(res["handover_available"])
			self.assertTrue(res["delete_available"])
			# The history reads without the column.
			self.assertEqual(skills_admin_api.admin_get_skill(skill)["history"], [])


# --------------------------------------------------------------------------- #
# The races
# --------------------------------------------------------------------------- #
class TestRaces(HandoverBase):
	"""The target's skill is committed on another connection after the act's snapshot
	opened and before the skill row is locked: a plain re-read under the lock reads the
	old snapshot and misses it. The locking read of the target's rows sees it: as the
	refusal where snapshot isolation is off, as 1020 ("busy") where it is on. Both
	refuse, with nothing written."""

	def _race(self, skill, twin, skill_name):
		real_lock_row = skills_admin._lock_row

		def save_then_lock(name, *extra_fields):
			now = frappe.utils.now()
			with other_connection() as other:
				other.sql(
					f"""INSERT INTO `tab{SKILL}` (name, owner, modified_by, creation, modified,
					skill_name, scope, enabled) VALUES (%s, %s, %s, %s, %s, %s, 'User', 1)""",
					(twin, TARGET, TARGET, now, now, skill_name),
				)
				other.commit()
			return real_lock_row(name, *extra_fields)

		before = self._state(skill)
		frappe.set_user(ADMIN)
		with patch.object(skills_admin, "_lock_row", side_effect=save_then_lock):
			with self.assertRaises(frappe.ValidationError):
				skills_admin_api.admin_handover(skill, TARGET, OWNER)
		self._untouched(skill, before)
		self.assertEqual(frappe.db.get_value(SKILL, twin, "owner"), TARGET)

	def test_a_same_name_skill_the_target_saves_while_acting_refuses_it(self):
		skill = self._skill(OWNER, "race")
		self._race(skill, "skadm-race-twin", f"{TAG}-race")

	def test_the_skill_that_fills_the_targets_cap_while_acting_refuses_it(self):
		# The cap patched to 2 stands in for 25: the target owns one, and the second
		# (the one that reaches the cap) lands between the first read and the lock.
		self._skill(TARGET, "cap-race-first")
		skill = self._skill(OWNER, "cap-race")
		with patch.object(skill_doctype, "MAX_SKILLS_PER_OWNER", 2):
			self._race(skill, "skadm-cap-race-last", f"{TAG}-cap-race-last")


# --------------------------------------------------------------------------- #
# Who a skill can go to
# --------------------------------------------------------------------------- #
class TestTargets(HandoverBase):
	def test_only_enabled_jarvis_users_are_offered(self):
		res = self._targets()
		self.assertEqual(set(res), {"targets", "more", "cap"})
		self.assertEqual(res["cap"], skill_doctype.MAX_SKILLS_PER_OWNER)
		users = {t["user"]: t for t in res["targets"]}
		for user in (OWNER, OTHER, PLAIN, REVIEWER, ADMIN, TARGET):
			self.assertIn(user, users)
		# SYSMGR passes the Jarvis gate but holds no Jarvis User role (ledger item 13).
		for user in (SYSMGR, DISABLED, WEBSITE):
			self.assertNotIn(user, users)
		self.assertEqual(users[TARGET], {"user": TARGET, "full_name": "skadm-target", "at_cap": False})
		for absent in ("Administrator", "Guest"):
			names = [t["user"] for t in self._targets(absent)["targets"]]
			self.assertNotIn(absent, names)

	def test_the_current_owner_is_left_out(self):
		skill = self._skill(OWNER, "exclude")
		names = [t["user"] for t in self._targets(skill=skill)["targets"]]
		self.assertNotIn(OWNER, names)
		self.assertIn(TARGET, names)

	def test_who_is_at_the_cap(self):
		self._skill(TARGET, "cap")
		with patch.object(skill_doctype, "MAX_SKILLS_PER_OWNER", 1):
			users = {t["user"]: t["at_cap"] for t in self._targets()["targets"]}
		self.assertTrue(users[TARGET])
		self.assertFalse(users[PLAIN])

	def test_bounded_saying_there_are_more(self):
		with patch.object(skills_handover, "TARGETS_MAX", 1):
			res = self._targets()
		self.assertEqual(len(res["targets"]), 1)
		self.assertTrue(res["more"])
		self.assertFalse(self._targets("skadm-target")["more"])

	def test_percent_and_underscore_are_matched_as_typed(self):
		for search in ("skadm%target", "skadm_target"):
			with self.subTest(search=search):
				self.assertEqual(self._targets(search)["targets"], [])

	def test_a_search_or_skill_that_is_not_text_is_refused(self):
		for kwargs in ({"search": ["x"]}, {"search": {"a": 1}}, {"skill": {"owner": OWNER}}, {"skill": 7}):
			with self.subTest(**{k: repr(v) for k, v in kwargs.items()}):
				frappe.set_user(ADMIN)
				with self.assertRaises(frappe.ValidationError):
					skills_admin_api.admin_handover_targets(**kwargs)
