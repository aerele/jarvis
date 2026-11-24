"""The per-turn "connected apps" prompt line.

Without it the agent did not reach for an available connector on its own (a
docs question went unanswered while Microsoft Learn sat connected). The line
names the caller's usable connectors with the catalog's one-line description,
so the agent knows ``list_connector_actions`` / ``call_connector`` can help.

Usable means what ``call_connector`` would accept: visible to the caller
(Shared, or their own Personal row, Personal winning on a shared key), enabled,
with a passing connection test, at least one allowed action, and (for a
sign-in connector) connected by the caller. The rows and the readiness verdict
come from the same code ``list_connector_actions`` uses, so the two can never
disagree.

Every usable app is named. Descriptions are added only while the whole line
stays short; past the name budget the line ends with a pointer to the full
listing, so no app is ever silently missing.
"""

from __future__ import annotations

from jarvis._session import impersonate
from jarvis.chat.connector_focus import one_line
from jarvis.connectors import action_rows, catalog, readiness
from jarvis.tools.list_connector_actions import _resolve_connectors

DESCRIBED_MAX_CHARS = 600
NAMES_MAX_CHARS = 1000
_PREFIX = "[Connected apps you can use via list_connector_actions / call_connector: "
_ROW_FIELDS = ("preset", *readiness.ROW_FIELDS)


def prompt_line(user: str) -> str:
	"""The bracket line for ``user``, or "" when they have no usable connector.
	The resolver is permission checked and runs as ``user`` (the chat user the
	tool calls also run as), so another user's Personal row never appears."""
	with impersonate(user):
		rows = list(_resolve_connectors(None, extra_fields=_ROW_FIELDS).values())
		children = action_rows.by_parent([row["name"] for row in rows], list(readiness.ACTION_FIELDS))
		why_not = readiness.reasons(rows, children)
	usable = [row for row in rows if why_not[row["name"]] is None]
	return _render([_name(row) for row in usable], [_entry(row) for row in usable])


def _name(row: dict) -> str:
	return one_line(row.get("label") or row.get("key"))


def _entry(row: dict) -> str:
	provider = catalog.by_name(row.get("preset") or "")
	description = one_line(provider.description) if provider else ""
	return f"{_name(row)} ({description})" if description else _name(row)


def _render(names: list[str], entries: list[str]) -> str:
	"""Descriptions when the whole line fits ``DESCRIBED_MAX_CHARS``, else names
	only. Names that still do not fit ``NAMES_MAX_CHARS`` end with "and N more"
	pointing at the full listing; an app is dropped whole, never cut mid-name."""
	if not names:
		return ""
	line = _line(entries)
	if len(line) <= DESCRIBED_MAX_CHARS:
		return line
	return _capped_names(names)


def _capped_names(names: list[str]) -> str:
	line = _line(names)
	if len(line) <= NAMES_MAX_CHARS:
		return line
	kept = len(names)
	while kept > 1 and len(_line(_with_more(names, kept))) > NAMES_MAX_CHARS:
		kept -= 1
	return _line(_with_more(names, kept))


def _with_more(names: list[str], kept: int) -> list[str]:
	more = len(names) - kept
	return [*names[:kept], f"and {more} more: call list_connector_actions for the full list"]


def _line(parts: list[str]) -> str:
	return f"{_PREFIX}{', '.join(parts)}]"
