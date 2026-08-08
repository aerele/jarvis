"""Server-side link fetch for Personalise Link-kind notes (Skills-area rework,
DESIGN.md sections 4 / 5b / 6b).

Before this module, there was NO general-purpose URL-fetch capability
anywhere in this app (research/processing.md section (c): every existing
``requests.*`` call is internal/infra - LLM key probes, the tenant's own
container gateway, OpenRouter, OAuth token exchange - never an arbitrary
customer-supplied URL). A Link-kind Personalise note is the first feature
that fetches content the CALLER chose, from wherever it points, so the SSRF
defenses below are load-bearing, not decorative:

  * scheme allowlist (http/https only - no ``file://``, ``gopher://``, etc.)
  * the hostname is resolved via ``socket.getaddrinfo`` and EVERY returned
    address is checked against private/loopback/link-local/reserved/
    multicast ranges plus the cloud-metadata address explicitly - a bare
    string check on the URL's host is not a guard (DNS rebinding, a hostname
    that only *later* resolves to 169.254.169.254, etc.)
  * the connection is then PINNED to the exact address that was just vetted:
    the socket is opened straight to that validated IP (via a per-request
    ``urllib3`` connection pool whose host IS the IP), NEVER by handing the
    hostname back to a client that would resolve it a SECOND, independent
    time. TLS still verifies against the ORIGINAL hostname (SNI
    ``server_hostname`` + certificate ``assert_hostname``) and the ``Host:``
    header carries the original hostname, so name-based virtual hosts and
    certificate checks keep working - but a DNS record that flips between the
    ``getaddrinfo`` check and the TCP connect can no longer divert the socket
    anywhere the guard did not approve. A validate-then-``requests.get(host)``
    shape does two separate resolutions and left exactly this DNS-rebinding
    TOCTOU wide open; pinning closes it.
  * redirects are followed MANUALLY (``redirect=False`` on every request),
    capped at 3 hops, and EVERY hop re-runs the full host validation AND
    re-pins to the freshly-vetted IP before being fetched - a public-looking
    URL that redirects to an internal address is exactly the same attack as
    supplying the internal address directly, so the guard must re-run, not
    just run once up front.
  * the response body is read in a bounded stream and rejected (not silently
    truncated) the instant it exceeds ``max_bytes`` - a slow/huge response
    must not tie up a worker or fill memory.
  * only a ``text/*`` content-type is accepted (HTML is readability-stripped
    via BeautifulSoup; anything else - images, PDFs, octet-streams - is
    refused outright; there is no OCR/PDF path here, matching the v1 scope
    research/processing.md calls out).

The extracted text additionally passes through the SAME instruction-
injection neutralization ``Jarvis Wiki Page`` bodies get
(``jarvis_wiki_page.py: JarvisWikiPage._sanitize_untrusted_text``) before
this function ever returns it: fetched web content is fully untrusted and
this is the funnel every Link-kind note's ``extracted_text`` goes through on
its way into a later extraction/ingest prompt. That method is instance-bound
on the wiki page controller (not a free function), so it is not importable
here - the five neutralization patterns below are a deliberate, documented
self-contained COPY of ``jarvis_wiki_page.py``'s ``_BODY_NEUTRALIZE`` list
(the same "every writer keeps its own copy" convention this codebase already
uses for ``_lk``/``_clamp_page`` across the ``*_api.py`` modules). Whatever
calls this function and feeds the result into an LLM prompt is still
responsible for its own ``<untrusted-data>`` fencing at that boundary
(``turn_handler.py``'s convention) - neutralization here only strips
instruction-shaped substrings, it does not label the text as untrusted.

Callers (``jarvis.chat.personalise_api``) treat every failure here -
guard rejection, network error, wrong content-type, oversize response - as
"answer/save the note anyway, just leave extracted_text empty" (never fail
the whole note save over a bad link); this module does the opposite and
RAISES ``LinkFetchError`` for every failure mode so a caller can log exactly
what went wrong via ``frappe.log_error`` instead of guessing from an empty
string. Pure exception-vs-string-return split, no silent partial results.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlparse

import certifi
import urllib3
from bs4 import BeautifulSoup

# ``fetch_and_extract``'s frozen defaults (DESIGN.md 6b: "max_bytes=1MB,
# timeout=10").
MAX_BYTES_DEFAULT = 1 * 1024 * 1024
TIMEOUT_S_DEFAULT = 10

MAX_REDIRECTS = 3
MAX_EXTRACTED_LEN = 20000

_ALLOWED_SCHEMES = ("http", "https")
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# Explicit belt-and-braces alongside ip.is_link_local below (169.254.0.0/16
# already covers this, but the cloud-metadata address is important enough,
# and named often enough in SSRF writeups, to check by name too).
_METADATA_IPS = {"169.254.169.254"}

_USER_AGENT = "JarvisLinkFetch/1.0 (+personalise-note-capture)"


# Failure classes carried on LinkFetchError.kind. The distinction that matters
# to a caller is WHETHER THE REMOTE END EVER SPOKE: the first three kinds mean
# this bench never got a byte back, which says nothing about the endpoint
# itself, only about what this network can reach. jarvis.llm_key_probe turns
# exactly that split into its "could not verify from here" verdict instead of
# calling a container-only endpoint a failure (#680), so classify at the raise
# site - matching on the message text would break the day one is reworded.
ERR_INVALID_URL = "invalid_url"  # caller's URL is unusable (scheme/host/empty)
ERR_UNRESOLVED = "unresolved"  # DNS gave us nothing
ERR_BLOCKED_ADDRESS = "blocked_address"  # SSRF guard refused the resolved address
ERR_CONNECT_FAILED = "connect_failed"  # socket/TLS/timeout before any response
ERR_RESPONSE = "response"  # the remote DID answer; we rejected what it said

# The three kinds that mean "this bench could not reach the endpoint at all".
UNREACHABLE_KINDS = frozenset({ERR_UNRESOLVED, ERR_BLOCKED_ADDRESS, ERR_CONNECT_FAILED})


class LinkFetchError(Exception):
	"""Raised for any guard rejection or fetch failure. Callers treat this
	(and any other exception this module might let through, defensively) as
	"skip extraction, keep the note" - see the module docstring.

	``kind`` is one of the ``ERR_*`` constants above. It defaults to
	``ERR_RESPONSE`` rather than to an unreachable kind so that anything
	constructed without an explicit class is treated as a definitive answer,
	never silently softened into "we could not check" - a caller that grants
	leniency on the unreachable kinds must only do so where this module really
	said the network failed."""

	def __init__(self, message: str, *, kind: str = ERR_RESPONSE):
		super().__init__(message)
		self.kind = kind


# --------------------------------------------------------------------------- #
# SSRF guard
# --------------------------------------------------------------------------- #
def _is_blocked_ip(ip_str: str) -> bool:
	try:
		ip = ipaddress.ip_address(ip_str)
	except ValueError:
		# Unparsable address from a resolver response - fail closed rather
		# than risk treating something we can't classify as safe.
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


def _validate_host(hostname: str) -> list[str]:
	"""Resolve ``hostname``, reject if ANY returned address is blocked, and
	return the list of vetted addresses for the caller to PIN the connection
	to.

	This is the actual guard - a scheme/host STRING check alone proves
	nothing about where the request will actually go (DNS rebinding, a
	hostname resolving to multiple addresses where only one is safe, etc.),
	so every candidate address ``getaddrinfo`` returns must be individually
	safe before the request is allowed to proceed. Returning the vetted
	addresses (rather than just passing/failing) is what lets ``fetch_and_
	extract`` connect to an address it already checked instead of letting the
	HTTP client resolve the name a second, unchecked time."""
	if not hostname:
		raise LinkFetchError("URL has no host to validate.", kind=ERR_INVALID_URL)
	try:
		infos = socket.getaddrinfo(hostname, None)
	except Exception as exc:
		raise LinkFetchError(f"Could not resolve host: {hostname}", kind=ERR_UNRESOLVED) from exc
	if not infos:
		raise LinkFetchError(f"Could not resolve host: {hostname}", kind=ERR_UNRESOLVED)
	addrs: list[str] = []
	for info in infos:
		addr = info[4][0]
		if _is_blocked_ip(addr):
			raise LinkFetchError(
				f"Host {hostname} resolves to a disallowed address ({addr}).",
				kind=ERR_BLOCKED_ADDRESS,
			)
		addrs.append(addr)
	return addrs


def _validate_url(url: str):
	"""Validate scheme + host and return ``(parsed, pinned_ip)`` - the parsed
	URL plus one vetted address the connection MUST be pinned to (every
	returned address already passed ``_is_blocked_ip``, so the first is as
	safe as any)."""
	parsed = urlparse(url)
	if parsed.scheme not in _ALLOWED_SCHEMES:
		raise LinkFetchError(
			f"URL scheme must be http or https (got {parsed.scheme!r}).", kind=ERR_INVALID_URL
		)
	if not parsed.hostname:
		raise LinkFetchError("URL has no host.", kind=ERR_INVALID_URL)
	vetted = _validate_host(parsed.hostname)
	return parsed, vetted[0]


# --------------------------------------------------------------------------- #
# bounded, streamed read
# --------------------------------------------------------------------------- #
def _read_capped(resp, max_bytes: int) -> bytes:
	"""Stream the body and raise the instant the running total exceeds
	``max_bytes`` - never buffer an unbounded/lying Content-Length response,
	and never silently truncate (an oversize response is a REJECTED note-
	content candidate, not a quietly-clipped one). ``resp`` is a
	``urllib3.HTTPResponse`` opened with ``preload_content=False``; the cap
	is measured on the DECODED bytes so a gzip bomb can't sneak past it."""
	chunks: list[bytes] = []
	total = 0
	for chunk in resp.stream(8192, decode_content=True):
		if not chunk:
			continue
		total += len(chunk)
		if total > max_bytes:
			raise LinkFetchError(f"Response exceeds the {max_bytes}-byte cap.")
		chunks.append(chunk)
	return b"".join(chunks)


# --------------------------------------------------------------------------- #
# HTML/text -> plain text + neutralization
# --------------------------------------------------------------------------- #
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Self-contained copy of jarvis_wiki_page.py's ``_BODY_NEUTRALIZE`` list (see
# the module docstring for why this is a copy, not an import): the same five
# injection shapes, same replacement convention.
_BODY_NEUTRALIZE = (
	(re.compile(r"ignore\s+(?:all\s+|the\s+|any\s+)?previous", re.IGNORECASE), "(sanitized)"),
	(re.compile(r"disregard\s+(?:all\s+|the\s+|any\s+)?(?:previous|above)", re.IGNORECASE), "(sanitized)"),
	(re.compile(r"(?im)^(\s*)(?:system|assistant|developer)\s*:"), r"\1(sanitized):"),
	(re.compile(r"<\s*/?\s*available_skills\s*>", re.IGNORECASE), "(sanitized)"),
	(re.compile(r"jarvis__"), "jarvis-"),
)


def _neutralize(text: str) -> str:
	text = _CONTROL_RE.sub("", text or "")
	for pattern, repl in _BODY_NEUTRALIZE:
		text = pattern.sub(repl, text)
	return text


def _decode(body: bytes) -> str:
	try:
		return body.decode("utf-8")
	except UnicodeDecodeError:
		return body.decode("utf-8", errors="replace")


def _extract_text(body: bytes, content_type: str) -> str:
	decoded = _decode(body)
	if content_type == "text/html":
		try:
			soup = BeautifulSoup(decoded, "html.parser")
			for tag in soup(["script", "style", "noscript"]):
				tag.decompose()
			decoded = soup.get_text(separator="\n")
		except Exception:
			# Malformed markup: fall back to the raw decoded text rather than
			# losing the note entirely.
			pass
	lines = [ln.strip() for ln in decoded.splitlines()]
	return "\n".join(ln for ln in lines if ln)


# --------------------------------------------------------------------------- #
# pinned fetch seam (SSRF DNS-rebinding defense - see the module docstring)
# --------------------------------------------------------------------------- #
def _host_header(hostname: str, port: int, scheme: str) -> str:
	"""``Host:`` header value carrying the ORIGINAL hostname (never the pinned
	IP), with the port appended only when it is non-default for the scheme."""
	default = 443 if scheme == "https" else 80
	return hostname if port == default else f"{hostname}:{port}"


def _build_pool(scheme: str, ip: str, port: int, hostname: str, timeout: int):
	"""The pinned single-use urllib3 pool: the socket goes to ``ip``, while TLS
	still verifies against the ORIGINAL ``hostname`` (SNI + cert check).

	Split out of ``_open_pinned`` so the pool can be built - and its connection
	actually constructed - in a test without a network round trip. It could not
	be, when this was inline, and that is precisely how the bug below survived:
	every test in test_link_fetch.py stubs ``_open_pinned`` wholesale, so the
	real pool construction was never once executed in CI.

	``server_hostname`` is a CONNECTION kwarg, not a pool one. urllib3 v2 pools
	collect their surplus **kwargs into ``self.conn_kw`` and splat that onto the
	connection, so it must be passed as a PLAIN kwarg here. Passing a literal
	``conn_kw={"server_hostname": ...}`` (as this did until now) gets collected
	one level too deep - ``conn_kw={"conn_kw": {...}}`` - and every https fetch
	dies with ``TypeError: HTTPSConnection.__init__() got an unexpected keyword
	argument 'conn_kw'``. That TypeError is neither HTTPError nor OSError, so
	``fetch_and_extract``'s except clause does not catch it either."""
	pool_timeout = urllib3.Timeout(connect=timeout, read=timeout)
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
	timeout: int,
	*,
	method: str = "GET",
	body: bytes | None = None,
	extra_headers: dict | None = None,
):
	"""Open a request pinned to the already-vetted ``ip`` - the DNS-rebind
	defense the module docstring describes. The socket connects straight to
	``ip`` via a fresh single-use ``urllib3`` pool whose host IS the IP, so no
	second resolution of ``parsed.hostname`` can ever divert the connection.
	TLS still verifies against the ORIGINAL hostname (SNI ``server_hostname``
	+ cert ``assert_hostname``) and the ``Host:`` header carries it too, so
	virtual hosts and certificate checks keep working. Returns
	``(response, pool)``; the caller owns closing both.

	``method``/``body``/``extra_headers`` default to a bare GET (every
	original caller here, ``fetch_and_extract``) - ``request_pinned`` below
	is the only caller that overrides them, for a JSON POST probe."""
	hostname = parsed.hostname
	scheme = parsed.scheme
	port = parsed.port or (443 if scheme == "https" else 80)
	target = parsed.path or "/"
	if parsed.query:
		target += "?" + parsed.query
	# extra_headers first, THEN the computed Host/User-Agent - never the other way
	# round. Host in particular is part of the DNS-rebind defense's bookkeeping
	# (it must always name the ORIGINAL hostname the address was vetted for, not
	# whatever a caller's headers dict happens to carry); request_pinned's caller
	# passes its own headers through here as extra_headers, and nothing stops a
	# future caller from including a "Host" key, so this order stays defensive
	# even though today's only extra_headers callers never set one. (The actual
	# TCP/TLS destination is pinned independently via host=ip / assert_hostname
	# below, so this was never an SSRF bypass - just correctness hardening for a
	# general-purpose entry point.)
	headers = dict(extra_headers or {})
	headers["User-Agent"] = _USER_AGENT
	headers["Host"] = _host_header(hostname, port, scheme)
	pool = _build_pool(scheme, ip, port, hostname, timeout)
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
def fetch_and_extract(
	url: str,
	max_bytes: int = MAX_BYTES_DEFAULT,
	timeout: int = TIMEOUT_S_DEFAULT,
) -> str:
	"""Fetch ``url`` server-side (SSRF-guarded, connection pinned to a vetted
	address on EVERY hop) and return readability-stripped plain text, truncated
	to ``MAX_EXTRACTED_LEN`` chars.

	Raises :class:`LinkFetchError` for every failure mode (bad scheme,
	disallowed address at any redirect hop, too many redirects, non-``text/*``
	content-type, oversize response, network error) - see the module
	docstring for why this raises instead of returning ``""``."""
	current = (url or "").strip()
	if not current:
		raise LinkFetchError("No URL given.", kind=ERR_INVALID_URL)

	redirects = 0
	while True:
		parsed, ip = _validate_url(current)
		try:
			resp, pool = _open_pinned(parsed, ip, timeout)
		except (urllib3.exceptions.HTTPError, OSError) as exc:
			raise LinkFetchError(f"Fetch failed: {exc}", kind=ERR_CONNECT_FAILED) from exc

		try:
			if resp.status in _REDIRECT_STATUSES:
				location = resp.headers.get("Location")
				if not location:
					raise LinkFetchError("Redirect response had no Location header.")
				if redirects >= MAX_REDIRECTS:
					raise LinkFetchError("Too many redirects.")
				redirects += 1
				current = urljoin(current, location)
				continue

			if resp.status < 200 or resp.status >= 300:
				raise LinkFetchError(f"Fetch failed with status {resp.status}.")

			content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
			if not content_type.startswith("text/"):
				raise LinkFetchError(f"Unsupported content-type: {content_type or 'unknown'}.")

			body = _read_capped(resp, max_bytes)
		finally:
			resp.close()
			pool.close()

		text = _neutralize(_extract_text(body, content_type))
		return text[:MAX_EXTRACTED_LEN]


# --------------------------------------------------------------------------- #
# generic pinned request (non-GET, non-text/html callers)
# --------------------------------------------------------------------------- #
def request_pinned(
	url: str,
	*,
	method: str = "GET",
	headers: dict | None = None,
	json_body: dict | None = None,
	timeout: int = TIMEOUT_S_DEFAULT,
	max_bytes: int = MAX_BYTES_DEFAULT,
) -> tuple[int, dict, bytes]:
	"""SSRF-guarded HTTP request for a caller that needs something other than
	``fetch_and_extract``'s GET + ``text/*``-only shape - e.g.
	``jarvis.llm_key_probe``'s provider API-key test, which POSTs a JSON body
	to a CUSTOMER-SUPPLIED ``base_url`` (an OpenAI-Compatible / GLM-Z.ai /
	self-hosted vLLM endpoint) and needs the provider's raw error body back,
	not extracted page text.

	Reuses this module's guard exactly (see the module docstring): scheme
	allowlist + every resolved address checked via ``_validate_url``, then the
	connection is PINNED to that one vetted address so a DNS record that
	changes between the check and the connect can't divert the socket - the
	same TOCTOU defense ``fetch_and_extract`` relies on. Unlike
	``fetch_and_extract`` this does NOT follow redirects (a 3xx from a JSON
	API is returned to the caller as-is - chasing HTML-style redirect chains
	is not this entry point's job) and does NOT restrict the response
	content-type (JSON APIs reply ``application/json``, not ``text/*``).

	Returns ``(status_code, headers, body_bytes)`` - the body is READ AND
	CAPPED at ``max_bytes`` (via ``_read_capped``, same as the fetch path) but
	otherwise handed back raw; the caller owns parsing/scrubbing it.

	Raises :class:`LinkFetchError` for a blocked scheme/host (SSRF) or any
	network failure. The raised message never includes ``headers`` or
	``json_body`` - callers routinely pass a secret (e.g. an
	``Authorization`` header), and that secret must never end up in an
	exception string a caller might log.
	"""
	current = (url or "").strip()
	if not current:
		raise LinkFetchError("No URL given.", kind=ERR_INVALID_URL)

	parsed, ip = _validate_url(current)

	body: bytes | None = None
	hdrs = dict(headers or {})
	if json_body is not None:
		body = json.dumps(json_body).encode("utf-8")
		hdrs.setdefault("Content-Type", "application/json")

	try:
		resp, pool = _open_pinned(parsed, ip, timeout, method=method, body=body, extra_headers=hdrs)
	except (urllib3.exceptions.HTTPError, OSError) as exc:
		raise LinkFetchError(f"Request failed: {exc}", kind=ERR_CONNECT_FAILED) from exc

	try:
		data = _read_capped(resp, max_bytes)
		return resp.status, dict(resp.headers), data
	finally:
		resp.close()
		pool.close()
