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
- **A set of records** (``filters`` given, not together with ``name``): ``filters`` has
  the syntax of the get_list tool. The matching records are listed once as the caller
  (so unreadable ones are left out, as get_list leaves them out), up to
  ``_MAX_FILTER_RECORDS``; more than that is refused, never cut short. Only the comments
  on those records are scanned, each still checked against its record as above. The set
  is listed afresh on every call, so keep ``filters`` unchanged while paging with
  ``before``. Nothing matching gives an empty page with a ``note``.

Only ``comment_type = "Comment"`` rows are returned (what the Desk timeline shows as
comments; not likes, assignments or attachment notes). Content is converted from
HTML to plain text so @mentions read as ``@Name``. Newest first; page with
``before`` (the ``next_before`` of the previous call).
"""

from __future__ import annotations

import json
import re
import time
from typing import NamedTuple
from zoneinfo import ZoneInfo

import frappe
from frappe.query_builder import Order
from frappe.utils import cint, get_datetime, get_fullname, get_system_timezone

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
# The most records ``filters`` may match. One bounded IN list on Comment's
# (reference_doctype, reference_name) index; past it the call fails rather than
# silently reading a partial set.
_MAX_FILTER_RECORDS = 1_000
# A DocType-wide scan also stops after this many seconds, well inside the agent's
# tool call timeout, and hands back where to continue.
_TIME_BUDGET_S = 15

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
	filters: dict | list | str | None = None,
) -> dict:
	"""Return ``{doctype, name, comments, count, has_more, next_before, note?}``.

	Each comment is ``{name, reference_name, title?, by, by_email, at, text,
	edited, text_truncated?}``. ``title`` is the record's title when the DocType
	has a title field distinct from its name. ``has_more`` with ``next_before``
	means older matching comments may exist: call again with ``before`` set to
	``next_before`` exactly as given. ``note`` says when a scan stopped before the
	page filled, or that no readable record matched ``filters``. With ``filters``
	``name`` in the result is None; keep ``filters`` unchanged while paging.
	"""
	for arg, value in (("doctype", doctype), ("name", name), ("search", search)):
		if value is not None and not isinstance(value, str):
			raise InvalidArgumentError(f"{arg} must be text")
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	filters = _normalise_filters(filters)
	if name and filters:
		raise InvalidArgumentError(
			"pass name for one record or filters for a set, not both; for several named "
			'records use filters={"name": ["in", [...]]}'
		)
	if not frappe.db.exists("DocType", doctype):
		raise InvalidArgumentError(f"unknown DocType: {doctype}")
	if doctype == "Comment":
		raise InvalidArgumentError(
			"doctype is the record the comments are on (for example Employee), not Comment"
		)
	limit = _clamp_limit(limit)

	if name:
		if not frappe.db.exists(doctype, name):
			raise InvalidArgumentError(f"unknown {doctype}: {name}")
		if not frappe.has_permission(doctype, "read", doc=name):
			raise PermissionDeniedError(f"no read permission on {doctype} {name}")
	elif not frappe.has_permission(doctype, "read"):
		raise PermissionDeniedError(f"no read permission on {doctype}")

	names = _resolve_names(doctype, filters) if filters else None
	if names is not None and not names:
		return _no_matching_record(doctype)

	scan = _Scan(
		doctype=doctype,
		name=name or None,
		search=search or None,
		since=_parse_datetime(since, "since") if since else None,
		cursor=_parse_cursor(before) if before else None,
		names=names,
	)
	result = _collect(scan, limit)
	_attach_titles(doctype, result["comments"])
	return {"doctype": doctype, "name": name, **result, "count": len(result["comments"])}


_FILTERS_ERROR = (
	'filters must be an object such as {"status": "Open"} or a list of [field, operator, value] conditions'
)


def _normalise_filters(filters) -> dict | list | None:
	"""``filters`` as a non-empty dict or list, or None for "no filters" (None, "",
	{}, []). A JSON string is parsed first. Anything else is refused here, so a bad
	type never reaches Frappe as a raw error. The content is Frappe's to judge."""
	if filters is None:
		return None
	if isinstance(filters, str):
		if not filters.strip():
			return None
		try:
			filters = json.loads(filters)
		except (ValueError, RecursionError):  # deep nesting raises RecursionError
			raise InvalidArgumentError(_FILTERS_ERROR) from None
	if not isinstance(filters, dict | list):
		raise InvalidArgumentError(_FILTERS_ERROR)
	return filters or None


def _resolve_names(doctype: str, filters: dict | list) -> tuple[str, ...]:
	"""Names of the ``doctype`` records the caller can list that match ``filters``.

	One ``get_list`` as the caller (roles, user permissions, shares and query conditions
	apply in SQL). DISTINCT, because a child-table filter joins the child and repeats
	the parent. The order_by is table-qualified: Frappe 15 passes it through as written,
	so a bare ``name`` is ambiguous once a child filter joins a second table."""
	names = frappe.get_list(
		doctype,
		filters=filters,
		pluck="name",
		distinct=True,
		order_by=f"`tab{doctype}`.`name` asc",
		limit=_MAX_FILTER_RECORDS + 1,
	)
	if len(names) > _MAX_FILTER_RECORDS:
		raise InvalidArgumentError(
			f"filters match more than {_MAX_FILTER_RECORDS:,} {doctype} records; narrow the "
			"filter, or omit filters and use search / since to scan the DocType"
		)
	return tuple(dict.fromkeys(names))


def _no_matching_record(doctype: str) -> dict:
	"""The normal empty page, with a note: no record, not records without comments."""
	return {
		"doctype": doctype,
		"name": None,
		"comments": [],
		"count": 0,
		"has_more": False,
		"next_before": None,
		"note": f"No {doctype} record you can read matches these filters.",
	}


def _clamp_limit(limit) -> int:
	if limit in (None, ""):
		return _DEFAULT_LIMIT
	value = cint(limit)
	if value < 1:
		raise InvalidArgumentError("limit must be a positive number")
	return min(value, _MAX_LIMIT)


def _parse_datetime(value, label: str):
	"""A since/before value as a naive site-clock datetime (one with its own offset
	is converted, not read as site time)."""
	try:
		moment = get_datetime(value) if isinstance(value, str) else None
	except Exception:
		moment = None
	if moment is not None and moment.tzinfo is not None:
		moment = moment.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
	if moment is None:
		raise InvalidArgumentError(f"{label} must be a date or datetime, got {value!r}")
	return moment


# A page cursor is "<creation>|<comment name>": comments sharing a creation time (an
# import, a script) are then neither skipped nor repeated across pages.
_CURSOR_SEP = "|"
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>|])")


def _parse_cursor(before) -> tuple:
	"""``(creation, name or None)`` from ``before``: a ``next_before`` cursor, or a
	plain date/datetime (everything older than it)."""
	if not isinstance(before, str):
		raise InvalidArgumentError(f"before must be a next_before value or a datetime, got {before!r}")
	at, sep, last = before.rpartition(_CURSOR_SEP) if _CURSOR_SEP in before else (before, "", "")
	return _parse_datetime(at, "before"), (last or None) if sep else None


def _cursor_text(row) -> str:
	return f"{row.creation}{_CURSOR_SEP}{row.name}"


class _Scan(NamedTuple):
	doctype: str
	name: str | None
	search: str | None
	since: object
	cursor: tuple | None
	names: tuple | None = None  # set mode: the resolved records; never empty when set


def _chunk(scan: _Scan, cursor: tuple | None) -> list:
	"""The next ``_SCAN_CHUNK`` matching comments older than ``cursor``, newest first,
	ordered (creation, name) so the cursor is exact even across equal timestamps."""
	c = frappe.qb.DocType("Comment")
	q = (
		frappe.qb.from_(c)
		.select(*[c[f] for f in _FIELDS])
		.where(c.reference_doctype == scan.doctype)
		.where(c.comment_type == "Comment")
	)
	if scan.name:
		q = q.where(c.reference_name == scan.name)
	if scan.names is not None:
		q = q.where(c.reference_name.isin(scan.names))
	if scan.search:
		q = q.where(c.content.like(f"%{_escape_like(scan.search)}%"))
	if scan.since:
		q = q.where(c.creation >= scan.since)
	if cursor:
		at, last = cursor
		q = q.where((c.creation < at) | ((c.creation == at) & (c.name < last)) if last else c.creation < at)
	return (
		q.orderby(c.creation, order=Order.desc)
		.orderby(c.name, order=Order.desc)
		.limit(_SCAN_CHUNK)
		.run(as_dict=True)
	)


def _escape_like(text: str) -> str:
	"""``text`` matched literally inside LIKE: ``%`` and ``_`` are not wildcards."""
	return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _collect(scan: _Scan, limit: int) -> dict:
	"""Walk matching comments newest first; keep the readable ones until the page is
	full, the text budget is spent, or (DocType-wide or set) the scan cap or time
	budget is reached, then hand back where to continue."""
	readable = _Readable(scan.doctype)
	if scan.names is not None:
		readable.seed(scan.names)
	out: list[dict] = []
	spent = scanned = 0
	cursor = scan.cursor
	started = time.monotonic()
	while True:
		rows = _chunk(scan, cursor)
		if not scan.name and scan.names is None:
			readable.prefetch([r.reference_name for r in rows])
		for row in rows:
			if scan.name or readable.can_read(row.reference_name):
				item = _shape(row)
				cost = len(item["text"])
				if out and (len(out) >= limit or spent + cost > _PAGE_TEXT_BUDGET):
					# Stopped before this row: it starts the next page.
					next_before = out[-1]["_cursor"]
					return {"comments": _strip(out), "has_more": True, "next_before": next_before}
				item["_cursor"] = _cursor_text(row)
				out.append(item)
				spent += cost
			cursor = (row.creation, row.name)
			scanned += 1
			if not scan.name and (scanned >= _MAX_SCANNED or time.monotonic() - started > _TIME_BUDGET_S):
				return _stopped(out, cursor, scanned, scan.names is not None)
		if len(rows) < _SCAN_CHUNK:
			return {"comments": _strip(out), "has_more": False, "next_before": None}


def _stopped(out: list, cursor: tuple, scanned: int, for_set: bool) -> dict:
	"""A scan that stopped before the page filled: continue from the last comment
	looked at, and say so (an empty page is not "none")."""
	where = "the matching records" if for_set else "this DocType"
	return {
		"comments": _strip(out),
		"has_more": True,
		"next_before": f"{cursor[0]}{_CURSOR_SEP}{cursor[1]}",
		"note": (
			f"Looked through {scanned} comments on {where}; more may match. Call again "
			"with before=next_before to continue, or narrow with search / since."
		),
	}


def _strip(out: list) -> list:
	for item in out:
		item.pop("_cursor", None)
	return out


class _Readable:
	"""Which records of ``doctype`` the caller can read, for a DocType-wide or set scan.

	One ``get_list`` per chunk of names (role, user-permission, share and query
	conditions applied in SQL) instead of a document load per record; a name it
	does not return is unreadable, a record deleted under its comment included. A
	name it returns still gets the full ``has_permission`` (a DocType's own
	has_permission hook is not in the query), only when one of its comments would
	be shown."""

	def __init__(self, doctype: str):
		self.doctype = doctype
		self.state: dict[str, bool | None] = {}  # None: listed, full check not run yet

	def seed(self, names) -> None:
		"""Mark records already listed as the caller (a set's resolved names): only
		the full ``has_permission`` is left, run when one of their comments is shown."""
		for n in names:
			self.state[n] = None

	def prefetch(self, names: list) -> None:
		unknown = sorted({n for n in names if n and n not in self.state})
		if not unknown:
			return
		listed = set(
			frappe.get_list(
				self.doctype, filters={"name": ["in", unknown]}, pluck="name", limit_page_length=0
			)
		)
		for n in unknown:
			self.state[n] = None if n in listed else False

	def can_read(self, name: str) -> bool:
		if not name:
			return False
		if self.state.get(name) is None and name in self.state:
			try:
				allowed = frappe.has_permission(self.doctype, "read", doc=name)
			except frappe.DoesNotExistError:
				allowed = False
			self.state[name] = bool(allowed)
		return bool(self.state.get(name))


def _shape(row) -> dict:
	text = _to_text(row.content)
	item = {
		"name": row.name,
		"reference_name": row.reference_name,
		"by": row.comment_by or get_fullname(row.owner),
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
	# The markdown converter escapes markdown characters ("a_b" becomes "a\\_b");
	# the agent reads plain text, so the escapes go.
	return _MD_ESCAPE.sub(r"\1", text).strip()


def _attach_titles(doctype: str, comments: list[dict]) -> None:
	"""Add each record's title, when the DocType has a title field other than name.
	Only records already permission-checked above are looked up."""
	if not comments:
		return
	meta = frappe.get_meta(doctype)
	title_field = meta.title_field
	if not title_field or title_field == "name":
		return
	field = meta.get_field(title_field)
	if field and field.permlevel and field.permlevel not in meta.get_permlevel_access("read"):
		return  # the caller cannot read that field on the form either
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
