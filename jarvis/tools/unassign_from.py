"""Close a user's ToDo assignment on a document (one, or a whole batch).

Wraps ``frappe.desk.form.assign_to.remove``. The underlying helper
sets the ToDo's status to ``Closed`` rather than deleting it - the
record stays for audit history. The auto-share created by ``assign_to``
is not revoked (a separate ``unshare_doc`` call handles that).

Two shapes:

- **Single:** ``unassign_from(doctype, name, user)`` -> ``{doctype, name, user}``.
- **Batch:** ``unassign_from(doctype, names=[...], user=...)`` ->
  ``{doctype, user, unassigned:[name,...], count}`` - the same user
  unassigned from every record in ONE atomic savepoint (all-or-nothing).
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import desk_action
from jarvis.tools._bulk import run_atomic_batch


@desk_action(check_user_arg="user")
def unassign_from(
	doctype: str,
	name: str | None = None,
	user: str | None = None,
	names: list | None = None,
) -> dict:
	"""Close the ToDo assigning ``user`` to ``doctype/name`` - or to
	every doc in ``names``.

	Single: returns ``{doctype, name, user}``.
	Batch: returns ``{doctype, user, unassigned:[name,...], count}``."""
	if not user:
		raise InvalidArgumentError("user is required")

	if names is not None:
		return _unassign_from_batch(doctype, names, user)

	_unassign_from_one(doctype, name, user)
	return {"doctype": doctype, "name": name, "user": user}


def _unassign_from_one(doctype: str, name: str, user: str) -> None:
	"""Read-permission check + close the ToDo, for ONE record."""
	frappe.has_permission(doctype, "read", doc=name, throw=True)

	from frappe.desk.form.assign_to import remove as _assign_remove

	_assign_remove(doctype=doctype, name=name, assign_to=user)


def _unassign_from_batch(doctype: str, names: list, user: str) -> dict:
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> str:
		if not frappe.db.exists(doctype, name):
			raise InvalidArgumentError(f"unknown {doctype}: {name}")
		_unassign_from_one(doctype, name, user)
		return name

	unassigned = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "user": user, "unassigned": unassigned, "count": len(unassigned)}
