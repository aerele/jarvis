"""Edit a Comment's body.

Wraps ``frappe.desk.form.utils.update_comment``. The bundled helper
permits the comment's owner to edit it (System Manager bypasses); we
forward as-is so the agent gets the same gate the Desk UI applies.

Composes with ``add_comment``: the new ``comment_name`` returned from
add_comment is what update_comment targets.
"""

from __future__ import annotations

import frappe

from jarvis._text import plaintext_to_html
from jarvis.exceptions import InvalidArgumentError


def update_comment(name: str, content: str) -> dict:
	"""Replace the body of Comment ``name`` with ``content``. Returns
	``{comment_name, content}``."""
	if not name:
		raise InvalidArgumentError("name is required")
	if content is None or content == "":
		raise InvalidArgumentError("content is required")
	if not frappe.db.exists("Comment", name):
		raise InvalidArgumentError(f"unknown Comment: {name}")

	from frappe.desk.form.utils import update_comment as _uc

	# Comment.content is an HTML field; convert the agent's plain text so \n
	# newlines survive instead of collapsing to one line (jarvis._text).
	_uc(name=name, content=plaintext_to_html(content))
	return {"comment_name": name, "content": content}
