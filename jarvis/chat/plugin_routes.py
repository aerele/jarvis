"""Signing for the site's requests to the plugin's routes.

The plugin verifies ``X-Jarvis-Token`` plus an HMAC over
``session | sha256(body) | nonce | timestamp``. It mirrors ``signRequest`` in the
plugin's ``frappe-client.ts``. The site signs with the per-tenant callback key when it
has one, else the owner token (the key-distribution rule: callback key first).
"""

import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import quote

from jarvis._plugin_auth import _agent_token, _callback_key
from jarvis.exceptions import JarvisError


class PluginRouteKeyMissingError(JarvisError):
	"""Neither a callback key nor an owner token is stored, so nothing can be signed."""


class PluginRouteError(JarvisError):
	"""A plugin route call could not be made or was refused. The message is fixed text."""


# Plugin routes, relative to the agent's HTTP base (the plugin's ROUTE_PATHS).
LLM_TASK_PATH = "/jarvis/llm-task"
CANVAS_PATH = "/jarvis/canvas/"
SESSIONS_DELETE_PATH = "/jarvis/sessions/delete"
SESSIONS_PATCH_PATH = "/jarvis/sessions/patch"

_ROUTE_CONNECT_TIMEOUT_S = 10
# The only session fields the sessions/patch route accepts; the plugin refuses the rest.
PATCH_ALLOWED_FIELDS = frozenset({"inheritedToolAllow", "inheritedToolDeny", "inheritedToolPolicyVersion"})


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


def canvas_path(name: str) -> str:
	"""The canvas route path for an artifact name; nested names are kept, ``..`` never passes."""
	segments = str(name).split("/")
	if not name or any(seg in ("", ".", "..") for seg in segments):
		raise PluginRouteError("Invalid canvas name.")
	return CANVAS_PATH + "/".join(quote(seg, safe="") for seg in segments)


def route_request(
	agent_url: str,
	method: str,
	path: str,
	session_key: str,
	body: dict | None = None,
	*,
	read_timeout_s: float = 30,
	stream: bool = False,
):
	"""One signed request to a plugin route; returns the ``requests`` response.

	The host is always the tenant's own ``agent_url``. The body is serialised once and the
	same bytes are signed and sent. Redirects are never followed. Raises
	``PluginRouteError`` (fixed text) when the URL is unset, nothing can be signed or the
	request fails; an HTTP error status is the caller's to read.
	"""
	import requests

	from jarvis.chat.canvas import _http_base

	base = _http_base(agent_url)
	if not base:
		raise PluginRouteError("The agent is not configured.")
	data = b"" if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
	try:
		headers = signed_route_headers(session_key, data)
	except PluginRouteKeyMissingError as e:
		raise PluginRouteError("No plugin signing key is stored on this site.") from e
	if body is not None:
		headers["Content-Type"] = "application/json"
	try:
		return requests.request(
			method,
			f"{base}{path}",
			data=data or None,
			headers=headers,
			timeout=(_ROUTE_CONNECT_TIMEOUT_S, read_timeout_s),
			stream=stream,
			allow_redirects=False,
		)
	except requests.Timeout as e:
		raise PluginRouteError("The agent route timed out.") from e
	except requests.RequestException as e:
		raise PluginRouteError("The agent route request failed.") from e


def post_session_action(
	agent_url: str, path: str, session_key: str, body: dict, *, read_timeout_s: float = 30
) -> dict:
	"""POST a sessions/delete or sessions/patch body; returns the plugin's payload.

	401/403/404/5xx raise ``PluginRouteError``. Only a 502 carries the gateway's refusal text
	(for example "session not found"), which the plugin has already scrubbed of secrets.
	"""
	response = route_request(agent_url, "POST", path, session_key, body, read_timeout_s=read_timeout_s)
	try:
		parsed = response.json()
	except ValueError:
		parsed = {}
	if response.status_code != 200 or not isinstance(parsed, dict) or not parsed.get("ok"):
		# Only a 502 carries the gateway's own (scrubbed) refusal text, e.g. "session not
		# found". Any other status gets a fixed message, so a missing route's "Not Found"
		# can never be read by a caller as "the session is already gone".
		if response.status_code == 502 and isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
			raise PluginRouteError(f"The agent route was refused (HTTP 502): {parsed['error'][:200]}")
		raise PluginRouteError(f"The agent route was refused (HTTP {response.status_code}).")
	payload = parsed.get("payload")
	return payload if isinstance(payload, dict) else {}


def delete_session(agent_url: str, session_key: str, *, read_timeout_s: float = 30) -> None:
	"""Delete a gateway session through the plugin. Call it BEFORE the site drops its own
	row for the key: the plugin vouches the key with this site and refuses an unknown one.
	``read_timeout_s`` bounds the wait for the reply (sweeps pass a short one)."""
	post_session_action(
		agent_url, SESSIONS_DELETE_PATH, session_key, {"key": session_key}, read_timeout_s=read_timeout_s
	)


def patch_session_policy(agent_url: str, session_key: str, patch: dict) -> dict:
	"""Set the inherited tool policy of a session through the plugin."""
	extra = set(patch) - PATCH_ALLOWED_FIELDS
	if extra:
		raise PluginRouteError("Unsupported session patch field.")
	return post_session_action(
		agent_url, SESSIONS_PATCH_PATH, session_key, {"key": session_key, "patch": patch}
	)
