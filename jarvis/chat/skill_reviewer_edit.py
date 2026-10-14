"""A skill reviewer's edit of a shared skill they do not own (jarvis-admin-v2#720, 5c).

The reviewer set (Jarvis Skill Reviewer, Jarvis Admin, System Manager) may maintain a
Role or company skill in the app: its description, instructions, user-invocable flag,
roles, File Box settings, whether it is on, and who it is shared with. Never its name,
never "Approve & run", never its scope, never a delete, and never a private or learned
skill. Each such edit is recorded in ``Jarvis Skill Admin Log`` (verb ``edit``) and the
skill's author is told.

``custom_skills_api`` calls this from ``update_custom_skill`` and the share endpoints;
the skill controller's guards still run on the save (a held skill stays off).
"""

from __future__ import annotations

import frappe
from frappe import _

from jarvis.chat import skills_admin
from jarvis.jarvis.doctype.jarvis_skill_admin_log import jarvis_skill_admin_log as admin_log

OWNER = "owner"
REVIEWER = "reviewer"
EDIT_VERB = "edit"

# What an edit record names, in the words the form uses.
_LABELS = {
	"description": "description",
	"instructions": "instructions",
	"user_invocable": "user invocable",
	"enabled": "enabled",
	"use_in_file_box": "use in File Box",
	"file_box_creates": "File Box creates",
	"allowed_roles": "roles",
	"shared_with": "shared with",
}


def may_edit(row, user: str) -> bool:
	"""Whether ``user`` may edit ``row`` as a reviewer: a Role or company skill that the
	learning board does not manage, and ``user`` is in the reviewer set."""
	from jarvis.permissions import is_skill_reviewer

	return skills_admin.is_shared(row) and not skills_admin.is_learned(row) and is_skill_reviewer(user)


def require_editor(doc, action: str = "edit") -> str:
	"""``OWNER`` for the skill's owner, ``REVIEWER`` for a reviewer on a shared skill
	they do not own; anyone else is refused. The reviewer branch is never taken from
	inside a tool call or a job one queued: the assistant does not rewrite what a
	shared audience follows."""
	me = frappe.session.user
	if (doc.get("owner") or "") == me:
		return OWNER
	from jarvis.permissions import is_skill_reviewer, refuse_in_tool_dispatch, require_jarvis_access
	from jarvis.tools import _write_risk

	if not is_skill_reviewer(me):
		frappe.throw(_("You can only {0} your own skills.").format(action), frappe.PermissionError)
	# The reviewer set who also have Jarvis access (D8); share_custom_skill has no gate of its own.
	require_jarvis_access()
	refuse_in_tool_dispatch()
	if _write_risk.in_guarded_call():
		frappe.throw(
			_("A skill reviewer changes a shared skill from its own page, not from chat."),
			frappe.PermissionError,
		)
	if skills_admin.is_learned(doc):
		frappe.throw(_("Learned skills are managed on the learning board, not here."), frappe.PermissionError)
	if not skills_admin.is_shared(doc):
		frappe.throw(_("Only its owner can change a private skill."), frappe.PermissionError)
	if not log_ready():
		frappe.throw(_("Editing shared skills is not available yet on this site."), frappe.ValidationError)
	return REVIEWER


def refuse_fields(doc, *, skill_name=None, allow_approve_run=None) -> None:
	"""A reviewer may not rename the skill or change "Approve & run". The form always
	sends what it shows, so only a change is refused, not a value equal to the stored one."""
	if skill_name is not None and (skill_name or "").strip().lower() != (doc.skill_name or ""):
		frappe.throw(_("A skill reviewer cannot rename someone else's skill."), frappe.PermissionError)
	if allow_approve_run is not None and bool(frappe.utils.cint(allow_approve_run)) != bool(
		frappe.utils.cint(doc.get("allow_approve_run"))
	):
		frappe.throw(
			_("A skill reviewer cannot change Approve & run on someone else's skill."),
			frappe.PermissionError,
		)


def set_allowed_roles(doc, roles) -> None:
	"""Replace a Role skill's roles. A role added must be one the caller may target, as
	a promotion request's are (``promotable_target_roles``); a role it already has may
	stay, and dropping one is always allowed. The target role follows the set, or the
	controller would reset the set to the target role alone."""
	from jarvis.chat.custom_skills_api import promotable_target_roles

	roles = _role_list(roles)
	current = [r.role for r in doc.get("allowed_roles") or [] if r.role]
	if roles == current:
		return
	if skills_admin.scope_of(doc) != "Role":
		frappe.throw(_("Only a skill shared with roles has a list of roles."), frappe.ValidationError)
	if not roles:
		frappe.throw(_("Keep at least one role."), frappe.ValidationError)
	kept = set(current) | {doc.get("target_role") or ""}
	targetable = set(promotable_target_roles().get("roles") or [])
	bad = [r for r in roles if r not in kept and r not in targetable]
	if bad:
		frappe.throw(
			_("You cannot share this skill with these roles: {0}.").format(", ".join(bad)),
			frappe.PermissionError,
		)
	if doc.get("target_role") not in roles:
		doc.target_role = roles[0]
	doc.set("allowed_roles", [{"role": r} for r in roles])


def snapshot(doc) -> dict:
	"""The values an edit record compares, before and after the save, and whether the
	skill is in the company catalog as stored (read before the save)."""
	values = {f: doc.get(f) for f in _LABELS if f not in ("allowed_roles", "shared_with")}
	values["allowed_roles"] = sorted(r.role for r in doc.get("allowed_roles") or [] if r.role)
	values["shared_with"] = sorted(r.user for r in doc.get("shared_with") or [] if r.user)
	values["in_catalog"] = skills_admin.in_company_catalog(doc.name)
	return values


def record(doc, before: dict) -> list[str]:
	"""Write the edit record for what changed since ``before`` (nothing when nothing
	did), in the caller's transaction: the edit and its record land together. It waits
	for an Apply when the skill was in the company catalog or is now: switching one off
	takes it out, and that is what most needs the Apply."""
	after = snapshot(doc)
	changed = [_LABELS[f] for f in _LABELS if _norm(before.get(f)) != _norm(after.get(f))]
	if not changed:
		return []
	admin_log.write(
		verb=EDIT_VERB,
		skill=doc.name,
		skill_name=doc.skill_name,
		skill_owner=doc.owner,
		reason=_("Changed: {0}").format(", ".join(changed)),
		needs_apply=bool(before.get("in_catalog") or after["in_catalog"]),
	)
	return changed


def tell_author(doc) -> None:
	"""Tell the skill's author: its owner, or for a published copy the owner of the
	private skill it came from (nobody once that is deleted)."""
	author = skills_admin.lineage_author(doc)
	skills_admin.notify(
		doc,
		skills_admin.REVIEWER_EDIT_NOTICE,
		_("A skill reviewer changed your skill {0}.").format(doc.skill_name),
		users={author} if author else set(),
	)


def log_ready() -> bool:
	"""Whether this site's admin log takes an ``edit`` row (the migrate ran): a reviewer
	edit is never made without its record."""
	if not skills_admin.admin_log_exists():
		return False
	field = frappe.get_meta(admin_log.DT).get_field("verb")
	return bool(field) and EDIT_VERB in (field.options or "").split("\n")


def _role_list(roles) -> list[str]:
	"""An ordered, de-duplicated list of role names from a list or its JSON text."""
	raw = roles
	if isinstance(raw, str):
		try:
			raw = frappe.parse_json(raw) if raw.strip() else []
		except ValueError:
			raw = None
	if not isinstance(raw, list) or not all(isinstance(r, str) for r in raw):
		frappe.throw(_("Roles must be a list of role names."), frappe.ValidationError)
	out: list[str] = []
	for r in raw:
		r = r.strip()
		if r and r not in out:
			out.append(r)
	return out


def _norm(value):
	if isinstance(value, list):
		return value
	if isinstance(value, int | bool):
		return int(value)
	return value or ""
