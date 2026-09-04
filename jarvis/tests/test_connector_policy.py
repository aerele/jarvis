"""Unit tests for ``jarvis.connectors.policy`` - the allowed-actions gate and the
argument check. Plain ``unittest``; the policy module is frappe-free, so a simple
duck-typed row (a dict-backed ``.get``) drives it with no bench.
"""

from __future__ import annotations

import json
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

	def test_read_only_non_destructive_auto_allowed(self):
		row = _row(actions=[{"action": "get_x", "allowed": 0, "read_only": 1, "destructive": 0}])
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


if __name__ == "__main__":
	unittest.main()
