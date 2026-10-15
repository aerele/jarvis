"""Jarvis Skill Admin Log: what a Jarvis Admin did to someone else's skill.

One row per act (hold, release, delete, handover), written by
``jarvis.chat.skills_admin_api`` with ``ignore_permissions`` and never changed
afterwards. A skill reviewer's edit of a shared skill they do not own is an act too
(``edit``, ``jarvis.chat.skill_reviewer_edit``). Nobody has a write or a delete row on
the doctype, so an admin cannot edit the record of their own act; this controller
refuses a change to a stored row even where permissions are ignored. It outlives the
skill (no Link to it) and holds no instructions text.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from jarvis.permissions import NotRenamable

DT = "Jarvis Skill Admin Log"
# The hand-over's column (5b). Code served ahead of its migrate must not write a
# hand-over: the verb is not yet a Select option there and the column is missing.
NEW_OWNER_FIELD = "new_owner"


class JarvisSkillAdminLog(NotRenamable, Document):
	def validate(self) -> None:
		if not self.is_new():
			frappe.throw(_("A skill admin log row cannot be changed."), frappe.PermissionError)


def write(
	*,
	verb: str,
	skill: str,
	skill_name: str,
	skill_owner: str,
	reason: str = "",
	needs_apply: bool = False,
	new_owner: str = "",
) -> None:
	"""Insert one row for the session user's act. No commit: the act's own
	transaction carries it, so the act and its record land together or not at all.
	``new_owner`` is a hand-over's; ``skill_owner`` is then the owner it was taken from."""
	frappe.get_doc(
		{
			"doctype": DT,
			"verb": verb,
			"admin": frappe.session.user,
			"acted_at": frappe.utils.now_datetime(),
			"skill": skill,
			"skill_name": skill_name or "",
			"skill_owner": skill_owner or "",
			"new_owner": new_owner or None,
			"needs_apply": int(bool(needs_apply)),
			"reason": reason or None,
		}
	).insert(ignore_permissions=True)


def handover_available() -> bool:
	"""Whether this site's log has the hand-over's column (its migrate ran)."""
	return bool(frappe.db.table_exists(DT) and frappe.get_meta(DT).has_field(NEW_OWNER_FIELD))
