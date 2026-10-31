"""Tests for the Jarvis Settings snapshot helper. Mutations are limited to one
probe row in tabSingles, and every test asserts the hash is back afterwards."""

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.tests._settings_snapshot import SETTINGS, JarvisSettingsSnapshot, _child_doctypes, _child_rows, _rows, compute_hash

PROBE = "zz_snapshot_probe"


class TestSettingsSnapshot(FrappeTestCase):
	def test_failing_restore_rolls_back_and_reraises(self):
		before = compute_hash()
		probe = JarvisSettingsSnapshot()
		probe._js_singles, probe._js_auth = _rows()
		probe._js_child = _child_rows()
		real_sql = frappe.db.sql

		def flaky(query, *args, **kwargs):
			if str(query).lstrip().startswith("insert"):
				raise RuntimeError("boom")
			return real_sql(query, *args, **kwargs)

		with patch.object(frappe.db, "sql", side_effect=flaky):
			with self.assertRaises(RuntimeError):
				probe._restore()
		self.assertEqual(compute_hash(), before)

	def test_setup_failure_after_snapshot_still_restores(self):
		before = compute_hash()

		class Inner(JarvisSettingsSnapshot, unittest.TestCase):
			def setUp(self):
				super().setUp()
				frappe.db.sql(
					"insert into tabSingles (doctype, field, value) values (%s, %s, %s)",
					(SETTINGS, PROBE, "1"),
				)
				frappe.db.commit()
				raise RuntimeError("setUp failed")

			def runTest(self):
				pass  # never reached

		result = unittest.TestResult()
		Inner().run(result)
		self.assertEqual(len(result.errors), 1)
		self.assertEqual(compute_hash(), before)

	def test_child_rows_restored_after_insert_and_delete(self):
		child_dts = _child_doctypes()
		self.assertTrue(child_dts)
		before = compute_hash()
		probe = JarvisSettingsSnapshot()
		probe._js_singles, probe._js_auth = _rows()
		probe._js_child = _child_rows()
		dt = child_dts[0]
		rows = probe._js_child[dt]
		if rows:
			# a deleted row must come back
			frappe.db.sql(f"delete from `tab{dt}` where name=%s", rows[0]["name"])
		else:
			# an inserted row must go away
			template = frappe.db.sql(f"select * from `tab{dt}` limit 1", as_dict=True)
			self.assertFalse(template and template[0].get("parenttype") == SETTINGS)
			frappe.db.sql(
				f"insert into `tab{dt}` (name, parent, parenttype, parentfield) "
				"values (%s, %s, %s, %s)",
				("zz_snapshot_probe", SETTINGS, SETTINGS, "zz"),
			)
		self.assertNotEqual(compute_hash(), before)
		probe._restore()
		self.assertEqual(compute_hash(), before)
