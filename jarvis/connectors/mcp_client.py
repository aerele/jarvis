"""Synchronous MCP Streamable-HTTP client (JSON-RPC over HTTP POST), dual era.

TRANSPORT DECISION (settled - see MCP_CONNECTORS_PLAN.md P1 and the design
memory). This is a HAND-ROLLED synchronous client built on the same
requests/urllib3 stack link_fetch already uses, NOT the official ``mcp`` Python
SDK. Reasons, in order:

  1. The whole point of the connector security model is the SSRF guard PINNING
     the socket to a pre-vetted IP while TLS still verifies the original
     hostname. That requires driving a urllib3 connection pool whose host IS the
     IP (see ``ssrf._open_pinned``). The ``mcp`` SDK is anyio/httpx and offers no
     seam to substitute the resolved address, so the pin - the load-bearing
     defense - cannot be enforced through it.
  2. This code runs INSIDE a synchronous gunicorn request handler (the
     ``call_tool`` web worker). The SDK is async; bolting an event loop into a
     sync Frappe request to reach it is pure liability.
  3. The SDK is not installed in the bench venv and we must not add it there
     (its pydantic/anyio/httpx pins risk colliding with frappe/erpnext). requests
     + urllib3 + certifi are already dependencies.

TWO ERAS (spec revision 2026-07-28, basic/versioning + streamable-http):

  * LEGACY (2024-11-05 .. 2025-11-25): ``initialize`` handshake, then the
    ``notifications/initialized`` notification, an optional ``Mcp-Session-Id``
    echoed on every later request, ``MCP-Protocol-Version`` carrying the
    negotiated version after initialize, DELETE to tear the session down, and a
    404 on a session id meaning "expired, start over".
  * MODERN (2026-07-28 and later): no handshake at all. Every request carries
    ``params._meta`` with the protocol version, client info and client
    capabilities, mirrored into the ``MCP-Protocol-Version``, ``Mcp-Method`` and
    (for tools/call) ``Mcp-Name`` headers, plus ``Mcp-Param-*`` for any
    ``x-mcp-header`` parameter. No session id, no DELETE.

  Era DETECTION runs only when the caller does not know it (the Settings Test
  button): a modern ``server/discover`` goes first; a 2xx or a recognised modern
  JSON-RPC error on a 4xx means modern, any other 400/404/405 means legacy and
  the client falls back to ``initialize``. The broker passes the era it stored at
  Test time, so a chat-turn call never pays the probe.

Both eras: every client->server message is a fresh HTTP POST carrying
``Accept: application/json, text/event-stream``; the answer is a JSON object or
an SSE stream carrying the response as a ``data:`` frame; a notification POST
yields 202 with no body.

No frappe import here on purpose - see the package docstring. Pure wire helpers
live in ``mcp_wire`` so both modules stay unit-testable without a bench.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from jarvis.connectors import mcp_wire, ssrf
from jarvis.connectors.mcp_wire import (
	iter_sse_messages,
)
from jarvis.connectors.mcp_wire import (
	matches_response as _matches_response,
)

MODERN_PROTOCOL_VERSION = mcp_wire.MODERN_PROTOCOL_VERSION
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
# Legacy versions this client knows how to speak. If a server negotiates
# something outside this set we disconnect (spec: client SHOULD disconnect).
_ACCEPTED_PROTOCOL_VERSIONS = frozenset({"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"})

ERA_MODERN = "modern"
ERA_LEGACY = "legacy"

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_TOTAL_TIMEOUT = 20.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
# A 4xx body is read only to recognise a modern JSON-RPC error; keep it small.
_ERROR_BODY_MAX = 64 * 1024

# JSON-RPC / transport error kinds carried on McpError.kind.
ERR_TRANSPORT = "transport"  # connect/timeout/malformed - breaker SHOULD count these
ERR_PROTOCOL = "protocol"  # unsupported version, bad handshake shape
ERR_RPC = "rpc"  # server returned a JSON-RPC error object
ERR_SESSION_EXPIRED = "session_expired"  # 404 to a request carrying a session id
ERR_HTTP = "http"  # a non-2xx we could not otherwise classify


class McpError(Exception):
	def __init__(
		self, message: str, *, kind: str = ERR_TRANSPORT, code: int | None = None, rpc: dict | None = None
	):
		super().__init__(message)
		self.kind = kind
		self.code = code
		#: The JSON-RPC error object behind an ERR_HTTP/ERR_RPC, when there was one.
		self.rpc = rpc


def era_for_version(protocol_version: str | None) -> str | None:
	"""``modern`` / ``legacy`` for a stored protocol version, ``None`` when unknown."""
	if not protocol_version:
		return None
	return ERA_MODERN if protocol_version >= MODERN_PROTOCOL_VERSION else ERA_LEGACY


def _probe_says_legacy(exc: McpError) -> bool:
	"""A legacy server answers the modern ``server/discover`` probe with a 400/404/405
	carrying no recognised modern JSON-RPC error (spec: Backward Compatibility), or,
	in the wild, with HTTP 200 and a JSON-RPC error such as "not initialized" or
	"method not found". Modern servers MUST implement ``server/discover``, so on
	this probe only the version/header/capability errors prove a modern server."""
	if exc.kind == ERR_RPC:
		return not mcp_wire.is_modern_probe_error(exc.rpc)
	return (
		exc.kind == ERR_HTTP and exc.code in (400, 404, 405) and not mcp_wire.is_modern_probe_error(exc.rpc)
	)


def _rearm_socket(resp, remaining: float) -> None:
	"""Point the connection's socket timeout at what is LEFT of the budget before
	each body read. urllib3 arms the socket once, before the headers, and never
	re-evaluates the total for the body, so without this a single recv could wait
	the whole original read timeout after the deadline has already passed."""
	sock = getattr(getattr(resp, "connection", None), "sock", None)
	if sock is not None:
		try:
			sock.settimeout(max(remaining, 0.05))
		except Exception:
			pass


class McpClient:
	"""One MCP conversation over Streamable HTTP. Construct, ``connect()`` (or
	``initialize()`` for a known-legacy server), then ``list_tools()`` /
	``call_tool()``; ``close()`` (or use as a context manager) best-effort DELETEs
	a legacy session. A single total-time budget spans the whole conversation -
	every round trip draws down the same deadline so a slow-drip server cannot
	beat the cap one recv at a time."""

	def __init__(
		self,
		base_url: str,
		token: str | None = None,
		*,
		protocol_version: str | None = None,
		connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
		total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
		max_bytes: int = DEFAULT_MAX_BYTES,
		egress_allowed: Callable[[str], bool] | None = None,
		client_name: str = "jarvis-connector",
		client_version: str = "1.0",
		clock: Callable[[], float] = time.monotonic,
	):
		self.base_url = base_url
		self._token = token
		# The version to OFFER: a known legacy version keeps the shipped
		# behaviour; None means "detect" (connect() probes modern first).
		self._protocol_version = protocol_version
		self._connect_timeout = connect_timeout
		self._total_timeout = total_timeout
		self._max_bytes = max_bytes
		self._egress_allowed = egress_allowed
		self._client_name = client_name
		self._client_version = client_version
		self._clock = clock

		self._deadline = None  # set on the first request
		self._session_id = None
		self._next_id = 0
		#: Filled by connect()/initialize(): which era the server speaks and the
		#: version every request carries from then on.
		self.era: str | None = era_for_version(protocol_version)
		self.negotiated_version: str | None = protocol_version if self.era == ERA_MODERN else None
		self.server_capabilities: dict = {}
		#: Smallest ``ttlMs`` seen across tools/list pages (modern servers only).
		self.tools_ttl_ms: int | None = None

	# -- budget ----------------------------------------------------------- #
	def _remaining(self) -> float:
		if self._deadline is None:
			self._deadline = self._clock() + self._total_timeout
		left = self._deadline - self._clock()
		if left <= 0:
			raise McpError("Connector call exceeded its time budget.", kind=ERR_TRANSPORT)
		return left

	# -- lifecycle -------------------------------------------------------- #
	def connect(self) -> dict:
		"""Establish which era the server speaks and get ready for requests.
		Known legacy -> ``initialize``. Known modern -> nothing to send. Unknown ->
		a modern ``server/discover`` probe, falling back to ``initialize`` when the
		server turns out to be legacy (spec: Backward Compatibility)."""
		if self.era == ERA_LEGACY:
			return self.initialize()
		if self.era == ERA_MODERN:
			return {}
		return self._detect_era()

	def _detect_era(self) -> dict:
		self.era = ERA_MODERN
		self.negotiated_version = MODERN_PROTOCOL_VERSION
		try:
			result = self._unwrap(self._post("server/discover", {}, is_request=True))
		except McpError as exc:
			return self._fall_back_from(exc)
		versions = result.get("supportedVersions") or []
		if not versions:
			# A legacy server that answers an unknown method with an empty result
			# instead of an error: not modern, fall back to the handshake.
			return self._go_legacy(DEFAULT_PROTOCOL_VERSION)
		if MODERN_PROTOCOL_VERSION not in versions:
			raise McpError(
				f"Connector supports no MCP protocol version this client speaks ({versions!r}).",
				kind=ERR_PROTOCOL,
			)
		self.server_capabilities = result.get("capabilities") or {}
		return result

	def _fall_back_from(self, exc: McpError) -> dict:
		"""Decide legacy vs modern from the probe's failure (spec: a 4xx carrying a
		recognised modern JSON-RPC error is a modern server; any other 400/404/405
		is legacy). A modern server that lists only legacy versions we speak gets
		the legacy handshake with the newest of them."""
		supported = mcp_wire.supported_versions(exc.rpc)
		if not supported and _probe_says_legacy(exc):
			return self._go_legacy(DEFAULT_PROTOCOL_VERSION)
		if supported:
			if MODERN_PROTOCOL_VERSION in supported:
				raise McpError("Connector rejected the protocol version it advertises.", kind=ERR_PROTOCOL)
			legacy = mcp_wire.pick_legacy_version(supported, _ACCEPTED_PROTOCOL_VERSIONS)
			if legacy:
				return self._go_legacy(legacy)
			raise McpError(
				f"Connector supports no MCP protocol version this client speaks ({supported!r}).",
				kind=ERR_PROTOCOL,
			)
		raise exc

	def _go_legacy(self, version: str) -> dict:
		self.era = ERA_LEGACY
		self.negotiated_version = None
		self._protocol_version = version
		return self.initialize()

	def initialize(self) -> dict:
		"""The legacy handshake. Sends ``initialize`` then ``notifications/initialized``."""
		self.era = ERA_LEGACY
		params = {
			"protocolVersion": self._protocol_version or DEFAULT_PROTOCOL_VERSION,
			# We serve none of roots/sampling/elicitation, so declare nothing.
			"capabilities": {},
			"clientInfo": {"name": self._client_name, "version": self._client_version},
		}
		result = self._unwrap(self._post("initialize", params, is_request=True, is_initialize=True))
		negotiated = result.get("protocolVersion")
		if negotiated not in _ACCEPTED_PROTOCOL_VERSIONS:
			raise McpError(
				f"Connector negotiated an unsupported MCP protocol version ({negotiated!r}).",
				kind=ERR_PROTOCOL,
			)
		self.negotiated_version = negotiated
		self.server_capabilities = result.get("capabilities") or {}
		# Announce readiness before any normal operation (spec requirement).
		self._post("notifications/initialized", None, is_request=False)
		return result

	def list_tools(self, *, max_pages: int = 20) -> list[dict]:
		"""Return the full tool list, following ``nextCursor`` pagination up to a
		hop cap. Each tool dict carries at least ``name``, ``description`` and
		``inputSchema`` (a JSON Schema object) plus optional ``annotations``.
		Records the smallest ``ttlMs`` hint seen (spec: caching utility)."""
		tools: list[dict] = []
		cursor = None
		for _ in range(max_pages):
			params = {"cursor": cursor} if cursor else {}
			result = self._unwrap(self._post("tools/list", params, is_request=True))
			tools.extend(result.get("tools") or [])
			self._note_ttl(result.get("ttlMs"))
			cursor = result.get("nextCursor")
			if not cursor:
				break
		return tools

	def _note_ttl(self, ttl) -> None:
		if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
			return
		self.tools_ttl_ms = ttl if self.tools_ttl_ms is None else min(self.tools_ttl_ms, ttl)

	def call_tool(
		self, name: str, arguments: dict | None = None, *, header_params: dict | None = None
	) -> dict:
		"""Call ``name`` with ``arguments`` and return the raw ``tools/call``
		result (``content`` array + optional ``isError`` / ``structuredContent``).
		A JSON-RPC error (unknown tool, bad args) raises; a tool-execution error
		is signalled in-band by ``isError: true`` and is returned, not raised -
		the broker decides how to surface it. ``header_params`` are the
		``Mcp-Param-*`` mirrors the broker derived from the tool's ``x-mcp-header``
		annotations (modern era only)."""
		params = {"name": name, "arguments": arguments or {}}
		result = self._unwrap(self._post("tools/call", params, is_request=True, extra_headers=header_params))
		if result.get("resultType") == "input_required":
			# We declared no elicitation/sampling, so a server asking for input
			# mid-call is one we cannot serve (spec: MRTR).
			raise McpError(
				"Connector asked for interactive input this client cannot provide.", kind=ERR_PROTOCOL
			)
		return result

	def close(self) -> None:
		"""Best-effort legacy session teardown (spec: DELETE with the session id).
		Never raises; skipped if there is no session or no budget left. A modern
		conversation has no session to tear down."""
		if not self._session_id:
			return
		try:
			read_timeout = min(self._remaining(), self._total_timeout)
			resp, pool, _ = ssrf.open_pinned_request(
				self.base_url,
				method="DELETE",
				headers=self._headers(is_initialize=False),
				body=None,
				connect_timeout=self._connect_timeout,
				read_timeout=read_timeout,
				egress_allowed=self._egress_allowed,
				deadline=self._deadline,
				clock=self._clock,
			)
			try:
				resp.close()
			finally:
				pool.close()
		except Exception:
			# A server MAY answer 405 (no client teardown) or we may be out of
			# budget; either way teardown is advisory.
			pass
		finally:
			self._session_id = None

	def __enter__(self) -> McpClient:
		return self

	def __exit__(self, *exc) -> None:
		self.close()

	# -- one round trip --------------------------------------------------- #
	def _headers(self, *, is_initialize: bool, method: str | None = None, params: dict | None = None) -> dict:
		headers = {
			"Accept": "application/json, text/event-stream",
			"Content-Type": "application/json",
		}
		if self._token:
			headers["Authorization"] = f"Bearer {self._token}"
		if self.era == ERA_MODERN:
			# Modern: version + method mirrored into headers on EVERY request; the
			# server rejects a header/body mismatch with 400 (-32020).
			headers["MCP-Protocol-Version"] = self.negotiated_version or MODERN_PROTOCOL_VERSION
			if method:
				headers["Mcp-Method"] = method
			if method == "tools/call" and params and params.get("name") is not None:
				headers["Mcp-Name"] = mcp_wire.encode_header_value(params["name"])
			return headers
		if self._session_id:
			headers["Mcp-Session-Id"] = self._session_id
		# Legacy: the protocol-version header goes on every request AFTER
		# initialize, carrying the negotiated version. Omitted on initialize.
		if not is_initialize:
			headers["MCP-Protocol-Version"] = (
				self.negotiated_version or self._protocol_version or DEFAULT_PROTOCOL_VERSION
			)
		return headers

	def _modern_meta(self) -> dict:
		return {
			mcp_wire.META_PROTOCOL_VERSION: self.negotiated_version or MODERN_PROTOCOL_VERSION,
			mcp_wire.META_CLIENT_INFO: {"name": self._client_name, "version": self._client_version},
			mcp_wire.META_CLIENT_CAPABILITIES: {},
		}

	def _post(
		self,
		method: str,
		params: dict | None,
		*,
		is_request: bool,
		is_initialize: bool = False,
		extra_headers: dict | None = None,
	):
		"""One JSON-RPC POST. For a request (``is_request``) returns the parsed
		JSON-RPC message dict (with ``result`` or ``error``); for a notification
		returns ``None`` (expects 2xx/202, no body)."""
		message = {"jsonrpc": "2.0", "method": method}
		if self.era == ERA_MODERN:
			params = {**(params or {}), "_meta": self._modern_meta()}
		if params is not None:
			message["params"] = params
		request_id = None
		if is_request:
			self._next_id += 1
			request_id = self._next_id
			message["id"] = request_id
		body = json.dumps(message).encode("utf-8")
		headers = self._headers(is_initialize=is_initialize, method=method, params=params)
		if extra_headers and self.era == ERA_MODERN:
			headers.update(extra_headers)

		read_timeout = min(self._remaining(), self._total_timeout)
		# A guard rejection (ssrf.SsrfError) propagates as-is: the broker
		# classifies it, and only a connect failure feeds the circuit breaker.
		resp, pool, _final = ssrf.open_pinned_request(
			self.base_url,
			method="POST",
			headers=headers,
			body=body,
			connect_timeout=self._connect_timeout,
			read_timeout=read_timeout,
			egress_allowed=self._egress_allowed,
			deadline=self._deadline,
			clock=self._clock,
		)
		try:
			return self._handle_response(resp, request_id, is_request=is_request, is_initialize=is_initialize)
		finally:
			try:
				resp.close()
			finally:
				pool.close()

	def _handle_response(self, resp, request_id, *, is_request: bool, is_initialize: bool):
		status = resp.status
		if is_initialize:
			sid = resp.headers.get("Mcp-Session-Id")
			if sid:
				self._session_id = sid

		if status == 404 and self._session_id:
			# The session is gone; drop it so a later close() does not DELETE a
			# dead id, and so a retry starts a genuinely fresh session.
			self._session_id = None
			raise McpError("MCP session expired.", kind=ERR_SESSION_EXPIRED)
		if not is_request:
			if 200 <= status < 300:
				return None
			raise McpError(f"Notification rejected (HTTP {status}).", kind=ERR_HTTP, code=status)
		if status < 200 or status >= 300:
			raise self._http_error(resp, status)

		content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
		if content_type == "text/event-stream":
			return self._stream_for_response(resp, request_id)
		raw = self._read_capped(resp)
		try:
			msg = json.loads(raw.decode("utf-8"))
		except (ValueError, UnicodeDecodeError) as exc:
			raise McpError("Connector returned a non-JSON body.", kind=ERR_TRANSPORT) from exc
		if not _matches_response(msg, request_id):
			raise McpError("Connector response did not match the request id.", kind=ERR_TRANSPORT)
		return msg

	def _http_error(self, resp, status: int) -> McpError:
		"""``code`` carries the HTTP status so the broker can count 5xx toward the
		circuit breaker but leave 4xx (auth/bad request) out of it. A JSON 4xx body
		is read (capped) so era detection can recognise a modern JSON-RPC error."""
		rpc = None
		if 400 <= status < 500:
			content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
			if content_type == "application/json":
				try:
					rpc = mcp_wire.parse_rpc_error(self._read_capped(resp, limit=_ERROR_BODY_MAX))
				except McpError:
					rpc = None
		return McpError(f"Connector returned HTTP {status}.", kind=ERR_HTTP, code=status, rpc=rpc)

	@staticmethod
	def _unwrap(msg: dict) -> dict:
		"""Return ``result`` or raise on a JSON-RPC ``error`` object."""
		if "error" in msg:
			err = msg.get("error") or {}
			raise McpError(
				str(err.get("message") or "Connector returned a JSON-RPC error."),
				kind=ERR_RPC,
				code=err.get("code"),
				rpc=err if isinstance(err, dict) else None,
			)
		return msg.get("result") or {}

	# -- body reading ----------------------------------------------------- #
	def _iter_body(self, resp, limit: int | None = None):
		"""Yield the response body ONE syscall at a time, re-arming the socket to the
		remaining budget before each read and re-checking the deadline after it, so a
		slow-drip server cannot loop recvs past the time budget or the size cap."""
		read1 = getattr(resp, "read1", None)
		total = 0
		cap = limit or self._max_bytes

		def _reads():
			if read1 is None:
				yield from resp.stream(8192, decode_content=True)
				return
			while True:
				_rearm_socket(resp, self._remaining())
				chunk = read1(65536, decode_content=True)
				if not chunk:
					return
				yield chunk

		for chunk in _reads():
			self._remaining()
			if not chunk:
				continue
			total += len(chunk)
			if total > cap:
				raise McpError("Connector response exceeded the size cap.", kind=ERR_TRANSPORT)
			yield chunk

	def _read_capped(self, resp, limit: int | None = None) -> bytes:
		return b"".join(self._iter_body(resp, limit))

	def _stream_for_response(self, resp, request_id) -> dict:
		"""Read an SSE stream incrementally and return the JSON-RPC response to
		``request_id`` the instant it arrives, without draining the rest of the
		stream. Non-matching frames (notifications, keep-alives) are skipped."""
		buf = b""
		for chunk in self._iter_body(resp):
			buf += chunk
			while b"\n\n" in buf or b"\r\n\r\n" in buf:
				sep = (
					b"\r\n\r\n"
					if (
						b"\r\n\r\n" in buf
						and (b"\n\n" not in buf or buf.index(b"\r\n\r\n") < buf.index(b"\n\n"))
					)
					else b"\n\n"
				)
				event, buf = buf.split(sep, 1)
				for msg in iter_sse_messages(event + sep):
					if _matches_response(msg, request_id):
						return msg
		for msg in iter_sse_messages(buf):
			if _matches_response(msg, request_id):
				return msg
		raise McpError("SSE stream closed before the response arrived.", kind=ERR_TRANSPORT)


# --------------------------------------------------------------------------- #
# convenience: one full conversation, with a single retry on an expired session
# --------------------------------------------------------------------------- #
def _run_session(base_url, token, op, *, _retried=False, _deadline=None, **client_kw):
	"""Run one MCP conversation, with a single retry on an expired legacy
	session (404). A single absolute deadline spans BOTH attempts so the retry
	can never add a second full timeout on top of the first."""
	clock = client_kw.get("clock", time.monotonic)
	now = clock()
	if _deadline is None:
		_deadline = now + client_kw.get("total_timeout", DEFAULT_TOTAL_TIMEOUT)
	remaining = _deadline - now
	if remaining <= 0:
		raise McpError("Connector call exceeded its time budget.", kind=ERR_TRANSPORT)

	client = McpClient(base_url, token, **{**client_kw, "total_timeout": remaining})
	try:
		client.connect()
		return op(client)
	except McpError as exc:
		if exc.kind == ERR_SESSION_EXPIRED and not _retried and (_deadline - clock()) > 0:
			return _run_session(base_url, token, op, _retried=True, _deadline=_deadline, **client_kw)
		raise
	finally:
		client.close()


def fetch_tools(base_url: str, token: str | None = None, **client_kw) -> list[dict]:
	"""connect + tools/list, one conversation. Returns the tool list only; see
	:func:`probe` for the era and cache hints alongside it."""
	return probe(base_url, token, **client_kw)["tools"]


def probe(base_url: str, token: str | None = None, **client_kw) -> dict:
	"""connect (detecting the era unless ``protocol_version`` is given) +
	tools/list, one conversation. Returns ``{tools, era, protocol_version,
	tools_ttl_ms, capabilities}`` - what the Settings Test button stores."""

	def _op(client: McpClient) -> dict:
		tools = client.list_tools()
		return {
			"tools": tools,
			"era": client.era,
			"protocol_version": client.negotiated_version,
			"tools_ttl_ms": client.tools_ttl_ms,
			"capabilities": client.server_capabilities,
		}

	return _run_session(base_url, token, _op, **client_kw)


def run_tool(
	base_url: str,
	token: str | None,
	name: str,
	arguments: dict | None = None,
	*,
	header_params: dict | None = None,
	**client_kw,
) -> dict:
	"""connect + tools/call, one conversation. Pass ``protocol_version`` (the
	value stored at Test time) to skip era detection."""
	return _run_session(
		base_url, token, lambda c: c.call_tool(name, arguments, header_params=header_params), **client_kw
	)
