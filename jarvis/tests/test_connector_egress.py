"""Unit tests for ``jarvis.connectors.egress`` - the connector egress allow/deny
host match. Plain ``unittest``: the module is frappe-free.

The core check is property-style: for each (rule, host) pair, every spelling the
resolver treats as the same host (built by ``_spellings``) must get the same answer.
"""

from __future__ import annotations

import ipaddress
import random
import socket
import time
import unittest

from jarvis.connectors import egress, policy

_DOT_LIKES = ("\u3002", "\uff0e", "\uff61")


def _spellings(host: str) -> set[str]:
	"""Spellings ``getaddrinfo`` resolves to the same host as ``host``."""
	try:
		ip = ipaddress.ip_address(host)
	except ValueError:
		return _name_spellings(host)
	return _ipv4_spellings(ip) if ip.version == 4 else _ipv6_spellings(ip)


def _name_spellings(host: str) -> set[str]:
	ascii_name = host.encode("idna").decode("ascii")
	out: set[str] = set()
	for base in {host, ascii_name, _unicode(ascii_name)}:
		out |= {base, base + ".", base.upper(), base.title(), _fullwidth(base)}
		out.add(base.replace(".", "\u00ad.", 1))  # nameprep maps a soft hyphen to nothing
		for dot in _DOT_LIKES:
			out |= {base.replace(".", dot), base + dot}
	return out


def _ipv4_spellings(ip) -> set[str]:
	a, b, c, d = ip.packed
	n = int(ip)
	mapped = ipaddress.IPv6Address((0xFFFF << 32) | n)
	return {
		str(ip),
		f"{ip}.",
		f"{a:#x}.{b:#x}.{c:#x}.{d:#x}",
		f"0{a:o}.0{b:o}.0{c:o}.0{d:o}",
		f"{a}.{b}.{c << 8 | d}",
		f"{a}.{b << 16 | c << 8 | d}",
		str(n),
		f"{n:#x}",
		f"::ffff:{ip}",
		f"[::ffff:{ip}]",
		mapped.exploded,
		f"::ffff:{n >> 16:x}:{n & 0xFFFF:x}",
		_fullwidth(str(ip)),
	}


def _ipv6_spellings(ip) -> set[str]:
	hextets = ip.exploded.split(":")
	unpadded = ":".join(h.lstrip("0") or "0" for h in hextets)
	return {str(ip), ip.exploded, unpadded, ip.exploded.upper(), f"[{ip}]", f"{ip}%0"}


def _unicode(name: str) -> str:
	labels = []
	for label in name.split("."):
		try:
			labels.append(label[4:].encode("ascii").decode("punycode") if label.startswith("xn--") else label)
		except UnicodeError:
			labels.append(label)
	return ".".join(labels)


def _fullwidth(text: str) -> str:
	return "".join(chr(ord(ch) + 0xFEE0) if ch.isascii() and ch.isalnum() else ch for ch in text)


# (rules, host): the host is DENIED in every spelling.
DENIED = [
	("!evil.com", "evil.com"),
	("!evil.com", "sub.evil.com"),
	("!*.example.com", "a.b.example.com"),
	("!evi?.com", "evil.com"),
	("!b\u00fccher.example", "b\u00fccher.example"),
	("!xn--bcher-kva.example", "sub.b\u00fccher.example"),
	("!*.b\u00fccher.example", "a.b\u00fccher.example"),
	("!stra\u00dfe.de", "stra\u00dfe.de"),
	("!stra\u00dfe.de", "xn--strae-oqa.de"),
	("!*\u00fc*.com", "\u00fcber.com"),
	("!*\u00fc*.com", "a.xn--tda.b.com"),
	("!34.1.2.3", "34.1.2.3"),
	("!0x22.1.2.3", "34.1.2.3"),
	("!34.1.2.0/24", "34.1.2.200"),
	("!0x22.1.2.0/24", "34.1.2.200"),
	("!34.1.2.*", "34.1.2.7"),
	("!::ffff:34.1.2.0/120", "34.1.2.9"),
	("!2606:4700:4700::1111", "2606:4700:4700::1111"),
	("![2606:4700:4700:0:0:0:0:1111]", "2606:4700:4700::1111"),
	("!2606:4700:4700::/48", "2606:4700:4700::1111"),
	("!2606:4700:4700::*", "2606:4700:4700::1111"),
	("!2606:4700:4700:0:*", "2606:4700:4700::1111"),
	("!2606:4700:4700:0000:*", "2606:4700:4700::1111"),
	("*.example.com\n!secret.example.com", "secret.example.com"),
	("api.github.com", "api.stripe.com"),
]

# (rules, host): the host is ALLOWED in every spelling.
ALLOWED = [
	("!evil.com", "api.github.com"),
	("!evil.com", "notevil.com"),
	("!evil.com", "evil.com.au"),
	("!*.example.com", "evil-example.com"),
	("!*.example.com", "example.com"),
	("!*\u00fc*.com", "plain.com"),
	("!*\u00fc*.com", "\u00fcber.org"),
	("!34.1.2.0/24", "34.1.3.1"),
	("!2606:4700:4700::/48", "2606:4700:4701::1"),
	("!2606:4700:4700:0:*", "2606:4700:4701::1"),
	("api.github.com", "api.github.com"),
	("githubcopilot.com", "mcp.githubcopilot.com"),
	("*.atlassian.net", "mcp.atlassian.net"),
	("*.example.com\n!secret.example.com", "ok.example.com"),
	("34.1.2.3", "34.1.2.3"),
	("b\u00fccher.example", "xn--bcher-kva.example"),
]


class TestEquivalentSpellings(unittest.TestCase):
	def test_deny_holds_for_every_spelling(self):
		for rules, host in DENIED:
			for spelling in _spellings(host):
				with self.subTest(rules=rules, host=spelling):
					self.assertFalse(egress.egress_match(spelling, rules))

	def test_allow_holds_for_every_spelling(self):
		for rules, host in ALLOWED:
			for spelling in _spellings(host):
				with self.subTest(rules=rules, host=spelling):
					self.assertTrue(egress.egress_match(spelling, rules))

	def test_plain_rule_spellings_deny_the_host(self):
		for rule, host in (
			("evil.com", "sub.evil.com"),
			("34.1.2.3", "34.1.2.3"),
			("b\u00fccher.de", "b\u00fccher.de"),
		):
			for spelling in _spellings(rule):
				with self.subTest(rule=spelling):
					self.assertFalse(egress.egress_match(host, "!" + spelling))

	def test_resolved_address_deny_holds_for_ip_rules(self):
		for rules, host in DENIED:
			if (
				egress.canonical_ip(host)
				and not rules.startswith("*")
				and "\n" not in rules
				and rules[0] == "!"
			):
				with self.subTest(rules=rules, address=host):
					self.assertFalse(egress.egress_address_match(host, rules))

	def test_name_spelling_parity_with_the_resolver_codec(self):
		# The comparison space IS what getaddrinfo sends: the stdlib idna codec.
		for _rules, host in DENIED + ALLOWED:
			if egress.canonical_ip(host):
				continue
			for spelling in _spellings(host):
				with self.subTest(host=spelling):
					sent = spelling.lower().encode("idna").decode("ascii").lower()
					self.assertEqual(
						egress.normalise_host(spelling), sent[:-1] if sent.endswith(".") else sent
					)


class TestGrammar(unittest.TestCase):
	def test_empty_rules_allow_all(self):
		for raw in (None, "", "  \n # comment\n", "!"):
			self.assertTrue(egress.egress_match("api.githubcopilot.com", raw))
		self.assertTrue(egress.egress_match("evil..com", "# only a comment"))

	def test_policy_reexports_the_egress_names(self):
		self.assertIs(policy.egress_match, egress.egress_match)
		self.assertIs(policy.egress_address_match, egress.egress_address_match)
		self.assertIs(policy.normalise_host, egress.normalise_host)
		self.assertIs(policy.canonical_ip, egress.canonical_ip)

	def test_unnormalisable_host_denied_when_rules_exist(self):
		for host in ("evil.com..", ".evil.com", "a..b", "...", ".", "evil.com\u3002\u3002", "", None, b"x"):
			with self.subTest(host=host):
				self.assertIsNone(egress.normalise_host(host))
				self.assertFalse(egress.egress_match(host, "!other.com"))
				self.assertFalse(egress.egress_match(host, "api.github.com"))

	def test_dot_after_idna_is_dropped_once(self):
		# U+2024 becomes a dot inside nameprep, after the label split.
		self.assertEqual(egress.normalise_host("evil.com\u2024"), "evil.com")
		self.assertIsNone(egress.normalise_host("evil.com\u2024."))

	def test_ip_rules_never_match_a_name(self):
		self.assertTrue(egress.egress_match("x.34.1.2.3.example", "!34.1.2.3"))
		self.assertTrue(egress.egress_match("example.com", "!34.1.2.0/24"))

	def test_malformed_ip_is_a_name_not_an_ip(self):
		for text in ("08.1.2.3", "1.2.3.4.5", "256.1.1.1", "0x", "1.2.3.4x", "1.-2.3.4", "1.2.3.*", "[::1"):
			with self.subTest(text=text):
				self.assertIsNone(egress.canonical_ip(text))

	def test_bad_cidr_is_kept_not_dropped(self):
		self.assertTrue(egress.egress_match("34.1.2.3", "!34.1.2.0/99"))
		self.assertFalse(egress.egress_match("api.github.com", "34.1.2.0/99"))
		self.assertTrue(egress.egress_address_match("34.1.2.3", "!34.1.2.0/" + "9" * 5000))


class TestReportedFindings(unittest.TestCase):
	def test_a_one_undecodable_label_does_not_open_a_unicode_glob_deny(self):
		# xn--strae-oqa does not round-trip through the IDNA2003 decoder.
		self.assertFalse(egress.egress_match("xn--bcher-kva.xn--strae-oqa.com", "!*\u00fc*.com"))
		self.assertFalse(egress.egress_match("xn--zz.com", "!*\u00fc*.com"))

	def test_b_rule_and_host_use_the_same_mapping(self):
		self.assertFalse(egress.egress_match("STRASSE.de", "!stra\u00dfe.de"))
		self.assertFalse(egress.egress_match("\u03a3\u039f\u03a3.gr", "!\u03c3\u03bf\u03c2.gr"))
		self.assertFalse(egress.egress_match("xn--0xagb.gr", "!\u03c3\u03bf\u03c2.gr"))
		self.assertFalse(egress.egress_match("a.xn--strae-oqa.de", "!*.stra\u00dfe.de"))

	def test_c_ipv6_globs_match_exploded_and_unpadded_text(self):
		for rule in ("!2606:4700:4700:0:*", "!2606:4700:4700:0000:*", "!2606:4700:4700::*"):
			with self.subTest(rule=rule):
				self.assertFalse(egress.egress_match("2606:4700:4700::1111", rule))
				self.assertFalse(egress.egress_address_match("2606:4700:4700::1111", rule))
				self.assertTrue(egress.egress_address_match("2606:4700:4701::1111", rule))

	def test_mapped_address_matches_both_spellings_on_any_python(self):
		self.assertFalse(egress.egress_address_match("::ffff:34.1.2.5", "!::ffff:34.1.2.*"))
		self.assertFalse(egress.egress_address_match("34.1.2.5", "!::ffff:2201:*"))


class TestForeignGlobRules(unittest.TestCase):
	def test_deny_blocks_every_non_ascii_name_under_the_suffix(self):
		rules = "!*\u00fc*.com"
		self.assertFalse(egress.egress_match("abc\u00fccd.com", rules))
		self.assertFalse(egress.egress_match("\u00e9t\u00e9.com", rules))  # over-blocks on purpose
		self.assertTrue(egress.egress_match("plain.com", rules))
		self.assertTrue(egress.egress_match("\u00fcber.org", rules))

	def test_ascii_alternative_in_a_mixed_class_is_not_blocked(self):
		# Behaviour note: write the ASCII spelling as its own rule.
		self.assertFalse(egress.egress_match("\u00fcber.com", "![\u00fcu]ber.com"))
		self.assertTrue(egress.egress_match("uber.com", "![\u00fcu]ber.com"))
		self.assertFalse(egress.egress_match("uber.com", "![\u00fcu]ber.com\n!uber.com"))

	def test_allow_matches_nothing(self):
		self.assertFalse(egress.egress_match("\u00fcber.com", "*\u00fc*.com"))
		self.assertFalse(egress.egress_match("plain.com", "*\u00fc*.com"))

	def test_ascii_glob_on_punycode_is_a_normal_glob(self):
		self.assertTrue(egress.egress_match("\u00fcber.com", "xn--*.com"))
		self.assertFalse(egress.egress_match("\u00fcber.com", "!xn--*.com"))
		self.assertTrue(egress.egress_match("plain.com", "!xn--*.com"))

	def test_fullwidth_wildcard_is_a_wildcard(self):
		self.assertFalse(egress.egress_match("a.evil.com", "!\uff0a.evil.com"))


class TestRuleProblems(unittest.TestCase):
	def test_clean_rules_have_no_problems(self):
		raw = "api.github.com\n*.atlassian.net\n!34.1.2.0/24\n!2606:4700:4700::/48\n# note\n"
		self.assertEqual(egress.rule_problems(raw), [])
		self.assertEqual(egress.rule_problems(None), [])

	def test_names_each_problem_rule(self):
		problems = egress.rule_problems(
			"!*\u00fc*.com\n*\u00fc*.com\n!2606:4700::*\n!stra\u00dfe.de\n!34.1.2.0/99"
		)
		self.assertEqual(len(problems), 5)
		self.assertIn("blocks every non-English name under com", problems[0])
		self.assertIn("allows nothing", problems[1])
		self.assertIn("CIDR", problems[2])
		self.assertIn("xn--strae-oqa.de", problems[3])
		self.assertIn("only matches this exact text", problems[4])

	def test_hostile_rules_never_raise(self):
		raw = "!" + "9" * 5000 + "\n" + "\u00fc." * 8192 + "\n![\n!*[\u00fc-\n!\u200d*"
		self.assertEqual(len(egress.rule_problems(raw)), 4)


class TestHostileInput(unittest.TestCase):
	def _fast(self, call):
		started = time.monotonic()
		call()
		self.assertLess(time.monotonic() - started, 0.5)

	def test_oversized_hosts_and_rules_are_fast(self):
		self._fast(lambda: self.assertFalse(egress.egress_match("\u00fc" * 16384, "!other.com")))
		self._fast(lambda: self.assertFalse(egress.egress_match("\u00fc." * 8192, "!other.com")))
		self._fast(lambda: self.assertTrue(egress.egress_match("good.example.com", "!" + "\u00fc." * 8192)))
		self.assertIsNone(egress.normalise_host("\u00fc" * 4096))

	def test_huge_numeric_parts_never_raise(self):
		rule = "!" + "9" * 5000 + ".1.2.3"
		self._fast(lambda: self.assertTrue(egress.egress_match("good.example.com", rule)))
		self.assertTrue(egress.egress_address_match("34.1.2.3", rule))
		self.assertIsNone(egress.canonical_ip("9" * 5000))
		self.assertIsNone(egress.canonical_ip("0x" + "f" * 5000))
		self.assertIsNone(egress.canonical_ip("0" + "7" * 5000))
		self.assertFalse(egress.egress_match("9" * 5000 + ".example", "!other.com"))

	def test_random_input_never_raises(self):
		alphabet = "a.:*?[]!-/0x9\u00fc\u00df\u3002\uff0e\u2024\u00ad\u200d\u0301 %#"
		rng = random.Random(2847)
		for _ in range(3000):
			host = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
			rule = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
			egress.egress_match(host, rule)
			egress.egress_address_match("34.1.2.3", rule)
			egress.egress_address_match(host, rule)
			egress.rule_problems(rule)


class TestInetAtonParity(unittest.TestCase):
	"""The loose-IPv4 parser must agree with the C resolver on every clean form."""

	def test_matches_socket_inet_aton(self):
		forms = [
			"127.1",
			"0x7f.1",
			"2130706433",
			"0x7f000001",
			"0177.0.0.1",
			"10.1.2",
			"34.66051",
			"1.2.3.4",
			"0.0.0.0",
			"0",
			"255.255.255.255",
			"4294967295",
			"0x22.1.2.3",
			"08.1.2.3",
			"1.2.3.4.5",
			"256.1.1.1",
			"4294967296",
			"1.2.65536",
			"0xg",
			"1..2",
		]
		for text in forms:
			with self.subTest(text=text):
				try:
					expected = socket.inet_ntoa(socket.inet_aton(text))
				except OSError:
					expected = None
				self.assertEqual(egress.canonical_ip(text), expected)


class TestEgressAddressMatch(unittest.TestCase):
	def test_ip_deny_rule_blocks_resolved_address(self):
		self.assertFalse(egress.egress_address_match("34.1.2.3", "!34.1.2.3"))
		self.assertFalse(egress.egress_address_match("::ffff:34.1.2.3", "!0x22.1.2.3"))
		self.assertTrue(egress.egress_address_match("34.1.2.4", "!34.1.2.3"))

	def test_cidr_deny_rules_block_resolved_address(self):
		self.assertFalse(egress.egress_address_match("34.1.2.5", "!34.1.2.0/24"))
		self.assertFalse(egress.egress_address_match("::ffff:34.1.2.5", "!34.1.2.0/24"))
		self.assertTrue(egress.egress_address_match("34.1.3.5", "!34.1.2.0/24"))

	def test_hostname_and_allow_rules_do_not_apply_to_addresses(self):
		self.assertTrue(egress.egress_address_match("34.1.2.3", "!evil.com\napi.github.com"))
		self.assertTrue(egress.egress_address_match("34.1.2.3", "*.atlassian.com"))
		self.assertTrue(egress.egress_address_match("34.1.2.3", "34.1.2.0/24"))
		self.assertTrue(egress.egress_address_match("34.1.2.3", None))
		self.assertTrue(egress.egress_address_match("34.1.2.3", "# comment"))

	def test_unreadable_address_is_denied_when_deny_rules_exist(self):
		self.assertFalse(egress.egress_address_match("not-an-ip", "!evil.com"))
		self.assertTrue(egress.egress_address_match("not-an-ip", "api.github.com"))


if __name__ == "__main__":
	unittest.main()
