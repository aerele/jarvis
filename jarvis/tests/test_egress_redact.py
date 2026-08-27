"""Tests for jarvis.chat.egress_redact - the generic outbound-text redactor.

Deliberately uses a NEUTRAL fake brand ("acmebot" / an emoji / a fake env var)
as the sample patterns, never the real runtime name: the module is generic and
the real patterns arrive from control-plane config, so nothing in the app repo -
source OR tests - names the runtime.
"""

import re
import unittest

from jarvis.chat.egress_redact import MODE_NAME, MODE_REMOVE, compile_rules, redact_egress


def _rules(*specs):
	return [(re.compile(p, re.IGNORECASE), m) for p, m in specs]


class TestRedactEgress(unittest.TestCase):
	def test_name_mode_replaces_with_whitelabel(self):
		out, hit = redact_egress("running on Acmebot v2", _rules((r"acmebot", MODE_NAME)), "Jarvis")
		self.assertEqual(out, "running on Jarvis v2")
		self.assertTrue(hit)

	def test_remove_mode_drops_match(self):
		rules = _rules((r"🤖", MODE_REMOVE), (r"ACMEBOT_[A-Z]+", MODE_REMOVE))
		out, hit = redact_egress("status 🤖 ACMEBOT_TOKEN done", rules, "Jarvis")
		self.assertNotIn("🤖", out)
		self.assertNotIn("ACMEBOT_TOKEN", out)
		self.assertTrue(hit)

	def test_no_match_returns_hit_false(self):
		out, hit = redact_egress("a perfectly clean reply", _rules((r"acmebot", MODE_NAME)), "Jarvis")
		self.assertEqual(out, "a perfectly clean reply")
		self.assertFalse(hit)

	def test_fail_open_on_non_str_input(self):
		rules = _rules((r"acmebot", MODE_NAME))
		self.assertEqual(redact_egress(None, rules, "Jarvis"), (None, False))
		self.assertEqual(redact_egress(123, rules, "Jarvis"), (123, False))
		self.assertEqual(redact_egress("", rules, "Jarvis"), ("", False))

	def test_fail_open_when_a_rule_raises(self):
		class Exploding:
			def sub(self, *a, **k):
				raise RuntimeError("boom")

			def search(self, *a, **k):
				return None

		out, hit = redact_egress("acmebot here", [(Exploding(), MODE_NAME)], "Jarvis")
		self.assertEqual((out, hit), ("acmebot here", False))

	def test_remove_rule_reconstruction_is_scrubbed(self):
		# A REMOVE rule can splice text into a match an EARLIER rule would have
		# caught: removing "XX" turns "acmeXXbot" into "acmebot". A single pass would
		# ship that brand token; the bounded fixpoint re-runs and drops it.
		rules = _rules((r"acmebot", MODE_REMOVE), (r"XX", MODE_REMOVE))
		out, hit = redact_egress("acmeXXbot", rules, "Jarvis")
		self.assertNotIn("acmebot", out)
		self.assertTrue(hit)

	def test_hit_is_false_when_text_is_unchanged(self):
		# A MODE_NAME match that already equals the name changes nothing; `hit` must
		# be False (it feeds a tripwire - a match-count-based hit would be spurious).
		rules = _rules((r"Jarvis", MODE_NAME))
		out, hit = redact_egress("hello Jarvis", rules, "Jarvis")
		self.assertEqual(out, "hello Jarvis")
		self.assertFalse(hit)

	def test_compile_rules_skips_empty_matchable(self):
		# An empty-matchable pattern ("a*") would splice the name at every position;
		# it must be dropped so only the real token ("valid") is scrubbed.
		compiled = compile_rules([("a*", MODE_NAME), ("valid", MODE_NAME)])
		self.assertEqual(len(compiled), 1)
		out, _ = redact_egress("aaa valid aaa", compiled, "J")
		self.assertEqual(out, "aaa J aaa")  # 'aaa' survived; only 'valid' -> 'J'

	def test_compile_rules_skips_unknown_mode(self):
		compiled = compile_rules([("valid", "delete"), ("valid2", MODE_NAME)])
		self.assertEqual(len(compiled), 1)

	def test_injection_safe_replacement(self):
		# A replacement carrying regex-replacement metacharacters must be inserted
		# LITERALLY (callable replacement), never parsed as a backreference/group.
		rules = _rules((r"acmebot", MODE_NAME))
		out, hit = redact_egress("acmebot", rules, r"\1")
		self.assertEqual(out, r"\1")
		self.assertTrue(hit)
		out2, _ = redact_egress("acmebot", rules, "a\\b\\g<0>")
		self.assertEqual(out2, "a\\b\\g<0>")

	def test_blank_replacement_falls_back(self):
		rules = _rules((r"acmebot", MODE_NAME))
		self.assertEqual(redact_egress("acmebot", rules, "")[0], "Jarvis")
		self.assertEqual(redact_egress("acmebot", rules, None)[0], "Jarvis")

	def test_idempotent_normal_replacement(self):
		rules = _rules((r"acmebot", MODE_NAME))
		once, _ = redact_egress("on acmebot now", rules, "Jarvis")
		twice, _ = redact_egress(once, rules, "Jarvis")
		self.assertEqual(once, twice)

	def test_idempotent_with_brand_like_replacement(self):
		# If the whitelabel name itself trips a rule, reusing it would COMPOUND on a
		# second pass - the redactor must fall back to a rule-safe name.
		rules = _rules((r"acme", MODE_NAME))
		once, _ = redact_egress("acme runtime", rules, "acme-agent")
		twice, _ = redact_egress(once, rules, "acme-agent")
		self.assertEqual(once, twice)
		self.assertNotIn("acme", once.lower())  # the brand-like name did not survive

	def test_compile_rules_skips_uncompilable(self):
		compiled = compile_rules([("valid", MODE_NAME), ("[unterminated", MODE_REMOVE)])
		self.assertEqual(len(compiled), 1)
		out, hit = redact_egress("valid here", compiled, "Jarvis")
		self.assertTrue(hit)

	def test_full_removal_falls_back_to_name(self):
		# INVARIANT: a non-empty input never collapses to empty. A reply that is
		# ENTIRELY remove-match content (a lone glyph / a bare env token) falls back
		# to the replacement name instead of "" (else the card blanks / recovery hangs).
		rules = _rules((r"🦞", MODE_REMOVE), (r"ACMEBOT_[A-Z]+", MODE_REMOVE))
		out, hit = redact_egress("🦞", rules, "Jarvis")
		self.assertEqual(out, "Jarvis")
		self.assertTrue(hit)
		self.assertEqual(redact_egress("ACMEBOT_TOKEN", rules, "Jarvis")[0], "Jarvis")
		# whitespace around a removed token also collapses -> name (never a blank card)
		self.assertEqual(redact_egress("🦞   ", rules, "Jarvis")[0], "Jarvis")

	def test_empty_input_stays_empty(self):
		# The no-collapse invariant applies only to non-empty input; "" stays "".
		self.assertEqual(redact_egress("", _rules((r"acme", MODE_NAME)), "Jarvis"), ("", False))

	def test_whitespace_only_replacement_falls_back(self):
		self.assertEqual(redact_egress("acmebot", _rules((r"acmebot", MODE_NAME)), "   ")[0], "Jarvis")

	def test_compile_rules_tolerates_non_list(self):
		# A corrupt cache blob (non-iterable) must not raise out of compile_rules.
		self.assertEqual(compile_rules(123), [])
		self.assertEqual(compile_rules(None), [])
		self.assertEqual(compile_rules({"a": 1}), [])
