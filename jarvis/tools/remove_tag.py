"""Remove a tag from a document (one, or a whole batch).

Wraps ``frappe.desk.doctype.tag.tag.remove_tag``. No-op when the tag
isn't on the doc (idempotent).

Permission: requires write permission on each target doc; the explicit
``has_permission`` floor lives in ``_remove_tag_one`` so the per-record
check is visible and runs on every record in a batch.

Two shapes:

- **Single:** ``remove_tag(doctype, name, tag)`` -> ``{doctype, name, tag}``.
- **Batch:** ``remove_tag(doctype, names=[...], tag=...)`` ->
  ``{doctype, tag, untagged:[name,...], count}`` - the same tag removed
  from every record in ONE atomic savepoint (all-or-nothing).
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)
from jarvis.tools import desk_action
from jarvis.tools._bulk import run_atomic_batch


@desk_action()
def remove_tag(
	doctype: str,
	name: str | None = None,
	tag: str | None = None,
	names: list | None = None,
) -> dict:
	"""Remove ``tag`` from ``doctype/name`` - or from every doc in ``names``.

	Single: returns ``{doctype, name, tag}``.
	Batch: returns ``{doctype, tag, untagged:[name,...], count}``."""
	if not tag:
		raise InvalidArgumentError("tag is required")

	if names is not None:
		return _remove_tag_batch(doctype, names, tag)

	_remove_tag_one(doctype, name, tag)
	return {"doctype": doctype, "name": name, "tag": tag}


def _remove_tag_one(doctype: str, name: str, tag: str) -> None:
	"""Write-permission check + remove the tag, for ONE record."""
	if not frappe.has_permission(doctype, "write", doc=name):
		raise PermissionDeniedError(
			f"no write permission on {doctype} {name}",
		)

	from frappe.desk.doctype.tag.tag import remove_tag as _rt

	_rt(tag=tag, dt=doctype, dn=name)


def _remove_tag_batch(doctype: str, names: list, tag: str) -> dict:
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> str:
		if not frappe.db.exists(doctype, name):
			raise InvalidArgumentError(f"unknown {doctype}: {name}")
		_remove_tag_one(doctype, name, tag)  # per-record write-permission check
		return name

	untagged = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "tag": tag, "untagged": untagged, "count": len(untagged)}
