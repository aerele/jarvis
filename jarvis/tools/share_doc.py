"""Grant per-record permissions to a user (or to "Everyone") - one, or a batch.

Wraps ``frappe.share.add``. The DocShare row this creates is what
overrides the DocType-level permissions for a specific record - useful
when the agent is asked to "share this invoice with sales" or "let X
view this customer's history".

Permission: requires "share" permission on each target doc; the explicit
``has_permission`` floor lives in ``_share_doc_one`` so the per-record
check is visible and runs on every record in a batch.

ALWAYS-CONFIRM: granting share permission means the target user can
re-share the record further. The descriptor's agent-facing copy is
explicit about that escalation risk.

Two shapes:

- **Single:** ``share_doc(doctype, name, user=..., read=..., ...)`` ->
  ``{doctype, name, user, everyone, read, write, submit, share}``.
- **Batch:** ``share_doc(doctype, names=[...], user=..., ...)`` ->
  ``{doctype, user, shared:[name,...], count}`` - the same grant on every
  record in ONE atomic savepoint (all-or-nothing).
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import desk_action
from jarvis.tools._bulk import run_atomic_batch


@desk_action(check_user_arg="user")
def share_doc(
	doctype: str,
	name: str | None = None,
	user: str | None = None,
	read: bool = True,
	write: bool = False,
	submit: bool = False,
	share: bool = False,
	everyone: bool = False,
	notify: bool = False,
	names: list | None = None,
) -> dict:
	"""Grant the given permission flags on ``doctype/name`` to ``user``
	(or to every user when ``everyone=True``) - or on every doc in ``names``.

	Single: returns ``{doctype, name, user, everyone, read, write, submit, share}``.
	Batch: returns ``{doctype, user, shared:[name,...], count}``."""
	if not everyone and not user:
		raise InvalidArgumentError(
			"either user or everyone=True is required",
		)

	if names is not None:
		return _share_doc_batch(doctype, names, user, read, write, submit, share, everyone, notify)

	_share_doc_one(doctype, name, user, read, write, submit, share, everyone, notify)
	return {
		"doctype": doctype,
		"name": name,
		"user": user,
		"everyone": bool(everyone),
		"read": bool(read),
		"write": bool(write),
		"submit": bool(submit),
		"share": bool(share),
	}


def _share_doc_one(
	doctype: str,
	name: str,
	user: str | None,
	read: bool,
	write: bool,
	submit: bool,
	share: bool,
	everyone: bool,
	notify: bool,
) -> None:
	"""Share-permission check + grant the DocShare, for ONE record."""
	frappe.has_permission(doctype, "share", doc=name, throw=True)

	from frappe.share import add as _share_add

	_share_add(
		doctype=doctype,
		name=name,
		user=user,
		read=int(bool(read)),
		write=int(bool(write)),
		submit=int(bool(submit)),
		share=int(bool(share)),
		everyone=int(bool(everyone)),
		notify=int(bool(notify)),
	)


def _share_doc_batch(
	doctype: str,
	names: list,
	user: str | None,
	read: bool,
	write: bool,
	submit: bool,
	share: bool,
	everyone: bool,
	notify: bool,
) -> dict:
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> str:
		if not frappe.db.exists(doctype, name):
			raise InvalidArgumentError(f"unknown {doctype}: {name}")
		_share_doc_one(doctype, name, user, read, write, submit, share, everyone, notify)
		return name

	shared = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "user": user, "shared": shared, "count": len(shared)}
