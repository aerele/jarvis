"""Push a compact User/Role/Org wiki-utilization graph to admin (wiki v2 D3).

The site DB is canonical (``Jarvis Wiki Page``); this renders a bounded node/edge
STRUCTURE payload — the scope tiers (Org/Role/User), the authored-from-provenance
edges, and user→role membership resolved via ``Has Role`` — and POSTs it to the
control plane (``jarvis.admin_client.push_wiki_graph`` → jarvis_admin relay →
``Jarvis Wiki Graph Snapshot``). Admin overlays READ/WRITE + latent-demand
activity from agent telemetry it already reads; this push carries only the
DB-only tiers (roles, scope, page metadata) that never otherwise leave the site
— Role/User pages are never mirrored to the container. Derived + rebuildable;
NEVER raises into the save/sync path.

What actually travels to admin, stated plainly (#495: "page metadata" glossed
over the fact that a title is authored content):

* Org pages: real title and real slug. They are org-wide by definition, and they
  are what make the vendor console legible.
* Role pages: real title and real slug, deliberately. A Role page is authored for
  a named group audience whose role name already travels as its own node, and the
  console joins its READ/WRITE activity overlay on the slug, so minimising here
  would go beyond what #495 asked and blind a working view. Widening is one line:
  add "Role" to ``_MINIMISED_SCOPES``.
* User pages: NEITHER title nor slug. Both are replaced by a stable opaque ref
  (``_opaque_page_ref``) because a personal page titled "salary dispute with
  manager X" is authored free text and the slug derives from the title.
* Owning + authoring user EMAILS still travel, as ``user:`` node labels. #495
  rules that out of scope on the ground that the usage rollup ingest already
  sends them.
* Page bodies and summaries never travel (``include_content``), and never have.

The whole push is short-circuited when the "Enable Business Wiki" toggle is off
(#493), before any of the above is computed.
"""

from __future__ import annotations

import hashlib
import json
import re

import frappe
from frappe.utils import cint

from jarvis.chat.wiki import PAGE_TYPES, WIKI_DISABLED_REASON, is_stale, wiki_enabled

# Obsidian-style wikilink in a page body: [[slug]], [[slug|alias]], [[slug#h]].
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_MAX_LINKS_PER_PAGE = 50

WIKI = "Jarvis Wiki Page"
SETTINGS = "Jarvis Settings"

JOB_METHOD = "jarvis.chat.wiki_graph.sync"
JOB_ID = "wiki-graph-sync"
QUEUE = "short"
JOB_TIMEOUT_S = 120

# Defensive caps — the admin ingest re-bounds these, but a bench must never emit
# an unbounded payload. A normal org sits far under these.
MAX_PAGES = 2000
MAX_USERS = 500
MAX_EDGES = 20000

_PAGE_FIELDS = [
	"name",
	"title",
	"page_type",
	"scope",
	"target_role",
	"target_user",
	"sources",
	"status",
	"last_confirmed_at",
	"contradiction_flag",
	"modified",
	"creation",
	# manual_links: human-curated [[links]] kept OUT of body_md so LLM re-ingest
	# (which full-replaces body_md) can't clobber them; unioned into links-to.
	"manual_links",
	# body_md is read to extract [[wikilink]] edges tenant-side; it is NEVER
	# put on a node (only the derived page→page edges travel to admin).
	"body_md",
]

# Scope value → the node an Org/Role/User page is "covered by".
_ORG_ID = "org"

# #495: scopes whose page title AND slug are replaced by an opaque ref in the
# ADMIN push. See the module docstring for why Role is not in here; adding it is
# the one-line lever if that judgement is ever overruled.
_MINIMISED_SCOPES = frozenset({"User"})
_OPAQUE_REF_CHARS = 10


def _norm_scope(scope) -> str:
	"""NULL/'' → Org (pre-v2 rows), else the stored scope."""
	return (scope or "").strip() or "Org"


def _org_label() -> str:
	"""Best-effort human name for the org node; admin can override from the
	Jarvis Customer. Falls back to the site name."""
	company = frappe.defaults.get_global_default("company")
	return company or frappe.local.site or "Organization"


def _extract_link_targets(body, known: set[str]) -> list[str]:
	"""``[[slug]]`` wikilinks in a body → target slugs, keeping only links to
	pages that exist (Obsidian's graph drops dangling links) and capping per
	page. ``[[slug|alias]]`` / ``[[slug#heading]]`` normalize to ``slug``."""
	out: list[str] = []
	seen: set[str] = set()
	for raw in _WIKILINK_RE.findall(body or ""):
		target = raw.split("|", 1)[0].split("#", 1)[0].strip().lower()
		if target and target in known and target not in seen:
			seen.add(target)
			out.append(target)
			if len(out) >= _MAX_LINKS_PER_PAGE:
				break
	return out


def _manual_link_targets(raw, known: set[str]) -> list[str]:
	"""``manual_links`` (JSON list of target slugs) → the ones that exist, capped
	per page. The durable, out-of-body half of the link set (R1).

	#645: capped at the same ``_MAX_LINKS_PER_PAGE`` the body links already use, which
	is also what the mirror caps its ``## Related`` tail at (``wiki_mirror._MAX_RELATED``).
	The asymmetry was harmless while nothing wrote ``manual_links``; PR #642 wired the
	add-link loop, so they now accumulate one click at a time and are never pruned. Left
	uncapped, one heavily curated page can take a large share of the global ``MAX_EDGES``
	budget and crowd every other page out of the graph, and the daily push to the control
	plane grows with no per-page bound.

	Capped separately rather than sharing one budget with the body links, so a page with
	many ``[[wikilinks]]`` in its body cannot silently swallow a human's deliberate
	curation. A page therefore contributes at most ``2 * _MAX_LINKS_PER_PAGE`` link
	edges.

	Keeps the NEWEST, not the oldest. ``add_wiki_link`` APPENDS, so truncating the head
	would mean that past the cap a user clicks "+ link", is told it succeeded, the link
	is durably stored, and it never appears in the graph. Dropping the oldest edge is a
	bounded, understandable loss; silently discarding the one the user just made is
	not."""
	try:
		arr = json.loads(raw) if isinstance(raw, str) else (raw or [])
	except Exception:
		return []
	if not isinstance(arr, list):
		return []
	out: list[str] = []
	for t in arr:
		s = str(t or "").strip().lower()
		if s and s in known and s not in out:
			out.append(s)
	return out[-_MAX_LINKS_PER_PAGE:]


def _opaque_page_ref(p, scope: str) -> tuple[str, str]:
	"""``(slug, label)`` a scope-minimised page travels to admin under (#495).

	BOTH fields are replaced, because a slug derives from the title
	(``jarvis_wiki_page._apply_scope_slug_suffix``): minimising the label while
	still sending the slug would carry the same authored text one field over and
	defeat the whole change.

	STABLE across pushes by construction. The digest is taken over the docname,
	which ``autoname: field:slug`` freezes at create time, so the console's graph
	shows the same node every day and its history stays worth keeping. Nothing here
	is a counter, a timestamp or an iteration order.

	The audience part (``--u-<localpart>`` / ``--r-<role>``) derives from the
	target, never from the title, and is kept for legibility: it lets a vendor
	operator still see WHOSE tier a node sits in, which is what the admin view is
	for. Two pages of the same owner therefore share a LABEL and differ only by id,
	which is the intended reading: "this person has four personal pages" without
	what they are about.

	That audience part is normally redundant, since the same person is already on
	the graph as a ``user:`` node labelled with their full email. ONE case where it
	is not: past ``MAX_USERS`` distinct authors ``_user_node`` stops minting nodes
	and the scope edge falls back to org, so a capped-out owner's local part rides
	here with no ``user:`` node beside it. Accepted rather than fixed. Suppressing
	it conditionally would make the ref depend on how many OTHER pages were in the
	push, which breaks the stability this whole helper exists to provide, and a
	local part is strictly less than the title that used to travel in its place.

	``SLUG_RE`` forbids a leading dash, so a synthetic ref beginning with ``--``
	can never collide with a real page's slug and steal its activity overlay."""
	digest = hashlib.sha256(str(p.name or "").encode("utf-8")).hexdigest()[:_OPAQUE_REF_CHARS]
	if scope == "Role":
		role = str(p.target_role or "").strip()
		return (f"--r-{digest}", f"Role page ({role})" if role else "Role page")
	local = str(p.target_user or "").split("@", 1)[0].strip().lower()
	if not local:
		return f"--u-{digest}", "User page"
	return f"--u-{local}-{digest}", f"User page (--u-{local})"


def _emitted_ref(p, minimise_scopes: bool) -> tuple[str, str]:
	"""``(slug, label)`` this page is emitted under.

	The real slug and real title unless the CALLER asked for minimisation and the
	page's scope is in ``_MINIMISED_SCOPES``. The tenant SPA path never asks, so it
	is byte-identical to before #495."""
	slug = p.name
	if minimise_scopes and _norm_scope(p.scope) in _MINIMISED_SCOPES:
		return _opaque_page_ref(p, _norm_scope(p.scope))
	return slug, (p.title or slug)


def _sources_authors(raw) -> dict[str, int]:
	"""``sources`` JSON ([{date, kind, ref, user}]) → {user: authored_count}.
	Corrupt JSON contributes nothing rather than failing the compute."""
	try:
		entries = json.loads(raw) if raw else []
	except Exception:
		return {}
	if not isinstance(entries, list):
		return {}
	counts: dict[str, int] = {}
	for entry in entries:
		if not isinstance(entry, dict):
			continue
		user = (entry.get("user") or "").strip()
		if user:
			counts[user] = counts.get(user, 0) + 1
	return counts


def compute_graph() -> dict:
	"""Org-wide graph for the admin push (all scopes, no page content). Bounded
	``{nodes, edges, counts}`` — see ``_build_graph_from_pages`` for the shape."""
	pages = frappe.get_all(
		WIKI,
		filters={"status": "Active"},
		fields=_PAGE_FIELDS,
		order_by="modified desc",
		limit=MAX_PAGES,
	)
	# minimise_scopes: this is the ADMIN-bound graph (#495). The tenant SPA builds
	# its own through _build_graph_from_pages and keeps real titles.
	return _build_graph_from_pages(pages, minimise_scopes=True)


def _build_graph_from_pages(pages, include_content: bool = False, minimise_scopes: bool = False) -> dict:
	"""Bounded ``{nodes, edges, counts}`` from an already-fetched page list.

	Shared by ``compute_graph`` (org-wide → admin push) and ``get_wiki_graph``
	(caller-scoped → tenant SPA). Nodes: one ``org``; ``role`` nodes per
	``target_role``; ``user`` nodes for authors + User-page targets; a ``page``
	node per page (slug, title, type, scope, stale/contradiction; +summary and
	+curated ``manual_links`` targets when ``include_content`` (#644, so the
	tenant detail panel can offer removal of exactly what it curated)). Edges:
	``scope`` (page→tier), ``authored`` (user→page,
	weighted), ``member-of`` (user→role via live ``frappe.get_roles``),
	``links-to`` (body ``[[links]]`` ∪ durable ``manual_links``). Callers pass a
	page set already filtered to what they may emit, so edges to pages outside
	the set are dropped by construction (the isolation invariant, R3).

	``minimise_scopes`` (#495, admin push only) replaces the title AND slug of a
	``_MINIMISED_SCOPES`` page with a stable opaque ref. Left False every node is
	exactly what it was before #495."""
	nodes: list[dict] = [{"id": _ORG_ID, "kind": "org", "label": _org_label()}]
	edges: list[dict] = []
	role_ids: set[str] = set()
	user_ids: set[str] = set()
	known_slugs = {p.name for p in pages}
	# Resolved for EVERY page UP FRONT (#495): links-to edges address other pages by
	# id, so a per-page substitution decided inside the loop would leave every
	# inbound edge still pointing at the authored slug.
	emitted = {p.name: _emitted_ref(p, minimise_scopes) for p in pages}
	link_edges = 0

	def _role_node(role: str) -> str:
		rid = f"role:{role}"
		if rid not in role_ids:
			role_ids.add(rid)
			nodes.append({"id": rid, "kind": "role", "label": role})
		return rid

	def _user_node(user: str) -> str:
		uid = f"user:{user}"
		if uid not in user_ids and len(user_ids) < MAX_USERS:
			user_ids.add(uid)
			nodes.append({"id": uid, "kind": "user", "label": user})
		return uid

	for p in pages:
		slug = p.name
		emit_slug, emit_label = emitted[slug]
		pid = f"page:{emit_slug}"
		scope = _norm_scope(p.scope)
		# Computed once, up front, so both the node's curated-links list below and
		# the links-to edges further down read the same value.
		manual_targets = _manual_link_targets(p.get("manual_links"), known_slugs)
		node = {
			"id": pid,
			"kind": "page",
			"label": emit_label,
			"slug": emit_slug,
			"page_type": p.page_type if p.page_type in PAGE_TYPES else "Org",
			"scope": scope,
			"stale": is_stale(p.last_confirmed_at, p.modified),
			"contradiction": bool(cint(p.contradiction_flag)),
		}
		# creation date for the Evolution timeline (day precision) — needed by
		# BOTH pushes (admin's daily Evolution tab has no other source); summary
		# stays gated on include_content (content must not travel to admin).
		node["created"] = str(p.get("creation") or "")[:10]
		if include_content:
			# tenant SPA needs content for client-side TF-IDF similarity.
			node["summary"] = p.get("summary") or ""
			# #644: the curated (manual_links) targets only, never body-derived;
			# what the tenant detail panel offers a "remove" affordance on, since
			# that is exactly what remove_wiki_link mutates. Plain slugs, not
			# `emitted[...]`: include_content is only ever True on this
			# (minimise_scopes=False) tenant path, so emission is identity here.
			# Every entry already passed through `known_slugs` (the caller's own
			# visible-page set) inside `_manual_link_targets`, so this can never
			# leak a target slug the caller isn't allowed to see (R3).
			node["manual_links"] = [t for t in manual_targets if t != slug]
		nodes.append(node)

		# scope-covers edge: page → the tier that can see it. A User-scope page
		# whose target user didn't get a node (MAX_USERS cap) falls back to org
		# so no page is left tierless and no edge dangles.
		if scope == "Role" and p.target_role:
			edges.append({"source": pid, "target": _role_node(p.target_role), "kind": "scope"})
		elif scope == "User" and p.target_user:
			uid = _user_node(p.target_user)
			edges.append(
				{
					"source": pid,
					"target": uid if uid in user_ids else _ORG_ID,
					"kind": "scope",
				}
			)
		else:
			edges.append({"source": pid, "target": _ORG_ID, "kind": "scope"})

		# authored edges from provenance.
		for user, count in _sources_authors(p.sources).items():
			uid = _user_node(user)
			if uid in user_ids:
				edges.append({"source": uid, "target": pid, "kind": "authored", "weight": count})

		# page→page links — the Obsidian knowledge graph itself: body [[wikilinks]]
		# ∪ durable out-of-body manual_links (R1), deduped.
		targets = _extract_link_targets(p.body_md, known_slugs)
		for t in manual_targets:
			if t not in targets:
				targets.append(t)
		for target in targets:
			if target != slug:
				# Through ``emitted`` (#495), never the raw target: an inbound edge to a
				# minimised page would otherwise re-publish the slug this change hides.
				# Every target came from ``known_slugs``, so the lookup always resolves.
				edges.append({"source": pid, "target": f"page:{emitted[target][0]}", "kind": "links-to"})
				link_edges += 1

	# member-of: connect each user to the role nodes they actually hold (roles
	# that have wiki pages). Resolved now — role assignments are mutable.
	role_labels = {rid.split(":", 1)[1] for rid in role_ids}
	if role_labels:
		for uid in list(user_ids):
			user = uid.split(":", 1)[1]
			try:
				held = set(frappe.get_roles(user))
			except Exception:
				continue
			for role in role_labels & held:
				edges.append({"source": uid, "target": f"role:{role}", "kind": "member-of"})

	if len(edges) > MAX_EDGES:
		edges = edges[:MAX_EDGES]
		frappe.log_error(title="wiki graph: edge cap hit", message=f"pages={len(pages)}")

	return {
		"nodes": nodes,
		"edges": edges,
		"counts": {
			"pages": len(pages),
			"authors": len(user_ids),
			"roles": len(role_ids),
			"links": link_edges,
		},
	}


HISTORY_DT = "Jarvis Wiki Graph History"


def record_history_snapshot() -> dict:
	"""Daily: append today's org-wide graph totals to ``Jarvis Wiki Graph History``
	(one row per day, idempotent). This is the MEASURED Knowledge-Evolution series
	— real link growth + orphan decline over time — which the Evolution tab prefers
	over the reconstructed-from-creation-dates fallback. Derived + rebuildable;
	never raises into the scheduler."""
	# #493: a disabled wiki records nothing. Tenant-side only, so no data leaves the
	# site either way, but the toggle is meant to stop the wiki DOING things and the
	# issue names this job explicitly.
	if not wiki_enabled():
		return {"ok": False, "reason": WIKI_DISABLED_REASON}
	try:
		return _record_history_snapshot()
	except Exception:
		frappe.log_error(title="wiki graph: history snapshot crashed", message=frappe.get_traceback())
		return {"ok": False, "reason": "history snapshot crashed; see Error Log"}


def _record_history_snapshot() -> dict:
	g = compute_graph()
	pages = [n for n in g["nodes"] if n.get("kind") == "page"]
	linked: set[str] = set()
	for e in g["edges"]:
		if e.get("kind") == "links-to":
			linked.add(e["source"])
			linked.add(e["target"])
	stats = {
		"pages": len(pages),
		"links": g["counts"]["links"],
		"orphans": sum(1 for n in pages if n["id"] not in linked),
		"stale": sum(1 for n in pages if n.get("stale")),
		"contradictions": sum(1 for n in pages if n.get("contradiction")),
	}
	today = frappe.utils.today()
	# Idempotent per day: the row is named by its date, so a re-run overwrites
	# today's totals rather than appending a duplicate.
	if frappe.db.exists(HISTORY_DT, today):
		doc = frappe.get_doc(HISTORY_DT, today)
		doc.update(stats)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": HISTORY_DT, "snapshot_date": today, **stats})
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "date": today, **stats}


def sync() -> dict:
	"""Compute + push the graph. Swallows + logs; never raises into callers."""
	# #493: no page metadata leaves a workspace whose wiki is switched off. Checked
	# BEFORE _sync, so a disabled workspace does no graph computation at all rather
	# than building a payload (minimisation included) and then discarding it.
	if not wiki_enabled():
		return {"ok": False, "reason": WIKI_DISABLED_REASON}
	try:
		return _sync()
	except Exception:
		frappe.log_error(title="wiki graph: sync crashed", message=frappe.get_traceback())
		return {"ok": False, "reason": "sync crashed; see Error Log"}


def _sync() -> dict:
	from jarvis import admin_client

	payload = compute_graph()
	resp = admin_client.push_wiki_graph(payload)
	if resp is None:
		return {"ok": False, "reason": "admin/tenant unreachable; will retry next sync"}
	return {"ok": True, **payload["counts"]}


def enqueue_sync() -> None:
	"""Queue the deduped graph sync (short queue). Suppressed under tests;
	enqueue failures (Redis down) are swallowed."""
	# #493: checked BEFORE the in_test suppression so a test that opts into real
	# enqueues still sees the kill switch.
	if not wiki_enabled():
		return
	if frappe.flags.in_test and not frappe.flags.get("jarvis_test_wiki_graph_enqueue"):
		return
	try:
		frappe.enqueue(
			JOB_METHOD,
			queue=QUEUE,
			timeout=JOB_TIMEOUT_S,
			job_id=JOB_ID,
			deduplicate=True,
		)
	except Exception:
		frappe.log_error(title="wiki graph: enqueue failed", message=frappe.get_traceback())


@frappe.whitelist()
def sync_now() -> dict:
	"""Operator 'Sync now' from the Wiki tab (Jarvis Admin / System Manager —
	PART 4 REVISED, TASK 45)."""
	from jarvis.permissions import require_jarvis_admin

	require_jarvis_admin()
	return sync()
