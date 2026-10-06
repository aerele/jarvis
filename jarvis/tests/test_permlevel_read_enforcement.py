"""Field-level (permlevel) read enforcement across the tools that reach item data.

Each test pins one decision the fixes rely on, with the permission inputs
controlled, so it fails on any regression that reopens the leak:

- query, a child JOINed under a DocType that does not own it (#4)
- query, a bare ORDER BY / GROUP BY name (#15)
- get_list / export_query, a child filter / sort / aggregate (#5)
- load_doc and the draft form meta (#6)
- get_itemised_tax_breakup (#7)

The end-to-end behaviour (restricted user, hidden rate) was verified against a
real ERPNext site; these keep the logic from drifting.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import actions_api
from jarvis.exceptions import PermissionDeniedError
from jarvis.tools import get_itemised_tax_breakup as tax_mod
from jarvis.tools import get_list as get_list_mod
from jarvis.tools import query as query_mod

_RESTRICTED = "Guest"  # any non-Administrator session: the guards only skip Administrator


class _NonAdmin(FrappeTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")


# --------------------------------------------------------------------------- #
# #4  query: a child's field ACL must come from a parent that OWNS it
# --------------------------------------------------------------------------- #
class TestChildAclParent(_NonAdmin):
	@staticmethod
	def _fake_permitted(doctype, parenttype, permission_type, ignore_virtual):
		# Sales Order grants the rate; Delivery Note (the real parent) does not.
		return {"Sales Order": ["item_code", "rate"], "Delivery Note": ["item_code"]}[parenttype]

	def test_a_from_doctype_that_does_not_own_the_child_is_ignored(self):
		with (
			patch.object(query_mod, "get_permitted_fields", side_effect=self._fake_permitted) as gpf,
			patch("jarvis.tools.get_list._child_table_parents", return_value=["Delivery Note"]),
			patch("jarvis.tools.get_list._readable_child_parents", return_value=["Delivery Note"]),
		):
			got = query_mod._compute_permitted_read_fields("Delivery Note Item", "Sales Order")
		self.assertEqual(got, frozenset({"item_code"}))
		self.assertNotIn("Sales Order", [c.kwargs["parenttype"] for c in gpf.call_args_list])

	def test_an_owning_from_doctype_still_governs_the_child(self):
		with (
			patch.object(query_mod, "get_permitted_fields", side_effect=self._fake_permitted),
			patch("jarvis.tools.get_list._child_table_parents", return_value=["Sales Order"]),
		):
			got = query_mod._compute_permitted_read_fields("Delivery Note Item", "Sales Order")
		self.assertEqual(got, frozenset({"item_code", "rate"}))


# --------------------------------------------------------------------------- #
# #15  query: a bare ORDER BY / GROUP BY name is an alias only if declared
# --------------------------------------------------------------------------- #
class TestBareOrderGroupBy(_NonAdmin):
	def setUp(self):
		self.alias_map = {"t": ("ToDo", frappe.qb.DocType("ToDo"))}

	def _denying_column_check(self):
		return patch.object(
			query_mod, "_validate_column", side_effect=PermissionDeniedError("no read permission on field")
		)

	def test_a_non_alias_bare_name_is_permission_checked_as_a_column(self):
		with self._denying_column_check() as vc, self.assertRaises(PermissionDeniedError):
			query_mod._resolve_field(
				"description", self.alias_map, allow_alias=True, select_aliases=frozenset({"n"})
			)
		vc.assert_called_once()

	def test_a_declared_select_alias_is_emitted_unqualified(self):
		with patch.object(query_mod, "_validate_column") as vc:
			field = query_mod._resolve_field(
				"n", self.alias_map, allow_alias=True, select_aliases=frozenset({"n"})
			)
		vc.assert_not_called()
		self.assertEqual(field.name, "n")

	def test_group_by_alias_shadowing_a_column_is_checked_as_that_column(self):
		# MariaDB resolves GROUP BY names against the FROM columns first.
		with self._denying_column_check() as vc, self.assertRaises(PermissionDeniedError):
			query_mod._resolve_field(
				"description",
				self.alias_map,
				allow_alias=True,
				select_aliases=frozenset({"description"}),
				group_by=True,
			)
		vc.assert_called_once()

	def test_order_by_alias_shadowing_a_column_stays_an_alias(self):
		# ORDER BY resolves SELECT aliases first, so a shadowing alias is safe there.
		with patch.object(query_mod, "_validate_column") as vc:
			field = query_mod._resolve_field(
				"description", self.alias_map, allow_alias=True, select_aliases=frozenset({"description"})
			)
		vc.assert_not_called()
		self.assertEqual(field.name, "description")


# --------------------------------------------------------------------------- #
# #5  get_list / export_query: child filter / sort / aggregate fields
# --------------------------------------------------------------------------- #
class TestChildQueryFieldGuard(_NonAdmin):
	CHILD, PARENT = "Delivery Note Item", "Delivery Note"

	def _meta(self, levels):
		meta = MagicMock(istable=True)
		meta.fields = [
			frappe._dict(fieldname="item_code", permlevel=0),
			frappe._dict(fieldname="rate", permlevel=2),
		]
		meta.get_permlevel_access.return_value = levels
		return meta

	def _check(self, levels=(0,), parent=PARENT, **kw):
		frappe.set_user(_RESTRICTED)
		with patch.object(get_list_mod.frappe, "get_meta", return_value=self._meta(list(levels))):
			get_list_mod.assert_child_query_fields_readable(
				self.CHILD, parent, kw.get("fields"), kw.get("filters"), kw.get("order_by")
			)

	def test_an_aggregate_on_a_hidden_field_is_refused(self):
		for fields in ([{"MAX": "rate"}], ["max(rate)"], ["sum(`rate`) as total"]):
			with self.subTest(fields=fields), self.assertRaises(PermissionDeniedError):
				self._check(fields=fields)

	def test_a_filter_on_a_hidden_field_is_refused(self):
		for filters in (
			{"rate": [">", 0]},
			[["Delivery Note Item", "rate", ">", 0]],
			["rate", ">", 0],
			'{"rate": 5}',
		):
			with self.subTest(filters=filters), self.assertRaises(PermissionDeniedError):
				self._check(filters=filters)

	def test_sorting_on_a_hidden_field_is_refused(self):
		with self.assertRaises(PermissionDeniedError):
			self._check(order_by="idx asc, `rate` desc")

	def test_readable_fields_and_a_value_that_spells_a_field_are_allowed(self):
		self._check(fields=["item_code", "rate"], filters={"item_code": "rate"}, order_by="idx desc")

	def test_a_user_who_can_read_the_field_is_allowed(self):
		self._check(levels=(0, 2), fields=[{"MAX": "rate"}], filters={"rate": [">", 0]}, order_by="rate")

	def test_no_parent_doctype_or_administrator_is_a_no_op(self):
		self._check(parent=None, fields=[{"MAX": "rate"}])
		frappe.set_user("Administrator")
		get_list_mod.assert_child_query_fields_readable(
			self.CHILD, self.PARENT, [{"MAX": "rate"}], None, None
		)


# --------------------------------------------------------------------------- #
# #6  load_doc / draft form meta: hidden fields are neither listed nor returned
# --------------------------------------------------------------------------- #
class TestLoadDocFieldLevel(FrappeTestCase):
	def test_can_read_field(self):
		self.assertTrue(actions_api._can_read_field(frappe._dict(permlevel=0), {0}))
		self.assertTrue(actions_api._can_read_field(frappe._dict(permlevel=2), {0, 2}))
		self.assertFalse(actions_api._can_read_field(frappe._dict(permlevel=2), {0}))
		self.assertTrue(actions_api._can_read_field(frappe._dict(permlevel=2), None))  # Administrator

	def test_form_meta_drops_fields_the_user_cannot_read(self):
		real = actions_api._can_read_field

		def deny_description(df, levels):
			return df.fieldname != "description" and real(df, levels)

		with patch.object(actions_api, "_can_read_field", side_effect=deny_description):
			fm = actions_api.get_doctype_form_meta("ToDo")
		self.assertNotIn("description", {f["fieldname"] for f in fm["fields"]})
		self.assertIn("status", {f["fieldname"] for f in fm["fields"]})

	def test_child_grid_and_extra_columns_respect_parent_permlevels(self):
		fields = [
			frappe._dict(fieldname=name, fieldtype="Data", permlevel=level, in_list_view=listed)
			for name, level, listed in (
				("visible_grid", 0, 1),
				("hidden_grid", 2, 1),
				("visible_extra", 0, 0),
				("hidden_extra", 2, 0),
			)
		]
		meta = MagicMock(fields=fields)
		with (
			patch.object(actions_api.frappe, "get_meta", return_value=meta),
			patch.object(actions_api.frappe, "session", frappe._dict(user="restricted@example.com")),
			patch.object(actions_api, "_field_dict", side_effect=lambda df, *args: df),
		):
			for levels in ([0], [0, 2]):
				with self.subTest(levels=levels):
					meta.get_permlevel_access.return_value = levels
					columns = actions_api._child_columns("Sales Order Item", "Sales Order", "items")
					extra = actions_api._extra_child_columns("Sales Order Item", "Sales Order", "items")
					self.assertEqual(
						[c.fieldname for c in columns],
						["visible_grid", "hidden_grid"] if 2 in levels else ["visible_grid"],
					)
					self.assertEqual(
						[c.fieldname for c in extra],
						["visible_extra", "hidden_extra"] if 2 in levels else ["visible_extra"],
					)
					meta.get_permlevel_access.assert_called_with(
						permission_type="read", parenttype="Sales Order"
					)

	def test_load_doc_strips_permlevel_fields_from_the_document(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "permlevel probe"}).insert(
			ignore_permissions=True
		)
		self.addCleanup(frappe.delete_doc, "ToDo", todo.name, force=True, ignore_permissions=True)
		with patch("frappe.model.document.Document.apply_fieldlevel_read_permissions") as strip:
			actions_api.load_doc("ToDo", todo.name)
		strip.assert_called_once()


# --------------------------------------------------------------------------- #
# #7  get_itemised_tax_breakup: needs read on the item amounts it reveals
# --------------------------------------------------------------------------- #
class TestTaxBreakupItemAmounts(_NonAdmin):
	def _doc_and_child_meta(self, levels):
		child_meta = MagicMock()
		child_meta.get_permlevel_access.return_value = levels
		child_meta.get_field.side_effect = lambda f: (
			frappe._dict(permlevel=2) if f in tax_mod._REVEALED_ITEM_FIELDS else None
		)
		doc = MagicMock(doctype="Sales Invoice")
		doc.meta.get_field.return_value = frappe._dict(options="Sales Invoice Item")
		return doc, child_meta

	def test_hidden_item_amounts_make_the_breakup_unreadable(self):
		frappe.set_user(_RESTRICTED)
		doc, child_meta = self._doc_and_child_meta([0])
		with patch.object(tax_mod.frappe, "get_meta", return_value=child_meta):
			self.assertFalse(tax_mod._item_amounts_readable(doc))

	def test_readable_item_amounts_allow_the_breakup(self):
		frappe.set_user(_RESTRICTED)
		doc, child_meta = self._doc_and_child_meta([0, 2])
		with patch.object(tax_mod.frappe, "get_meta", return_value=child_meta):
			self.assertTrue(tax_mod._item_amounts_readable(doc))

	def test_the_tool_refuses_when_item_amounts_are_hidden(self):
		name = frappe.db.get_value("Sales Invoice", {}, "name")
		if not name:
			self.skipTest("no Sales Invoice on this site")
		with (
			patch.object(tax_mod, "_item_amounts_readable", return_value=False),
			patch.object(tax_mod.compat, "itemised_tax") as compute,
			self.assertRaises(PermissionDeniedError),
		):
			tax_mod.get_itemised_tax_breakup("Sales Invoice", name)
		compute.assert_not_called()
