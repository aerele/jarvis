"""The one seam every outbound OAuth hop goes through.

Two layers, both swappable, both share the same call shape - ``(url, *,
method, headers, body, connect_timeout, read_timeout, total_timeout,
egress_allowed) -> HttpResult``:

  * :func:`open_pinned` - the REAL transport. A thin wrapper over
    ``ssrf.open_pinned_request`` (the same IP-pinned, SSRF-guarded seam
    ``mcp_client`` uses for live MCP calls): it opens the pinned request, then
    READS the streamed body and CLOSES both the response and the pool in a
    try/finally, and returns a plain, already-materialized :class:`HttpResult`.
    An ``ssrf.SsrfError`` is mapped to :class:`OAuthTransportError` (carrying
    the ssrf ``kind``) so callers never need to import ``ssrf`` themselves.

  * :func:`http_form` - a convenience wrapper over a ``transport`` (default
    :func:`open_pinned`) for the hops whose body is an
    ``application/x-www-form-urlencoded`` form (the token and refresh POSTs)
    or that have no body at all (a discovery GET, or a bodyless probe). It
    encodes ``form`` (a dict) when given, adds ``Accept: application/json`` and
    an optional ``Authorization: Bearer <bearer>`` header, then calls
    ``transport`` - the exact function tests substitute a fake for.

The RFC 7591 dynamic-registration POST and the initial unauthenticated MCP
``initialize`` probe both need a raw JSON body, which ``http_form`` does not
build (its ``form`` argument is specifically form-urlencoded per the phase
brief). Those two hops call the injected ``transport`` callable directly with
a JSON-encoded ``body`` - still the SAME single seam, just without the
form-encoding step ``http_form`` adds on top of it. See ``discovery.py`` and
``registration.py``.

No ``import frappe`` here or anywhere in this package.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

from jarvis.connectors import ssrf
from jarvis.connectors.mcp_oauth.errors import OAuthTransportError

# A single OAuth hop's response body (discovery/AS metadata JSON, a token
# response) is small; this is a generous ceiling against a malicious or
# misbehaving server streaming an unbounded body at us.
_MAX_BODY_BYTES = 2 * 1024 * 1024

# WALL-CLOCK ceiling for ONE hop: connect + every redirect it follows + reading
# the whole body. ``read_timeout`` alone bounds none of that - it is a per-recv
# idle timer, so a server dripping one byte just under it streams forever, and a
# redirect chain multiplies it by the hop count. These are the totals every
# caller draws its own budget from:
#
#   * DISCOVERY_TOTAL_TIMEOUT_S - one discovery hop (the probe, the
#     protected-resource document, one sign-in-service metadata attempt).
#   * TOKEN_TOTAL_TIMEOUT_S - a token exchange, a refresh, or a registration
#     POST. Tighter because a refresh runs inside the broker's own sub-30s call
#     budget (see ``broker.REFRESH_BUDGET_S``).
DEFAULT_TOTAL_TIMEOUT_S = 20.0
DISCOVERY_TOTAL_TIMEOUT_S = 15.0
TOKEN_TOTAL_TIMEOUT_S = 10.0

# Redirect cap for one OAuth hop, below ``ssrf.MAX_REDIRECTS``: an OAuth
# endpoint that redirects at all is unusual, and every extra hop is budget the
# wall-clock deadline above then has to absorb.
MAX_REDIRECTS = 2


@dataclass
class HttpResult:
	status: int
	# Keys are lowercased (see open_pinned) - look up "www-authenticate", not "WWW-Authenticate".
	headers: dict
	json: dict | None
	text: str


TransportFn = Callable[..., HttpResult]


def _check_deadline(deadline: float, clock: Callable[[], float]) -> None:
	if clock() >= deadline:
		raise OAuthTransportError("timeout", "The sign-in service took too long to answer.")


def _read_capped(resp, deadline: float, clock: Callable[[], float]) -> bytes:
	chunks: list[bytes] = []
	total = 0
	for chunk in resp.stream(8192, decode_content=True):
		# Re-check between recvs, like ``mcp_client._read_capped`` does: a server
		# trickling bytes just inside the read timeout would otherwise stream past
		# any budget, one chunk at a time.
		_check_deadline(deadline, clock)
		if not chunk:
			continue
		total += len(chunk)
		if total > _MAX_BODY_BYTES:
			raise OAuthTransportError("response_too_large", "OAuth endpoint response exceeded the size cap.")
		chunks.append(chunk)
	return b"".join(chunks)


def _content_type(headers: dict) -> str:
	return headers.get("content-type", "").split(";")[0].strip().lower()


def open_pinned(
	url: str,
	*,
	method: str = "GET",
	headers: dict | None = None,
	body: bytes | None = None,
	connect_timeout: float = 5.0,
	read_timeout: float = 20.0,
	total_timeout: float = DEFAULT_TOTAL_TIMEOUT_S,
	egress_allowed: Callable[[str], bool] | None = None,
	drain_event_stream: bool = True,
	clock: Callable[[], float] = time.monotonic,
) -> HttpResult:
	"""The real transport: SSRF-guarded, IP-pinned, one hop, under a WALL-CLOCK
	``total_timeout``. Reads the whole body and closes both the response and the
	pool before returning.

	The deadline spans everything, as ONE clock: DNS, connect and the header wait
	(each clamped to what is left when it starts, plus a urllib3 ``total`` so they
	cannot be spent one after another), every redirect (capped at
	:data:`MAX_REDIRECTS`) and each chunk of the body read. Overrunning it raises
	``OAuthTransportError("timeout")`` with the response and pool already closed.

	``drain_event_stream=False`` says "this hop only needs the headers of an event
	stream": a 2xx ``text/event-stream`` answer is returned with an empty body
	instead of being read. The unauthenticated discovery probe uses it - a server
	that answers that probe rather than challenging it has told us everything we
	need (there is no sign-in to set up) and its stream may never end."""
	deadline = clock() + total_timeout
	try:
		resp, pool, _final_url = ssrf.open_pinned_request(
			url,
			method=method,
			headers=headers,
			body=body,
			connect_timeout=connect_timeout,
			read_timeout=read_timeout,
			egress_allowed=egress_allowed,
			max_redirects=MAX_REDIRECTS,
			deadline=deadline,
			clock=clock,
		)
	except ssrf.SsrfError as exc:
		# A guard rejection keeps its own kind; an exhausted budget surfaces as a
		# timeout, since ssrf reports that as a plain connect failure. exc's own
		# message never carries headers/body (see ssrf.SsrfError's docstring), so it
		# is safe to reuse as-is - no secret to redact.
		if clock() >= deadline:
			raise OAuthTransportError("timeout", "The sign-in service took too long to answer.") from exc
		raise OAuthTransportError(exc.kind, str(exc), kind=exc.kind) from exc

	try:
		resp_headers = {k.lower(): v for k, v in resp.headers.items()}
		content_type = _content_type(resp_headers)
		if not drain_event_stream and 200 <= resp.status < 300 and content_type == "text/event-stream":
			return HttpResult(status=resp.status, headers=resp_headers, json=None, text="")
		_check_deadline(deadline, clock)
		raw = _read_capped(resp, deadline, clock)
		text = raw.decode("utf-8", errors="replace")
		parsed = None
		if content_type == "application/json" or content_type.endswith("+json"):
			try:
				value = json.loads(text)
			except ValueError:
				value = None
			# A JSON document, not merely valid JSON: every caller reads named
			# fields off this, and handing them a list or a bare scalar would turn a
			# hostile response into an AttributeError deep in a validation gate.
			parsed = value if isinstance(value, dict) else None
		return HttpResult(status=resp.status, headers=resp_headers, json=parsed, text=text)
	finally:
		try:
			resp.close()
		finally:
			pool.close()


def http_form(
	url: str,
	*,
	method: str = "POST",
	headers: dict | None = None,
	form: dict | None = None,
	bearer: str | None = None,
	egress_allowed: Callable[[str], bool] | None = None,
	connect_timeout: float = 5.0,
	read_timeout: float = 20.0,
	total_timeout: float = DEFAULT_TOTAL_TIMEOUT_S,
	transport: TransportFn = open_pinned,
) -> HttpResult:
	"""Build a form-encoded (or bodyless) request and hand it to ``transport``.

	``form`` (if given) is url-encoded as ``application/x-www-form-urlencoded``;
	a ``form=None`` call (a discovery GET) sends no body. ``bearer`` (if given)
	sets ``Authorization: Bearer <bearer>``. ``transport`` is the single
	injection point - tests pass a fake with the same call shape as
	:func:`open_pinned` and no network is ever touched."""
	req_headers = dict(headers or {})
	req_headers.setdefault("Accept", "application/json")
	req_body = None
	if form is not None:
		req_body = urlencode(form).encode("utf-8")
		req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
	if bearer:
		req_headers["Authorization"] = f"Bearer {bearer}"
	return transport(
		url,
		method=method,
		headers=req_headers,
		body=req_body,
		connect_timeout=connect_timeout,
		read_timeout=read_timeout,
		total_timeout=total_timeout,
		egress_allowed=egress_allowed,
	)
