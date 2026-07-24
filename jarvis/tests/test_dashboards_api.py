"""Tests for jarvis.chat.dashboards_api - the SPA-facing Dashboards endpoints.

Fixture users: a Jarvis Admin, two plain jarvis users, plus Administrator as
the SM tier. A hermetic Query Report fixture (over ToDo) backs the run_report
paths. Every dashboard / ToDo / conversation created here is tracked and
deleted in tearDown; no LLM/network calls anywhere on these paths.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.api import search_workspace, send_message
from jarvis.chat.dashboards_api import (
	delete_dashboard,
	get_dashboard,
	get_dashboards_caps,
	list_dashboards_page,
	run_dashboard_source,
	save_dashboard,
)
from jarvis.permissions import (
	JARVIS_ADMIN_ROLE,
	JARVIS_USER_ROLE,
	ensure_jarvis_admin_role,
	ensure_jarvis_user_role,
)

DASHBOARD = "Jarvis Dashboard"

ADMIN_USER = "jarvis-dashapi-admin@example.com"
PLAIN_A = "jarvis-dashapi-user-a@example.com"
PLAIN_B = "jarvis-dashapi-user-b@example.com"
CUSTOM_ROLE = "Jarvis Dash Test Role"

REPORT_NAME = "Jarvis Dash Test Report"
PREPARED_REPORT_NAME = "Jarvis Dash Prepared Report"

_GET_LIST_SPEC = {"doctype": "ToDo", "fields": ["name", "description"], "limit": 10}

_HTML_WITH_BLOCK = (
	'<div id="app"></div>\n'
	'<script type="application/json" id="jarvis-sources">'
	'{"sources": [{"source_name": "todos", "tool": "get_list", '
	'"spec": {"doctype": "ToDo", "fields": ["name", "description"], "limit": 10}}]}'
	"</script>"
)


def _ensure_user(email: str, roles: list[str]) -> None:
	"""Create the fixture user if missing; idempotent."""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "DashApi",
				"last_name": "Test",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", email)
	doc.add_roles(*roles)
	frappe.db.commit()


def _ensure_custom_role() -> None:
	if not frappe.db.exists("Role", CUSTOM_ROLE):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": CUSTOM_ROLE,
				"desk_access": 1,
				"is_custom": 1,
			}
		).insert(ignore_permissions=True)


def _ensure_report(name: str, prepared: int) -> None:
	"""Hermetic Query Report over ToDo (is_standard No, runs inline unless
	``prepared``). Idempotent; left in place across tests like the roles."""
	if frappe.db.exists("Report", name):
		return
	frappe.get_doc(
		{
			"doctype": "Report",
			"report_name": name,
			"ref_doctype": "ToDo",
			"report_type": "Query Report",
			"is_standard": "No",
			"query": "select name, description from `tabToDo` order by creation desc limit 5",
			"prepared_report": prepared,
			"disabled": 0,
		}
	).insert(ignore_permissions=True)


class _DashboardsApiTestCase(FrappeTestCase):
	def setUp(self):
		ensure_jarvis_user_role()
		ensure_jarvis_admin_role()
		_ensure_custom_role()
		_ensure_user(ADMIN_USER, [JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE, CUSTOM_ROLE])
		_ensure_user(PLAIN_A, [JARVIS_USER_ROLE])
		_ensure_user(PLAIN_B, [JARVIS_USER_ROLE])
		_ensure_report(REPORT_NAME, prepared=0)
		_ensure_report(PREPARED_REPORT_NAME, prepared=1)
		frappe.db.commit()
		self._orig_user = frappe.session.user
		self._dashboards: list[str] = []
		self._todos: list[str] = []
		self._convs: list[str] = []

	def tearDown(self):
		frappe.set_user(self._orig_user)
		for name in self._dashboards:
			if frappe.db.exists(DASHBOARD, name):
				frappe.delete_doc(DASHBOARD, name, ignore_permissions=True, force=True)
		for name in self._todos:
			if frappe.db.exists("ToDo", name):
				frappe.delete_doc("ToDo", name, ignore_permissions=True, force=True)
		for name in self._convs:
			frappe.db.delete("Jarvis Chat Message", {"conversation": name})
			if frappe.db.exists("Jarvis Conversation", name):
				frappe.delete_doc("Jarvis Conversation", name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _save(self, payload: dict) -> dict:
		r = save_dashboard(frappe.as_json(payload))
		self._dashboards.append(r["data"]["name"])
		return r["data"]

	def _mk_static(self, user: str, title: str | None = None, **extra) -> dict:
		frappe.set_user(user)
		return self._save(
			{
				"dashboard_title": title or f"dash-{frappe.generate_hash(length=8)}",
				"html": "<h1>hello</h1>",
				**extra,
			}
		)

	def _mk_connected(self, user: str, sources: list, title: str | None = None) -> dict:
		frappe.set_user(user)
		return self._save(
			{
				"dashboard_title": title or f"dash-{frappe.generate_hash(length=8)}",
				"html": "<h1>data</h1>",
				"sources": sources,
			}
		)

	def _mk_todo(self, user: str, description: str = "dash test todo") -> str:
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": description,
				"allocated_to": user,
			}
		).insert(ignore_permissions=True)
		self._todos.append(todo.name)
		frappe.db.commit()
		return todo.name


class TestCaps(_DashboardsApiTestCase):
	def test_caps_shape_plain_vs_admin(self):
		frappe.set_user(PLAIN_A)
		data = get_dashboards_caps()["data"]
		self.assertEqual(data["creatable_scopes"], ["User"])
		self.assertEqual(data["manageable_roles"], [])
		self.assertEqual(data["max_sources"], 12)
		self.assertEqual(data["max_html_chars"], 1_000_000)
		self.assertEqual(data["max_rows"], 2000)
		self.assertIn(data["canvas_available"], (True, False))
		frappe.set_user(ADMIN_USER)
		data = get_dashboards_caps()["data"]
		self.assertEqual(data["creatable_scopes"], ["Org", "Role", "User"])
		self.assertIn(CUSTOM_ROLE, data["manageable_roles"])


class TestDashboardList(_DashboardsApiTestCase):
	def test_list_envelope_and_pagination(self):
		prefix = f"page-{frappe.generate_hash(length=6)}"
		for i in range(3):
			self._mk_static(PLAIN_A, title=f"{prefix} {i}")
		frappe.set_user(PLAIN_A)
		page = list_dashboards_page(search=prefix, page_length=2)["data"]
		self.assertEqual(page["total"], 3)
		self.assertEqual(len(page["rows"]), 2)
		self.assertTrue(page["has_more"])
		self.assertEqual(page["start"], 0)
		self.assertEqual(page["page_length"], 2)
		page = list_dashboards_page(search=prefix, start=2, page_length=2)["data"]
		self.assertEqual(len(page["rows"]), 1)
		self.assertFalse(page["has_more"])

	def test_search_by_title(self):
		needle = f"needle-{frappe.generate_hash(length=6)}"
		self._mk_static(PLAIN_A, title=f"{needle} sales")
		self._mk_static(PLAIN_A)
		frappe.set_user(PLAIN_A)
		page = list_dashboards_page(search=needle)["data"]
		self.assertEqual(page["total"], 1)
		self.assertEqual(page["rows"][0]["dashboard_title"], f"{needle} sales")
		self.assertIsInstance(page["rows"][0]["modified"], str)

	def test_filters_scope_type_owner(self):
		prefix = f"filt-{frappe.generate_hash(length=6)}"
		self._mk_static(PLAIN_A, title=f"{prefix} mine")
		self._mk_connected(
			ADMIN_USER,
			[{"source_name": "todos", "tool": "get_list", "spec": _GET_LIST_SPEC}],
			title=f"{prefix} connected",
		)
		frappe.set_user(ADMIN_USER)
		page = list_dashboards_page(
			search=prefix,
			filters=frappe.as_json({"scope": "User", "owner": PLAIN_A}),
		)["data"]
		self.assertEqual([r["dashboard_title"] for r in page["rows"]], [f"{prefix} mine"])
		page = list_dashboards_page(search=prefix, filters=frappe.as_json({"dashboard_type": "Connected"}))[
			"data"
		]
		self.assertEqual([r["dashboard_title"] for r in page["rows"]], [f"{prefix} connected"])
		self.assertRaises(
			frappe.ValidationError,
			list_dashboards_page,
			filters=frappe.as_json({"scope": "Bogus"}),
		)

	def test_unknown_filter_key_throws(self):
		frappe.set_user(PLAIN_A)
		self.assertRaises(
			frappe.ValidationError,
			list_dashboards_page,
			filters=frappe.as_json({"bogus": 1}),
		)

	def test_unknown_sort_field_throws(self):
		frappe.set_user(PLAIN_A)
		self.assertRaises(frappe.ValidationError, list_dashboards_page, sort_field="html")

	def test_list_visibility_excludes_others_private(self):
		prefix = f"vis-{frappe.generate_hash(length=6)}"
		self._mk_static(PLAIN_A, title=f"{prefix} private")
		frappe.set_user(PLAIN_B)
		page = list_dashboards_page(search=prefix)["data"]
		self.assertEqual(page["total"], 0)
		self.assertEqual(page["rows"], [])
		# The admin tier sees it (no visibility splice).
		frappe.set_user(ADMIN_USER)
		page = list_dashboards_page(search=prefix)["data"]
		self.assertEqual(page["total"], 1)


class TestDashboardDetail(_DashboardsApiTestCase):
	def test_get_dashboard_detail(self):
		created = self._mk_connected(
			PLAIN_A, [{"source_name": "todos", "tool": "get_list", "spec": _GET_LIST_SPEC}]
		)
		detail = get_dashboard(created["name"])["data"]
		self.assertEqual(detail["name"], created["name"])
		self.assertEqual(detail["dashboard_type"], "Connected")
		self.assertEqual(detail["scope"], "User")
		self.assertEqual(detail["target_user"], PLAIN_A)
		self.assertEqual(detail["owner"], PLAIN_A)
		self.assertEqual(detail["html"], "<h1>data</h1>")
		self.assertEqual(len(detail["sources"]), 1)
		self.assertEqual(detail["sources"][0]["source_name"], "todos")
		self.assertEqual(detail["sources"][0]["tool"], "get_list")
		self.assertEqual(frappe.parse_json(detail["sources"][0]["spec"]), _GET_LIST_SPEC)
		self.assertIsInstance(detail["modified"], str)
		self.assertTrue(detail["can_edit"])  # owner

	def test_get_dashboard_denied_and_missing(self):
		created = self._mk_static(PLAIN_A)
		frappe.set_user(PLAIN_B)
		self.assertRaises(frappe.PermissionError, get_dashboard, created["name"])
		frappe.clear_messages()
		self.assertRaises(frappe.DoesNotExistError, get_dashboard, "no-such-dash-zz")


class TestSaveDashboard(_DashboardsApiTestCase):
	def test_save_create_static_minimal(self):
		data = self._mk_static(PLAIN_A)
		self.assertEqual(data["dashboard_type"], "Static")
		self.assertEqual(data["scope"], "User")
		self.assertEqual(data["target_user"], PLAIN_A)  # auto-filled
		self.assertEqual(data["owner"], PLAIN_A)
		self.assertEqual(data["sources"], [])

	def test_save_connected_explicit_sources_roundtrip(self):
		data = self._mk_connected(
			PLAIN_A, [{"source_name": "todos", "tool": "get_list", "spec": _GET_LIST_SPEC}]
		)
		self.assertEqual(data["dashboard_type"], "Connected")
		self.assertEqual(len(data["sources"]), 1)
		self.assertIsInstance(data["sources"][0]["spec"], str)
		self.assertEqual(frappe.parse_json(data["sources"][0]["spec"]), _GET_LIST_SPEC)

	def test_save_parses_jarvis_sources_block_from_html(self):
		frappe.set_user(PLAIN_A)
		data = self._save(
			{
				"dashboard_title": f"block-{frappe.generate_hash(length=8)}",
				"html": _HTML_WITH_BLOCK,
			}
		)
		self.assertEqual(data["dashboard_type"], "Connected")
		self.assertEqual([s["source_name"] for s in data["sources"]], ["todos"])
		self.assertEqual(frappe.parse_json(data["sources"][0]["spec"]), _GET_LIST_SPEC)

	def test_save_normalizes_llm_source_dialect(self):
		"""LLM-authored blocks drift toward the tool-call surface: ``id`` for
		source_name, a ``jarvis__`` tool prefix, ``args``/``args.spec`` for
		spec. Save folds them into the canonical child-row shape."""
		frappe.set_user(PLAIN_A)
		html = (
			'<script type="application/json" id="jarvis-sources">'
			'{"sources": [{"id": "todos", "tool": "jarvis__get_list", "refresh": "view", '
			'"args": {"spec": {"doctype": "ToDo", "fields": ["name", "description"], "limit": 10}}}]}'
			"</script>"
		)
		data = self._save(
			{
				"dashboard_title": f"dialect-{frappe.generate_hash(length=8)}",
				"html": html,
			}
		)
		self.assertEqual(data["dashboard_type"], "Connected")
		self.assertEqual([s["source_name"] for s in data["sources"]], ["todos"])
		self.assertEqual(data["sources"][0]["tool"], "get_list")
		self.assertEqual(frappe.parse_json(data["sources"][0]["spec"]), _GET_LIST_SPEC)

	def test_unknown_payload_key_throws(self):
		frappe.set_user(PLAIN_A)
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json({"dashboard_title": "x", "html": "<p>x</p>", "owner": "evil"}),
		)

	def test_bad_tool_throws(self):
		frappe.set_user(PLAIN_A)
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json(
				{
					"dashboard_title": "x",
					"html": "<p>x</p>",
					"sources": [{"source_name": "s", "tool": "run_sql", "spec": {}}],
				}
			),
		)

	def test_non_json_spec_throws(self):
		frappe.set_user(PLAIN_A)
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json(
				{
					"dashboard_title": "x",
					"html": "<p>x</p>",
					"sources": [{"source_name": "s", "tool": "get_list", "spec": "{not json"}],
				}
			),
		)

	def test_bad_query_spec_invalid_op_throws(self):
		frappe.set_user(PLAIN_A)
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json(
				{
					"dashboard_title": "x",
					"html": "<p>x</p>",
					"sources": [
						{
							"source_name": "s",
							"tool": "query",
							"spec": {
								"from": "ToDo",
								"where": [{"field": "status", "op": "regexp", "value": "x"}],
							},
						}
					],
				}
			),
		)

	def test_get_list_unknown_doctype_throws(self):
		frappe.set_user(PLAIN_A)
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json(
				{
					"dashboard_title": "x",
					"html": "<p>x</p>",
					"sources": [
						{
							"source_name": "s",
							"tool": "get_list",
							"spec": {"doctype": "No Such Doctype Zzz"},
						}
					],
				}
			),
		)

	def test_run_report_unknown_report_throws(self):
		frappe.set_user(PLAIN_A)
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json(
				{
					"dashboard_title": "x",
					"html": "<p>x</p>",
					"sources": [
						{
							"source_name": "s",
							"tool": "run_report",
							"spec": {"report_name": "No Such Report Zzz"},
						}
					],
				}
			),
		)

	def test_get_list_bad_order_by_throws(self):
		"""order_by is passed straight to frappe.get_list; a non fieldname-token
		value (injection attempt) must be rejected at save."""
		frappe.set_user(PLAIN_A)
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json(
				{
					"dashboard_title": "x",
					"html": "<p>x</p>",
					"sources": [
						{
							"source_name": "s",
							"tool": "get_list",
							"spec": {"doctype": "ToDo", "order_by": "(select 1)"},
						}
					],
				}
			),
		)

	def test_get_list_good_order_by_saves(self):
		frappe.set_user(PLAIN_A)
		data = self._save(
			{
				"dashboard_title": f"ob-{frappe.generate_hash(length=8)}",
				"html": "<p>x</p>",
				"sources": [
					{
						"source_name": "s",
						"tool": "get_list",
						"spec": {"doctype": "ToDo", "order_by": "modified desc"},
					}
				],
			}
		)
		self.assertEqual(data["sources"][0]["source_name"], "s")

	def test_run_report_prepared_report_rejected(self):
		frappe.set_user(PLAIN_A)
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json(
				{
					"dashboard_title": "x",
					"html": "<p>x</p>",
					"sources": [
						{
							"source_name": "s",
							"tool": "run_report",
							"spec": {"report_name": PREPARED_REPORT_NAME},
						}
					],
				}
			),
		)

	def test_spec_over_32k_throws(self):
		frappe.set_user(PLAIN_A)
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json(
				{
					"dashboard_title": "x",
					"html": "<p>x</p>",
					"sources": [{"source_name": "s", "tool": "get_list", "spec": "x" * 33_000}],
				}
			),
		)

	def test_more_than_12_sources_throws(self):
		frappe.set_user(PLAIN_A)
		sources = [{"source_name": f"s{i}", "tool": "get_list", "spec": _GET_LIST_SPEC} for i in range(13)]
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json({"dashboard_title": "x", "html": "<p>x</p>", "sources": sources}),
		)

	def test_duplicate_source_name_throws(self):
		frappe.set_user(PLAIN_A)
		sources = [
			{"source_name": "dup", "tool": "get_list", "spec": _GET_LIST_SPEC},
			{"source_name": "dup", "tool": "get_list", "spec": _GET_LIST_SPEC},
		]
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json({"dashboard_title": "x", "html": "<p>x</p>", "sources": sources}),
		)

	def test_update_by_non_owner_denied(self):
		created = self._mk_static(PLAIN_A)
		frappe.set_user(PLAIN_B)
		self.assertRaises(
			frappe.PermissionError,
			save_dashboard,
			frappe.as_json({"name": created["name"], "html": "<h1>hijack</h1>"}),
		)

	def test_owner_update_mutates_html(self):
		created = self._mk_static(PLAIN_A)
		frappe.set_user(PLAIN_A)
		updated = save_dashboard(frappe.as_json({"name": created["name"], "html": "<h1>v2</h1>"}))["data"]
		self.assertEqual(updated["html"], "<h1>v2</h1>")
		self.assertEqual(updated["name"], created["name"])

	def test_delete_matrix(self):
		mine = self._mk_static(PLAIN_A)
		frappe.set_user(PLAIN_B)
		self.assertRaises(frappe.PermissionError, delete_dashboard, mine["name"])
		frappe.set_user(PLAIN_A)
		r = delete_dashboard(mine["name"])
		self.assertEqual(r["data"], {"deleted": mine["name"]})
		self.assertFalse(frappe.db.exists(DASHBOARD, mine["name"]))
		# A Jarvis Admin may delete someone else's.
		other = self._mk_static(PLAIN_A)
		frappe.set_user(ADMIN_USER)
		self.assertTrue(delete_dashboard(other["name"])["ok"])
		self.assertFalse(frappe.db.exists(DASHBOARD, other["name"]))


class TestRunDashboardSource(_DashboardsApiTestCase):
	def test_get_list_happy_path_envelope(self):
		self._mk_todo(PLAIN_A)
		created = self._mk_connected(
			PLAIN_A, [{"source_name": "todos", "tool": "get_list", "spec": _GET_LIST_SPEC}]
		)
		r = run_dashboard_source(created["name"], "todos")
		self.assertTrue(r["ok"])
		data = r["data"]
		self.assertEqual(data["source_name"], "todos")
		self.assertEqual(data["tool"], "get_list")
		self.assertIsInstance(data["rows"], list)
		self.assertTrue(data["rows"])
		self.assertFalse(data["truncated"])
		self.assertIsInstance(data["took_ms"], int)
		self.assertNotIn("columns", data)

	def test_unknown_source_clean_error(self):
		created = self._mk_connected(
			PLAIN_A, [{"source_name": "todos", "tool": "get_list", "spec": _GET_LIST_SPEC}]
		)
		r = run_dashboard_source(created["name"], "nope")
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["code"], "InvalidArgumentError")
		self.assertIn("nope", r["error"]["message"])

	def test_dashboard_read_denied_error(self):
		created = self._mk_connected(
			PLAIN_A, [{"source_name": "todos", "tool": "get_list", "spec": _GET_LIST_SPEC}]
		)
		frappe.set_user(PLAIN_B)
		frappe.clear_messages()
		r = run_dashboard_source(created["name"], "todos")
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["code"], "PermissionError")
		self.assertFalse(getattr(frappe.local, "message_log", None))

	def test_in_tool_permission_error_no_leak(self):
		# Server Script is SM-only: the SAVE succeeds (the spec only checks the
		# doctype exists) but the RUN, as the viewing user, must deny cleanly.
		created = self._mk_connected(
			PLAIN_A,
			[
				{
					"source_name": "scripts",
					"tool": "get_list",
					"spec": {"doctype": "Server Script", "limit": 5},
				}
			],
		)
		frappe.set_user(PLAIN_A)
		frappe.clear_messages()
		r = run_dashboard_source(created["name"], "scripts")
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["code"], "PermissionError")
		self.assertFalse(getattr(frappe.local, "message_log", None))

	def test_run_report_happy_path_normalized(self):
		self._mk_todo("Administrator")
		frappe.set_user("Administrator")
		created = self._save(
			{
				"dashboard_title": f"rep-{frappe.generate_hash(length=8)}",
				"html": "<h1>report</h1>",
				"sources": [
					{
						"source_name": "rep",
						"tool": "run_report",
						"spec": {"report_name": REPORT_NAME},
					}
				],
			}
		)
		r = run_dashboard_source(created["name"], "rep")
		self.assertTrue(r["ok"])
		data = r["data"]
		self.assertEqual(data["tool"], "run_report")
		self.assertIn("columns", data)
		self.assertIsInstance(data["columns"], list)
		self.assertIsInstance(data["rows"], list)
		self.assertTrue(data["rows"])
		self.assertIsInstance(data["took_ms"], int)

	def test_truncation_cap(self):
		self._mk_todo("Administrator", "truncate one")
		self._mk_todo("Administrator", "truncate two")
		frappe.set_user("Administrator")
		created = self._save(
			{
				"dashboard_title": f"cap-{frappe.generate_hash(length=8)}",
				"html": "<h1>cap</h1>",
				"sources": [
					{
						"source_name": "todos",
						"tool": "get_list",
						"spec": {"doctype": "ToDo", "fields": ["name"], "limit": 100},
					}
				],
			}
		)
		with patch("jarvis.chat.dashboards_api.DASHBOARD_MAX_ROWS", 1):
			r = run_dashboard_source(created["name"], "todos")
		self.assertTrue(r["ok"])
		self.assertEqual(len(r["data"]["rows"]), 1)
		self.assertTrue(r["data"]["truncated"])

	def test_response_never_contains_sql(self):
		# Administrator: ToDo's core permission_query_conditions hook writes a
		# literal `tabToDo`.allocated_to predicate that breaks under the query
		# tool's FROM alias for non-admin users (a pre-existing query-tool
		# limitation, surfaced as an InternalError envelope) — the sql-leak
		# assertion needs a user whose run actually succeeds.
		self._mk_todo("Administrator")
		created = self._mk_connected(
			"Administrator",
			[
				{
					"source_name": "q",
					"tool": "query",
					"spec": {"from": "ToDo", "select": ["name"], "limit": 5},
				}
			],
		)
		r = run_dashboard_source(created["name"], "q")
		self.assertTrue(r["ok"])
		self.assertNotIn("sql", r["data"])
		self.assertNotIn('"sql"', frappe.as_json(r))


class TestWorkspaceSearchAndContext(_DashboardsApiTestCase):
	def test_search_workspace_dashboard_hit(self):
		marker = f"zzdashfind{frappe.generate_hash(length=6)}"
		mine = self._mk_static(PLAIN_A, title=f"{marker} alpha")
		theirs = self._mk_static(PLAIN_B, title=f"{marker} secret")
		frappe.set_user(PLAIN_A)
		groups = search_workspace(search=marker)["groups"]
		dash = next((g for g in groups if g["key"] == "dashboards"), None)
		self.assertIsNotNone(dash)
		self.assertEqual(dash["title"], "Dashboards")
		names = [i["name"] for i in dash["items"]]
		self.assertIn(f"dashboard::{mine['name']}", names)
		self.assertNotIn(f"dashboard::{theirs['name']}", names)
		item = next(i for i in dash["items"] if i["name"] == f"dashboard::{mine['name']}")
		self.assertEqual(item["suffix"], "Dashboard")
		self.assertEqual(item["spa_route"], f"/dashboards/{mine['name']}")
		self.assertNotIn("route", item)

	def test_send_message_context_dashboards_allowlist(self):
		frappe.set_user(PLAIN_A)
		conv = frappe.get_doc(
			{
				"doctype": "Jarvis Conversation",
				"title": "dash ctx test",
			}
		).insert(ignore_permissions=True)
		self._convs.append(conv.name)
		with (
			patch("jarvis.chat.api.validate_can_send", return_value=(True, "")),
			patch("jarvis.chat.api._dispatch_turn") as disp,
			# This asserts the context/dashboards allow-list forwarding at the
			# ``_dispatch_turn`` boundary. Since the Relay Pump's default-ON inversion,
			# ``send_message`` routes an absent-flag managed bench (patterntest AND a
			# fresh CI site) through ``accept_or_queue``'s pump branch, which leaves the
			# turn queued and NEVER invokes the ``_dispatch_turn`` dispatch callback — so
			# the mock stayed uncalled and ``call_args`` was None. Pin the pure-legacy
			# route (``turn_machine_enabled()`` -> False) so ``send_message`` calls
			# ``_dispatch_turn`` directly and the forwarding contract is exercised.
			patch("jarvis.chat.admission.turn_machine_enabled", return_value=False),
		):
			r = send_message(
				conv.name,
				"build me a dashboard",
				context=frappe.as_json({"page": "dashboards"}),
			)
			self.assertTrue(r["ok"])
			kwargs = disp.call_args[0][0]
			self.assertEqual(kwargs["context"]["page"], "dashboards")
			# A page value outside the allow-list forwards NO context at all.
			r2 = send_message(
				conv.name,
				"hello again",
				context=frappe.as_json({"page": "evil"}),
			)
			self.assertTrue(r2["ok"])
			self.assertNotIn("context", disp.call_args[0][0])
			# Explicit data-mode toggle: the two literal values forward; junk
			# does not.
			r3 = send_message(
				conv.name,
				"make it live",
				context=frappe.as_json({"page": "dashboards", "data_mode": "live"}),
			)
			self.assertTrue(r3["ok"])
			self.assertEqual(disp.call_args[0][0]["context"]["data_mode"], "live")
			r4 = send_message(
				conv.name,
				"make it weird",
				context=frappe.as_json({"page": "dashboards", "data_mode": "weird"}),
			)
			self.assertTrue(r4["ok"])
			self.assertNotIn("data_mode", disp.call_args[0][0]["context"])

	def test_save_unwraps_double_nested_spec(self):
		"""The LLM sometimes wraps the tool's kwargs shape into spec
		({"spec": {"spec": {...}}}); save folds it to the bare argument value."""
		frappe.set_user(PLAIN_A)
		data = self._save(
			{
				"dashboard_title": f"nest-{frappe.generate_hash(length=8)}",
				"html": "<p>x</p>",
				"sources": [
					{
						"source_name": "todos",
						"tool": "get_list",
						"spec": {"spec": dict(_GET_LIST_SPEC)},
					}
				],
			}
		)
		self.assertEqual(frappe.parse_json(data["sources"][0]["spec"]), _GET_LIST_SPEC)

	def test_theme_roundtrip_and_validation(self):
		frappe.set_user(PLAIN_A)
		data = self._save(
			{
				"dashboard_title": f"theme-{frappe.generate_hash(length=8)}",
				"html": "<p>x</p>",
				"theme": "Claude",
			}
		)
		self.assertEqual(data["theme"], "Claude")
		# omitted -> the Jarvis design-language default
		data2 = self._save(
			{
				"dashboard_title": f"theme-{frappe.generate_hash(length=8)}",
				"html": "<p>x</p>",
			}
		)
		self.assertEqual(data2["theme"], "Jarvis")
		# invalid value throws
		self.assertRaises(
			frappe.ValidationError,
			save_dashboard,
			frappe.as_json(
				{
					"dashboard_title": "x",
					"html": "<p>x</p>",
					"theme": "Neon",
				}
			),
		)
