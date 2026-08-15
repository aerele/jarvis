"""Tests for the org-wide knowledge-language preference (design D6):
``jarvis.chat.knowledge_language`` (English-default read + the prompt
directive block) and its wiring into the voice-facts extraction prompt
(``jarvis.learning.voice_facts._extract_batch`` - the single boundary that
covers BOTH rule and context facts) plus the ``_SETTINGS_DEFAULTS`` seeding.

The Select field never needs the tabSingles row-probe (that is a Check-field
trap); these tests still manipulate the raw Singles row to prove the falsy /
bogus-value fallbacks. The LLM boundary is always mocked.
"""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import now_datetime

from jarvis.chat import knowledge_language
from jarvis.learning import voice_facts

SETTINGS = "Jarvis Settings"
FIELD = "knowledge_language"


def _read_raw():
	rows = frappe.db.sql(
		"select value from `tabSingles` where doctype=%s and field=%s",
		(SETTINGS, FIELD),
	)
	return rows[0][0] if rows else None


def _set_value(value) -> None:
	"""Set (or with None: unset) the raw Singles row, clearing the
	get_single_value cache either way."""
	if value is None:
		frappe.db.delete("Singles", {"doctype": SETTINGS, "field": FIELD})
		frappe.clear_document_cache(SETTINGS, SETTINGS)
	else:
		# No endpoint validation here on purpose - the raw write lets the
		# bogus-value fallback be proven.
		frappe.db.set_single_value(SETTINGS, FIELD, value, update_modified=False)
	frappe.db.commit()


def _batch() -> dict:
	return {
		"owner": "Administrator",
		"notes": [
			frappe._dict(
				name="VN-kl-test-1",
				transcript="hello there, a durable fact",
				creation=now_datetime(),
			)
		],
	}


class TestKnowledgeLanguage(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls._original = _read_raw()

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		_set_value(self._original)

	# ------------------------------------------------------------------ #
	# get_knowledge_language
	# ------------------------------------------------------------------ #
	def test_default_english_when_unset(self):
		_set_value(None)
		self.assertEqual(knowledge_language.get_knowledge_language(), "English")

	def test_original_mode(self):
		_set_value("Original")
		self.assertEqual(knowledge_language.get_knowledge_language(), "Original")

	def test_bogus_stored_value_falls_back_to_english(self):
		# The whitelisted setter validates, but a raw Desk/db write can store
		# anything; the reader must coalesce unknown values.
		_set_value("Klingon")
		self.assertEqual(knowledge_language.get_knowledge_language(), "English")

	def test_survives_settings_read_failure(self):
		with mock.patch.object(frappe.db, "get_single_value", side_effect=Exception("boom")):
			self.assertEqual(knowledge_language.get_knowledge_language(), "English")
			self.assertTrue(knowledge_language.language_directive())

	# ------------------------------------------------------------------ #
	# language_directive
	# ------------------------------------------------------------------ #
	def test_directive_english(self):
		_set_value(None)
		directive = knowledge_language.language_directive()
		self.assertEqual(directive, knowledge_language._ENGLISH_DIRECTIVE)
		self.assertIn("in English", directive)
		self.assertIn("transliteration", directive)

	def test_directive_original(self):
		_set_value("Original")
		directive = knowledge_language.language_directive()
		self.assertEqual(directive, knowledge_language._ORIGINAL_DIRECTIVE)
		self.assertIn("dominant language of the source material", directive)

	def test_directive_always_non_empty(self):
		for value in (None, "English", "Original", "Klingon", ""):
			_set_value(value if value else None)
			self.assertTrue(knowledge_language.language_directive())

	# ------------------------------------------------------------------ #
	# voice_facts prompt wiring (the single boundary for rule AND context
	# facts - context facts bypass the wiki ingest prompt entirely)
	# ------------------------------------------------------------------ #
	def test_voice_facts_prompt_carries_english_directive_by_default(self):
		_set_value(None)
		with mock.patch("jarvis.chat.voice.openrouter_complete", return_value="[]") as m:
			out = voice_facts._extract_batch(_batch())
		self.assertEqual(out, [])
		m.assert_called_once()
		system = m.call_args.args[0][0]["content"]
		# The extraction schema stays intact; the directive is appended.
		self.assertIn(voice_facts._EXTRACTION_SYSTEM, system)
		self.assertIn(knowledge_language._ENGLISH_DIRECTIVE, system)

	def test_voice_facts_prompt_switches_to_original(self):
		_set_value("Original")
		with mock.patch("jarvis.chat.voice.openrouter_complete", return_value="[]") as m:
			voice_facts._extract_batch(_batch())
		system = m.call_args.args[0][0]["content"]
		self.assertIn(knowledge_language._ORIGINAL_DIRECTIVE, system)
		self.assertNotIn(knowledge_language._ENGLISH_DIRECTIVE, system)

	# ------------------------------------------------------------------ #
	# _SETTINGS_DEFAULTS seeding (after_migrate)
	# ------------------------------------------------------------------ #
	def test_settings_default_registered(self):
		self.assertEqual(voice_facts._SETTINGS_DEFAULTS.get(FIELD), "English")

	def test_after_migrate_seeds_missing_row_only(self):
		_set_value(None)
		voice_facts.after_migrate()
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		self.assertEqual(_read_raw(), "English")
		self.assertEqual(knowledge_language.get_knowledge_language(), "English")
		# An operator-set value is never clobbered by a re-migrate.
		_set_value("Original")
		voice_facts.after_migrate()
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		self.assertEqual(_read_raw(), "Original")


if __name__ == "__main__":
	unittest.main()
