"""Wave F8 foundation repairs to the shared list-filter contract.

Round-two review (plan 08) blocking + high-priority findings, each proven here:

* **P0-02** — a Table container the caller cannot read (its own permlevel, or a
  view-level exclusion) must hide EVERY child field, at schema and compiler, for
  both an ordinary and a privileged role.
* **P0-03** — Frappe's ``original → effective Data`` operator fallback: a
  string-like control with no invalid_condition_map row inherits Data's, so it
  never advertises ``Between``/``Timespan``. Golden per-type, schema + compiler.
* **P1-01** — malformed / non-finite numeric values fail with the stable code
  instead of silently becoming ``0`` or reaching the DB binder.
* **P1-10** — ``schema_revision`` hashes the FULL response contract.
* **P1-05** — the runtime per-view capability flag (default ON) and the bounded,
  value-free structural telemetry.

Runs on patterntest.localhost. Reuses ``test_list_filters``'s real-user
fixtures.
"""

from __future__ import annotations

import contextlib
import logging
import unittest

import frappe

from jarvis.chat import list_filters
from jarvis.chat.list_filters import (
	ERR_INVALID_OPERATOR,
	ERR_INVALID_VALUE,
	ERR_VIEW_NOT_FILTERABLE,
	ListFilterError,
	allowed_operators,
	build_field_catalog,
	compile_list_filters,
	emit_filter_telemetry,
	get_schema,
	invalid_conditions,
	list_filters_capabilities,
	view_filters_enabled,
)
from jarvis.tests.test_list_filters import (
	MACRO,
	PERMLEVEL_ROLE,
	USER_A,
	USER_B,
	USER_SM,
	_as,
	_ensure_role,
	_ensure_user,
	_entry,
)

STEP = "Jarvis Macro Step"


def setUpModule() -> None:
	frappe.set_user("Administrator")
	_ensure_role(PERMLEVEL_ROLE)
	_ensure_user(USER_SM, ("Jarvis User", "System Manager"))
	_ensure_user(USER_A, ("Jarvis User", PERMLEVEL_ROLE))
	_ensure_user(USER_B, ("Jarvis User",))


def _child_pairs(catalog) -> set[tuple[str, str]]:
	return {(f["doctype"], f["fieldname"]) for f in catalog if f.get("is_child")}


# --------------------------------------------------------------------------- #
# P0-02 — the Table-container permission boundary
# --------------------------------------------------------------------------- #
class TestContainerPermlevel(unittest.TestCase):
	"""A readable child field under an UNreadable Table field is an oracle over
	data the container hides. The container's own permlevel gates discovery."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		from frappe.permissions import add_permission, update_permission_property

		cls._perms_before = set(frappe.get_all("Custom DocPerm", filters={"parent": MACRO}, pluck="name"))
		# Raise the `steps` TABLE CONTAINER (not a child field) to permlevel 1.
		frappe.make_property_setter(
			{
				"doctype": MACRO,
				"fieldname": "steps",
				"property": "permlevel",
				"value": 1,
				"property_type": "Int",
			},
			is_system_generated=False,
			validate_fields_for_doctype=False,
		)
		# Grant permlevel-1 read on the parent to the role only USER_A holds.
		add_permission(MACRO, PERMLEVEL_ROLE, 1)
		update_permission_property(MACRO, PERMLEVEL_ROLE, 1, "read", 1)
		frappe.db.commit()
		frappe.clear_cache()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			"Property Setter",
			filters={"doc_type": MACRO, "property": "permlevel", "field_name": "steps"},
			pluck="name",
		):
			frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)
		for name in frappe.get_all("Custom DocPerm", filters={"parent": MACRO}, pluck="name"):
			if name not in cls._perms_before:
				frappe.delete_doc("Custom DocPerm", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()

	def test_unreadable_container_hides_every_child_from_the_schema(self):
		privileged = get_schema("macros", user=USER_A)
		ordinary = get_schema("macros", user=USER_B)
		# Non-vacuity: the privileged role (reads permlevel 1 → reads the container)
		# still sees the child fields.
		self.assertIsNotNone(
			_entry(privileged, STEP, "label"),
			"the container permlevel-1 grant did not take effect — test would be vacuous",
		)
		# The ordinary role cannot read the `steps` container, so NONE of its child
		# fields are catalogued.
		self.assertEqual(
			_child_pairs(ordinary["fields"]),
			set(),
			"an unreadable Table container leaked its children",
		)
		# Non-vacuity: the ordinary schema is otherwise full (main fields present).
		self.assertIsNotNone(_entry(ordinary, MACRO, "macro_name"))

	def test_compiler_rejects_a_child_of_an_unreadable_container(self):
		clause = [{"doctype": STEP, "fieldname": "label", "operator": "=", "value": "x"}]
		with self.assertRaises(ListFilterError) as caught:
			compile_list_filters("macros", clause, user=USER_B)
		self.assertEqual(caught.exception.filter_error_code, list_filters.ERR_UNKNOWN_FIELD)
		# …and the privileged role still can.
		self.assertIn("`label`", compile_list_filters("macros", clause, user=USER_A).fragment())

	def test_view_excluded_container_suppresses_its_whole_subtree(self):
		"""The `excluded_fields` check used to sit AFTER the Table `continue`, so an
		endpoint-withheld container still leaked its children (P0-02, second half)."""
		catalog = build_field_catalog(MACRO, user=USER_SM, excluded_fields=("steps",))
		self.assertEqual(
			_child_pairs(catalog),
			set(),
			"an excluded Table container leaked its children",
		)
		# The withholding is scoped: siblings survive.
		self.assertTrue(any(f["fieldname"] == "macro_name" for f in catalog))


class TestTwoTableFieldsSameChild(unittest.TestCase):
	"""Documented explicit behaviour (LIST-FILTERS-DEVIATIONS D16): when a parent
	reaches ONE child DocType through TWO Table fields, the catalog lists each
	child field ONCE (deduped by (child DocType, fieldname)), and a clause on that
	child compiles to ONE ``EXISTS`` keyed by the child DocType — so it matches a
	row in EITHER container. That is the known ``parentfield`` ambiguity, made
	explicit rather than left to chance."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if not frappe.db.exists("Custom Field", {"dt": MACRO, "fieldname": "steps_alt"}):
			frappe.get_doc(
				{
					"doctype": "Custom Field",
					"dt": MACRO,
					"fieldname": "steps_alt",
					"label": "Steps Alt",
					"fieldtype": "Table",
					"options": STEP,
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
			frappe.clear_cache()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		name = frappe.db.get_value("Custom Field", {"dt": MACRO, "fieldname": "steps_alt"})
		if name:
			frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)
			frappe.db.commit()
			frappe.clear_cache()

	def test_child_fields_are_deduplicated_across_two_containers(self):
		catalog = build_field_catalog(MACRO, user=USER_SM)
		labels = [f for f in catalog if f["doctype"] == STEP and f["fieldname"] == "label"]
		self.assertEqual(len(labels), 1, "a child field reached via two Table fields was duplicated")

	def test_same_child_clause_compiles_to_one_exists(self):
		clause = [{"doctype": STEP, "fieldname": "label", "operator": "=", "value": "x"}]
		fragment = compile_list_filters("macros", clause, user=USER_SM).fragment()
		self.assertEqual(fragment.count("EXISTS"), 1, "same-child clause must be one EXISTS")


# --------------------------------------------------------------------------- #
# P0-03 — original → effective Data operator fallback
# --------------------------------------------------------------------------- #
class TestOperatorMatrixEffectiveData(unittest.TestCase):
	#: Every string-like control Frappe scrubs to effective Data (filter.js) that
	#: has NO invalid_condition_map row of its own, plus Long Text (Jarvis D15).
	CONVERTED = (
		"Text",
		"Small Text",
		"Text Editor",
		"Attach",
		"Attach Image",
		"Tag",
		"Phone",
		"JSON",
		"Comments",
		"Barcode",
		"Dynamic Link",
		"Read Only",
		"Assign",
		"Long Text",
	)

	def test_converted_types_inherit_datas_invalid_conditions(self):
		for ft in self.CONVERTED:
			invalid = invalid_conditions(ft)
			self.assertIn("Between", invalid, f"{ft} must inherit Data's invalid Between")
			self.assertIn("Timespan", invalid, f"{ft} must inherit Data's invalid Timespan")
			ops = allowed_operators("body", ft)
			self.assertNotIn("Between", ops, f"{ft} must not advertise Between")
			self.assertNotIn("Timespan", ops, f"{ft} must not advertise Timespan")
			# non-vacuity: it is still a usable text control
			self.assertIn("like", ops)
			self.assertIn("=", ops)

	def test_types_with_their_own_row_keep_it(self):
		# original_type wins: these are converted to Data by set_fieldtype but keep
		# their own map row in hide_invalid_conditions.
		self.assertNotIn(">", allowed_operators("x", "Code"))
		self.assertNotIn("Between", allowed_operators("x", "Code"))
		self.assertNotIn("Between", allowed_operators("x", "Color"))
		# Data itself is unchanged.
		self.assertNotIn("Between", allowed_operators("x", "Data"))

	def test_comments_standard_text_field_excludes_between(self):
		"""``_comments`` is a standard Text field, so it must inherit the Data set."""
		self.assertNotIn("Between", allowed_operators("_comments", "Text"))
		self.assertNotIn("Timespan", allowed_operators("_comments", "Text"))

	def test_schema_never_offers_between_on_a_small_text_field(self):
		schema = get_schema("macros", user=USER_SM)
		desc = _entry(schema, MACRO, "description")  # Small Text
		self.assertIsNotNone(desc)
		self.assertNotIn("Between", desc["operators"])
		self.assertNotIn("Timespan", desc["operators"])

	def test_compiler_rejects_between_on_a_small_text_field(self):
		clause = [
			{
				"doctype": MACRO,
				"fieldname": "description",
				"operator": "Between",
				"value": ["2026-01-01", "2026-12-31"],
			}
		]
		with self.assertRaises(ListFilterError) as caught:
			compile_list_filters("macros", clause, user=USER_SM)
		self.assertEqual(caught.exception.filter_error_code, ERR_INVALID_OPERATOR)


# --------------------------------------------------------------------------- #
# P1-01 — strict numeric parsing
# --------------------------------------------------------------------------- #
class TestNumericStrictParse(unittest.TestCase):
	def _reject(self, value) -> str:
		clause = [{"doctype": MACRO, "fieldname": "idx", "operator": "=", "value": value}]
		with self.assertRaises(ListFilterError) as caught:
			compile_list_filters("macros", clause, user=USER_SM)
		return caught.exception.filter_error_code

	def test_nonnumeric_string_is_rejected_not_zeroed(self):
		self.assertEqual(self._reject("abc"), ERR_INVALID_VALUE)

	def test_blank_numeric_stays_zero_at_parity(self):
		# D14: a blank numeric compiles to `= 0` (Frappe parity), no error.
		clause = [{"doctype": MACRO, "fieldname": "idx", "operator": "=", "value": ""}]
		compiled = compile_list_filters("macros", clause, user=USER_SM)
		self.assertIn(0, compiled.params.values())

	def test_int_truncates_a_fractional_value(self):
		clause = [{"doctype": MACRO, "fieldname": "idx", "operator": "=", "value": "3.7"}]
		compiled = compile_list_filters("macros", clause, user=USER_SM)
		self.assertIn(3, compiled.params.values())

	def test_float_family_rejects_non_finite(self):
		# No Float field on a migrated view, so exercise the parser directly.
		entry = {"fieldtype": "Float", "label": "Amount"}
		for bad in ("inf", "-inf", "nan", "Infinity"):
			with self.assertRaises(ListFilterError):
				list_filters._as_number(entry, bad)

	def test_float_family_accepts_a_real_number(self):
		entry = {"fieldtype": "Float", "label": "Amount"}
		self.assertEqual(list_filters._as_number(entry, "3.5"), 3.5)


# --------------------------------------------------------------------------- #
# P1-10 — schema_revision hashes the full contract
# --------------------------------------------------------------------------- #
class TestSchemaRevisionContract(unittest.TestCase):
	def _field(self, **over) -> dict:
		base = {
			"doctype": MACRO,
			"fieldname": "status",
			"label": "Status",
			"fieldtype": "Select",
			"options": "Open\nClosed",
			"default_operator": "=",
			"operators": ["=", "!="],
			"is_standard": False,
			"is_child": False,
			"json_array": False,
		}
		base.update(over)
		return base

	def test_identical_contract_same_revision(self):
		self.assertEqual(
			list_filters._schema_digest([self._field()], {"max_clauses": 20}),
			list_filters._schema_digest([self._field()], {"max_clauses": 20}),
		)

	def test_label_change_changes_revision(self):
		a = list_filters._schema_digest([self._field(label="Status")])
		b = list_filters._schema_digest([self._field(label="State")])
		self.assertNotEqual(a, b, "a relabel must change schema_revision (P1-10)")

	def test_select_options_change_changes_revision(self):
		a = list_filters._schema_digest([self._field(options="Open\nClosed")])
		b = list_filters._schema_digest([self._field(options="Open\nClosed\nHeld")])
		self.assertNotEqual(a, b, "a Select-option change must change schema_revision")

	def test_link_target_change_changes_revision(self):
		a = list_filters._schema_digest([self._field(fieldtype="Link", options="User")])
		b = list_filters._schema_digest([self._field(fieldtype="Link", options="Role")])
		self.assertNotEqual(a, b, "a Link-target change must change schema_revision")

	def test_limits_change_changes_revision(self):
		a = list_filters._schema_digest([self._field()], {"max_clauses": 20})
		b = list_filters._schema_digest([self._field()], {"max_clauses": 10})
		self.assertNotEqual(a, b)


# --------------------------------------------------------------------------- #
# P1-05 — runtime capability flag + structural telemetry
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _conf(key, value):
	orig = frappe.conf.get(key)
	frappe.conf[key] = value
	try:
		yield
	finally:
		if orig is None:
			frappe.conf.pop(key, None)
		else:
			frappe.conf[key] = orig


class TestCapabilityFlag(unittest.TestCase):
	def test_default_on(self):
		self.assertTrue(view_filters_enabled("skills"))
		self.assertTrue(view_filters_enabled("macros"))

	def test_flag_rolls_a_view_back(self):
		with _conf("jarvis_list_filters_v2_off", ["macros"]):
			self.assertFalse(view_filters_enabled("macros"))
			self.assertTrue(view_filters_enabled("skills"))

	def test_disabled_view_schema_endpoint_declines(self):
		with _conf("jarvis_list_filters_v2_off", ["macros"]), _as(USER_SM):
			res = list_filters.get_list_filter_schema("macros")
		self.assertFalse(res.get("ok", True))
		self.assertEqual(res["error"]["code"], ERR_VIEW_NOT_FILTERABLE)

	def test_capabilities_map_lists_migrated_views(self):
		with _as(USER_SM):
			caps = list_filters_capabilities()
		self.assertIn("skills", caps)
		self.assertIn("macros", caps)
		self.assertTrue(caps["skills"])

	def test_compile_still_honours_v2_when_flag_off(self):
		# do-not-regress rule 8: a stale client's clause is answered, never dropped.
		clause = [{"doctype": MACRO, "fieldname": "macro_name", "operator": "=", "value": "x"}]
		with _conf("jarvis_list_filters_v2_off", ["macros"]):
			compiled = compile_list_filters("macros", clause, user=USER_SM)
		self.assertIn("`macro_name`", compiled.fragment())


class _CaptureHandler(logging.Handler):
	def __init__(self):
		super().__init__()
		self.records = []

	def emit(self, record):
		self.records.append(record.getMessage())


class TestFilterTelemetry(unittest.TestCase):
	def _capture(self, fn):
		logger = list_filters._filter_logger()
		handler = _CaptureHandler()
		logger.addHandler(handler)
		try:
			fn()
		finally:
			logger.removeHandler(handler)
		return handler.records

	def test_compile_emits_structural_line_without_values(self):
		clause = [{"doctype": MACRO, "fieldname": "macro_name", "operator": "like", "value": "SECRET-VALUE"}]
		records = self._capture(lambda: compile_list_filters("macros", clause, user=USER_SM))
		joined = "\n".join(records)
		self.assertIn("list_filter", joined)
		self.assertIn('"op": "like"', joined)
		self.assertIn("macros", joined)
		# The load-bearing invariant: no filter VALUE is ever logged.
		self.assertNotIn("SECRET-VALUE", joined)

	def test_emit_never_raises(self):
		# A broken clause entry must not let telemetry break a query.
		emit_filter_telemetry("macros", [], duration_ms=1.0)
