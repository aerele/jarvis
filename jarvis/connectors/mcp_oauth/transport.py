"""The one seam every outbound OAuth hop goes through.

Two layers, both swappable, both share the same call shape - ``(url, *,
method, headers, body, connect_timeout, read_timeout, egress_allowed) ->
HttpResult``:

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
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

from jarvis.connectors import ssrf
from jarvis.connectors.mcp_oauth.errors import OAuthTransportError

# A single OAuth hop's response body (discovery/AS metadata JSON, a token
# response) is small; this is a generous ceiling against a malicious or
# misbehaving server streaming an unbounded body at us.
_MAX_BODY_BYTES = 2 * 1024 * 1024


@dataclass
class HttpResult:
	status: int
	# Keys are lowercased (see open_pinned) - look up "www-authenticate", not "WWW-Authenticate".
	headers: dict
	json: dict | None
	text: str


TransportFn = Callable[..., HttpResult]


def _read_capped(resp) -> bytes:
	chunks: list[bytes] = []
	total = 0
	for chunk in resp.stream(8192, decode_content=True):
		if not chunk:
			continue
		total += len(chunk)
		if total > _MAX_BODY_BYTES:
			raise OAuthTransportError("response_too_large", "OAuth endpoint response exceeded the size cap.")
		chunks.append(chunk)
	return b"".join(chunks)


def open_pinned(
	url: str,
	*,
	method: str = "GET",
	headers: dict | None = None,
	body: bytes | None = None,
	connect_timeout: float = 5.0,
	read_timeout: float = 20.0,
	egress_allowed: Callable[[str], bool] | None = None,
) -> HttpResult:
	"""The real transport: SSRF-guarded, IP-pinned, one hop. Reads the whole
	body and closes both the response and the pool before returning."""
	try:
		resp, pool, _final_url = ssrf.open_pinned_request(
			url,
			method=method,
			headers=headers,
			body=body,
			connect_timeout=connect_timeout,
			read_timeout=read_timeout,
			egress_allowed=egress_allowed,
		)
	except ssrf.SsrfError as exc:
		# exc's own message never carries headers/body (see ssrf.SsrfError's
		# docstring), so it is safe to reuse as-is - no secret to redact.
		raise OAuthTransportError(exc.kind, str(exc), kind=exc.kind) from exc

	try:
		raw = _read_capped(resp)
		resp_headers = {k.lower(): v for k, v in resp.headers.items()}
		text = raw.decode("utf-8", errors="replace")
		parsed = None
		content_type = resp_headers.get("content-type", "").split(";")[0].strip().lower()
		if content_type == "application/json" or content_type.endswith("+json"):
			try:
				parsed = json.loads(text)
			except ValueError:
				parsed = None
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
		egress_allowed=egress_allowed,
	)
