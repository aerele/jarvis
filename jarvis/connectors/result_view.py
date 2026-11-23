"""What a connector's answer looks like to the agent. NO frappe import.

An MCP ``tools/call`` result is ``{content: [...], structuredContent: {...}}``. These
helpers turn it into one bounded, scrubbed string, for the receipt the agent reads
after a human confirms a connector write."""

from __future__ import annotations

import json

from jarvis.connectors.secret_scrub import scrub_text


def payload_text(mcp_result) -> str:
	"""The result as text. ``structuredContent`` comes first, as compact JSON; a text
	item that only repeats it is dropped. Text items follow, other content types
	become short markers (``[image omitted]``, ``[resource: <uri>]``)."""
	if not isinstance(mcp_result, dict):
		return ""
	structured = mcp_result.get("structuredContent")
	parts = [] if structured in (None, {}, []) else [_compact(structured)]
	for item in mcp_result.get("content") or []:
		part = _item_text(item, structured)
		if part:
			parts.append(part)
	return " ".join(parts)


def receipt_payload(mcp_result, limit: int) -> str:
	"""``payload_text`` scrubbed, then bounded to ``limit`` characters. A cut ends with
	``[cut: showed N of M characters]``. Scrubbing comes first, so a secret that
	straddles the cut is never half shown."""
	text, _count = scrub_text(payload_text(mcp_result))
	text = text.strip()
	if len(text) <= limit:
		return text
	return f"{text[:limit]} [cut: showed {limit} of {len(text)} characters]"


def _compact(value) -> str:
	try:
		return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
	except (TypeError, ValueError):
		return str(value)


def _item_text(item, structured) -> str:
	if not isinstance(item, dict):
		return ""
	kind = item.get("type")
	if kind == "text":
		text = str(item.get("text") or "")
		return "" if _repeats(text, structured) else text
	if kind in ("resource", "resource_link"):
		resource = item.get("resource") if isinstance(item.get("resource"), dict) else item
		uri, _count = scrub_text(resource.get("uri") or "")
		return f"[resource: {uri}]"
	return f"[{kind or 'content'} omitted]"


def _repeats(text: str, structured) -> bool:
	"""True when the text item is just the structured payload serialized again."""
	if structured in (None, {}, []):
		return False
	try:
		return json.loads(text) == structured
	except ValueError:
		return False
