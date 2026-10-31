"""Shared patches for tests that call the plugin's signed routes without a stored key."""

from unittest.mock import patch

SESSION = "agent:main:dashboard:conv-test"
SIGNER = "jarvis.chat.plugin_routes.signed_route_headers"


def fake_signer(session_key, body):
	"""Stands in for ``signed_route_headers``: records the session, signs nothing."""
	return {"X-Jarvis-Token": "test-key", "X-Jarvis-Session": session_key}


def patch_signer():
	"""Patch the route signer; stop with the returned patcher (or ``addCleanup``)."""
	return patch(SIGNER, side_effect=fake_signer)
