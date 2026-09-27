"""External upgrade inputs must be explicit, pinned, and safe before test DDL."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.tests import _legacy_migration_fixtures as fixtures


def sample():
	return {
		"version": 2,
		"watermark": {"legacy_column": "previous_watermark"},
		"capture_provider": {
			"legacy_column": "previous_provider",
			"patch": "jarvis.patches.provider.execute",
		},
	}


class TestLegacyFixtureLoader(unittest.TestCase):
	def test_valid_document_and_json_whitespace(self):
		doc = sample()
		self.assertEqual(fixtures.validate(json.dumps(doc, indent=2), fixtures.digest(doc)), doc)

	def test_changed_identifiers_fail_the_reviewed_digest(self):
		doc = sample()
		digest = fixtures.digest(doc)
		doc["watermark"]["legacy_column"] = "another_column"
		with self.assertRaises(ValueError):
			fixtures.validate(json.dumps(doc), digest)

	def test_bad_schema_identifiers_and_imports_fail_even_with_matching_digest(self):
		cases = []
		for section, field, values in (
			(
				"watermark",
				"legacy_column",
				["a`; DROP TABLE x; --", "has space", "agent_seq_watermark", "../file", None],
			),
			(
				"capture_provider",
				"patch",
				["os.system", "jarvis.patches.a.execute()", "jarvis.patches.a;evil.execute", None],
			),
		):
			for value in values:
				doc = sample()
				doc[section][field] = value
				cases.append(doc)
		for value in (True, 1, "2"):
			doc = sample()
			doc["version"] = value
			cases.append(doc)
		# v1 shapes: the retired settings section and watermark patch are rejected.
		doc = sample()
		doc["settings"] = {}
		cases.append(doc)
		doc = sample()
		doc["watermark"]["patch"] = "jarvis.patches.example.execute"
		cases.append(doc)
		for doc in cases:
			with self.subTest(doc=doc), self.assertRaises(ValueError):
				fixtures.validate(json.dumps(doc), fixtures.digest(doc))

	def test_missing_malformed_or_oversized_input_fails(self):
		for raw in (None, "", "{}", "null", "[]", "invalid", " " * 16_385):
			with self.subTest(raw_type=type(raw)), self.assertRaises(ValueError):
				fixtures.validate(raw)

	def test_missing_environment_has_no_production_derived_default(self):
		with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "require"):
			fixtures.load()

	def test_ambiguous_input_and_missing_file_fail(self):
		with patch.dict(os.environ, {fixtures.VALUE_ENV: "{}", fixtures.FILE_ENV: "/absent"}, clear=True):
			with self.assertRaisesRegex(ValueError, "not both"):
				fixtures.load()
		with patch.dict(os.environ, {fixtures.FILE_ENV: "/absent"}, clear=True):
			with self.assertRaises(OSError):
				fixtures.load()

	def test_file_and_environment_both_reach_validation(self):
		raw = json.dumps(sample())
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "fixture.json"
			path.write_text(raw)
			for env in ({fixtures.FILE_ENV: str(path)}, {fixtures.VALUE_ENV: raw}):
				with patch.dict(os.environ, env, clear=True), patch.object(fixtures, "validate") as validate:
					validate.return_value = copy.deepcopy(sample())
					self.assertEqual(fixtures.load(), sample())
					validate.assert_called_once_with(raw)
