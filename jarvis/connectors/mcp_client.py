"""Synchronous MCP Streamable-HTTP client (JSON-RPC over HTTP POST).

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

So "built on requests" here means the requests/urllib3 sync stack, reusing the
proven pin-to-IP seam in ``jarvis.connectors.ssrf`` - not ``requests.Session``
itself, which cannot pin the socket without a custom adapter.

SPEC. Validated 2026-09-04 against modelcontextprotocol.io, Streamable HTTP
transport + lifecycle + tools pages, protocol revision ``2025-06-18``:

  * Every client->server JSON-RPC message is a fresh HTTP POST to the one MCP
    endpoint. The POST MUST carry ``Accept: application/json, text/event-stream``
    (the server MAY answer a request with EITHER a single JSON object OR an SSE
    stream, and the client MUST support both).
  * A request POST yields the JSON-RPC response either as an
    ``application/json`` body or as an SSE stream that eventually carries a
    ``data:`` frame holding the response. A notification/response POST yields
    ``202 Accepted`` with no body.
  * ``initialize`` sends ``protocolVersion``, ``capabilities`` and
    ``clientInfo``; the server echoes a (possibly different) negotiated
    ``protocolVersion`` and MAY assign an ``Mcp-Session-Id`` response header. The
    client MUST echo that session id on every subsequent request, and MUST send
    ``MCP-Protocol-Version: <negotiated>`` on every request AFTER initialize
    (it is omitted on initialize itself - there is no negotiated version yet).
  * After a successful ``initialize`` the client MUST send the
    ``notifications/initialized`` notification before normal operations.
  * A 404 to a request carrying a session id means the session expired; the
    client must start a fresh session.

No frappe import here on purpose - see the package docstring. This module is
pure enough to unit-test (SSE parsing, header wiring, deadline math) with the
``ssrf`` seam mocked and no bench.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from jarvis.connectors import ssrf

DEFAULT_PROTOCOL_VERSION = "2025-06-18"
# Versions this client knows how to speak. If a server negotiates something
# outside this set we disconnect (spec: client SHOULD disconnect).
_ACCEPTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18", "2025-03-26", "2024-11-05"})

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_TOTAL_TIMEOUT = 20.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024

# JSON-RPC / transport error kinds carried on McpError.kind.
ERR_TRANSPORT = "transport"  # connect/timeout/malformed - breaker SHOULD count these
ERR_PROTOCOL = "protocol"  # unsupported version, bad handshake shape
ERR_RPC = "rpc"  # server returned a JSON-RPC error object
ERR_SESSION_EXPIRED = "session_expired"  # 404 to a request carrying a session id
ERR_HTTP = "http"  # a non-2xx we could not otherwise classify


class McpError(Exception):
	def __init__(self, message: str, *, kind: str = ERR_TRANSPORT, code: int | None = None):
		super().__init__(message)
		self.kind = kind
		self.code = code


# --------------------------------------------------------------------------- #
# SSE frame parsing (pure - unit-tested directly)
# --------------------------------------------------------------------------- #
def iter_sse_messages(raw: bytes):
	"""Yield the parsed JSON value of each complete SSE ``data:`` event in
	``raw``. Events are separated by a blank line; a ``data:`` field may span
	several lines (joined with ``\\n``); ``:`` comment lines and other fields
	(``event:``, ``id:``, ``retry:``) are ignored. A ``data`` payload that is not
	valid JSON is skipped (a keep-alive or non-JSON-RPC event), never raised on.

	Split out as a free function so it is exercised without any socket."""
	text = raw.decode("utf-8", errors="replace")
	# Normalize CRLF/CR to LF, then split on a blank line (event boundary).
	text = text.replace("\r\n", "\n").replace("\r", "\n")
	for block in text.split("\n\n"):
		data_lines = []
		for line in block.split("\n"):
			if not line or line.startswith(":"):
				continue
			field, _, value = line.partition(":")
			if field != "data":
				continue
			# A single leading space after the colon is stripped per the SSE spec.
			data_lines.append(value[1:] if value.startswith(" ") else value)
		if not data_lines:
			continue
		payload = "\n".join(data_lines).strip()
		if not payload:
			continue
		try:
			yield json.loads(payload)
		except (ValueError, TypeError):
			continue


def _matches_response(msg, request_id) -> bool:
	"""True when ``msg`` is the JSON-RPC response to our request: same ``id`` and
	carrying either ``result`` or ``error``. Server-to-client requests (they have
	an ``id`` AND a ``method``) and notifications (no ``id``) are NOT our
	response and must be skipped."""
	if not isinstance(msg, dict):
		return False
	if msg.get("method") is not None:
		return False
	if msg.get("id") != request_id:
		return False
	return "result" in msg or "error" in msg


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class McpClient:
	"""One MCP session over Streamable HTTP. Construct, ``initialize()``, then
	``list_tools()`` / ``call_tool()``; ``close()`` (or use as a context manager)
	best-effort DELETEs the session. A single total-time budget spans the whole
	session - every round trip draws down the same deadline so a slow-drip server
	cannot beat the cap one recv at a time."""

	def __init__(
		self,
		base_url: str,
		token: str | None = None,
		*,
		protocol_version: str = DEFAULT_PROTOCOL_VERSION,
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
		self._negotiated_version = None
		self._next_id = 0

	# -- budget ----------------------------------------------------------- #
	def _remaining(self) -> float:
		if self._deadline is None:
			self._deadline = self._clock() + self._total_timeout
		left = self._deadline - self._clock()
		if left <= 0:
			raise McpError("Connector call exceeded its time budget.", kind=ERR_TRANSPORT)
		return left

	# -- one round trip --------------------------------------------------- #
	def _headers(self, *, is_initialize: bool) -> dict:
		headers = {
			"Accept": "application/json, text/event-stream",
			"Content-Type": "application/json",
		}
		if self._token:
			headers["Authorization"] = f"Bearer {self._token}"
		if self._session_id:
			headers["Mcp-Session-Id"] = self._session_id
		# The protocol-version header goes on every request AFTER initialize,
		# carrying the negotiated version. It is omitted on initialize itself.
		if not is_initialize:
			headers["MCP-Protocol-Version"] = self._negotiated_version or self._protocol_version
		return headers

	def _read_capped(self, resp) -> bytes:
		chunks: list[bytes] = []
		total = 0
		for chunk in resp.stream(8192, decode_content=True):
			# Re-check the deadline between recvs so a slow trickle still trips it.
			self._remaining()
			if not chunk:
				continue
			total += len(chunk)
			if total > self._max_bytes:
				raise McpError("Connector response exceeded the size cap.", kind=ERR_TRANSPORT)
			chunks.append(chunk)
		return b"".join(chunks)

	def _stream_for_response(self, resp, request_id) -> dict:
		"""Read an SSE stream incrementally and return the JSON-RPC response to
		``request_id`` the instant it arrives, without draining the rest of the
		stream (a server MAY hold it open past the response). Non-matching frames
		(server notifications/requests, keep-alives) are skipped."""
		buf = b""
		total = 0
		for chunk in resp.stream(8192, decode_content=True):
			self._remaining()
			if not chunk:
				continue
			total += len(chunk)
			if total > self._max_bytes:
				raise McpError("Connector response exceeded the size cap.", kind=ERR_TRANSPORT)
			buf += chunk
			# Only whole events (terminated by a blank line) are safe to parse; a
			# partial trailing event stays in the buffer for the next chunk.
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
		# Stream ended (EOF) - try whatever is left, then give up.
		for msg in iter_sse_messages(buf):
			if _matches_response(msg, request_id):
				return msg
		raise McpError("SSE stream closed before the response arrived.", kind=ERR_TRANSPORT)

	def _post(self, method: str, params: dict | None, *, is_request: bool, is_initialize: bool = False):
		"""One JSON-RPC POST. For a request (``is_request``) returns the parsed
		JSON-RPC message dict (with ``result`` or ``error``); for a notification
		returns ``None`` (expects 2xx/202, no body)."""
		message = {"jsonrpc": "2.0", "method": method}
		if params is not None:
			message["params"] = params
		request_id = None
		if is_request:
			self._next_id += 1
			request_id = self._next_id
			message["id"] = request_id
		body = json.dumps(message).encode("utf-8")

		read_timeout = min(self._remaining(), self._total_timeout)
		try:
			resp, pool, _final = ssrf.open_pinned_request(
				self.base_url,
				method="POST",
				headers=self._headers(is_initialize=is_initialize),
				body=body,
				connect_timeout=self._connect_timeout,
				read_timeout=read_timeout,
				egress_allowed=self._egress_allowed,
			)
		except ssrf.SsrfError:
			# A guard rejection is not a transport flake - let the broker classify
			# it (it must NOT feed the circuit breaker). Re-raised as-is.
			raise
		try:
			return self._handle_response(resp, request_id, is_request=is_request, is_initialize=is_initialize)
		finally:
			try:
				resp.close()
			finally:
				pool.close()

	def _handle_response(self, resp, request_id, *, is_request: bool, is_initialize: bool):
		status = resp.status
		# Capture the session id the server assigns on the initialize response.
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
			# Notification/response: 202 Accepted (or any 2xx) with no body.
			if 200 <= status < 300:
				return None
			raise McpError(f"Notification rejected (HTTP {status}).", kind=ERR_HTTP, code=status)
		if status < 200 or status >= 300:
			# ``code`` carries the HTTP status so the broker can count 5xx toward
			# the circuit breaker but leave 4xx (auth/bad request) out of it.
			raise McpError(f"Connector returned HTTP {status}.", kind=ERR_HTTP, code=status)

		content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
		if content_type == "text/event-stream":
			return self._stream_for_response(resp, request_id)
		# Default to a single JSON object (application/json, or a server that
		# omitted the header).
		raw = self._read_capped(resp)
		try:
			msg = json.loads(raw.decode("utf-8"))
		except (ValueError, UnicodeDecodeError) as exc:
			raise McpError("Connector returned a non-JSON body.", kind=ERR_TRANSPORT) from exc
		if not _matches_response(msg, request_id):
			raise McpError("Connector response did not match the request id.", kind=ERR_TRANSPORT)
		return msg

	@staticmethod
	def _unwrap(msg: dict) -> dict:
		"""Return ``result`` or raise on a JSON-RPC ``error`` object."""
		if "error" in msg:
			err = msg.get("error") or {}
			raise McpError(
				str(err.get("message") or "Connector returned a JSON-RPC error."),
				kind=ERR_RPC,
				code=err.get("code"),
			)
		return msg.get("result") or {}

	# -- lifecycle -------------------------------------------------------- #
	def initialize(self) -> dict:
		params = {
			"protocolVersion": self._protocol_version,
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
		self._negotiated_version = negotiated
		# Announce readiness before any normal operation (spec requirement).
		self._post("notifications/initialized", None, is_request=False)
		return result

	def list_tools(self, *, max_pages: int = 20) -> list[dict]:
		"""Return the full tool list, following ``nextCursor`` pagination up to a
		hop cap. Each tool dict carries at least ``name``, ``description`` and
		``inputSchema`` (a JSON Schema object) plus optional ``annotations``."""
		tools: list[dict] = []
		cursor = None
		for _ in range(max_pages):
			params = {"cursor": cursor} if cursor else {}
			result = self._unwrap(self._post("tools/list", params, is_request=True))
			tools.extend(result.get("tools") or [])
			cursor = result.get("nextCursor")
			if not cursor:
				break
		return tools

	def call_tool(self, name: str, arguments: dict | None = None) -> dict:
		"""Call ``name`` with ``arguments`` and return the raw ``tools/call``
		result (``content`` array + optional ``isError`` / ``structuredContent``).
		A JSON-RPC error (unknown tool, bad args) raises; a tool-execution error
		is signalled in-band by ``isError: true`` and is returned, not raised -
		the broker decides how to surface it."""
		params = {"name": name, "arguments": arguments or {}}
		return self._unwrap(self._post("tools/call", params, is_request=True))

	def close(self) -> None:
		"""Best-effort session teardown (spec: DELETE with the session id). Never
		raises; skipped if there is no session or no budget left."""
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


# --------------------------------------------------------------------------- #
# convenience: one full session, with a single retry on an expired session
# --------------------------------------------------------------------------- #
def _run_session(base_url, token, op, *, _retried=False, **client_kw):
	client = McpClient(base_url, token, **client_kw)
	try:
		client.initialize()
		return op(client)
	except McpError as exc:
		if exc.kind == ERR_SESSION_EXPIRED and not _retried:
			# Fresh session, one retry (spec: 404 => start a new session).
			return _run_session(base_url, token, op, _retried=True, **client_kw)
		raise
	finally:
		client.close()


def fetch_tools(base_url: str, token: str | None = None, **client_kw) -> list[dict]:
	"""initialize + notifications/initialized + tools/list, one session. Used by
	the SPA test button and to fill ``tools_cache``."""
	return _run_session(base_url, token, lambda c: c.list_tools(), **client_kw)


def run_tool(base_url: str, token: str | None, name: str, arguments: dict | None = None, **client_kw) -> dict:
	"""initialize + notifications/initialized + tools/call, one session."""
	return _run_session(base_url, token, lambda c: c.call_tool(name, arguments), **client_kw)
