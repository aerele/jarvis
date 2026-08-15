"""Bench-side support endpoints (Plan 3 B3).

Role-gated: the caller's scope (own|all) is derived here from their bench roles and forwarded to
the control-plane proxy along with their identity. The control plane re-derives the customer from
the API key, so a role-less caller can't reach another customer's data. Admin-side errors are
surfaced as clean toasts via _surface. House envelope: {"ok": True, "data": ...}.
"""

import frappe
from frappe.utils import validate_email_address

from jarvis import admin_client
from jarvis.onboarding import _surface
from jarvis.permissions import support_scope


def _scope() -> str:
	scope = support_scope()
	if scope is None:
		frappe.throw("You don't have Jarvis support access.", frappe.PermissionError)
	return scope


def _requesting_user() -> str:
	"""The identity forwarded to the control plane — used both as Helpdesk's
	`raised_by` and as our per-user visibility key (raised_by == requesting_user).

	Helpdesk requires `raised_by` to be a valid email. `frappe.session.user` is
	the login name, which for real tenant users IS their email — but system
	accounts (Administrator, Guest) are named, not emailed, so Helpdesk rejects
	them with InvalidEmailAddressError. Resolve the User's email field (the idiom
	frappe core itself uses), falling back to the login name when it is unset.
	For a normal email-named user this is a no-op.
	"""
	user = frappe.session.user
	return frappe.db.get_value("User", user, "email") or user


@frappe.whitelist()
def list_tickets() -> dict:
	scope = _scope()
	return {
		"ok": True,
		"data": _surface(admin_client.support_list_tickets, requesting_user=_requesting_user(), scope=scope),
	}


@frappe.whitelist()
def create_ticket(subject: str, body: str = "") -> dict:
	scope = _scope()
	# raised_by must be a valid email — Helpdesk rejects a non-email, and that
	# rejection reaches the customer as an opaque "admin is unreachable" toast.
	# Fail fast here with an actionable message. _requesting_user() resolves the
	# User's email; a blank/invalid one is caught here rather than at Helpdesk.
	user = _requesting_user()
	if not validate_email_address(user, throw=False):
		frappe.throw(
			"Your account has no valid email address. Add one in your profile "
			"before raising a support ticket."
		)
	return {
		"ok": True,
		"data": _surface(
			admin_client.support_create_ticket,
			subject=subject,
			body=body or "",
			requesting_user=user,
			scope=scope,
		),
	}


@frappe.whitelist()
def get_thread(ticket: str) -> dict:
	scope = _scope()
	return {
		"ok": True,
		"data": _surface(
			admin_client.support_get_thread,
			ticket=ticket,
			requesting_user=_requesting_user(),
			scope=scope,
		),
	}


@frappe.whitelist()
def reply(ticket: str, body: str) -> dict:
	scope = _scope()
	return {
		"ok": True,
		"data": _surface(
			admin_client.support_reply,
			ticket=ticket,
			body=body,
			requesting_user=_requesting_user(),
			scope=scope,
		),
	}


@frappe.whitelist()
def close_ticket(ticket: str) -> dict:
	scope = _scope()
	return {
		"ok": True,
		"data": _surface(
			admin_client.support_close_ticket,
			ticket=ticket,
			requesting_user=_requesting_user(),
			scope=scope,
		),
	}


@frappe.whitelist()
def awaiting_count() -> dict:
	scope = _scope()
	return {
		"ok": True,
		"data": _surface(
			admin_client.support_awaiting_count, requesting_user=_requesting_user(), scope=scope
		),
	}
