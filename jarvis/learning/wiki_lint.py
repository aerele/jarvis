"""Weekly wiki health check (wiki v2 D3).

``run_lint`` runs deterministic, zero-LLM checks over the Active Org-scope
wiki pages first:

  * unresolved contradictions — ``contradiction_flag`` set, or the body still
	carries a ``Contradiction flagged`` section (the ingest merge appends
	those; a manual save clears the flag but may leave the section);
  * stale pages — >90 days since ``last_confirmed_at`` (``modified``
	fallback), via ``jarvis.chat.wiki.is_stale``;
  * orphan pages — no inbound reference from any OTHER Active Org page,
	counting body ``[[slug]]`` links ∪ the page's curated ``manual_links``
	(``add_wiki_link`` stores those OUT of ``body_md`` on purpose, so a
	body-only scan calls a curated-only target an orphan forever);
  * near-duplicate titles — normalized-title collisions (failure mode #1 of
	LLM-maintained wikis: the same entity re-created under a slightly
	different name).

Then at most ONE strict-JSON ``openrouter_complete`` call confirms up to
``_MAX_LLM_SUSPECTS`` contradiction/duplicate suspects (the job-safe pattern
from ``jarvis.learning.voice_facts``); the pass is skipped silently when no
OpenRouter key is resolvable (``voice._credentials``). Only LLM-CONFIRMED
contradictions get ``contradiction_flag`` re-stamped (``frappe.db.set_value``,
``update_modified=False`` — never ``doc.save``, which would re-fire the
mirror-sync doc_events) — the deterministic pass alone never overrides a
human's flag-clearing save.

Results persist on Jarvis Settings (``wiki_lint_last_run_at`` +
``wiki_lint_summary``, RO fields surfaced in the Wiki tab) and land in the
container mirror's log.md on the next sync. ``scheduled_lint`` (hooks weekly)
swallows everything.
"""

from __future__ import annotations

import json
import re

import frappe
from frappe.utils import cint, now_datetime

WIKI = "Jarvis Wiki Page"
SETTINGS = "Jarvis Settings"

_CONTRADICTION_MARKER = "Contradiction flagged"
# Matches the controller's slug grammar so a stray [[Some Prose]] never counts
# as an inbound reference.
_WIKILINK_RE = re.compile(r"\[\[([a-z0-9]+(?:-{1,2}[a-z0-9]+)*)\]\]")

_MAX_LLM_SUSPECTS = 10
_MAX_BODY_PROMPT_CHARS = 1500
_MAX_SUMMARY_CHARS = 400
_MAX_TOP_ISSUES = 10

_CONFIRM_SYSTEM = (
	"You review an internal business wiki for health issues. You are given "
	"suspect pages. Output ONLY a JSON array - no prose, no markdown fences. "
	'Each item must be an object with exactly these keys: "slug" (the page '
	'slug exactly as given), "kind" ("contradiction" or "duplicate", matching '
	'the suspicion you were given for that page) and "confirmed" (boolean). '
	'Confirm "contradiction" only when the page body still contains genuinely '
	"conflicting, unresolved statements about the same fact. Confirm "
	'"duplicate" only when the listed pages clearly describe the same entity '
	"or topic. When unsure, output false."
)


def _load_pages() -> list:
	"""Active Org-scope pages (NULL/'' scope reads as Org). Role/User pages
	are personal/team notes — the org health check leaves them alone."""
	rows = frappe.get_all(
		WIKI,
		filters={"status": "Active"},
		fields=[
			"name",
			"title",
			"summary",
			"body_md",
			"contradiction_flag",
			"last_confirmed_at",
			"modified",
			"scope",
			# curated [[links]] live here, never in body_md (add_wiki_link's R1
			# durability rule), so the orphan check has to read both halves.
			"manual_links",
		],
		order_by="name asc",
		limit_page_length=0,
	)
	return [r for r in rows if (r.scope or "").strip() in ("", "Org")]


# --------------------------------------------------------------------------- #
# deterministic checks
# --------------------------------------------------------------------------- #
def _contradiction_suspects(pages: list) -> list:
	return [p for p in pages if cint(p.contradiction_flag) or _CONTRADICTION_MARKER in (p.body_md or "")]


def _stale_pages(pages: list) -> list:
	from jarvis.chat.wiki import is_stale

	return [p for p in pages if is_stale(p.last_confirmed_at, p.modified)]


def _outbound_targets(page) -> set[str]:
	"""One page's outbound link targets: body ``[[slug]]`` wikilinks ∪ its
	curated ``manual_links``, minus self-references.

	The union is what ``wiki_graph._build_graph_from_pages`` already applies;
	reading only ``body_md`` here is what made the health summary contradict
	the Evolution tab (a curated inbound link left the target an orphan).
	"""
	from jarvis.chat.wiki import _parse_manual_links

	targets = set(_WIKILINK_RE.findall(page.body_md or ""))
	targets |= set(_parse_manual_links(page.get("manual_links")))
	targets.discard(page.name)
	return targets


def _find_orphans(pages: list) -> list:
	"""Pages no OTHER Active Org page links to (self-references don't count;
	the generated index.md is not a page and never counts).

	Young wikis have few cross-links, so "orphan" is only a signal once
	linking is actually practiced: with fewer than 2 linking pages the check
	would flag nearly the whole corpus (observed: 23 of 24) and read as
	noise, so it reports nothing."""
	linking_pages = 0
	inbound: set[str] = set()
	for p in pages:
		targets = _outbound_targets(p)
		if targets:
			linking_pages += 1
		inbound.update(targets)
	if linking_pages < 2:
		return []
	return [p for p in pages if p.name not in inbound]


def _normalize_title(title) -> str:
	return " ".join(re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).split())


def _duplicate_title_groups(pages: list) -> list[list]:
	groups: dict[str, list] = {}
	for p in pages:
		key = _normalize_title(p.title)
		if key:
			groups.setdefault(key, []).append(p)
	return [group for group in groups.values() if len(group) > 1]


# --------------------------------------------------------------------------- #
# LLM confirm pass (one call, strict JSON, silently skipped without a key)
# --------------------------------------------------------------------------- #
def _llm_confirm(contradictions: list, dupe_groups: list[list]) -> dict | None:
	"""Confirm up to _MAX_LLM_SUSPECTS contradiction/duplicate suspects with
	ONE strict-JSON call. Returns ``{"contradictions": [slug], "duplicates":
	[slug]}`` (confirmed only, restricted to the suspect set — the model's
	output is untrusted and must not flag arbitrary pages), or None when the
	pass was skipped/failed (no key, call error, unparseable output)."""
	suspects: list[tuple[str, dict]] = [("contradiction", p) for p in contradictions]
	for group in dupe_groups:
		suspects.extend(("duplicate", p) for p in group)
	suspects = suspects[:_MAX_LLM_SUSPECTS]
	if not suspects:
		return None

	try:
		from jarvis.chat import voice

		key, _model = voice._credentials()
	except Exception:
		return None
	if not key:
		return None

	try:
		raw = voice.openrouter_complete(
			[
				{"role": "system", "content": _CONFIRM_SYSTEM},
				{"role": "user", "content": _confirm_prompt(suspects)},
			]
		)
	except Exception:
		frappe.log_error(title="wiki lint: confirm call failed", message=frappe.get_traceback())
		return None
	items = _parse_json_array(raw)
	if items is None:
		frappe.log_error(
			title="wiki lint: unparseable confirm output",
			message=(raw or "")[:2000] if isinstance(raw, str) else repr(raw)[:2000],
		)
		return None

	allowed = {(kind, p.name) for kind, p in suspects}
	out: dict[str, list] = {"contradictions": [], "duplicates": []}
	for item in items:
		if not isinstance(item, dict) or not item.get("confirmed"):
			continue
		kind = item.get("kind")
		slug = str(item.get("slug") or "")
		if kind == "contradiction" and ("contradiction", slug) in allowed:
			out["contradictions"].append(slug)
		elif kind == "duplicate" and ("duplicate", slug) in allowed:
			out["duplicates"].append(slug)
	return out


def _confirm_prompt(suspects: list[tuple[str, dict]]) -> str:
	parts = []
	for i, (kind, p) in enumerate(suspects, 1):
		summary = " ".join(str(p.summary or "").split())
		# Contradiction sections are appended at the bottom by the ingest
		# merge, so the body TAIL is the informative slice.
		body_tail = (p.body_md or "")[-_MAX_BODY_PROMPT_CHARS:]
		parts.append(
			f"Suspect {i} (suspected {kind}):\n"
			f"slug: {p.name}\n"
			f"title: {p.title}\n"
			f"summary: {summary}\n"
			f"body (tail):\n{body_tail}"
		)
	return "Review these suspect wiki pages and confirm or reject each suspicion.\n\n" + "\n\n".join(parts)


def _parse_json_array(raw) -> list | None:
	if not raw or not isinstance(raw, str):
		return None
	text = raw.strip()
	if text.startswith("```"):
		text = text.strip("`").strip()
		if "\n" in text:
			first, rest = text.split("\n", 1)
			if first.strip().lower() in ("json", ""):
				text = rest
	try:
		parsed = json.loads(text)
	except Exception:
		lo, hi = text.find("["), text.rfind("]")
		if lo == -1 or hi <= lo:
			return None
		try:
			parsed = json.loads(text[lo : hi + 1])
		except Exception:
			return None
	return parsed if isinstance(parsed, list) else None


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def run_lint() -> dict:
	"""Run the health check now. Returns the summary dict (counts + top
	issues + per-check slug lists) and persists the run stamp + human summary
	on Jarvis Settings. The LLM pass is best-effort; deterministic results
	stand on their own."""
	pages = _load_pages()
	contradictions = _contradiction_suspects(pages)
	stale = _stale_pages(pages)
	orphans = _find_orphans(pages)
	dupe_groups = _duplicate_title_groups(pages)

	confirmed = _llm_confirm(contradictions, dupe_groups)
	flagged = 0
	if confirmed:
		for slug in confirmed["contradictions"]:
			if not cint(frappe.db.get_value(WIKI, slug, "contradiction_flag")):
				frappe.db.set_value(WIKI, slug, "contradiction_flag", 1, update_modified=False)
			flagged += 1

	counts = {
		"pages": len(pages),
		"contradictions": len(contradictions),
		"stale": len(stale),
		"orphans": len(orphans),
		"duplicate_title_groups": len(dupe_groups),
	}
	issues = _top_issues(contradictions, stale, orphans, dupe_groups)
	summary = _summary_text(counts, confirmed, flagged)
	_stamp_settings(summary)

	return {
		"ok": True,
		"counts": counts,
		"contradictions": [p.name for p in contradictions],
		"stale": [p.name for p in stale],
		"orphans": [p.name for p in orphans],
		"duplicate_titles": [[p.name for p in g] for g in dupe_groups],
		"issues": issues,
		"llm_checked": confirmed is not None,
		"confirmed_contradictions": (confirmed or {}).get("contradictions", []),
		"confirmed_duplicates": (confirmed or {}).get("duplicates", []),
		"summary": summary,
	}


def scheduled_lint() -> None:
	"""hooks ``weekly`` entry. Skips when the operator turned the wiki off;
	never raises out of the scheduler."""
	try:
		from jarvis.chat.wiki import wiki_enabled

		if not wiki_enabled():
			return
		run_lint()
	except Exception:
		frappe.log_error(title="wiki lint: scheduled run failed", message=frappe.get_traceback())


def _top_issues(contradictions, stale, orphans, dupe_groups) -> list[str]:
	issues: list[str] = []
	issues += [f"contradiction: {p.name}" for p in contradictions]
	issues += [f"stale: {p.name}" for p in stale]
	issues += ["duplicate titles: " + ", ".join(p.name for p in g) for g in dupe_groups]
	issues += [f"orphan: {p.name}" for p in orphans]
	return issues[:_MAX_TOP_ISSUES]


def _summary_text(counts: dict, confirmed: dict | None, flagged: int) -> str:
	"""Human copy, not a log line: named problems, real plurals, no pipeline
	jargon. Reads well in the Wiki tab settings popover."""

	def n(count, singular, plural=None):
		return f"{count} {singular if count == 1 else (plural or singular + 's')}"

	problems = []
	if counts["contradictions"]:
		problems.append(
			n(counts["contradictions"], "page with conflicting facts", "pages with conflicting facts")
		)
	if counts["stale"]:
		problems.append(
			n(counts["stale"], "page not confirmed in 90+ days", "pages not confirmed in 90+ days")
		)
	if counts["orphans"]:
		problems.append(n(counts["orphans"], "page no other page links to", "pages no other page links to"))
	if counts["duplicate_title_groups"]:
		problems.append(
			n(counts["duplicate_title_groups"], "possible duplicate title", "possible duplicate titles")
		)
	text = (
		f"Checked {n(counts['pages'], 'page')}: "
		+ ("; ".join(problems) if problems else "no problems found")
		+ "."
	)
	if confirmed is not None:
		text += (
			f" AI double-check confirmed {len(confirmed['contradictions'])}"
			f" conflict(s) ({flagged} flagged) and {len(confirmed['duplicates'])} duplicate(s)."
		)
	return text[:_MAX_SUMMARY_CHARS]


def _stamp_settings(summary: str) -> None:
	"""Best-effort RO-field stamp (the voice_facts idiom: a background write
	must never trip the Settings on_update sync)."""
	try:
		frappe.db.set_single_value(SETTINGS, "wiki_lint_last_run_at", now_datetime(), update_modified=False)
		frappe.db.set_single_value(
			SETTINGS,
			"wiki_lint_summary",
			(summary or "")[:_MAX_SUMMARY_CHARS],
			update_modified=False,
		)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="wiki lint: settings stamp failed", message=frappe.get_traceback())
