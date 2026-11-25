"""IP literals for the egress rules: every spelling the resolver accepts, CIDR
ranges, and the text forms a glob rule is tried against. Total: bad input gives
``None``, never an exception. No frappe import here.
"""

from __future__ import annotations

import functools
import ipaddress
import re

# DNS name limit. Longer input is refused BEFORE the idna codec, which is quadratic
# on one long non-ASCII label.
MAX_HOST_LENGTH = 253
# Part lengths are capped to what a 32-bit value can need, so a hostile rule or host
# cannot reach ``int()`` with thousands of digits (it raises past ~4,300).
_HEX_PART = re.compile(r"0x[0-9a-f]{1,8}", re.ASCII)
_OCTAL_PART = re.compile(r"0[0-7]{0,11}", re.ASCII)
_DECIMAL_PART = re.compile(r"[1-9][0-9]{0,9}", re.ASCII)
_PREFIX = re.compile(r"[0-9]{1,3}", re.ASCII)


def cidr(text: str):
	"""``34.1.2.0/24`` or ``2606:4700:4700::/48`` as a network (host bits allowed,
	loose IPv4 spellings accepted), else ``None``."""
	address, _, prefix = text.partition("/")
	ip = parse_ip(address)
	if ip is None or not _PREFIX.fullmatch(prefix):
		return None
	try:
		return ipaddress.ip_network((ip, int(prefix)), strict=False)
	except ValueError:
		return None


def to_ip(text):
	"""``parse_ip`` with IPv4-mapped IPv6 collapsed to the IPv4."""
	ip = parse_ip(text)
	if ip is not None and ip.version == 6 and ip.ipv4_mapped is not None:
		return ip.ipv4_mapped
	return ip


def parse_ip(text):
	"""An IP literal in any spelling the resolver accepts, else ``None``. Brackets and
	an IPv6 zone are ignored; text with glob characters is never an IP."""
	if not isinstance(text, str):
		return None
	text = text.strip().lower()
	if text.startswith("[") and text.endswith("]"):
		text = text[1:-1]
	if not text or len(text) > MAX_HOST_LENGTH or any(ch in text for ch in "*?[]"):
		return None
	if ":" in text:
		try:
			return ipaddress.IPv6Address(text.split("%", 1)[0])
		except ValueError:
			return None
	value = _parse_inet_aton(text)
	return None if value is None else ipaddress.IPv4Address(value)


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


def ip_forms(ip) -> tuple:
	"""An address and, for IPv4, its IPv4-mapped IPv6 twin."""
	if ip.version == 4:
		return (ip, ipaddress.IPv6Address((0xFFFF << 32) | int(ip)))
	return (ip,)


@functools.lru_cache(maxsize=64)
def ip_texts(ip) -> tuple[str, ...]:
	"""Every text spelling a glob is tried against, built from the integer so the
	result does not depend on how this Python prints a mapped address."""
	texts = []
	for addr in ip_forms(ip):
		texts.append(str(addr))
		if addr.version == 6:
			hextets = [(int(addr) >> (16 * (7 - i))) & 0xFFFF for i in range(8)]
			texts.append(":".join(f"{h:04x}" for h in hextets))
			texts.append(":".join(f"{h:x}" for h in hextets))
	if ip.version == 4:
		n = int(ip)
		texts += [f"::ffff:{ip}", f"::ffff:{n >> 16:x}:{n & 0xFFFF:x}"]
	return tuple(texts)
