"""Pinned CI snapshots must work without variables and reject tampering."""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.ci import inputs


class TestCIInputs(unittest.TestCase):
	def test_both_bundled_snapshots_validate(self):
		for kind in ("policy", "fixture"):
			self.assertEqual(json.loads(inputs.decode(kind))["version"], 1)

	def test_missing_or_malformed_snapshots_fail(self):
		with tempfile.TemporaryDirectory() as directory, patch.object(inputs, "_DATA", Path(directory)):
			for kind, name in inputs._FILES.items():
				with self.subTest(kind=kind):
					with self.assertRaises(FileNotFoundError):
						inputs.decode(kind)
					Path(directory, name).write_text("invalid!")
					with self.assertRaises(ValueError):
						inputs.decode(kind)

	def test_valid_json_with_changed_content_is_rejected(self):
		originals = {kind: json.loads(inputs.decode(kind)) for kind in inputs._FILES}
		with tempfile.TemporaryDirectory() as directory, patch.object(inputs, "_DATA", Path(directory)):
			for kind, document in originals.items():
				with self.subTest(kind=kind):
					if kind == "fixture":
						document["watermark"]["legacy_column"] = "another_column"
					else:
						document["minimum_files"]["jarvis"] += 1
					Path(directory, inputs._FILES[kind]).write_text(
						base64.b64encode(json.dumps(document).encode()).decode()
					)
					with self.assertRaises(ValueError):
						inputs.decode(kind)
