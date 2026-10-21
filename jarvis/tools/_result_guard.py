import json
from typing import Any

# Model-facing char budget for a single agent tool result, enforced on the
# agent path (jarvis.api._dispatch_from_session) - NOT in get_list/query,
# which the dashboard builder/renderer also call. Compact + ensure_ascii=False so
# it matches what the agent runtime feeds the model and does not over-count
# non-Latin scripts. ~9K tokens, with headroom for the runtime's ~1.3x tool_call
# envelope.
MAX_RESULT_CHARS = 35_000

# Top-level list-valued keys we know how to truncate: query -> "rows",
# run_report -> "result". A bare list (get_list) is handled directly.
_ROW_KEYS = ("rows", "result")
# Metadata keys the guard owns in a truncation envelope; a colliding data key of
# the same name must never overwrite the guard's authoritative value.
_META_KEYS = ("_truncated", "shown", "total", "note")
_SAFETY = 200  # pad for note-digit / separator variance below the hard cap

# Routing help (model-facing): the full-data escape is a FILE built server-side,
# which this cap never touches: export_query for records (doctype + filters, or the
# same query spec the agent just ran, joins and NOT EXISTS included) and report_pdf
# for a saved report. export_excel is not one: its rows pass through the model.
# Generic/legacy results have no reliable continuation. Paged get_list below
# updates its own next_start to the rows actually retained by this guard.
_NOTE = (
	"Result truncated to fit the context window: showing the first {shown} of "
	"{total} rows. PARTIAL - do not treat as complete; any file, summary, or count "
	"built from these rows is partial too. For the full answer: narrow the filter, "
	"aggregate with query, or put every row in a file with export_query (pass the "
	"same spec, or doctype + filters; built server-side, complete). A saved report: "
	"report_pdf. Never rebuild the full set in chunks through chat."
)
_NOTE_NONE = (
	"Result too large: even one row exceeds the size budget. Request fewer fields "
	"(avoid fields=['*']), narrow the filter, aggregate with query, or put the rows "
	"in a file with export_query (server-side, complete); a saved report: report_pdf."
)
# A paged get_list page: the guard keeps a prefix, so the walk simply resumes after
# it. No "of N" here - N would be the page size, which reads as the match count.
_PAGED_NOTE = (
	"Page cut to {shown} rows to fit the context window; more rows follow. Continue "
	"with start={next_start} and the same filters, fields and order_by. A limit near "
	"{shown} avoids re-reading rows."
)
_PAGED_NOTE_CEILING = (
	"Page cut to {shown} rows to fit the context window and the paging ceiling is "
	"reached. Use a narrower filter, aggregate with query, or a report."
)
# Longest of the three: the size overhead is measured with this one.
_PAGED_NOTE_NONE = (
	"Page cut to zero rows: even one row exceeds the size budget, so this page cannot "
	"be continued as asked. Request fewer fields (avoid fields=['*']) or narrow the "
	"filter and ask again from the same start, or aggregate with query."
)
_NOTE_NONROW = (
	"Result too large: the non-row content (e.g. the SQL text or columns) alone "
	"exceeds the size budget, so ALL rows were dropped. PARTIAL, showing zero rows. "
	"Narrow the query so the surrounding content is smaller, or use report_pdf for "
	"a saved report (server-side, returns the full data as a file, no cap)."
)


def _size(obj) -> int:
	return len(_json_text(obj))


def _json_text(obj) -> str:
	"""The compact JSON the agent runtime feeds the model. get_doc's text window
	slices a list or dict value in this same form."""
	return json.dumps(obj, default=str, ensure_ascii=False, separators=(",", ":"))


def _is_list_page(data: dict) -> bool:
	"""A list-page-v1 page (get_doc's child_table page), not a document that merely
	has a field called list_contract."""
	return data.get("list_contract") == "list-page-v1" and isinstance(data.get("rows"), list)


def _find_list(data) -> tuple[str | None, str | None, list | None]:
	if isinstance(data, list):
		return "bare", None, data
	if isinstance(data, dict):
		for k in _ROW_KEYS:
			if isinstance(data.get(k), list):
				return "nested", k, data[k]
	return None, None, None


def _fit(rows: list, budget: int) -> list:
	"""Largest prefix of ``rows`` whose serialized size is <= ``budget`` (may be []).

	``_size(rows[:k])`` is monotonic non-decreasing in ``k``, so binary-search the
	largest fitting ``k``. This keeps a small leading row that fits even when later
	rows are huge (an average-based estimate would wrongly drop it)."""
	if not rows or budget <= 0:
		return []
	lo, hi = 0, len(rows)
	while lo < hi:
		mid = (lo + hi + 1) // 2
		if _size(rows[:mid]) <= budget:
			lo = mid
		else:
			hi = mid - 1
	return rows[:lo]


def _envelope(data, kind: str, key: str | None, kept: list, n: int, note: str) -> dict:
	"""Build the ``_truncated`` PARTIAL envelope. Metadata (``_truncated``/``shown``/
	``total``/``note``) is placed BEFORE the row key so it survives the chat SPA's
	4000-char pretty-print slice even when the surviving/non-row content is large."""
	if kind == "bare":
		return {"_truncated": True, "shown": len(kept), "total": n, "note": note, "rows": kept}
	out: dict = {"_truncated": True, "shown": len(kept), "total": n, "note": note}
	if data.get("result_id"):
		# The query wrote this sentence knowing its own limit; restating it here would drift.
		own = data.get("note") or (
			f"This answer is partial. For every matching row, call export_query(result_id='{data['result_id']}'). "
			"Data is fresh at export time."
		)
		out["note"] = f"{own} Never rebuild the file in chunks through chat."
	# Skip the row key AND the four metadata keys: a tool result carrying its own
	# top-level "total"/"note"/"shown"/"_truncated" must not clobber the guard's
	# authoritative values (metadata is emitted first for the debug view, but must
	# always win over a colliding data key).
	for k, v in data.items():
		if k != key and k not in _META_KEYS:
			out[k] = v
	out[key] = kept
	if data.get("list_contract") == "list-page-v1":
		# Never leave a pre-truncation completeness claim or skip omitted rows.
		original = data.get("coverage")
		coverage = dict(original) if isinstance(original, dict) else {}
		start = coverage.get("start")
		valid_start = type(start) is int and 0 <= start <= 100_000
		next_start = start + len(kept) if valid_start else None
		coverage.update(
			returned=len(kept),
			has_more=True,
			complete=False,
			next_start=next_start if kept and next_start is not None and next_start <= 100_000 else None,
		)
		out["coverage"] = coverage
		out["note"] = _paged_note(len(kept), coverage["next_start"])
		del out["total"]
	return out


def _paged_note(shown: int, next_start: int | None) -> str:
	if not shown:
		return _PAGED_NOTE_NONE
	if next_start is None:
		return _PAGED_NOTE_CEILING.format(shown=shown)
	return _PAGED_NOTE.format(shown=shown, next_start=next_start)


def enforce_result_budget(data, tool: str) -> tuple[Any, dict | None]:
	"""Cap a tool result's model-facing serialized size on the agent path.

	Returns ``(data, event)``. ``event is None`` means the result is within budget
	(or an empty/bare edge) and ``data`` is returned unchanged. Otherwise ``event``
	is a dict whose ``kind`` names the outcome for telemetry:

	  * ``"truncated"``       - a list-shaped result was cut to the rows that fit;
	                            ``data`` is replaced by a PARTIAL envelope.
	  * ``"bounded_document"`` - a get_doc document (or batch) was cut to fit:
	                            child tables to their first rows, long text to a
	                            preview, with a ``_partial`` block naming each cut
	                            part and the get_doc call that reads it.
	                            ``shown``/``total`` count child rows (documents
	                            for a batch).
	  * ``"uncapped"``        - no truncatable top-level row list; passed through
	                            unchanged.
	  * ``"uncapped_nonrow"`` - a row list exists but the surrounding non-row
	                            content (e.g. a huge SQL string) alone exceeds the
	                            budget, so ALL rows are dropped to bound the blast
	                            radius; ``data`` is NOT returned unchanged.
	  * ``"measure_failed"``  - the result could not be serialized/measured; passed
	                            through unchanged (distinct from an in-budget None).

	Never raises: a measurement failure yields a ``measure_failed`` event and the
	original ``data``."""
	try:
		total = _size(data)
	except Exception:
		# Could not even measure the result: do not truncate blind, but surface it
		# as its own signal (distinct from an in-budget "small" result -> None).
		return data, {"kind": "measure_failed", "original_chars": None, "shown": None, "total": None}
	if total <= MAX_RESULT_CHARS:
		return data, None

	if tool == "get_doc" and isinstance(data, dict) and not _is_list_page(data):
		# A document is checked before _find_list: a child table named "rows" or
		# "result" must not get the row envelope, whose top-level total/note
		# would overwrite real fields of the same name. A child-table page is a
		# list page and takes the row path. Imported here because _doc_budget
		# measures with this module's _size.
		from jarvis.tools._doc_budget import bound_get_doc_result

		out, shown, parts = bound_get_doc_result(data, MAX_RESULT_CHARS - _SAFETY)
		# shown/total count child rows (documents, for a batch), not chars.
		return out, {"kind": "bounded_document", "original_chars": total, "shown": shown, "total": parts}

	kind, key, rows = _find_list(data)
	if rows is None:
		return data, {"kind": "uncapped", "original_chars": total, "shown": None, "total": None}

	n = len(rows)
	# Measured with the longer of the two notes the envelope can carry, so the note
	# itself can never push the result past the cap (_SAFETY pads digits only).
	longest = max(_NOTE.format(shown=n, total=n), _NOTE_NONE, key=len)
	overhead = _size(_envelope(data, kind, key, [], n, longest))
	row_budget = MAX_RESULT_CHARS - overhead - _SAFETY
	if row_budget <= 0:
		# Non-row bulk (e.g. a huge query `sql`) is itself over budget: truncating
		# rows can't get under the cap. Bound the blast radius by dropping ALL rows
		# (an honest zero-row PARTIAL) rather than shipping the whole result.
		out = _envelope(data, kind, key, [], n, _NOTE_NONROW)
		return out, {"kind": "uncapped_nonrow", "original_chars": total, "shown": 0, "total": n}

	kept = _fit(rows, row_budget)
	note = _NOTE.format(shown=len(kept), total=n) if kept else _NOTE_NONE
	out = _envelope(data, kind, key, kept, n, note)
	return out, {"kind": "truncated", "original_chars": total, "shown": len(kept), "total": n}
