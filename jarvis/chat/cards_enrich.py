"""Backend enrichment for ``jarvis-cards`` blocks: guarantee a browsable record
card never renders as a bare id.

The ``jarvis-cards`` block is authored by the agent. The SPA renders every field a
card supplies and, when a card's ``fields`` array is empty, falls back to the record
id as the card's only visible value (``frontend/src/views/ChatView.vue`` card parse).
The persona (skill A) tells the agent to fill ``fields`` dynamically, but that is model
guidance; this module is the deterministic floor: any card that arrives with a
``doctype`` + ``name`` but no ``fields`` is filled server-side from the record's own
schema, so a card CANNOT render id-only regardless of what the model emitted.

Two invariants:
  - IDEMPOTENT + non-destructive. A card that already carries ``fields`` is left
    exactly as the agent wrote it (skill A's dynamic choice wins); a re-run over
    already-enriched content is a no-op. Only EMPTY cards are touched.
  - PERMISSION-SAFE by delegation. Every value comes from
    ``_record_summary.summary_rows``, which does its own read + field-level
    permission check and secret masking under the CURRENT user. So the enrichment
    runs impersonating the message OWNER, never the ambient finalize/worker user.

Best-effort throughout: a parse error, a missing record, or a record the user cannot
read leaves the card untouched. Nothing here raises into the turn.
"""

from __future__ import annotations

import json
import re

import frappe

from jarvis._session import impersonate
from jarvis.chat import _record_summary

MSG = "Jarvis Chat Message"

# Mirrors ``ChatView.vue`` ``_CARDS_RE`` byte-for-byte (the fence the SPA parses):
# a ```jarvis-cards fence, optional trailing spaces/tabs, a newline, the JSON body,
# then the closing fence. Non-greedy so each block matches to its own close.
_CARDS_RE = re.compile(r"```jarvis-cards[ \t]*\n([\s\S]*?)```")

# Bound the per-message doc reads: a card strip is for browsing a handful of records,
# not a report. Cards beyond this cap keep whatever the agent gave them.
_MAX_CARDS = 20


def _enrich_card(card: dict) -> bool:
	"""Fill one id-only card's ``fields`` from the record's schema. Returns True iff
	the card was changed.

	Left untouched (returns False) when: it is not a dict; it already has ``fields``
	(the agent's own choice is authoritative); it lacks a ``doctype`` + ``name`` to
	identify the record; or the record is missing / unreadable by this user
	(``summary_rows`` returns None or no rows)."""
	if not isinstance(card, dict):
		return False
	if card.get("fields"):
		return False
	doctype = str(card.get("doctype") or "").strip()
	name = str(card.get("name") or "").strip()
	if not doctype or not name:
		return False
	summary = _record_summary.summary_rows(doctype, name)
	if not summary or not summary.get("rows"):
		return False
	card["fields"] = summary["rows"]
	# Promote the record's human title when the card is titled by its id (or blank),
	# so the header reads as a name, not a code. summary_rows' title is the
	# permission-checked title-field value.
	title = str(card.get("title") or "").strip()
	if summary.get("title") and (not title or title == name):
		card["title"] = summary["title"]
	return True


def _enrich_block(raw: str) -> tuple[str, bool]:
	"""Enrich one block's JSON body. Returns ``(new_raw, changed)``. Malformed JSON,
	or a body that is not ``{cards: [...]}``, is returned unchanged."""
	try:
		data = json.loads(raw)
	except (ValueError, TypeError):
		return raw, False
	if not isinstance(data, dict):
		return raw, False
	cards = data.get("cards")
	if not isinstance(cards, list):
		return raw, False
	changed = False
	for card in cards[:_MAX_CARDS]:
		if _enrich_card(card):
			changed = True
	if not changed:
		return raw, False
	return json.dumps(data, ensure_ascii=False), True


def enrich_text(content: str, *, owner: str | None = None) -> str:
	"""Enrich every ``jarvis-cards`` block in ``content`` and return the (possibly
	unchanged) text. Reads run under ``owner``'s permissions. Best-effort: any failure
	returns the original content verbatim."""
	if not content or "jarvis-cards" not in content:
		return content

	def _run() -> str:
		def _sub(m: re.Match) -> str:
			new_raw, changed = _enrich_block(m.group(1))
			if not changed:
				return m.group(0)
			return f"```jarvis-cards\n{new_raw}\n```"

		return _CARDS_RE.sub(_sub, content)

	try:
		if owner and owner != frappe.session.user:
			with impersonate(owner):
				return _run()
		return _run()
	except Exception:
		# A card is a convenience, never worth risking the turn. Clear any message
		# a failed read queued so it cannot leak into the reply, and degrade to the
		# original content (an id-only card, not a broken turn).
		frappe.clear_messages()
		return content


def enrich_message(message_name: str, owner: str | None = None) -> None:
	"""Read a ``Jarvis Chat Message``'s content, enrich its cards, and write the result
	back when it changed. Idempotent (re-run is a no-op once cards carry fields) and
	best-effort (never raises)."""
	try:
		row = frappe.db.get_value(MSG, message_name, ["content", "owner"], as_dict=True)
		if not row or not row.get("content"):
			return
		new = enrich_text(row["content"], owner=owner or row.get("owner"))
		if new != row["content"]:
			frappe.db.set_value(MSG, message_name, "content", new)
	except Exception:
		frappe.clear_messages()
