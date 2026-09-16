"""What the CURRENT chat user can do in this instance: their roles plus any
User Permission restrictions that narrow which records they see.

The persona answers "who am I" / "what roles do I have" straight from the
``[Context:]`` bracket (turn_handler folds the roles in on an identity/permission
turn) — no tool needed for that. This tool is the bounded, one-call answer for
DEEPER "what am I actually restricted to" questions (record-level User
Permissions), so the agent never has to improvise an open-ended walk over the
permission meta (the get_list/get_doc grind that ran into the turn wall-clock).

Read-only and self-scoped: it reports the calling user's OWN access only
(``frappe.session.user``), never another user's — so it can't be used to probe
someone else's privileges.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError

# Base roles every user carries — noise, never part of "my permissions".
_BASE_ROLES = ("All", "Guest")
# Restrictions are reported two ways: a per-DocType `by_doctype` summary (counts +
# a value sample — scale-proof, O(DocTypes)) plus a capped `detail` list of the
# precise rows for the common small case. `by_doctype` removes any fixed "wall" on
# the count; `detail` is where per-row includes_descendants/applies_to stay exact.
_MAX_DETAIL = 200  # precise-rows cap (by_doctype counts stay complete past this)
_RESTRICTION_SAMPLE = 20  # for_value names shown per DocType in by_doctype
_MAX_RESTRICTION_DOCTYPES = 50  # cap DISTINCT DocTypes in by_doctype (top by count) so the
# result stays within the model's tool-result budget even for a user restricted across many
# DocTypes — by_doctype caps values-per-DocType, this caps the number of DocType keys.
_MAX_RESTRICTION_SCAN = 5000  # internal safety fetch bound for the grouping scan (~never hit)
# Cap the per-call capability probe so one call can't fan out into a huge
# permission sweep (each doctype = a few has_permission checks + a meta read).
_MAX_DOCTYPES = 50
# The doctype-level actions worth reporting, mapped to the write tool each gates.
# submit/cancel are added only for submittable DocTypes (see _capabilities).
_BASE_PTYPES = ("create", "read", "write", "delete")
# Per-record actions (see _record_capabilities). No "create" — a create has no
# record yet, so it is inherently doctype-level (that lives in _capabilities).
_RECORD_PTYPES = ("read", "write", "delete")


def _is_tree_doctype(doctype: str) -> bool:
	"""True for a nested-set (tree) DocType (Warehouse, Cost Center, ...), where a
	User Permission on a node also covers its descendants unless hide_descendants is
	set. Own seam so tests can pin descendant-scope reporting without a live tree."""
	try:
		return bool(frappe.get_meta(doctype).is_nested_set())
	except Exception:
		return False


def _capabilities(doctype: str, user: str) -> dict:
	"""The calling user's doctype-level (role-based) actions on ``doctype`` —
	the "can I create / submit / ... this?" answer the write tools pre-flight on.
	Ignores record-level User Permissions (those are the separate ``restrictions``
	summary); submit/cancel only for a submittable DocType."""
	ptypes = list(_BASE_PTYPES)
	if frappe.get_meta(doctype).is_submittable:
		ptypes += ["submit", "cancel"]
	return {pt: bool(frappe.has_permission(doctype, ptype=pt, user=user)) for pt in ptypes}


def _record_capabilities(doctype: str, name: str, user: str) -> dict:
	"""The calling user's actions on ONE specific record. Passing ``doc`` to
	has_permission is what makes this User-Permission aware (plus DocShare + owner
	rules) — unlike the doctype-level _capabilities, this answers "can I actually
	read/write THIS record?" Submit/cancel only for a submittable DocType."""
	ptypes = list(_RECORD_PTYPES)
	if frappe.get_meta(doctype).is_submittable:
		ptypes += ["submit", "cancel"]
	return {pt: bool(frappe.has_permission(doctype, ptype=pt, doc=name, user=user)) for pt in ptypes}


def get_my_access(doctypes: list[str] | None = None, docs: list[dict] | None = None) -> dict:
	"""Return the calling user's roles + record-level restrictions, and optionally
	the doctype-level actions they can take on named DocTypes.

	Shape::

	    {
	      "user": "<id>", "full_name": "<name>",
	      "roles": [...meaningful roles, sorted...], "role_count": N,
	      "is_system_manager": bool,      # System Manager / Administrator
	      "restrictions": {
	        "by_doctype": {"<DocType>": {"count", "values": [...], "values_truncated"}},
	        "doctype_count": N, "doctypes_truncated": bool,
	        "detail": [{"doctype", "value", "applies_to", "includes_descendants"?}...],
	        "detail_truncated": bool, "total": N, "scan_truncated": bool,
	      },
	      "can": {"<DocType>": {"create", "read", "write", "delete", "submit"?, "cancel"?}},
	      "can_doc": [{"doctype", "name", "can": {"read", "write", "delete", "submit"?, "cancel"?}}],
	    }

	``restrictions`` are the user's User Permission rows — each limits the records of
	``doctype`` they can see to ``value``. ``by_doctype`` is the scale-proof summary
	(count + a value sample per restricted DocType, no fixed wall on the count);
	``detail`` is the precise per-row list for the common small case (capped at
	``_MAX_DETAIL``), where ``applies_to`` (the row narrowed to one linked doctype)
	and, for a tree DocType, ``includes_descendants`` (covers ``value``'s whole
	subtree unless False) stay exact. Empty ``by_doctype`` means no record-level
	restriction: access is governed by roles alone.

	Pass ``doctypes`` (max 50) to also get ``can`` — the role-based action map per
	DocType (so "can I create a Sales Invoice?" is one deterministic call instead of
	drafting a write that dies at save). Omitted entirely when ``doctypes`` is not
	given, so the plain "my access" call stays lean.

	Pass ``docs`` (a list of ``{"doctype", "name"}``, max 50) for the User-Permission
	AWARE answer on specific records — ``can_doc`` reports read/write/delete
	(+submit/cancel) per record, factoring in record-level User Permissions, DocShare
	and owner rules (which ``can`` deliberately does not). Use it to pre-check "can I
	actually write THIS invoice?" rather than just "can my role write invoices?".
	"""
	user = frappe.session.user
	full_name = frappe.db.get_value("User", user, "full_name") or ""
	all_roles = frappe.get_roles(user)
	roles = sorted(r for r in all_roles if r not in _BASE_ROLES)
	is_system_manager = "System Manager" in all_roles or user == "Administrator"

	# The user's OWN User Permission rows only — hard-filtered to session user, so
	# there is no cross-user leakage even though get_all skips the doctype's own
	# read gate (which a plain user lacks on User Permission). +1 to detect overflow.
	rows = frappe.get_all(
		"User Permission",
		filters={"user": user},
		fields=["allow", "for_value", "applicable_for", "hide_descendants"],
		order_by="allow asc, for_value asc",
		limit_page_length=_MAX_RESTRICTION_SCAN + 1,
	)
	scan_truncated = len(rows) > _MAX_RESTRICTION_SCAN
	rows = rows[:_MAX_RESTRICTION_SCAN]

	# by_doctype: scale-proof summary — count + a value sample per restricted DocType.
	# Counts are over ALL scanned rows, so there is no fixed wall on how many
	# restrictions a DocType can report.
	by_doctype: dict = {}
	for r in rows:
		g = by_doctype.setdefault(r.allow, {"count": 0, "values": [], "values_truncated": False})
		g["count"] += 1
		if len(g["values"]) < _RESTRICTION_SAMPLE:
			g["values"].append(r.for_value)
		else:
			g["values_truncated"] = True

	# Cap the NUMBER of DocType keys too (keep the top DocTypes by count), so a user
	# restricted across very many DocTypes can't blow the model's tool-result budget —
	# the dict is passed to the model uncapped (no rows/result key for the size guard to
	# trim). doctype_count keeps the true total honest.
	doctype_count = len(by_doctype)
	doctypes_truncated = doctype_count > _MAX_RESTRICTION_DOCTYPES
	if doctypes_truncated:
		top = sorted(by_doctype.items(), key=lambda kv: kv[1]["count"], reverse=True)
		by_doctype = dict(top[:_MAX_RESTRICTION_DOCTYPES])

	# detail: the PRECISE per-row rows (the common small case), capped. Per-row
	# includes_descendants (tree DocType) and applies_to stay exact here — the
	# grouped summary above deliberately coarsens to counts, which need no per-row
	# precision. On a tree DocType a permission on a node also grants its subtree
	# unless hide_descendants is set; that distinction lives per row.
	detail = []
	for r in rows[:_MAX_DETAIL]:
		entry = {"doctype": r.allow, "value": r.for_value, "applies_to": r.applicable_for or None}
		if _is_tree_doctype(r.allow):
			entry["includes_descendants"] = not r.hide_descendants
		detail.append(entry)

	result = {
		"user": user,
		"full_name": full_name,
		"roles": roles,
		"role_count": len(roles),
		"is_system_manager": is_system_manager,
		"restrictions": {
			"by_doctype": by_doctype,
			"doctype_count": doctype_count,
			"doctypes_truncated": doctypes_truncated,
			"detail": detail,
			"detail_truncated": len(rows) > _MAX_DETAIL,
			"total": len(rows),
			"scan_truncated": scan_truncated,
		},
	}

	if doctypes is not None:
		if not isinstance(doctypes, (list, tuple)):
			raise InvalidArgumentError("doctypes must be a list of DocType names")
		if len(doctypes) > _MAX_DOCTYPES:
			raise InvalidArgumentError(f"at most {_MAX_DOCTYPES} doctypes per call")
		can = {}
		for dt in doctypes:
			if not isinstance(dt, str) or not frappe.db.exists("DocType", dt):
				raise InvalidArgumentError(f"unknown DocType: {dt!r}")
			can[dt] = _capabilities(dt, user)
		result["can"] = can

	if docs is not None:
		if not isinstance(docs, (list, tuple)):
			raise InvalidArgumentError("docs must be a list of {doctype, name} objects")
		if len(docs) > _MAX_DOCTYPES:
			raise InvalidArgumentError(f"at most {_MAX_DOCTYPES} docs per call")
		can_doc = []
		for d in docs:
			if not isinstance(d, dict):
				raise InvalidArgumentError("each docs entry must be a {doctype, name} object")
			dt, nm = d.get("doctype"), d.get("name")
			if not isinstance(dt, str) or not frappe.db.exists("DocType", dt):
				raise InvalidArgumentError(f"unknown DocType: {dt!r}")
			if not isinstance(nm, str) or not frappe.db.exists(dt, nm):
				raise InvalidArgumentError(f"unknown {dt} record: {nm!r}")
			can_doc.append({"doctype": dt, "name": nm, "can": _record_capabilities(dt, nm, user)})
		result["can_doc"] = can_doc

	return result
