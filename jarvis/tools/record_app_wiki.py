"""``jarvis__record_app_wiki`` — the Custom App Learning scribe's wiki writeback
(audited, NOT gated).

Mirrors ``jarvis.tools.record_agent_run`` (api.py:501-508 precedent): it is a
``_WRITE_TOOLS`` member deliberately EXCLUDED from ``_GATED_WRITES`` — a detached
delegate turn has nobody to click a confirmation card, and the pages ARE the
agent's declared output, so it is audited but never parked. It does NOT use the
gated ``jarvis__update_wiki`` card path (which blocks an unattended run).

It resolves the run from the caller's session_key (never a model id), then
applies pages SERVER-SIDE through ``jarvis.chat.wiki.apply_extracted_page_updates``
— reusing that funnel's controller sanitizer (``_sanitize_untrusted_text``, incl.
the ``jarvis__`` -> ``jarvis-`` defang), Org scoping, provenance and page caps.

Namespace integrity (all SERVER-SIDE, never model-trusted):
  * every page MUST resolve to a real learnable custom app (``assert_custom_app``);
    a page without one is rejected;
  * the slug is FORCED app-prefixed (``<app>-<key>``, keyed on a STABLE per-page
    ``key`` so a re-run reconciles deterministically) — the model never supplies
    the namespace prefix;
  * the funnel lookup is FENCED to this agent's own provenance
    (``PROVENANCE_KIND_PREFIX``), so a scribe can create/update only pages of its
    OWN provenance and can NEVER overwrite a human-authored Org page even on a
    slug collision.
A per-run page cap (15) bounds a run; truncation is disclosed as a coverage
caveat on the last written page and stamped on the run.
"""

from __future__ import annotations

import json
import re

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _app_learning_ctx as ctx

# Body cap matches the wiki controller's MAX_BODY_LEN; the funnel clips too, but
# clipping here keeps the write predictable.
_BODY_CLIP = 20000

RUN = "Jarvis Agent Run"

# The provenance stamp every NEW scribe page carries in its ``sources[].kind``
# (the audit label): ``app-learning-agent:<app>``.
PROVENANCE_KIND_PREFIX = "app-learning-agent:"
# The funnel FENCE key: apply_extracted_page_updates only updates a page that
# already carries a kind with this prefix. It matches ANY app-learning
# provenance — the new scribe stamp (``app-learning-agent:``) AND the RETIRED
# chat-batch pipeline's stamp (``app-learning:<app>``) — so a re-run can refresh
# a page either produced (a migrated org's pages included), but a scribe can
# NEVER clobber a human-authored page: no human/other kind
# (chat/voice/edit/manual/promotion/tool) begins with ``app-learning``.
PROVENANCE_FENCE_PREFIX = "app-learning"


def _slugify(text: str) -> str:
	return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def _as_list(value) -> list:
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except Exception:
			return []
	return value if isinstance(value, list) else []


def record_app_wiki(pages=None, app: str | None = None) -> dict:
	"""Land the scribe's composed wiki pages for a custom app.

	Args (from the delegate):
	  pages: list of ``{title, page_type, body_md, mode, key?, app?}``.
	    ``page_type`` is one of the 9 wiki PAGE_TYPES (defaults to ``Process``);
	    ``mode`` is ``"create"`` (default, full body) or ``"append"`` (add a
	    section to an existing page). ``key`` is a STABLE per-page identifier
	    (e.g. ``overview``, ``doctype-gate-pass``, ``process-credit-approval``)
	    reused verbatim across runs so re-runs UPDATE the same page in place; it
	    falls back to the title only when omitted. Each page MUST resolve to a
	    learnable custom app; its slug is FORCED to ``<app>-<key>`` server-side.
	    Pages beyond the per-run cap are dropped and disclosed.
	  app: default app for pages that omit their own ``app`` (so the delegate can
	    call once per app). Every page's resolved app is validated against the
	    custom-apps allowlist; a page with no valid app is rejected.

	Returns ``{run, applied, failed, rejected, pages_remaining, truncated,
	slugs}``.
	"""
	from jarvis.chat.wiki import (
		MAX_PAGES_PER_NOTE,
		PAGE_TYPES,
		apply_extracted_page_updates,
	)

	run = ctx.resolve_scribe_run()  # self-gate: scribe run + admin-tier or raise
	session_key = run["session_key"]
	default_app = (app or "").strip()

	raw = _as_list(pages)
	# Each accepted page is kept as a flat entry {app, slug, title, update} in
	# arrival order, so an outcome can be mapped back to the EXACT page it wrote
	# (see the writeback loop). ``update`` carries the funnel payload; app/slug/
	# title are the summary/tally shape.
	accepted: list[dict] = []
	rejected = 0
	for item in raw:
		if not isinstance(item, dict):
			continue
		title = " ".join(str(item.get("title") or "").split())[:140]
		body = str(item.get("body_md") or "").strip()[:_BODY_CLIP]
		if not title or not body:
			continue
		# REQUIRE + VALIDATE a resolved custom app per page (never model-trusted):
		# a page with no valid learnable custom app is rejected outright.
		page_app = (str(item.get("app") or "").strip() or default_app).strip()
		try:
			ctx.assert_custom_app(page_app)
		except InvalidArgumentError:
			rejected += 1
			continue
		page_type = str(item.get("page_type") or "").strip()
		if page_type not in PAGE_TYPES:
			page_type = "Process"  # app knowledge is process-shaped by default
		# FORCE the slug app-prefixed SERVER-SIDE, keyed on a STABLE per-page key
		# (title only as a fallback) so a re-run reconciles the same page.
		page_key = _slugify(item.get("key") or "") or _slugify(title)
		slug = _slugify(f"{page_app} {page_key}")
		if not slug:
			rejected += 1
			continue
		update = {"slug": slug, "title": title, "page_type": page_type, "scope": "Org"}
		if str(item.get("mode") or "").strip().lower() == "append":
			update["append_md"] = body
		else:
			update["body_md"] = body
		accepted.append({"app": page_app, "slug": slug, "title": title, "update": update})

	total = len(accepted)

	# Per-run page cap: bound how many pages one run may write. A re-run gets a
	# fresh session_key (fresh counter), so re-learning the same app is not
	# penalised — the cap only stops one run flooding the wiki.
	already = ctx.pages_written(session_key)
	remaining = max(0, ctx.PER_RUN_PAGE_CAP - already)
	truncated = total > remaining
	dropped = 0
	if truncated:
		dropped = total - remaining
		# Keep the FIRST ``remaining`` pages in order; drop the tail.
		accepted = accepted[:remaining]
		# Disclose the truncation on the last surviving page so the wiki itself is
		# honest about partial coverage (reuse the coverage-caveat discipline).
		if accepted:
			last = accepted[-1]["update"]
			key = "append_md" if "append_md" in last else "body_md"
			marker = f"\n\n_Partial coverage: {dropped} further page(s) exceeded the per-run cap and were not written._"
			last[key] = (last[key] + marker)[:_BODY_CLIP]

	# Group into per-app batches (order preserved) so each app's pages are stamped
	# with their own provenance source (``app-learning-agent:<app>``).
	by_app: dict[str, list[dict]] = {}
	for entry in accepted:
		by_app.setdefault(entry["app"], []).append(entry)

	# Apply through the funnel and record ONLY the pages actually written: the
	# per-update outcome is zipped back to its accepted entry, so a refused
	# collision in the middle of a batch is counted as failed and never recorded
	# as a page this run wrote (the tally feeds the completion summary).
	applied = 0
	failed = 0
	applied_pages: list[dict] = []
	for page_app, entries in by_app.items():
		for i in range(0, len(entries), MAX_PAGES_PER_NOTE):
			chunk = entries[i : i + MAX_PAGES_PER_NOTE]
			outcomes = apply_extracted_page_updates(
				[e["update"] for e in chunk],
				source=f"{PROVENANCE_KIND_PREFIX}{page_app}",
				user=frappe.session.user,
				ref=run["name"],
				provenance_prefix=PROVENANCE_FENCE_PREFIX,
				return_outcomes=True,
			)
			for entry, outcome in zip(chunk, outcomes, strict=True):
				if outcome["ok"]:
					applied += 1
					applied_pages.append(
						{"app": entry["app"], "slug": entry["slug"], "title": entry["title"]}
					)
				else:
					failed += 1

	ctx.add_pages_written(session_key, applied)
	_tally_run(run["name"], applied_pages, truncated, dropped)
	return {
		"run": run["name"],
		"applied": applied,
		"failed": failed,
		"rejected": rejected,
		"pages_remaining": max(0, ctx.PER_RUN_PAGE_CAP - ctx.pages_written(session_key)),
		"truncated": truncated,
		"slugs": [m["slug"] for m in applied_pages],
	}


def _tally_run(run_name: str, applied_pages: list[dict], truncated: bool, dropped: int) -> None:
	"""Accumulate this call's written pages onto the run row so the Runs tab can
	show "N pages written" with links WITHOUT any findings shape, and so a
	successful run has its tally even before the terminal
	``finish_app_learning_run`` flips its status. Sequential within one delegate
	turn, so read-modify-write is safe. Best-effort: a tally hiccup never fails a
	landed write."""
	if not applied_pages and not truncated:
		return
	try:
		cur = frappe.db.get_value(RUN, run_name, ["pages_written", "pages_json"], as_dict=True) or {}
		try:
			existing = json.loads(cur.get("pages_json") or "[]")
		except Exception:
			existing = []
		if not isinstance(existing, list):
			existing = []
		seen = {p.get("slug") for p in existing if isinstance(p, dict)}
		for m in applied_pages:
			if m["slug"] not in seen:
				existing.append({"slug": m["slug"], "title": m["title"]})
				seen.add(m["slug"])
		values = {
			"pages_written": int(cur.get("pages_written") or 0) + len(applied_pages),
			"pages_json": frappe.as_json(existing[: ctx.PER_RUN_PAGE_CAP])[:60000],
		}
		if truncated and dropped:
			values["coverage_note"] = f"partial coverage: {dropped} page(s) exceeded the per-run cap"[:140]
		frappe.db.set_value(RUN, run_name, values, update_modified=False)
	except Exception:
		frappe.log_error(title="record_app_wiki: run tally failed", message=frappe.get_traceback())
