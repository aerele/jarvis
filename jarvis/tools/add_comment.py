"""Add a comment to a document (or the same comment to a batch).

Wraps ``frappe.desk.form.utils.add_comment``. ``comment_email`` and
``comment_by`` are auto-populated from the session user so the agent
doesn't have to specify "who" the comment is from.

Comment is distinct from Communication: a Comment is a free-form note
attached to the doc's timeline (no email side-effect).

Permission: read perm on each reference doc (checked per record - the
underlying helper uses ``get_lazy_doc(check_permission=True)`` and we add
an explicit ``has_permission`` floor so the per-record check is visible here).

Two shapes:

- **Single:** ``add_comment(doctype, name, content)``.
- **Batch:** ``add_comment(doctype, names=[...], content=...)`` -> the same
  note on every record in ONE atomic savepoint.
"""

from __future__ import annotations

import frappe

from jarvis._text import plaintext_to_html
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import desk_action
from jarvis.tools._bulk import run_atomic_batch


@desk_action()
def add_comment(
	doctype: str,
	name: str | None = None,
	content: str | None = None,
	names: list | None = None,
) -> dict:
	"""Add a Comment with ``content`` to one doc - or to every doc in ``names``.

	Single: returns ``{comment_name, doctype, name, content}``.
	Batch: returns ``{doctype, commented:[name,...], count}``.
	"""
	if content is None or content == "":
		raise InvalidArgumentError("content is required")

	if names is not None:
		return _add_comment_batch(doctype, names, content)

	comment_name = _add_comment_one(doctype, name, content)
	return {"comment_name": comment_name, "doctype": doctype, "name": name, "content": content}


def _add_comment_one(doctype: str, name: str, content: str) -> str | None:
	"""Read-permission check + add the comment, for ONE record."""
	frappe.has_permission(doctype, "read", doc=name, throw=True)

	from frappe.desk.form.utils import add_comment as _ac

	session_user = frappe.session.user
	user_full_name = frappe.db.get_value("User", session_user, "full_name") or session_user
	comment = _ac(
		reference_doctype=doctype,
		reference_name=name,
		# Comment.content is an HTML field; the agent writes plain text with \n
		# newlines, so convert or the note collapses to one line (jarvis._text).
		content=plaintext_to_html(content),
		comment_email=session_user,
		comment_by=user_full_name,
	)
	return comment.name if comment else None


def _add_comment_batch(doctype: str, names: list, content: str) -> dict:
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not isinstance(names, list) or not names:
		raise InvalidArgumentError("names must be a non-empty list of document names")

	def _do(name: str) -> str:
		if not frappe.db.exists(doctype, name):
			raise InvalidArgumentError(f"unknown {doctype}: {name}")
		_add_comment_one(doctype, name, content)  # per-record read-permission check
		return name

	commented = run_atomic_batch(names, _do, label=lambda n: n)
	return {"doctype": doctype, "commented": commented, "count": len(commented)}
