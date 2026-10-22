"""Hardening tests for jarvis.tools.query / jarvis.tools.get_list.

- GROUP BY alias vs column-name comparison is case-insensitive
- The record-permission Engine is built with reference_doctype
- Child-table reads also require read on the parent's Table field permlevel
- Result rows are masked like Frappe v16's query builder masks them
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import get_list as get_list_mod
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


def _parent_meta(table_fields, levels):
	"""A parent DocType meta whose Table fields are ``table_fields`` as
	``(fieldname, options, permlevel)``; the caller can read ``levels``."""
	meta = MagicMock(istable=False)
	meta.fields = [
		frappe._dict(fieldname=name, fieldtype="Table", options=options, permlevel=permlevel)
		for name, options, permlevel in table_fields
	]
	meta.get_permlevel_access.return_value = levels
	return meta


class _NonAdmin(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Guest")  # the guards only skip Administrator

	def tearDown(self):
		frappe.set_user("Administrator")


class TestChildTableFieldReadable(_NonAdmin):
	check = staticmethod(get_list_mod.assert_child_table_field_readable)

	def _check(self, table_fields, levels):
		meta = _parent_meta(table_fields, levels)
		with patch("frappe.get_meta", return_value=meta):
			self.check("Child Row", "Parent Doc")
		return meta

	def test_table_field_above_permlevel_zero_without_access_is_refused(self):
		with self.assertRaisesRegex(PermissionDeniedError, "items"):
			self._check([("items", "Child Row", 1)], [0])

	def test_table_field_above_permlevel_zero_with_access_is_allowed(self):
		self._check([("items", "Child Row", 1)], [0, 1])

	def test_permlevel_zero_table_field_needs_no_lookup(self):
		meta = self._check([("items", "Child Row", 0)], [0])
		meta.get_permlevel_access.assert_not_called()

	def test_a_table_field_for_another_child_is_ignored(self):
		self._check([("other", "Other Row", 2)], [0])

	def test_one_hidden_field_among_several_for_the_same_child_refuses(self):
		with self.assertRaisesRegex(PermissionDeniedError, "secret_items"):
			self._check([("items", "Child Row", 0), ("secret_items", "Child Row", 2)], [0, 1])

	def test_administrator_is_not_checked(self):
		frappe.set_user("Administrator")
		with patch("frappe.get_meta") as get_meta:
			self.check("Child Row", "Parent Doc")
		get_meta.assert_not_called()


class TestChildReadThroughParentField(_NonAdmin):
	def _metas(self, levels):
		parent = _parent_meta([("items", "Child Row", 1)], levels)
		child = MagicMock(istable=True)
		return lambda dt, *a, **k: child if dt == "Child Row" else parent

	def test_get_list_refuses_a_child_read_when_the_parent_table_field_is_hidden(self):
		with (
			patch("frappe.get_meta", side_effect=self._metas([0])),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.get_list") as fgl,
		):
			with self.assertRaises(PermissionDeniedError):
				get_list_mod.get_list("Child Row", parent_doctype="Parent Doc")
		fgl.assert_not_called()

	def test_get_list_reads_the_child_when_the_parent_table_field_is_readable(self):
		with (
			patch("frappe.get_meta", side_effect=self._metas([0, 1])),
			patch("frappe.has_permission", return_value=True),
			patch.object(get_list_mod, "assert_child_query_fields_readable"),
			patch("frappe.get_list", return_value=[{"name": "r1"}]) as fgl,
		):
			rows = get_list_mod.get_list("Child Row", parent_doctype="Parent Doc")
		self.assertEqual(rows, [{"name": "r1"}])
		fgl.assert_called_once()

	def test_query_child_gate_refuses_when_the_parent_table_field_is_hidden(self):
		with (
			patch("frappe.get_meta", side_effect=self._metas([0])),
			patch.object(query_mod, "_child_scoping_parent", return_value="Parent Doc"),
			patch.object(query_mod, "_child_record_scope") as scope,
		):
			with self.assertRaises(PermissionDeniedError):
				query_mod._weave_record_gate(None, None, "c", "Child Row", None, {}, {})
		scope.assert_not_called()

	def test_query_child_gate_scopes_the_child_when_the_parent_table_field_is_readable(self):
		with (
			patch("frappe.get_meta", side_effect=self._metas([0, 1])),
			patch.object(query_mod, "_child_scoping_parent", return_value="Parent Doc"),
			patch.object(query_mod, "_child_record_scope", return_value=None) as scope,
		):
			q = query_mod._weave_record_gate("Q", None, "c", "Child Row", None, {}, {})
		self.assertEqual(q, "Q")
		scope.assert_called_once()
