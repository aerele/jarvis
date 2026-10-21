import re
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools._export import resolvers
from jarvis.tools._export.resolvers import from_query
from jarvis.tools.get_list import _child_table_parents


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
		message = str(ctx.exception)
		self.assertIn("more than 2 rows", message)
		self.assertIn("Narrow the filter", message)
		self.assertNotIn("not yet available", message)

	def test_site_setting_can_only_lower_the_immediate_row_limit(self):
		def limit_for(value):
			with mock.patch.dict(frappe.conf, {"jarvis_export_immediate_row_limit": value}):
				return resolvers.immediate_row_limit()

		self.assertEqual(resolvers.immediate_row_limit(), 20_000)
		self.assertEqual(limit_for(5000), 5000)
		self.assertEqual(limit_for("5000"), 5000)
		# Raising is refused: the plugin aborts a call at 30 s, which is why 20,000 exists.
		self.assertEqual(limit_for(50_000), 20_000)
		# Unusable values fall back to the default instead of disabling or breaking exports.
		for bad in ("abc", "", 0, -5, "0", None, True, False, [], 1.5e-3):
			with self.subTest(value=bad):
				self.assertEqual(limit_for(bad), 20_000)
		# The upper clamp follows the module ceiling, which tests lower on purpose.
		with mock.patch.object(resolvers, "_HARD_ROW_CEILING", 2):
			self.assertEqual(limit_for(50_000), 2)

	def test_lowered_site_setting_moves_the_split_and_names_the_number(self):
		filters = {"description": ["like", "resolver-test-%"]}
		with mock.patch.dict(frappe.conf, {"jarvis_export_immediate_row_limit": 2}):
			with self.assertRaisesRegex(resolvers.NeedsBackground, "more than 2 rows"):
				from_query("ToDo", filters=filters, fields=["name"])
			spec = {
				"from": "ToDo",
				"select": ["name"],
				"where": [{"field": "description", "op": "like", "value": "resolver-test-%"}],
			}
			with self.assertRaisesRegex(resolvers.NeedsBackground, "more than 2 rows"):
				resolvers.from_spec(spec)
		# Without the setting the same three rows are exported at once.
		self.assertEqual(from_query("ToDo", filters=filters, fields=["name"]).total, 3)

	def test_stray_parent_doctype_is_dropped_for_a_normal_doctype(self):
		# parent_doctype means something only for a child table; it must not turn a
		# normal query into a "DocType not found" error.
		m = from_query(
			"ToDo",
			filters={"description": ["like", "resolver-test-%"]},
			fields=["name"],
			parent_doctype="User",
		)
		self.assertEqual(m.total, 3)

	def test_exact_ceiling_does_not_raise(self):
		# Boundary: exactly ceiling rows (N == ceiling) must SUCCEED, not raise.
		with mock.patch.object(resolvers, "_HARD_ROW_CEILING", 3):
			m = from_query("ToDo", filters={"description": ["like", "resolver-test-%"]}, fields=["name"])
		self.assertEqual(m.total, 3)


class TestExportPermGuards(FrappeTestCase):
	"""B (Export DocPerm) + doctype guards, on a real doctype (ToDo)."""

	USER = "jve-noexport@example.com"

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", self.USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": self.USER,
					"first_name": "noexport",
					"send_welcome_email": 0,
					"enabled": 1,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		if "System Manager" in frappe.get_roles(self.USER):
			frappe.get_doc("User", self.USER).remove_roles("System Manager")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_denies_without_export_permission(self):
		# The user can READ ToDo (role "All") but has no Export DocPerm on it, so a
		# bulk export must be refused (B), even though a plain read/get_list works.
		frappe.set_user(self.USER)
		self.assertTrue(frappe.has_permission("ToDo", "read"))
		with self.assertRaises(PermissionDeniedError) as ctx:
			from_query("ToDo", fields=["name"])
		self.assertIn("export", str(ctx.exception).lower())

	def test_missing_and_unknown_doctype_guards(self):
		with self.assertRaises(InvalidArgumentError):
			from_query(None, fields=["name"])
		with self.assertRaises(InvalidArgumentError) as ctx:
			from_query("JV No Such Doctype ZZZ", fields=["name"])
		self.assertIn("unknown DocType", str(ctx.exception))


class TestFieldLevelAlignment(FrappeTestCase):
	"""Field-level (permlevel) enforcement + column alignment, on a custom doctype
	where the user HAS export perm (B passes) but lacks permlevel-1 read.
	Mutation-proof for the v17 as_dict-projection fix: with the old positional
	as_list code, v17 drops the permlevel field and every column shifts left."""

	DT = "JV Export Perm Test"
	ROLE = "JVE Owner Role"
	USER = "jve-owner@example.com"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Role", cls.ROLE):
			frappe.get_doc(
				{"doctype": "Role", "role_name": cls.ROLE, "desk_access": 1, "is_custom": 1}
			).insert(ignore_permissions=True)
		# Recreate each run so the perms are always the current fixture's (a stale
		# copy from a prior run would otherwise persist - custom doctypes commit).
		if frappe.db.exists("DocType", cls.DT):
			frappe.delete_doc("DocType", cls.DT, force=True, ignore_permissions=True)
		if True:
			from frappe.core.doctype.doctype.test_doctype import new_doctype

			new_doctype(
				cls.DT,
				fields=[
					{"label": "Tag", "fieldname": "tag", "fieldtype": "Data"},
					{
						"label": "Secret",
						"fieldname": "secret",
						"fieldtype": "Data",
						"permlevel": 1,
						"default": "SECRET",
					},
				],
				permissions=[
					{"role": "System Manager", "read": 1, "write": 1, "create": 1, "export": 1},
					{"role": cls.ROLE, "read": 1, "export": 1, "permlevel": 0},
				],
			).insert(ignore_permissions=True)
		if not frappe.db.exists("User", cls.USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": cls.USER,
					"first_name": "owner",
					"send_welcome_email": 0,
					"enabled": 1,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)
		u = frappe.get_doc("User", cls.USER)
		if "System Manager" in frappe.get_roles(cls.USER):
			u.remove_roles("System Manager")
		if cls.ROLE not in frappe.get_roles(cls.USER):
			u.add_roles(cls.ROLE)
		frappe.clear_cache()  # refresh role/permission caches for the new doctype

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.delete(self.DT)
		# 3 rows, each carrying secret="SECRET" (a permlevel-1 field the user can't read).
		for i in range(3):
			frappe.get_doc({"doctype": self.DT, "tag": f"u{i}"}).insert(ignore_permissions=True)
		frappe.clear_cache(user=self.USER)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_field_level_projection_keeps_columns_aligned(self):
		frappe.set_user(self.USER)
		m = from_query(self.DT, fields=["name", "tag", "secret"])
		self.assertEqual(m.total, 3)  # export perm (B) passes; all rows visible
		self.assertEqual(m.columns, ["name", "tag", "secret"])
		s_idx, t_idx = m.columns.index("secret"), m.columns.index("tag")
		for row in m.rows:
			self.assertEqual(len(row), 3)  # alignment holds on every engine (v16/v17)
			# Field-level: permlevel-1 "secret" is blank for this user...
			self.assertIn(row[s_idx], (None, ""))
			# ...and "tag" (its real value) sits under the "tag" header, not shifted.
			self.assertTrue(str(row[t_idx]).startswith("u"))


class TestFromQueryGateTexts(FrappeTestCase):
	"""The exact words (and the order) the agent's error handling keys on.

	The class-only versions above survive a rewording; these do not. The background export
	is being moved onto the same gate, so the immediate texts are pinned first."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_doctype_is_required(self):
		with self.assertRaisesRegex(InvalidArgumentError, "^doctype is required$"):
			from_query(None, fields=["name"])

	def test_guest_has_no_read_permission_on_user(self):
		frappe.set_user("Guest")
		with self.assertRaisesRegex(PermissionDeniedError, "^no read permission on User$"):
			from_query("User", fields=["name"])

	def test_star_fields_tell_the_caller_to_name_columns(self):
		with self.assertRaisesRegex(InvalidArgumentError, "name the columns you want"):
			from_query("ToDo", fields=["*"])

	def test_an_unknown_doctype_is_reported_before_the_star_fields(self):
		with self.assertRaises(InvalidArgumentError) as ctx:
			from_query("JV No Such", fields=["*"])
		self.assertIn("unknown DocType: JV No Such", str(ctx.exception))
		self.assertNotIn("name the columns", str(ctx.exception))

	def test_the_child_table_hint_comes_before_the_permission_check(self):
		parents = _child_table_parents("Has Role")
		self.assertTrue(parents)
		frappe.set_user("Guest")
		with self.assertRaisesRegex(InvalidArgumentError, re.escape(f"parent_doctype='{parents[0]}'")):
			from_query("Has Role", fields=["role"])

	def test_a_child_export_needs_export_permission_on_the_parent(self):
		with mock.patch("frappe.permissions.can_export", side_effect=lambda d, *a, **k: d != "User"):
			with self.assertRaisesRegex(PermissionDeniedError, "^no export permission on User$"):
				from_query("Has Role", parent_doctype="User", fields=["role"])
