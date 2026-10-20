"""The per-turn "connected apps" prompt line.

Without it the agent did not reach for an available connector on its own (a
docs question went unanswered while Microsoft Learn sat connected). The line
names the caller's usable connectors with the catalog's one-line description,
so the agent knows ``list_connector_actions`` / ``call_connector`` can help.

Usable means what ``call_connector`` would accept: visible to the caller
(Shared, or their own Personal row, Personal winning on a shared key), enabled,
and with a passing connection test on file. The rows come from the resolver
``list_connector_actions`` itself uses, so the two can never disagree.
"""

from __future__ import annotations

from jarvis._session import impersonate
from jarvis.chat.connector_focus import one_line
from jarvis.connectors import catalog
from jarvis.tools.list_connector_actions import _resolve_connectors

MAX_APPS = 12
MAX_CHARS = 400
_PREFIX = "[Connected apps you can use via list_connector_actions / call_connector: "


def prompt_line(user: str) -> str:
	"""The bracket line for ``user``, or "" when they have no usable connector.
	The resolver is permission checked and runs as ``user`` (the chat user the
	tool calls also run as), so another user's Personal row never appears."""
	with impersonate(user):
		rows = _resolve_connectors(None, extra_fields=("preset", "last_test_status")).values()
	entries = [_entry(row) for row in rows if row.get("last_test_status") == "Passed"]
	return _render(entries[:MAX_APPS])


def _entry(row: dict) -> str:
	name = one_line(row.get("label") or row.get("key"))
	provider = catalog.by_name(row.get("preset") or "")
	description = one_line(provider.description) if provider else ""
	return f"{name} ({description})" if description else name


def _render(entries: list[str]) -> str:
	"""Join whole entries while the line stays within ``MAX_CHARS``; an app
	that does not fit is dropped rather than cut mid-name."""
	kept: list[str] = []
	for entry in entries:
		if len(_PREFIX) + len(", ".join([*kept, entry])) + 1 > MAX_CHARS:
			break
		kept.append(entry)
	return f"{_PREFIX}{', '.join(kept)}]" if kept else ""
