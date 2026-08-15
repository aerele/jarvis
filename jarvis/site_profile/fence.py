"""Per-user permission fence over collect_profile() output.

Drops every item attributable to a doctype the caller cannot read. The shape
holds no precomputed cross-doctype totals - render counts the fenced lists,
so a clerk's index says 12 custom doctypes, never "12 of 31". App/module
names pass unfenced (the accepted coarse leak, matching the clause).
"""

from __future__ import annotations

import frappe


def fence_for_user(data: dict, user: str | None = None) -> dict:
	"""Filter ``data`` to what ``user`` may read. New dict, same shape.
	Fails closed per item: a has_permission error hides the doctype."""
	user = user or frappe.session.user
	verdicts: dict[str, bool] = {}

	def can_read(doctype: str | None) -> bool:
		# Unkeyed entries name no doctype - nothing to leak.
		if not doctype:
			return True
		if doctype not in verdicts:
			try:
				verdicts[doctype] = bool(frappe.has_permission(doctype, ptype="read", user=user))
			except Exception:
				verdicts[doctype] = False
		return verdicts[doctype]

	return {
		"apps": list(data.get("apps") or []),
		"modules": dict(data.get("modules") or {}),
		"custom_doctypes": [d for d in (data.get("custom_doctypes") or []) if can_read(d.get("name"))],
		"core_customizations": [
			c for c in (data.get("core_customizations") or []) if can_read(c.get("doctype"))
		],
		"workflows": [w for w in (data.get("workflows") or []) if can_read(w.get("doctype"))],
		"reports": [r for r in (data.get("reports") or []) if can_read(r.get("doctype"))],
		"print_formats": [p for p in (data.get("print_formats") or []) if can_read(p.get("doctype"))],
		"scripts": {
			"server": {
				k: v for k, v in ((data.get("scripts") or {}).get("server") or {}).items() if can_read(k)
			},
			"client": {
				k: v for k, v in ((data.get("scripts") or {}).get("client") or {}).items() if can_read(k)
			},
		},
	}
