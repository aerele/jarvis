"""Per-tool implementations live in sibling modules
(jarvis.tools.get_doc, etc.); they're dispatched by
jarvis.tools.registry.

Shared helpers go here so per-tool files don't repeat them.
"""

from __future__ import annotations

from functools import wraps

import frappe

from jarvis.exceptions import InvalidArgumentError


def require_doctype_and_name(doctype: str, name: str) -> None:
	"""Reject empty doctype / name with InvalidArgumentError.

	Six tools (amend_doc, cancel_doc, delete_doc, get_doc, submit_doc,
	update_doc) all start with the identical two-line guard; the
	2026-06-16 review flagged this as duplicated prologue. Factored
	here so future tools that take a (doctype, name) pair have one
	place to import from and changes to the rejection message land
	in one site.
	"""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not name:
		raise InvalidArgumentError("name is required")


def desk_action(check_user_arg: str | None = None):
	"""Decorator for desk-action tools that take ``(doctype, name, ...)``.

	Hoists the three repeated guards each tool would otherwise inline:
	1. ``require_doctype_and_name(doctype, name)`` - reject empty inputs
	2. ``frappe.db.exists(doctype, name)`` - reject phantom doc references
	3. (optional) ``frappe.db.exists("User", <arg>)`` - reject phantom
	   User refs when the tool takes a user-id arg (assign_to, share_doc,
	   follow_document, etc.). Pass the arg name as ``check_user_arg``;
	   the decorator looks it up positionally or by keyword.

	Used by add_comment / add_tag / remove_tag / assign_to / unassign_from
	/ share_doc / unshare_doc / follow_document / unfollow_document /
	attach_to_doc. Other tools (update_comment) have a different
	signature and don't fit.
	"""

	def _deco(fn):
		@wraps(fn)
		def _wrapped(doctype: str, name: str | None = None, *args, **kwargs):
			# Bulk overload: a call carrying a non-empty ``names`` list validates
			# each record (existence + per-record permission) inside the tool's
			# own atomic batch, so the decorator only enforces the doctype +
			# the single shared user arg here - not the (absent) single name.
			names = kwargs.get("names")
			if isinstance(names, list) and names:
				if not doctype:
					raise InvalidArgumentError("doctype is required")
			else:
				require_doctype_and_name(doctype, name)
				if not frappe.db.exists(doctype, name):
					raise InvalidArgumentError(f"unknown {doctype}: {name}")
			if check_user_arg:
				# Look up the user-id arg positionally or by keyword.
				# Per-tool signatures put it at varying positions
				# (assign_to: user is 3rd positional; share_doc: user is
				# 3rd; follow_document: user is optional kwarg), so we
				# bind via the wrapped function's argspec.
				import inspect

				sig = inspect.signature(fn)
				bound = sig.bind_partial(doctype, name, *args, **kwargs)
				bound.apply_defaults()
				user = bound.arguments.get(check_user_arg)
				if user and not frappe.db.exists("User", user):
					raise InvalidArgumentError(f"unknown User: {user}")
			return fn(doctype, name, *args, **kwargs)

		return _wrapped

	return _deco
