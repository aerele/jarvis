"""jarvis__read_wiki: look up org wiki pages (Jarvis Wiki Page).

``slug`` returns ONE full page (body included); ``query`` searches Active
pages by slug/title/summary (LIKE) plus an exact ``ref_name`` match (so
"read the wiki on ACME Corp" finds ``customer--acme-corp`` by the record
name) and returns compact rows. Desk (System User) sessions only. Both paths
filter by the session user's scope visibility (Org pages for everyone, Role
pages for holders of the target role, User pages for their target user only;
System Manager sees all) via ``jarvis.chat.wiki_permissions``.

Both paths also carry ``manual_links``: human-curated ``[[link]]`` targets that
``add_wiki_link`` deliberately stores OUT of ``body_md`` so LLM re-ingest can't
clobber them. Without them here the agent stays blind to curation, because this
tool and the container mirror are its only two channels into the wiki. Targets
are resolved against the CALLER's own visibility, so a curated link never
discloses a page they may not read and never dangles.
"""

import json

import frappe
from frappe.utils import cint

from jarvis.chat import wiki_permissions
from jarvis.chat.wiki import WIKI_DISABLED_MESSAGE, is_stale, wiki_enabled
from jarvis.exceptions import FeatureDisabledError, InvalidArgumentError, PermissionDeniedError

WIKI = "Jarvis Wiki Page"
_MAX_LIMIT = 10


def _require_system_user() -> None:
	user = frappe.session.user
	if not user or user == "Guest":
		raise PermissionDeniedError("wiki tools require a signed-in desk user")
	if user == "Administrator":
		return
	if frappe.db.get_value("User", user, "user_type") != "System User":
		raise PermissionDeniedError("wiki tools require a desk (System User) session")


def read_wiki(query: str | None = None, slug: str | None = None, limit: int = 5):
	"""Read the org wiki: pass ``slug`` for one full page dict, or ``query``
	for a search returning ``[{slug, title, page_type, summary, stale,
	manual_links}]``."""
	_require_system_user()
	# #493: "Enable Business Wiki" is the operator's only wiki kill switch, and
	# nothing removes this tool from the agent's surface (registry.py builds a
	# static tuple at import; the plugin declares it statically), so the toggle has
	# to be honoured HERE or the agent keeps reading every Org page with the wiki
	# switched off. After the identity check on purpose: an unauthorised caller
	# still gets exactly the answer it got before this change.
	if not wiki_enabled():
		raise FeatureDisabledError(WIKI_DISABLED_MESSAGE)
	if slug:
		return _get_by_slug(str(slug))
	if query:
		return _search(str(query), max(1, min(cint(limit) or 5, _MAX_LIMIT)))
	raise InvalidArgumentError("pass slug (one full page) or query (search)")


def _get_by_slug(slug: str) -> dict:
	slug = slug.strip().lower()
	name = frappe.db.get_value(WIKI, {"slug": slug}, "name")
	if not name:
		raise InvalidArgumentError(f"unknown wiki page: {slug}")
	if not frappe.has_permission(WIKI, ptype="read", doc=name):
		# #733: byte-identical to the missing-row error above — a caller must
		# not be able to distinguish "no such page" from "exists but private".
		raise InvalidArgumentError(f"unknown wiki page: {slug}")
	doc = frappe.get_doc(WIKI, name)
	# Scope visibility (explicit, on top of the has_permission hook): a Role/
	# User page is only readable by its audience.
	if not wiki_permissions.can_read_page(doc, frappe.session.user):
		raise InvalidArgumentError(f"unknown wiki page: {slug}")
	try:
		sources = json.loads(doc.sources) if doc.sources else []
	except Exception:
		sources = []
	return {
		"slug": doc.slug,
		"title": doc.title,
		"page_type": doc.page_type,
		"ref_doctype": doc.ref_doctype,
		"ref_name": doc.ref_name,
		"summary": doc.summary,
		"body_md": doc.body_md,
		"sensitivity": doc.sensitivity,
		"status": doc.status,
		"sources": sources if isinstance(sources, list) else [],
		"last_confirmed_at": str(doc.last_confirmed_at) if doc.last_confirmed_at else None,
		"contradiction_flag": cint(doc.contradiction_flag),
		"stale": is_stale(doc.last_confirmed_at, doc.modified),
		"manual_links": _visible_links({doc.name: doc.manual_links})[doc.name],
	}


def _visible_links(by_page: dict) -> dict[str, list[str]]:
	"""``{page_name: manual_links JSON}`` -> ``{page_name: [target slug]}``,
	keeping only targets that exist as Active pages the session user may read.

	ONE query for the whole batch, using the same visibility fragment ``_search``
	splices in. Curated links are stored as bare slugs with no re-check after the
	write, so echoing them unfiltered would disclose a page whose scope has since
	narrowed — and would hand the agent links it cannot follow."""
	from jarvis.chat.wiki import _parse_manual_links

	parsed = {name: _parse_manual_links(raw) for name, raw in by_page.items()}
	wanted = sorted({t for targets in parsed.values() for t in targets})
	if not wanted:
		return {name: [] for name in parsed}

	where = "status = 'Active'"
	vis = (wiki_permissions.visible_scope_condition(frappe.session.user) or "").strip()
	if vis:
		where += f" and ({vis})"
	rows = frappe.db.sql(
		f"select slug from `tabJarvis Wiki Page` where {where} and slug in %(slugs)s",
		{"slugs": wanted},
		as_dict=True,
	)
	allowed = {r.slug for r in rows}
	return {name: [t for t in targets if t in allowed] for name, targets in parsed.items()}


def _search(query: str, limit: int) -> list[dict]:
	query = query.strip()
	if not query:
		raise InvalidArgumentError("query must not be empty")
	if not frappe.has_permission(WIKI, ptype="read"):
		raise PermissionDeniedError(f"no read permission on {WIKI}")
	# Raw SQL: the scope-visibility fragment (pre-escaped by
	# wiki_permissions) doesn't fit get_all's filters, and post-filtering
	# would silently shrink the LIMIT.
	where = "status = 'Active'"
	vis = (wiki_permissions.visible_scope_condition(frappe.session.user) or "").strip()
	if vis:
		where += f" and ({vis})"
	rows = frappe.db.sql(
		f"""select name, slug, title, page_type, summary, manual_links,
			last_confirmed_at, modified
		from `tabJarvis Wiki Page`
		where {where}
			and (slug like %(like)s or title like %(like)s
				or summary like %(like)s or ref_name = %(query)s)
		order by modified desc
		limit %(limit)s""",
		{"like": f"%{query[:140]}%", "query": query, "limit": limit},
		as_dict=True,
	)
	links = _visible_links({r.name: r.manual_links for r in rows})
	return [
		{
			"slug": r.slug,
			"title": r.title,
			"page_type": r.page_type,
			"summary": r.summary,
			"stale": is_stale(r.get("last_confirmed_at"), r.get("modified")),
			"manual_links": links[r.name],
		}
		for r in rows
	]
