"""Tag a document for categorisation (one, or a whole batch).

Wraps ``frappe.desk.doctype.tag.tag.add_tag``. Tags are free-form
labels that show up in the Desk list filters; the agent can use them
to bucket records (e.g. tag a Sales Order as 'urgent', tag a Customer
as 'follow-up').

Permission: ``add_tag`` requires write permission on the target doc
(implicitly: it modifies the doc's ``_user_tags`` field). The explicit
``has_permission`` floor lives in ``_add_tag_one`` so the per-record
check is visible and runs on every record in a batch.

Two shapes:

- **Single:** ``add_tag(doctype, name, tag, color=...)`` ->
  ``{doctype, name, tag}``.
- **Batch:** ``add_tag(doctype, names=[...], tag=..., color=...)`` ->
  ``{doctype, tag, tagged:[name,...], count}`` - the same tag on every
  record in ONE atomic savepoint (all-or-nothing).
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
def add_tag(
	doctype: str,
	name: str | None = None,
	tag: str | None = None,
	color: str | None = None,
	names: list | None = None,
) -> dict:
	"""Add ``tag`` to ``doctype/name`` - or to every doc in ``names``.
	``color`` is an optional hex code (e.g. ``#ff0000``) for the Tag
	record.

	Single: returns ``{doctype, name, tag}``.
	Batch: returns ``{doctype, tag, tagged:[name,...], count}``."""
	if not tag:
		raise InvalidArgumentError("tag is required")

	if names is not None:
		return _add_tag_batch(doctype, names, tag, color)

	_add_tag_one(doctype, name, tag, color)
	return {"doctype": doctype, "name": name, "tag": tag}


def _add_tag_one(doctype: str, name: str, tag: str, color: str | None) -> None:
	"""Write-permission check + add the tag, for ONE record."""
	if not frappe.has_permission(doctype, "write", doc=name):
		raise PermissionDeniedError(
			f"no write permission on {doctype} {name}",
		)

	from frappe.desk.doctype.tag.tag import add_tag as _at

	_at(tag=tag, dt=doctype, dn=name, color=color)


def _add_tag_batch(doctype: str, names: list, tag: str, color: str | None) -> dict:
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> str:
		if not frappe.db.exists(doctype, name):
			raise InvalidArgumentError(f"unknown {doctype}: {name}")
		_add_tag_one(doctype, name, tag, color)  # per-record write-permission check
		return name

	tagged = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "tag": tag, "tagged": tagged, "count": len(tagged)}
