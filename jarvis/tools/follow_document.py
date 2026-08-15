"""Subscribe a user to a document's change events (one, or a whole batch).

Wraps ``frappe.desk.form.document_follow.follow_document``. A
followed doc fires the Document Follow notification email to the
subscriber on every modification - the agent uses this for "let me
know when X changes" / "subscribe X to this Customer" flows.

By default ``user`` is the session user, so the agent can follow a
doc on behalf of the customer without naming them.

Permission: requires read permission on each target doc; the explicit
``has_permission`` floor lives in ``_follow_document_one`` so the
per-record check is visible and runs on every record in a batch.

Two shapes:

- **Single:** ``follow_document(doctype, name, user=...)`` ->
  ``{doctype, name, user, followed}``.
- **Batch:** ``follow_document(doctype, names=[...], user=...)`` ->
  ``{doctype, user, followed:[name,...], count}`` - the same user
  subscribed to every record in ONE atomic savepoint (all-or-nothing).
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import desk_action
from jarvis.tools._bulk import run_atomic_batch


@desk_action(check_user_arg="user")
def follow_document(
	doctype: str,
	name: str | None = None,
	user: str | None = None,
	names: list | None = None,
) -> dict:
	"""Subscribe ``user`` (or the session user) to ``doctype/name`` - or
	to every doc in ``names``.

	Single: returns ``{doctype, name, user, followed}`` where ``followed``
	is True on the wire path that actually inserted a follow row, False
	when the doc was already being followed (idempotent).
	Batch: returns ``{doctype, user, followed:[name,...], count}``."""
	target_user = user or frappe.session.user

	if names is not None:
		return _follow_document_batch(doctype, names, target_user)

	followed = _follow_document_one(doctype, name, target_user)
	return {
		"doctype": doctype,
		"name": name,
		"user": target_user,
		"followed": followed,
	}


def _follow_document_one(doctype: str, name: str, user: str) -> bool:
	"""Read-permission check + subscribe, for ONE record. Returns whether a
	follow row was actually inserted (False if already following)."""
	frappe.has_permission(doctype, "read", doc=name, throw=True)

	from frappe.desk.form.document_follow import (
		follow_document as _follow,
	)

	result = _follow(doctype=doctype, doc_name=name, user=user)
	return bool(result)


def _follow_document_batch(doctype: str, names: list, user: str) -> dict:
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> str:
		if not frappe.db.exists(doctype, name):
			raise InvalidArgumentError(f"unknown {doctype}: {name}")
		_follow_document_one(doctype, name, user)
		return name

	followed = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "user": user, "followed": followed, "count": len(followed)}
