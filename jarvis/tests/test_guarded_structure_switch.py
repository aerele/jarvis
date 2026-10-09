"""The structure-write switch reads the site config files directly."""

import json
import os
import tempfile

from frappe.tests.utils import FrappeTestCase

from jarvis.tools import _guarded_structure as gs


class TestReadJson(FrappeTestCase):
	def setUp(self):
		self.dir = tempfile.mkdtemp()

	def write(self, name, value):
		path = os.path.join(self.dir, name)
		with open(path, "w") as fh:
			json.dump(value, fh)
		self.addCleanup(os.unlink, path)
		return path

	def test_an_object_is_read(self):
		path = self.write("site_config.json", {gs.OFF_SWITCH: 1})
		self.assertEqual(gs._read_json(path), {gs.OFF_SWITCH: 1})

	def test_a_missing_file_is_empty_only_when_allowed(self):
		path = os.path.join(self.dir, "missing.json")
		self.assertEqual(gs._read_json(path, missing_ok=True), {})
		with self.assertRaises(FileNotFoundError):
			gs._read_json(path)

	def test_a_file_that_is_not_an_object_is_refused(self):
		with self.assertRaises(ValueError):
			gs._read_json(self.write("site_config.json", [1]))
