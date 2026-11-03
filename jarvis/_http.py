"""Shared HTTP helpers for Jarvis whitelisted endpoints.

Provides ``validate_bearer()`` for X-Jarvis-Token header auth.
"""

import hmac

import frappe


def constant_time_equal(a: str, b: str) -> bool:
	"""Compare text values without allowing non-ASCII input to raise.

	``hmac.compare_digest`` rejects non-ASCII ``str`` values. Compare UTF-8
	bytes instead so malformed request headers fail closed. ``surrogatepass``
	also makes a hostile lone surrogate a mismatch rather than a 500.
	"""
	return hmac.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass"))


def validate_bearer() -> bool:
	"""Return True iff the X-Jarvis-Token header matches either stored key: the callback key or, until retirement clears it, the agent token.

	We use a custom header rather than ``Authorization: Bearer`` so that Frappe's
	built-in OAuth validator (which runs before our handler) does not see a Bearer
	token it cannot resolve and raise AuthenticationError prematurely.
	"""
	from jarvis._plugin_auth import match_stored_key, stored_keys

	presented = frappe.request.headers.get("X-Jarvis-Token", "").strip()
	if not presented:
		return False
	keys = stored_keys()
	if not keys:
		return False
	# Constant-time comparison against each stored key (callback key, then owner token).
	return bool(match_stored_key(presented, keys))
