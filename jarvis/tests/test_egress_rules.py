"""Tests for jarvis.chat.egress_rules — the bench cache + accessor for the
control-plane-owned egress redaction rules.

Uses a NEUTRAL fake brand ("acme") for the sample patterns — the real patterns
arrive from the control plane, so nothing in the app repo (source or tests) names
the runtime. Exercises the cache round-trip, fail-open behaviour, per-request
memoization, and the replacement-name resolution.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import egress_rules
from jarvis.chat.egress_rules import _PATTERNS_FIELD, _SYNCED_AT_FIELD, SETTINGS

_LOBSTER = "\U0001f99e"


def _set_raw(blob):
	"""Write the raw patterns blob and force cache coherence for the reader: clear
	the Single's document cache (so get_rules' cache=True read sees it) and the
	per-request compile memo. Lets get_rules be tested in isolation from persist."""
	frappe.db.set_value(SETTINGS, SETTINGS, _PATTERNS_FIELD, blob, update_modified=False)
	frappe.clear_document_cache(SETTINGS, SETTINGS)
	egress_rules._invalidate_memo()


class TestPersist(FrappeTestCase):
	def setUp(self):
		egress_rules._invalidate_memo()

	def test_stores_blob_and_stamps_synced_at(self):
		egress_rules.persist([["acme", "name"]])
		blob = frappe.db.get_single_value(SETTINGS, _PATTERNS_FIELD)
		self.assertEqual(frappe.parse_json(blob), [["acme", "name"]])
		self.assertIsNotNone(frappe.db.get_single_value(SETTINGS, _SYNCED_AT_FIELD))

	def test_skips_write_when_unchanged(self):
		egress_rules.persist([["acme", "name"]])
		# A repeat of the identical list must not touch the row — churning `modified`
		# would collide with an operator editing the Settings form, and this runs on
		# every connection poll.
		with patch.object(frappe.db, "set_value", wraps=frappe.db.set_value) as sv:
			egress_rules.persist([["acme", "name"]])
			sv.assert_not_called()

	def test_none_keeps_last_known_good(self):
		# A degraded/old CP payload omits the key -> persist(None) must NOT wipe a
		# working backstop; it keeps the last-known-good list.
		egress_rules.persist([["acme", "name"]])
		egress_rules.persist(None)
		self.assertEqual(
			frappe.parse_json(frappe.db.get_single_value(SETTINGS, _PATTERNS_FIELD)), [["acme", "name"]]
		)

	def test_explicit_empty_list_clears(self):
		# The deliberate kill-switch: an explicit [] (CP blanked the constant) clears.
		egress_rules.persist([["acme", "name"]])
		egress_rules.persist([])
		self.assertEqual(frappe.parse_json(frappe.db.get_single_value(SETTINGS, _PATTERNS_FIELD)), [])

	def test_never_raises(self):
		with patch.object(frappe.db, "set_value", side_effect=RuntimeError("boom")):
			egress_rules.persist([["acme", "name"]])  # fail-open: no exception escapes


class TestGetRules(FrappeTestCase):
	def setUp(self):
		egress_rules._invalidate_memo()

	def test_returns_compiled_rules(self):
		_set_raw(frappe.as_json([["acme", "name"], [_LOBSTER, "remove"]]))
		self.assertEqual(len(egress_rules.get_rules()), 2)

	def test_empty_when_nothing_cached(self):
		_set_raw("")
		self.assertEqual(egress_rules.get_rules(), [])

	def test_failopen_on_malformed_blob(self):
		_set_raw("{not valid json")
		self.assertEqual(egress_rules.get_rules(), [])

	def test_failopen_on_nonlist_json(self):
		_set_raw('{"a": 1}')
		self.assertEqual(egress_rules.get_rules(), [])

	def test_skips_bad_entries_per_rule(self):
		# 'acme' + 'ok' valid; '[unterminated' uncompilable; 'a*' empty-matchable —
		# the last two are dropped by compile_rules, never fatal to the whole set.
		_set_raw(
			frappe.as_json([["acme", "name"], ["[unterminated", "remove"], ["a*", "name"], ["ok", "remove"]])
		)
		self.assertEqual(len(egress_rules.get_rules()), 2)

	def test_memoized_until_blob_changes(self):
		_set_raw(frappe.as_json([["acme", "name"]]))
		first = egress_rules.get_rules()
		self.assertIs(egress_rules.get_rules(), first)  # same object -> compile memoized
		_set_raw(frappe.as_json([["acme", "name"], ["beta", "remove"]]))
		second = egress_rules.get_rules()
		self.assertIsNot(second, first)  # blob changed -> recompiled
		self.assertEqual(len(second), 2)


class TestReplacementName(FrappeTestCase):
	def _set_agent_name(self, value):
		frappe.db.set_value(SETTINGS, SETTINGS, "agent_name", value, update_modified=False)
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def test_default_when_unset(self):
		self._set_agent_name("")
		self.assertEqual(egress_rules.get_replacement_name(), "Jarvis")

	def test_uses_trimmed_agent_name(self):
		self._set_agent_name("  Acme Bot  ")
		self.assertEqual(egress_rules.get_replacement_name(), "Acme Bot")


class TestRedact(FrappeTestCase):
	def setUp(self):
		egress_rules._invalidate_memo()

	def test_redacts_with_cached_rules_and_name(self):
		_set_raw(frappe.as_json([["acme", "name"]]))
		frappe.db.set_value(SETTINGS, SETTINGS, "agent_name", "", update_modified=False)
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		self.assertEqual(egress_rules.redact("run on acme now"), "run on Jarvis now")

	def test_noop_when_no_rules_cached(self):
		_set_raw("")
		self.assertEqual(egress_rules.redact("run on acme now"), "run on acme now")

	def test_failopen_on_non_str(self):
		_set_raw(frappe.as_json([["acme", "name"]]))
		self.assertEqual(egress_rules.redact(None), None)
		self.assertEqual(egress_rules.redact(123), 123)
