"""The shared list-filter schema + compiler (plan 08 Phase 1, C08-1..C08-5).

The load-bearing tests, in the order they matter:

1. **Permlevel is enforced (C08-1).** Twice: against the real moat field
   (``Jarvis Agent Listing.skill_bundle``, permlevel 1, System-Manager-only) and
   end-to-end through a registered view with a Property-Setter-raised permlevel,
   so both the schema and the compiler are proven at two roles, in both
   directions — a privileged caller CAN see and filter it, an unprivileged one
   can do neither.
2. **Fail closed.** Unknown view / field / operator / value never degrades into
   a wider result set; nothing is silently stripped.
3. **Injection stays bound.** Identifiers come from metadata; values are params.
4. **Child EXISTS.** No parent duplication, and same-child clauses bind to the
   SAME child row (the C08-2 semantic decision).
5. **Caps** on client input; server-authored ``IN`` lists exempt (C08-5).
6. **Facet self-exclusion** (C08-3) and **schema cache user isolation**.

Runs on patterntest.localhost. Uses ``unittest``-style explicit fixtures with
prefix-based cleanup, matching test_feature_pages_api.py, because the permlevel
cases need REAL users holding REAL roles.
"""

from __future__ import annotations

import contextlib
import unittest

import frappe

from jarvis.chat import list_filters
from jarvis.chat.list_filters import (
	ERR_INVALID_OPERATOR,
	ERR_INVALID_VALUE,
	ERR_TOO_MANY_CLAUSES,
	ERR_TOO_MANY_VALUES,
	ERR_UNKNOWN_FIELD,
	ERR_UNKNOWN_VIEW,
	ERR_VALUE_TOO_LONG,
	ERR_VIEW_NOT_FILTERABLE,
	ListFilterError,
	build_field_catalog,
	compile_list_filters,
	get_schema,
	new_query,
)

SKILL = "Jarvis Custom Skill"
MACRO = "Jarvis Macro"
LISTING = "Jarvis Agent Listing"

USER_SM = "lf-user-sm@example.com"
USER_A = "lf-user-a@example.com"
USER_B = "lf-user-b@example.com"
PERMLEVEL_ROLE = "lf-permlevel-reader"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _ensure_role(name: str) -> None:
	if not frappe.db.exists("Role", name):
		frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()


def _ensure_user(email: str, roles: tuple[str, ...]) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
		frappe.db.commit()
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
	doc = frappe.get_doc("User", email)
	have = set(frappe.get_roles(email))
	want = set(roles)
	extra = {"System Manager"} - want
	if extra & have:
		doc.remove_roles(*(extra & have))
	missing = want - have
	if missing:
		doc.add_roles(*missing)
	frappe.db.commit()
	frappe.clear_cache(user=email)
	return email


def setUpModule() -> None:
	frappe.set_user("Administrator")
	_ensure_role(PERMLEVEL_ROLE)
	_ensure_user(USER_SM, ("Jarvis User", "System Manager"))
	_ensure_user(USER_A, ("Jarvis User", PERMLEVEL_ROLE))
	_ensure_user(USER_B, ("Jarvis User",))


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _wipe() -> None:
	for name in frappe.get_all(SKILL, filters={"skill_name": ["like", "lf-%"]}, pluck="name"):
		frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(MACRO, filters={"macro_name": ["like", "lf-%"]}, pluck="name"):
		frappe.delete_doc(MACRO, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _mk_skill(owner: str, name: str, **kw) -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": name,
				"description": kw.get("description", f"{name} description"),
				"instructions": kw.get("instructions", "do the thing"),
				"enabled": kw.get("enabled", 1),
				"user_invocable": kw.get("user_invocable", 1),
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _mk_macro(owner: str, name: str, steps: list[tuple[str, str]] | None = None, **kw) -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": name,
				"description": kw.get("description", "m"),
				"enabled": kw.get("enabled", 1),
				"stop_on_error": 1,
				"steps": [{"label": lbl, "prompt": prm} for lbl, prm in (steps or [("s0", "p0")])],
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _names(catalog) -> set[str]:
	return {f["fieldname"] for f in catalog}


def _entry(schema, doctype, fieldname):
	for f in schema["fields"]:
		if f["doctype"] == doctype and f["fieldname"] == fieldname:
			return f
	return None


# --------------------------------------------------------------------------- #
# 1. Permlevel (C08-1)
# --------------------------------------------------------------------------- #
class TestPermlevelIsEnforced(unittest.TestCase):
	"""The agents-IP moat case, on the real DocType, at two real roles."""

	def test_moat_field_is_visible_to_system_manager_only(self):
		self.assertTrue(frappe.db.exists("DocType", LISTING), "Jarvis Agent Listing must exist")
		meta = frappe.get_meta(LISTING)
		bundle = meta.get_field("skill_bundle")
		# Non-vacuity guard 1: the field is really permlevel 1 and really a
		# value-bearing type. If someone flattens the permlevel, this test must
		# fail LOUDLY rather than quietly pass for the wrong reason.
		self.assertIsNotNone(bundle, "skill_bundle disappeared from Jarvis Agent Listing")
		self.assertEqual(int(bundle.permlevel or 0), 1)
		self.assertNotIn(bundle.fieldtype, list_filters.NO_VALUE_FIELDTYPES)

		privileged = _names(build_field_catalog(LISTING, user=USER_SM))
		ordinary = _names(build_field_catalog(LISTING, user=USER_B))

		# Non-vacuity guard 2: the ordinary catalog is not simply empty.
		self.assertIn("category", ordinary, "the unprivileged catalog collapsed — test would be vacuous")
		self.assertIn("skill_bundle", privileged, "System Manager should read permlevel 1")
		self.assertNotIn(
			"skill_bundle",
			ordinary,
			"a plain Jarvis User can see the agents-IP moat field in the filter schema",
		)


class TestPermlevelEndToEnd(unittest.TestCase):
	"""Same rule, but through a REGISTERED view: schema + compiler, two roles.

	``Jarvis Custom Skill.instructions`` is pushed to permlevel 1 with a Property
	Setter (which also proves plan §11.1's 'customizations reflected without a
	frontend release'), and a Custom DocPerm grants permlevel-1 read to a role
	only USER_A holds.
	"""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		from frappe.permissions import add_permission, update_permission_property

		frappe.make_property_setter(
			{
				"doctype": SKILL,
				"fieldname": "instructions",
				"property": "permlevel",
				"value": 1,
				"property_type": "Int",
			},
			is_system_generated=False,
			validate_fields_for_doctype=False,
		)
		add_permission(SKILL, PERMLEVEL_ROLE, 1)
		update_permission_property(SKILL, PERMLEVEL_ROLE, 1, "read", 1)
		frappe.db.commit()
		frappe.clear_cache(doctype=SKILL)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		from frappe.permissions import reset_perms

		for name in frappe.get_all(
			"Property Setter",
			filters={"doc_type": SKILL, "field_name": "instructions", "property": "permlevel"},
			pluck="name",
		):
			frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)
		reset_perms(SKILL)
		frappe.db.commit()
		frappe.clear_cache(doctype=SKILL)

	def test_schema_shows_the_field_only_to_the_privileged_role(self):
		privileged = get_schema("skills", user=USER_A)
		ordinary = get_schema("skills", user=USER_B)
		self.assertIsNotNone(
			_entry(privileged, SKILL, "instructions"),
			"the permlevel-1 grant did not take effect — fixture broken, test would be vacuous",
		)
		self.assertIsNone(_entry(ordinary, SKILL, "instructions"))
		# Non-vacuity: the ordinary schema is otherwise full.
		self.assertIsNotNone(_entry(ordinary, SKILL, "skill_name"))
		self.assertIsNotNone(_entry(ordinary, SKILL, "enabled"))

	def test_compiler_rejects_the_field_for_the_unprivileged_role(self):
		clause = [{"doctype": SKILL, "fieldname": "instructions", "operator": "like", "value": "secret"}]
		with self.assertRaises(ListFilterError) as caught:
			compile_list_filters("skills", clause, user=USER_B)
		self.assertEqual(caught.exception.filter_error_code, ERR_UNKNOWN_FIELD)

	def test_compiler_accepts_the_field_for_the_privileged_role(self):
		clause = [{"doctype": SKILL, "fieldname": "instructions", "operator": "like", "value": "secret"}]
		compiled = compile_list_filters("skills", clause, user=USER_A)
		fragment = compiled.fragment()
		self.assertIn("`instructions`", fragment)
		self.assertNotIn("secret", fragment)
		self.assertIn("%secret%", compiled.params.values())

	def test_schema_cache_never_serves_one_user_to_another(self):
		# Warm A, then B, then A again: B's build must not evict or overwrite A's.
		first_a = get_schema("skills", user=USER_A)
		b = get_schema("skills", user=USER_B)
		second_a = get_schema("skills", user=USER_A)
		self.assertIsNotNone(_entry(first_a, SKILL, "instructions"))
		self.assertIsNone(_entry(b, SKILL, "instructions"))
		self.assertIsNotNone(_entry(second_a, SKILL, "instructions"))
		self.assertNotEqual(first_a["schema_revision"], b["schema_revision"])


# --------------------------------------------------------------------------- #
# 2. Catalog shape / Frappe parity
# --------------------------------------------------------------------------- #
class TestCatalogShape(unittest.TestCase):
	def test_standard_fields_present_and_docstatus_absent_for_non_submittable(self):
		schema = get_schema("skills", user=USER_A)
		names = {f["fieldname"] for f in schema["fields"] if not f["is_child"]}
		for std in ("name", "owner", "creation", "modified", "modified_by", "idx"):
			self.assertIn(std, names)
		self.assertNotIn("docstatus", names, "Jarvis Custom Skill is not submittable")

	def test_table_containers_are_not_offered_but_their_children_are(self):
		schema = get_schema("macros", user=USER_A)
		main = {f["fieldname"] for f in schema["fields"] if not f["is_child"]}
		self.assertNotIn("steps", main, "a Table field is not itself filterable")
		child = {(f["doctype"], f["fieldname"]) for f in schema["fields"] if f["is_child"]}
		self.assertIn(("Jarvis Macro Step", "label"), child)
		self.assertIn(("Jarvis Macro Step", "prompt"), child)

	def test_child_fields_are_labelled_the_frappe_way(self):
		schema = get_schema("macros", user=USER_A)
		entry = _entry(schema, "Jarvis Macro Step", "label")
		self.assertTrue(entry["label"].endswith("(Jarvis Macro Step)"), entry["label"])

	def test_operator_vocabulary_matches_frappe_per_family(self):
		schema = get_schema("macros", user=USER_A)
		check = _entry(schema, MACRO, "enabled")
		self.assertEqual(check["operators"], ["="], "Check exposes Equals only")
		self.assertEqual(check["default_operator"], "=")

		data = _entry(schema, MACRO, "macro_name")
		self.assertEqual(data["default_operator"], "like", "Data defaults to like on a small table")
		self.assertNotIn("Between", data["operators"])
		self.assertIn("like", data["operators"])

		dt = _entry(schema, MACRO, "last_run_at")
		self.assertEqual(dt["default_operator"], "Between")
		for banned in ("like", "not like", "in", "not in", "=", "!="):
			self.assertNotIn(banned, dt["operators"], f"Datetime must not offer {banned}")

		select = _entry(schema, MACRO, "schedule_frequency")
		self.assertEqual(select["default_operator"], "=")
		self.assertNotIn("like", select["operators"])

		link = _entry(get_schema("skills", user=USER_A), SKILL, "target_role")
		self.assertEqual(link["default_operator"], "=")
		for banned in (">", "<", ">=", "<=", "Between", "Timespan"):
			self.assertNotIn(banned, link["operators"])

		small_text = _entry(schema, MACRO, "description")
		self.assertEqual(
			small_text["default_operator"],
			"=",
			"deviation D5: Frappe defaults Small Text to '=', not 'like' as plan §5.1 claimed",
		)

	def test_json_array_standard_fields_are_like_only(self):
		schema = get_schema("skills", user=USER_A)
		assign = _entry(schema, SKILL, "_assign")
		if assign is None:
			self.skipTest("_assign column not present on this site")
		self.assertTrue(assign["json_array"])
		self.assertEqual(assign["default_operator"], "like")
		self.assertEqual(set(assign["operators"]), {"like", "not like", "is"})

	def test_nested_set_operators_are_not_advertised(self):
		for view in ("skills", "macros"):
			for entry in get_schema(view, user=USER_A)["fields"]:
				self.assertNotIn("descendants of", entry["operators"])
				self.assertNotIn("ancestors of", entry["operators"])


# --------------------------------------------------------------------------- #
# 3. Fail closed
# --------------------------------------------------------------------------- #
class TestFailsClosed(unittest.TestCase):
	def _code(self, fn, *a, **kw) -> str:
		with self.assertRaises(ListFilterError) as caught:
			fn(*a, **kw)
		return caught.exception.filter_error_code

	def test_unknown_view(self):
		self.assertEqual(
			self._code(compile_list_filters, "not-a-view", [], user=USER_A), ERR_UNKNOWN_VIEW
		)

	def test_projection_view_is_not_filterable_yet(self):
		# file_box / approvals / agents_catalog have no declared projection schema
		# yet: answering with the root DocType's catalog would describe rows the
		# endpoint does not return.
		for view in ("file_box", "approvals", "agents_catalog", "support_tickets"):
			with self.subTest(view=view):
				self.assertEqual(
					self._code(compile_list_filters, view, [], user=USER_A), ERR_VIEW_NOT_FILTERABLE
				)

	def test_excluded_view_is_not_filterable(self):
		self.assertEqual(
			self._code(compile_list_filters, "chat_conversation_sidebar", [], user=USER_A),
			ERR_VIEW_NOT_FILTERABLE,
		)

	def test_unknown_field(self):
		clause = [{"doctype": SKILL, "fieldname": "no_such_field", "operator": "=", "value": "x"}]
		self.assertEqual(self._code(compile_list_filters, "skills", clause, user=USER_A), ERR_UNKNOWN_FIELD)

	def test_field_from_another_doctype_is_rejected(self):
		clause = [{"doctype": "User", "fieldname": "password", "operator": "like", "value": "%"}]
		self.assertEqual(self._code(compile_list_filters, "skills", clause, user=USER_A), ERR_UNKNOWN_FIELD)

	def test_child_field_of_an_unrelated_doctype_is_rejected(self):
		clause = [{"doctype": "Jarvis Macro Step", "fieldname": "prompt", "operator": "=", "value": "x"}]
		self.assertEqual(
			self._code(compile_list_filters, "skills", clause, user=USER_A),
			ERR_UNKNOWN_FIELD,
			"a child of a DIFFERENT root must not be filterable here",
		)

	def test_invalid_operator_for_family(self):
		clause = [{"doctype": MACRO, "fieldname": "enabled", "operator": "like", "value": "1"}]
		self.assertEqual(
			self._code(compile_list_filters, "macros", clause, user=USER_A), ERR_INVALID_OPERATOR
		)

	def test_unknown_operator(self):
		clause = [{"doctype": MACRO, "fieldname": "macro_name", "operator": "regexp", "value": "x"}]
		self.assertEqual(
			self._code(compile_list_filters, "macros", clause, user=USER_A), ERR_INVALID_OPERATOR
		)

	def test_bad_check_value(self):
		clause = [{"doctype": MACRO, "fieldname": "enabled", "operator": "=", "value": "maybe"}]
		self.assertEqual(self._code(compile_list_filters, "macros", clause, user=USER_A), ERR_INVALID_VALUE)

	def test_select_value_must_be_an_option(self):
		clause = [
			{"doctype": MACRO, "fieldname": "schedule_frequency", "operator": "=", "value": "hourly"}
		]
		self.assertEqual(self._code(compile_list_filters, "macros", clause, user=USER_A), ERR_INVALID_VALUE)

	def test_empty_in_list_is_rejected(self):
		clause = [{"doctype": MACRO, "fieldname": "macro_name", "operator": "in", "value": []}]
		self.assertEqual(self._code(compile_list_filters, "macros", clause, user=USER_A), ERR_INVALID_VALUE)

	def test_is_needs_set_or_not_set(self):
		clause = [{"doctype": MACRO, "fieldname": "description", "operator": "is", "value": "yes"}]
		self.assertEqual(self._code(compile_list_filters, "macros", clause, user=USER_A), ERR_INVALID_VALUE)

	def test_password_fieldtype_is_never_offered(self):
		# Deviation D2. Proven structurally: no catalog on any registered
		# doctype-backed view contains a Password field.
		from jarvis.chat import list_registry

		for view in list_registry.document_list_views():
			for entry in build_field_catalog(view.root_doctype, user="Administrator"):
				self.assertNotEqual(entry["fieldtype"], "Password")


# --------------------------------------------------------------------------- #
# 4. Injection
# --------------------------------------------------------------------------- #
class TestInjectionStaysBound(unittest.TestCase):
	PAYLOADS = (
		"' OR 1=1 -- ",
		"'; DROP TABLE `tabJarvis Macro`; --",
		"\\' UNION SELECT name FROM `tabUser` --",
		"%' AND (SELECT SLEEP(5)) AND '%",
	)

	def test_values_never_reach_sql(self):
		for payload in self.PAYLOADS:
			with self.subTest(payload=payload):
				clause = [{"doctype": MACRO, "fieldname": "macro_name", "operator": "like", "value": payload}]
				compiled = compile_list_filters("macros", clause, user=USER_A)
				fragment = compiled.fragment()
				self.assertNotIn("OR 1=1", fragment)
				self.assertNotIn("DROP TABLE", fragment)
				self.assertNotIn("UNION", fragment)
				self.assertNotIn("SLEEP", fragment)
				# Exactly one placeholder, and the payload lives in the params.
				self.assertEqual(fragment.count("%("), 1)
				self.assertTrue(any(payload.strip("%") in str(v) for v in compiled.params.values()))

	def test_identifier_shaped_payloads_are_rejected_not_interpolated(self):
		for bad in ("name`; DROP TABLE x; --", "name FROM tabUser", "*"):
			with self.subTest(bad=bad):
				clause = [{"doctype": MACRO, "fieldname": bad, "operator": "=", "value": "x"}]
				with self.assertRaises(ListFilterError) as caught:
					compile_list_filters("macros", clause, user=USER_A)
				self.assertEqual(caught.exception.filter_error_code, ERR_UNKNOWN_FIELD)

	def test_a_bound_payload_really_runs_and_matches_nothing(self):
		"""End to end: the query executes (so the SQL is valid) and the payload is
		treated as text, not as syntax."""
		_wipe()
		_mk_macro(USER_A, "lf-inject-target")
		try:
			from jarvis.chat.macros_api import list_macros_page

			with _as(USER_A):
				res = list_macros_page(
					filters_v2=[
						{
							"doctype": MACRO,
							"fieldname": "macro_name",
							"operator": "like",
							"value": "' OR 1=1 -- ",
						}
					]
				)
			self.assertEqual(res["total"], 0)
			self.assertEqual(res["rows"], [])
			self.assertTrue(frappe.db.exists(MACRO, {"macro_name": "lf-inject-target"}))
		finally:
			_wipe()


# --------------------------------------------------------------------------- #
# 5. Child EXISTS semantics (C08-2)
# --------------------------------------------------------------------------- #
class TestChildFilters(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_wipe()
		# two steps, distinct values -> the same-child grouping case
		cls.split = _mk_macro(USER_A, "lf-child-split", steps=[("alpha", "p1"), ("beta", "p2")])
		# two steps that BOTH match one clause -> the duplication case
		cls.dup = _mk_macro(USER_A, "lf-child-dup", steps=[("gamma", "x"), ("gamma", "y")])

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe()

	def _run(self, clauses):
		from jarvis.chat.macros_api import list_macros_page

		with _as(USER_A):
			return list_macros_page(filters_v2=clauses, page_length=50)

	def test_matching_children_do_not_duplicate_the_parent(self):
		res = self._run([{"doctype": "Jarvis Macro Step", "fieldname": "label", "operator": "=", "value": "gamma"}])
		self.assertEqual(res["total"], 1, "two matching child rows must not duplicate the parent")
		self.assertEqual(len(res["rows"]), 1)
		self.assertEqual(res["rows"][0]["name"], self.dup)
		self.assertFalse(res["has_more"])

	def test_two_clauses_on_one_child_must_hit_the_same_row(self):
		same = self._run(
			[
				{"doctype": "Jarvis Macro Step", "fieldname": "label", "operator": "=", "value": "alpha"},
				{"doctype": "Jarvis Macro Step", "fieldname": "prompt", "operator": "=", "value": "p1"},
			]
		)
		self.assertEqual(same["total"], 1, "alpha/p1 live on the SAME step, so the macro matches")

		cross = self._run(
			[
				{"doctype": "Jarvis Macro Step", "fieldname": "label", "operator": "=", "value": "alpha"},
				{"doctype": "Jarvis Macro Step", "fieldname": "prompt", "operator": "=", "value": "p2"},
			]
		)
		self.assertEqual(
			cross["total"],
			0,
			"alpha and p2 are on DIFFERENT steps: Frappe's single-join semantics "
			"require one child row to satisfy both clauses (C08-2)",
		)

	def test_one_exists_per_child_doctype(self):
		compiled = compile_list_filters(
			"macros",
			[
				{"doctype": "Jarvis Macro Step", "fieldname": "label", "operator": "=", "value": "alpha"},
				{"doctype": "Jarvis Macro Step", "fieldname": "prompt", "operator": "=", "value": "p1"},
			],
			user=USER_A,
		)
		fragment = compiled.fragment()
		self.assertEqual(fragment.count("EXISTS ("), 1, "same-child clauses share ONE subquery")
		self.assertIn("`parenttype`", fragment)
		self.assertIn("`parent` = `tabJarvis Macro`.`name`", fragment)

	def test_child_and_main_clauses_combine(self):
		res = self._run(
			[
				{"doctype": "Jarvis Macro Step", "fieldname": "label", "operator": "=", "value": "alpha"},
				{"doctype": MACRO, "fieldname": "macro_name", "operator": "like", "value": "lf-child"},
			]
		)
		self.assertEqual(res["total"], 1)
		self.assertEqual(res["rows"][0]["name"], self.split)


# --------------------------------------------------------------------------- #
# 6. Caps (plan §9 / C08-5)
# --------------------------------------------------------------------------- #
class TestCaps(unittest.TestCase):
	def _code(self, clauses) -> str:
		with self.assertRaises(ListFilterError) as caught:
			compile_list_filters("macros", clauses, user=USER_A)
		return caught.exception.filter_error_code

	def test_clause_count_cap(self):
		clause = {"doctype": MACRO, "fieldname": "macro_name", "operator": "like", "value": "x"}
		ok = [dict(clause) for _ in range(list_filters.MAX_CLAUSES)]
		compile_list_filters("macros", ok, user=USER_A)  # at the cap: fine
		self.assertEqual(self._code(ok + [dict(clause)]), ERR_TOO_MANY_CLAUSES)

	def test_in_value_cap(self):
		values = [f"v{i}" for i in range(list_filters.MAX_IN_VALUES + 1)]
		clause = [{"doctype": MACRO, "fieldname": "macro_name", "operator": "in", "value": values}]
		self.assertEqual(self._code(clause), ERR_TOO_MANY_VALUES)

	def test_value_length_cap(self):
		clause = [
			{
				"doctype": MACRO,
				"fieldname": "macro_name",
				"operator": "like",
				"value": "x" * (list_filters.MAX_VALUE_CHARS + 1),
			}
		]
		self.assertEqual(self._code(clause), ERR_VALUE_TOO_LONG)

	def test_server_authored_in_lists_are_exempt(self):
		"""C08-5: 'every skill shared with me' is not attacker input."""
		big = tuple(f"row-{i}" for i in range(list_filters.MAX_IN_VALUES * 5))
		q = new_query("skills", user=USER_A)
		q.server_condition("name IN %(shared)s", shared=big)
		q.apply([])
		self.assertIn("name IN %(shared)s", q.where())
		self.assertEqual(len(q.params()["shared"]), list_filters.MAX_IN_VALUES * 5)


# --------------------------------------------------------------------------- #
# 7. The structural barrier + facet self-exclusion
# --------------------------------------------------------------------------- #
class TestQueryBarrierAndFacets(unittest.TestCase):
	def test_server_predicate_and_user_clauses_are_separate_stores(self):
		q = new_query("macros", user=USER_A)
		q.server_condition("owner = %(me)s", me=USER_A)
		q.apply([{"doctype": MACRO, "fieldname": "enabled", "operator": "=", "value": 1}])
		where = q.where()
		self.assertTrue(where.startswith("owner = %(me)s AND "), where)
		self.assertIn("`tabJarvis Macro`.`enabled`", where)
		# The user parameter namespace is disjoint from the server's.
		user_params = [k for k in q.params() if k.startswith(list_filters.USER_PARAM_PREFIX)]
		self.assertTrue(user_params)
		self.assertNotIn("me", user_params)

	def test_server_condition_refuses_the_user_parameter_namespace(self):
		q = new_query("macros", user=USER_A)
		with self.assertRaises(ValueError):
			q.server_condition("x = %(jf0_0)s", jf0_0="spoof")

	def test_server_condition_refuses_positional_placeholders(self):
		q = new_query("macros", user=USER_A)
		with self.assertRaises(ValueError):
			q.server_condition("owner = %s")

	def test_no_clauses_leaves_the_server_predicate_untouched(self):
		q = new_query("macros", user=USER_A)
		q.server_condition("owner = %(me)s", me=USER_A)
		q.apply(None)
		self.assertEqual(q.where(), "owner = %(me)s")
		self.assertEqual(q.params(), {"me": USER_A})

	def test_facet_query_drops_only_its_own_dimension(self):
		q = new_query("macros", user=USER_A)
		q.server_condition("owner = %(me)s", me=USER_A)
		q.apply(
			[
				{"doctype": MACRO, "fieldname": "enabled", "operator": "=", "value": 1},
				{"doctype": MACRO, "fieldname": "schedule_frequency", "operator": "=", "value": "daily"},
			]
		)
		full = q.where()
		self.assertIn("`enabled`", full)
		self.assertIn("`schedule_frequency`", full)

		faceted = q.where(exclude_dimension=(MACRO, "schedule_frequency"))
		self.assertIn("owner = %(me)s", faceted)
		self.assertIn("`enabled`", faceted)
		self.assertNotIn("`schedule_frequency`", faceted)

	def test_facet_exclusion_regroups_the_child_exists(self):
		q = new_query("macros", user=USER_A)
		q.apply(
			[
				{"doctype": "Jarvis Macro Step", "fieldname": "label", "operator": "=", "value": "a"},
				{"doctype": "Jarvis Macro Step", "fieldname": "prompt", "operator": "=", "value": "b"},
			]
		)
		self.assertEqual(q.where().count("EXISTS ("), 1)
		reduced = q.where(exclude_dimension=("Jarvis Macro Step", "prompt"))
		self.assertEqual(reduced.count("EXISTS ("), 1)
		self.assertIn("`label`", reduced)
		self.assertNotIn("`prompt`", reduced)

	def test_excluding_the_only_child_clause_drops_the_exists_entirely(self):
		q = new_query("macros", user=USER_A)
		q.server_condition("owner = %(me)s", me=USER_A)
		q.apply([{"doctype": "Jarvis Macro Step", "fieldname": "label", "operator": "=", "value": "a"}])
		self.assertEqual(q.where(exclude_dimension=("Jarvis Macro Step", "label")), "owner = %(me)s")


# --------------------------------------------------------------------------- #
# 8. Typed compilation details
# --------------------------------------------------------------------------- #
class TestTypedCompilation(unittest.TestCase):
	def _fragment(self, view, clause):
		return compile_list_filters(view, [clause], user=USER_A).fragment()

	def test_is_set_and_is_not_set(self):
		st = self._fragment("macros", {"doctype": MACRO, "fieldname": "description", "operator": "is", "value": "set"})
		self.assertIn("ifnull(`tabJarvis Macro`.`description`, '') != ''", st)
		unset = self._fragment(
			"macros", {"doctype": MACRO, "fieldname": "description", "operator": "is", "value": "not set"}
		)
		self.assertIn("ifnull(`tabJarvis Macro`.`description`, '') = ''", unset)

	def test_between_on_datetime_expands_day_bounds(self):
		compiled = compile_list_filters(
			"macros",
			[
				{
					"doctype": MACRO,
					"fieldname": "last_run_at",
					"operator": "Between",
					"value": ["2026-07-01", "2026-07-31"],
				}
			],
			user=USER_A,
		)
		values = sorted(str(v) for v in compiled.params.values())
		self.assertTrue(values[0].startswith("2026-07-01 00:00:00"), values)
		self.assertTrue(values[1].startswith("2026-07-31 23:59:59"), values)
		self.assertIn("BETWEEN", compiled.fragment())

	def test_timespan_resolves_to_a_bound_range(self):
		compiled = compile_list_filters(
			"macros",
			[{"doctype": MACRO, "fieldname": "creation", "operator": "Timespan", "value": "today"}],
			user=USER_A,
		)
		self.assertIn("BETWEEN", compiled.fragment())
		self.assertEqual(len([v for v in compiled.params.values()]), 2)

	def test_unknown_timespan_fails_closed(self):
		with self.assertRaises(ListFilterError) as caught:
			compile_list_filters(
				"macros",
				[{"doctype": MACRO, "fieldname": "creation", "operator": "Timespan", "value": "last fortnight"}],
				user=USER_A,
			)
		self.assertEqual(caught.exception.filter_error_code, ERR_INVALID_VALUE)

	def test_like_wraps_wildcards_like_frappes_ui(self):
		compiled = compile_list_filters(
			"macros",
			[{"doctype": MACRO, "fieldname": "macro_name", "operator": "like", "value": "month end"}],
			user=USER_A,
		)
		self.assertIn("%month end%", compiled.params.values())

	def test_like_keeps_a_user_supplied_wildcard(self):
		compiled = compile_list_filters(
			"macros",
			[{"doctype": MACRO, "fieldname": "macro_name", "operator": "like", "value": "month%"}],
			user=USER_A,
		)
		self.assertIn("month%", compiled.params.values())
		self.assertNotIn("%month%%", compiled.params.values())

	def test_in_binds_a_tuple(self):
		compiled = compile_list_filters(
			"macros",
			[{"doctype": MACRO, "fieldname": "macro_name", "operator": "in", "value": ["a", "b"]}],
			user=USER_A,
		)
		self.assertIn("IN %(", compiled.fragment())
		self.assertIn(("a", "b"), compiled.params.values())

	def test_check_normalizes_to_zero_or_one(self):
		compiled = compile_list_filters(
			"macros",
			[{"doctype": MACRO, "fieldname": "enabled", "operator": "=", "value": "1"}],
			user=USER_A,
		)
		self.assertIn(1, compiled.params.values())

	def test_operator_defaults_to_the_schema_default_when_omitted(self):
		compiled = compile_list_filters(
			"macros", [{"doctype": MACRO, "fieldname": "macro_name", "value": "x"}], user=USER_A
		)
		self.assertIn("LIKE", compiled.fragment())
		self.assertIn("%x%", compiled.params.values())
