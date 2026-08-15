"""Unsubscribe a user from a document's change events (one, or a whole batch).

Wraps ``frappe.desk.form.document_follow.unfollow_document``. Idempotent
(no-op when the user isn't currently following).

Permission: requires read permission on each target doc; the explicit
``has_permission`` floor lives in ``_unfollow_document_one`` so the
per-record check is visible and runs on every record in a batch.

Two shapes:

- **Single:** ``unfollow_document(doctype, name, user=...)`` ->
  ``{doctype, name, user, unfollowed}``.
- **Batch:** ``unfollow_document(doctype, names=[...], user=...)`` ->
  ``{doctype, user, unfollowed:[name,...], count}`` - the same user
  unsubscribed from every record in ONE atomic savepoint (all-or-nothing).
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import desk_action
from jarvis.tools._bulk import run_atomic_batch


@desk_action(check_user_arg="user")
def unfollow_document(
	doctype: str,
	name: str | None = None,
	user: str | None = None,
	names: list | None = None,
) -> dict:
	"""Unsubscribe ``user`` (or the session user) from ``doctype/name`` -
	or from every doc in ``names``.

	Single: returns ``{doctype, name, user, unfollowed}``.
	Batch: returns ``{doctype, user, unfollowed:[name,...], count}``."""
	target_user = user or frappe.session.user

	if names is not None:
		return _unfollow_document_batch(doctype, names, target_user)

	unfollowed = _unfollow_document_one(doctype, name, target_user)
	return {
		"doctype": doctype,
		"name": name,
		"user": target_user,
		"unfollowed": unfollowed,
	}


def _unfollow_document_one(doctype: str, name: str, user: str) -> bool:
	"""Unsubscribe ONE record. Returns whether a follow row was actually removed
	(False if not following). Frappe lets a user stop their OWN notifications
	without doc read access (Desk allows it); only unfollowing on behalf of
	ANOTHER user needs a read floor here (the helper additionally requires
	Document Follow write for that cross-user path)."""
	if user != frappe.session.user:
		frappe.has_permission(doctype, "read", doc=name, throw=True)

	from frappe.desk.form.document_follow import (
		unfollow_document as _unfollow,
	)

	result = _unfollow(doctype=doctype, doc_name=name, user=user)
	return bool(result)


def _unfollow_document_batch(doctype: str, names: list, user: str) -> dict:
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> str:
		if not frappe.db.exists(doctype, name):
			raise InvalidArgumentError(f"unknown {doctype}: {name}")
		_unfollow_document_one(doctype, name, user)
		return name

	unfollowed = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "user": user, "unfollowed": unfollowed, "count": len(unfollowed)}
