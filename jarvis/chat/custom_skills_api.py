"""SPA-facing CRUD + apply for customer-authored custom skills.

The SPA (``/jarvis`` Skills settings tab) calls these whitelisted methods to
manage ``Jarvis Custom Skill`` rows (owner-scoped) and to APPLY them to the
running container. Apply is explicit (a button), not on every save: each apply
restarts the container (the only way openclaw re-scans ``workspace/skills``),
so reconciling all skills in one push avoids a restart storm.

The apply path mirrors ``JarvisSettings.on_update`` / ``_enqueued_sync_via_admin``
(jarvis/jarvis/doctype/jarvis_settings/jarvis_settings.py): mark a pending status
synchronously, enqueue a deduped redis-locked worker that calls the admin app,
and flip the status to a terminal ``ok ...`` / ``failed: ...`` the SPA polls.
"""

import frappe
from frappe import _

from jarvis.chat.custom_skills import (
	MANAGED_OWNER,
	MAX_SKILLS_PER_PUSH,
	build_push_payload,
	project_org_promotion_push,
	pushable_org_skill_count,
)
from jarvis.permissions import require_jarvis_user

SKILL = "Jarvis Custom Skill"
_SETTINGS = "Jarvis Settings"
_PUSH_JOB_ID = "jarvis_custom_skills_push"
_LOCK_NAME = "jarvis_custom_skills_push"


# --------------------------------------------------------------------------- #
# CRUD (owner-scoped; frappe.get_doc enforces if_owner on read/write/delete)
# --------------------------------------------------------------------------- #
SHARE = "Jarvis Custom Skill Share"


def _full_name(user: str) -> str:
	return frappe.db.get_value("User", user, "full_name") or user


def _require_skill_owner(doc, action: str = "modify") -> None:
	"""Raise a CLEAR PermissionError unless the caller owns the skill (or is a
	System Manager / Administrator — matching skill_permissions.has_skill_permission
	write rule). The ORM hook denies a non-owner write anyway, but the framework's
	generic message is opaque; the user-facing SPA write endpoints surface this
	instead (security review PART 2 TASK E1)."""
	me = frappe.session.user
	if me == "Administrator" or (doc.get("owner") or "") == me:
		return
	if "System Manager" in frappe.get_roles(me):
		return
	frappe.throw(_("You can only {0} your own skills.").format(action), frappe.PermissionError)


def _skill_names_shared_with(user: str) -> list[str]:
	"""Skill row-names shared with ``user`` (child-table lookup, perm-free)."""
	return [
		r.parent
		for r in frappe.get_all(SHARE, filters={"user": user, "parenttype": SKILL}, fields=["parent"])
	]


@frappe.whitelist()
@require_jarvis_user
def list_custom_skills() -> list[dict]:
	"""The current user's own skills PLUS skills shared with them (read-only).

	Own rows carry ``mine=1`` + ``shared_count``; shared-with-me rows carry
	``mine=0`` + ``shared_by`` (owner's name) and only when ``enabled`` (a draft
	the owner shared is not surfaced to recipients)."""
	me = frappe.session.user
	own = frappe.get_all(
		SKILL,
		filters={"owner": me},
		fields=["name", "skill_name", "description", "user_invocable", "enabled", "modified"],
		order_by="skill_name asc",
	)
	# One grouped query for ALL own rows' share counts (was an N+1 count per
	# skill — 123 queries at 121 skills, on every ChatView mount).
	share_counts: dict = {}
	own_names = [s["name"] for s in own]
	if own_names:
		for x in frappe.db.sql(
			"""SELECT parent, COUNT(*) n FROM `tabJarvis Custom Skill Share`
			WHERE parent IN %(names)s GROUP BY parent""",
			{"names": tuple(own_names)},
			as_dict=True,
		):
			share_counts[x.parent] = x.n
	for s in own:
		s["mine"] = 1
		s["shared_count"] = share_counts.get(s["name"], 0)

	shared_names = _skill_names_shared_with(me)
	shared = []
	if shared_names:
		rows = frappe.get_all(
			SKILL,
			filters={"name": ["in", shared_names], "enabled": 1},
			fields=["name", "skill_name", "description", "user_invocable", "enabled", "owner", "modified"],
			order_by="skill_name asc",
		)
		# One query for the owners' display names (was a per-row lookup).
		full_names = (
			{
				u.name: u.full_name
				for u in frappe.get_all(
					"User",
					filters={"name": ["in", list({r.owner for r in rows})]},
					fields=["name", "full_name"],
				)
			}
			if rows
			else {}
		)
		for s in rows:
			s["mine"] = 0
			owner = s.pop("owner")
			s["shared_by"] = full_names.get(owner) or owner
			shared.append(s)
	return own + shared


# --------------------------------------------------------------------------- #
# Paginated list (frozen envelope) — chat-features-page-migration-design §2.2.
# ADDITIVE: list_custom_skills (above) STAYS for the composer "/" autocomplete.
# --------------------------------------------------------------------------- #
_SKILLS_SORTABLE = {"skill_name": "skill_name", "modified": "modified", "enabled": "enabled"}
_SKILLS_FILTERS = {"scope", "enabled", "user_invocable"}


def _lk(s: str) -> str:
	"""Escape LIKE wildcards in user search input (``\\`` is the default escape)."""
	return (s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp_page(start, page_length) -> tuple[int, int]:
	try:
		start = max(0, int(start or 0))
	except (TypeError, ValueError):
		start = 0
	try:
		pl = int(page_length or 20)
	except (TypeError, ValueError):
		pl = 20
	return start, max(1, min(pl, 100))


def _bool01(v) -> int:
	try:
		iv = int(v)
	except (TypeError, ValueError):
		frappe.throw(_("Filter value must be 0 or 1."))
	if iv not in (0, 1):
		frappe.throw(_("Filter value must be 0 or 1."))
	return iv


def _load_filters(filters, allowed: set) -> dict:
	"""Parse ``filters`` (JSON string or dict), whitelist keys (unknown → throw),
	and drop empty values (an empty value = 'not filtering'; ``0`` is kept)."""
	if isinstance(filters, str):
		if filters.strip():
			try:
				raw = frappe.parse_json(filters)
			except Exception:
				raw = {}
		else:
			raw = {}
	else:
		raw = filters or {}
	if not isinstance(raw, dict):
		raw = {}
	out: dict = {}
	for k, v in raw.items():
		if k not in allowed:
			frappe.throw(_("Unknown filter: {0}").format(k))
		if v in (None, ""):
			continue
		out[k] = v
	return out


def _order_by(sort_field, sort_dir, sortable: dict, default_field, default_dir, prefix="") -> str:
	"""Build a safe ORDER BY: only whitelisted columns, direction normalized to
	asc/desc, a ``name`` tiebreak for stable OFFSET pagination. No user input is
	ever interpolated (columns come from ``sortable``; dir is a literal)."""
	col = sortable.get(sort_field or "")
	if not col:
		return f"{prefix}`{sortable[default_field]}` {default_dir}, {prefix}`name` asc"
	d = "desc" if (sort_dir or "").lower() == "desc" else "asc"
	return f"{prefix}`{col}` {d}, {prefix}`name` asc"


@frappe.whitelist()
@require_jarvis_user
def list_custom_skills_page(
	search: str = "",
	filters: str | dict | None = None,
	sort_field: str = "",
	sort_dir: str = "",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""Owner-scoped + shared-with-me skills, server-side search/filter/sort/paginate.

	Visibility parity with ``list_custom_skills``: own rows (any state) UNION
	shared-with-me rows (``enabled=1`` only) — expressed in ONE owner-scoped SQL
	WHERE so ``page_length``/``start`` slice the real result set (never a
	post-filtered page). Envelope: ``{rows, total, has_more, start, page_length}``.
	"""
	me = frappe.session.user
	start, pl = _clamp_page(start, page_length)
	f = _load_filters(filters, _SKILLS_FILTERS)
	shared = tuple(_skill_names_shared_with(me))

	conds: list[str] = []
	params: dict = {"me": me, "start": start, "page_length": pl}

	scope = f.get("scope")
	if scope is not None and scope not in ("mine", "shared"):
		frappe.throw(_("Invalid scope filter."))
	if scope == "mine":
		conds.append("owner = %(me)s")
	elif scope == "shared":
		if not shared:
			conds.append("1=0")
		else:
			params["shared"] = shared
			conds.append("(name IN %(shared)s AND enabled = 1)")
	else:  # both
		if shared:
			params["shared"] = shared
			conds.append("(owner = %(me)s OR (name IN %(shared)s AND enabled = 1))")
		else:
			conds.append("owner = %(me)s")

	if search:
		params["q"] = f"%{_lk(search)}%"
		conds.append("(skill_name LIKE %(q)s OR description LIKE %(q)s)")
	if "enabled" in f:
		params["enabled"] = _bool01(f["enabled"])
		conds.append("enabled = %(enabled)s")
	if "user_invocable" in f:
		params["user_invocable"] = _bool01(f["user_invocable"])
		conds.append("user_invocable = %(user_invocable)s")

	where = " AND ".join(conds)
	order = _order_by(sort_field, sort_dir, _SKILLS_SORTABLE, "skill_name", "asc")

	total = frappe.db.sql(f"SELECT COUNT(*) FROM `tabJarvis Custom Skill` WHERE {where}", params)[0][0]
	rows = frappe.db.sql(
		f"""SELECT name, skill_name, description, user_invocable, enabled, modified, owner,
			ifnull(scope, 'Org') AS scope
		FROM `tabJarvis Custom Skill`
		WHERE {where}
		ORDER BY {order}
		LIMIT %(page_length)s OFFSET %(start)s""",
		params,
		as_dict=True,
	)

	# One grouped child query for share counts over THIS page's own rows.
	my_names = [r.name for r in rows if r.owner == me]
	share_counts: dict = {}
	if my_names:
		for x in frappe.db.sql(
			"""SELECT parent, COUNT(*) n FROM `tabJarvis Custom Skill Share`
			WHERE parent IN %(names)s GROUP BY parent""",
			{"names": tuple(my_names)},
			as_dict=True,
		):
			share_counts[x.parent] = x.n
	for r in rows:
		mine = 1 if r.owner == me else 0
		r["mine"] = mine
		r["shared_by"] = "" if mine else _full_name(r.owner)
		r["shared_count"] = share_counts.get(r.name, 0) if mine else 0
		r.pop("owner", None)

	return {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": pl,
	}


@frappe.whitelist()
@require_jarvis_user
def get_custom_skill(name: str) -> dict:
	"""Return one skill incl. the full markdown instructions. Readable by the
	owner (editable) or a user it's shared with (read-only). ``can_edit`` tells
	the SPA which view to render."""
	doc = frappe.get_doc(SKILL, name)
	me = frappe.session.user
	is_owner = doc.owner == me
	if not is_owner and not frappe.db.exists(SHARE, {"parent": name, "user": me}):
		frappe.throw(_("You don't have access to this skill."), frappe.PermissionError)
	# ``scope`` / ``target_role`` / ``managed_by_learning`` let the SPA gate the
	# "Request promotion…" affordance (owner + User-scope + not learned) and show
	# the current scope on the promotion status chip — the requester UI can't
	# reason about promotion without them. Read-only, still owner/shared-gated by
	# the access check above (Skills-area promotion surfacing).
	scope = (doc.scope or "Org") or "Org"
	if scope == "Personal":
		scope = "User"
	return {
		"name": doc.name,
		"skill_name": doc.skill_name,
		"description": doc.description,
		"instructions": doc.instructions,
		"user_invocable": int(doc.user_invocable or 0),
		"enabled": int(doc.enabled or 0),
		"modified": str(doc.modified or ""),
		"mine": int(is_owner),
		"can_edit": int(is_owner),
		"shared_by": "" if is_owner else _full_name(doc.owner),
		"scope": scope,
		"target_role": doc.get("target_role") or "",
		"managed_by_learning": int(doc.get("managed_by_learning") or 0),
	}


@frappe.whitelist()
@require_jarvis_user
def create_custom_skill(
	skill_name: str,
	description: str,
	instructions: str,
	user_invocable: int = 1,
	enabled: int = 1,
) -> dict:
	"""Create a skill. Validation (slug/caps) runs in the doctype's validate()."""
	return _create_custom_skill_impl(skill_name, description, instructions, user_invocable, enabled)


def _create_custom_skill_impl(
	skill_name: str,
	description: str,
	instructions: str,
	user_invocable: int = 1,
	enabled: int = 1,
	scope: str | None = None,
	ignore_permissions: bool = False,
) -> dict:
	"""Undecorated core of :func:`create_custom_skill`. Split out so a trusted
	server caller already authorized by its own gate can create a skill without
	re-tripping the ``@require_jarvis_user`` HTTP gate on the wrapper: the reviewer
	insight-apply path (``learned_api._apply_create_target``) is gated on
	``_REVIEWER_ROLES``, which admits a Jarvis Skill Reviewer / Jarvis Admin who
	holds neither Jarvis User nor System Manager.

	``scope`` is only accepted from trusted server callers (the reviewer
	insight-apply passes ``scope="Org"``); a blank scope defaults to User
	(private-first — security review PART 2 TASK 10). ``ignore_permissions``
	skips the doctype create-perm check for reviewer callers who may lack the
	Jarvis User role that the doctype now requires (the controller's own scope
	guard still runs and admits them as reviewers)."""
	fields = {
		"doctype": SKILL,
		"skill_name": skill_name,
		"description": description,
		"instructions": instructions,
		"user_invocable": int(user_invocable or 0),
		"enabled": int(enabled or 0),
	}
	if scope:
		fields["scope"] = scope
	doc = frappe.get_doc(fields)
	doc.insert(ignore_permissions=bool(ignore_permissions))
	frappe.db.commit()
	return {"ok": True, "data": {"name": doc.name, "skill_name": doc.skill_name}}


@frappe.whitelist()
@require_jarvis_user
def update_custom_skill(
	name: str,
	skill_name: str | None = None,
	description: str | None = None,
	instructions: str | None = None,
	user_invocable: int | None = None,
	enabled: int | None = None,
) -> dict:
	"""Update provided fields of a skill (owner-gated)."""
	doc = frappe.get_doc(SKILL, name)
	_require_skill_owner(doc, "edit")
	if skill_name is not None:
		doc.skill_name = skill_name
	if description is not None:
		doc.description = description
	if instructions is not None:
		doc.instructions = instructions
	if user_invocable is not None:
		doc.user_invocable = int(user_invocable)
	if enabled is not None:
		doc.enabled = int(enabled)
	doc.save()
	frappe.db.commit()
	return {"ok": True, "data": {"name": doc.name, "modified": str(doc.modified)}}


@frappe.whitelist()
@require_jarvis_user
def delete_custom_skill(name: str) -> dict:
	"""Delete a skill row (owner-gated). The delete only propagates to the
	container on the next Apply (the fleet endpoint does a full reconcile)."""
	_require_skill_owner(frappe.get_doc(SKILL, name), "delete")
	frappe.delete_doc(SKILL, name)  # ORM hook also enforces owner-only delete
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
@require_jarvis_user
def delete_custom_skills_bulk(names: str | list | None = None) -> dict:
	"""Bulk delete skills the caller OWNS (DESIGN-V3 §8.3 / D20). ``names`` is a
	JSON array of skill row-names. Per-row try/except so one bad row never
	aborts the batch: shared-with-me / foreign rows skip with ``not owner``.
	One deduped skills-apply enqueue at the end (mirrors the save/delete
	auto-apply) — only when something was actually deleted.
	Returns ``{deleted, skipped: [{name, reason}]}``."""
	raw = frappe.parse_json(names) if isinstance(names, str) else (names or [])
	items = [str(n) for n in raw if n] if isinstance(raw, list) else []
	me = frappe.session.user
	deleted = 0
	skipped: list[dict] = []
	for n in items:
		try:
			doc = frappe.get_doc(SKILL, n)
			if doc.owner != me:
				skipped.append({"name": n, "reason": "not owner"})
				continue
			frappe.delete_doc(SKILL, n)  # same path as delete_custom_skill (if_owner)
			deleted += 1
		except frappe.DoesNotExistError:
			skipped.append({"name": n, "reason": "not found"})
		except frappe.PermissionError:
			skipped.append({"name": n, "reason": "not permitted"})
		except Exception:
			# Never leak internal exception text to the client — log server-side.
			frappe.log_error(title="Jarvis: bulk skill delete failed", message=frappe.get_traceback())
			skipped.append({"name": n, "reason": "error"})
	frappe.db.commit()
	# Only a reviewer's delete reconciles the shared catalog (TASK 12): a plain
	# Jarvis User's rows are User/Role-scope and never in the shared push, so
	# their delete changes nothing there and must not trigger a bench-wide
	# restart. A reviewer deleting an Org skill DOES need the reconcile.
	if deleted:
		from jarvis.permissions import is_skill_reviewer

		if is_skill_reviewer(me):
			_apply_custom_skills_impl()
	return {"deleted": deleted, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Sharing (owner shares a skill with specific users; recipients get read-only
# use — they cannot edit, disable, delete, or re-share it)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def list_shareable_users() -> list[dict]:
	"""Users the current user can share a skill with (staff on this bench,
	excluding self + Guest). Feeds the share multiselect."""
	me = frappe.session.user
	return frappe.get_all(
		"User",
		filters={"enabled": 1, "name": ["not in", [me, "Guest", "Administrator"]]},
		fields=["name", "full_name"],
		order_by="full_name asc",
		limit_page_length=500,
	)


@frappe.whitelist()
@require_jarvis_user
def get_skill_shares(name: str) -> dict:
	"""Return who a skill is currently shared with (owner only)."""
	doc = frappe.get_doc(SKILL, name)
	if doc.owner != frappe.session.user:
		frappe.throw(_("Only the owner can manage sharing."), frappe.PermissionError)
	return {"users": [{"name": r.user, "full_name": _full_name(r.user)} for r in (doc.shared_with or [])]}


@frappe.whitelist()
def share_custom_skill(name: str, users: str | list | None = None) -> dict:
	"""Replace a skill's share list with ``users`` (a JSON array or list of user
	ids). Owner only. Recipients get read-only use; they can never re-share."""
	doc = frappe.get_doc(SKILL, name)
	doc.check_permission("write")  # owner-gate
	if doc.owner != frappe.session.user:
		frappe.throw(_("Only the owner can share this skill."), frappe.PermissionError)
	raw = frappe.parse_json(users) if isinstance(users, str) else (users or [])
	clean, seen = [], set()
	for u in raw if isinstance(raw, list) else []:
		u = (u or "").strip()
		if not u or u in seen or u == doc.owner or u == "Guest":
			continue
		if not frappe.db.exists("User", u):
			continue
		seen.add(u)
		clean.append(u)
	doc.set("shared_with", [{"user": u} for u in clean])
	doc.save()
	frappe.db.commit()
	return {"ok": True, "data": {"count": len(clean)}}


# --------------------------------------------------------------------------- #
# Scope promotion (User -> Role -> Org) — reviewer-gated widening workflow
# (security review PART 2 TASK 10, mirrors the wiki promotion machinery).
# --------------------------------------------------------------------------- #
PROMO = "Jarvis Skill Promotion Request"
_SCOPE_RANK = {"User": 0, "Personal": 0, "Role": 1, "Org": 2}


def _parse_ack_projection(ack):
	"""Coerce the reviewer's acknowledged push projection (a JSON string over HTTP,
	or a dict from a python caller) to a dict, or ``None``."""
	if isinstance(ack, str):
		if not ack.strip():
			return None
		try:
			ack = frappe.parse_json(ack)
		except Exception:
			return None
	return ack if isinstance(ack, dict) else None


def _projection_signature(proj) -> tuple | None:
	"""The decision-relevant fingerprint of a push projection (R2-SP-5). Two
	projections with the same signature warn the reviewer identically; a changed
	signature means the shared catalog moved under them and they must reconfirm.
	Only the fields that drive the warning are compared, so incidental fields never
	force a spurious reconfirm."""
	if not proj:
		return None
	return (
		bool(proj.get("over_budget")),
		bool(proj.get("at_budget")),
		int(proj.get("projected_count") or 0),
		tuple(proj.get("dropped_slugs") or []),
		bool(proj.get("promoted_dropped")),
	)


def _notify_skill_reviewers(request_name: str | None = None) -> None:
	"""Best-effort nudge to the skill-reviewer set that a promotion request
	landed. Carries the request name so the reviewer client can deep-link to it.
	Never breaks the request on failure."""
	try:
		from frappe.utils.user import get_users_with_role

		from jarvis.chat.events import publish_to_user
		from jarvis.permissions import JARVIS_REVIEWER_ROLES

		users: set = set()
		for role in JARVIS_REVIEWER_ROLES:
			try:
				users |= set(get_users_with_role(role))
			except Exception:
				pass
		payload = {"kind": "review:pending", "queue": "skill_promotion"}
		if request_name:
			payload["request"] = request_name
		for u in sorted(users):
			if u and u not in ("Administrator", "Guest"):
				publish_to_user(u, payload)
	except Exception:
		pass


@frappe.whitelist()
@require_jarvis_user
def request_skill_promotion(name: str, to_scope: str, target_role: str = "", note: str = "") -> dict:
	"""Ask a reviewer to widen one of the caller's OWN skills up the scope ladder
	(User->Role->Org). Files a Pending ``Jarvis Skill Promotion Request`` and
	pings the reviewer set — the skill itself is untouched; promotion is a
	request, never a self-service scope switch (the controller's scope guard
	rejects a self-service widen anyway)."""
	me = frappe.session.user
	doc = frappe.get_doc(SKILL, name)
	if doc.owner != me and me != "Administrator":
		frappe.throw(_("You can only promote your own skills."), frappe.PermissionError)
	if doc.get("managed_by_learning"):
		frappe.throw(_("Learned skills are managed by the learning board, not promotion."))

	from_scope = (doc.scope or "Org").strip() or "Org"
	if from_scope == "Personal":
		from_scope = "User"
	to_scope = (to_scope or "").strip()
	if to_scope not in ("Role", "Org"):
		frappe.throw(_("Promotion target must be Role or Org."))
	if _SCOPE_RANK.get(to_scope, 0) <= _SCOPE_RANK.get(from_scope, 0):
		frappe.throw(_("Promotion can only widen a skill's scope."))
	target_role = (str(target_role).strip() if target_role else "") or None
	if to_scope == "Role" and not target_role:
		frappe.throw(_("Promoting to Role scope needs a target role."))

	req = frappe.get_doc(
		{
			"doctype": PROMO,
			"skill": doc.name,
			"skill_name": doc.skill_name,
			# Immutable snapshot of the FULL content at request time (CDX-SP-1). The
			# reviewer decides on this, and approval promotes exactly this — so a
			# post-request edit, or content hidden past the old 300-char excerpt, can
			# never change what the Role/Org audience ends up running. The requester
			# owns ``doc`` (checked above), so this reads only their own content.
			"instructions_snapshot": doc.instructions or "",
			"description_snapshot": doc.description or "",
			"user_invocable_snapshot": int(doc.user_invocable or 0),
			"from_scope": from_scope,
			"to_scope": to_scope,
			"target_role": target_role if to_scope == "Role" else None,
			"note": (note or "").strip()[:140] or None,
			"status": "Pending",
		}
	)
	req.insert(ignore_permissions=True)
	frappe.db.commit()
	_notify_skill_reviewers(req.name)
	return {"ok": True, "request": req.name, "skill": doc.skill_name}


def _stamp_decision(req, reviewer: str, approved: bool, note: str) -> None:
	"""Write the terminal decision fields and commit. Shared by the approve and
	reject branches of :func:`decide_skill_promotion`; the approve branch calls it
	INSIDE the slug lock so the commit that publishes the shared copy happens while
	the lock is still held (R2-SP-3a)."""
	req.status = "Approved" if approved else "Rejected"
	req.reviewer = reviewer
	req.decided_at = frappe.utils.now_datetime()
	req.decision_note = (note or "").strip()[:140] or None
	req.save(ignore_permissions=True)
	frappe.db.commit()


@frappe.whitelist()
def decide_skill_promotion(
	request_name: str, approve: int | str, note: str = "", ack_projection=None
) -> dict:
	"""Approve or reject a skill promotion request. Reviewer-gated
	(``require_skill_reviewer``) + four-eyes (a reviewer cannot approve their own
	request). On approve, PUBLISH a new system-owned Role/Org skill from the
	request's immutable content snapshot, leaving the requester's private skill
	untouched (CDX-SP-1). The audience runs EXACTLY what the reviewer approved, and
	the requester holds no edit rights over the shared copy (they don't own it, and
	``_guard_content_change`` locks its content to reviewers), so the approval can
	never be silently rewritten after review. TOCTOU-safe: the status is re-read
	under a row lock, so two concurrent approvals can't double-publish.

	R2-SP-5 (budget binding): for an Org approval the push-budget projection is
	recomputed fresh under THIS decision and compared to what the reviewer
	acknowledged (``ack_projection``, the fresh preflight they confirmed on). If a
	warning-worthy catalog moved under them, nothing is published — the NEW
	projection is returned and the reviewer must reconfirm against it.

	R2-SP-3a (atomic materialization): the approve path holds a slug-scoped lock
	across the shared-sibling check + insert + commit, so two concurrent same-slug
	approvals serialize (one publishes, the other sees the committed copy and
	refuses as a lineage conflict — never a duplicate shared row). The promoted
	skill joins the shared catalog on the next explicit Apply (never auto-pushed
	here)."""
	from jarvis._redis_lock import redis_lock
	from jarvis.permissions import require_skill_reviewer

	require_skill_reviewer()
	reviewer = frappe.session.user
	req = frappe.get_doc(PROMO, request_name)
	if reviewer == (req.owner or "") and reviewer != "Administrator":
		frappe.throw(
			_("You cannot decide your own promotion request; another reviewer must approve or reject it."),
			frappe.PermissionError,
		)
	status = frappe.db.get_value(PROMO, request_name, "status", for_update=True)
	if status != "Pending":
		return {"ok": False, "reason": _("Already {0}.").format((status or "").lower())}

	approved = str(approve).strip().lower() in ("1", "true", "yes", "on")

	# R2-SP-5: bind the budget projection to THIS decision. A reconfirm is required
	# only when the reviewer would publish INTO a budget warning without having
	# acknowledged this exact projection — either the catalog moved since their ack,
	# or a warning state exists and they sent no matching ack. A clear (room to
	# spare) catalog never needs a reconfirm, so a well-formed under-budget approval
	# is unaffected.
	if approved and (req.to_scope or "") == "Org":
		fresh = project_org_promotion_push(req.skill, req.skill_name or "")
		warned = bool(fresh and (fresh.get("over_budget") or fresh.get("at_budget")))
		if warned and _projection_signature(_parse_ack_projection(ack_projection)) != _projection_signature(
			fresh
		):
			return {
				"ok": False,
				"needs_reconfirm": True,
				"to_scope": "Org",
				"push_projection": fresh,
				"reason": _(
					"The shared catalog changed since you last checked — review the updated "
					"push impact and confirm again."
				),
			}

	out: dict = {"ok": True, "status": "Approved" if approved else "Rejected"}
	if approved:
		# Serialize concurrent same-slug approvals across the check+insert+commit;
		# the winner commits before releasing, so a contender sees the published
		# copy and refuses as a conflict instead of minting a duplicate.
		slug = (req.skill_name or "").strip().lower()
		with redis_lock(f"skillpromo:{slug}", timeout_s=60, blocking_timeout_s=15.0) as acquired:
			if not acquired:
				return {
					"ok": False,
					"reason": _(
						"Another promotion for '{0}' is being decided right now; try again in a moment."
					).format(req.skill_name),
				}
			out.update(_materialize_promotion(req))
			_stamp_decision(req, reviewer, True, note)
		return out

	_stamp_decision(req, reviewer, False, note)
	return out


def _materialize_promotion(req) -> dict:
	"""Publish the reviewed snapshot as a system-owned Role/Org skill (the approve
	path — CDX-SP-1). The requester's private (User/Role) skill is left intact; the
	shared copy carries EXACTLY the request-time snapshot content, is owned by the
	system identity (``MANAGED_OWNER``) rather than the requester, and is
	content-locked to reviewers by the controller guard. Called INSIDE the
	slug-scoped lock held by :func:`decide_skill_promotion`. Returns ``{skill,
	materialized, push_projection?}`` folded into the decision result."""
	src = frappe.get_doc(SKILL, req.skill)
	live = (src.scope or "Org").strip() or "Org"
	if live == "Personal":
		live = "User"
	if _SCOPE_RANK.get(req.to_scope, 0) <= _SCOPE_RANK.get(live, 0):
		frappe.throw(_("The skill is already at or above the requested scope."))

	# R2-SP-1: approval binds to the IMMUTABLE request-time snapshot and NEVER reads
	# the (mutable) live source at decision time — a live fallback would reopen the
	# CDX-SP-1 TOCTOU (reviewer sees v A, requester edits to v B, v B publishes). A
	# legacy request filed before content-binding carries a null snapshot; it cannot
	# be approved — refuse with a resubmit message (a resubmission mints a fresh full
	# snapshot). New requests are guarded non-null at request time, so this only ever
	# trips a genuine pre-binding row.
	if req.get("instructions_snapshot") is None or req.get("description_snapshot") is None:
		frappe.throw(
			_(
				"This promotion request predates content-binding and cannot be approved; "
				"ask the requester to resubmit it."
			)
		)
	instructions = req.get("instructions_snapshot")
	description = req.get("description_snapshot") or ""
	user_invocable = int(req.get("user_invocable_snapshot") or 0)
	target_role = req.target_role if req.to_scope == "Role" else None

	# R2-SP-3 (lineage + atomic uniqueness). The container writes ONE custom-<slug>
	# dir, so at most one SHARED (Role/Org) copy may carry a given authored slug.
	# Under the slug lock (held + committed by the caller before releasing), examine
	# the shared siblings of this slug:
	#   * an existing shared copy materialized from THIS SAME private source
	#     (``source_skill`` lineage) is SUPERSEDED in place — widened to the new
	#     scope with the new snapshot content, so User->Role then User->Org leaves
	#     ONE shared copy at Org (no orphan Role copy, no dead-end for the requester);
	#   * an existing shared copy from a DIFFERENT source that authored the same slug
	#     is a hard conflict — refuse with a clear message.
	shared_siblings = [
		s
		for s in frappe.get_all(
			SKILL,
			filters={"skill_name": req.skill_name, "name": ["!=", req.skill or ""]},
			fields=["name", "scope", "source_skill"],
		)
		if (s.scope or "Org").strip() not in ("User", "Personal")
	]
	same_lineage = next((s for s in shared_siblings if (s.source_skill or "") == req.skill), None)
	if any((s.source_skill or "") != req.skill for s in shared_siblings):
		frappe.throw(
			_("A shared skill named '{0}' already exists; remove or rename it before promoting.").format(
				req.skill_name
			)
		)

	prev_flag = frappe.flags.jarvis_promotion_materialize
	frappe.flags.jarvis_promotion_materialize = True
	try:
		if same_lineage is not None:
			# Supersede the prior materialized copy IN PLACE (one-row atomic widen).
			existing_scope = (same_lineage.scope or "Org").strip() or "Org"
			if _SCOPE_RANK.get(req.to_scope, 0) <= _SCOPE_RANK.get(existing_scope, 0):
				frappe.throw(_("This skill is already shared at {0} scope or wider.").format(existing_scope))
			shared = frappe.get_doc(SKILL, same_lineage.name)
			shared.scope = req.to_scope
			shared.target_role = target_role
			shared.description = description
			shared.instructions = instructions
			shared.user_invocable = user_invocable
			shared.enabled = 1
			shared.save(ignore_permissions=True)
			new_name = shared.name
		else:
			new = frappe.get_doc(
				{
					"doctype": SKILL,
					"skill_name": req.skill_name,
					"description": description,
					"instructions": instructions,
					"user_invocable": user_invocable,
					"enabled": 1,
					"scope": req.to_scope,
					"target_role": target_role,
					"source_skill": req.skill,
				}
			)
			new.insert(ignore_permissions=True)
			new_name = new.name
	finally:
		frappe.flags.jarvis_promotion_materialize = prev_flag
	# Own the shared copy as the system identity, not the approving reviewer: the
	# requester must not own what the audience runs (they'd keep edit rights), and a
	# system owner survives reviewer account changes. Metadata-only, so a direct db
	# write (update_modified=False) is the right tool. Uniqueness was validated
	# against this FINAL owner before commit (see _validate_unique_per_owner under
	# the jarvis_promotion_materialize flag — R2-SP-3a).
	frappe.db.set_value(SKILL, new_name, "owner", MANAGED_OWNER, update_modified=False)

	out = {"skill": req.skill_name, "materialized": new_name}
	if req.to_scope == "Org":
		out["push_projection"] = project_org_promotion_push(new_name, req.skill_name)
	return out


_PROMO_STATUSES = ("Pending", "Approved", "Rejected")


@frappe.whitelist()
def list_skill_promotion_requests(
	status: str = "Pending",
	search: str = "",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""Reviewer discovery queue for skill promotion requests. The doctype perms
	are ``All: read+if_owner`` (so a requester sees only their OWN requests) — a
	reviewer holding only Jarvis Skill Reviewer would get [] from generic
	get_list. So this reviewer-gated raw-SQL list sidesteps the doctype-perm limit
	exactly as ``learned_api.list_promotion_requests_page`` does for the wiki
	queue. Envelope parity: ``{rows, total, has_more, start, page_length}``.
	Requester full name resolved via one batched lookup."""
	from jarvis.permissions import require_skill_reviewer

	require_skill_reviewer()
	start, pl = _clamp_page(start, page_length)
	if status and status != "All" and status not in _PROMO_STATUSES:
		frappe.throw(_("Invalid status filter."))

	params: dict = {"start": start, "page_length": pl}
	conds: list[str] = []
	if status and status != "All":
		params["status"] = status
		conds.append("status = %(status)s")
	if search:
		params["q"] = f"%{_lk(search)}%"
		conds.append("(skill_name LIKE %(q)s OR note LIKE %(q)s)")
	where = " AND ".join(conds) or "1=1"

	total = frappe.db.sql(f"SELECT COUNT(*) FROM `tab{PROMO}` WHERE {where}", params)[0][0]
	rows = frappe.db.sql(
		f"""SELECT name, skill, skill_name, from_scope, to_scope, target_role,
			note, status, owner, creation, reviewer, decided_at, decision_note,
			instructions_snapshot, description_snapshot, user_invocable_snapshot
		FROM `tab{PROMO}`
		WHERE {where}
		ORDER BY creation DESC, name ASC
		LIMIT %(page_length)s OFFSET %(start)s""",
		params,
		as_dict=True,
	)

	owner_names = list({r["owner"] for r in rows if r.get("owner")})
	fullnames = {
		u.name: u.full_name
		for u in (
			frappe.get_all("User", filters={"name": ["in", owner_names]}, fields=["name", "full_name"])
			if owner_names
			else []
		)
	}
	# Reviewer content preview: return the request's IMMUTABLE snapshot (the FULL
	# content), not a 300-char live excerpt — the reviewer decides on EXACTLY what
	# approval promotes, so nothing can be hidden past a truncation or edited after
	# review (CDX-SP-1). R2-SP-2: materialization publishes the instructions AND the
	# description AND the user-invocable flag from the snapshot, so ALL THREE are
	# surfaced here — a reviewer must see every field their approval publishes.
	# Strict-need (SAR-3): the preview is only decision-relevant for Pending rows; a
	# decided (Approved/Rejected) row must NOT re-surface the requester's content.
	# Legacy rows filed before the snapshot fields fall back to the live values via
	# one reviewer-gated batch fetch (a reviewer cannot read a User-scope skill body
	# via get_custom_skill, so this raw fetch is the only surface that can show it —
	# the same reason the list itself sidesteps if_owner); such rows cannot actually
	# be approved (R2-SP-1) but the preview still explains what to resubmit.
	legacy_pending_skills = list(
		{
			r["skill"]
			for r in rows
			if r.get("skill") and r.get("status") == "Pending" and r.get("instructions_snapshot") is None
		}
	)
	legacy_live = {
		s.name: s
		for s in (
			frappe.get_all(
				SKILL,
				filters={"name": ["in", legacy_pending_skills]},
				fields=["name", "instructions", "description", "user_invocable"],
			)
			if legacy_pending_skills
			else []
		)
	}

	def _preview(r) -> str:
		if r.get("status") != "Pending":
			return ""
		snap = r.get("instructions_snapshot")
		if snap is not None:
			return snap
		live = legacy_live.get(r.get("skill"))
		return (live.instructions if live else "") or ""

	def _desc_preview(r) -> str:
		if r.get("status") != "Pending":
			return ""
		snap = r.get("description_snapshot")
		if snap is not None:
			return snap
		live = legacy_live.get(r.get("skill"))
		return (live.description if live else "") or ""

	def _ui_preview(r):
		# The user-invocable flag approval publishes (int 0/1), or None for a decided
		# row (nothing to surface).
		if r.get("status") != "Pending":
			return None
		snap = r.get("user_invocable_snapshot")
		if snap is not None:
			return int(snap or 0)
		live = legacy_live.get(r.get("skill"))
		return int(live.user_invocable or 0) if live else None

	def _projection(r):
		# Server-side push-budget projection for a Pending Org promotion, sharing
		# build_push_payload's exact logic (CDX-SP-2). The client renders THIS, not
		# a stale count-based guess. Role/User targets never touch the push.
		if r.get("status") == "Pending" and r.get("to_scope") == "Org" and r.get("skill"):
			return project_org_promotion_push(r["skill"], r.get("skill_name") or "")
		return None

	out_rows = [
		{
			"name": r["name"],
			"skill": r.get("skill") or "",
			"skill_name": r.get("skill_name") or "",
			"from_scope": r.get("from_scope") or "",
			"to_scope": r.get("to_scope") or "",
			"target_role": r.get("target_role") or "",
			"note": r.get("note") or "",
			"status": r.get("status") or "",
			"requested_by": r.get("owner") or "",
			"requested_by_name": fullnames.get(r.get("owner")) or (r.get("owner") or ""),
			"created": str(r.get("creation") or ""),
			"reviewer": r.get("reviewer") or "",
			"decided_at": str(r.get("decided_at") or ""),
			"decision_note": r.get("decision_note") or "",
			"body_excerpt": _preview(r),
			# R2-SP-2: the description + user-invocable flag approval ALSO publishes,
			# so the reviewer card + approve confirm render them alongside the body.
			"description_snapshot": _desc_preview(r),
			"user_invocable_snapshot": _ui_preview(r),
			"push_projection": _projection(r),
		}
		for r in rows
	]
	return {
		"rows": out_rows,
		"total": total,
		"has_more": start + len(out_rows) < total,
		"start": start,
		"page_length": pl,
		# Coarse container push-budget context (uncapped pushable Org count + the
		# real cap). The AUTHORITATIVE per-row truth is each Pending Org row's
		# ``push_projection`` (recomputed fresh at approval via
		# ``preflight_skill_promotion``); these two stay for at-a-glance context.
		"push_count": pushable_org_skill_count(),
		"push_budget": MAX_SKILLS_PER_PUSH,
	}


@frappe.whitelist()
def preflight_skill_promotion(request_name: str) -> dict:
	"""Fresh, reviewer-gated push-budget projection for one Pending promotion,
	recomputed at the moment the reviewer is about to decide (CDX-SP-2) — a
	list-load value goes stale under concurrent promotions/edits. Returns
	``{ok, to_scope, push_projection}``; ``push_projection`` is ``None`` for
	Role/User targets (they never reach the container push) or a non-Pending row.
	Shares :func:`build_push_payload`'s exact logic via
	``project_org_promotion_push`` so the warning states the TRUTH, not a guess."""
	from jarvis.permissions import require_skill_reviewer

	require_skill_reviewer()
	req = frappe.get_doc(PROMO, request_name)
	projection = None
	if req.status == "Pending" and req.to_scope == "Org":
		projection = project_org_promotion_push(req.skill, req.skill_name or "")
	return {"ok": True, "to_scope": req.to_scope or "", "push_projection": projection}


@frappe.whitelist()
@require_jarvis_user
def my_skill_promotion(name: str) -> dict:
	"""The caller's MOST-RECENT promotion request for one of their OWN skills —
	the requester-side status read (the reviewer list endpoint is reviewer-gated,
	so a requester needs their own read to render the "requested → approved /
	rejected" chip). Owner-scoped by the ``owner`` filter, so it can only ever
	return the caller's own request. Returns ``{}`` when there is none.

	Read-only, smallest addition (Skills-area promotion surfacing): the doctype
	perms are ``All: read + if_owner``, but the SPA does not use generic
	client.get_list anywhere, so this thin owner-scoped wrapper keeps the
	house "one whitelisted method per read" idiom."""
	me = frappe.session.user
	rows = frappe.get_all(
		PROMO,
		filters={"skill": name, "owner": me},
		fields=[
			"name",
			"status",
			"from_scope",
			"to_scope",
			"target_role",
			"note",
			"reviewer",
			"decided_at",
			"decision_note",
			"creation",
		],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return {}
	r = rows[0]
	return {
		"name": r.name,
		"status": r.status or "",
		"from_scope": r.from_scope or "",
		"to_scope": r.to_scope or "",
		"target_role": r.target_role or "",
		"note": r.note or "",
		"reviewer": r.reviewer or "",
		"reviewer_name": _full_name(r.reviewer) if r.reviewer else "",
		"decided_at": str(r.decided_at or ""),
		"decision_note": r.decision_note or "",
		"created": str(r.creation or ""),
	}


@frappe.whitelist()
@require_jarvis_user
def promotable_target_roles() -> dict:
	"""Role options for the promotion requester's Role picker: the caller's OWN
	desk roles, minus non-targetable system roles. A requester can only sensibly
	ask to widen a skill to a team they belong to; the reviewer makes the final
	call. Reuses the wiki's ``_NON_TARGETABLE_ROLES`` vocabulary so skill- and
	wiki-promotion requesters speak ONE consistent role language (the wiki
	reviewer-side ``manageable_roles`` is a CURATOR concept — empty for a plain
	user — and is the wrong source for a requester). Returns ``{roles: [...]}``.

	Gated with ``@require_jarvis_user`` (SAR-2) — no cross-user leak (it only
	ever reflects the caller's OWN session roles), but it carries the module's
	name-your-caller gate for surface uniformity with every sibling read."""
	from jarvis.chat.wiki_permissions import _NON_TARGETABLE_ROLES

	me = frappe.session.user
	roles = sorted(r for r in frappe.get_roles(me) if r and r not in _NON_TARGETABLE_ROLES)
	return {"roles": roles}


# --------------------------------------------------------------------------- #
# Apply (explicit push to the container, via admin → fleet)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def get_custom_skills_sync_status() -> dict:
	"""Lightweight poller mirroring onboarding.get_llm_sync_status."""
	return _custom_sync_status()


def _custom_sync_status() -> dict:
	"""Undecorated core of :func:`get_custom_skills_sync_status`. Split out so the
	reviewer Apply board (``learned_api._cutover_custom_sync_status``, reached from
	``get_learned_apply_status`` under ``_REVIEWER_ROLES``) can read the custom sync
	pair without tripping the ``@require_jarvis_user`` HTTP gate — a read of two
	``Jarvis Settings`` fields the reviewer is already authorized to see."""
	s = frappe.get_single(_SETTINGS)
	status = s.get("custom_skills_sync_status") or ""
	return {
		"last_sync_at": str(s.get("custom_skills_synced_at") or ""),
		"last_sync_status": status,
		"pending": status.startswith("pending:"),
	}


@frappe.whitelist()
def apply_custom_skills() -> dict:
	"""Push all enabled skills to the assistant (one restart). Explicit action.

	Reviewer/admin-gated (security review PART 2 TASK 12): a bench-wide push
	reconciles + RESTARTS the shared container for EVERY user, so a plain Jarvis
	User (which every backfilled user holds) must not be able to trigger it (DoS +
	it is what forces any Org skill out to all pods). Gated with the skill-reviewer
	set (Jarvis Skill Reviewer / Jarvis Admin / System Manager) — deliberately NOT
	stacked under @require_jarvis_user, since a reviewer/admin may hold neither
	Jarvis User nor System Manager.

	Builds the payload synchronously so size/cap errors surface immediately,
	marks a pending status, then enqueues the deduped worker (mirrors
	``JarvisSettings.on_update``). ``strict=True``: this is the interactive
	endpoint - a human is present to act on the over-cap error, so raise
	instead of truncating (the unattended worker below stays graceful).
	"""
	from jarvis.permissions import require_skill_reviewer

	require_skill_reviewer()
	return _apply_custom_skills_impl()


def _apply_custom_skills_impl() -> dict:
	"""Undecorated core of :func:`apply_custom_skills`. Split out so a caller
	already authorized by its own gate can reconcile the shared catalog without
	re-checking the reviewer gate (the bulk-delete path only invokes it for a
	reviewer)."""
	skills = build_push_payload(strict=True)
	frappe.db.set_single_value(_SETTINGS, "custom_skills_sync_status", "pending: applying skills")
	frappe.db.commit()
	run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
	frappe.enqueue(
		"jarvis.chat.custom_skills_api._enqueued_push_custom_skills",
		queue="long",
		timeout=180,
		enqueue_after_commit=not run_inline,
		now=run_inline,
		job_id=_PUSH_JOB_ID,
		deduplicate=True,
	)
	return {"ok": True, "custom_skills_sync_status": "pending: applying skills", "count": len(skills)}


def _enqueued_push_custom_skills() -> None:
	"""Background worker: push the enabled skills via admin → fleet → container.

	Re-builds the payload fresh (never trust a payload passed across the queue
	boundary) and mirrors ``_sync_via_admin``'s try/except/finally so the status
	never stays at ``pending:`` forever. ``strict=False``: this worker also runs
	unattended (post-restart resync), where an over-cap raise would leave a
	rebuilt container skill-less - truncate gracefully instead (the build logs
	the truncation loudly).
	"""
	from jarvis import admin_client
	from jarvis._redis_lock import redis_lock

	with redis_lock(_LOCK_NAME, timeout_s=180, blocking_timeout_s=60.0) as acquired:
		if not acquired:
			frappe.db.set_single_value(
				_SETTINGS, "custom_skills_sync_status", "failed: skipped (concurrent sync)"
			)
			frappe.db.commit()
			return

		terminal_written = False
		try:
			payload = build_push_payload(strict=False)
			admin_client.post_push_custom_skills(skills=payload)
			frappe.db.set_value(
				_SETTINGS,
				_SETTINGS,
				{
					"custom_skills_synced_at": frappe.utils.now(),
					"custom_skills_sync_status": f"ok (applied {len(payload)} via admin)",
				},
			)
			terminal_written = True
		except admin_client.AdminAuthError as e:
			_fail(f"failed: auth: {e}")
			terminal_written = True
			frappe.log_error(title="Jarvis: custom-skills admin auth failed", message=frappe.get_traceback())
		except admin_client.AdminUnreachableError as e:
			_fail(f"failed: admin unreachable: {e}")
			terminal_written = True
			frappe.log_error(title="Jarvis: custom-skills admin unreachable", message=frappe.get_traceback())
		except admin_client.AdminRateLimitedError as e:
			retry = getattr(e, "retry_after_seconds", 0) or 0
			retry_str = f"retry_after={retry}s" if retry > 0 else "retry shortly"
			_fail(f"failed: rate-limited; {retry_str}")
			terminal_written = True
		except admin_client.AdminValidationError as e:
			_fail(f"failed: invalid: {e}")
			terminal_written = True
		except Exception:
			# Graceful-resync guard: this worker also runs unattended after a
			# container restart, so ANY exception (build, network class the
			# admin_client doesn't translate, programmer error) must degrade to
			# a logged terminal failure - never a skill-less container with a
			# silent dead job. Mirrors _enqueued_push_learned_skills.
			_fail("failed: unexpected error; see Error Log")
			terminal_written = True
			frappe.log_error(title="Jarvis: custom-skills push failed", message=frappe.get_traceback())
		finally:
			if not terminal_written:
				try:
					_fail("failed: unexpected error; see Error Log")
				except Exception:
					pass
		frappe.db.commit()


def _fail(status: str) -> None:
	frappe.db.set_value(
		_SETTINGS,
		_SETTINGS,
		{"custom_skills_synced_at": frappe.utils.now(), "custom_skills_sync_status": status},
	)
