"""The generic branding gate must fail closed without embedding a runtime policy."""

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from jarvis.ci import runtime_branding as guard


class TestBrandingScanner(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name).resolve()
		subprocess.run(["git", "init", "-q", str(self.root)], check=True)
		self.write("src/safe.py", "# neutral content\n")
		self.policy = {
			"version": 1,
			"terms": ["vendorbrand"],
			"allowed_lines": {},
			"allowed_paths": [],
			"minimum_files": {"src": 1},
		}

	def write(self, name, content):
		path = self.root / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(content)

	def test_clean_checkout_passes(self):
		self.assertEqual(guard.scan(self.root, self.policy), [])

	def test_new_case_insensitive_reference_in_any_text_file_fails(self):
		for name in ("README.md", "src/new.py", "frontend/new.vue", ".github/settings", "LICENSE"):
			with self.subTest(name=name):
				self.write(name, "VendOrBrand\n")
				self.assertTrue(any(name in error for error in guard.scan(self.root, self.policy)))
				(self.root / name).unlink()

	def test_exact_allowance_cannot_cover_another_file_or_extra_text(self):
		line = 'OLD_COLUMN = "vendorbrand_column"'
		self.policy["allowed_lines"] = {"src/legacy.py": [line]}
		self.write("src/legacy.py", line + "\n")
		self.assertEqual(guard.scan(self.root, self.policy), [])
		self.write("src/copied.py", line + "\n")
		self.assertTrue(guard.scan(self.root, self.policy))
		(self.root / "src/copied.py").unlink()
		self.write("src/legacy.py", line + " # vendorbrand\n")
		self.assertTrue(guard.scan(self.root, self.policy))

	def test_duplicate_line_and_stale_allowance_fail(self):
		line = "# vendorbrand compatibility"
		self.policy["allowed_lines"] = {"src/legacy.py": [line]}
		self.write("src/legacy.py", (line + "\n") * 2)
		self.assertTrue(guard.scan(self.root, self.policy))
		self.write("src/legacy.py", "# removed\n")
		self.assertTrue(any("stale" in error for error in guard.scan(self.root, self.policy)))

	def test_filenames_need_their_own_exact_allowance(self):
		name = "src/vendorbrand_migration.py"
		self.write(name, "# neutral\n")
		self.assertTrue(guard.scan(self.root, self.policy))
		self.policy["allowed_paths"] = [name]
		self.assertEqual(guard.scan(self.root, self.policy), [])
		(self.root / name).unlink()
		self.assertTrue(any("stale path" in error for error in guard.scan(self.root, self.policy)))

	def test_tracked_ignored_source_is_still_scanned(self):
		self.write(".gitignore", "src/ignored.py\n")
		self.write("src/ignored.py", "# vendorbrand\n")
		subprocess.run(["git", "-C", str(self.root), "add", "-f", "src/ignored.py"], check=True)
		self.assertTrue(guard.scan(self.root, self.policy))

	def test_symlinks_are_not_followed_outside_checkout(self):
		(self.root / "src/link.py").symlink_to(self.root.parent / "outside.py")
		self.assertTrue(
			any("unsupported source path" in error for error in guard.scan(self.root, self.policy))
		)

	def test_empty_scan_and_wrong_root_fail(self):
		(self.root / "src/safe.py").unlink()
		self.assertTrue(any("minimum" in error for error in guard.scan(self.root, self.policy)))
		with self.assertRaises(guard.PolicyError):
			guard.scan(self.root / "src", self.policy)

	def test_policy_is_required_and_validated(self):
		for raw in (None, "", "{}", "[]", "null", "invalid"):
			with self.subTest(raw=raw), self.assertRaises(guard.PolicyError):
				guard.load_policy(raw)
		for field, bad in (
			("terms", []),
			("minimum_files", {}),
			("minimum_files", {"src": 0}),
			("allowed_lines", {"src/*": ["vendorbrand"]}),
			("allowed_paths", ["../outside"]),
		):
			doc = copy.deepcopy(self.policy)
			doc[field] = bad
			with self.subTest(field=field), self.assertRaises(guard.PolicyError):
				guard.load_policy(json.dumps(doc))

	def test_digest_pins_policy_content_independent_of_json_whitespace(self):
		digest = guard.policy_digest(self.policy)
		self.assertEqual(guard.load_policy(json.dumps(self.policy, indent=2), digest), self.policy)
		changed = copy.deepcopy(self.policy)
		changed["terms"] = ["different"]
		with self.assertRaises(guard.PolicyError):
			guard.load_policy(json.dumps(changed), digest)

	def test_missing_ci_variable_and_missing_policy_file_exit_nonzero(self):
		with patch.dict(os.environ, {}, clear=True), redirect_stderr(io.StringIO()):
			self.assertEqual(guard.main(["--policy-env", "TEST_POLICY"]), 1)
			self.assertEqual(guard.main(["--policy", str(self.root / "missing.json")]), 1)

	def test_terms_are_literal_not_regex_patterns(self):
		self.policy["terms"] = ["vendor.brand"]
		self.write("src/safe.py", "vendorXbrand\n")
		self.assertEqual(guard.scan(self.root, self.policy), [])
		self.write("src/safe.py", "vendor.brand\n")
		self.assertTrue(guard.scan(self.root, self.policy))
