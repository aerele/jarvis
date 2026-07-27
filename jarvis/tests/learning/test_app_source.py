"""Tests for ``jarvis.learning.app_source`` — the SHARED custom-app source
collection machinery (extracted from ``app_analysis`` so the legacy chat pipeline
and the Custom App Learning *scribe* delegate's source-read tools use one
implementation of the guards).

Covers the load-bearing guards at their new home: the extension allowlist + dir
excludes + per-file cap + total-file cap in ``_collect_files``, the realpath-
containment + symlink-skipping secret-exfil guard (``_is_contained`` /
``_collect_files`` / ``read_source_file``), the analysis priority ordering, and
the NEW ``read_source_file`` primitive the delegate read tool is built on.

Hermetic: the app "source tree" is a temp dir behind the ``_app_source_dir``
seam. No frappe DB writes.

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.learning.test_app_source
"""

from __future__ import annotations

import os
import shutil
import tempfile
from unittest import mock

from frappe.tests.utils import FrappeTestCase

from jarvis.learning import app_source

_BIG = app_source.PER_FILE_CAP_BYTES + 1


class TestAppSource(FrappeTestCase):
	def setUp(self):
		self.tmp = tempfile.mkdtemp(prefix="jarvis-app-source-test-")
		self.app_dir = os.path.join(self.tmp, "fakeapp")
		os.makedirs(self.app_dir)
		self._patch = mock.patch.object(app_source, "_app_source_dir", side_effect=lambda app: self.app_dir)
		self._patch.start()

	def tearDown(self):
		self._patch.stop()
		shutil.rmtree(self.tmp, ignore_errors=True)

	def _write(self, files: dict[str, str]) -> None:
		for rel, content in files.items():
			full = os.path.join(self.app_dir, rel)
			os.makedirs(os.path.dirname(full), exist_ok=True)
			with open(full, "w") as fh:
				fh.write(content)

	# ------------------------------------------------------------------ #
	# collection walk: allowlist + excludes + per-file cap + symlink guard
	# ------------------------------------------------------------------ #
	def test_collect_allowlist_excludes_and_per_file_cap(self):
		self._write(
			{
				"hooks.py": "app_title = 'Fake'\n",
				"fakeapp/doctype/widget/widget.json": '{"doctype": "DocType"}',
				"fakeapp/doctype/widget/widget.py": "class Widget:\n\tpass\n",
				"api.py": "import frappe\n",
				"public/js/app.js": "console.log(1)\n",
				"README.md": "# fake\n",
				"logo.png": "not-source",
				"node_modules/pkg/index.js": "excluded",
				".git/config": "[core]",
				"public/frontend/main.vue": "<template/>",
				"__pycache__/x.py": "excluded",
				"big.py": "#" * _BIG,
			}
		)
		rels, notes = app_source._collect_files(self.app_dir)
		self.assertEqual(
			set(rels),
			{
				"hooks.py",
				"fakeapp/doctype/widget/widget.json",
				"fakeapp/doctype/widget/widget.py",
				"api.py",
				"public/js/app.js",
				"README.md",
			},
		)
		self.assertEqual(notes["skipped_large_files"], 1)
		self.assertEqual(notes["dropped_files"], 0)
		# read-only wrt the source: the big file is still there, untouched.
		self.assertTrue(os.path.isfile(os.path.join(self.app_dir, "big.py")))

	def test_file_cap_keeps_prioritized_subset(self):
		self._write(
			{
				"hooks.py": "h\n",
				"m/doctype/x/x.json": "{}",
				"m/doctype/x/x.py": "pass\n",
				"misc.py": "pass\n",
				"README.md": "# r\n",
			}
		)
		with mock.patch.object(app_source, "FILE_CAP", 3):
			rels, notes = app_source._collect_files(self.app_dir)
		self.assertEqual(set(rels), {"hooks.py", "m/doctype/x/x.json", "m/doctype/x/x.py"})
		self.assertEqual(notes["dropped_files"], 2)

	def test_symlink_escape_is_not_collected_or_read(self):
		# The secret-exfil guard: a symlink whose target is OUTSIDE the app tree
		# (e.g. site secrets) must never be collected or served to the LLM.
		outside = os.path.join(self.tmp, "secret.py")
		with open(outside, "w") as fh:
			fh.write("ENCRYPTION_KEY = 'hunter2'\n")
		self._write({"hooks.py": "app_title = 'Fake'\n"})
		os.symlink(outside, os.path.join(self.app_dir, "config.py"))  # allowed-ext link out of tree
		rels, notes = app_source._collect_files(self.app_dir)
		self.assertNotIn("config.py", set(rels))
		self.assertEqual(notes["skipped_symlinks"], 1)
		# and the read primitive refuses it too
		with self.assertRaises(ValueError):
			app_source.read_source_file("fakeapp", "config.py", src_dir=self.app_dir)

	def test_symlinked_directory_is_not_descended(self):
		outside_dir = os.path.join(self.tmp, "outside")
		os.makedirs(outside_dir)
		with open(os.path.join(outside_dir, "leak.py"), "w") as fh:
			fh.write("SECRET = 'zzz'\n")
		self._write({"hooks.py": "h\n"})
		os.symlink(outside_dir, os.path.join(self.app_dir, "linkdir"))
		rels, _notes = app_source._collect_files(self.app_dir)
		self.assertNotIn("linkdir/leak.py", set(rels))

	def test_is_contained_guard(self):
		app_real = os.path.realpath(self.app_dir)
		self._write({"hooks.py": "h\n"})
		self.assertTrue(app_source._is_contained(os.path.join(self.app_dir, "hooks.py"), app_real))
		self.assertFalse(app_source._is_contained("/etc/passwd", app_real))

	def test_symlinked_app_root_is_refused(self):
		# CA-1: a symlinked app ROOT (apps/<app> -> <bench>/sites) defeats the
		# per-file containment guard — every regular file beneath the link passes
		# _is_contained AND os.path.islink (only the ROOT is the link), so site
		# secrets could be served. The inner-symlink tests above cover files inside
		# the tree, NOT this trust root. _resolve_app_root must reject it BEFORE any
		# walk/read.
		bench = os.path.join(self.tmp, "bench")
		apps = os.path.join(bench, "apps")
		sites = os.path.join(bench, "sites")
		os.makedirs(apps)
		os.makedirs(sites)
		with open(os.path.join(sites, "common_site_config.json"), "w") as fh:
			fh.write('{"encryption_key": "hunter2"}')
		good = os.path.join(apps, "goodapp")
		os.makedirs(good)
		with open(os.path.join(good, "hooks.py"), "w") as fh:
			fh.write("app_title = 'Good'\n")
		os.symlink(sites, os.path.join(apps, "evilapp"))  # ROOT symlink to <bench>/sites
		outside = os.path.join(self.tmp, "outside")  # out-of-tree target
		os.makedirs(outside)
		os.symlink(outside, os.path.join(apps, "escapee"))

		with mock.patch.object(app_source, "_app_source_dir", side_effect=lambda a: os.path.join(apps, a)):
			# a legit real root resolves to its own realpath
			self.assertEqual(app_source._resolve_app_root("goodapp"), os.path.realpath(good))
			# the symlinked-to-sites root is REFUSED
			with self.assertRaises(ValueError):
				app_source._resolve_app_root("evilapp")
			# an out-of-tree symlinked root is REFUSED
			with self.assertRaises(ValueError):
				app_source._resolve_app_root("escapee")
			# a name carrying a path separator never reaches the filesystem
			for bad in ("../sites", "a/b", "..", "."):
				with self.assertRaises(ValueError):
					app_source._resolve_app_root(bad)
			# end-to-end: read_source_file cannot serve the site secret through the
			# symlinked root (src_dir=None routes through _resolve_app_root)
			with self.assertRaises(ValueError):
				app_source.read_source_file("evilapp", "common_site_config.json")
			# the legit app still reads normally through the validated root
			text, size = app_source.read_source_file("goodapp", "hooks.py")
			self.assertIn("app_title", text)
			self.assertGreater(size, 0)

	# ------------------------------------------------------------------ #
	# priority ordering
	# ------------------------------------------------------------------ #
	def test_priority_report_and_fixture_json_outrank_js_and_md(self):
		self.assertEqual(app_source._priority("hooks.py"), 0)
		self.assertLess(
			app_source._priority("m/report/sales/sales.py"),
			app_source._priority("public/js/app.js"),
		)
		self.assertLess(
			app_source._priority("m/fixtures/workflow.json"),
			app_source._priority("README.md"),
		)

	# ------------------------------------------------------------------ #
	# read_source_file (the delegate read tool primitive)
	# ------------------------------------------------------------------ #
	def test_read_source_file_reads_in_tree_allowed_file(self):
		self._write({"hooks.py": "app_title = 'Fake'\n"})
		text, size = app_source.read_source_file("fakeapp", "hooks.py", src_dir=self.app_dir)
		self.assertIn("app_title", text)
		self.assertGreater(size, 0)

	def test_read_source_file_rejects_traversal_absolute_and_bad_ext(self):
		self._write({"hooks.py": "h\n", "logo.png": "x", "big.py": "#" * _BIG})
		for bad in ("../secret.py", "/etc/passwd", "logo.png", "big.py", "nope.py"):
			with self.assertRaises(ValueError):
				app_source.read_source_file("fakeapp", bad, src_dir=self.app_dir)
