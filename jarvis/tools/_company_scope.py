"""Company-scope gating by User Permission (not Company-doctype read).

ERPNext scopes multi-company access via User Permissions on Company, not the
Company doctype's own read role - so a user with no Company User Permission is
NOT company-restricted, and a user WITH one may only touch companies in that
set. Gating on Company-doctype read would wrongly deny the "Auditor" role
(GL read, Company `select` only).
"""

from __future__ import annotations

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions

from jarvis.exceptions import PermissionDeniedError


def _permitted_companies(user: str | None = None):
	"""Set of companies the user is restricted to, or None if unrestricted."""
	scope = get_user_permissions(user or frappe.session.user).get("Company")
	return {up.get("doc") for up in scope} if scope else None


def is_company_permitted(company: str, user: str | None = None) -> bool:
	allowed = _permitted_companies(user)
	return allowed is None or company in allowed


def assert_company_permitted(company: str) -> None:
	if not is_company_permitted(company):
		raise PermissionDeniedError(
			f"no access to company {company!r} (restricted by Company User Permission)"
		)
