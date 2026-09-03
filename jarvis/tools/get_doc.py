import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._bulk import _MAX_BATCH

_NO_NAME_MESSAGE = (
	"Pass name (one document) or names (a non-empty list). For a single "
	"record like Stock Settings call get_doc with only the doctype."
)


def get_doc(doctype: str, name: str | None = None, names: list | None = None) -> dict:
	"""Return one document as a dict - or a batch when ``names`` is given.

	Enforces read permission on EACH specific document for the current user.

	Single: returns the document dict.
	Batch: returns ``{"doctype","docs":[<doc>,...],"count":N}`` (fail-fast: a
	missing or unreadable name raises, naming the offending record).

	A Single DocType (e.g. Stock Settings) has exactly one document, whose
	name IS the doctype - there is nothing else to pass. ``name``/``names``
	are ignored for one: a delegate with nothing sensible to put there
	commonly sends "" or [], which used to bounce back "names must be a
	non-empty list of document names" or "unknown Stock Settings: x" instead
	of just reading the one document that exists.
	"""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not frappe.db.exists("DocType", doctype):
		raise InvalidArgumentError(f"unknown doctype: {doctype}")

	if frappe.get_meta(doctype).issingle:
		return _get_single(doctype)

	if names is not None:
		if not isinstance(names, list) or not names:
			raise InvalidArgumentError(_NO_NAME_MESSAGE)
		return _get_doc_batch(doctype, names)

	if not name:
		raise InvalidArgumentError(_NO_NAME_MESSAGE)
	return _get_doc_one(doctype, name)


def _get_doc_one(doctype: str, name: str) -> dict:
	"""Existence + per-record read-permission check, then the doc dict."""
	if not frappe.db.exists(doctype, name):
		raise InvalidArgumentError(f"No {doctype} named '{name}'. Use get_list to find valid names first.")

	if not frappe.has_permission(doctype, ptype="read", doc=name):
		raise PermissionDeniedError(f"no read permission on {doctype} {name}")

	doc = frappe.get_doc(doctype, name)
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
