from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.get_schema import _field_record, get_schema


class TestGetSchema(FrappeTestCase):
	def test_returns_fields_for_known_doctype(self):
		result = get_schema(doctype="Customer")
		self.assertEqual(result["doctype"], "Customer")
		self.assertIn("fields", result)
		fieldnames = {f["fieldname"] for f in result["fields"]}
		self.assertIn("customer_name", fieldnames)

	def test_identity_keys_always_present_on_every_field(self):
		"""fieldname + fieldtype are unconditional; a record without them is
		anonymous. Everything else - label, options, reqd included - appears only
		when configured, because falsy IS the Frappe default and absence says so
		for free. This used to assert an exact 5-key set."""
		result = get_schema(doctype="Customer")
		for f in result["fields"]:
			self.assertIn("fieldname", f)
			self.assertIn("fieldtype", f)

	def test_default_includes_options_on_link_field(self):
		"""A Link field's options carries its target DocType. Without
		this the agent can't follow the link."""
		result = get_schema(doctype="Sales Invoice")
		customer_field = next(f for f in result["fields"] if f["fieldname"] == "customer")
		self.assertEqual(customer_field["fieldtype"], "Link")
		self.assertEqual(customer_field["options"], "Customer")

	def test_default_includes_options_on_table_field_but_no_child_fields(self):
		"""A Table field's options names the child DocType. The agent
		uses that to call get_schema on the child when it needs the
		child fields - the recursive child_fields expansion is what we
		cut to keep transcripts small."""
		result = get_schema(doctype="Sales Invoice")
		items_field = next(f for f in result["fields"] if f["fieldname"] == "items")
		self.assertEqual(items_field["fieldtype"], "Table")
		self.assertEqual(items_field["options"], "Sales Invoice Item")
		self.assertNotIn("child_fields", items_field)

	def test_default_includes_reqd_on_mandatory_fields_only(self):
		"""reqd is essential for the agent to know what create_doc must set - but
		it rides the omit-when-falsy rule like everything else, so it marks the
		mandatory fields and is simply absent on the rest."""
		result = get_schema(doctype="Customer")
		# At least one field on every standard DocType is mandatory.
		self.assertTrue(
			any(f.get("reqd") for f in result["fields"]),
			"expected at least one reqd field",
		)
		# ...and a field that is not mandatory says so by omission, not reqd=False.
		optional = [f for f in result["fields"] if not f.get("reqd")]
		self.assertTrue(optional, "expected some optional fields")
		self.assertNotIn("reqd", optional[0])

	def test_verbose_expands_child_table_schemas(self):
		"""verbose=True is the opt-in for inlined child schemas. Child records
		use the same shape as parents: the core keys always, plus whatever the
		child DocType configures."""
		result = get_schema(doctype="Sales Invoice", verbose=True)
		items_field = next(f for f in result["fields"] if f["fieldname"] == "items")
		self.assertEqual(items_field["fieldtype"], "Table")
		self.assertEqual(items_field["options"], "Sales Invoice Item")
		self.assertIn("child_fields", items_field)
		child_fieldnames = {cf["fieldname"] for cf in items_field["child_fields"]}
		self.assertIn("item_code", child_fieldnames)
		self.assertIn("qty", child_fieldnames)
		for cf in items_field["child_fields"]:
			self.assertIn("fieldname", cf)
			self.assertIn("fieldtype", cf)

	def test_verbose_non_table_fields_have_no_child_fields_key(self):
		result = get_schema(doctype="Customer", verbose=True)
		customer_name = next(f for f in result["fields"] if f["fieldname"] == "customer_name")
		self.assertNotIn("child_fields", customer_name)

	def test_rejects_unknown_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			get_schema(doctype="Definitely Not A DocType")

	def test_rejects_missing_argument(self):
		with self.assertRaises(InvalidArgumentError):
			get_schema(doctype="")

	def test_permission_check_blocks_unauthorized_user(self):
		# Create a user with no roles and switch to them.
		user_email = "schemaless@example.com"
		if not frappe.db.exists("User", user_email):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": user_email,
					"first_name": "Schemaless",
					"send_welcome_email": 0,
				}
			)
			user.insert(ignore_permissions=True)
		frappe.set_user(user_email)
		try:
			with self.assertRaises(PermissionDeniedError):
				get_schema(doctype="Customer")
		finally:
			frappe.set_user("Administrator")


class _StubField:
	"""Stands in for a meta DocField: _field_record only needs the five core
	attributes plus as_dict(). A stub (not a real doctype) keeps these assertions
	from drifting when ERPNext reshuffles its own field properties."""

	def __init__(self, **props):
		self._props = props
		for k, v in props.items():
			setattr(self, k, v)

	def as_dict(self):
		return dict(self._props)


class TestFieldRecordConfiguredProps(FrappeTestCase):
	"""Per-field records carry the configured docfield properties, and only
	those: absent == not configured (the Frappe default, always falsy)."""

	def _rec(self, **props):
		base = {"fieldname": "f1", "fieldtype": "Data", "label": "F1", "options": "", "reqd": 0}
		base.update(props)
		return _field_record(_StubField(**base))

	def test_configured_props_surface(self):
		rec = self._rec(
			read_only=1,
			fetch_from="customer.customer_name",
			allow_on_submit=1,
			mandatory_depends_on="eval:doc.docstatus==1",
			default="Draft",
			link_filters='[["Item","has_variants","=",0]]',
		)
		self.assertEqual(rec["read_only"], 1)
		self.assertEqual(rec["fetch_from"], "customer.customer_name")
		self.assertEqual(rec["allow_on_submit"], 1)
		self.assertEqual(rec["mandatory_depends_on"], "eval:doc.docstatus==1")
		self.assertEqual(rec["default"], "Draft")
		self.assertEqual(rec["link_filters"], '[["Item","has_variants","=",0]]')

	def test_unconfigured_props_are_omitted_not_falsy(self):
		"""The whole point of the omit rule: an ordinary field costs almost
		nothing, which is what pays for the properties added above."""
		rec = self._rec(read_only=0, fetch_from="", allow_on_submit=None, default="")
		self.assertEqual(set(rec.keys()), {"fieldname", "fieldtype", "label"})

	def test_falsy_label_options_reqd_are_omitted_too(self):
		"""These three used to be emitted unconditionally. Emitting `options: ""`
		on every primitive field said "no options" in 15 bytes where absence says
		it in zero - across ~1000 fields that padding cost more than every
		property this change adds."""
		rec = self._rec(options="", reqd=0, label="")
		self.assertEqual(set(rec.keys()), {"fieldname", "fieldtype"})

	def test_configured_label_options_reqd_survive(self):
		rec = self._rec(label="Customer", options="Customer", reqd=1, fieldtype="Link")
		self.assertEqual(rec["label"], "Customer")
		self.assertEqual(rec["options"], "Customer")
		self.assertEqual(rec["reqd"], 1)

	def test_presentation_only_props_never_surface(self):
		"""These can't affect a write; measured at ~17% of an all-props payload."""
		rec = self._rec(
			print_hide=1,
			width="200px",
			collapsible=1,
			bold=1,
			in_global_search=1,
			translatable=1,
			report_hide=1,
			columns=2,
			search_index=1,
			show_dashboard=1,
		)
		for k in (
			"print_hide",
			"width",
			"collapsible",
			"bold",
			"in_global_search",
			"translatable",
			"report_hide",
			"columns",
			"search_index",
			"show_dashboard",
		):
			self.assertNotIn(k, rec)

	def test_docfield_row_metadata_never_surfaces(self):
		"""as_dict() carries the DocField ROW's own fields; they describe the
		docfield record, not the field it defines - and creation/modified are
		datetimes that would not survive the JSON hop."""
		import datetime

		rec = self._rec(
			name="abc123",
			parent="Sales Order",
			idx=7,
			docstatus=0,
			owner="Administrator",
			creation=datetime.datetime.now(),
			parentfield="fields",
			parenttype="DocType",
		)
		for k in ("name", "parent", "idx", "owner", "creation", "parentfield", "parenttype", "docstatus"):
			self.assertNotIn(k, rec)

	def test_non_primitive_values_are_dropped(self):
		"""Defensive: everything emitted must survive the JSON hop to the plugin
		and the pickled cache entry."""
		rec = self._rec(some_future_column={"a": 1}, another=[1, 2])
		self.assertNotIn("some_future_column", rec)
		self.assertNotIn("another", rec)


class TestConfiguredPropsOnRealDoctype(FrappeTestCase):
	def test_real_schema_omits_presentation_props_and_row_metadata(self):
		result = get_schema(doctype="Customer", refresh=True)
		banned = {
			"print_hide",
			"width",
			"collapsible",
			"bold",
			"in_global_search",
			"search_index",
			"translatable",
			"report_hide",
			"print_width",
			"name",
			"parent",
			"idx",
			"owner",
			"creation",
			"modified",
			"parentfield",
			"parenttype",
			"docstatus",
		}
		for f in result["fields"]:
			leaked = banned & set(f.keys())
			self.assertFalse(leaked, f"{f['fieldname']} leaked {leaked}")

	def test_real_schema_surfaces_a_read_only_field(self):
		"""Sanity that the mechanism fires against real meta, not just stubs.
		Customer always has at least one read-only field."""
		result = get_schema(doctype="Customer", refresh=True)
		self.assertTrue(
			any(f.get("read_only") for f in result["fields"]),
			"expected at least one read_only field on Customer",
		)


class TestGetSchemaCustomFlags(FrappeTestCase):
	"""The v2 flags: per-field `is_custom` (Custom Field provenance, emitted
	only when true) and doctype-level `custom` (custom=1 OR module of a
	non-core app)."""

	CF_FIELD = "custdisc_probe"
	CUSTOM_DT = "CustDisc Probe DT"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		from frappe.custom.doctype.custom_field.custom_field import create_custom_field

		if not frappe.db.exists("Custom Field", {"dt": "ToDo", "fieldname": cls.CF_FIELD}):
			create_custom_field(
				"ToDo",
				{"fieldname": cls.CF_FIELD, "label": "CustDisc Probe", "fieldtype": "Data"},
			)
		if not frappe.db.exists("DocType", cls.CUSTOM_DT):
			frappe.get_doc(
				{
					"doctype": "DocType",
					"name": cls.CUSTOM_DT,
					"module": "Custom",
					"custom": 1,
					"fields": [{"fieldname": "title", "fieldtype": "Data", "label": "Title"}],
					"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
				}
			).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cf = frappe.db.get_value("Custom Field", {"dt": "ToDo", "fieldname": cls.CF_FIELD})
		if cf:
			frappe.delete_doc("Custom Field", cf, force=True, ignore_permissions=True)
		if frappe.db.exists("DocType", cls.CUSTOM_DT):
			frappe.delete_doc("DocType", cls.CUSTOM_DT, force=True, ignore_permissions=True)
		frappe.clear_cache(doctype="ToDo")
		super().tearDownClass()

	def test_custom_field_carries_is_custom(self):
		result = get_schema(doctype="ToDo", refresh=True)
		probe = next(f for f in result["fields"] if f["fieldname"] == self.CF_FIELD)
		self.assertIs(probe.get("is_custom"), True)

	def test_standard_field_omits_is_custom(self):
		"""Absent means standard - never is_custom: False."""
		result = get_schema(doctype="ToDo", refresh=True)
		standard = next(f for f in result["fields"] if f["fieldname"] == "description")
		self.assertNotIn("is_custom", standard)

	def test_custom1_doctype_flagged(self):
		result = get_schema(doctype=self.CUSTOM_DT, refresh=True)
		self.assertTrue(result["custom"])

	def test_core_doctype_not_custom(self):
		result = get_schema(doctype="Sales Invoice", refresh=True)
		self.assertFalse(result["custom"])

	def test_custom_app_module_marks_shipped_doctype_custom(self):
		"""An app-shipped doctype has custom=0; classification must catch it
		via its module -> custom-app mapping (the two-union rule)."""
		with patch("jarvis.site_profile.apps.is_custom_doctype_module", return_value=True):
			result = get_schema(doctype="ToDo", refresh=True)
		self.assertTrue(result["custom"])

	def test_classification_failure_reads_standard(self):
		"""Invariant 5: a broken site_profile must never break get_schema."""
		with patch(
			"jarvis.site_profile.apps.is_custom_doctype_module",
			side_effect=RuntimeError("boom"),
		):
			result = get_schema(doctype="ToDo", refresh=True)
		self.assertFalse(result["custom"])
