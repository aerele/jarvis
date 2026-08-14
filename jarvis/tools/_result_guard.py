import json
from typing import Any

# Model-facing char budget for a single agent tool result, enforced on the
# openclaw-agent path (jarvis.api._dispatch_from_session) - NOT in get_list/query,
# which the dashboard builder/renderer also call. Compact + ensure_ascii=False so
# it matches what openclaw feeds the model and does not over-count non-Latin
# scripts. ~9K tokens, with headroom for openclaw's ~1.3x tool_call envelope.
MAX_RESULT_CHARS = 35_000

# Top-level list-valued keys we know how to truncate: query -> "rows",
# run_report -> "result". A bare list (get_list) is handled directly.
_ROW_KEYS = ("rows", "result")
_SAFETY = 200  # pad for note-digit / separator variance below the hard cap

_NOTE = (
	"Result truncated to fit the context window: showing the first {shown} of "
	"{total} rows. This is PARTIAL - do not treat it as complete, and any file, "
	"summary, or count built from these rows is also partial. To get the full "
	"answer: narrow the filter, aggregate with query / run_report, or request "
	"specific rows."
)
_NOTE_NONE = (
	"Result too large: even one row exceeds the size budget. Request fewer fields "
	"(avoid fields=['*']), narrow the query, or aggregate with query / run_report."
)


def _size(obj) -> int:
	return len(json.dumps(obj, default=str, ensure_ascii=False, separators=(",", ":")))


def _find_list(data):
	if isinstance(data, list):
		return "bare", None, data
	if isinstance(data, dict):
		for k in _ROW_KEYS:
			if isinstance(data.get(k), list):
				return "nested", k, data[k]
	return None, None, None


def _fit(rows: list, budget: int) -> list:
	"""Largest prefix of ``rows`` whose serialized size is <= ``budget`` (may be [])."""
	if not rows or budget <= 0:
		return []
	if _size(rows) <= budget:
		return rows
	avg = max(1, _size(rows) // len(rows))
	k = min(len(rows), max(0, budget // avg))
	while k > 0 and _size(rows[:k]) > budget:
		k -= max(1, k // 5)
	return rows[: max(0, k)]


def enforce_result_budget(data, tool: str) -> tuple[Any, dict | None]:
	try:
		total = _size(data)
	except Exception:
		return data, None  # never let the guard itself break a call
	if total <= MAX_RESULT_CHARS:
		return data, None

	kind, key, rows = _find_list(data)
	if rows is None:
		return data, {"tool": tool, "kind": "uncapped", "original_chars": total, "shown": None, "total": None}

	n = len(rows)
	if kind == "bare":
		overhead = _size({"_truncated": True, "shown": n, "total": n, "note": _NOTE_NONE, "rows": []})
	else:
		skel = {k: v for k, v in data.items() if k != key}
		skel.update({"_truncated": True, "shown": n, "total": n, "note": _NOTE_NONE, key: []})
		overhead = _size(skel)

	row_budget = MAX_RESULT_CHARS - overhead - _SAFETY
	if row_budget <= 0:
		# Non-row bulk (e.g. a huge query `sql`) is itself over budget: truncating
		# rows can't help and would lie. Pass through unchanged.
		return data, {
			"tool": tool,
			"kind": "uncapped_nonrow",
			"original_chars": total,
			"shown": None,
			"total": None,
		}

	kept = _fit(rows, row_budget)
	note = _NOTE.format(shown=len(kept), total=n) if kept else _NOTE_NONE
	if kind == "bare":
		out = {"_truncated": True, "shown": len(kept), "total": n, "note": note, "rows": kept}
	else:
		out = dict(data)
		out[key] = kept
		out["_truncated"] = True
		out["shown"] = len(kept)
		out["total"] = n
		out["note"] = note
	return out, {"tool": tool, "kind": "truncated", "original_chars": total, "shown": len(kept), "total": n}
