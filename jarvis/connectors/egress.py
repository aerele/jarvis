"""Egress allow/deny rules for connector hosts (``Jarvis Settings.connector_egress_rules``).

One comparison space. A name becomes ASCII labels exactly as ``getaddrinfo``
sends it (the stdlib ``idna`` codec), lower-cased, with one trailing dot dropped
and empty labels refused. An IP becomes an ``ipaddress`` object. Every rule goes
through the same steps as a host. Each function is total: hostile input gets a
fail-closed answer, never an exception. No frappe import here.

Grammar, one rule per line (blank lines and ``#`` comments ignored; ``!`` = deny):

  * an IP in any spelling the resolver accepts, or a CIDR range (``34.1.2.0/24``,
    ``2606:4700:4700::/48``): network containment, IPv4-mapped IPv6 included.
  * a glob (``*``, ``?``, ``[...]``): ``fnmatch`` on the ASCII name, or on each text
    spelling of an IP (IPv4, short, full and unpadded IPv6, mapped forms).
  * a plain name: that name or any subdomain.
  * a label mixing a wildcard with non-ASCII letters has no ASCII form. As a DENY
    it blocks every name with an ``xn--`` label under the rule's plain suffix (it
    over-blocks on purpose); as an ALLOW it matches nothing.
  * anything else is kept as lower-cased text, never dropped.

Deny wins; any allow rule turns on allow-list mode. Only deny rules apply to a
resolved address. :func:`rule_problems` names rules an admin should rewrite.
"""

from __future__ import annotations

import fnmatch
import functools
import ipaddress
import re
import unicodedata

from jarvis.connectors.egress_ip import MAX_HOST_LENGTH, cidr, ip_forms, ip_texts, parse_ip, to_ip

_GLOB_CHARS = "*?["
# The label separators of the stdlib idna codec (``encodings.idna.dots``).
_DOTS = re.compile("[.\u3002\uff0e\uff61]")
# IDNA2003 maps these away (``ß`` -> ``ss``) but IDNA2008 keeps them, so a deny rule
# naming one also blocks the IDNA2008 spelling (``straße`` -> ``xn--strae-oqa``).
_DEVIATIONS = frozenset("\u00df\u03c2\u200c\u200d")


def egress_match(host, raw) -> bool:
	"""Whether ``host`` is permitted by the rules text ``raw``. Empty rules allow
	everything (the SSRF guard still blocks private and metadata addresses). A host
	that cannot be normalised is denied whenever any rule exists."""
	if not raw:
		return True
	allows, denies = _rules(str(raw))
	if not allows and not denies:
		return True
	target = _target(host)
	if target is None:
		return False
	if any(rule.matches(target) for rule in denies):
		return False
	return not allows or any(rule.matches(target) for rule in allows)


def egress_address_match(address, raw) -> bool:
	"""Check a RESOLVED address against the DENY rules, so a name whose DNS record
	points at a denied IP cannot slip past an IP rule. Allow rules are about names:
	applying them here would deny every call in allow-list mode."""
	if not raw:
		return True
	denies = _rules(str(raw))[1]
	if not denies:
		return True
	ip = to_ip(address)
	if ip is None:
		return False
	return not any(rule.matches(ip) for rule in denies)


def normalise_host(host) -> str | None:
	"""The comparison form of a host as text (canonical IP or ASCII name), or ``None``."""
	target = _target(host)
	return None if target is None else str(target)


def canonical_ip(text) -> str | None:
	"""Canonical text of an IP literal in any form the resolver accepts, else ``None``.
	IPv4-mapped IPv6 collapses to the IPv4. Text with glob characters is never an IP."""
	ip = to_ip(text)
	return None if ip is None else str(ip)


def rule_problems(rules_text) -> list[str]:
	"""One plain-English warning per rule that will not do what it looks like, for the
	settings form. The egress check never calls this."""
	if not isinstance(rules_text, str):
		return []
	problems = []
	for deny, text in _rule_lines(rules_text):
		shown = ("!" if deny else "") + (text if len(text) <= 80 else text[:77] + "...")
		problem = _problem(shown, deny, _parse_rule(text, deny))
		if problem:
			problems.append(problem)
	return problems


class _PatternRule:
	"""A plain name (itself or a subdomain) or a glob. ``raw`` marks text kept as typed
	because it is not a readable name, IP or CIDR range."""

	def __init__(self, pattern: str, raw: bool = False):
		self.pattern = pattern
		self.raw = raw
		self.glob = _has_glob(pattern)

	def matches(self, target) -> bool:
		if not isinstance(target, str):
			return self.glob and any(_glob_match(text, self.pattern) for text in ip_texts(target))
		if self.glob:
			return _glob_match(target, self.pattern)
		return target == self.pattern or target.endswith("." + self.pattern)


class _NetworkRule:
	"""An IP literal (a one-address network) or a CIDR range."""

	def __init__(self, network):
		self.network = network

	def matches(self, target) -> bool:
		if isinstance(target, str):
			return False
		return any(ip.version == self.network.version and ip in self.network for ip in ip_forms(target))


class _ForeignRule:
	"""A rule with a label that mixes a wildcard and non-ASCII letters. A deny blocks
	every name with an ``xn--`` label under ``suffix``; an allow matches nothing."""

	def __init__(self, suffix: str, deny: bool):
		self.suffix = suffix
		self.deny = deny

	def matches(self, target) -> bool:
		if not self.deny or not isinstance(target, str):
			return False
		if self.suffix:
			if not target.endswith("." + self.suffix):
				return False
			target = target[: -len(self.suffix) - 1]
		return any(label.startswith("xn--") for label in target.split("."))


@functools.lru_cache(maxsize=32)
def _rules(raw: str) -> tuple[tuple, tuple]:
	"""Parse the rules text into ``(allows, denies)``. Cached: it is read per call."""
	allows: list = []
	denies: list = []
	for deny, text in _rule_lines(raw):
		(denies if deny else allows).extend(_parse_rule(text, deny))
	return tuple(allows), tuple(denies)


def _rule_lines(raw: str):
	for line in raw.splitlines():
		text = line.strip().lower()
		if not text or text.startswith("#"):
			continue
		deny = text.startswith("!")
		text = text[1:].strip() if deny else text
		if text:
			yield deny, text


def _parse_rule(text: str, deny: bool) -> list:
	"""The rules one line stands for: one, plus the IDNA2008 spelling for a deny."""
	if len(text) > MAX_HOST_LENGTH:
		return [_PatternRule(text, raw=True)]
	if ":" in text and text.startswith("[") and text.endswith("]"):
		text = text[1:-1]
	if "/" in text:
		network = cidr(text)
		return [_NetworkRule(network) if network else _PatternRule(text, raw=True)]
	labels, trailing = _split(text)
	encoded = [_encode(label) for label in labels]
	if any(code is None and not _foreign(label, code) for label, code in zip(labels, encoded, strict=True)):
		return [_PatternRule(text, raw=True)]
	rules = [_name_rule(text, labels, encoded, trailing, deny)]
	alternate = [_idna2008(label) or code for label, code in zip(labels, encoded, strict=True)]
	if deny and alternate != encoded:
		rules.append(_name_rule(text, labels, alternate, trailing, deny))
	return rules


def _name_rule(text: str, labels: list, encoded: list, trailing: bool, deny: bool):
	pairs = list(zip(labels, encoded, strict=True))
	if any(_foreign(label, code) for label, code in pairs):
		return _ForeignRule(_plain_suffix(encoded, trailing), deny)
	name = _join(encoded, trailing)
	if name is None:
		return _PatternRule(text, raw=True)
	ip = parse_ip(name)
	if ip is not None:
		return _NetworkRule(ipaddress.ip_network((ip, ip.max_prefixlen)))
	return _PatternRule(name)


def _problem(shown: str, deny: bool, rules: list) -> str | None:
	foreign = next((rule for rule in rules if isinstance(rule, _ForeignRule)), None)
	if foreign and deny:
		where = foreign.suffix or "any domain"
		return (
			f"{shown}: a wildcard mixed with non-English letters cannot be matched exactly, so it "
			f"blocks every non-English name under {where}. Write the xn-- spelling, and any "
			"plain-English spelling, as their own rules."
		)
	if foreign:
		return (
			f"{shown}: a wildcard mixed with non-English letters cannot be matched, so this allow "
			"rule allows nothing. Write the xn-- spelling instead."
		)
	patterns = [rule for rule in rules if isinstance(rule, _PatternRule)]
	if any(rule.raw for rule in patterns):
		return (
			f"{shown}: not a valid host name, IP address or CIDR range, so it only matches this exact text."
		)
	if any(rule.glob and ":" in rule.pattern for rule in patterns):
		return (
			f"{shown}: an IPv6 wildcard matches only the short, full and unpadded spellings. "
			"Use a CIDR range such as 2606:4700:4700::/48."
		)
	if len(patterns) > 1:
		return f"{shown}: also blocks the other international spelling {patterns[1].pattern}."
	return None


def _target(host):
	"""A host as a comparison target: an IP object, an ASCII name, or ``None``."""
	if not isinstance(host, str):
		return None
	text = host.strip().lower()
	if not text or len(text) > MAX_HOST_LENGTH:
		return None
	name = _ascii_name(text)
	if name is None:
		return None
	ip = to_ip(name)
	return name if ip is None else ip


def _ascii_name(text: str) -> str | None:
	labels, trailing = _split(text)
	encoded = [_encode(label) for label in labels]
	return None if None in encoded else _join(encoded, trailing)


def _split(text: str) -> tuple[list[str], bool]:
	"""Labels split the way the idna codec splits them, and whether a dot ended the text."""
	labels = _DOTS.split(text)
	trailing = len(labels) > 1 and labels[-1] == ""
	if trailing:
		labels.pop()
	return labels, trailing


def _encode(label: str) -> str | None:
	"""One label through the codec ``getaddrinfo`` uses (nameprep, then punycode)."""
	try:
		return label.encode("idna").decode("ascii").lower()
	except (UnicodeError, ValueError):
		return None


def _join(encoded: list, trailing: bool) -> str | None:
	"""Join encoded labels, drop ONE trailing dot after IDNA, refuse empty labels."""
	name = ".".join(encoded) + ("." if trailing else "")
	if name.endswith("."):
		name = name[:-1]
	if not name or "" in name.split("."):
		return None
	return name


def _foreign(label: str, code: str | None) -> bool:
	"""A typed non-ASCII label with a glob character: its punycode carries the glob
	(``ü*``), or it does not encode at all (nameprep refuses ``*`` at the edge of a
	right-to-left label)."""
	if label.isascii():
		return False
	if code is None:
		return _has_glob(unicodedata.normalize("NFKC", label))
	return code.startswith("xn--") and _has_glob(code)


def _plain_suffix(encoded: list, trailing: bool) -> str:
	"""The trailing run of glob-free labels, as a name ("" when there is none)."""
	suffix: list = []
	for code in reversed(encoded):
		if code is None or _has_glob(code):
			break
		suffix.insert(0, code)
	return _join(suffix, trailing) or ""


def _idna2008(label: str) -> str | None:
	if _DEVIATIONS.isdisjoint(label):
		return None
	try:
		return "xn--" + unicodedata.normalize("NFKC", label).encode("punycode").decode("ascii")
	except (UnicodeError, ValueError):
		return None


def _has_glob(text: str) -> bool:
	return any(ch in text for ch in _GLOB_CHARS)


def _glob_match(text: str, pattern: str) -> bool:
	regex = _compile(pattern)
	return text == pattern if regex is None else regex.match(text) is not None


@functools.lru_cache(maxsize=256)
def _compile(pattern: str):
	try:
		return re.compile(fnmatch.translate(pattern))
	except (re.error, ValueError, OverflowError, RecursionError):
		return None
