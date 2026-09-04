"""SSRF-guarded, IP-pinned HTTP seam for the MCP connector client.

This is a deliberate, self-contained COPY of the guard proven in
``jarvis.chat.link_fetch`` (``_is_blocked_ip`` / ``_validate_host`` /
``_validate_url`` + pin-to-vetted-IP + per-redirect revalidation), adapted for
the connector client's needs. The codebase already keeps a per-writer copy of
this guard rather than sharing one importable seam (link_fetch's docstring
spells out why - see its ``_BODY_NEUTRALIZE`` note), so this module owns its
copy too. The differences from link_fetch, and why:

  * The connector talks JSON-RPC over HTTP POST, not GET of an HTML page. So
    this seam POSTs a body, accepts ``application/json`` AND
    ``text/event-stream``, and hands the STREAMING response object back to the
    caller (``mcp_client``) instead of reading and extracting text itself - the
    client parses SSE frames incrementally and must be able to stop reading the
    instant its JSON-RPC response arrives (a server MAY keep an SSE stream open
    past the response; reading to EOF would hang until the read timeout).

  * Redirects: an MCP endpoint should not redirect, but if it does we follow
    only 307/308 (a re-POST of the same body), cap the hops, and re-run the FULL
    host validation + re-pin + egress hook on every hop - a public-looking URL
    that 307s to an internal address is exactly the SSRF the pin defends
    against. A 301/302/303 on a POST is treated as an error, not silently
    downgraded to a GET. CROSS-HOST redirects are refused outright: following
    one would forward the ``Authorization: Bearer <token>`` header to a host the
    user never approved, leaking the credential. link_fetch has no credential to
    leak and no body to re-send, so it did neither of these.

  * Optional admin egress hook: ``egress_allowed`` is an injected callable the
    broker wires to a control-plane / operator setting (allow/deny custom
    hosts). It is injected rather than imported so this module stays frappe-free
    and unit-testable; the default is allow-all (the setting is owned by the
    data-model layer and may be absent).

No frappe import here on purpose - see the package docstring.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

import certifi
import urllib3

# --------------------------------------------------------------------------- #
# tunables
# --------------------------------------------------------------------------- #
_ALLOWED_SCHEMES = ("http", "https")

# Only same-body re-POST redirects are followed; 301/302/303 change method
# semantics and are refused on a JSON-RPC POST (see the module docstring).
_REPOST_REDIRECT_STATUSES = (307, 308)
MAX_REDIRECTS = 3

# Belt-and-braces alongside ip.is_link_local (169.254.0.0/16 already covers it,
# but the cloud-metadata address is important enough to name explicitly).
_METADATA_IPS = {"169.254.169.254", "fd00:ec2::254"}

_USER_AGENT = "JarvisConnector/1.0"

# Hard wall-clock bound for a single DNS resolution. getaddrinfo has no timeout of
# its own and can block a worker for the OS resolver's full retry budget
# (typically 20-30s) on a black-holed nameserver; the caller lowers this to fit
# whatever remains of the overall request budget.
DEFAULT_RESOLVE_TIMEOUT = 5.0


# Failure kinds carried on SsrfError.kind - mirrors link_fetch's split so a
# caller can tell "this bench could not reach the endpoint" (unreachable) from
# "the endpoint answered and we rejected it".
ERR_INVALID_URL = "invalid_url"
ERR_UNRESOLVED = "unresolved"
ERR_BLOCKED_ADDRESS = "blocked_address"
ERR_EGRESS_DENIED = "egress_denied"
ERR_CONNECT_FAILED = "connect_failed"
ERR_TOO_MANY_REDIRECTS = "too_many_redirects"
ERR_CROSS_HOST_REDIRECT = "cross_host_redirect"
ERR_INSECURE_REDIRECT = "insecure_redirect"

UNREACHABLE_KINDS = frozenset({ERR_UNRESOLVED, ERR_BLOCKED_ADDRESS, ERR_CONNECT_FAILED})


class SsrfError(Exception):
	"""Raised for any guard rejection or transport failure. Never carries the
	request headers or body - callers pass a Bearer token in the headers, and
	that secret must never reach an exception string a caller might log."""

	def __init__(self, message: str, *, kind: str = ERR_CONNECT_FAILED):
		super().__init__(message)
		self.kind = kind


# --------------------------------------------------------------------------- #
# SSRF guard (copied from link_fetch - see module docstring)
# --------------------------------------------------------------------------- #
def _is_blocked_ip(ip_str: str) -> bool:
	try:
		ip = ipaddress.ip_address(ip_str)
	except ValueError:
		# Unparsable address from a resolver response - fail closed.
		return True
	if ip_str in _METADATA_IPS:
		return True
	return bool(
		ip.is_private
		or ip.is_loopback
		or ip.is_link_local
		or ip.is_reserved
		or ip.is_multicast
		or ip.is_unspecified
	)


def _resolve(hostname: str, resolve_timeout: float) -> list:
	"""``socket.getaddrinfo`` with a HARD wall-clock bound.

	getaddrinfo cannot be given a timeout, so run it in a daemon thread and abandon
	it if it overruns ``resolve_timeout`` - a black-holed nameserver otherwise pins
	a gunicorn worker for the OS resolver's full retry budget, well past the
	request deadline. A timeout is reported as ``ERR_CONNECT_FAILED`` (not
	``ERR_UNRESOLVED``): a resolver that keeps hanging IS an endpoint-health signal,
	so the broker's circuit breaker should open on it and fast-fail the next call
	rather than burn another worker. A normal resolution failure stays
	``ERR_UNRESOLVED``. The abandoned thread is a daemon and cannot block shutdown."""
	result: dict = {}

	def _run():
		try:
			result["infos"] = socket.getaddrinfo(hostname, None)
		except Exception as exc:
			# Any resolver failure is handed back to the calling thread below.
			result["error"] = exc

	thread = threading.Thread(target=_run, name="ssrf-getaddrinfo", daemon=True)
	thread.start()
	# A small floor so a near-exhausted budget still gives the resolver a real
	# (if tiny) window rather than an instant, guaranteed timeout.
	thread.join(max(0.1, resolve_timeout))
	if thread.is_alive():
		raise SsrfError(f"DNS resolution for {hostname} timed out.", kind=ERR_CONNECT_FAILED)
	if "error" in result:
		raise SsrfError(f"Could not resolve host: {hostname}", kind=ERR_UNRESOLVED) from result["error"]
	return result.get("infos") or []


def _validate_host(hostname: str, resolve_timeout: float = DEFAULT_RESOLVE_TIMEOUT) -> list[str]:
	"""Resolve ``hostname`` and reject if ANY returned address is blocked; return
	the vetted addresses so the connection can be PINNED to one of them. A string
	check on the host alone proves nothing about where the socket will land (DNS
	rebinding, split-horizon records), so every ``getaddrinfo`` result must be
	individually safe before the request proceeds. Resolution is time-bounded (see
	:func:`_resolve`) so it cannot outlast the request budget."""
	if not hostname:
		raise SsrfError("URL has no host to validate.", kind=ERR_INVALID_URL)
	infos = _resolve(hostname, resolve_timeout)
	if not infos:
		raise SsrfError(f"Could not resolve host: {hostname}", kind=ERR_UNRESOLVED)
	addrs: list[str] = []
	for info in infos:
		addr = info[4][0]
		if _is_blocked_ip(addr):
			raise SsrfError(
				f"Host {hostname} resolves to a disallowed address ({addr}).",
				kind=ERR_BLOCKED_ADDRESS,
			)
		addrs.append(addr)
	return addrs


def _validate_url(
	url: str,
	egress_allowed: Callable[[str], bool] | None,
	resolve_timeout: float = DEFAULT_RESOLVE_TIMEOUT,
):
	"""Validate scheme, reject embedded credentials, run the optional egress
	hook, resolve + vet the host, and return ``(parsed, pinned_ip)``.

	``egress_allowed`` (if given) is consulted with the bare hostname BEFORE any
	DNS lookup, so an operator deny-list short-circuits the resolution too.
	``resolve_timeout`` bounds the DNS lookup (see :func:`_validate_host`)."""
	parsed = urlparse(url)
	if parsed.scheme not in _ALLOWED_SCHEMES:
		raise SsrfError(f"URL scheme must be http or https (got {parsed.scheme!r}).", kind=ERR_INVALID_URL)
	# Reject userinfo in the URL - never let a "https://user:PAT@host" form put a
	# secret where an exception string or a Host header could carry it.
	if parsed.username or parsed.password:
		raise SsrfError("URL must not contain embedded credentials.", kind=ERR_INVALID_URL)
	if not parsed.hostname:
		raise SsrfError("URL has no host.", kind=ERR_INVALID_URL)
	if egress_allowed is not None and not egress_allowed(parsed.hostname):
		raise SsrfError(
			f"Egress to host {parsed.hostname} is not permitted by policy.",
			kind=ERR_EGRESS_DENIED,
		)
	vetted = _validate_host(parsed.hostname, resolve_timeout)
	return parsed, vetted[0]


# --------------------------------------------------------------------------- #
# pinned open seam (DNS-rebind defense - the socket goes to the vetted IP, TLS
# still verifies the ORIGINAL hostname via SNI + cert). Copied from link_fetch;
# see its ``_build_pool`` docstring for the urllib3 v2 ``server_hostname``/
# ``conn_kw`` gotcha these two functions encode.
# --------------------------------------------------------------------------- #
def _host_header(hostname: str, port: int, scheme: str) -> str:
	default = 443 if scheme == "https" else 80
	return hostname if port == default else f"{hostname}:{port}"


def _build_pool(scheme: str, ip: str, port: int, hostname: str, connect_timeout: float, read_timeout: float):
	pool_timeout = urllib3.Timeout(connect=connect_timeout, read=read_timeout)
	if scheme != "https":
		return urllib3.HTTPConnectionPool(host=ip, port=port, timeout=pool_timeout, retries=False)
	return urllib3.HTTPSConnectionPool(
		host=ip,
		port=port,
		assert_hostname=hostname,
		cert_reqs="CERT_REQUIRED",
		ca_certs=certifi.where(),
		timeout=pool_timeout,
		retries=False,
		server_hostname=hostname,
	)


def _open_pinned(
	parsed,
	ip: str,
	connect_timeout: float,
	read_timeout: float,
	*,
	method: str,
	body: bytes | None,
	extra_headers: dict,
):
	"""Open one request pinned to the already-vetted ``ip``. Returns
	``(response, pool)`` with ``preload_content=False`` so the caller streams the
	body itself; the caller owns closing both. ``extra_headers`` is applied
	first, then User-Agent/Host are set last so a caller can never override the
	Host bookkeeping the pin relies on."""
	hostname = parsed.hostname
	scheme = parsed.scheme
	port = parsed.port or (443 if scheme == "https" else 80)
	target = parsed.path or "/"
	if parsed.query:
		target += "?" + parsed.query
	headers = dict(extra_headers or {})
	headers["User-Agent"] = _USER_AGENT
	headers["Host"] = _host_header(hostname, port, scheme)
	pool = _build_pool(scheme, ip, port, hostname, connect_timeout, read_timeout)
	resp = pool.urlopen(
		method,
		target,
		body=body,
		headers=headers,
		redirect=False,
		preload_content=False,
		decode_content=True,
	)
	return resp, pool


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def open_pinned_request(
	url: str,
	*,
	method: str = "POST",
	headers: dict | None = None,
	body: bytes | None = None,
	connect_timeout: float = 5.0,
	read_timeout: float = 20.0,
	egress_allowed: Callable[[str], bool] | None = None,
):
	"""Open ``method url`` SSRF-guarded and pinned to a vetted IP, following only
	same-body 307/308 redirects (each re-validated, re-pinned, re-egress-checked;
	cross-host refused), and return ``(response, pool, final_url)``.

	The response is a streaming ``urllib3.HTTPResponse`` (``preload_content=
	False``); the caller reads/parses and MUST close both the response and the
	pool. Raises :class:`SsrfError` for any guard rejection or transport error.
	The original ``url``'s host is the only host an ``Authorization`` header will
	ever be sent to."""
	current = (url or "").strip()
	if not current:
		raise SsrfError("No URL given.", kind=ERR_INVALID_URL)

	# Bound DNS resolution by what remains of the connect/read budget so a hung
	# resolver cannot outlast the request the caller asked for.
	resolve_timeout = min(connect_timeout, read_timeout)

	origin_host = None
	redirects = 0
	while True:
		parsed, ip = _validate_url(current, egress_allowed, resolve_timeout)
		if origin_host is None:
			origin_host = parsed.hostname
		try:
			resp, pool = _open_pinned(
				parsed,
				ip,
				connect_timeout,
				read_timeout,
				method=method,
				body=body,
				extra_headers=dict(headers or {}),
			)
		except (urllib3.exceptions.HTTPError, OSError) as exc:
			raise SsrfError(f"Request failed: {exc}", kind=ERR_CONNECT_FAILED) from exc

		if resp.status in _REPOST_REDIRECT_STATUSES:
			location = resp.headers.get("Location")
			resp.close()
			pool.close()
			if not location:
				raise SsrfError("Redirect response had no Location header.", kind=ERR_CONNECT_FAILED)
			if redirects >= MAX_REDIRECTS:
				raise SsrfError("Too many redirects.", kind=ERR_TOO_MANY_REDIRECTS)
			redirects += 1
			nxt = urljoin(current, location)
			nxt_parsed = urlparse(nxt)
			if (nxt_parsed.hostname or "").lower() != (origin_host or "").lower():
				# Following would forward the Bearer token to a host the user
				# never approved. Refuse rather than strip-and-follow.
				raise SsrfError(
					"Refusing cross-host redirect (would leak credential).",
					kind=ERR_CROSS_HOST_REDIRECT,
				)
			if parsed.scheme == "https" and nxt_parsed.scheme == "http":
				# A same-host downgrade to http would re-POST the Authorization:
				# Bearer header in cleartext. Refuse rather than leak the credential.
				raise SsrfError(
					"Refusing https to http downgrade on redirect (would expose credential).",
					kind=ERR_INSECURE_REDIRECT,
				)
			current = nxt
			continue

		return resp, pool, current
