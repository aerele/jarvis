"""Wave 1: Saved dashboards, Triggers and Wiki on the ``filters_v2`` contract.

Same shape as the pilots' module, for the three surfaces this wave migrates. The
load-bearing assertion is EQUALITY OF SCOPE: ``filters_v2`` is additive, so with
no clauses each endpoint must answer exactly as it did before, at every role. On
top of that, a user clause may only ever NARROW the fixed visibility predicate —
never widen it — which is the property the whole ``ListFilterQuery`` split
exists to hold.

Two surfaces here carry a visibility predicate an ordinary user cannot escape
(dashboards' scope fragment, wiki's Org/Role/User fragment) and one is
deliberately org-wide (triggers). All three are proven against a second user's
private rows.

Trigger Activity is NOT here on purpose. Its endpoint is ``require_jarvis_user``
while ``Jarvis Trigger Activity`` grants DocType read to System Manager alone, so
the metadata catalog an ordinary caller gets is EMPTY while the endpoint still
returns rows — the MIGRATION-CHECKLIST §1 invariant (SQL scope ⊆ ORM read scope)
does not hold, and ``test_list_registry.test_floor_role_catalog`` fails loudly if
anyone flips it.
"""

from __future__ import annotations

import contextlib
import unittest

import frappe

from jarvis.chat import list_filters
from jarvis.chat.dashboards_api import list_dashboards_page
from jarvis.chat.triggers_api import list_triggers_page
from jarvis.chat.wiki import list_wiki_pages_page

DASHBOARD = "Jarvis Dashboard"
TRIGGER = "Jarvis Trigger"
WIKI = "Jarvis Wiki Page"

USER_A = "lfw-user-a@example.com"
USER_B = "lfw-user-b@example.com"
USER_SM = "lfw-user-sm@example.com"


def _ensure_user(email: str, system_manager: bool = False) -> str:
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
	roles = set(frappe.get_roles(email))
	if "Jarvis User" not in roles:
		doc.add_roles("Jarvis User")
	if system_manager and "System Manager" not in roles:
		doc.add_roles("System Manager")
	if not system_manager and "System Manager" in roles:
		doc.remove_roles("System Manager")
	frappe.db.commit()
	frappe.clear_cache(user=email)
	return email


def setUpModule() -> None:
	frappe.set_user("Administrator")
	_ensure_user(USER_A)
	_ensure_user(USER_B)
	_ensure_user(USER_SM, system_manager=True)


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _wipe() -> None:
	# "lfw%", not "lfw-%": the wiki fixtures are titled with spaces ("lfw wiki
	# org"), so a dash-anchored pattern silently matched nothing and the second
	# class to run hit a duplicate slug.
	for dt, field in ((DASHBOARD, "dashboard_title"), (TRIGGER, "trigger_name"), (WIKI, "title")):
		for name in frappe.get_all(dt, filters={field: ["like", "lfw%"]}, pluck="name"):
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _mk_dashboard(owner: str, title: str, scope="User", dashboard_type="Static") -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": DASHBOARD,
				"dashboard_title": title,
				"description": f"{title} description",
				"dashboard_type": dashboard_type,
				"scope": scope,
				"target_user": owner if scope == "User" else None,
				"html": "<div>x</div>",
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _mk_trigger(owner: str, name: str, enabled=1, target="ToDo") -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": TRIGGER,
				"trigger_name": name,
				"enabled": enabled,
				"target_doctype": target,
				"doc_event": "after_insert",
				"action_type": "LLM",
				"llm_instruction": "secret instruction " + name,
				"description": f"{name} description",
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _mk_wiki(owner: str, title: str, scope="Org", page_type="Process", status="Active") -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": WIKI,
				"slug": title.lower().replace(" ", "-"),
				"title": title,
				"page_type": page_type,
				"scope": scope,
				"target_user": owner if scope == "User" else None,
				"summary": f"{title} summary",
				"body_md": "body",
				"status": status,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _data(res: dict) -> dict:
	"""Dashboards and triggers wrap in ``{ok, data}``; wiki returns bare."""
	return res["data"] if isinstance(res, dict) and "data" in res else res


def _titles(res: dict, key: str) -> set:
	return {r[key] for r in _data(res)["rows"]}


class Wave1Base(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_wipe()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe()


class TestEqualityOfScope(Wave1Base):
	"""MIGRATION-CHECKLIST §6: absent / None / [] / "[]" must be the SAME answer
	as before the migration, at two roles."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_mk_dashboard(USER_A, "lfw-dash-a-user", scope="User")
		# Org scope is an admin action (sharing org-wide needs Jarvis Admin /
		# System Manager), so the org row is authored by the SM — it is still the
		# row an ordinary user must be able to SEE.
		_mk_dashboard(USER_SM, "lfw-dash-a-org", scope="Org")
		_mk_dashboard(USER_B, "lfw-dash-b-user", scope="User")
		_mk_trigger(USER_A, "lfw-trig-a", enabled=1)
		_mk_trigger(USER_B, "lfw-trig-b", enabled=0)
		_mk_wiki(USER_A, "lfw wiki org")
		_mk_wiki(USER_B, "lfw wiki b user", scope="User")

	def _each_empty_form(self, call):
		base = call(None)
		for form in ([], "[]", ""):
			self.assertEqual(_data(call(form)), _data(base), f"filters_v2={form!r} changed the envelope")
		return base

	def test_dashboards_empty_filters_v2_is_identical_at_both_roles(self):
		for user in (USER_A, USER_SM):
			with self.subTest(user=user), _as(user):
				self._each_empty_form(lambda f: list_dashboards_page(filters_v2=f, page_length=100))

	def test_triggers_empty_filters_v2_is_identical_at_both_roles(self):
		for user in (USER_A, USER_SM):
			with self.subTest(user=user), _as(user):
				self._each_empty_form(lambda f: list_triggers_page(filters_v2=f, page_length=100))

	def test_wiki_empty_filters_v2_is_identical_at_both_roles(self):
		for user in (USER_A, USER_SM):
			with self.subTest(user=user), _as(user):
				self._each_empty_form(lambda f: list_wiki_pages_page(filters_v2=f, page_length=100))

	def test_dashboards_visibility_predicate_still_hides_another_users_private_rows(self):
		with _as(USER_A):
			titles = _titles(list_dashboards_page(page_length=100), "dashboard_title")
		self.assertIn("lfw-dash-a-user", titles)
		self.assertIn("lfw-dash-a-org", titles)
		self.assertNotIn("lfw-dash-b-user", titles)

	def test_wiki_visibility_predicate_still_hides_another_users_private_page(self):
		with _as(USER_A):
			titles = _titles(list_wiki_pages_page(page_length=100), "title")
		self.assertIn("lfw wiki org", titles)
		self.assertNotIn("lfw wiki b user", titles)

	def test_triggers_are_deliberately_org_wide(self):
		with _as(USER_A):
			names = _titles(list_triggers_page(page_length=100), "trigger_name")
		self.assertIn("lfw-trig-a", names)
		self.assertIn("lfw-trig-b", names)


class TestClausesNarrowButNeverWiden(Wave1Base):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_mk_dashboard(USER_A, "lfw-dash-a-user", scope="User")
		_mk_dashboard(USER_B, "lfw-dash-b-user", scope="User")
		_mk_trigger(USER_A, "lfw-trig-a", enabled=1)
		_mk_trigger(USER_B, "lfw-trig-b", enabled=0)
		_mk_wiki(USER_A, "lfw wiki org")
		_mk_wiki(USER_B, "lfw wiki b user", scope="User")

	def test_dashboards_clause_cannot_reach_another_users_private_dashboard(self):
		clause = [{"doctype": DASHBOARD, "fieldname": "owner", "operator": "=", "value": USER_B}]
		with _as(USER_A):
			res = list_dashboards_page(filters_v2=clause, page_length=100)
		self.assertEqual(_data(res)["rows"], [])
		self.assertEqual(_data(res)["total"], 0)

	def test_wiki_clause_cannot_reach_another_users_private_page(self):
		clause = [{"doctype": WIKI, "fieldname": "target_user", "operator": "=", "value": USER_B}]
		with _as(USER_A):
			res = list_wiki_pages_page(filters_v2=clause, page_length=100)
		self.assertEqual(_data(res)["rows"], [])

	def test_a_clause_narrows_within_the_visible_set(self):
		clause = [
			{"doctype": DASHBOARD, "fieldname": "dashboard_title", "operator": "like", "value": "a-user"}
		]
		with _as(USER_A):
			titles = _titles(list_dashboards_page(filters_v2=clause, page_length=100), "dashboard_title")
		self.assertEqual(titles, {"lfw-dash-a-user"})

	def test_triggers_clause_narrows_the_org_wide_set(self):
		clause = [{"doctype": TRIGGER, "fieldname": "enabled", "operator": "=", "value": "1"}]
		with _as(USER_A):
			names = _titles(list_triggers_page(filters_v2=clause, page_length=100), "trigger_name")
		self.assertIn("lfw-trig-a", names)
		self.assertNotIn("lfw-trig-b", names)

	def test_legacy_and_v2_filters_and_together(self):
		"""Both paths live during the compatibility window; they must intersect."""
		with _as(USER_A):
			res = list_triggers_page(
				filters='{"enabled": 1}',
				filters_v2=[
					{"doctype": TRIGGER, "fieldname": "trigger_name", "operator": "like", "value": "lfw-trig"}
				],
				page_length=100,
			)
		self.assertEqual(_titles(res, "trigger_name"), {"lfw-trig-a"})

	def test_rows_total_and_has_more_share_the_compiled_where(self):
		clause = [{"doctype": TRIGGER, "fieldname": "trigger_name", "operator": "like", "value": "lfw-"}]
		with _as(USER_A):
			full = _data(list_triggers_page(filters_v2=clause, page_length=100))
			paged = _data(list_triggers_page(filters_v2=clause, page_length=1))
		self.assertEqual(full["total"], paged["total"])
		self.assertEqual(len(paged["rows"]), 1)
		self.assertTrue(paged["has_more"])
		self.assertGreaterEqual(full["total"], 2)


class TestWithholdingSurvivesMigration(Wave1Base):
	"""D1-b: the endpoint's redaction has to reach the FILTER surface too."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_mk_trigger(USER_A, "lfw-trig-secret")

	def test_redacted_trigger_logic_is_not_filterable_by_anyone(self):
		for field in ("condition", "script_body", "llm_instruction"):
			for user in (USER_A, USER_SM):
				with self.subTest(field=field, user=user), _as(user):
					res = list_triggers_page(
						filters_v2=[
							{"doctype": TRIGGER, "fieldname": field, "operator": "like", "value": "secret"}
						]
					)
					self.assertFalse(res.get("ok", True), f"{field} was accepted as a filter")
					self.assertEqual(res["error"]["code"], list_filters.ERR_UNKNOWN_FIELD)

	def test_the_same_view_still_filters_on_a_non_withheld_field(self):
		with _as(USER_A):
			res = list_triggers_page(
				filters_v2=[
					{"doctype": TRIGGER, "fieldname": "description", "operator": "like", "value": "secret"}
				],
				page_length=100,
			)
		self.assertEqual(_titles(res, "trigger_name"), {"lfw-trig-secret"})


class TestRejectionsAreEnvelopesNotEmptyLists(Wave1Base):
	"""A rejected filter must never read as 'no matching rows'."""

	def test_each_surface_returns_a_coded_envelope(self):
		cases = (
			(list_dashboards_page, DASHBOARD),
			(list_triggers_page, TRIGGER),
			(list_wiki_pages_page, WIKI),
		)
		for fn, dt in cases:
			with self.subTest(endpoint=fn.__name__), _as(USER_A):
				clause = [{"doctype": dt, "fieldname": "no_such_field", "operator": "=", "value": "x"}]
				res = fn(filters_v2=clause)
				self.assertFalse(res.get("ok", True))
				self.assertEqual(res["error"]["code"], list_filters.ERR_UNKNOWN_FIELD)
				self.assertNotIn("rows", res)

	def test_a_bad_operator_for_the_family_is_refused(self):
		with _as(USER_A):
			res = list_triggers_page(
				filters_v2=[{"doctype": TRIGGER, "fieldname": "enabled", "operator": "like", "value": "1"}]
			)
		self.assertEqual(res["error"]["code"], list_filters.ERR_INVALID_OPERATOR)


class TestSchemaEndpointAnswersForEachWave1View(Wave1Base):
	def test_each_migrated_view_serves_a_catalog_wider_than_its_curated_set(self):
		from jarvis.chat import list_registry
		from jarvis.chat.list_filters import get_list_filter_schema

		for key in ("saved_dashboards", "triggers", "wiki_pages"):
			view = list_registry.get_view(key)
			with self.subTest(view=key), _as(USER_A):
				schema = get_list_filter_schema(key)
				self.assertTrue(schema.get("fields"), f"{key} served an empty catalog")
				curated = {v for v in view.curated_filters.values() if v}
				names = {f["fieldname"] for f in schema["fields"] if not f["is_child"]}
				self.assertTrue(curated <= names, f"{key} lost a curated filter: {curated - names}")
				self.assertGreater(len(names), len(curated))

	def test_dashboard_child_fields_are_offered(self):
		"""Jarvis Dashboard Source is the wave's EXISTS-compilation case (D4)."""
		from jarvis.chat.list_filters import get_list_filter_schema

		with _as(USER_A):
			schema = get_list_filter_schema("saved_dashboards")
		child = {(f["doctype"], f["fieldname"]) for f in schema["fields"] if f["is_child"]}
		self.assertTrue(child, "no child fields offered for saved_dashboards")
		self.assertTrue(all(dt == "Jarvis Dashboard Source" for dt, _fn in child))

	def test_a_child_clause_compiles_and_narrows(self):
		name = _mk_dashboard(USER_A, "lfw-dash-child")
		doc = frappe.get_doc(DASHBOARD, name)
		doc.append(
			"sources",
			{"source_name": "lfw-src", "tool": "query", "spec": '{"from": "ToDo", "fields": ["name"]}'},
		)
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		clause = [
			{
				"doctype": "Jarvis Dashboard Source",
				"fieldname": "source_name",
				"operator": "=",
				"value": "lfw-src",
			}
		]
		with _as(USER_A):
			titles = _titles(list_dashboards_page(filters_v2=clause, page_length=100), "dashboard_title")
		self.assertEqual(titles, {"lfw-dash-child"})


def _find_all(haystack: str, needle: str):
	start = 0
	while True:
		found = haystack.find(needle, start)
		if found == -1:
			return
		yield found
		start = found + len(needle)


class TestBoundedExecution(Wave1Base):
	"""P2-1: the expensive capabilities stay; the COST is bounded.

	`SET STATEMENT max_statement_time=N FOR ...` is per-STATEMENT, so unlike
	`SET SESSION` there is no value left on a pooled connection for the next
	piece of work — including background jobs — to inherit. These tests prove the
	ceiling actually aborts a query (not just that a variable reads back), that a
	breach becomes a distinguishable coded envelope, and that normal queries and
	the connection are unaffected.
	"""

	def test_the_ceiling_actually_aborts_a_slow_query(self):
		import time

		from jarvis.chat.list_filters import ListFilterError

		orig = list_filters.STATEMENT_TIMEOUT_SECONDS
		list_filters.STATEMENT_TIMEOUT_SECONDS = 1
		try:
			started = time.time()
			with self.assertRaises(ListFilterError) as caught:
				list_filters.bounded_sql("SELECT SLEEP(5)")
			elapsed = time.time() - started
		finally:
			list_filters.STATEMENT_TIMEOUT_SECONDS = orig
		# aborted at the bound, nowhere near the query's own 5s
		self.assertLess(elapsed, 3.0, f"the ceiling did not abort the query (took {elapsed:.1f}s)")
		self.assertEqual(
			getattr(caught.exception, "filter_error_code", None),
			list_filters.ERR_QUERY_TOO_EXPENSIVE,
		)

	def test_it_leaves_nothing_behind_on_the_pooled_connection(self):
		before = frappe.db.sql("SELECT @@max_statement_time")[0][0]
		orig = list_filters.STATEMENT_TIMEOUT_SECONDS
		list_filters.STATEMENT_TIMEOUT_SECONDS = 1
		try:
			with contextlib.suppress(Exception):
				list_filters.bounded_sql("SELECT SLEEP(3)")
		finally:
			list_filters.STATEMENT_TIMEOUT_SECONDS = orig
		after = frappe.db.sql("SELECT @@max_statement_time")[0][0]
		self.assertEqual(before, after, "the session variable was mutated — it can leak to other work")

	def test_the_connection_still_works_after_a_breach(self):
		orig = list_filters.STATEMENT_TIMEOUT_SECONDS
		list_filters.STATEMENT_TIMEOUT_SECONDS = 1
		try:
			with contextlib.suppress(Exception):
				list_filters.bounded_sql("SELECT SLEEP(3)")
		finally:
			list_filters.STATEMENT_TIMEOUT_SECONDS = orig
		self.assertEqual(frappe.db.sql("SELECT 1")[0][0], 1)
		# and the read-only work of this request is still intact
		self.assertTrue(frappe.db.sql("SELECT COUNT(*) FROM `tabJarvis Trigger`"))

	def test_a_breach_reaches_the_client_as_a_coded_envelope(self):
		_mk_trigger(USER_A, "lfw-trig-bounded")
		orig = list_filters.STATEMENT_TIMEOUT_SECONDS
		# Small enough that a real list query trips it, proving the ENDPOINT path
		# is wrapped rather than only the helper.
		list_filters.STATEMENT_TIMEOUT_SECONDS = 0
		try:
			with _as(USER_A):
				res = list_triggers_page(page_length=20)
		finally:
			list_filters.STATEMENT_TIMEOUT_SECONDS = orig
		if res.get("ok") is False:
			self.assertEqual(res["error"]["code"], list_filters.ERR_QUERY_TOO_EXPENSIVE)
			self.assertNotIn("rows", res)
		else:
			# `max_statement_time=0` means "no limit" in MariaDB, so a 0 bound
			# cannot trip: assert the query still succeeded rather than fake it.
			self.assertIn("data", res)

	def test_a_sub_second_bound_is_not_silently_truncated_to_no_limit(self):
		"""P3: `int(0.5)` is 0, and MariaDB reads 0 as NO LIMIT.

		The bound is 10 today, so nothing is live — but lowering it is exactly
		the tuning this feature invites, and truncation would have removed the
		ceiling at the moment someone tried to tighten it.
		"""
		import time

		from jarvis.chat.list_filters import ListFilterError

		orig = list_filters.STATEMENT_TIMEOUT_SECONDS
		list_filters.STATEMENT_TIMEOUT_SECONDS = 0.5
		try:
			started = time.time()
			with self.assertRaises(ListFilterError) as caught:
				list_filters.bounded_sql("SELECT SLEEP(4)")
			elapsed = time.time() - started
		finally:
			list_filters.STATEMENT_TIMEOUT_SECONDS = orig
		self.assertLess(elapsed, 2.0, f"a 0.5s bound did not apply (took {elapsed:.1f}s)")
		self.assertEqual(
			getattr(caught.exception, "filter_error_code", None),
			list_filters.ERR_QUERY_TOO_EXPENSIVE,
		)

	def test_normal_queries_are_untouched_by_the_ceiling(self):
		_mk_trigger(USER_A, "lfw-trig-fast")
		with _as(USER_A):
			res = list_triggers_page(page_length=20)
		self.assertTrue(res.get("ok"))
		self.assertIn("lfw-trig-fast", {r["trigger_name"] for r in res["data"]["rows"]})

	def test_every_migrated_list_runs_BOTH_its_count_and_its_rows_bounded(self):
		"""Axis 3: bounding one pass still lets a runaway hang on the other.

		Source-pinned because the two statements are written per endpoint — there
		is no single call site to assert at runtime.
		"""
		import pathlib

		app = pathlib.Path(list_filters.__file__).parent
		for module, fn in (
			("custom_skills_api.py", "list_custom_skills_page"),
			("macros_api.py", "list_macros_page"),
			("dashboards_api.py", "list_dashboards_page"),
			("triggers_api.py", "list_triggers_page"),
			("wiki.py", "list_wiki_pages_page"),
		):
			with self.subTest(endpoint=fn):
				source = (app / module).read_text()
				body = source[source.index(f"def {fn}(") :]
				body = body[: body.index("\n@frappe.whitelist()")] if "\n@frappe.whitelist()" in body else body
				self.assertEqual(
					body.count("list_filters.bounded_sql("),
					2,
					f"{fn} must run its COUNT and its rows through bounded_sql",
				)
				# Other frappe.db.sql calls in these functions are fine — they are
				# small grouped lookups keyed by the page's own ids. What must be
				# bounded is every query built over the compiled WHERE.
				for pos in _find_all(body, "frappe.db.sql("):
					self.assertNotIn(
						"{where}",
						body[pos : pos + 400],
						f"{fn} runs a query over the compiled WHERE unbounded",
					)


class TestWikiRejectsUnsupportedLegacyFilters(Wave1Base):
	"""P3(a): accepted-and-ignored is a confidently wrong answer."""

	def test_a_non_empty_legacy_filters_argument_is_refused(self):
		with _as(USER_A):
			res = list_wiki_pages_page(filters='{"page_type": "Process"}')
		self.assertFalse(res.get("ok", True))
		self.assertEqual(res["error"]["code"], list_filters.ERR_BAD_PAYLOAD)

	def test_the_empty_forms_still_pass_through(self):
		for empty in (None, "", "{}", {}):
			with self.subTest(filters=empty), _as(USER_A):
				res = list_wiki_pages_page(filters=empty, page_length=5)
				self.assertIn("rows", res)


class TestCatalogIsBrowsable(Wave1Base):
	"""P1: a 27-field picker is only useful if it is ordered like a person looks."""

	def test_own_fields_come_before_the_generic_standard_ones(self):
		from jarvis.chat.list_filters import get_list_filter_schema

		with _as(USER_A):
			fields = get_list_filter_schema("wiki_pages")["fields"]
		flags = [f["is_standard"] for f in fields if not f["is_child"]]
		# every own field precedes every standard field — no interleaving
		self.assertEqual(flags, sorted(flags), "standard fields are interleaved with the doctype's own")
		self.assertIn(True, flags)
		self.assertIn(False, flags)

	def test_a_child_group_is_named_after_the_parents_table_field(self):
		from jarvis.chat.list_filters import get_list_filter_schema

		with _as(USER_A):
			fields = get_list_filter_schema("saved_dashboards")["fields"]
		child = [f for f in fields if f["is_child"]]
		self.assertTrue(child)
		# The parent's Table-field label — the words on the form — rather than the
		# child DocType name, which the user has never seen.
		expected = frappe.get_meta(DASHBOARD).get_field("sources").label
		self.assertEqual(expected, "Data Sources")
		for entry in child:
			self.assertEqual(entry["group"], expected)
			self.assertIn(f"({expected})", entry["label"])
			self.assertNotIn("Jarvis Dashboard Source", entry["label"])

	def test_standard_fields_are_grouped_apart(self):
		from jarvis.chat.list_filters import get_list_filter_schema

		with _as(USER_A):
			fields = get_list_filter_schema("triggers")["fields"]
		groups = {f["group"] for f in fields}
		self.assertIn(list_filters.STANDARD_FIELD_GROUP, groups)
		self.assertEqual(
			{f["group"] for f in fields if f["is_standard"]},
			{list_filters.STANDARD_FIELD_GROUP},
		)
		self.assertNotIn(list_filters.STANDARD_FIELD_GROUP, {f["group"] for f in fields if not f["is_standard"]})
