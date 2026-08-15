"""Tests for jarvis.chat.triggers_api - the SPA-facing Triggers endpoints.

Two fixture users: an ADMIN (System Manager) who may manage triggers and a
PLAIN jarvis user (only the ``Jarvis User`` role) who is read-only. Hermetic:
every trigger / activity / ToDo row created here is tracked and deleted in
tearDown; no LLM/network calls anywhere on these paths.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.triggers_api import (
	activity_stats,
	create_trigger,
	delete_trigger,
	delete_triggers_bulk,
	get_trigger,
	get_triggers_caps,
	list_activity_page,
	list_triggers_page,
	set_trigger_enabled,
	test_trigger_condition,
	update_trigger,
)
from jarvis.permissions import JARVIS_USER_ROLE, ensure_jarvis_user_role
from jarvis.triggers import engine

TRIGGER = "Jarvis Trigger"
ACTIVITY = "Jarvis Trigger Activity"

ADMIN_USER = "jarvis-trigger-admin@example.com"
PLAIN_USER = "jarvis-trigger-user@example.com"


def _ensure_user(email: str, roles: list[str]) -> None:
	"""Create the fixture user if missing; idempotent."""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Trigger",
				"last_name": "Test",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	doc = frappe.get_doc("User", email)
	doc.add_roles(*roles)
	frappe.db.commit()


class _TriggersApiTestCase(FrappeTestCase):
	def setUp(self):
		ensure_jarvis_user_role()
		_ensure_user(ADMIN_USER, ["System Manager"])
		_ensure_user(PLAIN_USER, [JARVIS_USER_ROLE])
		self._orig_user = frappe.session.user
		self._triggers: list[str] = []
		self._activities: list[str] = []
		self._todos: list[str] = []
		engine.clear_cache()

	def tearDown(self):
		frappe.set_user(self._orig_user)
		for name in self._triggers:
			frappe.db.delete(ACTIVITY, {"trigger": name})
			if frappe.db.exists(TRIGGER, name):
				frappe.delete_doc(TRIGGER, name, ignore_permissions=True, force=True)
		for name in self._activities:
			if frappe.db.exists(ACTIVITY, name):
				frappe.delete_doc(ACTIVITY, name, ignore_permissions=True, force=True)
		for name in self._todos:
			if frappe.db.exists("ToDo", name):
				frappe.delete_doc("ToDo", name, ignore_permissions=True, force=True)
		engine.clear_cache()
		frappe.db.commit()

	def _llm_payload(self, **overrides) -> dict:
		fields = {
			"trigger_name": f"api-{frappe.generate_hash(length=8)}",
			"target_doctype": "ToDo",
			"doc_event": "on_update",
			"action_type": "LLM",
			"llm_instruction": "Check this todo.",
		}
		fields.update(overrides)
		return fields

	def _create_as_admin(self, **overrides) -> str:
		frappe.set_user(ADMIN_USER)
		r = create_trigger(frappe.as_json(self._llm_payload(**overrides)))
		name = r["data"]["name"]
		self._triggers.append(name)
		return name

	def _insert_activity_row(self, **fields) -> str:
		"""Server-side activity row (the engine writes these with
		ignore_permissions; tests do the same)."""
		row = {
			"doctype": ACTIVITY,
			"trigger": fields.pop("trigger", f"vis-{frappe.generate_hash(length=8)}"),
			"trigger_label": "visibility test",
			"target_doctype": "ToDo",
			"target_docname": "",
			"doc_event": "on_update",
			"action_type": "LLM",
			"status": "Success",
			"summary": "s",
		}
		row.update(fields)
		doc = frappe.get_doc(row)
		doc.insert(ignore_permissions=True)
		self._activities.append(doc.name)
		return doc.name


class TestCaps(_TriggersApiTestCase):
	def test_caps_shape_and_manage_flag(self):
		frappe.set_user(PLAIN_USER)
		r = get_triggers_caps()
		self.assertTrue(r["ok"])
		data = r["data"]
		self.assertFalse(data["can_manage"])
		self.assertIn("scripts_enabled", data)
		self.assertIn("stt_enabled", data)
		self.assertEqual(len(data["events"]), 8)
		self.assertEqual(data["events"][0], {"value": "validate", "label": "Before Save (blockable)"})
		self.assertEqual(len(data["llm_events"]), 6)
		self.assertNotIn("validate", data["llm_events"])
		self.assertNotIn("before_submit", data["llm_events"])
		frappe.set_user(ADMIN_USER)
		self.assertTrue(get_triggers_caps()["data"]["can_manage"])


class TestCrudGating(_TriggersApiTestCase):
	def test_admin_crud_roundtrip(self):
		name = self._create_as_admin(description="round trip")
		detail = get_trigger(name)["data"]
		self.assertEqual(detail["description"], "round trip")
		updated = update_trigger(name, frappe.as_json({"description": "edited"}))
		self.assertEqual(updated["data"]["description"], "edited")
		toggled = set_trigger_enabled(name, 0)
		self.assertEqual(toggled["data"]["enabled"], 0)
		r = delete_trigger(name)
		self.assertTrue(r["ok"])
		self.assertFalse(frappe.db.exists(TRIGGER, name))

	def test_plain_user_is_read_only(self):
		name = self._create_as_admin()
		frappe.set_user(PLAIN_USER)
		# read surfaces work
		page = list_triggers_page()
		self.assertIn(name, [r["name"] for r in page["data"]["rows"]])
		self.assertEqual(get_trigger(name)["data"]["name"], name)
		# manage surfaces throw
		payload = frappe.as_json(self._llm_payload())
		self.assertRaises(frappe.PermissionError, create_trigger, payload)
		self.assertRaises(frappe.PermissionError, update_trigger, name, frappe.as_json({"description": "x"}))
		self.assertRaises(frappe.PermissionError, set_trigger_enabled, name, 0)
		self.assertRaises(frappe.PermissionError, delete_trigger, name)
		self.assertRaises(frappe.PermissionError, delete_triggers_bulk, frappe.as_json([name]))
		self.assertTrue(frappe.db.exists(TRIGGER, name))

	def test_plain_user_get_trigger_redacts_logic_fields(self):
		# Review P2 (security): condition/script_body/llm_instruction carry the
		# trigger's internal logic; a plain Jarvis User (org-wide READ) must not
		# see them. Uses an LLM trigger so the test never depends on
		# server_script_enabled (off on the CI test site).
		name = self._create_as_admin(
			condition='doc.status == "Open"',
			llm_instruction="Secret reviewer brief: flag anything unusual.",
		)
		frappe.set_user(ADMIN_USER)
		admin_view = get_trigger(name)["data"]
		self.assertEqual(admin_view["condition"], 'doc.status == "Open"')
		self.assertEqual(admin_view["llm_instruction"], "Secret reviewer brief: flag anything unusual.")
		self.assertTrue(admin_view["can_manage"])
		frappe.set_user(PLAIN_USER)
		user_view = get_trigger(name)["data"]
		self.assertIsNone(user_view["condition"])
		self.assertIsNone(user_view["llm_instruction"])
		self.assertIsNone(user_view["script_body"])
		self.assertFalse(user_view["can_manage"])
		# non-logic metadata is still visible
		self.assertEqual(user_view["target_doctype"], "ToDo")

	def test_payload_field_whitelist(self):
		frappe.set_user(ADMIN_USER)
		payload = self._llm_payload()
		payload["server_script"] = "smuggled"  # server-owned field
		self.assertRaises(frappe.ValidationError, create_trigger, frappe.as_json(payload))

	def test_bulk_delete_caps_at_50(self):
		frappe.set_user(ADMIN_USER)
		names = [f"fake-{i}" for i in range(51)]
		self.assertRaises(frappe.ValidationError, delete_triggers_bulk, frappe.as_json(names))

	def test_bulk_delete_reports_skipped(self):
		name = self._create_as_admin()
		r = delete_triggers_bulk(frappe.as_json([name, "no-such-trigger-zz"]))
		self.assertEqual(r["data"]["deleted"], 1)
		self.assertEqual(r["data"]["skipped"], [{"name": "no-such-trigger-zz", "reason": "not found"}])


class TestTriggerList(_TriggersApiTestCase):
	def test_search_filter_sort(self):
		needle = f"needle-{frappe.generate_hash(length=6)}"
		self._create_as_admin(trigger_name=needle)
		self._create_as_admin()
		frappe.set_user(PLAIN_USER)
		page = list_triggers_page(search=needle)["data"]
		self.assertEqual(page["total"], 1)
		self.assertEqual(page["rows"][0]["trigger_name"], needle)
		self.assertIn("last_activity_at", page["rows"][0])
		self.assertIn("activity_24h", page["rows"][0])
		page = list_triggers_page(
			filters=frappe.as_json({"action_type": "LLM"}), sort_field="trigger_name", sort_dir="asc"
		)["data"]
		self.assertGreaterEqual(page["total"], 2)

	def test_unknown_filter_throws(self):
		frappe.set_user(PLAIN_USER)
		self.assertRaises(frappe.ValidationError, list_triggers_page, filters=frappe.as_json({"bogus": 1}))

	def test_invalid_filter_value_throws(self):
		frappe.set_user(PLAIN_USER)
		self.assertRaises(
			frappe.ValidationError,
			list_triggers_page,
			filters=frappe.as_json({"action_type": "Bogus"}),
		)

	def test_unknown_sort_throws(self):
		frappe.set_user(PLAIN_USER)
		self.assertRaises(frappe.ValidationError, list_triggers_page, sort_field="owner")

	def test_pagination_clamps(self):
		frappe.set_user(PLAIN_USER)
		page = list_triggers_page(start=-5, page_length=500)["data"]
		self.assertEqual(page["start"], 0)
		self.assertEqual(page["page_length"], 100)
		# falsy page_length falls back to the shared _clamp_page default (20),
		# matching the Macros list semantics.
		page = list_triggers_page(page_length=0)["data"]
		self.assertEqual(page["page_length"], 20)


class TestConditionTester(_TriggersApiTestCase):
	def test_happy_path(self):
		frappe.set_user(PLAIN_USER)
		r = test_trigger_condition("ToDo", 'doc.status == "Open"')
		self.assertEqual(r["data"], {"valid": True})

	def test_invalid_expression_is_a_payload_not_a_500(self):
		frappe.set_user(PLAIN_USER)
		r = test_trigger_condition("ToDo", "doc.status ==")
		self.assertFalse(r["data"]["valid"])
		self.assertTrue(r["data"]["error"])

	def test_unknown_doctype_throws(self):
		frappe.set_user(PLAIN_USER)
		self.assertRaises(frappe.ValidationError, test_trigger_condition, "No Such Doctype Zzz", "True")

	def test_would_fire_against_named_doc(self):
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "cond test",
				"allocated_to": PLAIN_USER,
				"status": "Open",
			}
		).insert(ignore_permissions=True)
		self._todos.append(todo.name)
		frappe.set_user(PLAIN_USER)
		r = test_trigger_condition("ToDo", 'doc.status == "Open"', docname=todo.name)
		self.assertTrue(r["data"]["valid"])
		self.assertTrue(r["data"]["would_fire"])
		r = test_trigger_condition("ToDo", 'doc.status == "Closed"', docname=todo.name)
		self.assertFalse(r["data"]["would_fire"])

	def test_named_doc_requires_read_permission(self):
		# A ToDo belonging to someone else: plain users only read ToDos they
		# own or are assigned (frappe.desk.doctype.todo has_permission).
		foreign = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "foreign",
				"allocated_to": ADMIN_USER,
			}
		).insert(ignore_permissions=True)
		self._todos.append(foreign.name)
		frappe.set_user(PLAIN_USER)
		self.assertRaises(
			frappe.PermissionError,
			test_trigger_condition,
			"ToDo",
			"True",
			foreign.name,
		)


class TestActivityFeed(_TriggersApiTestCase):
	def _seed_visibility_rows(self) -> tuple[str, str, str]:
		"""One row the plain user can see (their own ToDo) and one they cannot
		(a Server Script target they lack read on / that does not exist)."""
		marker = f"vis-{frappe.generate_hash(length=8)}"
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "visible target",
				"allocated_to": PLAIN_USER,
			}
		).insert(ignore_permissions=True)
		self._todos.append(todo.name)
		visible = self._insert_activity_row(trigger=marker, target_doctype="ToDo", target_docname=todo.name)
		hidden = self._insert_activity_row(
			trigger=marker,
			target_doctype="Server Script",
			target_docname="no-such-script-zz",
			status="Failed",
		)
		return marker, visible, hidden

	def test_admin_sees_all_rows_with_exact_total(self):
		marker, visible, hidden = self._seed_visibility_rows()
		frappe.set_user(ADMIN_USER)
		page = list_activity_page(filters=frappe.as_json({"trigger": marker}))["data"]
		self.assertEqual(page["total"], 2)
		self.assertFalse(page["approximate"])
		self.assertEqual({r["name"] for r in page["rows"]}, {visible, hidden})

	def test_plain_user_sees_only_readable_targets(self):
		marker, visible, hidden = self._seed_visibility_rows()
		frappe.set_user(PLAIN_USER)
		page = list_activity_page(filters=frappe.as_json({"trigger": marker}))["data"]
		self.assertTrue(page["approximate"])
		names = {r["name"] for r in page["rows"]}
		self.assertIn(visible, names)
		self.assertNotIn(hidden, names)

	def test_status_filter_and_search(self):
		marker, visible, hidden = self._seed_visibility_rows()
		frappe.set_user(ADMIN_USER)
		page = list_activity_page(
			search="visibility test", filters=frappe.as_json({"trigger": marker, "status": "Failed"})
		)["data"]
		self.assertEqual([r["name"] for r in page["rows"]], [hidden])

	def test_unknown_filter_and_sort_throw(self):
		frappe.set_user(PLAIN_USER)
		self.assertRaises(frappe.ValidationError, list_activity_page, filters=frappe.as_json({"bogus": 1}))
		self.assertRaises(frappe.ValidationError, list_activity_page, sort_field="summary")
		self.assertRaises(
			frappe.ValidationError, list_activity_page, filters=frappe.as_json({"status": "Nope"})
		)
		self.assertRaises(
			frappe.ValidationError,
			list_activity_page,
			filters=frappe.as_json({"from_date": "not-a-date"}),
		)

	def test_stats_admin_only(self):
		self._seed_visibility_rows()
		frappe.set_user(PLAIN_USER)
		self.assertEqual(activity_stats()["data"], {})
		frappe.set_user(ADMIN_USER)
		data = activity_stats()["data"]
		self.assertIn("last_24h", data)
		self.assertIn("total_rows", data)
		self.assertGreaterEqual(data["last_24h"]["Success"], 1)
		self.assertGreaterEqual(data["last_24h"]["Failed"], 1)
