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


# ---------------------------------------------------------------------------
# The inline answer the agent reads (the agent path, not the confirmed receipt).
# ---------------------------------------------------------------------------

_HINT = (
	"For the rest, narrow the request (filters, page size, fields) and ask again. "
	"Never rebuild the full set in chunks."
)
_BOUND_META = ("_truncated", "note")
_MAX_CUTS_NAMED = 3


def bound_tool_result(data, budget: int) -> tuple:
	"""Cut a connector answer ``{"ok": True, "result": <MCP result>}`` to ``budget`` characters.

	Returns ``(out, cuts)``. ``cuts is None``: ``data`` fits and is returned as is.
	``cuts == []``: only a ``structuredContent`` that repeats the text was dropped (no
	loss). Otherwise ``cuts`` lists every cut part as ``{where, unit, shown, total}``,
	``unit`` being ``items`` (a JSON list was cut, whole items kept) or ``characters``.

	The shape the plugin reads stays: ``ok``, ``result.content`` (text items),
	``result.isError``. A cut adds ``_truncated`` / ``shown_<unit>`` / ``total_<unit>`` /
	``note`` before ``result``, and one last text item saying the same in a line, so the
	cut is visible whichever of the two the agent runtime shows. Never raises on shape:
	an answer that is not ``{result: {...}}`` is returned as is."""
	if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
		return data, None
	if len(_compact(data)) <= budget:
		return data, None
	return _ResultBounder(data, budget).run()


class _ResultBounder:
	"""Works on copies, so the caller's ``data`` is never mutated."""

	def __init__(self, data: dict, budget: int):
		self.data = data
		self.budget = budget
		self.mcp = dict(data["result"])
		content = self.mcp.get("content")
		self.content = (
			[dict(i) if isinstance(i, dict) else i for i in content] if isinstance(content, list) else None
		)
		self.cuts: list[dict] = []

	def run(self) -> tuple:
		self._drop_repeated_structured()
		if not self._fits() and self._content_is_the_bulk():
			self._cut_content_prefix()
		if not self._fits():
			for part in self._parts_largest_first():
				if self._fits():
					break
				self._cut(part)
		if not self._fits():
			self._fallback()
		return self._build(), self.cuts

	# -- the output ---------------------------------------------------------

	def _build(self) -> dict:
		mcp = dict(self.mcp)
		if self.content is not None:
			mcp["content"] = self.content + ([self._marker_item()] if self.cuts else [])
		out = {k: v for k, v in self.data.items() if k != "result" and k not in _BOUND_META}
		if self.cuts:
			first = self.cuts[0]
			out["_truncated"] = True
			out[f"shown_{first['unit']}"] = first["shown"]
			out[f"total_{first['unit']}"] = first["total"]
			out["note"] = self._note()
		out["result"] = mcp
		return out

	def _fits(self) -> bool:
		return len(_compact(self._build())) <= self.budget

	def _where_cut(self) -> str:
		# Capped, so the marker stays short whatever the number of cut parts.
		shown = self.cuts[:_MAX_CUTS_NAMED]
		text = "; ".join(f"{c['where']}: first {c['shown']} of {c['total']} {c['unit']}" for c in shown)
		more = len(self.cuts) - len(shown)
		return f"{text}; and {more} more parts cut" if more > 0 else text

	def _note(self) -> str:
		return f"Result cut to fit the context window ({self._where_cut()}). PARTIAL: do not present it as complete, and tell the person it was cut. {_HINT}"

	def _marker_item(self) -> dict:
		return {
			"type": "text",
			"text": f"[cut: {self._where_cut()}. Narrow the request (filters, page size, fields) to see the rest.]",
		}

	# -- the cuts -----------------------------------------------------------

	def _drop_repeated_structured(self) -> None:
		structured = self.mcp.get("structuredContent")
		if structured in (None, {}, []) or not self.content:
			return
		if any(_item_repeats(i, structured) for i in self.content):
			del self.mcp["structuredContent"]

	def _content_is_the_bulk(self) -> bool:
		"""Many content items (one per record is a valid MCP shape) are cut as a list."""
		if not self.content or len(self.content) < 2:
			return False
		structured = self.mcp.get("structuredContent")
		return structured is None or len(_compact(self.content)) >= len(_compact(structured))

	def _cut_content_prefix(self) -> None:
		"""Keep the largest prefix of whole content items that fits: log2(N) dumps in all.
		When not even the first item fits, only that item is kept and the per-item cut
		takes it from there."""
		full = self.content
		cut = self._start_cut("content", "items", len(full))

		def apply(k: int) -> None:
			self.content = full[:k]

		if self._largest_fit(len(full), cut, apply) == 0:
			cut["shown"] = 1
			self.content = full[:1]

	def _parts_largest_first(self) -> list:
		parts = [
			(len(_compact(i)), ("item", n)) for n, i in enumerate(self.content or []) if isinstance(i, dict)
		]
		if "structuredContent" in self.mcp:
			parts.append((len(_compact(self.mcp["structuredContent"])), ("structured", 0)))
		return [part for _size, part in sorted(parts, key=lambda p: -p[0])]

	def _cut(self, part: tuple) -> None:
		kind, n = part
		if kind == "structured":
			self._cut_structured()
		elif self.content[n].get("type") == "text":
			self._cut_text_item(n)
		else:
			self._replace_item(n)

	def _cut_text_item(self, n: int) -> None:
		item = self.content[n]
		raw = str(item.get("text") or "")
		shape = _list_shape(raw)
		if shape and self._cut_items(
			f"text item {n + 1}", shape, lambda value: item.update(text=_compact(value))
		):
			return
		cut = self._start_cut(f"text item {n + 1}", "characters", len(raw))
		self._largest_fit(min(len(raw), self.budget), cut, lambda k: item.update(text=raw[:k]))

	def _cut_structured(self) -> None:
		structured = self.mcp["structuredContent"]
		shape = _list_shape(structured)
		if shape and self._cut_items(
			"structured data", shape, lambda value: self.mcp.update(structuredContent=value)
		):
			return
		# A dict with no list in it cannot be cut by parts: it goes, and the cut says so.
		cut = self._start_cut("structured data", "characters", len(_compact(structured)))
		del self.mcp["structuredContent"]
		cut["shown"] = 0

	def _cut_items(self, where: str, shape: tuple, store) -> bool:
		"""Keep the largest prefix of whole items that fits. False (and nothing kept)
		when not even one item fits: the caller then cuts by characters."""
		value, key = shape
		items = value if key is None else value[key]
		cut = self._start_cut(where, "items", len(items))

		def apply(k: int) -> None:
			store(items[:k] if key is None else {**value, key: items[:k]})

		if self._largest_fit(len(items), cut, apply) > 0:
			return True
		self.cuts.remove(cut)
		return False

	def _replace_item(self, n: int) -> None:
		"""A big non-text item (an image, a blob) becomes a one-line text item."""
		item = self.content[n]
		cut = self._start_cut(
			f"item {n + 1} ({item.get('type') or 'content'})", "characters", len(_compact(item))
		)
		cut["shown"] = 0
		self.content[n] = {"type": "text", "text": f"[{item.get('type') or 'content'} omitted: too large]"}

	def _fallback(self) -> None:
		"""Last resort, for an answer whose bulk sits outside content: keep only the flag."""
		total = len(_compact(self.data["result"]))
		self.cuts = [{"where": "result", "unit": "characters", "shown": 0, "total": total}]
		self.mcp = {k: v for k, v in self.mcp.items() if k == "isError"}
		self.content = []

	def _start_cut(self, where: str, unit: str, total: int) -> dict:
		cut = {"where": where, "unit": unit, "shown": 0, "total": total}
		self.cuts.append(cut)
		return cut

	def _largest_fit(self, hi: int, cut: dict, apply) -> int:
		"""Binary search the largest ``k <= hi`` whose result fits (0 when none). A longer
		prefix is never smaller, so the search is sound."""
		lo = 0
		while lo < hi:
			mid = (lo + hi + 1) // 2
			cut["shown"] = mid
			apply(mid)
			if self._fits():
				lo = mid
			else:
				hi = mid - 1
		cut["shown"] = lo
		apply(lo)
		return lo


def _list_shape(value) -> tuple | None:
	"""``(value, key)`` when ``value`` (a JSON string or a parsed value) is a non-empty
	list (``key`` None) or a dict with a list in it (``key`` the largest one), else None."""
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			return None
	if isinstance(value, list):
		return (value, None) if value else None
	if isinstance(value, dict):
		lists = {k: v for k, v in value.items() if isinstance(v, list) and v}
		if lists:
			return value, max(lists, key=lambda k: len(_compact(lists[k])))
	return None


def _item_repeats(item, structured) -> bool:
	return (
		isinstance(item, dict)
		and item.get("type") == "text"
		and _repeats(str(item.get("text") or ""), structured)
	)
