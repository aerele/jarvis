"""Connector broker - the orchestrator the connector tools call.

The agent container hands the bench a (connector_key, action, args) triple over
``call_tool``; by the time this runs, ``jarvis.api._dispatch_from_session`` has
already ``impersonate``-d the real end user, so ``frappe.session.user`` IS that
user and Frappe row permissions apply beneath everything here. This module:

  1. Resolves the ``Jarvis Connector`` row for the user (a PERSONAL row wins over
     a SHARED one of the same key), honouring ``enabled``.
  2. Reads the encrypted credential.
  3. Gates the action against the row's ``Jarvis Connector Action`` children
     (deny unknown; ``allowed`` wins; else auto-allow a read-only, non-
     destructive action; else deny).
  4. Validates the arguments against the tool's cached ``inputSchema``.
  5. Runs the outbound MCP call under a per-connector circuit breaker and a
     per-(tenant, connector) concurrency cap, through the SSRF-guarded, IP-pinned
     client with a hard sub-30s time budget.
  6. Writes a ``Jarvis Connector Log`` audit row (args summary truncated +
     redacted) and returns a structured ``{ok: False, error: {code, message}}``
     on ANY failure - it never raises into the chat turn.

``tools_cache`` CONTRACT (written by the P3 ``test_connector`` SPA path, read by
step 4): a JSON object ``{"tools": [ {"name", "description", "inputSchema",
"annotations"?}, ... ]}``. A bare JSON array of the same tool objects is also
accepted defensively. ``inputSchema`` is the tool's MCP JSON Schema.

This is the ONLY module under ``jarvis.connectors`` that imports frappe.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import frappe

from jarvis.connectors import mcp_client, oauth, policy, ssrf
from jarvis.connectors.limits import AtCapacityError, CircuitBreaker, ConcurrencyCap
from jarvis.jarvis.doctype.jarvis_connector_log.jarvis_connector_log import log_call

CONNECTOR_DOCTYPE = "Jarvis Connector"
SETTINGS_DOCTYPE = "Jarvis Settings"

# Time budget for one outbound MCP session - kept strictly under the plugin's 30s
# AbortController so the agent never sees a transport timeout it would classify
# differently from a clean ``{ok: false}`` tool error (see MCP_CONNECTORS_PLAN.md).
CONNECT_TIMEOUT_S = 5.0
TOTAL_TIMEOUT_S = 20.0

# Circuit breaker + concurrency tunables.
CB_THRESHOLD = 5
CB_WINDOW_S = 60
CB_OPEN_S = 30
CC_LIMIT = 4
CC_TTL_S = int(TOTAL_TIMEOUT_S) + 20  # comfortably longer than one call

_ARGS_SUMMARY_MAX = 500
_MESSAGE_MAX = 1000

# Keys whose VALUES must never reach the audit log - a connector's own args can
# carry a per-call secret (a one-off token, a password field).
_REDACT_KEY_RE = re.compile(r"token|secret|password|authorization|credential|key|bearer", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# structured error
# --------------------------------------------------------------------------- #
class _BrokerError(Exception):
	"""Carries a stable ``code`` + human ``message`` for the ``{ok: False}``
	envelope. Codes are part of the tool contract the plugin relays to the
	model, so keep them stable."""

	def __init__(self, code: str, message: str):
		super().__init__(message)
		self.code = code
		self.message = message

	def as_dict(self) -> dict:
		return {"ok": False, "error": {"code": self.code, "message": self.message}}


# --------------------------------------------------------------------------- #
# egress policy hook - operator allow/deny for connector hosts
# --------------------------------------------------------------------------- #
#: An optional in-process override for tests / bespoke wiring: set
#: ``broker.egress_hook = fn`` (``fn(host: str) -> bool``). When ``None`` (the
#: default) the policy is read from ``Jarvis Settings.connector_egress_rules``.
egress_hook: Callable[[str], bool] | None = None


def _egress_allowed(host: str) -> bool:
	"""Consult the operator's egress policy for ``host`` before any outbound
	connection. An empty/unset policy allows the host (allow-all is the design
	default; the SSRF guard still blocks private/metadata addresses regardless),
	but a READ ERROR now fails CLOSED rather than open: we cannot tell an
	unconfigured (allow-all) field from a configured allow/deny list when the read
	itself fails, and a configured egress policy must never be silently bypassed by
	a transient error. A hard read error here also means the DB is unreachable, so
	the surrounding broker call (row resolution, credential decrypt) is failing
	anyway - denying egress is the safe outcome, not a regression.

	POLICY GRAMMAR (``Jarvis Settings.connector_egress_rules``, one rule per line;
	blank lines and ``#`` comments ignored). A rule is a host glob (``fnmatch``);
	a bare domain also matches its subdomains. A line beginning with ``!`` is a
	DENY rule, otherwise it is an ALLOW rule:

	  * any DENY rule matching the host        -> deny
	  * ALLOW rules exist but none match       -> deny (allow-list mode)
	  * no ALLOW rules (deny-list or empty)    -> allow

	NOTE: this grammar is the broker's interpretation of the free-text field; the
	admin/data-model layer owns the field and should confirm it (open question in
	the P1 report)."""
	if egress_hook is not None:
		try:
			return bool(egress_hook(host))
		except Exception:
			# Fail CLOSED for consistency with the settings-read path below: an egress
			# hook that errors must not be read as "allow".
			frappe.logger("jarvis.connectors").warning("connector egress hook failed", exc_info=True)
			return False
	try:
		raw = frappe.db.get_single_value(SETTINGS_DOCTYPE, "connector_egress_rules")
	except Exception:
		frappe.logger("jarvis.connectors").warning("connector egress rules read failed", exc_info=True)
		return False
	return policy.egress_match(host, raw)


# --------------------------------------------------------------------------- #
# row resolution + credential
# --------------------------------------------------------------------------- #
def _resolve_row(connector_key: str):
	"""Personal row (owned by the current user) wins over a shared row of the
	same key. Filters are EXPLICIT (never trust perms alone), and the row's own
	DocType permissions still apply because we run under the impersonated user."""
	if not connector_key:
		raise _BrokerError("connector_not_found", "No connector was named.")
	user = frappe.session.user
	personal = frappe.get_all(
		CONNECTOR_DOCTYPE,
		filters={"key": connector_key, "scope": "Personal", "owner": user},
		pluck="name",
		limit=1,
	)
	if personal:
		return frappe.get_doc(CONNECTOR_DOCTYPE, personal[0])
	shared = frappe.get_all(
		CONNECTOR_DOCTYPE,
		filters={"key": connector_key, "scope": "Shared"},
		pluck="name",
		limit=1,
	)
	if shared:
		return frappe.get_doc(CONNECTOR_DOCTYPE, shared[0])
	raise _BrokerError("connector_not_found", f"No connector named {connector_key!r} is available to you.")


def resolve_for_status(connector_key: str):
	"""The same row :func:`_resolve_row` would use (Personal wins over Shared),
	for a caller that needs to inspect a connector's readiness BEFORE deciding
	whether to call :func:`call` at all - ``call_connector`` uses this to
	fast-fail with a ``connector_not_ready`` error when a connector has never
	passed a connection test (or lost that pass on a credential/URL edit -
	the SPA's ``update_connector`` clears ``last_test_status``/``tools_cache``
	on either), instead of a less legible failure surfacing deeper in the
	broker or the MCP client.

	Returns the resolved row, or ``None`` when it cannot be resolved (unknown
	key, or none visible to the caller) - the caller should fall through to
	:func:`call` in that case so the ORDINARY ``connector_not_found`` error
	(with its own wording) is what the model sees, not a second one invented
	here. Never raises."""
	try:
		return _resolve_row(connector_key)
	except _BrokerError:
		return None


def _credential(row) -> str:
	"""Resolve the connector's bearer credential. An OAuth row (see
	``oauth.is_oauth``) resolves a live access token for the CURRENT
	impersonated user from its linked Connected App - no ``credential`` field
	is ever read for it - and raises ``connector_not_ready`` when the user has
	not connected (or the token could not be refreshed), rather than handing
	the MCP call a blank/broken bearer.

	Otherwise: decrypt the stored ``credential`` (the shipped API-key path,
	unchanged). On an UNSAVED row (the P3 test button tests before first save)
	``get_password`` cannot read ``__Auth`` by name, so fall back to the
	in-memory field value."""
	if oauth.is_oauth(row):
		token = oauth.resolve_access_token(row)
		if not token:
			raise _BrokerError(
				"connector_not_ready", "Connect this app in Settings before Jarvis can use it."
			)
		return token
	is_new = getattr(row, "is_new", None)
	if callable(is_new) and is_new():
		return row.get("credential") or ""
	try:
		return row.get_password("credential", raise_exception=False) or ""
	except Exception:
		return ""


# --------------------------------------------------------------------------- #
# action gate + arg validation (pure decisions live in jarvis.connectors.policy)
# --------------------------------------------------------------------------- #
def _gate(row, action: str) -> None:
	denied = policy.action_decision(row, action)
	if denied:
		raise _BrokerError(*denied)


def _validate_args(row, action: str, args) -> None:
	err = policy.argument_error(row, action, args)
	if err:
		raise _BrokerError(*err)


# --------------------------------------------------------------------------- #
# outbound call under breaker + concurrency cap
# --------------------------------------------------------------------------- #
def _store():
	return _RedisStore(frappe.cache(), frappe.local.site)


def _guard_key(row) -> str:
	# Concurrency is per (tenant, connector): the site scoping is baked into the
	# store's key prefix, so the row name alone is enough here.
	return row.name


def _execute(row, action: str, args: dict) -> dict:
	store = _store()
	breaker = CircuitBreaker(
		store, _guard_key(row), threshold=CB_THRESHOLD, window_s=CB_WINDOW_S, open_s=CB_OPEN_S
	)
	if not breaker.allow():
		raise _BrokerError(
			"circuit_open",
			"This connector is temporarily paused after repeated failures. Please try again shortly.",
		)
	cap = ConcurrencyCap(store, _guard_key(row), limit=CC_LIMIT, ttl_s=CC_TTL_S)
	credential = _credential(row)
	try:
		with cap.slot():
			return _do_call(row, action, args, credential, breaker)
	except AtCapacityError as exc:
		raise _BrokerError(
			"at_capacity",
			"This connector is handling too many requests right now. Please try again shortly.",
		) from exc


def _do_call(row, action: str, args: dict, credential: str, breaker: CircuitBreaker) -> dict:
	try:
		result = mcp_client.run_tool(
			row.base_url,
			credential or None,
			action,
			args,
			connect_timeout=CONNECT_TIMEOUT_S,
			total_timeout=TOTAL_TIMEOUT_S,
			egress_allowed=_egress_allowed,
		)
	except ssrf.SsrfError as exc:
		if exc.kind == ssrf.ERR_CONNECT_FAILED:
			# A real transport failure - count it toward the breaker.
			breaker.record_failure()
			raise _BrokerError("transport_error", "The connector could not be reached.") from exc
		# Guard rejections (blocked/unresolved address, egress policy, cross-host
		# redirect) are policy/config, NOT endpoint health - never open the circuit.
		code = "egress_denied" if exc.kind == ssrf.ERR_EGRESS_DENIED else "ssrf_blocked"
		raise _BrokerError(code, _ssrf_message(exc.kind)) from exc
	except mcp_client.McpError as exc:
		if _is_transport_failure(exc):
			breaker.record_failure()
		raise _BrokerError(_mcp_code(exc), _clean(str(exc))) from exc
	breaker.record_success()
	return result


def _is_transport_failure(exc: mcp_client.McpError) -> bool:
	"""Only genuine endpoint-health signals feed the breaker: connect/timeout/
	malformed (ERR_TRANSPORT) and 5xx. A 4xx (auth, bad request), a JSON-RPC
	error, a protocol mismatch, or an expired session say nothing about health."""
	if exc.kind == mcp_client.ERR_TRANSPORT:
		return True
	if exc.kind == mcp_client.ERR_HTTP and exc.code and exc.code >= 500:
		return True
	return False


def _mcp_code(exc: mcp_client.McpError) -> str:
	return {
		mcp_client.ERR_TRANSPORT: "transport_error",
		mcp_client.ERR_HTTP: "http_error",
		mcp_client.ERR_RPC: "rpc_error",
		mcp_client.ERR_PROTOCOL: "protocol_error",
		mcp_client.ERR_SESSION_EXPIRED: "session_expired",
	}.get(exc.kind, "transport_error")


def _ssrf_message(kind: str) -> str:
	if kind == ssrf.ERR_EGRESS_DENIED:
		return "This connector address is not permitted by your administrator's policy."
	return "This connector address is not permitted."


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def call(connector_key: str, action: str, args: dict | None = None, *, run_id: str | None = None) -> dict:
	"""Run ``action`` on ``connector_key`` with ``args`` as the current
	(impersonated) user. Returns ``{ok: True, result: <tools/call result>}`` or
	``{ok: False, error: {code, message}}``. NEVER raises into the chat turn."""
	started = time.monotonic()
	row = None
	try:
		row = _resolve_row(connector_key)
		if not row.get("enabled"):
			raise _BrokerError("connector_disabled", "This connector is turned off.")
		_gate(row, action)
		_validate_args(row, action, args)
		result = _execute(row, action, args or {})

		if isinstance(result, dict) and result.get("isError"):
			# In-band tool-execution error (spec: isError:true) - a clean tool
			# error, not a broker/transport failure.
			message = _first_text(result) or "The connector reported an error."
			_log(row, action, "tool_error", message, started, run_id, args, result)
			return {"ok": False, "error": {"code": "tool_error", "message": _clean(message)}}

		_log(row, action, "", "", started, run_id, args, result)
		return {"ok": True, "result": result}
	except _BrokerError as exc:
		if row is not None:
			_log(row, action, exc.code, exc.message, started, run_id, args, None)
		return exc.as_dict()
	except Exception as exc:  # never let anything reach the turn as a 500
		frappe.logger("jarvis.connectors").warning("connector broker unexpected error", exc_info=True)
		if row is not None:
			_log(row, action, "internal_error", str(exc), started, run_id, args, None)
		return {
			"ok": False,
			"error": {"code": "internal_error", "message": "The connector call failed unexpectedly."},
		}


# --------------------------------------------------------------------------- #
# test path (SPA test button + tools_cache fill)
# --------------------------------------------------------------------------- #
class _NullBreaker:
	"""No-op breaker for the bare :func:`test_endpoint` seam, which has no connector
	row to key a real breaker/cap on. Satisfies the record_* contract only."""

	def record_failure(self) -> None:
		pass

	def record_success(self) -> None:
		pass


def _test_probe(base_url: str, credential: str | None, breaker) -> dict:
	"""Run initialize + tools/list against ``base_url`` and return
	``{ok: True, tools: [...]}`` or ``{ok: False, error: {code, message}}``,
	feeding transport-class failures to ``breaker`` exactly as :func:`_do_call`
	does for a real call. The SPA saves nothing until this passes and writes the
	tool list to ``tools_cache``."""
	if not base_url:
		return {
			"ok": False,
			"error": {"code": "invalid_arguments", "message": "A connector URL is required."},
		}
	try:
		tools = mcp_client.fetch_tools(
			base_url,
			credential or None,
			connect_timeout=CONNECT_TIMEOUT_S,
			total_timeout=TOTAL_TIMEOUT_S,
			egress_allowed=_egress_allowed,
		)
	except ssrf.SsrfError as exc:
		if exc.kind == ssrf.ERR_CONNECT_FAILED:
			breaker.record_failure()
			return {
				"ok": False,
				"error": {"code": "transport_error", "message": "The connector could not be reached."},
			}
		code = "egress_denied" if exc.kind == ssrf.ERR_EGRESS_DENIED else "ssrf_blocked"
		return {"ok": False, "error": {"code": code, "message": _ssrf_message(exc.kind)}}
	except mcp_client.McpError as exc:
		if _is_transport_failure(exc):
			breaker.record_failure()
		return {"ok": False, "error": {"code": _mcp_code(exc), "message": _clean(str(exc))}}
	breaker.record_success()
	return {"ok": True, "tools": tools}


def test_endpoint(base_url: str, credential: str | None) -> dict:
	"""Bare, UNGUARDED initialize + tools/list probe by URL. :func:`test_connector`
	is the guarded entry point the SPA uses; this form has no connector row to key a
	breaker/cap on and so runs without them (kept for callers that only have a URL)."""
	return _test_probe(base_url, credential or None, _NullBreaker())


def test_connector(row) -> dict:
	"""Guarded row wrapper: runs the initialize + tools/list probe through the SAME
	per-connector circuit breaker and per-(tenant, connector) concurrency cap as a
	real :func:`call`, so the Test button cannot bypass the worker protection or
	hammer a flapping endpoint. Handles the unsaved-row credential read (P3 tests
	before the first save).

	The breaker/cap key is the connector row, shared with :func:`call`: 5 test-probe
	transport failures in the window open the circuit for chat calls too, which is
	the intent (the endpoint is unhealthy for either path)."""
	store = _store()
	guard_key = _guard_key(row) or (row.get("base_url") or "test")
	breaker = CircuitBreaker(store, guard_key, threshold=CB_THRESHOLD, window_s=CB_WINDOW_S, open_s=CB_OPEN_S)
	if not breaker.allow():
		return {
			"ok": False,
			"error": {
				"code": "circuit_open",
				"message": "This connector is temporarily paused after repeated failures. Please try again shortly.",
			},
		}
	cap = ConcurrencyCap(store, guard_key, limit=CC_LIMIT, ttl_s=CC_TTL_S)
	try:
		with cap.slot():
			try:
				credential = _credential(row)
			except _BrokerError as exc:
				# An OAuth row with no live token - never a health/transport signal,
				# so it does not touch the breaker (mirrors _do_call's own split
				# between auth problems and endpoint-health signals).
				return exc.as_dict()
			return _test_probe(row.get("base_url"), credential, breaker)
	except AtCapacityError:
		return {
			"ok": False,
			"error": {
				"code": "at_capacity",
				"message": "This connector is handling too many requests right now. Please try again shortly.",
			},
		}


# --------------------------------------------------------------------------- #
# audit log + helpers
# --------------------------------------------------------------------------- #
def _log(row, action, error_code, message, started, run_id, args, result) -> None:
	"""Write one audit row via the DocType's own ``log_call`` (it truncates
	defensively, swallows its own errors, and does not commit). ``error_code``
	empty means success. Args are redacted HERE - ``log_call`` only truncates, it
	does not scrub secret-shaped keys."""
	log_call(
		connector=getattr(row, "name", None) or "",
		action=action,
		status=_status_for(error_code),
		error_code=error_code or "",
		message=_clean(message or ""),
		duration_ms=int((time.monotonic() - started) * 1000),
		run_id=run_id or "",
		args_summary=_redact_args(args),
		response_bytes=_response_bytes(result),
	)


# Map a broker error code (empty == success) to the DocType's status vocabulary
# (Success / Failed / Denied / Blocked - see Jarvis Connector Log.validate).
_DENIED_CODES = frozenset({"action_denied", "action_unknown", "connector_disabled"})
_BLOCKED_CODES = frozenset({"ssrf_blocked", "egress_denied"})


def _status_for(error_code: str) -> str:
	if not error_code:
		return "Success"
	if error_code in _DENIED_CODES:
		return "Denied"
	if error_code in _BLOCKED_CODES:
		return "Blocked"
	return "Failed"


def _redact_value(value):
	"""Recursively redact secret-shaped keys and clip long strings anywhere in a
	nested structure. A top-level-only scrub let a nested token (e.g.
	``{"config": {"token": ...}}``) reach the audit table verbatim; this walks
	dicts and lists so no secret-shaped key survives at any depth."""
	if isinstance(value, dict):
		out = {}
		for key, val in value.items():
			if _REDACT_KEY_RE.search(str(key)):
				out[key] = "***"
			else:
				out[key] = _redact_value(val)
		return out
	if isinstance(value, (list, tuple)):
		return [_redact_value(v) for v in value]
	if isinstance(value, str) and len(value) > 80:
		return value[:80] + "..."
	return value


def _redact_args(args) -> str:
	"""Redact secret-shaped keys (recursively), clip long string values, then
	truncate the whole summary. Never let a per-call token land in the audit table."""
	try:
		if not isinstance(args, dict):
			return _clip(frappe.as_json(args) if args is not None else "", _ARGS_SUMMARY_MAX)
		return _clip(frappe.as_json(_redact_value(args)), _ARGS_SUMMARY_MAX)
	except Exception:
		return ""


def _response_bytes(result) -> int:
	if result is None:
		return 0
	try:
		return len(frappe.as_json(result).encode("utf-8"))
	except Exception:
		return 0


def _first_text(result: dict) -> str:
	for item in result.get("content") or []:
		if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
			return str(item["text"])
	return ""


def _clean(text: str) -> str:
	return _clip((text or "").strip(), _MESSAGE_MAX)


def _clip(text: str, length: int) -> str:
	text = text or ""
	return text if len(text) <= length else text[:length]


# --------------------------------------------------------------------------- #
# Redis store adapter for the breaker + concurrency cap
# --------------------------------------------------------------------------- #
class _RedisStore:
	"""Thin adapter over ``frappe.cache()`` implementing the ``limits`` store
	contract. Raw redis-py ``incr``/``decr``/``expire``/``get``/``set``/``delete``
	are NOT site-prefixed by RedisWrapper (only its ``*_value`` helpers are - see
	``api_errors._over_report_rate_limit``), so a multi-site bench sharing one
	Redis would collide; we prefix with the site ourselves."""

	def __init__(self, cache, site: str):
		self._c = cache
		self._prefix = f"{site}:jarvis:connector:"

	def _k(self, key: str) -> str:
		return self._prefix + key

	def incr(self, key: str) -> int:
		return int(self._c.incr(self._k(key)))

	def decr(self, key: str) -> int:
		return int(self._c.decr(self._k(key)))

	def expire(self, key: str, ttl_s: int) -> None:
		self._c.expire(self._k(key), int(ttl_s))

	def get(self, key: str):
		val = self._c.get(self._k(key))
		if val is None:
			return None
		return val.decode() if isinstance(val, bytes) else str(val)

	def set(self, key: str, value, ttl_s: int) -> None:
		self._c.set(self._k(key), str(value), ex=int(ttl_s))

	def delete(self, key: str) -> None:
		self._c.delete(self._k(key))
