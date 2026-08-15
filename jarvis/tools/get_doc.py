import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import require_doctype_and_name
from jarvis.tools._bulk import _MAX_BATCH


def get_doc(doctype: str, name: str | None = None, names: list | None = None) -> dict:
	"""Return one document as a dict - or a batch when ``names`` is given.

	Enforces read permission on EACH specific document for the current user.

	Single: returns the document dict.
	Batch: returns ``{"doctype","docs":[<doc>,...],"count":N}`` (fail-fast: a
	missing or unreadable name raises, naming the offending record).
	"""
	if names is not None:
		return _get_doc_batch(doctype, names)

	require_doctype_and_name(doctype, name)
	return _get_doc_one(doctype, name)


def _get_doc_one(doctype: str, name: str) -> dict:
	"""Existence + per-record read-permission check, then the doc dict."""
	if not frappe.db.exists(doctype, name):
		raise InvalidArgumentError(f"unknown {doctype}: {name}")

	if not frappe.has_permission(doctype, ptype="read", doc=name):
		raise PermissionDeniedError(f"no read permission on {doctype} {name}")

	doc = frappe.get_doc(doctype, name)
	doc.apply_fieldlevel_read_permissions()
	return doc.as_dict(no_default_fields=False)


def _get_doc_batch(doctype: str, names: list) -> dict:
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")
	if len(names) > _MAX_BATCH:
		raise InvalidArgumentError(f"too many names in one batch (max {_MAX_BATCH})")

	# Pure read - no savepoint needed (no writes to roll back). Each doc gets
	# its own read-permission check in _get_doc_one; a missing/unreadable name
	# fails the whole call. Capped like the write batches so a huge name list
	# can't flood the turn with full docs - use get_list/query for wider reads.
	docs = [_get_doc_one(doctype, n) for n in names]
	return {"doctype": doctype, "docs": docs, "count": len(docs)}
