"""Create a ToDo + assign a user to a document (one, or a whole batch).

Wraps ``frappe.desk.form.assign_to.add``. The underlying helper takes a
single ``args`` dict (Frappe's idiomatic form-bag shape); we accept
typed arguments and pack into the dict so the agent has a clean
signature.

Side-effects beyond the ToDo row: assignment auto-shares the
referenced document with the assignee (so they can read it) AND fires
a "you've been assigned" notification email by default.

ALWAYS-CONFIRM: a person gets a notification email.

Two shapes:

- **Single:** ``assign_to(doctype, name, user, ...)`` ->
  ``{doctype, name, user, description, notify, priority, date}``.
- **Batch:** ``assign_to(doctype, names=[...], user=..., ...)`` ->
  ``{doctype, user, assigned:[name,...], count}`` - the same user
  assigned to every record in ONE atomic savepoint (all-or-nothing).
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import desk_action
from jarvis.tools._bulk import run_atomic_batch


@desk_action(check_user_arg="user")
def assign_to(
	doctype: str,
	name: str | None = None,
	user: str | None = None,
	description: str | None = None,
	notify: bool = True,
	priority: str | None = None,
	date: str | None = None,
	names: list | None = None,
) -> dict:
	"""Open a ToDo for ``user`` assigned to ``doctype/name`` - or to
	every doc in ``names``.

	Single: returns ``{doctype, name, user, description, notify, priority, date}``.
	Batch: returns ``{doctype, user, assigned:[name,...], count}``."""
	if not user:
		raise InvalidArgumentError("user is required")

	if names is not None:
		return _assign_to_batch(doctype, names, user, description, notify, priority, date)

	_assign_to_one(doctype, name, user, description, notify, priority, date)
	return {
		"doctype": doctype,
		"name": name,
		"user": user,
		"description": description,
		"notify": bool(notify),
		"priority": priority,
		"date": date,
	}


def _assign_to_one(
	doctype: str,
	name: str,
	user: str,
	description: str | None,
	notify: bool,
	priority: str | None,
	date: str | None,
) -> None:
	"""Read-permission check + open the ToDo, for ONE record."""
	frappe.has_permission(doctype, "read", doc=name, throw=True)

	from frappe.desk.form.assign_to import add as _assign_add

	args = {
		"doctype": doctype,
		"name": name,
		"assign_to": [user],
		"description": description or "",
		"notify": int(bool(notify)),
	}
	if priority:
		args["priority"] = priority
	if date:
		args["date"] = date

	_assign_add(args)


def _assign_to_batch(
	doctype: str,
	names: list,
	user: str,
	description: str | None,
	notify: bool,
	priority: str | None,
	date: str | None,
) -> dict:
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> str:
		if not frappe.db.exists(doctype, name):
			raise InvalidArgumentError(f"unknown {doctype}: {name}")
		_assign_to_one(doctype, name, user, description, notify, priority, date)
		return name

	assigned = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "user": user, "assigned": assigned, "count": len(assigned)}
