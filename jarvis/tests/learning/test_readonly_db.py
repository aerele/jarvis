"""Fence tests for the PatternDB SELECT-only facade (plan section 5.4).

Two layers under test: statement validation (pure, backend-agnostic) and
the database-enforced READ ONLY transaction + MariaDB per-statement kill
(skipped on non-MariaDB backends; Postgres containment is facade-only,
documented in readonly_db).
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.learning.compat import statement_timeout_prefix
from jarvis.learning.readonly_db import (
	PatternDB,
	PatternDBViolation,
	read_only_transaction,
	validate_select,
)


class TestValidateSelect(FrappeTestCase):
	def assert_rejected(self, query):
		with self.assertRaises(PatternDBViolation):
			validate_select(query)

	def test_rejects_write_statements(self):
		self.assert_rejected("INSERT INTO `tabUser` (name) VALUES ('x')")
		self.assert_rejected("UPDATE `tabUser` SET enabled = 0")
		self.assert_rejected("DELETE FROM `tabUser`")
		self.assert_rejected("DROP TABLE `tabUser`")
		self.assert_rejected("TRUNCATE `tabUser`")
		self.assert_rejected("CREATE TABLE x (id int)")
		self.assert_rejected("ALTER TABLE `tabUser` ADD COLUMN x int")

	def test_rejects_semicolon_anywhere(self):
		self.assert_rejected("SELECT 1; DROP TABLE `tabUser`")
		self.assert_rejected("SELECT 1;")
		self.assert_rejected("SELECT ';' AS v")

	def test_rejects_into_variants(self):
		self.assert_rejected("SELECT name INTO OUTFILE '/tmp/x' FROM `tabUser`")
		self.assert_rejected("SELECT name INTO @v FROM `tabUser`")
		self.assert_rejected("SELECT name into dumpfile '/tmp/x' FROM `tabUser`")

	def test_rejects_locking_reads(self):
		self.assert_rejected("SELECT name FROM `tabUser` FOR UPDATE")
		self.assert_rejected("SELECT name FROM `tabUser` for  update")
		self.assert_rejected("SELECT name FROM `tabUser` LOCK IN SHARE MODE")

	def test_rejects_comment_hidden_write(self):
		self.assert_rejected("/* just reading */ UPDATE `tabUser` SET enabled = 0")
		self.assert_rejected("-- select\nDELETE FROM `tabUser`")
		self.assert_rejected("# select\nDELETE FROM `tabUser`")

	def test_rejects_comment_only_and_empty(self):
		self.assert_rejected("")
		self.assert_rejected(None)
		self.assert_rejected("-- nothing here")
		self.assert_rejected("/* unterminated SELECT 1")

	def test_accepts_plain_select(self):
		validate_select("SELECT 1")
		validate_select("select name from `tabUser` where enabled = 1")

	def test_accepts_leading_comments_before_select(self):
		validate_select("-- detector: buy-supplier-stockness\nSELECT 1")
		validate_select("/* v1 */ SELECT 1")
		validate_select("  \n\t/* a */ -- b\n SELECT 1")


class TestPatternDBExecution(FrappeTestCase):
	def test_select_with_params(self):
		rows = PatternDB().sql_select("SELECT %(v)s AS v", {"v": 1})
		self.assertEqual(rows, [{"v": 1}])

	def test_select_as_list(self):
		rows = PatternDB().sql_select("SELECT 2 AS v", as_dict=False)
		self.assertEqual(rows[0][0], 2)

	def test_execution_path_rejects_writes_too(self):
		with self.assertRaises(PatternDBViolation):
			PatternDB().sql_select("UPDATE `tabUser` SET enabled = 0")

	def test_timed_select_validates_inner_query(self):
		with self.assertRaises(PatternDBViolation):
			PatternDB().timed_select("DELETE FROM `tabUser`", timeout_s=5)

	def test_timed_select_returns_rows(self):
		rows = PatternDB().timed_select("SELECT 3 AS v", timeout_s=5)
		self.assertEqual(rows, [{"v": 3}])

	def test_statement_timeout_prefix_is_mariadb_idiom(self):
		# max_statement_time in SECONDS; MySQL's max_execution_time does
		# not exist on MariaDB.
		self.assertEqual(
			statement_timeout_prefix("SELECT 1", 30),
			"SET STATEMENT max_statement_time=30 FOR SELECT 1",
		)
		self.assertNotIn("max_execution_time", statement_timeout_prefix("SELECT 1", 30))


class TestReadOnlyTransaction(FrappeTestCase):
	"""Database-level containment; MariaDB only (the primary backend)."""

	def setUp(self):
		super().setUp()
		if frappe.db.db_type != "mariadb":
			self.skipTest("READ ONLY transaction containment is MariaDB-only")
		# START TRANSACTION would refuse (ImplicitCommitError) with writes
		# pending; begin the fence from a clean transaction.
		frappe.db.rollback()

	def test_write_inside_fence_fails_at_db(self):
		probe = "__jarvis_ro_probe"
		with read_only_transaction() as pdb:
			self.assertEqual(pdb.sql_select("SELECT 1 AS v"), [{"v": 1}])
			# Raw write on the SAME connection, bypassing the facade
			# entirely: the DB itself must refuse (errno 1792, surfaced by
			# frappe as InReadOnlyMode).
			with self.assertRaises(Exception):
				frappe.db.sql(
					"insert into `tabDefaultValue` (name, defkey, defvalue) values (%s, %s, %s)",
					(probe, probe, "1"),
				)
		self.assertFalse(frappe.db.sql("select name from `tabDefaultValue` where name = %s", (probe,)))

	def test_reads_work_and_connection_usable_after_exit(self):
		with read_only_transaction() as pdb:
			rows = pdb.sql_select("SELECT name FROM `tabDocType` WHERE name = %(n)s", {"n": "User"})
			self.assertEqual(rows[0]["name"], "User")
		# ROLLBACK on exit: the connection must be writable again.
		frappe.db.sql("select 1")

	def test_timed_select_kills_slow_query(self):
		# Plan section 4.5: a deliberately slow query is actually killed on
		# MariaDB (errno 1969, max_statement_time exceeded). Verified
		# behavior on this bench: raises after ~1s.
		with read_only_transaction() as pdb:
			with self.assertRaises(Exception) as ctx:
				pdb.timed_select("SELECT SLEEP(5)", timeout_s=1)
			self.assertIn("1969", str(ctx.exception))
