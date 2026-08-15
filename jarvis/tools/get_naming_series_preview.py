"""Preview the next 3 names a naming series will generate.

Wraps ``frappe.model.naming.NamingSeries.get_preview``. Returns three
projected names WITHOUT consuming the DB counter, so the agent can
answer "what's the next invoice number going to be?" without side
effects.

Read-only: the underlying helper uses a fake-counter callback and
never touches the Series table. We add an Item-style existence check
so a typo'd DocType / series surfaces as InvalidArgumentError.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import (
	InvalidArgumentError,
	PermissionDeniedError,
)


def get_naming_series_preview(
	doctype: str,
	series: str | None = None,
) -> dict:
	"""Return ``{preview, doctype, series}`` where ``preview`` is a
	list of 3 projected names.

	If ``series`` is omitted, the DocType's first ``autoname``-style
	series is used. The agent typically passes the bare series string
	(e.g. ``SINV-.YYYY.-``).
	"""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not frappe.db.exists("DocType", doctype):
		raise InvalidArgumentError(f"unknown DocType: {doctype}")
	if not frappe.has_permission(doctype, "create"):
		raise PermissionDeniedError(
			f"no create permission on {doctype}; preview not allowed",
		)

	# Resolve series from the DocType's autoname if not supplied.
	if not series:
		autoname = frappe.db.get_value("DocType", doctype, "autoname") or ""
		if not autoname:
			raise InvalidArgumentError(
				f"{doctype} has no autoname; pass `series` explicitly",
			)
		# autoname can be "naming_series:", "field:somefield", "hash" -
		# we only support the explicit series strings.
		if autoname.startswith("field:") or autoname == "hash":
			raise InvalidArgumentError(
				f"{doctype} uses {autoname!r} naming; series preview "
				f"is only meaningful for static series patterns",
			)
		series = autoname.split(":", 1)[1] if ":" in autoname else autoname

	from frappe.model.naming import NamingSeries

	preview = NamingSeries(series).get_preview()
	return {
		"preview": preview,
		"doctype": doctype,
		"series": series,
	}
