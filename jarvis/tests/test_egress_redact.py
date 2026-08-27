"""Tests for jarvis.chat.egress_redact - the generic REMOVE-ONLY outbound-text redactor.

Deliberately uses a NEUTRAL fake brand ("acmebot" / an emoji / a fake env var) as
the sample patterns, never the real runtime name: the module is generic and the
real patterns arrive from control-plane config, so nothing in the app repo - source
OR tests - names the runtime.
"""

import re
import unittest

from jarvis.chat.egress_redact import (
	_COLLAPSED_PLACEHOLDER,
	MODE_REMOVE,
	compile_rules,
	redact_egress,
)


def _rules(*patterns):
	"""Build the redactor's rule shape: a list of compiled regexes (remove-only)."""
	return [re.compile(p, re.IGNORECASE) for p in patterns]


class TestRedactEgress(unittest.TestCase):
	def test_remove_drops_every_match(self):
		rules = _rules(r"🤖", r"ACMEBOT_[A-Z]+", r"acmebot")
		out, hit = redact_egress("status 🤖 ACMEBOT_TOKEN for acmebot done", rules)
		self.assertNotIn("🤖", out)
		self.assertNotIn("ACMEBOT_TOKEN", out)
		self.assertNotIn("acmebot", out.lower())
		self.assertTrue(hit)

	def test_no_match_returns_hit_false(self):
		out, hit = redact_egress("a perfectly clean reply", _rules(r"acmebot"))
		self.assertEqual(out, "a perfectly clean reply")
		self.assertFalse(hit)

	def test_fail_open_on_non_str_input(self):
		rules = _rules(r"acmebot")
		self.assertEqual(redact_egress(None, rules), (None, False))
		self.assertEqual(redact_egress(123, rules), (123, False))
		self.assertEqual(redact_egress("", rules), ("", False))

	def test_fail_open_when_a_rule_raises(self):
		class Exploding:
			def sub(self, *a, **k):
				raise RuntimeError("boom")

		out, hit = redact_egress("acmebot here", [Exploding()])
		self.assertEqual((out, hit), ("acmebot here", False))

	def test_remove_reconstruction_is_scrubbed(self):
		# Removing "XX" turns "acmeXXbot" into "acmebot" - a match an EARLIER rule would
		# have caught. A single pass would ship that token; the bounded fixpoint re-runs.
		rules = _rules(r"acmebot", r"XX")
		out, hit = redact_egress("acmeXXbot", rules)
		self.assertNotIn("acmebot", out)
		self.assertTrue(hit)

	def test_idempotent(self):
		# redact(redact(x)) == redact(x)
		rules = _rules(r"acmebot")
		once, _ = redact_egress("on acmebot now", rules)
		twice, _ = redact_egress(once, rules)
		self.assertEqual(once, twice)
		self.assertNotIn("acmebot", once.lower())

	def test_compile_rules_skips_empty_matchable(self):
		# An empty-matchable pattern ("a*") would match at every position; it must be
		# dropped so only the real token ("valid") is scrubbed.
		compiled = compile_rules([["a*", MODE_REMOVE], ["valid", MODE_REMOVE]])
		self.assertEqual(len(compiled), 1)
		out, _ = redact_egress("aaa valid aaa", compiled)
		self.assertEqual(out, "aaa  aaa")  # 'aaa' survived; only 'valid' removed

	def test_compile_rules_skips_non_remove_mode(self):
		# Remove-only: any non-`remove` mode (including the RETIRED "name" rename mode)
		# is skipped, not applied - a future CP rename rule is dropped rather than
		# silently removing text the operator meant to relabel.
		compiled = compile_rules([["a", "name"], ["b", "delete"], ["c", MODE_REMOVE]])
		self.assertEqual(len(compiled), 1)

	def test_compile_rules_skips_uncompilable(self):
		compiled = compile_rules([["valid", MODE_REMOVE], ["[unterminated", MODE_REMOVE]])
		self.assertEqual(len(compiled), 1)
		_out, hit = redact_egress("valid here", compiled)
		self.assertTrue(hit)

	def test_full_removal_falls_back_to_placeholder(self):
		# INVARIANT: a non-empty input never collapses to empty. A reply that is ENTIRELY
		# remove-match content (a lone glyph / a bare env token) falls back to a name-free
		# placeholder instead of "" (else the card blanks / recovery hangs to a false timeout).
		rules = _rules(r"🦞", r"ACMEBOT_[A-Z]+")
		out, hit = redact_egress("🦞", rules)
		self.assertEqual(out, _COLLAPSED_PLACEHOLDER)
		self.assertTrue(hit)
		self.assertEqual(redact_egress("ACMEBOT_TOKEN", rules)[0], _COLLAPSED_PLACEHOLDER)
		# whitespace around a removed token also collapses -> placeholder (never a blank card)
		self.assertEqual(redact_egress("🦞   ", rules)[0], _COLLAPSED_PLACEHOLDER)

	def test_collapse_placeholder_names_nothing(self):
		# The placeholder must NOT be a tenant/agent name - this module names nothing.
		self.assertNotIn("jarvis", _COLLAPSED_PLACEHOLDER.lower())

	def test_empty_input_stays_empty(self):
		# The no-collapse invariant applies only to non-empty input; "" stays "".
		self.assertEqual(redact_egress("", _rules(r"acme")), ("", False))

	def test_compile_rules_tolerates_non_list(self):
		# A corrupt cache blob (non-iterable) must not raise out of compile_rules.
		self.assertEqual(compile_rules(123), [])
		self.assertEqual(compile_rules(None), [])
		self.assertEqual(compile_rules({"a": 1}), [])
