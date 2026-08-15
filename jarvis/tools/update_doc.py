"""Update a Frappe document - one, or a whole batch - with permission-aware writes.

This is the first **mutating** tool in Jarvis. Every guarantee that the
read tools rely on (per-user permissions, no leakage, etc.) carries over
to writes - Frappe's permission engine checks ``write`` on the target
DocType + record, not just ``read``.

Two shapes:

- **Single:** ``update_doc(doctype, name, changes)`` -> the saved doc as a dict.
- **Batch:** ``update_doc(doctype, updates=[{"name","changes"}, ...])`` -> a
  lean ``{"doctype","updated":[name,...],"count":N}``, every save in ONE atomic
  savepoint (a single confirmation card). One doctype, per-doc name + changes.

Safety bounds enforced here (on top of Frappe's standard validation):

- Calling user must have ``write`` permission on the target record
  (``frappe.has_permission(doctype, ptype="write", doc=name)``)
- System fields (``name``, ``owner``, ``creation``, ``modified``,
  ``modified_by``, ``doctype``, ``docstatus``, ``idx``, ``parent``,
  ``parentfield``, ``parenttype``) are refused - they're maintained by
  Frappe, not user-editable, and an LLM shouldn't be poking at them
- ``docstatus`` changes are refused - submit/cancel/amend go through
  dedicated tools, not raw field writes
- Empty ``changes`` dict is refused so the agent doesn't accidentally
  call this with no-op intent
"""

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import require_doctype_and_name
from jarvis.tools._bulk import run_atomic_batch
from jarvis.tools._delegate_write_caps import enforce_update

# Fields Frappe maintains itself or that govern DocType identity. An LLM
# rewriting these would corrupt the row.
PROTECTED_FIELDS = frozenset(
	{
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"doctype",
		"docstatus",
		"idx",
		"parent",
		"parentfield",
		"parenttype",
	}
)


def _update_one(doctype: str, name: str, changes: dict) -> "frappe.model.document.Document":
	"""Guards (non-empty changes, no protected fields, write perm) + apply +
	save for ONE doc. Returns the saved Document. Shared by the single and
	batch paths so they never drift."""
	if not isinstance(changes, dict) or not changes:
		raise InvalidArgumentError("changes must be a non-empty dict")

	protected = sorted(set(changes.keys()) & PROTECTED_FIELDS)
	if protected:
		raise InvalidArgumentError(f"refusing to write protected field(s): {', '.join(protected)}")

	# R5-J9: bind a DELEGATE agent's update to its declared writes[] contract BEFORE
	# the Frappe permission check below (a no-op for non-delegate callers). An auditor
	# is refused outright; an operator may update only a declared doctype, only a draft
	# (docstatus==0), and only a row owned by its run-as identity.
	enforce_update(doctype, name)

	if not frappe.has_permission(doctype, ptype="write", doc=name):
		raise PermissionDeniedError(f"no write permission on {doctype} '{name}'")

	doc = frappe.get_doc(doctype, name)  # raises DoesNotExistError if missing
	for field, value in changes.items():
		doc.set(field, value)
	doc.save()  # runs DocType validate() and on_update hooks
	return doc


def update_doc(
	doctype: str,
	name: str | None = None,
	changes: dict | None = None,
	updates: list | None = None,
) -> dict:
	"""Apply changes to one document - or a whole batch when ``updates`` is given.

	Single: returns the saved document as a dict (matching ``get_doc``'s shape).
	Batch: returns ``{"doctype","updated":[name,...],"count":N}``.

	Raises:
	  - InvalidArgumentError on empty args, empty changes, attempts to write
	    protected fields (per item, for a batch)
	  - PermissionDeniedError when the calling user lacks write on a record
	  - frappe.DoesNotExistError when a record doesn't exist
	  - frappe.ValidationError when a DocType's own validate() rejects
	"""
	if updates is not None:
		return _update_batch(doctype, updates)

	require_doctype_and_name(doctype, name)
	doc = _update_one(doctype, name, changes)
	doc.apply_fieldlevel_read_permissions()
	return doc.as_dict()


def _update_batch(doctype: str, updates: list) -> dict:
	"""Save every ``{"name","changes"}`` in ``updates`` atomically (one doctype,
	per-doc changes). All saves run in one savepoint: if any fails, the whole
	batch rolls back and nothing is committed."""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(updates, list) or not updates:
		raise InvalidArgumentError("updates must be a non-empty list of {name, changes}")
	for i, item in enumerate(updates):
		if not isinstance(item, dict) or not item.get("name"):
			raise InvalidArgumentError(f"updates[{i}] must be a dict with a name + changes")

	def _do(item: dict) -> str:
		_update_one(doctype, item["name"], item.get("changes"))
		return item["name"]

	updated = run_atomic_batch(updates, _do, label=lambda u: u.get("name"))
	return {"doctype": doctype, "updated": updated, "count": len(updated)}
