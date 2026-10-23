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

import inspect

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import desk_action
from jarvis.tools._bulk import run_atomic_batch


def require_system_manager_for_other_user(user: str, verb: str) -> None:
	"""Acting on behalf of ANOTHER user needs Administrator or System Manager:
	Frappe's follow helpers do not gate ``user``, and Document Follow rights
	are held by every Desk User, so that doctype's permission restricts nobody.
	Called once up front so a batch is refused as a whole."""
	if user == frappe.session.user:
		return
	if frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles():
		return
	raise PermissionDeniedError(f"only a System Manager may {verb} a document for another user")


def pick_follow_function(fn_name: str):
	"""Frappe's ``follow_document`` / ``unfollow_document`` taking a user as the
	third positional argument. v15's public function takes ``user`` itself; v16's
	whitelisted one acts for the session user and the user-taking variant is the
	underscore-prefixed one. Picked by signature so both majors work, resolved at
	call time."""
	from frappe.desk.form import document_follow

	public = getattr(document_follow, fn_name)
	if "user" in inspect.signature(public).parameters:
		return public
	return getattr(document_follow, f"_{fn_name}")


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
	require_system_manager_for_other_user(target_user, "follow")

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

	result = pick_follow_function("follow_document")(doctype, name, user)
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
