"""Push a compact User/Role/Org wiki-utilization graph to admin (wiki v2 D3).

The site DB is canonical (``Jarvis Wiki Page``); this renders a bounded node/edge
STRUCTURE payload — the scope tiers (Org/Role/User), the authored-from-provenance
edges, and user→role membership resolved via ``Has Role`` — and POSTs it to the
control plane (``jarvis.admin_client.push_wiki_graph`` → jarvis_admin relay →
``Jarvis Wiki Graph Snapshot``). Admin overlays READ/WRITE + latent-demand
activity from openclaw telemetry it already reads; this push carries only the
DB-only tiers (roles, scope, page metadata) that never otherwise leave the site
— Role/User pages are never mirrored to the container. Derived + rebuildable;
NEVER raises into the save/sync path.
"""

from __future__ import annotations

import json
import re

import frappe
from frappe.utils import cint

from jarvis.chat.wiki import PAGE_TYPES, is_stale

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
	"""``manual_links`` (JSON list of target slugs) → the ones that exist. The
	durable, out-of-body half of the link set (R1)."""
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
	return out


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
	return _build_graph_from_pages(pages)


def _build_graph_from_pages(pages, include_content: bool = False) -> dict:
	"""Bounded ``{nodes, edges, counts}`` from an already-fetched page list.

	Shared by ``compute_graph`` (org-wide → admin push) and ``get_wiki_graph``
	(caller-scoped → tenant SPA). Nodes: one ``org``; ``role`` nodes per
	``target_role``; ``user`` nodes for authors + User-page targets; a ``page``
	node per page (slug, title, type, scope, stale/contradiction; +summary when
	``include_content``). Edges: ``scope`` (page→tier), ``authored`` (user→page,
	weighted), ``member-of`` (user→role via live ``frappe.get_roles``),
	``links-to`` (body ``[[links]]`` ∪ durable ``manual_links``). Callers pass a
	page set already filtered to what they may emit, so edges to pages outside
	the set are dropped by construction (the isolation invariant, R3)."""
	nodes: list[dict] = [{"id": _ORG_ID, "kind": "org", "label": _org_label()}]
	edges: list[dict] = []
	role_ids: set[str] = set()
	user_ids: set[str] = set()
	known_slugs = {p.name for p in pages}
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
		pid = f"page:{slug}"
		scope = _norm_scope(p.scope)
		node = {
			"id": pid,
			"kind": "page",
			"label": p.title or slug,
			"slug": slug,
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
		for t in _manual_link_targets(p.get("manual_links"), known_slugs):
			if t not in targets:
				targets.append(t)
		for target in targets:
			if target != slug:
				edges.append({"source": pid, "target": f"page:{target}", "kind": "links-to"})
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
