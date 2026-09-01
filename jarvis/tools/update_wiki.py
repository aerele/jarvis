"""jarvis__update_wiki: create or update one wiki page (Jarvis Wiki Page).

Prefer ``append_md`` (adds a section, keeps everything already recorded) over
``replace_body_md`` (full rewrite); passing both is rejected so an intent is
never half-applied. Creating a NEW page additionally requires ``title`` and
``page_type``; the slug grammar is enforced by the doctype controller. Desk
(System User) sessions only. Registered as a gated write in jarvis.api
(_WRITE_TOOLS + _GATED_WRITES, never auto-applied).

Scopes (wiki v2): optional ``scope`` — "Org" (default) or "User".
- Org keeps the deliberate v1 LLM-channel behavior: ANY desk user maintains
  org pages through this confirm-gated tool (the human write matrix only
  governs the SPA/desk channel), so writes go ``ignore_permissions=True``
  behind the explicit checks here + the controller sanitizer.
- User scope requires app access (any ``Jarvis User``; the old Knowledge Wiki
  User role was retired) and always targets the CALLER's personal namespace:
  ``target_user`` is forced to the session user and the controller suffixes
  the slug
  (``<slug>--u-…``), so follow-up calls with the original base slug resolve
  back to the same personal page.
- Updating an existing Role/User page (by exact slug) follows the human
  write matrix (``wiki_permissions.can_edit_page``).
"""

import frappe

from jarvis.chat import wiki_permissions
from jarvis.chat.wiki import PAGE_TYPES, WIKI_DISABLED_MESSAGE, append_source, wiki_enabled
from jarvis.exceptions import FeatureDisabledError, InvalidArgumentError, PermissionDeniedError
from jarvis.permissions import JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE

WIKI = "Jarvis Wiki Page"

_SCOPES = ("Org", "User")
# User-scope writes now ride the platform "Jarvis User" role (Knowledge Wiki
# User retired); Manager/Admin/SM hold or supersede it. Mirrors
# wiki_permissions._is_wiki_user.
_USER_SCOPE_ROLES = frozenset(
	{
		JARVIS_USER_ROLE,
		JARVIS_ADMIN_ROLE,
		wiki_permissions.WIKI_MANAGER_ROLE,
		"System Manager",
	}
)


def _require_system_user() -> None:
	user = frappe.session.user
	if not user or user == "Guest":
		raise PermissionDeniedError("wiki tools require a signed-in desk user")
	if user == "Administrator":
		return
	if frappe.db.get_value("User", user, "user_type") != "System User":
		raise PermissionDeniedError("wiki tools require a desk (System User) session")


def _find_existing(slug: str, scope: str, user: str) -> str | None:
	"""Resolve the page this call targets. ``scope="User"`` targets the
	caller's personal namespace: an exact slug match only counts when it IS
	the caller's own User page, and the controller-suffixed variant
	(``<slug>--u-…``) is probed so follow-up calls with the original base
	slug keep landing on the same page. Slug grammar has no LIKE
	metacharacters, so the pattern is literal."""
	row = frappe.db.get_value(WIKI, {"slug": slug}, ["name", "scope", "target_user"], as_dict=True)
	if scope != "User":
		return row.name if row else None
	if row and (row.scope or "Org") == "User" and row.target_user == user:
		return row.name
	return frappe.db.get_value(
		WIKI,
		{"scope": "User", "target_user": user, "slug": ["like", f"{slug}--u-%"]},
		"name",
	)


def update_wiki(
	slug: str,
	title: str | None = None,
	page_type: str | None = None,
	ref_doctype: str | None = None,
	ref_name: str | None = None,
	summary: str | None = None,
	append_md: str | None = None,
	replace_body_md: str | None = None,
	scope: str = "Org",
) -> dict:
	"""Create or update the wiki page ``slug``. ``append_md`` appends a
	section to the existing body (preferred); ``replace_body_md`` rewrites it.
	``scope="User"`` writes the caller's personal page instead of the org
	wiki (any Jarvis User). Returns a compact summary of the saved page."""
	_require_system_user()
	# #493: the operator toggle has to refuse the write channel too, or turning the
	# wiki off leaves the agent still creating pages on request. api._run_tool
	# carries the same gate BEFORE the confirmation park (so a workspace with the
	# wiki off never raises a card whose only outcome is this refusal); this one
	# catches every other dispatch path, including confirming a card that was
	# parked before the toggle was flipped.
	if not wiki_enabled():
		raise FeatureDisabledError(WIKI_DISABLED_MESSAGE)
	user = frappe.session.user

	slug = (slug or "").strip().lower()
	if not slug:
		raise InvalidArgumentError("slug is required")
	if append_md and replace_body_md:
		raise InvalidArgumentError("pass either append_md or replace_body_md, not both")
	if page_type and page_type not in PAGE_TYPES:
		raise InvalidArgumentError(f"page_type must be one of: {', '.join(PAGE_TYPES)}")
	scope = (str(scope).strip() if scope else "") or "Org"
	scope = scope.capitalize()
	if scope not in _SCOPES:
		raise InvalidArgumentError('scope must be "Org" or "User"')
	if scope == "User" and user != "Administrator" and not (_USER_SCOPE_ROLES & set(frappe.get_roles(user))):
		return {
			"ok": False,
			"reason": ("personal (User-scope) wiki pages require Jarvis app access (the Jarvis User role)"),
		}

	name = _find_existing(slug, scope, user)
	if name:
		doc = frappe.get_doc(WIKI, name)
		# Org pages: preserved v1 LLM-channel behavior — any desk user may
		# maintain them through this confirm-gated tool (the explicit checks
		# here + ignore_permissions replace the old doctype-perm probe, since
		# write moved off Desk User). Role/User pages follow the human matrix.
		if (doc.get("scope") or "Org") != "Org" and not wiki_permissions.can_edit_page(doc, user):
			# #733: never let a forbidden existing page fall through to the
			# create branch below — hitting the unique-slug duplicate-key
			# error there would be an even louder existence leak. Mask it
			# the same way a genuinely unknown slug is handled: the same
			# missing-title/page_type message when a create was not even
			# attempted, otherwise the same "unknown" message read_wiki uses.
			if not (title and str(title).strip()) or not page_type:
				raise InvalidArgumentError("creating a new wiki page requires title and page_type")
			raise InvalidArgumentError(f"unknown wiki page: {slug}")
		created = False
		if title and str(title).strip():
			doc.title = str(title).strip()[:140]
		if page_type:
			doc.page_type = page_type
		if ref_doctype is not None and str(ref_doctype).strip():
			doc.ref_doctype = str(ref_doctype).strip()
		if ref_name is not None and str(ref_name).strip():
			doc.ref_name = str(ref_name).strip()
		if summary is not None:
			doc.summary = " ".join(str(summary).split()) or None
		if append_md and str(append_md).strip():
			existing = (doc.body_md or "").strip()
			doc.body_md = f"{existing}\n\n{str(append_md).strip()}".strip()
		elif replace_body_md is not None:
			doc.body_md = str(replace_body_md)
		append_source(doc, "tool", None, user)
		doc.last_confirmed_at = frappe.utils.now_datetime()
		doc.save(ignore_permissions=True)
	else:
		if not (title and str(title).strip()) or not page_type:
			raise InvalidArgumentError("creating a new wiki page requires title and page_type")
		created = True
		doc = frappe.get_doc(
			{
				"doctype": WIKI,
				"slug": slug,
				"title": str(title).strip()[:140],
				"page_type": page_type,
				"ref_doctype": (str(ref_doctype).strip() if ref_doctype else None),
				"ref_name": (str(ref_name).strip() if ref_name else None),
				"summary": (" ".join(str(summary).split()) or None) if summary is not None else None,
				"body_md": str(replace_body_md or append_md or "").strip(),
				"status": "Active",
				"scope": scope,
				"target_user": user if scope == "User" else None,
				"last_confirmed_at": frappe.utils.now_datetime(),
			}
		)
		append_source(doc, "tool", None, user)
		doc.insert(ignore_permissions=True)

	return {
		"ok": True,
		"created": created,
		"slug": doc.slug,
		"title": doc.title,
		"page_type": doc.page_type,
		"scope": doc.get("scope") or "Org",
		"status": doc.status,
		"body_chars": len(doc.body_md or ""),
	}
