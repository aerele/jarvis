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
