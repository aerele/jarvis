"""A skill reviewer's edit of a shared skill they do not own (jarvis-admin-v2#720, 5c).

What is pinned here:

* the actor by kind matrix for ``update_custom_skill``: the owner edits their own; a
  reviewer (Jarvis Skill Reviewer, Jarvis Admin, System Manager) edits a Role or
  company skill and a published copy, never a private, shared-with or learned one;
  a plain Jarvis User edits nothing of anyone else's;
* a reviewer may not rename the skill, change "Approve & run", re-scope or delete it;
  the roles they change are checked as a promotion request's are;
* each reviewer edit writes one admin log row (verb ``edit``, the changed fields in
  ``reason``) and tells the author (the lineage author for a published copy), and an
  edit that changes nothing writes neither;
* a held skill keeps its hold: text may change, switching it on is refused;
* the reviewer branch is refused inside a tool call and from a job one queued;
* the share endpoints admit a reviewer on a shared skill, recorded the same way;
* ``get_custom_skill`` carries ``is_owner`` and ``can_edit``, and a reviewer reads a
  Role skill outside their own roles;
* the approved promotion row names the shared copy (``_shared_copies``).

The admin log's ``edit`` verb arrives with a migrate: a site that has not run it
refuses a reviewer edit rather than make one without its record.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import frappe

from jarvis.chat import custom_skills_api, skill_reviewer_edit, skills_admin
from jarvis.jarvis.doctype.jarvis_custom_skill.jarvis_custom_skill import SkillOnHoldError
from jarvis.tests import test_skill_admin_api as base
from jarvis.tests._pending_action_helpers import ensure_user

SKILL = base.SKILL
TAG = base.TAG
OWNER = base.OWNER
OTHER = base.OTHER
PLAIN = base.PLAIN
REVIEWER = base.REVIEWER
ADMIN = base.ADMIN
SYSMGR = base.SYSMGR
REVIEWERS = (REVIEWER, ADMIN, SYSMGR)
# The reviewer role alone: no Jarvis access.
REVIEWER_ONLY = "skadm-revonly@example.com"
# A role REVIEWER holds and may give a skill, and one nobody here holds.
TEAM = "Skadm Team A"
FOREIGN = "Skadm Team B"
NOTICE = skills_admin.REVIEWER_EDIT_NOTICE


class ReviewerEditBase(base.SkillAdminBase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for role in (TEAM, FOREIGN):
			if not frappe.db.exists("Role", role):
				frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
					ignore_permissions=True
				)
		ensure_user(REVIEWER, roles=("Jarvis User", "Jarvis Skill Reviewer", TEAM))

	def _kinds(self) -> dict:
		kinds = super()._kinds()
		kinds["held_role"] = self._skill(
			OWNER, "heldrole", scope="Role", enabled=0, admin_hold=1, admin_hold_reason="Paused"
		)
		kinds["learned_org"] = self._skill("Administrator", "learnedorg", scope="Org", managed_by_learning=1)
		return kinds

	def _edit(self, user, skill, **fields):
		frappe.set_user(user)
		return custom_skills_api.update_custom_skill(skill, **fields)

	def _edit_notes(self, user):
		return [n for n in self._notes(user) if n.subject.startswith(f"{NOTICE}:")]


# --------------------------------------------------------------------------- #
# Who may edit what
# --------------------------------------------------------------------------- #
class TestTheMatrix(ReviewerEditBase):
	# The kinds each actor may change the description of. Everything else is refused.
	ALLOWED = {
		OWNER: {"private", "shared_with", "held"},
		PLAIN: set(),
		**{r: {"role", "org", "published", "held_role"} for r in REVIEWERS},
	}

	def test_actor_by_kind(self):
		kinds = self._kinds()
		for actor, allowed in self.ALLOWED.items():
			for kind, skill in kinds.items():
				with self.subTest(actor=actor, kind=kind):
					text = f"edited by {actor} on {kind}"
					try:
						if kind in allowed:
							self._edit(actor, skill, description=text)
							self.assertEqual(self._row(skill, "description").description, text)
						else:
							with self.assertRaises(frappe.PermissionError):
								self._edit(actor, skill, description=text)
							self.assertNotEqual(self._row(skill, "description").description, text)
					finally:
						frappe.set_user("Administrator")
						frappe.db.rollback()

	def test_the_owner_of_a_shared_skill_is_told_to_ask_a_skill_reviewer(self):
		skill = self._skill(OWNER, "ask", scope="Org")
		with self.assertRaises(frappe.PermissionError) as ctx:
			self._edit(OWNER, skill, instructions="rewritten")
		self.assertIn("Ask a skill reviewer", str(ctx.exception))

	def test_a_plain_user_gets_the_clear_message(self):
		skill = self._skill(OWNER, "plain", scope="Org")
		with self.assertRaises(frappe.PermissionError) as ctx:
			self._edit(PLAIN, skill, description="nope")
		self.assertIn("your own skills", str(ctx.exception))


class TestWhatAReviewerMayNotChange(ReviewerEditBase):
	def test_the_name_is_refused_unless_unchanged(self):
		skill = self._skill(OWNER, "rename", scope="Org")
		with self.assertRaises(frappe.PermissionError):
			self._edit(REVIEWER, skill, skill_name=f"{TAG}-renamed")
		frappe.db.rollback()
		self.assertEqual(self._row(skill, "skill_name").skill_name, f"{TAG}-rename")
		# The form sends what it shows: the same name, however it is typed, is no change.
		self._edit(REVIEWER, skill, skill_name=f"  {TAG.upper()}-RENAME ", description="kept name")
		self.assertEqual(self._row(skill, "description").description, "kept name")

	def test_approve_and_run_is_refused_both_ways(self):
		unarmed = self._skill(OWNER, "unarmed", scope="Org")
		armed = self._skill(OWNER, "armed", scope="Org", allow_approve_run=1)
		for skill, value in ((unarmed, 1), (armed, 0)):
			for user in REVIEWERS:
				with self.subTest(skill=skill, user=user):
					with self.assertRaises(frappe.PermissionError):
						self._edit(user, skill, allow_approve_run=value)
					frappe.db.rollback()
					self.assertEqual(self._row(skill, "allow_approve_run").allow_approve_run, 1 - value)
		# The same value the form always sends is not a change.
		self._edit(REVIEWER, armed, allow_approve_run=1, description="still armed")
		self.assertEqual(self._row(armed, "allow_approve_run").allow_approve_run, 1)

	def test_no_scope_parameter_and_no_delete(self):
		params = inspect.signature(inspect.unwrap(custom_skills_api.update_custom_skill)).parameters
		self.assertNotIn("scope", params)
		self.assertNotIn("target_role", params)
		skill = self._skill(OWNER, "nodelete", scope="Org")
		frappe.set_user(REVIEWER)
		with self.assertRaises(frappe.PermissionError):
			custom_skills_api.delete_custom_skill(skill)
		out = custom_skills_api.delete_custom_skills_bulk([skill])
		self.assertEqual(out["skipped"], [{"name": skill, "reason": "not owner"}])
		self.assertTrue(frappe.db.exists(SKILL, skill))

	def test_a_held_skill_takes_text_but_stays_off(self):
		skill = self._kinds()["held_role"]
		self._edit(REVIEWER, skill, instructions="fixed text")
		self.assertEqual(self._row(skill, "instructions").instructions, "fixed text")
		with self.assertRaises(SkillOnHoldError):
			self._edit(REVIEWER, skill, enabled=1)
		frappe.db.rollback()
		row = self._row(skill, "enabled", "admin_hold")
		self.assertEqual((row.enabled, row.admin_hold), (0, 1))

	def test_refused_before_the_log_can_take_it(self):
		skill = self._skill(OWNER, "unmigrated", scope="Org")
		with patch.object(skill_reviewer_edit, "log_ready", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				self._edit(REVIEWER, skill, description="no record, no edit")
		frappe.db.rollback()
		self.assertEqual(self._row(skill, "description").description, "unmigrated description")


class TestRoles(ReviewerEditBase):
	def _roles(self, skill):
		return frappe.get_all(
			"Jarvis Custom Skill Allowed Role",
			filters={"parent": skill, "parenttype": SKILL},
			pluck="role",
			order_by="idx asc",
		)

	def test_a_reviewer_adds_a_role_they_hold_and_keeps_the_ones_there(self):
		skill = self._skill(OWNER, "roles", scope="Role")
		self._edit(REVIEWER, skill, allowed_roles=[base.AUDIENCE, TEAM])
		self.assertEqual(self._roles(skill), [base.AUDIENCE, TEAM])
		self.assertEqual(self._row(skill, "target_role").target_role, base.AUDIENCE)
		(row,) = self._log(skill)
		self.assertEqual(row.reason, "Changed: roles")

	def test_dropping_the_target_role_moves_it(self):
		skill = self._skill(OWNER, "retarget", scope="Role")
		self._edit(REVIEWER, skill, allowed_roles=f'["{TEAM}"]')  # JSON text, as a form post sends it
		self.assertEqual(self._roles(skill), [TEAM])
		self.assertEqual(self._row(skill, "target_role").target_role, TEAM)

	def test_refused_roles(self):
		role_skill = self._skill(OWNER, "badroles", scope="Role")
		org_skill = self._skill(OWNER, "orgroles", scope="Org")
		for skill, roles, error in (
			(role_skill, [base.AUDIENCE, FOREIGN], frappe.PermissionError),  # one they do not hold
			(role_skill, [], frappe.ValidationError),  # none left
			(role_skill, "not json", frappe.ValidationError),
			(role_skill, [{"role": TEAM}], frappe.ValidationError),
			(org_skill, [TEAM], frappe.ValidationError),  # a company skill has no role list
		):
			with self.subTest(skill=skill, roles=roles):
				with self.assertRaises(error):
					self._edit(REVIEWER, skill, allowed_roles=roles)
				frappe.db.rollback()
		self.assertEqual(self._roles(role_skill), [base.AUDIENCE])
		self.assertEqual(self._roles(org_skill), [])


# --------------------------------------------------------------------------- #
# The record and the notice
# --------------------------------------------------------------------------- #
class TestRecordAndNotice(ReviewerEditBase):
	def test_an_edit_is_recorded_and_the_owner_told(self):
		skill = self._skill(OWNER, "audited", scope="Org")
		self._edit(REVIEWER, skill, description="new words", instructions="new steps")
		(row,) = self._log(skill)
		self.assertEqual(
			(row.verb, row.admin, row.skill_name, row.skill_owner, row.reason),
			("edit", REVIEWER, f"{TAG}-audited", OWNER, "Changed: description, instructions"),
		)
		self.assertEqual(row.needs_apply, 1)  # a company skill: the Apply carries the new text
		(note,) = self._edit_notes(OWNER)
		self.assertEqual(note.subject, f"{NOTICE}: {TAG}-audited")
		self.assertEqual(note.email_content, f"A skill reviewer changed your skill {TAG}-audited.")
		self.assertFalse(note.document_name)
		self.assertEqual(self._edit_notes(REVIEWER), [])
		frappe.set_user(OWNER)  # the Skills list shows it to its owner
		self.assertIn(note.name, [n["name"] for n in skills_admin.owner_notices(OWNER, 5)])

	def test_switching_a_company_skill_off_waits_for_an_apply(self):
		# It leaves the catalog with the save, so the record reads membership before it.
		skill = self._skill(OWNER, "offapply", scope="Org")
		self.assertTrue(skills_admin.in_company_catalog(skill))
		self._edit(REVIEWER, skill, enabled=0)
		self.assertFalse(skills_admin.in_company_catalog(skill))
		(row,) = self._log(skill)
		self.assertEqual((row.reason, row.needs_apply), ("Changed: enabled", 1))

	def test_a_role_skill_edit_does_not_wait_for_an_apply(self):
		skill = self._skill(OWNER, "roleapply", scope="Role")
		self._edit(REVIEWER, skill, instructions="role-only text")
		(row,) = self._log(skill)
		self.assertEqual(row.needs_apply, 0)

	def test_an_edit_that_changes_nothing_writes_nothing(self):
		skill = self._skill(OWNER, "same", scope="Org")
		self._edit(REVIEWER, skill, description="same description")
		self.assertEqual(self._log(skill), [])
		self.assertEqual(self._edit_notes(OWNER), [])

	def test_the_owners_own_edit_is_not_recorded(self):
		skill = self._skill(OWNER, "ownedit")
		self._edit(OWNER, skill, description="mine to change")
		self.assertEqual(self._log(skill), [])
		self.assertEqual(self._edit_notes(OWNER), [])

	def test_a_published_copy_tells_its_author_only(self):
		kinds = self._kinds()
		self._edit(REVIEWER, kinds["published"], instructions="tidied")
		(row,) = self._log(kinds["published"])
		self.assertEqual((row.verb, row.skill_owner), ("edit", "Administrator"))
		self.assertEqual(len(self._edit_notes(OWNER)), 1)
		self.assertEqual(self._edit_notes(ADMIN), [])


# --------------------------------------------------------------------------- #
# Never from the assistant
# --------------------------------------------------------------------------- #
class TestNotFromATool(ReviewerEditBase):
	def test_refused_inside_a_tool_call(self):
		skill = self._skill(OWNER, "tool", scope="Org")
		frappe.set_user(REVIEWER)
		for fn, kwargs in (
			(custom_skills_api.update_custom_skill, {"name": skill, "description": "from chat"}),
			(custom_skills_api.share_custom_skill, {"name": skill, "users": [OTHER]}),
		):
			with self.subTest(fn=fn.__name__):
				err = base._raised_in_a_tool(fn, **kwargs)
				self.assertIsInstance(err, frappe.PermissionError)
				self.assertIn("inside a tool", str(err))
		frappe.db.rollback()
		self.assertEqual(self._row(skill, "description").description, "tool description")
		self.assertEqual(self._log(skill), [])

	def test_refused_from_a_job_a_tool_call_queued(self):
		skill = self._skill(OWNER, "guarded", scope="Org")
		frappe.set_user(REVIEWER)
		with patch("jarvis.tools._write_risk.in_guarded_call", return_value=True):
			with self.assertRaises(frappe.PermissionError):
				custom_skills_api.update_custom_skill(skill, description="queued")
		frappe.db.rollback()
		self.assertEqual(self._row(skill, "description").description, "guarded description")


# --------------------------------------------------------------------------- #
# Shares
# --------------------------------------------------------------------------- #
class TestShares(ReviewerEditBase):
	def _shared(self, skill):
		return sorted(
			frappe.get_all(
				"Jarvis Custom Skill Share", filters={"parent": skill, "parenttype": SKILL}, pluck="user"
			)
		)

	def test_a_reviewer_manages_a_shared_skills_shares(self):
		skill = self._skill(OWNER, "rshare", scope="Role")
		frappe.set_user(REVIEWER)
		self.assertEqual(custom_skills_api.get_skill_shares(skill), {"users": []})
		custom_skills_api.share_custom_skill(skill, [OTHER])
		self.assertEqual(self._shared(skill), [OTHER])
		self.assertEqual(
			custom_skills_api.get_skill_shares(skill)["users"], [{"name": OTHER, "full_name": "skadm-other"}]
		)
		(row,) = self._log(skill)
		self.assertEqual((row.verb, row.reason), ("edit", "Changed: shared with"))
		self.assertEqual(len(self._edit_notes(OWNER)), 1)

	def test_a_reviewer_without_jarvis_access_is_refused(self):
		# share_custom_skill carries no Jarvis gate of its own: the reviewer branch does.
		skill = self._skill(OWNER, "noaccess", scope="Org")
		ensure_user(REVIEWER_ONLY, roles=("Jarvis Skill Reviewer",))
		frappe.set_user(REVIEWER_ONLY)
		with self.assertRaises(frappe.PermissionError):
			custom_skills_api.share_custom_skill(skill, [OTHER])
		frappe.db.rollback()
		self.assertEqual(self._shared(skill), [])

	def test_a_private_skills_shares_stay_the_owners(self):
		kinds = self._kinds()
		for user in (*REVIEWERS, PLAIN):
			for kind in ("private", "shared_with", "learned_org"):
				with self.subTest(user=user, kind=kind):
					frappe.set_user(user)
					with self.assertRaises(frappe.PermissionError):
						custom_skills_api.get_skill_shares(kinds[kind])
					with self.assertRaises(frappe.PermissionError):
						custom_skills_api.share_custom_skill(kinds[kind], [PLAIN])
					frappe.db.rollback()
		self.assertEqual(self._shared(kinds["shared_with"]), [OTHER])
		frappe.set_user(OWNER)
		self.assertEqual(custom_skills_api.get_skill_shares(kinds["shared_with"])["users"][0]["name"], OTHER)


# --------------------------------------------------------------------------- #
# The detail payload
# --------------------------------------------------------------------------- #
class TestThePayload(ReviewerEditBase):
	def _get(self, user, skill):
		frappe.set_user(user)
		return custom_skills_api.get_custom_skill(skill)

	def test_is_owner_and_can_edit(self):
		kinds = self._kinds()
		cases = {
			(OWNER, "private"): (True, True),
			(OTHER, "shared_with"): (False, False),
			(OWNER, "role"): (True, True),
			(SYSMGR, "role"): (False, True),  # outside SYSMGR's roles: readable all the same
			(REVIEWER, "org"): (False, True),
			(ADMIN, "published"): (False, True),
			(REVIEWER, "held_role"): (False, True),
			(REVIEWER, "learned_org"): (False, False),
		}
		for (user, kind), (is_owner, can_edit) in cases.items():
			with self.subTest(user=user, kind=kind):
				out = self._get(user, kinds[kind])
				self.assertIs(out["is_owner"], is_owner)
				self.assertIs(out["can_edit"], can_edit)
				self.assertEqual(out["mine"], int(is_owner))

	def test_who_may_not_read_it(self):
		kinds = self._kinds()
		for user, kind in (
			(REVIEWER, "private"),
			(SYSMGR, "private"),
			(ADMIN, "held"),
			(PLAIN, "role"),
			(PLAIN, "org"),
		):
			with self.subTest(user=user, kind=kind):
				with self.assertRaises(frappe.PermissionError):
					self._get(user, kinds[kind])

	def test_whose_skill_and_its_roles(self):
		kinds = self._kinds()
		out = self._get(REVIEWER, kinds["published"])
		self.assertEqual(out["owner_name"], "skadm-owner")  # the author, not the system owner
		self.assertEqual(out["allowed_roles"], [base.AUDIENCE])
		self.assertEqual(out["admin_hold_reason"], "")
		self.assertEqual(self._get(OWNER, kinds["role"])["owner_name"], "")
		with self.assertRaises(frappe.DoesNotExistError):
			self._get(OWNER, "zz-no-such-skill")


class TestTheReviewTabLink(ReviewerEditBase):
	def test_an_approved_promotion_names_its_shared_copy(self):
		kinds = self._kinds()
		copies = custom_skills_api._shared_copies([kinds["private"], kinds["role"], kinds["held"]])
		self.assertEqual(copies, {kinds["private"]: kinds["published"], kinds["role"]: kinds["role"]})
		self.assertEqual(custom_skills_api._shared_copies([]), {})
