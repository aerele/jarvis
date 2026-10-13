"""Bound an oversized get_doc result on the agent path without breaking its shape.

Slicing the JSON would hand the model a broken document, so a document keeps
every top-level field: each child table keeps its first whole rows, and a long
text field keeps its start plus a marker. A ``_partial`` block, placed first so
a head slice of the result still shows it, names each cut part with the get_doc
call that reads it. Its words match a cut list page: returned, total, has_more,
next_start.
"""

from jarvis.tools._result_guard import MAX_RESULT_CHARS, _size

# In a cut document a longer text field shows only this much, so the rows get the
# room. With no child table to share with, text gets all the room there is.
TEXT_PREVIEW_CHARS = 2_000
_MARKER = " [TRUNCATED: {more} more chars]"
_MAX_START = 100_000
_GUARD_KEYS = ("_truncated", "_partial")
_IDENTITY_KEYS = ("doctype", "name")

_DOC_NOTE = (
	"PARTIAL document, cut to fit the context window: each table in child_tables "
	"shows only its first rows and each field in text_fields ends in a TRUNCATED "
	"marker. Do not treat it as complete or write a cut value back. Read an omitted "
	"part by calling get_doc with its next_call args; on a child_table call, "
	"fields=[...] and a larger limit read more rows per call. A text field with no "
	"next_call is longer than any one result."
)
_BATCH_NOTE = (
	"PARTIAL batch: only the first {returned} of {total} documents fit the context "
	"window. Read the rest by calling get_doc with docs.next_call args."
)
_BARE_NOTE = (
	"Document too large: even its top-level fields exceed the size budget, so none "
	"are shown. Ask get_doc for the fields you need with fields=[...]."
)


def bound_get_doc_result(data: dict, budget: int) -> tuple[dict, int, int]:
	"""``(bounded result, parts shown, parts total)`` for an over-budget get_doc
	result: rows of one document, or documents of a batch."""
	if isinstance(data.get("docs"), list):
		out, shown, total = _bound_batch(data, budget)
	else:
		out, shown, total = _DocumentCut(data).within(budget)
	if _size(out) > budget:
		return _bare(data), 0, total
	return out, shown, total


class _DocumentCut:
	"""One document dict cut to a char budget: tables to row prefixes, long text
	to a preview, guard metadata first."""

	def __init__(self, doc: dict):
		self.doc = doc
		self.tables = [k for k, v in doc.items() if isinstance(v, list) and v and k not in _GUARD_KEYS]
		self.row_total = sum(len(doc[t]) for t in self.tables)
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
		longest = max((len(v) for k, v in self._texts()), default=0)
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
		cut = dict(self._texts())
		return {
			k: [] if k in self.tables else _cut_text(v, cap) if k in cut else v
			for k, v in self.doc.items()
			if k not in _GUARD_KEYS
		}

	def _texts(self):
		return ((k, v) for k, v in self.doc.items() if isinstance(v, str) and k not in _IDENTITY_KEYS)

	def _partial(self, cap: int, kept: dict, worst: bool = False) -> dict:
		partial = {"note": _DOC_NOTE}
		tables = {
			t: self._table_entry(t, kept[t]) for t in self.tables if worst or kept[t] < len(self.doc[t])
		}
		texts = {k: self._text_entry(k, v, cap) for k, v in self._texts() if _cut_text(v, cap) is not v}
		if tables:
			partial["child_tables"] = tables
		if texts:
			partial["text_fields"] = texts
		return partial

	def _table_entry(self, table: str, returned: int) -> dict:
		next_start = returned if returned <= _MAX_START else None
		entry = {
			"returned": returned,
			"total": len(self.doc[table]),
			"has_more": True,
			"next_start": next_start,
		}
		if self.identity and next_start is not None:
			entry["next_call"] = _call({**self.identity, "child_table": table, "start": next_start})
		return entry

	def _text_entry(self, field: str, value: str, cap: int) -> dict:
		entry = {"returned_chars": cap, "total_chars": len(value)}
		# Offer the fields call only when its result passes the guard uncut.
		if self.identity and _size({**self.identity, field: value}) <= MAX_RESULT_CHARS:
			entry["next_call"] = _call({**self.identity, "fields": [field]})
		return entry


def _bound_batch(data: dict, budget: int) -> tuple[dict, int, int]:
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
	first, _, _ = _DocumentCut(docs[0]).within(room)
	return _batch_envelope(data, [first], 1), 1, total


def _batch_envelope(data: dict, kept: list, returned: int) -> dict:
	total = len(data["docs"])
	coverage = {"returned": returned, "total": total, "has_more": returned < total}
	rest = [d.get("name") for d in data["docs"][returned:] if isinstance(d, dict)]
	if rest and all(isinstance(n, str) for n in rest):
		coverage["next_call"] = _call({"doctype": data.get("doctype"), "names": rest})
	partial = {"note": _BATCH_NOTE.format(returned=returned, total=total), "docs": coverage}
	return _wrap({**data, "docs": kept, "count": len(kept)}, partial)


def _bare(data: dict) -> dict:
	return _wrap({k: data[k] for k in _IDENTITY_KEYS if isinstance(data.get(k), str)}, {"note": _BARE_NOTE})


def _wrap(body: dict, partial: dict) -> dict:
	"""Guard metadata first; a data key of the same name never overrides it."""
	return {
		"_truncated": True,
		"_partial": partial,
		**{k: v for k, v in body.items() if k not in _GUARD_KEYS},
	}


def _cut_text(value: str, cap: int) -> str:
	"""``value`` cut to ``cap`` chars plus a marker, or ``value`` itself when the
	marker would cost more than the cut saves."""
	more = len(value) - cap
	marker = _MARKER.format(more=more)
	return value[:cap] + marker if more > len(marker) else value


def _call(args: dict) -> dict:
	return {"tool": "get_doc", "args": args}
