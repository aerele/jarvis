"""Shared HTTP helpers for Jarvis whitelisted endpoints.

Provides ``validate_bearer()`` for X-Jarvis-Token header auth.
"""

import hmac

import frappe


def validate_bearer() -> bool:
	"""Return True iff the X-Jarvis-Token header contains the correct agent token.

	We use a custom header rather than ``Authorization: Bearer`` so that Frappe's
	built-in OAuth validator (which runs before our handler) does not see a Bearer
	token it cannot resolve and raise AuthenticationError prematurely.
	"""
	presented = frappe.request.headers.get("X-Jarvis-Token", "").strip()
	if not presented:
		return False
	settings = frappe.get_single("Jarvis Settings")
	expected = settings.get_password("agent_token") or ""
	if not expected:
		return False
	# Constant-time comparison to prevent timing-oracle attacks
	return hmac.compare_digest(presented, expected)
