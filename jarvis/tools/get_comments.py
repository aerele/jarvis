"""Read the timeline comments on a record, or find comments across a DocType.

Frappe's ``Comment`` DocType is readable only by System Manager / Website Manager,
yet every Desk user sees the comments on a record they can open: the form loader
(``frappe.desk.form.load.get_comments``) checks read permission on the PARENT
record and then reads that record's comments with ``frappe.get_all``. This tool
applies exactly that rule, so the agent can read what the user can already see in
Desk, and nothing more:

- **One record** (``name`` given): read permission on ``doctype/name``, then that
  record's comments.
- **Across a DocType** (``name`` omitted): read permission on the DocType, then each
  matching comment is kept only when the caller can read the record it is on
  (checked per record, cached per call). ``search`` / ``since`` narrow the scan.

Only ``comment_type = "Comment"`` rows are returned (what the Desk timeline shows as
comments; not likes, assignments or attachment notes). Content is converted from
HTML to plain text so @mentions read as ``@Name``. Newest first; page with
``before`` (the ``next_before`` of the previous call).
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, get_datetime

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError

# Page size the agent gets when it does not ask, and the most it may ask for.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50
# One comment's text is clipped past this many characters (flagged ``text_truncated``).
_MAX_TEXT_CHARS = 2_000
# Total comment text one call returns. Kept under the agent's result budget
# (_result_guard.MAX_RESULT_CHARS) with room for the envelope, so a page is never
# cut by the guard; reaching it ends the page early with ``has_more``.
_PAGE_TEXT_BUDGET = 24_000
# A DocType-wide call reads comments in chunks of this size and stops after
# _MAX_SCANNED of them, so a caller who can read few records of a large DocType
# gets a bounded call and a cursor to continue from, never an unbounded scan.
_SCAN_CHUNK = 200
_MAX_SCANNED = 2_000

# Shown by get_list / query when the agent tries to read the Comment DocType itself.
COMMENT_REDIRECT_HINT = (
	"Comments on a record are read with get_comments(doctype, name), which checks "
	"your access to that record the way Desk does; omit name to search the comments "
	"across a DocType."
)

_FIELDS = [
	"name",
	"reference_name",
	"content",
	"comment_by",
	"comment_email",
	"owner",
	"creation",
	"modified",
]


def get_comments(
	doctype: str,
	name: str | None = None,
	search: str | None = None,
	since: str | None = None,
	before: str | None = None,
	limit: int | None = None,
) -> dict:
	"""Return ``{doctype, name, comments, count, has_more, next_before}``.

	Each comment is ``{name, reference_name, title?, by, by_email, at, text,
	edited, text_truncated?}``. ``title`` is the record's title when the DocType
	has a title field distinct from its name. ``has_more`` with ``next_before``
	means older matching comments exist: call again with ``before=next_before``.
	"""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not frappe.db.exists("DocType", doctype):
		raise InvalidArgumentError(f"unknown DocType: {doctype}")
	if doctype == "Comment":
		raise InvalidArgumentError(
			"doctype is the record the comments are on (for example Employee), not Comment"
		)
	limit = _clamp_limit(limit)

	filters: dict = {"reference_doctype": doctype, "comment_type": "Comment"}
	if name:
		if not frappe.db.exists(doctype, name):
			raise InvalidArgumentError(f"unknown {doctype}: {name}")
		if not frappe.has_permission(doctype, "read", doc=name):
			raise PermissionDeniedError(f"no read permission on {doctype} {name}")
		filters["reference_name"] = name
	elif not frappe.has_permission(doctype, "read"):
		raise PermissionDeniedError(f"no read permission on {doctype}")

	if search:
		filters["content"] = ["like", f"%{search}%"]
	creation_filters = []
	if since:
		creation_filters.append([">=", _parse_datetime(since, "since")])
	if before:
		creation_filters.append(["<", _parse_datetime(before, "before")])

	comments, has_more, next_before = _collect(doctype, filters, creation_filters, limit, single=bool(name))
	_attach_titles(doctype, comments)
	return {
		"doctype": doctype,
		"name": name,
		"comments": comments,
		"count": len(comments),
		"has_more": has_more,
		"next_before": next_before,
	}


def _clamp_limit(limit) -> int:
	if limit in (None, ""):
		return _DEFAULT_LIMIT
	value = cint(limit)
	if value < 1:
		raise InvalidArgumentError("limit must be a positive number")
	return min(value, _MAX_LIMIT)


def _parse_datetime(value, label: str):
	try:
		return get_datetime(value)
	except Exception:
		raise InvalidArgumentError(f"{label} must be a date or datetime, got {value!r}") from None


def _collect(doctype: str, filters: dict, creation_filters: list, limit: int, single: bool):
	"""Walk matching comments newest first; keep the readable ones until the page is
	full, the text budget is spent, or (DocType-wide) the scan cap is reached."""
	readable: dict[str, bool] = {}
	out: list[dict] = []
	spent = 0
	scanned = 0
	cursor = None  # creation of the last comment looked at
	while True:
		chunk_filters = [[k, *v] if isinstance(v, list) else [k, "=", v] for k, v in filters.items()]
		chunk_filters += [["creation", *cf] for cf in creation_filters]
		if cursor is not None:
			chunk_filters.append(["creation", "<", cursor])
		rows = frappe.get_all(
			"Comment",
			filters=chunk_filters,
			fields=_FIELDS,
			order_by="creation desc",
			limit_page_length=_SCAN_CHUNK,
		)
		for row in rows:
			scanned += 1
			if single or _can_read(doctype, row.reference_name, readable):
				item = _shape(row)
				cost = len(item["text"])
				if out and (len(out) >= limit or spent + cost > _PAGE_TEXT_BUDGET):
					# Stopped before this row: it starts the next page.
					return out, True, str(out[-1]["at"])
				out.append(item)
				spent += cost
			cursor = row.creation
			if not single and scanned >= _MAX_SCANNED:
				break
		else:
			if len(rows) < _SCAN_CHUNK:
				return out, False, None
			continue
		# Scan cap reached on a DocType-wide search: hand back a cursor so the
		# caller can continue past the comments already looked at.
		return out, True, str(cursor)


def _can_read(doctype: str, reference_name: str, cache: dict) -> bool:
	if reference_name not in cache:
		cache[reference_name] = bool(
			reference_name and frappe.has_permission(doctype, "read", doc=reference_name)
		)
	return cache[reference_name]


def _shape(row) -> dict:
	text = _to_text(row.content)
	item = {
		"name": row.name,
		"reference_name": row.reference_name,
		"by": row.comment_by or _full_name(row.owner),
		"by_email": row.comment_email or row.owner,
		"at": row.creation,
		"text": text,
		"edited": bool(row.modified and row.creation and row.modified > row.creation),
	}
	if len(text) > _MAX_TEXT_CHARS:
		item["text"] = text[:_MAX_TEXT_CHARS].rstrip() + " ..."
		item["text_truncated"] = True
	return item


def _to_text(html: str | None) -> str:
	"""Comment content is HTML (Desk's editor); return readable plain text."""
	if not html:
		return ""
	from frappe.utils import strip_html_tags, to_markdown

	try:
		text = to_markdown(html)
	except Exception:
		text = None
	if text is None:
		text = strip_html_tags(html)
	return text.strip()


def _full_name(user: str | None) -> str:
	if not user:
		return ""
	return frappe.db.get_value("User", user, "full_name") or user


def _attach_titles(doctype: str, comments: list[dict]) -> None:
	"""Add each record's title, when the DocType has a title field other than name.
	Only records already permission-checked above are looked up."""
	if not comments:
		return
	title_field = frappe.get_meta(doctype).title_field
	if not title_field or title_field == "name":
		return
	names = sorted({c["reference_name"] for c in comments if c["reference_name"]})
	titles = dict(
		frappe.get_all(
			doctype,
			filters={"name": ["in", names]},
			fields=["name", title_field],
			as_list=True,
		)
	)
	for c in comments:
		title = titles.get(c["reference_name"])
		if title and title != c["reference_name"]:
			c["title"] = title
