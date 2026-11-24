"""Unit tests for ``jarvis.connectors.policy`` - the allowed-actions gate and the
argument check. Plain ``unittest``; the policy module is frappe-free, so a simple
duck-typed row (a dict-backed ``.get``) drives it with no bench.
"""

from __future__ import annotations

import json
import socket
import unittest

from jarvis.connectors import policy


class _Row:
	"""Minimal row/child stand-in exposing ``.get(field)`` like a Frappe doc."""

	def __init__(self, **fields):
		self._f = fields

	def get(self, key, default=None):
		return self._f.get(key, default)


def _row(actions=None, tools_cache=None):
	children = [_Row(**a) for a in (actions or [])]
	return _Row(allowed_actions=children, tools_cache=tools_cache)


class TestActionGate(unittest.TestCase):
	def test_unknown_action_denied(self):
		code, _ = policy.action_decision(_row(actions=[{"action": "other", "allowed": 1}]), "get_x")
		self.assertEqual(code, "action_unknown")

	def test_explicitly_allowed(self):
		self.assertIsNone(policy.action_decision(_row(actions=[{"action": "get_x", "allowed": 1}]), "get_x"))

	def test_switched_off_read_only_action_is_denied(self):
		# allowed=0 on a read-only row means the user unchecked it; read_only
		# must never re-grant it.
		row = _row(actions=[{"action": "get_x", "allowed": 0, "read_only": 1, "destructive": 0}])
		code, message = policy.action_decision(row, "get_x")
		self.assertEqual(code, "action_denied")
		self.assertIn("get_x", message)

	def test_allowed_read_only_action_runs(self):
		row = _row(actions=[{"action": "get_x", "allowed": 1, "read_only": 1, "destructive": 0}])
		self.assertIsNone(policy.action_decision(row, "get_x"))

	def test_destructive_not_allowed_is_denied(self):
		row = _row(actions=[{"action": "wipe", "allowed": 0, "read_only": 0, "destructive": 1}])
		code, _ = policy.action_decision(row, "wipe")
		self.assertEqual(code, "action_denied")

	def test_destructive_but_read_only_flag_still_needs_explicit_allow(self):
		# A destructive action is NOT auto-allowed even if read_only is somehow set;
		# destructive must be explicitly allowed.
		row = _row(actions=[{"action": "wipe", "allowed": 0, "read_only": 1, "destructive": 1}])
		code, _ = policy.action_decision(row, "wipe")
		self.assertEqual(code, "action_denied")

	def test_not_marked_anything_is_denied(self):
		row = _row(actions=[{"action": "get_x", "allowed": 0, "read_only": 0, "destructive": 0}])
		code, _ = policy.action_decision(row, "get_x")
		self.assertEqual(code, "action_denied")


class TestArgumentPolicy(unittest.TestCase):
	def _cache(self, name, input_schema):
		return json.dumps({"tools": [{"name": name, "inputSchema": input_schema}]})

	def test_no_cache_skips_validation(self):
		self.assertIsNone(policy.argument_error(_row(tools_cache=None), "get_x", {"a": 1}))

	def test_unknown_tool_in_cache_skips(self):
		row = _row(tools_cache=self._cache("other", {"type": "object", "required": ["a"]}))
		self.assertIsNone(policy.argument_error(row, "get_x", {}))

	def test_missing_required_argument_flagged(self):
		row = _row(tools_cache=self._cache("get_x", {"type": "object", "required": ["repo"]}))
		res = policy.argument_error(row, "get_x", {})
		self.assertIsNotNone(res)
		self.assertEqual(res[0], "invalid_arguments")

	def test_valid_arguments_pass(self):
		row = _row(
			tools_cache=self._cache(
				"get_x", {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]}
			)
		)
		self.assertIsNone(policy.argument_error(row, "get_x", {"repo": "acme/x"}))

	def test_bare_array_cache_shape_accepted(self):
		# tools_cache may be a bare array of tool objects, not just {"tools": [...]}
		cache = json.dumps([{"name": "get_x", "inputSchema": {"type": "object", "required": ["a"]}}])
		row = _row(tools_cache=cache)
		self.assertIsNotNone(policy.argument_error(row, "get_x", {}))

	def test_dict_cache_passed_through_not_string(self):
		row = _row(
			tools_cache={"tools": [{"name": "get_x", "inputSchema": {"type": "object", "required": ["a"]}}]}
		)
		self.assertIsNotNone(policy.argument_error(row, "get_x", {}))


class TestEgressMatch(unittest.TestCase):
	def test_empty_rules_allow_all(self):
		self.assertTrue(policy.egress_match("api.githubcopilot.com", None))
		self.assertTrue(policy.egress_match("api.githubcopilot.com", ""))
		self.assertTrue(policy.egress_match("api.githubcopilot.com", "  \n # comment\n"))

	def test_deny_rule_blocks(self):
		rules = "!evil.example.com"
		self.assertFalse(policy.egress_match("evil.example.com", rules))
		self.assertTrue(policy.egress_match("good.example.com", rules))

	def test_allow_list_mode_denies_unlisted(self):
		rules = "api.githubcopilot.com\napi.linear.app"
		self.assertTrue(policy.egress_match("api.linear.app", rules))
		self.assertFalse(policy.egress_match("api.stripe.com", rules))

	def test_bare_domain_matches_subdomains(self):
		self.assertTrue(policy.egress_match("mcp.githubcopilot.com", "githubcopilot.com"))

	def test_glob_matches(self):
		self.assertTrue(policy.egress_match("mcp.atlassian.net", "*.atlassian.net"))

	def test_deny_wins_over_allow(self):
		rules = "*.example.com\n!secret.example.com"
		self.assertTrue(policy.egress_match("ok.example.com", rules))
		self.assertFalse(policy.egress_match("secret.example.com", rules))

	def test_normal_hostnames_and_wildcards_unchanged(self):
		self.assertFalse(policy.egress_match("api.github.com", "!api.github.com"))
		self.assertTrue(policy.egress_match("api.github.com", "api.github.com"))
		self.assertTrue(policy.egress_match("mcp.atlassian.com", "*.atlassian.com"))
		self.assertFalse(policy.egress_match("atlassian.com.evil.io", "*.atlassian.com"))
		self.assertFalse(policy.egress_match("API.GitHub.com", "!api.github.com"))

	def test_trailing_dot_host_hits_deny_rule(self):
		self.assertFalse(policy.egress_match("evil.com.", "!evil.com"))
		self.assertFalse(policy.egress_match("sub.evil.com.", "!evil.com"))
		self.assertTrue(policy.egress_match("api.github.com.", "api.github.com"))

	def test_trailing_dot_rule_matches_plain_host(self):
		self.assertFalse(policy.egress_match("evil.com", "!evil.com."))

	def test_non_canonical_ipv4_hits_deny_rule(self):
		rule = "!34.1.2.3"
		for host in (
			"34.1.2.3",
			"0x22.1.2.3",
			"0x22010203",
			"570491395",
			"042.1.2.3",
			"34.66051",
			"34.1.515",
		):
			with self.subTest(host=host):
				self.assertFalse(policy.egress_match(host, rule))

	def test_non_canonical_ip_in_rule_matches_plain_host(self):
		self.assertFalse(policy.egress_match("34.1.2.3", "!0x22.1.2.3"))
		self.assertTrue(policy.egress_match("34.1.2.3", "0x22.1.2.3"))

	def test_ipv6_forms_hit_deny_rule(self):
		rule = "!2606:4700:4700::1111"
		for host in ("2606:4700:4700:0:0:0:0:1111", "2606:4700:4700::1111", "2606:4700:4700:0000::1111"):
			with self.subTest(host=host):
				self.assertFalse(policy.egress_match(host, rule))
		self.assertFalse(policy.egress_match("2606:4700:4700::1111", "![2606:4700:4700:0:0:0:0:1111]"))

	def test_ipv4_mapped_ipv6_hits_ipv4_rule(self):
		self.assertFalse(policy.egress_match("::ffff:34.1.2.3", "!34.1.2.3"))
		self.assertFalse(policy.egress_match("::ffff:2201:203", "!34.1.2.3"))
		self.assertFalse(policy.egress_match("34.1.2.3", "!::ffff:34.1.2.3"))

	def test_unicode_and_punycode_hosts_compare_equal(self):
		self.assertFalse(policy.egress_match("b\u00fccher.example", "!xn--bcher-kva.example"))
		self.assertFalse(policy.egress_match("xn--bcher-kva.example", "!b\u00fccher.example"))
		self.assertFalse(policy.egress_match("sub.b\u00fccher.example", "!xn--bcher-kva.example"))

	def test_ideographic_dot_host_hits_deny_rule(self):
		# the resolver's idna codec reads U+3002 as a label dot
		self.assertFalse(policy.egress_match("evil\u3002com", "!evil.com"))

	def test_dot_like_trailing_character_hits_deny_rule(self):
		# U+3002, U+FF0E, U+FF61: the resolver reads each as a trailing dot.
		for tail in ("\u3002", "\uff0e", "\uff61"):
			with self.subTest(tail=hex(ord(tail))):
				self.assertFalse(policy.egress_match("evil.com" + tail, "!evil.com"))
				self.assertFalse(policy.egress_match("sub.evil.com" + tail, "!evil.com"))
				self.assertFalse(policy.egress_match("evil.com", "!evil.com" + tail))

	def test_dot_like_middle_character_hits_deny_rule(self):
		for mid in ("\u3002", "\uff0e", "\uff61"):
			with self.subTest(mid=hex(ord(mid))):
				self.assertFalse(policy.egress_match("evil" + mid + "com", "!evil.com"))

	def test_fullwidth_digit_ip_hits_deny_rule(self):
		self.assertFalse(policy.egress_match("\uff13\uff14.1.2.3", "!34.1.2.3"))

	def test_empty_label_hosts_refused_when_rules_exist(self):
		for host in ("evil.com..", ".evil.com", "a..b", "...", ".", "evil.com\u3002\u3002"):
			with self.subTest(host=host):
				self.assertIsNone(policy.normalise_host(host))
				self.assertFalse(policy.egress_match(host, "!other.com"))
				self.assertFalse(policy.egress_match(host, "api.github.com"))

	def test_one_real_trailing_dot_still_normalises(self):
		self.assertEqual(policy.normalise_host("api.github.com."), "api.github.com")
		self.assertEqual(policy.normalise_host("API.GitHub.com"), "api.github.com")

	def test_unnormalisable_host_denied_when_rules_exist(self):
		self.assertFalse(policy.egress_match("evil..com", "!other.com"))
		self.assertFalse(policy.egress_match("", "ok.example.com"))
		self.assertTrue(policy.egress_match("evil..com", "# only a comment"))

	def test_malformed_ip_is_a_name_not_an_ip(self):
		for text in ("08.1.2.3", "1.2.3.4.5", "256.1.1.1", "0x", "1.2.3.4x", "1.-2.3.4"):
			with self.subTest(text=text):
				self.assertIsNone(policy.canonical_ip(text))


class TestEgressHardening(unittest.TestCase):
	def test_glob_with_unicode_label_still_denies(self):
		self.assertFalse(policy.egress_match("abc\u00fccd.com", "!*\u00fc*.com"))
		self.assertFalse(policy.egress_match("\u00fcber.com", "!*\u00fc*.com"))
		self.assertFalse(policy.egress_match("\u00fcber.com", "![\u00fcu]ber.com"))
		self.assertFalse(policy.egress_match("xn--ber-goa.com", "![\u00fcu]ber.com"))
		self.assertTrue(policy.egress_match("uber.org", "![\u00fc]ber.com"))
		self.assertTrue(policy.egress_match("plain.com", "!*\u00fc*.com"))

	def test_glob_with_unicode_label_in_allow_rule(self):
		self.assertTrue(policy.egress_match("\u00fcber.com", "*\u00fc*.com"))
		self.assertFalse(policy.egress_match("plain.com", "*\u00fc*.com"))

	def test_whole_label_wildcard_with_unicode_domain_still_works(self):
		self.assertFalse(policy.egress_match("sub.b\u00fccher.example", "!*.b\u00fccher.example"))
		self.assertFalse(policy.egress_match("sub.xn--bcher-kva.example", "!*.b\u00fccher.example"))
		self.assertTrue(policy.egress_match("sub.other.example", "!*.b\u00fccher.example"))

	def test_oversized_host_is_refused(self):
		self.assertIsNone(policy.normalise_host("\u00fc" * 4096))
		self.assertFalse(policy.egress_match("\u00fc" * 4096, "!other.com"))

	def test_huge_numeric_parts_never_raise(self):
		rule = "!" + "9" * 5000 + ".1.2.3"
		self.assertTrue(policy.egress_match("good.example.com", rule))
		self.assertTrue(policy.egress_address_match("34.1.2.3", rule))
		self.assertIsNone(policy.canonical_ip("9" * 5000))
		self.assertIsNone(policy.canonical_ip("0x" + "f" * 5000))
		self.assertIsNone(policy.canonical_ip("0" + "7" * 5000))
		self.assertFalse(policy.egress_match("9" * 5000 + ".example", "!other.com"))


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
				self.assertEqual(policy.canonical_ip(text), expected)


class TestEgressAddressMatch(unittest.TestCase):
	def test_ip_deny_rule_blocks_resolved_address(self):
		self.assertFalse(policy.egress_address_match("34.1.2.3", "!34.1.2.3"))
		self.assertFalse(policy.egress_address_match("::ffff:34.1.2.3", "!0x22.1.2.3"))
		self.assertTrue(policy.egress_address_match("34.1.2.4", "!34.1.2.3"))

	def test_glob_ip_deny_rules_block_resolved_address(self):
		self.assertFalse(policy.egress_address_match("34.1.2.5", "!34.1.2.*"))
		self.assertFalse(policy.egress_address_match("::ffff:34.1.2.5", "!34.1.2.*"))
		self.assertTrue(policy.egress_address_match("34.1.3.5", "!34.1.2.*"))
		self.assertFalse(policy.egress_address_match("2606:4700:4700::1111", "!2606:4700:4700::*"))
		self.assertTrue(policy.egress_address_match("2606:4700:4701::1111", "!2606:4700:4700::*"))

	def test_hostname_and_allow_rules_do_not_apply_to_addresses(self):
		self.assertTrue(policy.egress_address_match("34.1.2.3", "!evil.com\napi.github.com"))
		self.assertTrue(policy.egress_address_match("34.1.2.3", "*.atlassian.com"))
		self.assertTrue(policy.egress_address_match("34.1.2.3", None))
		self.assertTrue(policy.egress_address_match("34.1.2.3", "# comment"))


if __name__ == "__main__":
	unittest.main()
