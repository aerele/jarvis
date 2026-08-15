from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._export import resolvers
from jarvis.tools._export.resolvers import from_query


class TestFromQuery(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		# Deterministic regardless of cross-test rollback behaviour: start from a
		# known-empty set for our marker, then insert exactly 3.
		frappe.db.delete("ToDo", {"description": ["like", "resolver-test-%"]})
		for i in range(3):
			frappe.get_doc({"doctype": "ToDo", "description": f"resolver-test-{i}"}).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_returns_all_matching_rows_uncapped(self):
		m = from_query(
			"ToDo",
			filters={"description": ["like", "resolver-test-%"]},
			fields=["name", "description"],
		)
		self.assertEqual(m.total, 3)
		self.assertEqual(len(m.rows), 3)
		self.assertEqual(m.columns, ["name", "description"])

	def test_default_fields_when_none(self):
		m = from_query("ToDo", filters={"description": ["like", "resolver-test-%"]})
		self.assertIn("name", m.columns)

	def test_rejects_star_fields(self):
		with self.assertRaises(InvalidArgumentError):
			from_query("ToDo", fields=["*"])

	def test_child_table_needs_parent_doctype(self):
		# "Has Role" is a child table (istable) of User.
		with self.assertRaises(InvalidArgumentError) as ctx:
			from_query("Has Role", fields=["role"])
		self.assertIn("child table", str(ctx.exception))

	def test_denies_doctype_without_read(self):
		frappe.set_user("Guest")
		with self.assertRaises(PermissionDeniedError):
			from_query("User", fields=["name"])

	def test_fails_closed_above_ceiling(self):
		# 3 matching rows, ceiling forced to 2 -> must RAISE, never a partial file.
		with mock.patch.object(resolvers, "_HARD_ROW_CEILING", 2):
			with self.assertRaises(InvalidArgumentError) as ctx:
				from_query("ToDo", filters={"description": ["like", "resolver-test-%"]}, fields=["name"])
		self.assertIn("too many to export", str(ctx.exception))
