"""Invoice picker eligibility and child-schema authorization on a real test site."""

import json
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.desk.search import search_link
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.actions_api import get_doctype_form_meta
from jarvis.chat.api import get_doctype_fields
from jarvis.chat.chat_asks import parse_ask
from jarvis.chat.link_filters import draft_link_query_filters
from jarvis.permissions import ensure_jarvis_user_role
from jarvis.tools.get_creation_context import get_creation_context


class TestContextualItemPicker(FrappeTestCase):
	def setUp(self):
		super().setUp()
		assert frappe.local.site in ("test_jarvis", "jarvis-ci.test")
		frappe.set_user("Administrator")
		self.prefix = "_J2172_" + uuid4().hex[:10]
		ensure_jarvis_user_role()
		self.user = self.prefix + "@example.com"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": self.user,
				"first_name": self.prefix,
				"send_welcome_email": 0,
				"user_type": "System User",
				"roles": [{"role": "Accounts User"}, {"role": "Jarvis User"}],
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.clear_cache(user=self.user)
		super().tearDown()

	def field(self):
		result = get_doctype_fields("Sales Invoice Item", parent_doctype="Sales Invoice", parentfield="items")
		self.assertTrue(result["ok"])
		return next(f for f in result["fields"] if f["fieldname"] == "item_code")

	def test_child_schema_uses_the_readable_parent_and_exposes_eligibility(self):
		frappe.set_user(self.user)
		self.assertTrue(frappe.has_permission("Sales Invoice", "read"))
		field = self.field()
		self.assertEqual(field["options"], "Item")
		self.assertIn(["Item", "is_sales_item", "=", 1], field["link_query_filters"])
		self.assertIn(["Item", "disabled", "=", 0], field["link_query_filters"])

	def test_draft_grid_receives_the_same_item_eligibility(self):
		result = get_doctype_form_meta("Sales Invoice")
		column = next(f for f in result["tables"]["items"]["columns"] if f["fieldname"] == "item_code")
		self.assertEqual(column["link_query_filters"], self.field()["link_query_filters"])

	def test_child_schema_still_refuses_parentless_non_admin_reads(self):
		frappe.set_user(self.user)
		with self.assertRaises(frappe.PermissionError):
			get_doctype_fields("Sales Invoice Item")

	def test_parentless_administrator_cannot_get_an_unscoped_child_picker(self):
		self.assertFalse(get_doctype_fields("Sales Invoice Item")["ok"])

	def test_child_field_permlevels_and_configured_filters_are_respected(self):
		real_meta = frappe.get_meta
		meta = deepcopy(real_meta("Sales Invoice Item"))
		meta.get_field("item_code").link_filters = '[["Item","item_group","=","Products"]]'
		with patch(
			"frappe.get_meta",
			side_effect=lambda dt, *a, **kw: meta if dt == meta.name else real_meta(dt, *a, **kw),
		):
			self.assertEqual(self.field()["link_filters"], meta.get_field("item_code").link_filters)
			meta.get_field("item_code").permlevel = 9
			frappe.set_user(self.user)
			result = get_doctype_fields(
				"Sales Invoice Item", parent_doctype="Sales Invoice", parentfield="items"
			)
			self.assertNotIn("item_code", [f["fieldname"] for f in result["fields"]])

	def test_parent_table_permlevel_is_not_bypassed(self):
		real_meta = frappe.get_meta
		meta = deepcopy(real_meta("Sales Invoice"))
		meta.get_field("items").permlevel = 9
		frappe.set_user(self.user)
		with patch(
			"frappe.get_meta",
			side_effect=lambda dt, *a, **kw: meta if dt == meta.name else real_meta(dt, *a, **kw),
		):
			with self.assertRaises(frappe.PermissionError):
				self.field()

	def test_invalid_parent_relationship_is_refused_even_for_administrator(self):
		for parent, table in (
			("Customer", "items"),
			("Sales Invoice", "taxes"),
			("Sales Invoice Item", "items"),
		):
			with self.subTest(parent=parent, table=table):
				result = get_doctype_fields("Sales Invoice Item", parent_doctype=parent, parentfield=table)
				self.assertFalse(result["ok"])
				self.assertEqual(result["fields"], [])

	def test_valid_parent_without_read_permission_is_refused(self):
		frappe.get_doc("User", self.user).remove_roles("Accounts User")
		frappe.set_user(self.user)
		self.assertFalse(frappe.has_permission("Sales Invoice", "read"))
		with self.assertRaises(frappe.PermissionError):
			self.field()

	def test_missing_parentfield_is_inferred_only_from_the_real_relationship(self):
		result = get_doctype_fields("Sales Invoice Item", parent_doctype="Sales Invoice")
		self.assertTrue(result["ok"])
		self.assertIn(
			["Item", "is_sales_item", "=", 1],
			next(f for f in result["fields"] if f["fieldname"] == "item_code")["link_query_filters"],
		)

	def test_rule_does_not_attach_to_custom_contexts(self):
		df = frappe.get_meta("Sales Invoice Item").get_field("item_code")
		for child, parent, table in (
			("Sales Invoice Item", "Custom Invoice", "items"),
			("Sales Invoice Item", "Sales Invoice", "custom_items"),
		):
			self.assertEqual(draft_link_query_filters(child, df, parent, table), [])

	def test_creation_tool_tells_the_model_how_to_request_the_contextual_field(self):
		result = get_creation_context("Sales Invoice", limit=1)
		self.assertEqual(result["child_doctypes"]["items"], "Sales Invoice Item")
		self.assertEqual(set(result["child_doctypes"]), set(result["child_fields"]))
		self.assertIn('"parentfield"', result["note"])
		self.assertIn('"parent"', result["note"])

	def test_server_ask_parser_keeps_the_same_validated_context(self):
		field = {
			"doctype": "Sales Invoice Item",
			"fieldname": "item_code",
			"parent": "Sales Invoice",
			"parentfield": "items",
		}
		for question in ({"q": "Item", "type": "field", **field}, {"q": "Item", "field": field}):
			parsed = parse_ask("```jarvis-ask\n" + json.dumps([question]) + "\n```")[0]
			for key, value in field.items():
				self.assertEqual(parsed[key], value)
		for bad in ({"parent": "Sales<script>"}, {"parentfield": "items.code"}):
			parsed = parse_ask(
				"```jarvis-ask\n" + json.dumps([{"q": "Item", "type": "field", **field, **bad}]) + "\n```"
			)[0]
			self.assertEqual(parsed["type"], "text")

	def test_real_search_excludes_ineligible_items_but_generic_search_keeps_non_sales(self):
		items = {}
		for label, fields in {
			"sale": {"is_sales_item": 1},
			"purchase": {"is_sales_item": 0},
			"disabled": {"is_sales_item": 1, "disabled": 1},
			"template": {"is_sales_item": 1, "has_variants": 1},
		}.items():
			name = self.prefix + label
			frappe.get_doc(
				{
					"doctype": "Item",
					"name": name,
					"item_code": name,
					"item_name": name,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					**fields,
				}
			).db_insert()
			items[label] = name
		frappe.set_user(self.user)
		filters = self.field()["link_query_filters"]
		results = search_link(
			"Item",
			self.prefix,
			filters=filters,
			reference_doctype="Sales Invoice",
			link_fieldname="item_code",
			page_length=20,
		)
		self.assertEqual({r["value"] for r in results}, {items["sale"]})
		generic = search_link("Item", self.prefix, page_length=20)
		self.assertIn(items["purchase"], {r["value"] for r in generic})
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": self.user,
				"allow": "Item",
				"for_value": items["purchase"],
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=self.user)
		frappe.set_user(self.user)
		results = search_link(
			"Item",
			self.prefix,
			filters=filters,
			reference_doctype="Sales Invoice",
			link_fieldname="item_code",
			page_length=20,
		)
		self.assertEqual(results, [])

	def purchase_field(self, parent):
		result = get_doctype_fields(parent + " Item", parent_doctype=parent, parentfield="items")
		self.assertTrue(result["ok"])
		return next(f for f in result["fields"] if f["fieldname"] == "item_code")

	def test_purchase_child_metadata_requires_allow_purchase_without_sales_filter(self):
		frappe.get_doc("User", self.user).add_roles("Purchase User", "Stock User")
		frappe.set_user(self.user)
		for parent in ("Purchase Invoice", "Purchase Order", "Purchase Receipt", "Supplier Quotation"):
			with self.subTest(parent=parent):
				self.assertEqual(
					self.purchase_field(parent)["link_query_filters"],
					[
						["Item", "is_purchase_item", "=", 1],
						["Item", "disabled", "=", 0],
						["Item", "has_variants", "=", 0],
					],
				)

	def test_purchase_draft_grid_has_the_same_eligibility_as_the_question(self):
		for parent in ("Purchase Invoice", "Purchase Order", "Purchase Receipt", "Supplier Quotation"):
			with self.subTest(parent=parent):
				meta = get_doctype_form_meta(parent)
				field = next(f for f in meta["tables"]["items"]["columns"] if f["fieldname"] == "item_code")
				self.assertEqual(
					field["link_query_filters"], self.purchase_field(parent)["link_query_filters"]
				)
				self.assertIn(["Item", "is_purchase_item", "=", 1], field["link_query_filters"])

	def test_purchase_rule_is_limited_to_the_canonical_item_field(self):
		df = frappe.get_meta("Purchase Invoice Item").get_field("item_code")
		for child, parent, table in (
			("Purchase Invoice Item", "Sales Invoice", "items"),
			("Purchase Invoice Item", "Purchase Invoice", "custom_items"),
			("Custom Purchase Item", "Purchase Invoice", "items"),
			("Material Request Item", "Material Request", "items"),
			("Request for Quotation Item", "Request for Quotation", "items"),
		):
			with self.subTest(child=child, parent=parent, table=table):
				self.assertEqual(draft_link_query_filters(child, df, parent, table), [])
		for attr, value in (("fieldname", "custom_item"), ("options", "Asset"), ("fieldtype", "Data")):
			other = deepcopy(df)
			setattr(other, attr, value)
			self.assertEqual(
				draft_link_query_filters("Purchase Invoice Item", other, "Purchase Invoice", "items"), []
			)

	def test_purchase_creation_context_identifies_the_item_child(self):
		for parent in ("Purchase Invoice", "Purchase Order", "Purchase Receipt", "Supplier Quotation"):
			with self.subTest(parent=parent):
				result = get_creation_context(parent, limit=1)
				self.assertEqual(result["child_doctypes"]["items"], parent + " Item")
				self.assertIn('"parentfield"', result["note"])

	def test_real_purchase_search_excludes_sales_only_and_preserves_item_permissions(self):
		items = {}
		for label, flags in {
			"purchase_only": {"is_purchase_item": 1, "is_sales_item": 0},
			"both": {"is_purchase_item": 1, "is_sales_item": 1},
			"sales_only": {"is_purchase_item": 0, "is_sales_item": 1},
			"legacy_subcontract": {"is_purchase_item": 0, "is_sub_contracted_item": 1},
			"disabled": {"is_purchase_item": 1, "disabled": 1},
			"template": {"is_purchase_item": 1, "has_variants": 1},
		}.items():
			name = self.prefix + label
			frappe.get_doc(
				{
					"doctype": "Item",
					"name": name,
					"item_code": name,
					"item_name": name,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					**flags,
				}
			).db_insert()
			items[label] = name
		frappe.get_doc("User", self.user).add_roles("Purchase User", "Stock User")
		frappe.set_user(self.user)
		parents = ("Purchase Invoice", "Purchase Order", "Purchase Receipt", "Supplier Quotation")
		for parent in parents:
			with self.subTest(parent=parent):
				results = search_link(
					"Item",
					self.prefix,
					filters=self.purchase_field(parent)["link_query_filters"],
					reference_doctype=parent,
					link_fieldname="item_code",
					page_length=20,
				)
				self.assertEqual({r["value"] for r in results}, {items["purchase_only"], items["both"]})
		self.assertIn(
			items["sales_only"], {r["value"] for r in search_link("Item", self.prefix, page_length=20)}
		)
		# Legacy old-flow subcontracted-only Items require ERPNext Desk.
		self.assertIn(
			items["legacy_subcontract"],
			{r["value"] for r in search_link("Item", self.prefix, page_length=20)},
		)
		frappe.set_user("Administrator")
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": self.user,
				"allow": "Item",
				"for_value": items["sales_only"],
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(user=self.user)
		frappe.set_user(self.user)
		for parent in parents:
			with self.subTest(restricted_parent=parent):
				self.assertEqual(
					search_link(
						"Item",
						self.prefix,
						filters=self.purchase_field(parent)["link_query_filters"],
						reference_doctype=parent,
						link_fieldname="item_code",
						page_length=20,
					),
					[],
				)
