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


# Scrubbing is not linear for every pattern, and an answer can be megabytes: only a window
# just past the cut is scrubbed. The margin keeps a secret that straddles the cut whole
# inside the window, so it is masked before the cut and never half shown.
_WINDOW_MARGIN = 4096


def receipt_payload(mcp_result, limit: int) -> str:
	"""``payload_text`` scrubbed, then bounded to ``limit`` characters. A cut ends with
	``[cut: showed N of M characters]``, M being the full length of the answer."""
	text, total = scrubbed_window(payload_text(mcp_result), limit)
	if total <= limit:
		return text
	return f"{text} [cut: showed {len(text)} of {total} characters]"


def scrubbed_window(raw: str, limit: int) -> tuple[str, int]:
	"""``(text, total)``: the first ``limit`` characters of ``raw`` after scrubbing, and
	the length of the whole of ``raw``. Only ``limit`` plus a margin is scrubbed, so the
	cost is bounded whatever the size of ``raw``. A key block whose end lies past the
	window is masked to the window's end (``scrub_text``)."""
	head = raw[: limit + _WINDOW_MARGIN]
	scrubbed, _count = scrub_text(head)
	return scrubbed.strip()[:limit], len(raw)


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
