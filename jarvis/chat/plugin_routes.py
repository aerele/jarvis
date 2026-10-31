"""Signing for the site's requests to the plugin's routes.

The plugin verifies ``X-Jarvis-Token`` plus an HMAC over
``session | sha256(body) | nonce | timestamp``. It mirrors ``signRequest`` in the
plugin's ``frappe-client.ts``. The site signs with the per-tenant callback key when it
has one, else the owner token (the key-distribution rule: callback key first).
"""

import hashlib
import hmac
import secrets
import time

from jarvis._plugin_auth import _agent_token, _callback_key
from jarvis.exceptions import JarvisError


class PluginRouteKeyMissingError(JarvisError):
	"""Neither a callback key nor an owner token is stored, so nothing can be signed."""


def signed_route_headers(session_key: str, body: bytes) -> dict:
	"""Headers for a signed request to a plugin route.

	``body`` must be the exact bytes sent on the wire: the signature covers its sha256.
	"""
	key = _callback_key() or _agent_token()
	if not key:
		raise PluginRouteKeyMissingError("No plugin signing key is stored on this site.")
	nonce = secrets.token_hex(16)
	timestamp = str(int(time.time()))
	canonical = "|".join([session_key, hashlib.sha256(body or b"").hexdigest(), nonce, timestamp])
	signature = hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
	return {
		"X-Jarvis-Token": key,
		"X-Jarvis-Session": session_key,
		"X-Jarvis-Signature": signature,
		"X-Jarvis-Nonce": nonce,
		"X-Jarvis-Timestamp": timestamp,
	}
