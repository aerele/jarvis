"""A Jarvis Admin's view of every user's skills: list them, open one read-only, put
one on hold or release it, delete it (jarvis-admin-v2#720, piece 5a), hand it to
another owner (piece 5b).

The owner's endpoints (``jarvis.chat.custom_skills_api``) stay owner-scoped, and
nothing here changes who may save a skill: an admin still cannot edit another
user's private skill. These endpoints act with the role gate instead of the
document permission; their writes are the two hold fields (switching the skill off
and disarming it with them), a release, a delete and a hand-over. The acts themselves
are in ``jarvis.chat.skills_admin`` and ``jarvis.chat.skills_handover``.

Every endpoint is POST-only, carries ``@require_jarvis_user`` and starts with
``refuse_in_tool_dispatch()`` and ``require_jarvis_admin()``, as the macro admin
does: an admin's own assistant must never reach these, or one tool call would put
every user's private skill text in the model's context. The module is on
``run_method``'s denied prefixes for the same reason. The write endpoints also
refuse a job queued inside a tool call (``_write_risk.in_guarded_call()``).

Arguments are checked HERE, by hand (``from __future__ import annotations`` makes
every hint a string, and Frappe's request-time type check skips string hints). Five
helpers are COPIED from ``macros_admin_api`` rather than shared, so a macro hotfix
backport does not drag this module along: ``_name_arg``, ``_text_arg``,
``_filters_arg``, ``_full_names`` and ``_reason_arg``. A behavioural parity test
(``test_skill_admin_api.TestHelperParity``) keeps the copies honest.

Privacy: a Jarvis Admin reads every user's private skill text here (read only).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder.functions import Coalesce, Count

from jarvis.chat import list_filters, skills_admin, skills_handover
from jarvis.chat.skills_admin import HOLD_FIELD, HOLD_REASON_FIELD, SKILL
from jarvis.permissions import refuse_in_tool_dispatch, require_jarvis_admin, require_jarvis_user

SHARE = "Jarvis Custom Skill Share"
ALLOWED_ROLE = "Jarvis Custom Skill Allowed Role"

PAGE_SIZE = 50
PAGE_SIZE_MAX = 100
OWNERS_MAX = 200
SEARCH_MAX = 140
HISTORY_MAX = 50
HOLD_REASON_MAX = 500

_SORTABLE = ("skill_name", "owner", "modified", "scope")
_FILTERS = {"owner", "scope", "enabled", "armed", "on_hold"}
_SCOPES = ("User", "Role", "Org")


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_list_skills(
	filters: str | dict | None = None,
	search: str = "",
	sort: str = "",
	page: int = 1,
	page_size: int = PAGE_SIZE,
):
	"""Every non-learned skill, private and shared, published company copies labelled.
	No instructions: those are read one skill at a time (``admin_get_skill``).

	``filters``: ``owner`` (a user), ``scope`` (User / Role / Org), ``enabled``,
	``armed``, ``on_hold`` (0 or 1). ``search`` matches the name, the description, the
	owner's address and full name. ``sort``: ``"<field>"`` or ``"<field> desc"``, a
	field of ``_SORTABLE``. ``page`` counts from 1."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	search = _text_arg(search, _("Search"))[:SEARCH_MAX]
	order_field, order_desc = _sort_arg(sort)
	page, page_size = _page_args(page, page_size)
	f = list_filters.load_legacy_filters(_filters_arg(filters), _FILTERS)
	held_column = skills_admin.hold_fields_exist()

	s = frappe.qb.DocType(SKILL)
	where = _list_where(s, f, search, held_column)
	total = list_filters.bounded_sql(*frappe.qb.from_(s).select(Count("*")).where(where).walk())[0][0]
	columns = [
		s.name,
		s.skill_name,
		s.description,
		s.owner,
		s.scope,
		s.target_role,
		s.enabled,
		s.allow_approve_run,
		s.modified,
		s.source_skill,
	]
	if held_column:
		columns += [s.admin_hold, s.admin_hold_reason]
	order = frappe.qb.desc if order_desc else frappe.qb.asc
	query = (
		frappe.qb.from_(s)
		.select(*columns)
		.where(where)
		.orderby(getattr(s, order_field), order=order)
		.orderby(s.name)
		.limit(page_size)
		.offset((page - 1) * page_size)
	)
	rows = list_filters.bounded_sql(*query.walk(), as_dict=True)
	status = _sync_status()
	return {
		"rows": _list_rows(rows),
		"page": page,
		"page_size": page_size,
		"has_more": (page - 1) * page_size + len(rows) < total,
		"total": total,
		"hold_available": held_column,
		"delete_available": skills_admin.admin_log_exists(),
		"handover_available": skills_handover.available(),
		"sync": status,
	}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_skill_owners(search: str = "") -> dict:
	"""The owner filter's choices: who owns a non-learned skill, at most ``OWNERS_MAX``
	(``more`` says there are others, reached through ``search``)."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	search = _text_arg(search, _("Search"))[:SEARCH_MAX]
	s = frappe.qb.DocType(SKILL)
	where = Coalesce(s.managed_by_learning, 0) == 0
	if search:
		where &= _owner_matches(s, f"%{list_filters.escape_like(search)}%")
	query = frappe.qb.from_(s).select(s.owner).distinct().where(where).orderby(s.owner).limit(OWNERS_MAX + 1)
	users = [u for u in query.run(pluck=True) if u]
	more = len(users) > OWNERS_MAX
	users = users[:OWNERS_MAX]
	names = _full_names(users)
	owners = [{"user": u, "full_name": names.get(u) or u} for u in users]
	owners.sort(key=lambda o: (o["full_name"].lower(), o["user"]))
	return {"owners": owners, "more": more}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_get_skill(name: str) -> dict:
	"""One skill of any user, to look at: the list row's fields plus its instructions,
	who it is shared with, its allowed roles, its lineage, the admin history and how
	many macros use it. Nothing to save it with."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	name = _name_arg(name, _("Skill"))
	fields = [*skills_admin.ROW_FIELDS, "description", "instructions", "modified"]
	if skills_admin.hold_fields_exist():
		fields += [HOLD_FIELD, HOLD_REASON_FIELD]
	# No document permission: reading other users' skills is what the role gate grants.
	row = frappe.db.get_value(SKILL, {"name": name}, fields, as_dict=True)
	if not row:
		frappe.throw(_("This skill was deleted."), frappe.DoesNotExistError)
	out = _list_rows([row])[0]
	shares = _children(SHARE, row.name, "user")
	names = _full_names(shares)
	out.update(
		{
			"instructions": row.instructions or "",
			"shared_with": [{"user": u, "full_name": names.get(u) or u} for u in shares],
			"allowed_roles": _children(ALLOWED_ROLE, row.name, "role"),
			"lineage": _lineage(row),
			"history": _history(row.name),
			"used_by_macros": skills_admin.used_by_macros(row.name),
		}
	)
	return out


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_hold(skill: str, reason: str = "") -> dict:
	"""Put another user's skill on hold with a reason its owner reads. It is switched
	off and disarmed, is not served to any assistant, and cannot be promoted, switched
	back on or armed until an admin releases it. ``{ok, needs_apply, used_by_macros}``."""
	_write_gate()
	name = _name_arg(skill, _("Skill"))
	reason = _reason_arg(reason)
	_refuse_without_the_hold_fields()
	return skills_admin.hold(skills_admin.load(name), reason)


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_release(skill: str, restore: bool | int | str = False) -> dict:
	"""Lift an admin's hold. ``restore`` (a Role or company skill only) switches it
	back on; arming is never restored. ``{ok, restored, needs_apply}``."""
	_write_gate()
	name = _name_arg(skill, _("Skill"))
	restore = _bool_arg(restore, _("Restore"))
	_refuse_without_the_hold_fields()
	return skills_admin.release(skills_admin.load(name), restore)


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_delete(skill: str) -> dict:
	"""Delete any user's skill (not a learned one). Its Pending promotion request is
	rejected; decided requests stay as history. ``{ok, needs_apply, used_by_macros}``."""
	_write_gate()
	name = _name_arg(skill, _("Skill"))
	if not skills_admin.admin_log_exists():
		# The audit row must outlive the skill; without its table there is no record.
		frappe.throw(_("Deleting another user's skill is not available yet on this site."))
	return skills_admin.delete(skills_admin.load(name))


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_handover(
	skill: str,
	new_owner: str | None = None,
	expected_owner: str | None = None,
	new_owners: str | list | None = None,
	keep: bool | int | str = False,
) -> dict:
	"""Hand any user's skill to ``new_owner`` (``skills_handover``): it arrives switched
	off and disarmed, with no shares, no File Box opt-in, no assignments and no hold,
	and the old owner's Pending promotion requests are rejected. ``expected_owner`` is the
	owner the dialog showed (required). An admin may hand a skill to themselves and may
	hand their own skill away.

	Several people (``new_owners``, a list or a comma separated text, with or without
	``new_owner``): the first is handed the skill, each of the others gets a private
	copy (``skills_handover.give_copies``). With ``keep`` the skill stays where it is
	and everyone named gets a copy. Every person is checked before anything is written.
	``{ok, handed_over, new_owner, cleared: {shares, armed, file_box, assignments},
	hold_lifted, requests_cancelled, needs_apply, copies: [{user, skill}],
	copy_failed: [{user, reason}]}`` (the hand-over's keys only when it was handed)."""
	_write_gate()
	name = _name_arg(skill, _("Skill"))
	targets = _new_owners_arg(new_owner, new_owners)
	keep = _bool_arg(keep, _("Keep"))
	# As stored, as the new owner is: a name in another case is the same person.
	expected_owner = _name_arg(expected_owner, _("Current owner"))
	expected_owner = skills_handover.stored_user(expected_owner) or expected_owner
	if not skills_handover.available():
		frappe.throw(_("Handing over a skill is not available yet on this site."), frappe.ValidationError)
	row = skills_admin.load(name)
	if len(targets) == 1 and not keep:
		res = skills_handover.handover(row, targets[0], expected_owner)
		return {**res, "handed_over": True, "copies": [], "copy_failed": []}
	skills_handover.refuse_all(row, targets, expected_owner, keep=keep)
	res = {"ok": True, "handed_over": False, "new_owner": None, "needs_apply": False}
	moved_to = None
	if not keep:
		moved_to = targets[0]
		res = {**skills_handover.handover(row, moved_to, expected_owner), "handed_over": True}
	copies, failed = skills_handover.give_copies(row.name, targets[1:] if moved_to else targets, row.owner)
	skills_handover.tell_about_copies(row, row.owner, [c["user"] for c in copies], moved_to=moved_to)
	return {**res, "copies": copies, "copy_failed": failed}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def admin_handover_targets(search: str = "", skill: str | None = None) -> dict:
	"""Who a skill can be handed to, for the dialog's search: enabled desk users with
	the Jarvis User role, not Administrator or Guest, nor the current owner of ``skill``
	(optional), matching ``search`` on their address or full name, at most
	``skills_handover.TARGETS_MAX``. ``{targets: [{user, full_name, at_cap}], more, cap}``:
	``cap`` is the per-owner cap ``at_cap`` is measured against.
	The hand-over checks everything again."""
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	search = _text_arg(search, _("Search"))[:SEARCH_MAX]
	owner = None
	if skill is not None and skill != "":
		owner = skills_admin.load(_name_arg(skill, _("Skill"))).owner
	return skills_handover.targets(search, exclude=owner)


def _write_gate() -> None:
	refuse_in_tool_dispatch()
	require_jarvis_admin()
	from jarvis.tools import _write_risk

	if _write_risk.in_guarded_call():
		frappe.throw(_("Not permitted inside a tool call"), frappe.PermissionError)


def _refuse_without_the_hold_fields() -> None:
	if not skills_admin.hold_fields_exist():
		frappe.throw(_("Holding is not available yet on this site."), frappe.ValidationError)


# --------------------------------------------------------------------------- #
# Row shaping
# --------------------------------------------------------------------------- #
def _list_rows(rows: list) -> list[dict]:
	"""The list's row shape, with one query each for share counts, full names and the
	lineage authors of published copies."""
	me = frappe.session.user
	shares = _share_counts([r.name for r in rows])
	authors = _owners_of([r.source_skill for r in rows if skills_admin.is_published(r)])
	names = _full_names(r.owner for r in rows)
	out = []
	for r in rows:
		published = skills_admin.is_published(r)
		author = authors.get(r.source_skill) if published else r.owner
		held = frappe.utils.cint(r.get(HOLD_FIELD))
		out.append(
			{
				"name": r.name,
				"skill_name": r.skill_name,
				"description": r.description or "",
				"owner": r.owner,
				"owner_name": names.get(r.owner) or r.owner,
				"scope": skills_admin.scope_of(r),
				"target_role": r.target_role or "",
				"enabled": frappe.utils.cint(r.enabled),
				"allow_approve_run": frappe.utils.cint(r.allow_approve_run),
				"admin_hold": held,
				"admin_hold_reason": (r.get(HOLD_REASON_FIELD) or "") if held else "",
				"shared_with_count": shares.get(r.name, 0),
				"modified": str(r.modified or ""),
				"is_mine": int(bool(author) and author == me),
				"is_published": int(published),
				"is_learned": int(skills_admin.is_learned(r)),
			}
		)
	return out


def _list_where(s, f: dict, search: str, held_column: bool):
	where = Coalesce(s.managed_by_learning, 0) == 0
	if search:
		q = f"%{list_filters.escape_like(search)}%"
		where &= s.skill_name.like(q) | s.description.like(q) | _owner_matches(s, q)
	if "owner" in f:
		if not isinstance(f["owner"], str):
			frappe.throw(_("Invalid owner filter."))
		where &= s.owner == f["owner"]
	if "scope" in f:
		where &= _scope_is(s, f["scope"])
	if "enabled" in f:
		where &= s.enabled == list_filters.bool01(f["enabled"])
	if "armed" in f:
		where &= s.allow_approve_run == list_filters.bool01(f["armed"])
	if "on_hold" in f:
		on_hold = list_filters.bool01(f["on_hold"])
		if held_column:
			where &= Coalesce(s.admin_hold, 0) == on_hold
		elif on_hold:
			where &= s.name.isnull()  # before the migrate nothing is held
	return where


def _owner_matches(s, q: str):
	"""The owner's address or full name, in one statement (a subquery on the user table)."""
	u = frappe.qb.DocType("User")
	return s.owner.like(q) | s.owner.isin(frappe.qb.from_(u).select(u.name).where(u.full_name.like(q)))


def _scope_is(s, scope):
	"""A stored scope as the ladder reads it: blank is Org, "Personal" is User."""
	if scope not in _SCOPES:
		frappe.throw(_("Invalid audience filter."))
	stored = Coalesce(s.scope, "")
	if scope == "Org":
		return stored.isin(("Org", ""))
	if scope == "User":
		return stored.isin(("User", "Personal"))
	return stored == "Role"


def _sync_status() -> dict:
	from jarvis.chat.custom_skills_api import _custom_sync_status

	status = _custom_sync_status()
	return {
		"pending": bool(status.get("pending")),
		"needs_apply": skills_admin.catalog_update_pending(),
		"last_synced_at": status.get("last_sync_at") or "",
	}


def _share_counts(names: list[str]) -> dict:
	if not names:
		return {}
	c = frappe.qb.DocType(SHARE)
	rows = (
		frappe.qb.from_(c)
		.select(c.parent, Count("*").as_("n"))
		.where((c.parenttype == SKILL) & c.parent.isin(names))
		.groupby(c.parent)
		.run(as_dict=True)
	)
	return {r.parent: r.n for r in rows}


def _owners_of(skills: list[str]) -> dict:
	"""``{skill: its owner}`` for these skill names, one query."""
	skills = sorted({n for n in skills if n})
	if not skills:
		return {}
	s = frappe.qb.DocType(SKILL)
	rows = frappe.qb.from_(s).select(s.name, s.owner).where(s.name.isin(skills)).run(as_dict=True)
	return {r.name: r.owner for r in rows}


def _children(child: str, parent: str, field: str) -> list[str]:
	c = frappe.qb.DocType(child)
	return (
		frappe.qb.from_(c)
		.select(getattr(c, field))
		.where((c.parenttype == SKILL) & (c.parent == parent))
		.orderby(c.idx)
		.run(pluck=True)
	)


def _lineage(row) -> dict | None:
	"""Where a promoted copy came from: its private source and that source's author
	(``None`` once the source is deleted, or for a row that is nobody's copy)."""
	if not row.get("source_skill"):
		return None
	author = _owners_of([row.source_skill]).get(row.source_skill)
	return {
		"source_skill": row.source_skill,
		"author": author,
		"author_name": (_full_names([author]).get(author) or author) if author else None,
	}


def _history(skill: str) -> list[dict]:
	"""The admin acts on this skill, newest first, from the audit log. ``new_owner`` and
	``new_owner_name`` are a hand-over's ("" for the other acts, and before the
	hand-over's migrate)."""
	if not skills_admin.admin_log_exists():
		return []
	log = frappe.qb.DocType("Jarvis Skill Admin Log")
	columns = [log.admin, log.verb, log.reason, log.acted_at]
	if skills_handover.available():
		columns.append(log.new_owner)
	rows = (
		frappe.qb.from_(log)
		.select(*columns)
		.where(log.skill == skill)
		.orderby(log.acted_at, order=frappe.qb.desc)
		.orderby(log.creation, order=frappe.qb.desc)
		.limit(HISTORY_MAX)
		.run(as_dict=True)
	)
	names = _full_names([*(r.admin for r in rows), *(r.get("new_owner") for r in rows)])
	return [
		{
			"admin": r.admin,
			"admin_name": names.get(r.admin) or r.admin,
			"verb": r.verb,
			"reason": r.reason or "",
			"new_owner": r.get("new_owner") or "",
			"new_owner_name": (names.get(r.get("new_owner")) or r.get("new_owner") or ""),
			"at": str(r.acted_at or ""),
		}
		for r in rows
	]


# --------------------------------------------------------------------------- #
# Arguments (the five copies from macros_admin_api first, then this module's own)
# --------------------------------------------------------------------------- #
def _name_arg(value, what: str) -> str:
	"""``value`` as a document name, or a refusal. A dict would reach the database
	layer as a FILTER and match some other row."""
	if not isinstance(value, str) or not value.strip():
		frappe.throw(_("{0} must be a name.").format(what), frappe.ValidationError)
	return value.strip()


def _text_arg(value, what: str) -> str:
	"""``value`` as optional text ("" when absent), or a refusal. A list or a dict is
	refused, not ignored: ignoring a search would answer with the unfiltered list."""
	if value is None:
		return ""
	if not isinstance(value, str):
		frappe.throw(_("{0} must be text.").format(what), frappe.ValidationError)
	return value.strip()


def _filters_arg(filters) -> dict:
	"""``filters`` as a dict, from a dict or a JSON object; nothing sent is ``{}``.
	Anything else is refused: a filter that silently does nothing answers with every
	user's skills."""
	if filters is None or (isinstance(filters, str) and not filters.strip()):
		return {}
	if isinstance(filters, str):
		try:
			filters = frappe.parse_json(filters)
		except Exception:
			frappe.throw(_("Filters could not be read."), frappe.ValidationError)
	if not isinstance(filters, dict):
		frappe.throw(_("Filters must be an object of filter names and values."), frappe.ValidationError)
	return filters


def _full_names(users) -> dict:
	"""``{user: full name}``, one query. ``frappe.qb`` skips permissions on purpose:
	every caller is behind ``require_jarvis_admin``, and a Jarvis Admin who is not a
	System Manager may not read User rows through ``get_list``."""
	users = sorted({u for u in users if u})
	if not users:
		return {}
	u = frappe.qb.DocType("User")
	rows = frappe.qb.from_(u).select(u.name, u.full_name).where(u.name.isin(users)).run(as_dict=True)
	return {r.name: r.full_name or r.name for r in rows}


def _reason_arg(value) -> str:
	"""The hold's reason: text, not empty, at most ``HOLD_REASON_MAX`` characters. The
	owner reads it, so it is asked for, and refused rather than cut."""
	reason = _text_arg(value, _("Reason"))
	if not reason:
		frappe.throw(_("Say why the skill is put on hold: its owner reads it."), frappe.ValidationError)
	if len(reason) > HOLD_REASON_MAX:
		frappe.throw(
			_("The reason must be at most {0} characters.").format(HOLD_REASON_MAX), frappe.ValidationError
		)
	return reason


def _new_owner_arg(value) -> str:
	"""The new owner as ``User.name`` exactly as stored (``skills_handover.stored_user``),
	or a refusal. Modelled on ``macros_admin_api._new_owner_arg``."""
	name = _name_arg(value, _("New owner"))
	stored = skills_handover.stored_user(name)
	if not stored:
		frappe.throw(
			_("There is no user {0}.").format(frappe.utils.escape_html(name)),
			skills_handover.SkillHandoverError,
		)
	return stored


# How many people one hand-over can name (the skill to the first, copies to the rest).
HANDOVER_MAX = 10


def _new_owners_arg(new_owner, new_owners) -> list[str]:
	"""Who the skill goes to, in the order picked, each as stored and named once:
	``new_owner`` first when given, then ``new_owners`` (a list, a JSON list or a comma
	separated text). At least one and at most ``HANDOVER_MAX``. Modelled on
	``macros_admin_api._new_owners_arg``."""
	listed = new_owners
	if isinstance(listed, str):
		text = listed.strip()
		listed = (
			frappe.parse_json(text) if text.startswith("[") else [p for p in text.split(",") if p.strip()]
		)
	if listed is None:
		listed = []
	if not isinstance(listed, list):
		frappe.throw(_("New owners must be a list of users."), frappe.ValidationError)
	names = ([new_owner] if new_owner not in (None, "") else []) + listed
	if not names:
		frappe.throw(_("Pick who gets the skill."), skills_handover.SkillHandoverError)
	too_many = _("A skill can go to at most {0} people at once.").format(HANDOVER_MAX)
	if len(names) > HANDOVER_MAX * 2:  # before a database read per name
		frappe.throw(too_many, skills_handover.SkillHandoverError)
	targets = []
	for name in names:
		stored = _new_owner_arg(name)
		if stored not in targets:
			targets.append(stored)
	if len(targets) > HANDOVER_MAX:
		frappe.throw(too_many, skills_handover.SkillHandoverError)
	return targets


def _bool_arg(value, what: str) -> bool:
	"""A checkbox as JSON or form data sends it: a bool, 0 / 1, or their text."""
	if isinstance(value, bool):
		return value
	if isinstance(value, int) and value in (0, 1):
		return bool(value)
	if isinstance(value, str) and value.strip().lower() in ("0", "1", "true", "false", ""):
		return value.strip().lower() in ("1", "true")
	if value is None:
		return False
	frappe.throw(_("{0} must be true or false.").format(what), frappe.ValidationError)


def _sort_arg(value) -> tuple[str, bool]:
	"""``"<field>"`` or ``"<field> asc|desc"``; nothing is the name, ascending."""
	text = _text_arg(value, _("Sort"))
	if not text:
		return "skill_name", False
	parts = text.split()
	field = parts[0]
	direction = parts[1].lower() if len(parts) == 2 else "asc"
	if field not in _SORTABLE or len(parts) > 2 or direction not in ("asc", "desc"):
		frappe.throw(_("Sort must be one of {0}, then asc or desc.").format(", ".join(_SORTABLE)))
	return field, direction == "desc"


def _page_args(page, page_size) -> tuple[int, int]:
	try:
		page = max(1, int(page or 1))
		page_size = int(page_size or PAGE_SIZE)
	except (TypeError, ValueError):
		frappe.throw(_("Page must be a number."), frappe.ValidationError)
	return page, max(1, min(page_size, PAGE_SIZE_MAX))
