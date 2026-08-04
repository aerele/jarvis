"""One-way markdown mirror of the Org-scope wiki into the tenant container
workspace (wiki v2 D2).

The site DB stays canonical (``Jarvis Wiki Page``); this module renders the
Org-scope Active pages as Obsidian-style markdown and reconciles them into the
container workspace ``wiki/`` directory through the no-restart push chain
``jarvis.admin_client.push_wiki_files`` -> jarvis_admin relay -> fleet-agent
``PUT /wiki-files`` (the workspace is a live RW bind mount, so writes appear
to the running agent instantly). The mirror is a DERIVED, rebuildable copy for
cheap native file reads/greps; ``jarvis__read_wiki`` stays authoritative.

Scope discipline: only Org pages (scope NULL/'' counts as Org) are ever
mirrored — the container workspace is org-shared, so Role/User pages must
never land there. That runs both ways: narrowing a mirrored page to Role/User
is a revocation, so its file is deleted on the next sync (there is no periodic
sweep to fall back on). Diffing rides ``mirror_hash`` (sha256 of the rendered
content, stamped per page via ``frappe.db.set_value`` — deliberately NOT
``doc.save``, which would re-fire the very doc_events that trigger the sync).

Renders return workspace-relative paths (``wiki/<typedir>/<slug>.md``); the
wire payload strips the ``wiki/`` prefix — the fleet endpoint takes paths
relative to its wiki dir (e.g. ``customers/customer--acme.md``, ``index.md``).

Every failure path swallows + logs (``frappe.log_error``): the doc_events
trigger runs inside user save paths and the sync job must degrade to "try
again next sync" when the tenant is not provisioned or admin is unreachable.
"""

from __future__ import annotations

import base64
import hashlib
import json

import frappe
from frappe.utils import cint

WIKI = "Jarvis Wiki Page"
SETTINGS = "Jarvis Settings"

JOB_METHOD = "jarvis.chat.wiki_mirror.sync"
# Two dedup ids so a queued incremental sync can't swallow a requested FULL
# sync (manual "Sync now" / on_trash prune both need known_paths).
JOB_ID = "wiki-mirror-sync"
JOB_ID_FULL = "wiki-mirror-sync-full"
QUEUE = "short"
JOB_TIMEOUT_S = 120

# fleet-agent hard-caps request bodies at 256KB; keep each push call's b64
# file payload comfortably under it.
MAX_CALL_PAYLOAD_BYTES = 200 * 1024
_PER_FILE_OVERHEAD_BYTES = 64

# page_type -> mirror subdirectory. Keys mirror the 9 options of
# ``Jarvis Wiki Page.page_type`` (jarvis_wiki_page.json) / wiki.PAGE_TYPES.
TYPE_DIRS = {
	"Customer": "customers",
	"Supplier": "suppliers",
	"Item": "items",
	"Process": "processes",
	"Doctype": "doctypes",
	"Exception": "exceptions",
	"Integration": "integrations",
	"People": "people",
	"Org": "org",
}
_DEFAULT_TYPE_DIR = "org"

INDEX_PATH = "wiki/index.md"
LOG_PATH = "wiki/log.md"
_INDEX_SUMMARY_CHARS = 100
_LOG_MAX_EVENTS = 150

_PAGE_FIELDS = [
	"name",
	"slug",
	"title",
	"page_type",
	"scope",
	"status",
	"summary",
	"body_md",
	"sources",
	"last_confirmed_at",
	"contradiction_flag",
	"modified",
	"mirror_hash",
]


def _is_org_scope(scope) -> bool:
	"""NULL/'' scope (pre-v2 rows) reads as Org everywhere."""
	return (scope or "").strip() in ("", "Org")


def _is_mirrorable(page) -> bool:
	"""Only Active Org pages belong on the org-shared container. Everything
	else (archived, or narrowed to Role/User) must have its file removed."""
	return _is_org_scope(page.get("scope")) and (page.get("status") or "Active") == "Active"


def _wire_path(path: str) -> str:
	"""Workspace-relative render path -> wire path relative to the fleet
	endpoint's wiki dir ("wiki/customers/x.md" -> "customers/x.md")."""
	return path[len("wiki/") :] if path.startswith("wiki/") else path


def page_path(page) -> str:
	"""Workspace-relative mirror path for one page (dict row or Document)."""
	type_dir = TYPE_DIRS.get((page.get("page_type") or "").strip(), _DEFAULT_TYPE_DIR)
	slug = page.get("slug") or page.get("name")
	return f"wiki/{type_dir}/{slug}.md"


# --------------------------------------------------------------------------- #
# renders (pure functions of page data; deterministic modulo the stale clock)
# --------------------------------------------------------------------------- #
def render_page(doc) -> tuple[str, str]:
	"""Render one page as Obsidian-style markdown. Returns ``(path, content)``
	with path ``wiki/<typedir>/<slug>.md``. Frontmatter carries the metadata
	the agent needs to trust-or-verify (stale/contradiction flags); the body's
	existing ``[[slug]]`` links pass through untouched; the provenance trail
	renders as a ``## Sources`` tail."""
	from jarvis.chat.wiki import is_stale

	stale = is_stale(doc.get("last_confirmed_at"), doc.get("modified"))
	lines = [
		"---",
		# json.dumps == a valid YAML double-quoted scalar; keeps a title with
		# ':' / quotes from corrupting the frontmatter.
		f"title: {json.dumps(str(doc.get('title') or ''))}",
		f"type: {(doc.get('page_type') or '').strip() or 'Org'}",
		f"updated: {str(doc.get('modified') or '')[:10]}",
		f"stale: {'true' if stale else 'false'}",
		f"contradiction: {'true' if cint(doc.get('contradiction_flag')) else 'false'}",
		"---",
		"",
	]
	summary = " ".join(str(doc.get("summary") or "").split())
	if summary:
		lines += [summary, ""]
	body = str(doc.get("body_md") or "").strip("\n")
	if body:
		lines += [body, ""]
	source_lines = _source_lines(doc.get("sources"))
	if source_lines:
		lines += ["## Sources", ""] + source_lines + [""]
	content = "\n".join(lines).rstrip("\n") + "\n"
	return page_path(doc), content


def _source_lines(raw) -> list[str]:
	"""``sources`` JSON ([{date, kind, ref, user}], already capped at 20 by
	the controller-side append) -> markdown bullets. Corrupt JSON renders as
	no tail rather than failing the sync."""
	try:
		entries = json.loads(raw) if raw else []
	except Exception:
		return []
	if not isinstance(entries, list):
		return []
	out = []
	for entry in entries:
		if not isinstance(entry, dict):
			continue
		parts = [str(entry.get(key)) for key in ("date", "kind", "ref", "user") if entry.get(key)]
		if parts:
			out.append("- " + " · ".join(parts))
	return out


def render_index() -> tuple[str, str]:
	"""``wiki/index.md``: Active Org pages grouped by type, one line per page
	``- [[slug]] — <summary ≤100ch>``, with a counts header. Surgically small
	on purpose — the index is a routing file, not content."""
	rows = frappe.get_all(
		WIKI,
		fields=["name", "page_type", "scope", "status", "summary"],
		order_by="name asc",
		limit_page_length=0,
	)
	pages = [r for r in rows if _is_mirrorable(r)]
	by_type: dict[str, list] = {}
	for r in pages:
		by_type.setdefault((r.page_type or "").strip() or "Org", []).append(r)

	lines = [
		"# Org wiki index",
		"",
		f"{len(pages)} active page(s). Each [[slug]] is a file under wiki/; "
		"use jarvis__read_wiki for the authoritative copy.",
		"",
	]
	# TYPE_DIRS order = the doctype's option order; unknown types (defensive)
	# trail alphabetically.
	ordered = [t for t in TYPE_DIRS if t in by_type]
	ordered += sorted(set(by_type) - set(TYPE_DIRS))
	for ptype in ordered:
		group = by_type[ptype]
		lines.append(f"## {ptype} ({len(group)})")
		lines.append("")
		for r in group:
			summary = " ".join(str(r.summary or "").split())[:_INDEX_SUMMARY_CHARS]
			lines.append(f"- [[{r.name}]] — {summary}" if summary else f"- [[{r.name}]]")
		lines.append("")
	return INDEX_PATH, "\n".join(lines).rstrip("\n") + "\n"


def render_log() -> tuple[str, str]:
	"""``wiki/log.md``: the last ``_LOG_MAX_EVENTS`` mirror-relevant events
	(page creation/modification/archival + the last lint run), newest first,
	one grep-able line per event: ``## [YYYY-MM-DD] <action> | <slug>``."""
	rows = frappe.get_all(
		WIKI,
		fields=["name", "scope", "status", "creation", "modified"],
		limit_page_length=0,
	)
	events: list[tuple[str, str, str]] = []
	for r in rows:
		if not _is_org_scope(r.scope):
			continue
		events.append((str(r.creation), "created", r.name))
		# A modified stamp beyond the same second as creation = a later write.
		if str(r.modified)[:19] != str(r.creation)[:19]:
			action = "archived" if (r.status or "") == "Archived" else "updated"
			events.append((str(r.modified), action, r.name))
	lint_at = frappe.db.get_single_value(SETTINGS, "wiki_lint_last_run_at")
	if lint_at:
		events.append((str(lint_at), "lint", "org-wiki"))
	events.sort(key=lambda e: e[0], reverse=True)

	lines = [
		"# Org wiki log",
		"",
		"Newest first. Grep `| <slug>` for one page's history.",
		"",
	]
	for ts, action, slug in events[:_LOG_MAX_EVENTS]:
		lines.append(f"## [{ts[:10]}] {action} | {slug}")
	return LOG_PATH, "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
def sync(full: bool = False) -> dict:
	"""Reconcile the mirror: push changed/new Org Active pages (mirror_hash
	sha256 diff; ``full`` bypasses the diff so a wiped container rebuilds),
	delete archived pages' files, always re-send index.md + log.md, and on
	``full`` send ``known_paths`` so the fleet prunes strays (trashed pages,
	type/dir moves). Returns a summary dict; NEVER raises into callers."""
	try:
		result = _sync(full=bool(full))
	except Exception:
		frappe.log_error(title="wiki mirror: sync crashed", message=frappe.get_traceback())
		result = {"ok": False, "reason": "sync crashed; see Error Log"}
	_stamp_sync_status(result)
	return result


def _stamp_sync_status(result: dict) -> None:
	"""Persist the outcome on Jarvis Settings so the Wiki tab can show a
	"last synced" line — otherwise the sync is fire-and-forget and a failed
	push surfaces nowhere in the SPA. Best-effort."""
	try:
		if result.get("skipped"):
			return
		if result.get("ok"):
			status = f"OK — {result.get('pushed_files', 0)} file(s) pushed"
		else:
			status = f"Failed — {result.get('reason', 'see Error Log')}"
		frappe.db.set_single_value(
			SETTINGS,
			{
				"wiki_mirror_last_synced_at": frappe.utils.now_datetime(),
				"wiki_mirror_last_sync_status": status[:140],
			},
			update_modified=False,
		)
	except Exception:
		pass


def _sync(full: bool) -> dict:
	rows = frappe.get_all(WIKI, fields=_PAGE_FIELDS, limit_page_length=0)
	active = [r for r in rows if _is_mirrorable(r)]

	files: list[dict] = []
	for r in active:
		path, content = render_page(r)
		digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
		if full or digest != (r.mirror_hash or ""):
			files.append(_file_entry(path, content, page=r.name, digest=digest))

	# index/log go LAST so navigation only lands after the content it points at.
	ipath, icontent = render_index()
	lpath, lcontent = render_log()
	files.append(_file_entry(ipath, icontent))
	files.append(_file_entry(lpath, lcontent))

	# Every page whose file is (still) on the container but no longer belongs
	# there: archived, or narrowed out of Org scope. A stamped mirror_hash is
	# the standing proof a file was pushed, so it drives the delete list no
	# matter WHY the page stopped being mirrorable; clearing the stamp after a
	# confirmed push stops the delete being re-sent forever and lets a later
	# re-promotion push the same content again.
	deletes = [r for r in rows if (r.mirror_hash or "") and not _is_mirrorable(r)]
	delete_paths = [_wire_path(page_path(r)) for r in deletes]
	known_paths = None
	if full:
		known_paths = sorted(
			{_wire_path(page_path(r)) for r in active} | {_wire_path(INDEX_PATH), _wire_path(LOG_PATH)}
		)

	from jarvis import admin_client

	pushed = 0
	calls = 0
	batches = _chunk_files(files)
	for i, batch in enumerate(batches):
		last = i == len(batches) - 1
		resp = admin_client.push_wiki_files(
			files=[{"path": f["path"], "content_b64": f["content_b64"]} for f in batch],
			delete=delete_paths if (last and delete_paths) else None,
			known_paths=known_paths if last else None,
		)
		if resp is None:
			# Tenant not provisioned / admin unreachable: hashes for this and
			# later batches stay unstamped, so the next sync retries them.
			frappe.log_error(
				title="wiki mirror: push failed; sync left partial",
				message=f"full={full} calls_ok={calls} batches={len(batches)}",
			)
			return {
				"ok": False,
				"reason": "admin/tenant unreachable; will retry next sync",
				"calls": calls,
				"pushed_files": pushed,
			}
		calls += 1
		pushed += len(batch)
		for f in batch:
			if f["page"]:
				frappe.db.set_value(WIKI, f["page"], "mirror_hash", f["hash"], update_modified=False)
		if last:
			for r in deletes:
				frappe.db.set_value(WIKI, r.name, "mirror_hash", "", update_modified=False)
		frappe.db.commit()

	return {
		"ok": True,
		"full": full,
		"pages": len(active),
		"pushed_files": pushed,
		"deleted": len(delete_paths),
		"calls": calls,
	}


def _file_entry(path: str, content: str, page: str | None = None, digest: str | None = None) -> dict:
	return {
		"path": _wire_path(path),
		"content_b64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
		"page": page,
		"hash": digest,
	}


def _chunk_files(files: list[dict]) -> list[list[dict]]:
	"""Greedy order-preserving batching: each call's summed b64+path payload
	stays under MAX_CALL_PAYLOAD_BYTES (a single page can't exceed it — the
	controller caps bodies at 20k chars, ~27KB b64)."""
	batches: list[list[dict]] = []
	cur: list[dict] = []
	cur_size = 0
	for f in files:
		size = len(f["content_b64"]) + len(f["path"]) + _PER_FILE_OVERHEAD_BYTES
		if cur and cur_size + size > MAX_CALL_PAYLOAD_BYTES:
			batches.append(cur)
			cur, cur_size = [], 0
		cur.append(f)
		cur_size += size
	if cur:
		batches.append(cur)
	return batches


# --------------------------------------------------------------------------- #
# triggers
# --------------------------------------------------------------------------- #
def enqueue_sync(full: bool = False) -> None:
	"""Queue the deduped mirror sync (short queue, 120s deadline). Suppressed
	under tests unless ``frappe.flags.jarvis_test_wiki_mirror_enqueue`` is set
	— fixture inserts must not spray RQ jobs. Enqueue failures (Redis down)
	are swallowed: this runs inside user save paths via doc_events."""
	if frappe.flags.in_test and not frappe.flags.jarvis_test_wiki_mirror_enqueue:
		return
	try:
		frappe.enqueue(
			JOB_METHOD,
			queue=QUEUE,
			timeout=JOB_TIMEOUT_S,
			job_id=JOB_ID_FULL if full else JOB_ID,
			deduplicate=True,
			full=bool(full),
		)
	except Exception:
		frappe.log_error(title="wiki mirror: enqueue failed", message=frappe.get_traceback())


def on_wiki_page_change(doc, method: str | None = None) -> None:
	"""doc_events hook (after_insert / on_update / on_trash on Jarvis Wiki
	Page). Org-scope pages trigger a sync; so does a Role/User page that still
	has a file on the org-shared container, because narrowing a page's scope is
	a REVOCATION and the file has to go. A trash enqueues a FULL sync: the row
	is gone before the job runs, so only known_paths pruning can remove its
	file (archival, by contrast, is a status flip the incremental sync sees as
	a delete). Never raises into the save/delete path."""
	try:
		prune = _needs_mirror_prune(doc)
		if not prune and not _is_org_scope(doc.get("scope")):
			return
		enqueue_sync(full=(prune or method == "on_trash"))
	except Exception:
		frappe.log_error(
			title="wiki mirror: doc-event trigger failed",
			message=frappe.get_traceback(),
		)


def _needs_mirror_prune(doc) -> bool:
	"""True when this non-Org page has a mirrored file to revoke.

	Two signals, because each covers the other's blind spot:

	* A stamped ``mirror_hash``. Set only after a confirmed push and cleared
	  only after a confirmed delete, so it means "a file is out there" without
	  needing any save history — which is also why it catches pages demoted
	  BEFORE this guard existed, and why it works in ``on_trash`` (where the
	  doc is loaded fresh from the DB and there is no pre-save copy).
	* A pre-save row that was Org scope. ``mirror_hash`` is stamped with
	  ``update_modified=False``, so a Desk form opened before the last sync
	  submits a stale empty hash without tripping the timestamp check; the
	  pre-save row still knows the page was Org. Only ``on_update`` has one.

	The prune rides a FULL sync so the fleet's ``known_paths`` walk removes the
	file even in that stale-hash case, where no delete path can be derived.
	"""
	if _is_org_scope(doc.get("scope")):
		return False
	if (doc.get("mirror_hash") or "").strip():
		return True
	# callable-checked, not hasattr: frappe._dict answers every attribute with
	# None, so a plain dict-shaped doc would hasattr-pass and then TypeError.
	loader = getattr(doc, "get_doc_before_save", None)
	before = loader() if callable(loader) else None
	return bool(before and _is_org_scope(before.get("scope")))
