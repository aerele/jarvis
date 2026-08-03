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
from frappe.model import data_fieldtypes

from jarvis.chat import list_filters
from jarvis.chat.list_filters import (
	ERR_INVALID_OPERATOR,
	ERR_INVALID_VALUE,
	ERR_VIEW_NOT_FILTERABLE,
	ERR_VIEW_ROLLED_BACK,
	ListFilterError,
	_Clause,
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


def _mk_macro_with(owner: str, name: str, rows_by_field: dict[str, list[tuple[str, str]]]) -> str:
	"""A macro whose child steps are distributed across named Table containers, so a
	step can live under ``steps`` OR a second container (M2 leak fixture). ``steps``
	is a mandatory child table, so a filler row is added when the caller only put
	rows under the second container."""
	frappe.set_user("Administrator")
	doc = frappe.get_doc(
		{"doctype": MACRO, "macro_name": name, "description": "m", "enabled": 1, "stop_on_error": 1}
	)
	rows_by_field = dict(rows_by_field)
	rows_by_field.setdefault("steps", [("filler", "f")])
	for field_name, rows in rows_by_field.items():
		for lbl, prm in rows:
			doc.append(field_name, {"label": lbl, "prompt": prm})
	doc.flags.ignore_validate = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(MACRO, doc.name, "owner", owner)
	frappe.db.commit()
	return doc.name


class TestTwoTableFieldsSameChild(unittest.TestCase):
	"""When a parent reaches ONE child DocType through TWO Table fields the catalog
	lists each child field ONCE (deduped by (child DocType, fieldname)), and a
	clause on it compiles to ONE ``EXISTS`` per child DocType. The EXISTS now binds
	``parentfield IN (<containers the caller may read>)`` (M2 / ledger D16), so it
	matches a row under EITHER readable container for a caller who reads both — the
	behaviour verified here — while a caller who can read only one container is
	scoped to that one (see :class:`TestContainerParentfieldLeak`)."""

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

	def test_both_readable_binds_every_container(self):
		# SM reads both containers, so the child entry carries both parentfields and
		# the compiled EXISTS binds both — the both-readable case keeps working.
		entry = next(
			f
			for f in build_field_catalog(MACRO, user=USER_SM)
			if f["doctype"] == STEP and f["fieldname"] == "label"
		)
		self.assertEqual(sorted(entry["parentfields"]), ["steps", "steps_alt"])
		clause = [{"doctype": STEP, "fieldname": "label", "operator": "=", "value": "x"}]
		compiled = compile_list_filters("macros", clause, user=USER_SM)
		self.assertIn("`parentfield` IN", compiled.fragment())
		self.assertEqual(
			{v for v in compiled.params.values() if isinstance(v, tuple)},
			{("steps", "steps_alt")},
		)


class TestContainerParentfieldLeak(unittest.TestCase):
	"""M2: the container permission bypass. Two Table fields reach ONE child
	DocType; the second is above the floor caller's permlevel. A clause via the
	READABLE container must never match rows attached to the hidden one — same child
	DocType, same parent+parenttype, told apart only by ``parentfield``."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		from frappe.permissions import add_permission, update_permission_property

		cls._perms_before = set(frappe.get_all("Custom DocPerm", filters={"parent": MACRO}, pluck="name"))
		if not frappe.db.exists("Custom Field", {"dt": MACRO, "fieldname": "steps_hidden"}):
			frappe.get_doc(
				{
					"doctype": "Custom Field",
					"dt": MACRO,
					"fieldname": "steps_hidden",
					"label": "Steps Alt",
					"fieldtype": "Table",
					"options": STEP,
				}
			).insert(ignore_permissions=True)
		# Raise the SECOND container to permlevel 1 and grant that level only to the
		# role USER_A holds — so USER_A/USER_SM read both containers, USER_B reads
		# only `steps`.
		frappe.make_property_setter(
			{
				"doctype": MACRO,
				"fieldname": "steps_hidden",
				"property": "permlevel",
				"value": 1,
				"property_type": "Int",
			},
			is_system_generated=False,
			validate_fields_for_doctype=False,
		)
		add_permission(MACRO, PERMLEVEL_ROLE, 1)
		update_permission_property(MACRO, PERMLEVEL_ROLE, 1, "read", 1)
		frappe.db.commit()
		frappe.clear_cache()
		# leakp under the HIDDEN container only, and under the readable one only.
		cls.hidden = _mk_macro_with(USER_B, "lf-leak-hidden", {"steps_hidden": [("leakp", "p")]})
		cls.readable = _mk_macro_with(USER_B, "lf-leak-readable", {"steps": [("leakp", "p")]})
		cls.priv = _mk_macro_with(USER_A, "lf-leak-priv", {"steps_hidden": [("leakp", "p")]})

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in (cls.hidden, cls.readable, cls.priv):
			if frappe.db.exists(MACRO, name):
				frappe.delete_doc(MACRO, name, force=True, ignore_permissions=True)
		for name in frappe.get_all(
			"Property Setter",
			filters={"doc_type": MACRO, "property": "permlevel", "field_name": "steps_hidden"},
			pluck="name",
		):
			frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)
		for name in frappe.get_all("Custom DocPerm", filters={"parent": MACRO}, pluck="name"):
			if name not in cls._perms_before:
				frappe.delete_doc("Custom DocPerm", name, force=True, ignore_permissions=True)
		cf = frappe.db.get_value("Custom Field", {"dt": MACRO, "fieldname": "steps_hidden"})
		if cf:
			frappe.delete_doc("Custom Field", cf, force=True, ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()

	def _run(self, user, clauses):
		from jarvis.chat.macros_api import list_macros_page

		with _as(user):
			return list_macros_page(filters_v2=clauses, page_length=50)

	_LEAKP = [{"doctype": STEP, "fieldname": "label", "operator": "=", "value": "leakp"}]

	def test_floor_user_does_not_match_rows_under_the_hidden_container(self):
		# The security assertion: USER_B owns BOTH lf-leak-hidden and
		# lf-leak-readable, but filtering leakp returns ONLY the readable one.
		res = self._run(USER_B, self._LEAKP)
		names = {r["name"] for r in res["rows"]}
		self.assertIn(self.readable, names)
		self.assertNotIn(self.hidden, names, "a hidden container's child row leaked through EXISTS")

	def test_floor_user_binds_only_the_readable_parentfield(self):
		compiled = compile_list_filters("macros", self._LEAKP, user=USER_B)
		self.assertEqual(
			{v for v in compiled.params.values() if isinstance(v, tuple)},
			{("steps",)},
			"the floor caller's EXISTS must bind only the container it can read",
		)

	def test_privileged_user_still_matches_the_other_container(self):
		# USER_A reads permlevel 1, so its clause spans both containers — the
		# both-readable case is not narrowed by the fix.
		res = self._run(USER_A, self._LEAKP)
		self.assertIn(self.priv, {r["name"] for r in res["rows"]})

	def test_view_excluded_container_scopes_the_parentfield(self):
		# An endpoint that withholds the second container via excluded_fields must
		# scope the child EXISTS the same way permlevel does. USER_A CAN read
		# steps_hidden (permlevel 1), so the exclusion — not the permlevel — is what
		# narrows the bound parentfield set here.
		entry = next(
			f
			for f in build_field_catalog(MACRO, user=USER_A, excluded_fields=("steps_hidden",))
			if f["doctype"] == STEP and f["fieldname"] == "label"
		)
		self.assertEqual(entry["parentfields"], ["steps"], "view-excluded container still bound")


class TestMultiSelectContainerParentfieldScoping(unittest.TestCase):
	"""The Table MultiSelect equivalent of the leak: the parentfield binding is
	keyed on the container fieldname regardless of Table vs Table MultiSelect, so a
	hidden MultiSelect container is scoped out the same way."""

	CHILD = "Has Role"

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		from frappe.permissions import add_permission, update_permission_property

		cls._perms_before = set(frappe.get_all("Custom DocPerm", filters={"parent": MACRO}, pluck="name"))
		for fn, label in (("roles_a", "Roles A"), ("roles_b", "Roles B")):
			if not frappe.db.exists("Custom Field", {"dt": MACRO, "fieldname": fn}):
				frappe.get_doc(
					{
						"doctype": "Custom Field",
						"dt": MACRO,
						"fieldname": fn,
						"label": label,
						"fieldtype": "Table MultiSelect",
						"options": cls.CHILD,
					}
				).insert(ignore_permissions=True)
		frappe.make_property_setter(
			{
				"doctype": MACRO,
				"fieldname": "roles_b",
				"property": "permlevel",
				"value": 1,
				"property_type": "Int",
			},
			is_system_generated=False,
			validate_fields_for_doctype=False,
		)
		add_permission(MACRO, PERMLEVEL_ROLE, 1)
		update_permission_property(MACRO, PERMLEVEL_ROLE, 1, "read", 1)
		frappe.db.commit()
		frappe.clear_cache()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			"Property Setter",
			filters={"doc_type": MACRO, "property": "permlevel", "field_name": "roles_b"},
			pluck="name",
		):
			frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)
		for name in frappe.get_all("Custom DocPerm", filters={"parent": MACRO}, pluck="name"):
			if name not in cls._perms_before:
				frappe.delete_doc("Custom DocPerm", name, force=True, ignore_permissions=True)
		for fn in ("roles_a", "roles_b"):
			cf = frappe.db.get_value("Custom Field", {"dt": MACRO, "fieldname": fn})
			if cf:
				frappe.delete_doc("Custom Field", cf, force=True, ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()

	def test_floor_user_scopes_multiselect_to_readable_container(self):
		entry = next(
			(
				f
				for f in build_field_catalog(MACRO, user=USER_B)
				if f["doctype"] == self.CHILD and f["fieldname"] == "role"
			),
			None,
		)
		self.assertIsNotNone(entry, "the MultiSelect Link value field was not catalogued")
		self.assertEqual(entry["parentfields"], ["roles_a"], "hidden MultiSelect container leaked")

	def test_privileged_user_sees_both_multiselect_containers(self):
		# USER_A holds the permlevel-1 role granted below, so it reads roles_b.
		entry = next(
			f
			for f in build_field_catalog(MACRO, user=USER_A)
			if f["doctype"] == self.CHILD and f["fieldname"] == "role"
		)
		self.assertEqual(sorted(entry["parentfields"]), ["roles_a", "roles_b"])


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
# S5 — Between/Timespan are temporal-only, asserted as a COMPLEMENT
# --------------------------------------------------------------------------- #
class TestRangeOperatorsAreTemporalOnly(unittest.TestCase):
	"""The golden rule stated in the positive: NO catalogable fieldtype advertises
	(or compiles) ``Between``/``Timespan`` unless it is explicitly temporal — the
	COMPLEMENT of a fixed allow-list, so a NEW or currently-unmapped type (Duration,
	Autocomplete, a future control) is caught automatically rather than inheriting
	the empty invalid set and silently offering a date range."""

	#: A range operator, IF advertised at all, may only be on an explicitly temporal
	#: family. Time is temporal but Frappe's own invalid_condition_map still withholds
	#: Between/Timespan from it (a time-of-day range is not a date range), so the
	#: complement is one-directional — "Between ⟹ temporal", never "temporal ⟹
	#: Between". Only Date/Datetime actually carry it.
	TEMPORAL = frozenset({"Date", "Datetime", "Time"})
	CARRIES_RANGE = frozenset({"Date", "Datetime"})

	#: EVERY value-bearing fieldtype the schema builder can emit into a catalog,
	#: enumerated from Frappe's canonical ``data_fieldtypes`` (so a fieldtype Frappe
	#: adds later is swept the day it lands) minus the two families the builder never
	#: catalogs: the Table containers and the secret types (Password, deviation D2),
	#: which is exactly the ``NO_VALUE_FIELDTYPES | SECRET_FIELDTYPES`` gate in
	#: ``build_field_catalog``. Deriving from the source is the whole point of S5: a
	#: hand-kept tuple silently omitted Signature/Icon (and Long Int), so promoting
	#: any of them into ``_TEMPORAL_FIELDTYPES`` by mistake stayed green.
	CATALOGABLE = tuple(
		sorted(frozenset(data_fieldtypes) - list_filters.NO_VALUE_FIELDTYPES - list_filters.SECRET_FIELDTYPES)
	)

	def test_between_or_timespan_implies_temporal(self):
		# The complement: whatever family advertises a range MUST be temporal. A
		# non-temporal type — mapped or not (Duration/Autocomplete/Geolocation are
		# unmapped) — advertises neither.
		for ft in self.CATALOGABLE:
			ops = allowed_operators("x", ft)
			with self.subTest(fieldtype=ft):
				if "Between" in ops:
					self.assertIn(ft, self.TEMPORAL, f"{ft} advertises Between but is not temporal")
				if "Timespan" in ops:
					self.assertIn(ft, self.TEMPORAL, f"{ft} advertises Timespan but is not temporal")
				if ft not in self.TEMPORAL:
					self.assertNotIn("Between", ops, f"{ft} (non-temporal) must not advertise Between")
					self.assertNotIn("Timespan", ops, f"{ft} (non-temporal) must not advertise Timespan")

	def test_date_families_do_advertise_a_range(self):
		# Non-vacuity: Date/Datetime really do keep Between.
		for ft in self.CARRIES_RANGE:
			with self.subTest(fieldtype=ft):
				self.assertIn("Between", allowed_operators("when", ft))

	def test_compiler_guard_rejects_between_on_an_unmapped_numeric(self):
		# Belt to the schema's suspenders: _between_bounds itself rejects a
		# non-temporal type even if a clause reaches it bypassing the schema.
		entry = {"fieldtype": "Duration", "label": "Elapsed"}
		with self.assertRaises(ListFilterError) as caught:
			list_filters._between_bounds(entry, ["1", "2"])
		self.assertEqual(caught.exception.filter_error_code, ERR_INVALID_OPERATOR)
		with self.assertRaises(ListFilterError) as caught:
			list_filters._timespan_bounds(entry, "last 7 days")
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

	def test_thousands_separators_are_stripped_like_frappe_flt(self):
		# S10: frappe.utils.flt runs s.replace(",", "") before float(), so a
		# human-shaped "10,500.50" is a valid number, not a rejected one. The
		# stripped value is what we bind, too (one parser for validate + bind).
		entry = {"fieldtype": "Currency", "label": "Amount"}
		self.assertEqual(list_filters._as_number(entry, "10,500.50"), 10500.50)
		int_entry = {"fieldtype": "Int", "label": "Count"}
		self.assertEqual(list_filters._as_number(int_entry, "1,000"), 1000)

	def test_garbage_still_rejected_after_comma_strip(self):
		# Stripping commas must not open a hole: non-numeric and non-finite still
		# fail with the stable code.
		entry = {"fieldtype": "Float", "label": "Amount"}
		for bad in ("abc", "1,2,a", "inf", "nan"):
			with self.subTest(value=bad), self.assertRaises(ListFilterError):
				list_filters._as_number(entry, bad)


# --------------------------------------------------------------------------- #
# D8 — a child clause with no readable container must NEVER compile to `IN ()`
# --------------------------------------------------------------------------- #
class TestEmptyParentfieldsFailClosed(unittest.TestCase):
	"""A child clause carries the readable parent containers as ``parentfields``
	(M2). An empty set would bind ``parentfield IN ()`` — a hard MariaDB syntax
	error (pymysql renders the empty tuple literally) that 500s every child-table
	filter for up to SCHEMA_CACHE_TTL after a restart-only deploy served a stale
	pre-v2 cached schema. The compiler must fail closed with the stable
	invalid-field code instead, independently of the CONTRACT_VERSION bump that
	discards such caches on deploy."""

	def test_empty_parentfields_raise_unknown_field_not_empty_in(self):
		clause = _Clause(
			index=0,
			entry={"is_child": True, "doctype": STEP, "parentfields": []},
			operator="=",
			value="x",
		)
		with self.assertRaises(ListFilterError) as caught:
			list_filters.compile_validated(MACRO, [clause], ref="`m`")
		self.assertEqual(caught.exception.filter_error_code, list_filters.ERR_UNKNOWN_FIELD)

	def test_missing_parentfields_key_also_fails_closed(self):
		# A pre-M2 cached entry has no `parentfields` key at all, not just an empty
		# list — `.get(...) or ()` must land it on the same closed failure.
		clause = _Clause(
			index=0,
			entry={"is_child": True, "doctype": STEP},
			operator="=",
			value="x",
		)
		with self.assertRaises(ListFilterError) as caught:
			list_filters.compile_validated(MACRO, [clause], ref="`m`")
		self.assertEqual(caught.exception.filter_error_code, list_filters.ERR_UNKNOWN_FIELD)


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
			"parentfields": [],
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

	def test_a_new_wire_key_changes_revision(self):
		# S8: the digest serializes the WHOLE field dict, so any wire key — the live
		# example is `parentfields` — is covered without hand-listing it. A change
		# to it must move the revision, or a caching consumer serves a stale panel.
		a = list_filters._schema_digest([self._field(parentfields=["steps"])])
		b = list_filters._schema_digest([self._field(parentfields=["steps", "steps_alt"])])
		self.assertNotEqual(a, b, "a change to a wire key (parentfields) must change the revision")

	def test_group_change_changes_revision(self):
		# S8: the field-picker GROUP the client renders is not a wire key of its own —
		# it is derived from `is_child`+`doctype` (filterModel `fieldOptions`). A field
		# that moves group (a different doctype, or parent→child) must therefore move
		# the revision, or the picker regroups against a stale ETag. Guards the digest
		# against a future "hash only some keys" change that dropped either input.
		a = list_filters._schema_digest([self._field(doctype=MACRO, is_child=False)])
		b = list_filters._schema_digest([self._field(doctype=STEP, is_child=True)])
		self.assertNotEqual(a, b, "a change to a field's group (doctype/is_child) must change the revision")

	def test_header_keys_change_revision(self):
		# S8: schema-LEVEL keys the client renders (root_doctype, is_large_table,
		# contract_version, label) live in the header, not on a field. Each must
		# move the revision.
		base = [self._field()]
		for key, alt in (
			("root_doctype", "Jarvis Skill"),
			("is_large_table", True),
			("contract_version", 999),
			("label", "Renamed"),
		):
			with self.subTest(header_key=key):
				h1 = {
					"root_doctype": MACRO,
					"is_large_table": False,
					"contract_version": 1,
					"label": "Macros",
				}
				h2 = dict(h1, **{key: alt})
				self.assertNotEqual(
					list_filters._schema_digest(base, {}, h1),
					list_filters._schema_digest(base, {}, h2),
					f"a change to header {key!r} must change schema_revision",
				)


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

	def test_rolled_back_view_declines_with_its_own_code(self):
		# M1.2: a MIGRATED view rolled back by the flag is distinct from one that
		# was never migrated, so the client can be honest ("turned off", no retry).
		with _conf("jarvis_list_filters_v2_off", ["macros"]), _as(USER_SM):
			res = list_filters.get_list_filter_schema("macros")
		self.assertFalse(res.get("ok", True))
		self.assertEqual(res["error"]["code"], ERR_VIEW_ROLLED_BACK)

	def test_unmigrated_view_declines_with_not_filterable(self):
		# The other side of the M1.2 distinction: a registered-but-PENDING view
		# (never migrated) still declines with ERR_VIEW_NOT_FILTERABLE, NOT the
		# rolled-back code — the two truths must never collapse into one message.
		with _as(USER_SM):
			res = list_filters.get_list_filter_schema("approvals")
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

	def test_off_false_or_zero_disables_nothing(self):
		# D4: `false`/`0` is the most natural spelling of "kill switch not engaged"
		# for a key named `*_off`. Neither equals any of None/""/[]/() under `==`, so
		# without an explicit allow they fell THROUGH to the fail-closed branch and
		# silently disabled EVERY migrated view, fleet-wide. They must read as
		# "disable nothing". `0.0` is included as the JSON-number sibling of `0`.
		for off in (False, 0, 0.0):
			with self.subTest(value=off), _conf("jarvis_list_filters_v2_off", off):
				self.assertTrue(view_filters_enabled("macros"), f"{off!r} disabled macros")
				self.assertTrue(view_filters_enabled("skills"), f"{off!r} disabled skills")

	def test_malformed_flag_fails_closed_disabling_every_view(self):
		# M1.1: the kill switch must NEVER fail open. A bool (the classic
		# `"...v2_off": true` typo) used to raise inside the parse and silently
		# re-enable everything; now it disables every migrated view. `True`/`1` are
		# ambiguous junk (distinct from the `false`/`0` = "off" case above) and, with
		# a dict or a mixed list, must all fail CLOSED.
		for bad in (True, 1, {"macros": True}, ["macros", 2]):
			with self.subTest(value=bad), _conf("jarvis_list_filters_v2_off", bad):
				self.assertFalse(view_filters_enabled("macros"), f"{bad!r} failed OPEN")
				self.assertFalse(view_filters_enabled("skills"), f"{bad!r} failed OPEN")

	def test_malformed_flag_logs_once(self):
		# One loud Error Log, deduped so a broken switch cannot flood it.
		frappe.cache().delete_value("jarvis:list-filter-flag-misconfig")
		flt = {"method": ("like", "%kill-switch misconfigured%")}
		before = frappe.db.count("Error Log", flt)
		with _conf("jarvis_list_filters_v2_off", True):
			view_filters_enabled("macros")
			view_filters_enabled("skills")  # second call must NOT log again
		frappe.db.commit()
		after = frappe.db.count("Error Log", flt)
		self.assertEqual(after - before, 1, "a malformed kill switch must log exactly once per TTL")


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
		# S9: a GENUINELY broken clause (its schema entry is missing `fieldtype`)
		# must not let telemetry break a query. An empty list never entered the
		# except-guard, so the old test passed even with the guard removed — this
		# one turns red the moment the try/except comes off `emit_filter_telemetry`.
		broken = _Clause(index=0, entry={"is_child": False}, operator="=", value="x")
		emit_filter_telemetry("macros", [broken], duration_ms=1.0)
