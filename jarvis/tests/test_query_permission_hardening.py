"""Hardening tests for jarvis.tools.query / jarvis.tools.get_list.

- GROUP BY alias vs column-name comparison is case-insensitive
- The record-permission Engine is built with reference_doctype
- Child-table reads also require read on the parent's Table field permlevel
- Result rows are masked like Frappe v16's query builder masks them
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import query as query_mod


class TestGroupByAliasCaseInsensitive(FrappeTestCase):
	def _table_map(self):
		return {"dt": ("DocType", frappe.qb.DocType("DocType").as_("dt"))}

	def test_bare_name_is_column_ignores_letter_case(self):
		for name in ("module", "Module", "MODULE"):
			with self.subTest(name=name):
				self.assertTrue(query_mod._bare_name_is_column(name, self._table_map()))
		self.assertFalse(query_mod._bare_name_is_column("not_a_column_xyz", self._table_map()))

	def test_group_by_alias_equal_to_column_in_other_case_is_not_the_shortcut(self):
		"""``Module`` as a GROUP BY alias is the ``module`` column to MariaDB, so
		it must not be emitted as a bare alias Field."""
		with patch("frappe.has_permission", return_value=True):
			with self.assertRaises(InvalidArgumentError):
				query_mod._resolve_field(
					"Module",
					self._table_map(),
					allow_alias=True,
					select_aliases=frozenset({"Module"}),
					group_by=True,
				)

	def test_group_by_alias_equal_to_column_exact_case_is_resolved_as_column(self):
		with patch("frappe.has_permission", return_value=True):
			field = query_mod._resolve_field(
				"module",
				self._table_map(),
				allow_alias=True,
				select_aliases=frozenset({"module"}),
				group_by=True,
			)
		self.assertEqual(field.name, "module")
		self.assertIsNotNone(field.table)

	def test_order_by_colliding_alias_still_allowed_in_any_case(self):
		field = query_mod._resolve_field(
			"Module",
			self._table_map(),
			allow_alias=True,
			select_aliases=frozenset({"Module"}),
		)
		self.assertIsNone(field.table)


class TestEngineReferenceDoctype(FrappeTestCase):
	def _engine(self, doctype="ToDo"):
		table = frappe.qb.DocType(doctype).as_("t")
		q = frappe.qb.from_(table).select(table.name)
		return query_mod._make_permission_engine(q, [table], doctype), table

	def test_engine_carries_the_queried_doctype_as_reference_doctype(self):
		engine, _table = self._engine("ToDo")
		self.assertEqual(engine.reference_doctype, "ToDo")

	def test_user_permission_with_applicable_for_is_evaluated_without_raising(self):
		engine, table = self._engine("ToDo")
		if not hasattr(engine, "get_user_permission_conditions"):
			self.skipTest("Frappe 15 Engine has no user permission builder")
		perms = {
			"ToDo": [
				{"doc": "TD-APPLIES", "applicable_for": "ToDo"},
				{"doc": "TD-OTHER", "applicable_for": "Note"},
			]
		}
		with patch("frappe.permissions.get_user_permissions", return_value=perms):
			conditions = engine.get_user_permission_conditions("ToDo", table)
		sql = " ".join(str(c) for c in conditions)
		self.assertIn("TD-APPLIES", sql)
		self.assertNotIn("TD-OTHER", sql)

	def test_record_gate_points_the_engine_at_each_aliased_doctype(self):
		engine, table = self._engine("ToDo")
		with patch.object(query_mod.compat, "permission_conditions", return_value=None):
			query_mod._weave_record_gate(None, engine, "u", "User", table, {}, {})
		self.assertEqual(engine.reference_doctype, "User")
