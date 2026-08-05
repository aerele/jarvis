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

from jarvis.chat.wiki_graph import _MAX_LINKS_PER_PAGE

WIKI = "Jarvis Wiki Page"
SETTINGS = "Jarvis Settings"

JOB_METHOD = "jarvis.chat.wiki_mirror.sync"
# Two dedup ids so a queued incremental sync can't swallow a requested FULL
# sync (manual "Sync now" / on_trash prune both need known_paths).
JOB_ID = "wiki-mirror-sync"
JOB_ID_FULL = "wiki-mirror-sync-full"
QUEUE = "short"
JOB_TIMEOUT_S = 120

# #622: one mirror sync at a time per bench. The two JOB_IDs above deliberately let an
# incremental and a FULL sync be QUEUED at once; this serialises their EXECUTION, which
# is the part that matters. ``_sync`` derives ``known_paths`` from a snapshot taken at
# its top but the prune only executes on the LAST push, so a file an incremental writes
# in between is missing from that list and gets pruned even though it is current.
_LOCK_NAME = "jarvis_wiki_mirror"
# Longer than the RQ deadline, so a crashed holder's key always expires on its own.
_LOCK_TTL_S = JOB_TIMEOUT_S + 30
# WAITING is the primary resolution, not a fallback. A job that waits and then acquires
# re-reads the page table itself, so its own change is in the snapshot and nothing is
# lost. Sized to leave headroom inside JOB_TIMEOUT_S for the sync that follows, so
# contention almost always resolves here rather than on the re-queue path below.
_LOCK_WAIT_S = 75.0
# Re-queue ids, DISTINCT from JOB_ID / JOB_ID_FULL on purpose. frappe.enqueue's
# deduplicate=True declines a job whose id is already QUEUED or STARTED
# (background_jobs.py: "Not queueing job ... because it is in queue already"), and a
# contended worker is itself STARTED under its own id - so re-queueing under that id is
# silently declined and the work is dropped, which is the failure this whole change
# exists to prevent. A separate id is a different dedup slot, so the retry is really
# queued while still being deduped against other retries.
JOB_ID_RETRY = "wiki-mirror-sync-retry"
JOB_ID_FULL_RETRY = "wiki-mirror-sync-full-retry"

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
# Curated links are appended one at a time and never pruned, so the "## Related" tail
# gets the same defensive cap wiki_graph puts on a page's link set.
#
# #645: IMPORTED rather than restated. These were two independent literals whose
# equality only a test enforced, so a future change to either one silently broke the
# coupling. Now the coupling is structural and there is one number to change.
_MAX_RELATED = _MAX_LINKS_PER_PAGE

_PAGE_FIELDS = [
	"name",
	"slug",
	"title",
	"page_type",
	"scope",
	"status",
	"summary",
	"body_md",
	# Curated [[links]], kept out of body_md by add_wiki_link; rendered as the
	# "## Related" tail so they reach the container at all.
	"manual_links",
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
def render_page(doc, mirrored_slugs: set[str] | None = None) -> tuple[str, str]:
	"""Render one page as Obsidian-style markdown. Returns ``(path, content)``
	with path ``wiki/<typedir>/<slug>.md``. Frontmatter carries the metadata
	the agent needs to trust-or-verify (stale/contradiction flags); the body's
	existing ``[[slug]]`` links pass through untouched; curated out-of-body
	links render as a ``## Related`` section and the provenance trail as a
	``## Sources`` tail.

	``mirrored_slugs`` is the set of slugs that actually have a file on the
	container. ``_sync`` always passes it, because scope discipline runs
	through Related too: a curated link may point at a Role/User or archived
	page, and neither its slug nor a dangling ``[[link]]`` belongs in the
	org-shared mirror. ``None`` (direct/test calls) renders every curated
	target unfiltered."""
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
	related = _related_lines(doc, mirrored_slugs)
	if related:
		lines += ["## Related", ""] + related + [""]
	source_lines = _source_lines(doc.get("sources"))
	if source_lines:
		lines += ["## Sources", ""] + source_lines + [""]
	content = "\n".join(lines).rstrip("\n") + "\n"
	return page_path(doc), content


def _related_lines(doc, mirrored_slugs: set[str] | None) -> list[str]:
	"""``manual_links`` -> ``- [[slug]]`` bullets, in curation order.

	Curated links are stored OUT of ``body_md`` so LLM re-ingest can't clobber
	them, which also meant they never reached the container at all: the agent's
	two channels are this mirror and ``jarvis__read_wiki``, and neither read the
	field. Rendering them as real ``[[slug]]`` links keeps every body-based
	consumer (Obsidian, a grep, the agent itself) working unchanged.

	#645: keeps the NEWEST ``_MAX_RELATED``, not the oldest. ``add_wiki_link`` APPENDS,
	so truncating the head meant that past the cap a user clicks "+ link", is told it
	succeeded, the link is durably stored, and it never reaches the container. Dropping
	the oldest bullet is a bounded, understandable loss; silently discarding the one the
	user just made is not. The graph applies the same rule to the same field."""
	from jarvis.chat.wiki import _parse_manual_links

	self_slug = doc.get("slug") or doc.get("name")
	out = []
	for target in _parse_manual_links(doc.get("manual_links")):
		if target == self_slug:
			continue
		if mirrored_slugs is not None and target not in mirrored_slugs:
			continue
		out.append(f"- [[{target}]]")
	return out[-_MAX_RELATED:]


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
	type/dir moves). Returns a summary dict; NEVER raises into callers.

	#622: serialised per bench. ``_sync`` reads the page table once at its top and
	derives ``known_paths`` from that snapshot, but the prune only runs on the final
	push, after every render, hash and earlier batch. A page an incremental sync wrote
	inside that window is absent from the list and gets pruned even though it is
	current, which surfaces as a wiki file silently vanishing from the container with
	no error anywhere.

	Contention is resolved by WAITING, which is the important part: a job that waits and
	then acquires re-reads the page table itself, so its own change is in the snapshot
	and nothing is lost. Only a wait that times out falls back to a re-queue, and that
	re-queue uses a DISTINCT job id (see ``JOB_ID_RETRY``) because Frappe declines a
	dedup-enqueue under an id that is already STARTED, which a contended worker's own id
	always is. Dropping the work is not an option: there is no periodic sweep to pick a
	stranded change up later (see ``enqueue_sync``).

	A real Redis fault is re-queued too, not just logged. ``redis_lock`` propagates those
	rather than yielding False, and treating one as a plain crash would strand the page
	edit that triggered the job for exactly the same reason."""
	from jarvis._redis_lock import redis_lock

	try:
		with redis_lock(_LOCK_NAME, timeout_s=_LOCK_TTL_S, blocking_timeout_s=_LOCK_WAIT_S) as acquired:
			if acquired:
				result = _sync(full=bool(full))
			else:
				result = _requeue_contended(full=bool(full), why="another sync still in flight")
	except Exception:
		frappe.log_error(title="wiki mirror: sync crashed", message=frappe.get_traceback())
		# A fault reaching here may be the lock itself (redis_lock propagates real Redis
		# errors by design), in which case _sync never ran and the change is unpushed.
		# Re-queue rather than let it strand; the retry id makes this safe to repeat.
		result = _requeue_contended(full=bool(full), why="sync crashed; see Error Log")
	_stamp_sync_status(result)
	return result


def _requeue_contended(full: bool, why: str) -> dict:
	"""Re-queue a sync that could not run, under the RETRY job id.

	Returns the result dict the caller reports. ``requeued`` reflects what actually
	happened rather than what was attempted: ``enqueue_sync`` swallows enqueue failures
	(Redis down), and a result that claims a re-queue which never occurred is worse than
	one that admits it, because nothing else will come back for this change."""
	queued = enqueue_sync(full=full, retry=True)
	# "skipped" keeps _stamp_sync_status from overwriting the Wiki tab's "last synced"
	# line with a failure that did not happen, but only when the retry is really pending.
	return {
		"ok": False,
		"skipped": bool(queued),
		"requeued": bool(queued),
		"reason": f"{why}; {'re-queued' if queued else 'RE-QUEUE FAILED, change may be unpushed'}",
	}


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

	# Only these slugs have a file on the container, so only these may appear in
	# a "## Related" tail (a curated link out to a Role/User or archived page
	# would otherwise leak its slug into the org-shared mirror and dangle).
	mirrored_slugs = {r.name for r in active}

	files: list[dict] = []
	for r in active:
		path, content = render_page(r, mirrored_slugs)
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
def enqueue_sync(full: bool = False, after_commit: bool = False, retry: bool = False) -> bool:
	"""Queue the deduped mirror sync (short queue, 120s deadline). Suppressed
	under tests unless ``frappe.flags.jarvis_test_wiki_mirror_enqueue`` is set
	— fixture inserts must not spray RQ jobs. Enqueue failures (Redis down)
	are swallowed: this runs inside user save paths via doc_events.

	Returns whether a job was really queued, so a caller that depends on the retry
	actually existing can say so honestly instead of assuming.

	``after_commit`` is what the doc_events trigger needs and what the manual
	endpoint must not use. The worker opens its own DB connection, so a job
	queued mid-save can read the PRE-save row, find nothing to push or prune,
	and report success — and with no periodic sweep, a prune lost that way is
	lost for good. Deferring to the save's commit closes that window. The
	manual "Sync now" endpoint writes nothing, so its request may never commit
	and deferring there would drop the job entirely.

	``retry=True`` uses the RETRY job ids. A contended worker re-queueing itself must
	NOT reuse its own id: ``frappe.enqueue`` with ``deduplicate=True`` declines a job
	whose id is already QUEUED or STARTED (background_jobs.py:119-125), and the caller
	is itself STARTED under that id, so the enqueue is silently skipped and the work is
	dropped. A separate id is a separate dedup slot, so the retry is really queued while
	still being deduped against other retries."""
	if frappe.flags.in_test and not frappe.flags.jarvis_test_wiki_mirror_enqueue:
		return False
	if retry:
		job_id = JOB_ID_FULL_RETRY if full else JOB_ID_RETRY
	else:
		job_id = JOB_ID_FULL if full else JOB_ID
	try:
		frappe.enqueue(
			JOB_METHOD,
			queue=QUEUE,
			timeout=JOB_TIMEOUT_S,
			job_id=job_id,
			deduplicate=True,
			enqueue_after_commit=bool(after_commit),
			full=bool(full),
		)
		return True
	except Exception:
		frappe.log_error(title="wiki mirror: enqueue failed", message=frappe.get_traceback())
		return False


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
		enqueue_sync(full=(prune or method == "on_trash"), after_commit=True)
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
