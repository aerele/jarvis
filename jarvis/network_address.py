"""Public-address rules for the private-network onboarding (design spec section 9).

MIRRORS jarvis_admin_v2/fleet/address_rules.py (admin is the authority). The pure logic
below that mirror is copied verbatim: reason codes, regexes, reserved-TLD set, port, path
and label rules, canonicalisation. The only edit is the rename of admin's
``check_public_address`` to ``address_reason`` so the name is free for the customer-facing
wrapper at the bottom of this file. Change this file and the admin file TOGETHER; the
parity table in jarvis/tests/test_network_address.py is the contract between them.

Local test mode relaxes exactly three things: ``http`` is accepted, ``localhost`` and
``*.localhost`` names are accepted, and IPv4 loopback (127.0.0.0/8) is accepted.
"""

import ipaddress
import re
from urllib.parse import urlsplit

MAX_ADDRESS_LEN = 255  # = billing.signup._MAX_SITE_URL_LEN and the Jarvis Customer column

# Why an address was refused. Stable strings: tests, logs and the operator console
# read them; the customer-facing wording lives with the caller.
REASON_EMPTY = "empty"
REASON_TOO_LONG = "too_long"
REASON_BAD_CHARS = "bad_characters"
REASON_BAD_URL = "not_a_url"
REASON_SCHEME = "scheme_not_https"
REASON_USERINFO = "has_user_info"
REASON_QUERY = "has_query_or_fragment"
REASON_PORT = "bad_port"
REASON_PATH = "bad_path"
REASON_HOST = "bad_host"
REASON_IPV6 = "ipv6_not_supported"
REASON_NOT_GLOBAL = "not_a_public_ip"
REASON_LOCAL_NAME = "local_name"
REASON_RESERVED_TLD = "reserved_tld"
REASON_SINGLE_LABEL = "single_label_host"

# TLDs (and pseudo-TLDs) that never name a public host: RFC 2606 / 6761 / 6762 / 8375
# reserved names plus the private-network suffixes routers and corporate DNS invent.
_RESERVED_TLDS = frozenset(
	{
		"test",
		"example",
		"invalid",
		"localhost",
		"local",
		"localdomain",
		"internal",
		"intranet",
		"private",
		"corp",
		"home",
		"lan",
		"arpa",
		"onion",
		"alt",
	}
)

_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/\-]*$")
_DEFAULT_PORT = {"https": 443, "http": 80}
_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")


def is_global_ipv4(value: str) -> bool:
	"""True when ``value`` is an IPv4 literal ``ipaddress`` calls global. Documentation
	ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), private, loopback, link-local,
	CGNAT (100.64.0.0/10), multicast and reserved space are all NOT global."""
	try:
		ip = ipaddress.IPv4Address(value)
	except ValueError:
		return False
	return _is_public(ip)


def _is_public(ip) -> bool:
	"""``ipaddress`` calls some multicast blocks global; no customer sits behind one."""
	return ip.is_global and not ip.is_multicast


def _split(url):
	"""``(parts, reason)``. ``reason`` is set when the string is not a plain URL."""
	if not isinstance(url, str) or not url.strip():
		return None, REASON_EMPTY
	if len(url) > MAX_ADDRESS_LEN:
		return None, REASON_TOO_LONG
	if any(ord(c) <= 0x20 or ord(c) == 0x7F or c == "\\" for c in url):
		return None, REASON_BAD_CHARS
	if not url.isascii():
		return None, REASON_BAD_CHARS
	try:
		parts = urlsplit(url)
	except ValueError:
		return None, REASON_BAD_URL
	if not parts.scheme or not parts.netloc:
		return None, REASON_BAD_URL
	return parts, None


def _host_reason(host: str, *, local_test: bool) -> str | None:
	"""Why ``host`` (lower-cased, no port) is not an acceptable public host, or None."""
	if host.startswith("[") or ":" in host:
		return REASON_IPV6
	if host.endswith("."):
		return REASON_HOST
	try:
		ip = ipaddress.IPv4Address(host)
	except ValueError:
		ip = None
	if ip is not None:
		if local_test and ip in _LOOPBACK:
			return None
		return None if _is_public(ip) else REASON_NOT_GLOBAL
	labels = host.split(".")
	if not all(_LABEL_RE.match(label) for label in labels):
		return REASON_HOST
	if local_test and (host == "localhost" or host.endswith(".localhost")):
		return None
	if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
		return REASON_LOCAL_NAME
	if len(labels) < 2:
		return REASON_SINGLE_LABEL
	tld = labels[-1]
	if tld in _RESERVED_TLDS or ".".join(labels[-2:]) == "home.arpa":
		return REASON_RESERVED_TLD
	# A real TLD starts with a letter. This is what refuses "127.1", "2130706433.1",
	# "1.2.3.4.5" and every other numeric spelling of an address that is not dotted-quad.
	if not tld[0].isalpha():
		return REASON_HOST
	return None


def _path_has_odd_segment(path: str) -> bool:
	"""True for a ``.``, ``..`` or empty segment (other than one trailing slash): HTTP
	clients collapse these, so they would give one address several spellings."""
	if path in ("", "/"):
		return False
	body = path[1:]
	if body.endswith("/"):
		body = body[:-1]
	return any(seg in ("", ".", "..") for seg in body.split("/"))


def address_reason(url, *, local_test: bool = False) -> str | None:
	"""``None`` when ``url`` is an acceptable public address, else the reason code.

	Rules (spec section 9): scheme https; optional port; no user info, query or
	fragment; an optional path; host is a domain name or a global IPv4 literal;
	localhost, ``*.localhost``, ``*.local``, reserved TLDs, single-label names and
	non-global IPs are refused. ``local_test`` is the Deployment Mode ``local`` relaxation."""
	parts, reason = _split(url)
	if reason:
		return reason
	scheme = parts.scheme.lower()
	if scheme != "https" and not (local_test and scheme == "http"):
		return REASON_SCHEME
	if "@" in parts.netloc or parts.username is not None or parts.password is not None:
		return REASON_USERINFO
	if "?" in url or "#" in url:
		return REASON_QUERY
	try:
		port = parts.port
	except ValueError:
		return REASON_PORT
	if port is not None and not 1 <= port <= 65535:
		return REASON_PORT
	if parts.path and not _PATH_RE.match(parts.path):
		return REASON_PATH
	if _path_has_odd_segment(parts.path):
		return REASON_PATH
	host = (parts.hostname or "").lower()
	if not host:
		return REASON_HOST
	return _host_reason(host, local_test=local_test)


def normalize_address(url: str) -> str:
	"""The canonical spelling of an address that passed ``check_public_address``:
	lower-case scheme and host, default port dropped, no trailing slash. Raises
	``ValueError`` for anything unparseable, so call it after the check."""
	parts = urlsplit(url.strip())
	scheme = parts.scheme.lower()
	host = (parts.hostname or "").lower()
	if not host:
		raise ValueError("address has no host")
	port = parts.port
	netloc = host if port in (None, _DEFAULT_PORT.get(scheme)) else f"{host}:{port}"
	path = parts.path.rstrip("/")
	return f"{scheme}://{netloc}{path}"


def address_variants(url: str) -> list[str]:
	"""Every stored spelling that means the same address as ``url`` (the normal form
	with and without the trailing slash). Used to find another customer's row."""
	base = normalize_address(url)
	return [base, base + "/"]


# ---------------------------------------------------------------------------
# Bench-only layer: customer-facing copy and the test-mode switch.
# ---------------------------------------------------------------------------

MSG_EMPTY = "Enter the public address of your ERPNext."
MSG_HTTP = "Use an https:// address. Plain HTTP is not allowed."
MSG_SCHEME = "Start the address with https://"
MSG_NO_HOST = "Enter a domain name or a public IP after https://"
MSG_PRIVATE = "This is a private address. Use the public address your network team gave you."
MSG_DOMAIN = "Enter a full domain name, like erp.example.com, or a public IP address."
MSG_USERINFO = "Enter only the address: no user name or password."
MSG_EXTRAS = "Enter only the address: no query string or fragment."
MSG_PORT = "The port must be a number between 1 and 65535."
MSG_IPV6 = "IPv6 addresses aren't supported yet. Use a domain name or an IPv4 address."
MSG_CHARS = "The address can't contain spaces or control characters."
MSG_LONG = "The address is too long."
MSG_PATH = "Enter a plain path: letters, digits, dots, dashes and single slashes only."

_MESSAGES = {
	REASON_EMPTY: MSG_EMPTY,
	REASON_TOO_LONG: MSG_LONG,
	REASON_BAD_CHARS: MSG_CHARS,
	REASON_USERINFO: MSG_USERINFO,
	REASON_QUERY: MSG_EXTRAS,
	REASON_PORT: MSG_PORT,
	REASON_PATH: MSG_PATH,
	REASON_HOST: MSG_DOMAIN,
	REASON_IPV6: MSG_IPV6,
	REASON_NOT_GLOBAL: MSG_PRIVATE,
	REASON_LOCAL_NAME: MSG_PRIVATE,
	REASON_RESERVED_TLD: MSG_PRIVATE,
	REASON_SINGLE_LABEL: MSG_DOMAIN,
}


def private_test_mode() -> bool:
	"""Site config ``jarvis_private_network_test_mode`` (int 0 or 1). One of the three
	switches that let a local bench use http and loopback targets (spec section 10)."""
	import frappe

	return bool(frappe.utils.cint(frappe.conf.get("jarvis_private_network_test_mode")))


def _message_for(reason: str, raw: str) -> str:
	if reason == REASON_BAD_URL:
		return MSG_NO_HOST if "://" in raw else MSG_SCHEME
	if reason == REASON_SCHEME:
		return MSG_HTTP if raw.lower().startswith("http://") else MSG_SCHEME
	return _MESSAGES[reason]


def check_public_address(address, *, test_mode: bool = False) -> tuple[str, str]:
	"""``(normalized, "")`` for an acceptable address, else ``("", customer message)``.

	Surrounding whitespace is trimmed first; everything else is admin's rule set, so the bench
	never accepts an address admin refuses."""
	raw = address.strip() if isinstance(address, str) else ""
	reason = address_reason(raw, local_test=test_mode)
	if reason:
		return "", _message_for(reason, raw)
	return normalize_address(raw), ""
