"""Snapshot and restore every raw Jarvis Settings row (Singles, __Auth and child tables) around a test.

Every new test that touches Jarvis Settings (fields, passwords, or code that
resets/rewrites them) MUST use ``JarvisSettingsSnapshot``. e2e2 is the owner's
shared test site and code under test can commit, so per-field restores leak.
"""

import hashlib

import frappe

SETTINGS = "Jarvis Settings"


def _child_doctypes():
	meta = frappe.get_meta(SETTINGS)
	return sorted({f.options for f in meta.get_table_fields() if f.options})


def _rows():
	# Raw SQL is acceptable: this is a test fixture copying and restoring raw rows.
	singles = frappe.db.sql(
		"select doctype, field, value from tabSingles where doctype=%s", SETTINGS
	)
	auth = frappe.db.sql(
		"select doctype, name, fieldname, password, encrypted from `__Auth` where doctype=%s", SETTINGS
	)
	return singles, auth


def _child_rows():
	"""{child doctype: [row dict]} for every child row whose parent is Jarvis Settings."""
	return {
		dt: frappe.db.sql(
			f"select * from `tab{dt}` where parenttype=%s order by name", SETTINGS, as_dict=True
		)
		for dt in _child_doctypes()
	}


def _restore_child_rows(snapshot):
	for dt, rows in snapshot.items():
		frappe.db.sql(f"delete from `tab{dt}` where parenttype=%s", SETTINGS)
		for row in rows:
			cols = ", ".join(f"`{c}`" for c in row)
			marks = ", ".join(["%s"] * len(row))
			frappe.db.sql(f"insert into `tab{dt}` ({cols}) values ({marks})", tuple(row.values()))


def compute_hash():
	"""(sha256, tabSingles count, __Auth count, child-row count) of the raw rows."""
	singles, auth = _rows()
	child = _child_rows()
	child_norm = {dt: sorted(repr(sorted(r.items())) for r in rows) for dt, rows in child.items()}
	payload = repr((sorted(map(tuple, singles)), sorted(map(tuple, auth)), sorted(child_norm.items())))
	return (
		hashlib.sha256(payload.encode()).hexdigest(),
		len(singles),
		len(auth),
		sum(len(r) for r in child.values()),
	)


def snapshot_hash():
	"""Print hash + row counts only, never values. For bench execute."""
	print("hash=%s tabSingles=%s __Auth=%s childRows=%s" % compute_hash())


class JarvisSettingsSnapshot:
	"""Mixin: put first in the bases, e.g. ``class T(JarvisSettingsSnapshot, FrappeTestCase)``."""

	def setUp(self):
		super().setUp()
		self._take_snapshot()

	def _take_snapshot(self):
		self._js_singles, self._js_auth = _rows()
		self._js_child = _child_rows()
		# A cleanup runs even when a later setUp step raises (tearDown would not).
		self.addCleanup(self._restore)

	def _restore(self):
		try:
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
			_restore_child_rows(self._js_child)
			# Commit the restore: code under test may have committed, and the test
			# class's final rollback would otherwise revert to that leaked state.
			frappe.db.commit()
		except Exception:
			# Never commit a partial state.
			frappe.db.rollback()
			raise
		finally:
			frappe.clear_document_cache(SETTINGS, SETTINGS)
			frappe.clear_cache(doctype=SETTINGS)
