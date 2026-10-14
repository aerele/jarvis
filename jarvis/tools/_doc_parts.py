"""The part of one readable document a get_doc call asks for: some fields, a
window of one field's text, or one page of a child table (#715 F11).

A part is cut from the dict get_doc already permission-checked and masked, so it
never shows more than the whole. Where the whole shows a field the user cannot
read as an empty value, a part says so instead: a top-level field or a child
table is refused by name, and a child field is left out of the rows and listed.
"""

import frappe
from frappe.model import table_fields

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._result_guard import _json_text, _size
from jarvis.tools.get_list import LIST_PAGE_V1, MAX_LIMIT

MAX_START = 100_000
DEFAULT_CHILD_LIMIT = 20
# Most JSON chars one text window returns: well under the agent's 35,000-char
# result budget, so a window always reaches the model whole.
TEXT_WINDOW_CHARS = 30_000


class DocPart:
	"""Which part of a document to return, checked before the document is read."""

	def __init__(
		self, fields=None, child_table=None, start=None, limit=None, text_offset=None, text_length=None
	):
		# Models fill unused optional arguments with null, [], "" or 0 and send whole
		# numbers as 20.0 or "20": a filler means absent, a number is read as one.
		self.fields = fields or None
		self.child_table = child_table or None
		self.start = _whole(start, 0, MAX_START, f"start must be an integer between 0 and {MAX_START}") or 0
		self.limit = _whole(limit, 0, MAX_LIMIT, f"limit must be between 1 and {MAX_LIMIT}") or None
		self.text_offset = _whole(text_offset, 0, None, "text_offset must be an integer of 0 or more")
		self.text_length = (
			_whole(
				text_length, 0, TEXT_WINDOW_CHARS, f"text_length must be between 1 and {TEXT_WINDOW_CHARS}"
			)
			or None
		)
		self._check_names()
		self._check_paging()
		self._check_window()

	def refuse_with_batch(self, batch: bool) -> None:
		if batch and (self.fields or self.child_table):
			raise InvalidArgumentError("fields and child_table read one document; pass name, not names.")

	def of(self, doc: dict) -> dict:
		if self.child_table:
			return self._child_page(doc)
		if not self.fields:
			return doc
		_refuse_unreadable(doc["doctype"], self.fields)
		_refuse_unknown(doc["doctype"], self.fields, doc)
		if self.windowed:
			return self._text_window(doc)
		return {"doctype": doc["doctype"], "name": doc["name"], **{f: doc[f] for f in self.fields}}

	@property
	def windowed(self) -> bool:
		return self.text_offset is not None or self.text_length is not None

	def _check_names(self) -> None:
		if self.fields is not None and (
			not isinstance(self.fields, list) or not all(isinstance(f, str) and f for f in self.fields)
		):
			raise InvalidArgumentError("fields must be a non-empty list of field names")
		if self.child_table is not None and not isinstance(self.child_table, str):
			raise InvalidArgumentError("child_table must be a child table field name, e.g. 'items'")

	def _check_paging(self) -> None:
		if (self.start or self.limit) and not self.child_table:
			raise InvalidArgumentError("start and limit page a child table; pass child_table too.")

	def _check_window(self) -> None:
		one_field = bool(self.fields) and len(self.fields) == 1 and not self.child_table
		if self.text_offset == 0 and self.text_length is None and not one_field:
			self.text_offset = None  # a filler zero, not a window
		if self.windowed and not one_field:
			raise InvalidArgumentError(
				"text_offset and text_length read one field: pass fields=[that field] and no child_table."
			)

	def _text_window(self, doc: dict) -> dict:
		field = self.fields[0]
		value = doc[field]
		text = value if isinstance(value, str) else "" if value is None else _json_text(value)
		offset = self.text_offset or 0
		if offset > len(text):
			raise InvalidArgumentError(
				f"text_offset {offset} is past the end of {field} ({len(text)} chars)."
			)
		piece = _text_slice(text, offset, self.text_length or TEXT_WINDOW_CHARS)
		end = offset + len(piece)
		window = {
			"field": field,
			"offset": offset,
			"returned_chars": len(piece),
			"total_chars": len(text),
			"next_offset": end if end < len(text) else None,
		}
		if value is not None and not isinstance(value, str):
			window["as_json"] = True
		if end < len(text):
			args = {"doctype": doc["doctype"], "name": doc["name"], "fields": [field], "text_offset": end}
			if self.text_length:
				args["text_length"] = self.text_length
			window["next_call"] = {"tool": "get_doc", "args": args}
		return {"doctype": doc["doctype"], "name": doc["name"], "_text_window": window, field: piece}

	def _child_page(self, doc: dict) -> dict:
		doctype, table = doc["doctype"], self.child_table
		df = frappe.get_meta(doctype).get_field(table)
		if not df or df.fieldtype not in table_fields or not isinstance(doc.get(table), list):
			tables = ", ".join(k for k, v in doc.items() if isinstance(v, list)) or "none"
			raise InvalidArgumentError(f"{doctype} has no child table '{table}'. Its child tables: {tables}.")
		_refuse_unreadable(doctype, [table])
		if self.fields:
			# get_valid_columns: Frappe v15 has no get_valid_fields (same list on v16).
			_refuse_unknown(df.options, self.fields, set(frappe.get_meta(df.options).get_valid_columns()))
		hidden = _unreadable(doctype, df.options)
		omitted = sorted(hidden.intersection(self.fields) if self.fields else hidden)
		rows = doc[table]
		limit = self.limit or DEFAULT_CHILD_LIMIT
		page = [_row_part(row, self.fields, hidden) for row in rows[self.start : self.start + limit]]
		end = self.start + len(page)
		has_more = end < len(rows)
		out = {
			"list_contract": LIST_PAGE_V1,
			"doctype": doctype,
			"name": doc["name"],
			"child_table": table,
			"rows": page,
			"coverage": {
				"start": self.start,
				"limit": limit,
				"returned": len(page),
				"total": len(rows),
				"has_more": has_more,
				"next_start": end if has_more and end <= MAX_START else None,
				"complete": self.start == 0 and not has_more,
				"order_by": "idx asc",
				"scope": "this document's rows, as the current user can read them",
			},
			"note": "Rows of one document's child table in row order. Continue with start=next_start while has_more is true.",
		}
		if omitted:
			out["omitted_fields"] = omitted
			out["note"] += " Row fields you cannot read are left out of every row; omitted_fields names them."
		return out


def _row_part(row: dict, fields: list | None, hidden: set) -> dict:
	"""``row`` as asked: the named fields plus its name and idx, or all of it,
	without the fields the user cannot read."""
	if fields:
		keep = ["name", "idx", *(f for f in fields if f not in ("name", "idx"))]
		return {f: row.get(f) for f in keep if f not in hidden}
	return {k: v for k, v in row.items() if k not in hidden} if hidden else row


def _unreadable(doctype: str, of: str | None = None) -> set:
	"""Fields of ``of`` (default ``doctype``) that get_doc masks for this user:
	those at a permlevel the user's roles cannot read on ``doctype``, the rule
	Document.apply_fieldlevel_read_permissions applies."""
	if frappe.session.user == "Administrator":
		return set()
	access = set(frappe.get_meta(doctype).get_permlevel_access("read"))
	return {
		df.fieldname
		for df in frappe.get_meta(of or doctype).fields
		if df.permlevel and df.permlevel not in access
	}


def _refuse_unreadable(doctype: str, fields: list) -> None:
	unreadable = _unreadable(doctype)
	hidden = [f for f in fields if f in unreadable]
	if hidden:
		raise PermissionDeniedError(
			f"no read permission on {doctype} field(s) {', '.join(hidden)}; ask again without them."
		)


def _refuse_unknown(doctype: str, fields: list, known) -> None:
	unknown = [f for f in fields if f not in known]
	if unknown:
		raise InvalidArgumentError(
			f"{doctype} has no field(s) {', '.join(unknown)}; get_schema lists its fields."
		)


def _text_slice(text: str, offset: int, length: int) -> str:
	"""``text[offset:offset + length]``, shortened until its JSON fits the window:
	quotes, backslashes and control characters take more than one JSON char."""
	piece = text[offset : offset + length]
	if _size(piece) <= TEXT_WINDOW_CHARS:
		return piece
	low, high = 1, len(piece)  # one char always fits
	while low < high:
		mid = (low + high + 1) // 2
		if _size(piece[:mid]) <= TEXT_WINDOW_CHARS:
			low = mid
		else:
			high = mid - 1
	return piece[:low]


def _whole(value, low: int, high: int | None, message: str) -> int | None:
	"""``value`` as an int, or None when absent (null or ""). A whole number sent
	as 20.0 or "20" counts; anything else, or out of range, is refused."""
	if value is None or value == "":
		return None
	number = value
	if isinstance(value, str):
		try:
			number = float(value)
		except ValueError:
			raise InvalidArgumentError(message) from None
	if isinstance(number, bool) or not isinstance(number, int | float):
		raise InvalidArgumentError(message)
	if isinstance(number, float) and not number.is_integer():
		raise InvalidArgumentError(message)
	number = int(number)
	if number < low or (high is not None and number > high):
		raise InvalidArgumentError(message)
	return number
