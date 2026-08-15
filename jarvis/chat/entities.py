"""Entity extraction for the org wiki (voice & wiki feature).

Maps what a chat turn actually touched — the doc the user was viewing plus the
docs the agent's tool calls referenced — onto wiki page identities:

- ``refs_from_tool``: (ref_doctype, ref_name) for one tool call, reusing the
  battle-tested arg/result heuristics of ``jarvis.audit._ref`` so the tool-row
  stamping (jarvis/api.py) and the audit log can never disagree about what a
  call targeted.
- ``entities_for_turn``: the distinct doc refs stamped on a conversation's
  ``role=tool`` Jarvis Chat Message rows after a given ``seq`` (one indexed
  query, capped at 20).
- ``page_ref_for``: which wiki page (if any) an entity maps to. Master
  entities (Customer / Supplier / Item) get a per-record page; the
  transactional doctypes get ONE doctype-level page each (per-invoice pages
  would be noise — the durable knowledge is "how we handle Sales Invoices",
  not one invoice); everything else is not wiki-worthy.

Slug grammar (see the Jarvis Wiki Page controller): ``--`` separates the type
prefix from the scrubbed record name, single hyphens separate words inside
each half.
"""

from __future__ import annotations

import re

import frappe

MSG = "Jarvis Chat Message"

# Master doctypes -> their wiki page_type (per-record pages).
WIKI_PAGE_TYPES = {
	"Customer": "Customer",
	"Supplier": "Supplier",
	"Item": "Item",
}

# Transactional doctypes get one Doctype-level page each (ref_name None).
TRANSACTIONAL_DOCTYPES = frozenset(
	{
		"Sales Invoice",
		"Sales Order",
		"Purchase Invoice",
		"Purchase Order",
		"Delivery Note",
		"Purchase Receipt",
		"Payment Entry",
		"Journal Entry",
		"Stock Entry",
	}
)

_MAX_ENTITIES = 20
# Fetch window before dedupe: tool-heavy turns repeat the same doc many times.
_SCAN_ROWS = 100


def refs_from_tool(args: dict | None, result) -> tuple[str | None, str | None]:
	"""Best-effort ``(ref_doctype, ref_name)`` for one tool call, or
	``(None, None)``. Wraps :func:`jarvis.audit._ref` (same arg-name coverage:
	doctype/name/docname/target_* + result-doc fallback); never raises."""
	try:
		from jarvis import audit

		doctype, name, _method = audit._ref(args if isinstance(args, dict) else {}, result)
		if doctype and name:
			return str(doctype), str(name)
	except Exception:
		pass
	return None, None


def entities_for_turn(conversation: str, after_seq: int) -> list[dict]:
	"""Distinct ``{"doctype", "name"}`` refs from the conversation's
	``role=tool`` message rows with ``seq > after_seq`` (newest first, max
	20). ``after_seq=0`` scans the whole conversation's recent tool rows."""
	if not conversation:
		return []
	rows = frappe.get_all(
		MSG,
		filters={
			"conversation": conversation,
			"role": "tool",
			"seq": [">", int(after_seq or 0)],
			"ref_doctype": ["is", "set"],
		},
		fields=["ref_doctype", "ref_name"],
		order_by="seq desc",
		limit_page_length=_SCAN_ROWS,
	)
	out: list[dict] = []
	seen: set[tuple[str, str]] = set()
	for r in rows:
		doctype = (r.get("ref_doctype") or "").strip()
		name = (r.get("ref_name") or "").strip()
		if not doctype or not name or (doctype, name) in seen:
			continue
		seen.add((doctype, name))
		out.append({"doctype": doctype, "name": name})
		if len(out) >= _MAX_ENTITIES:
			break
	return out


def scrub(value: str) -> str:
	"""One slug half: lowercase, non-alnum runs folded to single hyphens.
	``"ACME Corp (EU) Ltd."`` -> ``"acme-corp-eu-ltd"``."""
	return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def page_ref_for(doctype: str, name: str) -> dict | None:
	"""The wiki page identity an entity maps to, or None when the entity is
	not wiki-worthy. Party/item pages are per-record
	(``customer--<scrub(name)>``); transactional doctypes map to one
	doctype-level page (``doctype--<scrub(doctype)>``, ``ref_name`` None)."""
	doctype = (doctype or "").strip()
	name = (name or "").strip()
	if not doctype:
		return None
	page_type = WIKI_PAGE_TYPES.get(doctype)
	if page_type:
		scrubbed = scrub(name)
		if not scrubbed:
			return None
		slug = f"{page_type.lower()}--{scrubbed}"[:140].rstrip("-")
		return {
			"page_type": page_type,
			"ref_doctype": doctype,
			"ref_name": name,
			"slug": slug,
		}
	if doctype in TRANSACTIONAL_DOCTYPES:
		return {
			"page_type": "Doctype",
			"ref_doctype": doctype,
			"ref_name": None,
			"slug": f"doctype--{scrub(doctype)}",
		}
	return None
