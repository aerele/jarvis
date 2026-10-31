"""Snapshot and restore every raw Jarvis Settings row around a test.

Every new test that touches Jarvis Settings (fields, passwords, or code that
resets/rewrites them) MUST use ``JarvisSettingsSnapshot``. e2e2 is the owner's
shared test site and code under test can commit, so per-field restores leak.
"""

import hashlib

import frappe

SETTINGS = "Jarvis Settings"


def _rows():
	# Raw SQL is acceptable: this is a test fixture copying and restoring raw rows.
	singles = frappe.db.sql(
		"select doctype, field, value from tabSingles where doctype=%s", SETTINGS
	)
	auth = frappe.db.sql(
		"select doctype, name, fieldname, password, encrypted from `__Auth` where doctype=%s", SETTINGS
	)
	return singles, auth


def snapshot_hash():
	"""sha256 + row counts of the raw rows (never prints values). For bench execute."""
	singles, auth = _rows()
	h = hashlib.sha256(repr((sorted(map(tuple, singles)), sorted(map(tuple, auth)))).encode()).hexdigest()
	print(f"hash={h} tabSingles={len(singles)} __Auth={len(auth)}")


class JarvisSettingsSnapshot:
	"""Mixin: put first in the bases, e.g. ``class T(JarvisSettingsSnapshot, FrappeTestCase)``."""

	def setUp(self):
		super().setUp()
		self._js_singles, self._js_auth = _rows()

	def tearDown(self):
		frappe.db.sql("delete from tabSingles where doctype=%s", SETTINGS)
		frappe.db.sql("delete from `__Auth` where doctype=%s", SETTINGS)
		for row in self._js_singles:
			frappe.db.sql("insert into tabSingles (doctype, field, value) values (%s, %s, %s)", row)
		for row in self._js_auth:
			frappe.db.sql(
				"insert into `__Auth` (doctype, name, fieldname, password, encrypted) "
				"values (%s, %s, %s, %s, %s)",
				row,
			)
		# Commit the restore: code under test may have committed, and the test
		# class's final rollback would otherwise revert to that leaked state.
		frappe.db.commit()
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		frappe.clear_cache(doctype=SETTINGS)
		super().tearDown()
