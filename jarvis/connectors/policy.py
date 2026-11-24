"""Pure policy decisions for a connector call: the allowed-actions GATE and the
argument check against the cached ``inputSchema``.

Extracted from the broker so the two security-critical decisions are frappe-free
and unit-testable without a bench. The broker feeds a row-like object (anything
with ``.get(field)`` and, for actions, a list of child dicts) and turns a
returned ``(code, message)`` into its ``{ok: False}`` envelope; ``None`` means
allowed. No frappe import here.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import re

from jarvis.connectors import schema

_HEX_PART = re.compile(r"0x[0-9a-f]+", re.ASCII)
_OCTAL_PART = re.compile(r"0[0-7]*", re.ASCII)
_DECIMAL_PART = re.compile(r"[1-9][0-9]*", re.ASCII)


def action_decision(row, action: str) -> tuple[str, str] | None:
	"""Return ``(code, message)`` if ``action`` is NOT permitted on ``row``, else
	``None``.

	Rules (reading only the STORED child-row flags - never a tool's
	``annotations``, which the MCP spec says are untrusted):

	  * no child row for the action  -> deny (``action_unknown``)
	  * child ``allowed`` is truthy  -> allow
	  * otherwise -> deny (``action_denied``)

	``allowed`` is the ONLY allow bit. ``read_only`` never grants on its own: a
	new read-only tool is pre-checked (``allowed=1``) when it is first stored, so
	an ``allowed=0`` read-only row is one a user switched off, and must stay off.

	The child rows live on the ``allowed_actions`` table of ``Jarvis Connector``
	(a ``Jarvis Connector Action`` each: ``action``, ``allowed``, ``read_only``,
	``destructive``, ``description``)."""
	for child in row.get("allowed_actions") or []:
		if child.get("action") != action:
			continue
		if child.get("allowed"):
			return None
		return ("action_denied", f"The action {action!r} is not enabled for this connector.")
	return ("action_unknown", f"The action {action!r} is not configured on this connector.")


def argument_error(row, action: str, args, schema_obj=None) -> tuple[str, str] | None:
	"""Return ``("invalid_arguments", msg)`` if ``args`` violates the cached
	``inputSchema`` for ``action``, else ``None``. Missing/unparsable cache or an
	unknown tool means "cannot validate locally" -> allow (the server validates
	authoritatively). Pass ``schema_obj`` when the caller already parsed it so the
	cache is not read twice. See the ``tools_cache`` contract in ``broker.py``."""
	if schema_obj is None:
		schema_obj = input_schema(row, action)
	if schema_obj is None:
		return None
	err = schema.validate_arguments(schema_obj, args or {})
	if err:
		return ("invalid_arguments", err)
	return None


def egress_match(host: str, raw: str | None) -> bool:
	"""Decide whether ``host`` is permitted by the operator's egress rules ``raw``
	(the free-text ``Jarvis Settings.connector_egress_rules``, one rule per line).
	Fail-OPEN: empty/unset rules allow everything (the SSRF guard still blocks
	private/metadata addresses regardless of this).

	Grammar (blank lines and ``#`` comments ignored). Each rule is a host glob
	(``fnmatch``); a bare domain also matches its subdomains. A line beginning
	``!`` is a DENY rule, otherwise an ALLOW rule:

	  * any DENY rule matches           -> deny
	  * ALLOW rules exist but none match -> deny (allow-list mode)
	  * no ALLOW rules (deny-list/empty) -> allow

	Host and rules are both put through :func:`normalise_host` first, so a
	trailing dot, an unusual IP spelling or a unicode name compares like for like
	(the resolver connects to all of those). A host that cannot be normalised is
	denied whenever any rule exists."""
	if not raw:
		return True
	allows, denies = _parse_rules(raw)
	if not allows and not denies:
		return True
	norm = normalise_host(host)
	if norm is None:
		return False
	if any(_host_matches(norm, r) for r in denies):
		return False
	if allows and not any(_host_matches(norm, r) for r in allows):
		return False
	return True


def egress_address_match(address: str, raw: str | None) -> bool:
	"""Check a RESOLVED address against the DENY rules that name an IP, so a
	hostname whose DNS record points at a denied IP cannot slip past an IP rule.
	Only IP-literal deny rules apply here: allow rules and hostname rules are
	about names, and applying them to an address would deny every call in
	allow-list mode."""
	if not raw:
		return True
	denied = {ip for ip in map(canonical_ip, _parse_rules(raw)[1]) if ip}
	if not denied:
		return True
	return canonical_ip(address) not in denied


def normalise_host(host: str | None) -> str | None:
	"""Canonical form of a host or rule for matching, or ``None`` if it cannot be
	one. IDNA-encodes to ASCII first (the same codec ``getaddrinfo`` applies to a
	``str`` host; it also maps U+3002, U+FF0E and U+FF61 to a dot), lower-cases,
	THEN drops ONE trailing dot, so a dot-like ending cannot hide behind the strip.
	Every IP literal the resolver accepts (``127.1``, ``0x7f.1``, a bare decimal,
	long IPv6, IPv4-mapped IPv6) becomes its canonical text. An empty result, an
	empty label (``a..b``, ``.a``) or a second trailing dot is refused."""
	text = (host or "").strip()
	if not text:
		return None
	try:
		text = text.encode("idna").decode("ascii").lower()
	except UnicodeError:
		return None
	if text.endswith("."):
		text = text[:-1]
	if not text or text.startswith(".") or text.endswith(".") or ".." in text:
		return None
	return canonical_ip(text) or text


def canonical_ip(text: str | None) -> str | None:
	"""Canonical text of an IP literal in any form the resolver accepts, else
	``None``. IPv4-mapped IPv6 collapses to the IPv4. A rule with glob characters
	is never an IP."""
	text = (text or "").strip().lower()
	if text.startswith("[") and text.endswith("]"):
		text = text[1:-1]
	if not text or any(ch in text for ch in "*?[]"):
		return None
	if ":" in text:
		return _canonical_ipv6(text)
	value = _parse_inet_aton(text)
	return str(ipaddress.IPv4Address(value)) if value is not None else None


def _canonical_ipv6(text: str) -> str | None:
	try:
		ip = ipaddress.IPv6Address(text.split("%", 1)[0])
	except ValueError:
		return None
	return str(ip.ipv4_mapped or ip)


def _parse_inet_aton(text: str) -> int | None:
	"""Strict parser for the ``inet_aton`` grammar: 1-4 dot-separated parts, each
	decimal, ``0``-prefixed octal or ``0x`` hex; the last part fills the remaining
	bytes. ``socket.inet_aton`` itself tolerates trailing junk, so it is not used."""
	parts = text.split(".")
	if len(parts) > 4:
		return None
	numbers = [_inet_part(p) for p in parts]
	if None in numbers:
		return None
	*head, last = numbers
	if any(n > 255 for n in head) or last >= 256 ** (5 - len(parts)):
		return None
	value = last
	for index, n in enumerate(head):
		value |= n << (8 * (3 - index))
	return value


def _inet_part(part: str) -> int | None:
	if _HEX_PART.fullmatch(part):
		return int(part, 16)
	if _OCTAL_PART.fullmatch(part):
		return int(part, 8)
	if _DECIMAL_PART.fullmatch(part):
		return int(part)
	return None


def _parse_rules(raw: str) -> tuple[list[str], list[str]]:
	"""Split the rules text into normalised ``(allows, denies)``. A rule that will
	not normalise is kept lower-cased: dropping a deny rule would fail open."""
	allows: list[str] = []
	denies: list[str] = []
	for line in raw.splitlines():
		rule = line.strip().lower()
		if not rule or rule.startswith("#"):
			continue
		is_deny = rule.startswith("!")
		rule = rule[1:].strip() if is_deny else rule
		if not rule:
			continue
		(denies if is_deny else allows).append(normalise_host(rule) or rule)
	return allows, denies


def _host_matches(host: str, rule: str) -> bool:
	if not rule:
		return False
	return fnmatch.fnmatch(host, rule) or host == rule or host.endswith("." + rule)


def input_schema(row, action: str):
	"""The cached MCP ``inputSchema`` for ``action`` on ``row`` (see the
	``tools_cache`` contract in ``broker.py``), or ``None`` when the cache is
	missing, unparsable, or does not list the action."""
	cache = row.get("tools_cache")
	if not cache:
		return None
	try:
		data = json.loads(cache) if isinstance(cache, str) else cache
	except (ValueError, TypeError):
		return None
	tools = data.get("tools") if isinstance(data, dict) else data
	for tool in tools or []:
		if isinstance(tool, dict) and tool.get("name") == action:
			return tool.get("inputSchema")
	return None
