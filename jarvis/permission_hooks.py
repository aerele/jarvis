"""Permission hooks registered for every DocType (the ``"*"`` key).

Jarvis refuses to vouch for row access it cannot reproduce, and an arbitrary
``"*"`` permission hook can narrow any DocType, so callers treat the key as "unknown
custom row filtering". ERPNext now registers one of its own (the Company
Restriction feature), which would make that refusal permanent on every site
running it. This module lets the audited ERPNext functions through, but only
while they provably cannot narrow anything for the user. Anything else under
``"*"``, and every doctype-specific hook, is passed back untouched.
"""

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions

_COMPANY_RESTRICTION = "erpnext.stock.doctype.company_restriction.company_restriction."

_RESTRICTABLE_MASTERS = ["Item", "Customer", "Supplier"]

# Audited against erpnext 495380b5 (company_restriction.py). Both functions return "no
# restriction" / ``True`` when the user has no Company User Permission
# (``get_allowed_companies`` is ``None``) and also when no Item, Customer or Supplier is
# restricted to companies (no Company Restriction rows, nothing to block).
AUDITED_WILDCARD_HOOKS = {
	"permission_query_conditions": frozenset(
		{_COMPANY_RESTRICTION + "get_inherited_permission_query_conditions"}
	),
	"has_permission": frozenset({_COMPANY_RESTRICTION + "has_inherited_permission"}),
}


def effective_hooks(hook_name: str, user: str | None = None):
	"""``frappe.get_hooks(hook_name)`` without an inert audited ``"*"`` entry.

	The audited wildcard hooks only restrict a user who holds a Company User Permission,
	and only while some Item, Customer or Supplier is restricted to companies; otherwise
	they are a no-op and are dropped. In every other case, or when an unaudited ``"*"``
	hook is registered (alone or next to the audited one), the entry is kept and callers
	refuse or prove as they always did. Re-evaluated on every call, so configuring a
	restriction later closes the gate again.
	"""
	hooks = frappe.get_hooks(hook_name, {})
	if not isinstance(hooks, dict) or "*" not in hooks:
		return hooks
	registered = hooks["*"]
	if isinstance(registered, str):
		registered = [registered]
	if not isinstance(registered, (list, tuple)) or not registered:
		return hooks
	if not set(registered) <= AUDITED_WILDCARD_HOOKS.get(hook_name, frozenset()):
		return hooks
	if "Company" in get_user_permissions(user or frappe.session.user) and _restriction_configured():
		return hooks
	return {key: value for key, value in hooks.items() if key != "*"}


def _restriction_configured() -> bool:
	"""False only when no master is restricted to companies; any doubt says True."""
	try:
		if frappe.db.exists(
			"Company Restriction",
			{"parentfield": "allowed_companies", "parenttype": ["in", _RESTRICTABLE_MASTERS]},
		):
			return True
		return any(
			frappe.db.exists(doctype, {"restrict_to_companies": 1}) for doctype in _RESTRICTABLE_MASTERS
		)
	except Exception:
		# Fail closed: an unreadable answer must keep the hook counted, never drop it.
		return True
