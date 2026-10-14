"""Bound an oversized get_doc result on the agent path without breaking its shape.

Slicing the JSON would hand the model a broken document, so a document keeps
every top-level field: each child table keeps its first whole rows, and a long
value keeps its start plus a marker. A ``_partial`` block, placed first so a
head slice of the result still shows it, names each cut part with the get_doc
call that reads the rest. Its words match a cut list page: returned, total,
has_more, next_start.
"""

import re

import frappe
from frappe.model import table_fields

from jarvis.tools._doc_parts import MAX_START
from jarvis.tools._result_guard import MAX_RESULT_CHARS, _json_text, _size
from jarvis.tools.get_list import MAX_LIMIT

# In a cut document a longer value shows only this much, so the rows get the
# room. With no child table to share with, values get all the room there is.
TEXT_PREVIEW_CHARS = 2_000
_MARKER = " [TRUNCATED: {more} more chars]"
# The marker as it appears inside a cut value. The write tools refuse a value
# holding it: it is a cut read, not the real data.
CUT_MARKER = re.compile(r" \[TRUNCATED: \d+ more chars\]")
# Room a child_table page needs besides its rows (coverage, note, identity).
_PAGE_OVERHEAD = 1_000
_BARE_FIELD_NAMES = 300
_GUARD_KEYS = ("_truncated", "_partial")
_IDENTITY_KEYS = ("doctype", "name")

_DOC_NOTE = (
	"PARTIAL document, cut to fit the context window: each table in child_tables "
	"shows only its first rows, and each field in text_fields shows only its first "
	"returned_chars chars, then a TRUNCATED marker (as_json: the value's JSON text). "
	"Do not treat it as complete or write a cut value back. Read an omitted part by "
	"calling get_doc with its next_call args. A child_table page continues at "
	"next_start; fields=[...] and a larger limit read more rows per call. A text "
	"window continues at next_offset: append it, then follow the window's own "
	"next_call until it has none."
)
_BATCH_NOTE = (
	"PARTIAL batch: only the first {returned} of {total} documents fit the context "
	"window. Read the rest by calling get_doc with docs.next_call args."
)
_BARE_NOTE = (
	"Document too large: even its top-level fields exceed the size budget, so none "
	"are shown. fields names them (field_count in all); ask get_doc for the ones you "
	"need with fields=[...]."
)


def bound_get_doc_result(data: dict, budget: int) -> tuple[dict, int, int]:
	"""``(bounded result, parts shown, parts total)`` for an over-budget get_doc
	result: rows of one document, or documents of a batch."""
	tables = _table_fieldnames(data.get("doctype"))
	if _is_batch(data):
		out, shown, total = _bound_batch(data, tables, budget)
	else:
		out, shown, total = _DocumentCut(data, tables).within(budget)
	if _size(out) > budget:
		return _bare(data), 0, total
	return out, shown, total


class _DocumentCut:
	"""One document dict cut to a char budget: child tables to row prefixes, other
	long values to a preview, guard metadata first."""

	def __init__(self, doc: dict, table_names: frozenset):
		self.doc = doc
		self.tables = [k for k, v in doc.items() if k in table_names and isinstance(v, list) and v]
		self.row_total = sum(len(doc[t]) for t in self.tables)
		# Every other text, list or dict value is cut as text: a list or dict as its JSON.
		skip = {*self.tables, *_IDENTITY_KEYS, *_GUARD_KEYS}
		self.texts = {
			k: v if isinstance(v, str) else _json_text(v)
			for k, v in doc.items()
			if isinstance(v, str | list | dict) and k not in skip
		}
		identity = {k: doc.get(k) for k in _IDENTITY_KEYS}
		self.identity = identity if all(isinstance(v, str) and v for v in identity.values()) else None

	def within(self, budget: int) -> tuple[dict, int, int]:
		cap = self._text_cap(budget)
		if cap is None:
			return _bare(self.doc), 0, self.row_total
		skeleton = self._skeleton(cap)
		# Reserve room for the worst-case table metadata, then fill rows exactly.
		worst = _wrap(skeleton, self._partial(cap, {t: len(self.doc[t]) for t in self.tables}, worst=True))
		kept = self._fill(budget - _size(worst))
		for table, count in kept.items():
			skeleton[table] = self.doc[table][:count]
		return _wrap(skeleton, self._partial(cap, kept)), sum(kept.values()), self.row_total

	def _text_cap(self, budget: int) -> int | None:
		"""The largest text length, up to the preview when there are rows to share
		with, at which the document without rows fits. None if nothing fits."""
		longest = max((len(t) for t in self.texts.values()), default=0)
		high = min(TEXT_PREVIEW_CHARS, longest) if self.tables else longest
		if self._fits(high, budget):
			return high
		low = 0
		if not self._fits(low, budget):
			return None
		while low < high - 1:
			mid = (low + high) // 2
			if self._fits(mid, budget):
				low = mid
			else:
				high = mid
		return low

	def _fits(self, cap: int, budget: int) -> bool:
		full = {t: len(self.doc[t]) for t in self.tables}
		return _size(_wrap(self._skeleton(cap), self._partial(cap, full, worst=True))) <= budget

	def _fill(self, room: int) -> dict:
		"""Rows per table, round robin, so no table starves the others. A table
		stops at its first row that does not fit; rows stay a prefix. Costs are
		exact: compact JSON adds a row's size, plus a comma after the first."""
		kept = dict.fromkeys(self.tables, 0)
		open_tables = list(self.tables)
		while open_tables:
			for table in list(open_tables):
				rows, i = self.doc[table], kept[table]
				cost = _size(rows[i]) + (1 if i else 0) if i < len(rows) else None
				if cost is None or cost > room:
					open_tables.remove(table)
					continue
				room -= cost
				kept[table] = i + 1
		return kept

	def _skeleton(self, cap: int) -> dict:
		skeleton = {}
		for k, v in self.doc.items():
			if k in _GUARD_KEYS:
				continue
			cut = _cut_text(self.texts[k], cap) if k in self.texts else None
			skeleton[k] = [] if k in self.tables else cut if cut is not None else v
		return skeleton

	def _partial(self, cap: int, kept: dict, worst: bool = False) -> dict:
		partial = {"note": _DOC_NOTE}
		tables = {
			t: self._table_entry(t, kept[t], worst)
			for t in self.tables
			if worst or kept[t] < len(self.doc[t])
		}
		texts = {k: self._text_entry(k, text, cap) for k, text in self.texts.items() if _cut_text(text, cap)}
		if tables:
			partial["child_tables"] = tables
		if texts:
			partial["text_fields"] = texts
		return partial

	def _table_entry(self, table: str, returned: int, worst: bool = False) -> dict:
		next_start = returned if returned <= MAX_START else None
		entry = {
			"returned": returned,
			"total": len(self.doc[table]),
			"has_more": True,
			"next_start": next_start,
		}
		if self.identity and next_start is not None:
			# The worst case reserves the widest limit; the real one is what fits.
			limit = MAX_LIMIT if worst else self._page_limit(table, next_start)
			entry["next_call"] = _call(
				{**self.identity, "child_table": table, "start": next_start, "limit": limit}
			)
		return entry

	def _page_limit(self, table: str, start: int) -> int:
		"""Rows from ``start`` that one child_table page carries uncut (at least 1:
		a row too big alone comes back as a page cut to zero rows, which says so)."""
		room, count = MAX_RESULT_CHARS - _PAGE_OVERHEAD, 0
		for row in self.doc[table][start : start + MAX_LIMIT]:
			room -= _size(row) + (1 if count else 0)
			if room < 0:
				break
			count += 1
		return max(count, 1)

	def _text_entry(self, field: str, text: str, cap: int) -> dict:
		entry = {"returned_chars": cap, "total_chars": len(text), "next_offset": cap}
		if not isinstance(self.doc[field], str):
			entry["as_json"] = True
		if self.identity:
			entry["next_call"] = _call({**self.identity, "fields": [field], "text_offset": cap})
		return entry


def _is_batch(data: dict) -> bool:
	"""get_doc's batch envelope, exactly: doctype, docs and count. A document always
	has a name, so one with a field or child table called docs is never a batch."""
	docs = data.get("docs")
	return (
		set(data) == {"doctype", "docs", "count"}
		and isinstance(docs, list)
		and bool(docs)
		and data["count"] == len(docs)
		and all(isinstance(d, dict) for d in docs)
	)


def _bound_batch(data: dict, tables: frozenset, budget: int) -> tuple[dict, int, int]:
	"""Keep the longest run of whole documents that fits; when not even the first
	does, cut that one to the room left. The rest are named for a next call."""
	docs = data["docs"]
	total = len(docs)
	shown = 0
	while shown < total and _size(_batch_envelope(data, docs[: shown + 1], shown + 1)) <= budget:
		shown += 1
	if shown:
		return _batch_envelope(data, docs[:shown], shown), shown, total
	room = budget - _size(_batch_envelope(data, [], 1))
	first, _, _ = _DocumentCut(docs[0], tables).within(room)
	return _batch_envelope(data, [first], 1), 1, total


def _batch_envelope(data: dict, kept: list, returned: int) -> dict:
	total = len(data["docs"])
	coverage = {"returned": returned, "total": total, "has_more": returned < total}
	rest = [d.get("name") for d in data["docs"][returned:] if isinstance(d, dict)]
	if rest and all(isinstance(n, str) for n in rest):
		coverage["next_call"] = _call({"doctype": data.get("doctype"), "names": rest})
	partial = {"note": _BATCH_NOTE.format(returned=returned, total=total), "docs": coverage}
	return _wrap({**data, "docs": kept, "count": len(kept)}, partial)


def _table_fieldnames(doctype) -> frozenset:
	"""The doctype's child table fields. Any other list is a value, cut as text."""
	if not isinstance(doctype, str) or not frappe.db.exists("DocType", doctype):
		return frozenset()
	return frozenset(df.fieldname for df in frappe.get_meta(doctype).fields if df.fieldtype in table_fields)


def _bare(data: dict) -> dict:
	"""Identity only, plus the field names, so the model can ask for some of them."""
	names = [k for k in data if k not in _GUARD_KEYS]
	partial = {"note": _BARE_NOTE, "fields": names[:_BARE_FIELD_NAMES], "field_count": len(names)}
	return _wrap({k: data[k] for k in _IDENTITY_KEYS if isinstance(data.get(k), str)}, partial)


def _wrap(body: dict, partial: dict) -> dict:
	"""Guard metadata first; a data key of the same name never overrides it."""
	return {
		"_truncated": True,
		"_partial": partial,
		**{k: v for k, v in body.items() if k not in _GUARD_KEYS},
	}


def _cut_text(text: str, cap: int) -> str | None:
	"""``text`` cut to ``cap`` chars plus a marker, or None when the marker would
	cost more than the cut saves (the value is then kept as it is)."""
	more = len(text) - cap
	marker = _MARKER.format(more=more)
	return text[:cap] + marker if more > len(marker) else None


def _call(args: dict) -> dict:
	return {"tool": "get_doc", "args": args}
