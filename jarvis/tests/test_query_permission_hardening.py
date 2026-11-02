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
from jarvis.tools._export import masking, resolvers


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


class TestChildJoinedToAnotherParent(_NonAdmin):
	"""A child joined to a DocType with no Table field for it (Sales Team on a
	Quotation) is a wrong query, not a permission problem."""

	OWNING = ["Customer", "Delivery Note", "Sales Order"]
	ALIASES = {"q": ("Quotation", None), "so": ("Sales Order", None), "st": ("Sales Team", None)}

	def _resolve(self, spec):
		with (
			patch.object(get_list_mod, "_child_table_parents", return_value=self.OWNING),
			patch.object(get_list_mod, "_readable_child_parents", return_value=self.OWNING),
			patch("frappe.has_permission", return_value=True),
		):
			return query_mod._child_scoping_parent("Sales Team", "st", spec, self.ALIASES)

	def test_a_join_to_a_parent_without_the_table_names_the_real_parents(self):
		spec = {"joins": [{"alias": "st", "doctype": "Sales Team", "on": {"st.parent": "q.name"}}]}
		with self.assertRaisesRegex(
			InvalidArgumentError, "'Quotation' has no 'Sales Team' table.*Sales Order"
		):
			self._resolve(spec)

	def test_a_parenttype_naming_a_parent_without_the_table_is_refused_the_same_way(self):
		spec = {
			"joins": [{"alias": "st", "doctype": "Sales Team", "on": {"st.parent": "q.name"}}],
			"where": [{"field": "st.parenttype", "op": "=", "value": "Quotation"}],
		}
		with self.assertRaisesRegex(InvalidArgumentError, "'Quotation' has no 'Sales Team' table"):
			self._resolve(spec)

	def test_a_join_to_an_owning_parent_still_scopes_to_it(self):
		spec = {"joins": [{"alias": "st", "doctype": "Sales Team", "on": {"st.parent": "so.name"}}]}
		self.assertEqual(self._resolve(spec), "Sales Order")

	def test_no_signal_with_several_readable_parents_is_still_the_ambiguous_denial(self):
		with self.assertRaisesRegex(PermissionDeniedError, "multiple parent DocTypes"):
			self._resolve({})


class TestQueryRowMasking(_NonAdmin):
	"""Frappe 16 masks a ``mask=1`` field for a user without the mask permission.
	The query tool builds rows with the query builder directly, so it masks them
	itself. ``mask_field_value`` is stubbed so the same tests run on Frappe 15."""

	PHONE = frappe._dict(fieldname="description", fieldtype="Data", options="Phone")
	# A standard column, so building the query under the mocked Engine skips the permlevel lookup.
	MODIFIED = frappe._dict(fieldname="modified", fieldtype="Datetime", options=None)

	def _run(self, spec, rows, masked_fields=(PHONE,), feature=True):
		stub = (lambda field, val: "MASKED") if feature else None
		with (
			patch.object(masking, "mask_field_value", stub),
			patch("jarvis.compat.masked_fields_of", return_value=list(masked_fields)) as lookup,
			patch.object(query_mod, "compile_spec") as compile_query,
		):
			q = compile_query.return_value
			q.limit.return_value = q
			q.get_sql.return_value = "SELECT"
			q.run.return_value = rows
			q._selects = [MagicMock(fields_=MagicMock(return_value=[])) for _ in spec["select"]]
			return query_mod.query(spec), lookup

	def test_a_masked_column_is_masked_in_the_rows(self):
		result, _ = self._run(
			{"from": "ToDo", "select": ["name", "description"]},
			[{"name": "T1", "description": "9876543210"}],
		)
		self.assertEqual(result["rows"], [{"name": "T1", "description": "MASKED"}])

	def test_a_column_that_is_not_masked_is_untouched(self):
		result, _ = self._run(
			{"from": "ToDo", "select": ["name", "status"]},
			[{"name": "T1", "status": "Open"}],
		)
		self.assertEqual(result["rows"], [{"name": "T1", "status": "Open"}])

	def test_a_joined_alias_is_masked_by_its_own_doctype(self):
		result, _ = self._run(
			{
				"from": "ToDo",
				"alias": "t",
				"joins": [{"doctype": "User", "alias": "u", "on": {"u.name": "t.owner"}}],
				"select": ["t.name", "u.description"],
			},
			[{"name": "T1", "description": "9876543210"}],
		)
		self.assertEqual(result["rows"][0]["description"], "MASKED")

	def test_without_the_masking_feature_rows_are_returned_as_is(self):
		result, lookup = self._run(
			{"from": "ToDo", "select": ["name", "description"]},
			[{"name": "T1", "description": "9876543210"}],
			feature=False,
		)
		self.assertEqual(result["rows"][0]["description"], "9876543210")
		lookup.assert_not_called()

	def test_administrator_is_never_masked(self):
		frappe.set_user("Administrator")
		result, lookup = self._run(
			{"from": "ToDo", "select": ["name", "description"]},
			[{"name": "T1", "description": "9876543210"}],
		)
		self.assertEqual(result["rows"][0]["description"], "9876543210")
		lookup.assert_not_called()

	def test_a_computed_column_over_a_masked_field_is_refused_before_the_select_runs(self):
		spec = {
			"from": "ToDo",
			"alias": "t",
			"select": [{"agg": "max", "field": "t.modified", "as": "latest"}],
		}
		frappe.set_user("Administrator")
		with (
			patch("frappe.has_permission", return_value=True),
			patch("frappe.database.query.Engine") as fake_engine,
		):
			fake_engine.return_value.get_permission_conditions.return_value = None
			q = query_mod.compile_spec(spec)
		frappe.set_user("Guest")
		with (
			patch.object(masking, "mask_field_value", lambda field, val: "MASKED"),
			patch("jarvis.compat.masked_fields_of", return_value=[self.MODIFIED]),
			patch.object(query_mod, "compile_spec", return_value=q),
			patch.object(q, "run", create=True) as run,
		):
			with self.assertRaisesRegex(InvalidArgumentError, "masked"):
				query_mod.query(spec)
		run.assert_not_called()

	def test_columns_that_do_not_line_up_with_the_query_terms_are_refused(self):
		spec = {"from": "ToDo", "select": ["name", "description"]}
		with self.assertRaisesRegex(InvalidArgumentError, "masking"):
			self._run_with_terms(spec, terms=1)

	def test_two_plain_columns_sharing_a_key_are_refused_when_one_is_masked(self):
		spec = {
			"from": "ToDo",
			"alias": "t",
			"joins": [{"doctype": "User", "alias": "u", "on": {"u.name": "t.owner"}}],
			"select": ["t.description", "u.description"],
		}
		with self.assertRaisesRegex(InvalidArgumentError, "more than one table"):
			self._run_with_terms(spec, terms=2, masked_alias="u")

	def test_two_plain_columns_sharing_a_key_pass_when_neither_is_masked(self):
		spec = {
			"from": "ToDo",
			"alias": "t",
			"joins": [{"doctype": "User", "alias": "u", "on": {"u.name": "t.owner"}}],
			"select": ["t.name", "u.name", "t.status"],
		}
		result, _ = self._run(spec, [{"name": "U1", "status": "Open"}])
		self.assertEqual(result["rows"], [{"name": "U1", "status": "Open"}])

	def _run_with_terms(self, spec, terms, masked_alias=None):
		"""Run ``query`` with a query that exposes ``terms`` select terms and a
		masked ``description`` (on every table, or only on ``masked_alias``)."""

		def lookup(meta, parenttype=None):
			if masked_alias is not None and meta.name != {"u": "User"}[masked_alias]:
				return []
			return [self.PHONE]

		with (
			patch.object(masking, "mask_field_value", lambda field, val: "MASKED"),
			patch("jarvis.compat.masked_fields_of", side_effect=lookup),
			patch.object(query_mod, "compile_spec") as compile_query,
		):
			q = compile_query.return_value
			q.limit.return_value = q
			q.run.return_value = []
			q._selects = [MagicMock(fields_=MagicMock(return_value=[])) for _ in range(terms)]
			return query_mod.query(spec)


class TestExportChildReadThroughParentField(_NonAdmin):
	def _authorize(self, levels):
		parent = _parent_meta([("items", "Child Row", 1)], levels)
		child = MagicMock(istable=True)
		with (
			patch("frappe.get_meta", side_effect=lambda dt, *a, **k: child if dt == "Child Row" else parent),
			patch("frappe.db.exists", return_value=True),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.permissions.can_export", return_value=True) as can_export,
			patch.object(resolvers, "assert_child_query_fields_readable"),
			patch.object(masking, "masked_fields", return_value={}),
		):
			try:
				return resolvers.authorize_filter("Child Row", ["name"], None, None, "Parent Doc")
			finally:
				self.can_export = can_export

	def test_export_refuses_a_child_when_the_parent_table_field_is_hidden(self):
		with self.assertRaises(PermissionDeniedError):
			self._authorize([0])
		self.can_export.assert_not_called()

	def test_export_reads_the_child_when_the_parent_table_field_is_readable(self):
		doctype, parent_doctype, _fields, _columns, _masked = self._authorize([0, 1])
		self.assertEqual((doctype, parent_doctype), ("Child Row", "Parent Doc"))
