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
Slugs are deterministic + app-prefixed (``<app>-<title>``) so a re-run UPDATES
the same page in place (the ``_find_existing``-by-slug precedent) rather than
duplicating. A per-run page cap (15) bounds a run; truncation is disclosed as a
coverage caveat on the last written page.
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
	  pages: list of ``{title, page_type, body_md, mode, app?}``. ``page_type``
	    is one of the 9 wiki PAGE_TYPES (defaults to ``Process``); ``mode`` is
	    ``"create"`` (default, full body) or ``"append"`` (add a section to an
	    existing page). Each page's slug is ``<app>-<title>`` so a re-run updates
	    in place. Pages beyond the per-run cap are dropped and disclosed.
	  app: default app for pages that omit their own ``app`` (so the delegate can
	    call once per app). Used only to prefix the slug + stamp provenance.

	Returns ``{run, applied, failed, pages_remaining, truncated, slugs}``.
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
	updates: list[dict] = []
	slugs: list[str] = []
	for item in raw:
		if not isinstance(item, dict):
			continue
		title = " ".join(str(item.get("title") or "").split())[:140]
		body = str(item.get("body_md") or "").strip()[:_BODY_CLIP]
		if not title or not body:
			continue
		page_app = (str(item.get("app") or "").strip() or default_app).strip()
		page_type = str(item.get("page_type") or "").strip()
		if page_type not in PAGE_TYPES:
			page_type = "Process"  # app knowledge is process-shaped by default
		slug = _slugify(f"{page_app} {title}") if page_app else _slugify(title)
		if not slug:
			continue
		update = {"slug": slug, "title": title, "page_type": page_type, "scope": "Org"}
		if str(item.get("mode") or "").strip().lower() == "append":
			update["append_md"] = body
		else:
			update["body_md"] = body
		updates.append(update)
		slugs.append(slug)

	# Per-run page cap: bound how many pages one run may write. A re-run gets a
	# fresh session_key (fresh counter), so re-learning the same app is not
	# penalised — the cap only stops one run flooding the wiki.
	already = ctx.pages_written(session_key)
	remaining = max(0, ctx.PER_RUN_PAGE_CAP - already)
	truncated = len(updates) > remaining
	if truncated:
		dropped = len(updates) - remaining
		updates = updates[:remaining]
		slugs = slugs[:remaining]
		# Disclose the truncation on the last surviving page so the wiki itself is
		# honest about partial coverage (reuse the coverage-caveat discipline).
		if updates:
			last = updates[-1]
			key = "append_md" if "append_md" in last else "body_md"
			marker = f"\n\n_Partial coverage: {dropped} further page(s) exceeded the per-run cap and were not written._"
			last[key] = (last[key] + marker)[:_BODY_CLIP]

	applied = 0
	failed = 0
	for i in range(0, len(updates), MAX_PAGES_PER_NOTE):
		a, f = apply_extracted_page_updates(
			updates[i : i + MAX_PAGES_PER_NOTE],
			source=f"app-learning-agent:{default_app or 'apps'}",
			user=frappe.session.user,
			ref=run["name"],
		)
		applied += a
		failed += f

	ctx.add_pages_written(session_key, applied)
	return {
		"run": run["name"],
		"applied": applied,
		"failed": failed,
		"pages_remaining": max(0, ctx.PER_RUN_PAGE_CAP - ctx.pages_written(session_key)),
		"truncated": truncated,
		"slugs": slugs[:applied] if applied else [],
	}
