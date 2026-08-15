"""Revoke a user's share permissions on a document (one, or a whole batch).

Wraps ``frappe.share.remove``. Removes the DocShare row entirely; the
target user falls back to whatever DocType-level perms they had before
the share was granted (which may still leave them with access).

Permission: requires "share" permission on each target doc; the explicit
``has_permission`` floor lives in ``_unshare_doc_one`` so the per-record
check is visible and runs on every record in a batch.

Two shapes:

- **Single:** ``unshare_doc(doctype, name, user)`` -> ``{doctype, name, user}``.
- **Batch:** ``unshare_doc(doctype, names=[...], user=...)`` ->
  ``{doctype, user, unshared:[name,...], count}`` - the same user's grant
  revoked on every record in ONE atomic savepoint (all-or-nothing).
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import desk_action
from jarvis.tools._bulk import run_atomic_batch


@desk_action(check_user_arg="user")
def unshare_doc(
	doctype: str,
	name: str | None = None,
	user: str | None = None,
	names: list | None = None,
) -> dict:
	"""Remove the DocShare granting ``user`` access to ``doctype/name`` -
	or on every doc in ``names``.

	Single: returns ``{doctype, name, user}``.
	Batch: returns ``{doctype, user, unshared:[name,...], count}``."""
	if not user:
		raise InvalidArgumentError("user is required")

	if names is not None:
		return _unshare_doc_batch(doctype, names, user)

	_unshare_doc_one(doctype, name, user)
	return {"doctype": doctype, "name": name, "user": user}


def _unshare_doc_one(doctype: str, name: str, user: str) -> None:
	"""Share-permission check + remove the DocShare, for ONE record."""
	# frappe.share.remove() (without ignore_permissions) deletes the DocShare
	# row via frappe.delete_doc, which checks DELETE permission on the
	# DocShare doctype itself - a System-Manager-only role table with no
	# if_owner - so it denies every ordinary user, even one with legitimate
	# share rights on the target document. The correct boundary is "share"
	# permission on the TARGET doc (mirroring share_doc's implicit check via
	# frappe.share.add -> check_share_permission), then bypass the DocShare
	# ACL explicitly - the same pattern frappe.share.set_docshare_permission
	# uses (share.flags.ignore_permissions = True before deleting).
	frappe.has_permission(doctype, "share", doc=name, throw=True)

	from frappe.share import remove as _share_remove

	_share_remove(doctype=doctype, name=name, user=user, flags={"ignore_permissions": True})


def _unshare_doc_batch(doctype: str, names: list, user: str) -> dict:
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> str:
		if not frappe.db.exists(doctype, name):
			raise InvalidArgumentError(f"unknown {doctype}: {name}")
		_unshare_doc_one(doctype, name, user)
		return name

	unshared = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "user": user, "unshared": unshared, "count": len(unshared)}
