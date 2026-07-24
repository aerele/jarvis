"""Jarvis Skill Promotion Request DocType controller (security review PART 2,
TASK 10).

One row per "promote my private (User) or role skill up the scope ladder" ask,
the Custom-Skill analogue of ``Jarvis Wiki Promotion Request``. Created by
``jarvis.chat.custom_skills_api.request_skill_promotion`` when a skill owner
asks to widen a skill they own; decided by a skill reviewer
(``custom_skills_api.decide_skill_promotion``), who approves — PUBLISHING a new
system-owned Role/Org skill from the request's immutable content snapshot,
leaving the requester's private skill intact — or rejects. The ``All`` role only
ever gets read/create-free here (create dropped, so the ownership-checked
endpoint is the sole creation path); a requester can never self-approve or edit a
decision after the fact.

The request SNAPSHOTS the full content at request time (``instructions_snapshot``
/ ``description_snapshot`` / ``user_invocable_snapshot``, mirroring the wiki
request's ``body_snapshot``): the reviewer decides on exactly this, and approval
promotes exactly this, so a post-request edit — or content hidden past a truncated
preview — can never change what the Role/Org audience runs (CDX-SP-1).

Promotion only ever WIDENS visibility: ``to_scope`` is Role/Org (never back to
User, never sideways). ``from_scope`` is a point-in-time snapshot of the source
skill's scope, re-validated at decision time by the reviewer endpoint.
"""

import frappe
from frappe import _
from frappe.model.document import Document

SKILL = "Jarvis Custom Skill"
FROM_SCOPES = ("User", "Role", "Org")
TO_SCOPES = ("Role", "Org")


class JarvisSkillPromotionRequest(Document):
	def before_insert(self):
		# The requester must be able to READ the source skill before a request is
		# filed against it — closes the same generic-REST disclosure class as the
		# Wiki promotion request (TASK 14). Runs for EVERY insert path; the skill
		# ORM hook resolves User-scope skills to their owner, so this subsumes an
		# explicit owner check. Administrator bypasses has_permission natively.
		if self.skill and not frappe.has_permission(SKILL, "read", self.skill):
			frappe.throw(_("You do not have access to this skill."), frappe.PermissionError)
		if self.skill and not self.skill_name:
			self.skill_name = frappe.db.get_value(SKILL, self.skill, "skill_name") or ""

	def validate(self):
		self._validate_skill()
		self._validate_scopes()
		self._validate_snapshot_bound()

	def _validate_skill(self):
		if not self.skill:
			frappe.throw(_("A promotion request must reference a skill."))

	def _validate_snapshot_bound(self):
		# R2-SP-1: every NEW request MUST carry a full content snapshot — approval
		# publishes EXACTLY this snapshot (never live source content), so a null /
		# partial snapshot is a request approval can never safely bind to. Enforced
		# only on insert (is_new): a legacy pre-snapshot row already in the DB is
		# never re-validated here, and its approval is refused separately by
		# ``custom_skills_api._materialize_promotion`` with a resubmit message. The
		# request endpoint fills these from the requester's own source at request
		# time, so a well-formed request always passes.
		if not self.is_new():
			return
		if self.instructions_snapshot is None or self.description_snapshot is None:
			frappe.throw(
				_(
					"A promotion request must snapshot the skill's content at request time. "
					"Please refile the request."
				)
			)
		if self.user_invocable_snapshot is None:
			self.user_invocable_snapshot = 0

	def _validate_scopes(self):
		self.from_scope = (self.from_scope or "").strip()
		if self.from_scope and self.from_scope not in FROM_SCOPES:
			frappe.throw(_("From Scope must be one of {0}.").format(", ".join(FROM_SCOPES)))
		self.to_scope = (self.to_scope or "").strip()
		if self.to_scope not in TO_SCOPES:
			frappe.throw(_("To Scope must be one of {0}.").format(", ".join(TO_SCOPES)))
		if self.to_scope == "Role" and not self.target_role:
			frappe.throw(_("Promoting to Role scope needs a Target Role."))
