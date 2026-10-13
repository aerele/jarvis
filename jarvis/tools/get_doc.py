import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._bulk import _MAX_BATCH
from jarvis.tools.get_list import LIST_PAGE_V1, MAX_LIMIT

_MAX_START = 100_000
_DEFAULT_CHILD_LIMIT = 20

_NO_NAME_MESSAGE = (
	"Pass name (one document) or names (a non-empty list). For a single "
	"record like Stock Settings call get_doc with only the doctype."
)


def get_doc(
	doctype: str,
	name: str | None = None,
	names: list | None = None,
	fields: list | None = None,
	child_table: str | None = None,
	start: int = 0,
	limit: int | None = None,
) -> dict:
	"""Return one document as a dict - or a batch when ``names`` is given.

	Enforces read permission on EACH specific document for the current user.
	For a regular DocType, ``name`` with ``names=[]`` is a single-record read;
	non-empty single and batch arguments together are rejected.

	Single: returns the document dict - or, if the caller passed a non-empty
	``names`` list, the SAME batch envelope as any other doctype
	(``{"doctype","docs":[<the one doc>],"count":1}``), because a caller that
	asked for the batch shape must get the batch shape back, not silently a
	different one it has to special-case. ``name``/an empty or absent
	``names`` are ignored: a Single DocType (e.g. Stock Settings) has exactly
	one document, whose name IS the doctype - there is nothing else to pass,
	and a delegate with nothing sensible to put there commonly sends "" or
	[], which used to bounce back "names must be a non-empty list of document
	names" or "unknown Stock Settings: x" instead of just reading the one
	document that exists.
	Batch (non-single): returns ``{"doctype","docs":[<doc>,...],"count":N}``
	(fail-fast: a missing or unreadable name raises, naming the offending
	record).

	Part of one document (single mode only), read through the same permission
	check and field masking as the whole document, so it never shows more:
	``fields`` returns just those fields plus doctype and name; ``child_table``
	returns one page of that table's rows (``start``, ``limit``) in the
	list-page-v1 rows/coverage shape, with ``fields`` naming the row fields.
	The agent's result-size guard points a cut document at these calls.
	"""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not frappe.db.exists("DocType", doctype):
		raise InvalidArgumentError(f"unknown doctype: {doctype}")
	part = _DocPart(fields, child_table, start, limit)

	if frappe.get_meta(doctype).issingle:
		# names=[...] on a Single is meaningless as a name list (its one
		# document's name IS the doctype - see docstring), but a caller who
		# passed it explicitly asked for the BATCH shape and must get it back,
		# not the flat dict every other Single call returns. names=None/[]
		# (the common "nothing sensible to pass" case) stays flat.
		batch = isinstance(names, list) and bool(names)
		part.refuse_with_batch(batch)
		doc = _get_single(doctype)
		if batch:
			return {"doctype": doctype, "docs": [doc], "count": 1}
		return part.of(doc)

	# Models may populate an unused optional array with [] in single-record mode.
	# Treat only that empty-list shape as absent; malformed batches still fail.
	if name and isinstance(names, list) and not names:
		names = None
	if name and names is not None:
		raise InvalidArgumentError("Pass either name or names, not both. Omit names for one document.")
	if names is not None:
		if not isinstance(names, list) or not names:
			raise InvalidArgumentError(_NO_NAME_MESSAGE)
		part.refuse_with_batch(True)
		return _get_doc_batch(doctype, names)

	if not name:
		raise InvalidArgumentError(_NO_NAME_MESSAGE)
	return part.of(_get_doc_one(doctype, name))


def _get_doc_one(doctype: str, name: str) -> dict:
	"""Existence + per-record read-permission check, then the doc dict."""
	if not frappe.db.exists(doctype, name):
		raise InvalidArgumentError(f"No {doctype} named '{name}'. Use get_list to find valid names first.")

	# Load once, then check READ permission on the loaded object. has_permission
	# (doc=<name string>) would lazy-load the parent row just to run the check -
	# a load this get_doc already does. Passing the pristine object gives an
	# identical verdict and drops the duplicate read. The check must precede
	# apply_fieldlevel_read_permissions() below, which mutates the doc to mask
	# permlevel-restricted fields. The exists() guard above keeps the friendly
	# "No <doctype> named ..." message (get_doc alone would raise DoesNotExist).
	doc = frappe.get_doc(doctype, name)
	if not frappe.has_permission(doctype, ptype="read", doc=doc):
		raise PermissionDeniedError(f"no read permission on {doctype} {name}")

	doc.apply_fieldlevel_read_permissions()
	return doc.as_dict(no_default_fields=False)


def _get_single(doctype: str) -> dict:
	"""Frappe's documented idiom for reading a Single - ``get_single``, not
	``get_doc(doctype, doctype)`` - plus the same read-permission check and
	output shaping ``_get_doc_one`` gives every other document. No existence
	check: a Single always exists (frappe.db.exists special-cases it), and
	singles carry their own DocPerms, so the permission check still matters."""
	if not frappe.has_permission(doctype, ptype="read", doc=doctype):
		raise PermissionDeniedError(f"no read permission on {doctype}")

	doc = frappe.get_single(doctype)
	doc.apply_fieldlevel_read_permissions()
	return doc.as_dict(no_default_fields=False)


def _get_doc_batch(doctype: str, names: list) -> dict:
	if len(names) > _MAX_BATCH:
		raise InvalidArgumentError(f"too many names in one batch (max {_MAX_BATCH})")

	# Pure read - no savepoint needed (no writes to roll back). Each doc gets
	# its own read-permission check in _get_doc_one; a missing/unreadable name
	# fails the whole call. Capped like the write batches so a huge name list
	# can't flood the turn with full docs - use get_list/query for wider reads.
	docs = [_get_doc_one(doctype, n) for n in names]
	return {"doctype": doctype, "docs": docs, "count": len(docs)}


class _DocPart:
	"""The part of one readable document a caller asked for: all of it, some
	fields, or one page of a child table. Applied to the dict ``_get_doc_one`` /
	``_get_single`` already permission-checked and masked."""

	def __init__(self, fields, child_table, start, limit):
		# Models fill unused optional arguments with [] or "": those mean absent.
		fields = None if fields == [] else fields
		child_table = None if child_table == "" else child_table
		if fields is not None and (
			not isinstance(fields, list) or not fields or not all(isinstance(f, str) and f for f in fields)
		):
			raise InvalidArgumentError("fields must be a non-empty list of field names")
		if child_table is not None and (not isinstance(child_table, str) or not child_table):
			raise InvalidArgumentError("child_table must be a child table field name, e.g. 'items'")
		if isinstance(start, bool) or not isinstance(start, int) or not 0 <= start <= _MAX_START:
			raise InvalidArgumentError(f"start must be an integer between 0 and {_MAX_START}")
		if limit is not None and (
			isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT
		):
			raise InvalidArgumentError(f"limit must be between 1 and {MAX_LIMIT}")
		if (start or limit is not None) and not child_table:
			raise InvalidArgumentError("start and limit page a child table; pass child_table too.")
		self.fields = fields
		self.child_table = child_table
		self.start = start
		self.limit = limit or _DEFAULT_CHILD_LIMIT

	def refuse_with_batch(self, batch: bool) -> None:
		if batch and (self.fields or self.child_table):
			raise InvalidArgumentError("fields and child_table read one document; pass name, not names.")

	def of(self, doc: dict) -> dict:
		if self.child_table:
			return self._child_page(doc)
		if self.fields:
			_refuse_unknown(doc["doctype"], self.fields, doc)
			return {"doctype": doc["doctype"], "name": doc["name"], **{f: doc[f] for f in self.fields}}
		return doc

	def _child_page(self, doc: dict) -> dict:
		doctype, table = doc["doctype"], self.child_table
		df = frappe.get_meta(doctype).get_field(table)
		if (
			not df
			or df.fieldtype not in ("Table", "Table MultiSelect")
			or not isinstance(doc.get(table), list)
		):
			tables = ", ".join(k for k, v in doc.items() if isinstance(v, list)) or "none"
			raise InvalidArgumentError(f"{doctype} has no child table '{table}'. Its child tables: {tables}.")
		if self.fields:
			_refuse_unknown(df.options, self.fields, set(frappe.get_meta(df.options).get_valid_fields()))
		rows = doc[table]
		page = rows[self.start : self.start + self.limit]
		if self.fields:
			keep = ["name", "idx", *(f for f in self.fields if f not in ("name", "idx"))]
			page = [{f: row.get(f) for f in keep} for row in page]
		end = self.start + len(page)
		has_more = end < len(rows)
		return {
			"list_contract": LIST_PAGE_V1,
			"doctype": doctype,
			"name": doc["name"],
			"child_table": table,
			"rows": page,
			"coverage": {
				"start": self.start,
				"limit": self.limit,
				"returned": len(page),
				"total": len(rows),
				"has_more": has_more,
				"next_start": end if has_more and end <= _MAX_START else None,
				"complete": self.start == 0 and not has_more,
				"order_by": "idx asc",
				"scope": "this document's rows, as the current user can read them",
			},
			"note": "Rows of one document's child table in row order. Continue with start=next_start while has_more is true.",
		}


def _refuse_unknown(doctype: str, fields: list, known) -> None:
	unknown = [f for f in fields if f not in known]
	if unknown:
		raise InvalidArgumentError(
			f"{doctype} has no field(s) {', '.join(unknown)}; get_schema lists its fields."
		)
