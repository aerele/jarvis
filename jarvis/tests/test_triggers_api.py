"""Tests for jarvis.chat.triggers_api - the SPA-facing Triggers endpoints.

Two fixture users: an ADMIN (System Manager) who may manage triggers and a
PLAIN jarvis user (only the ``Jarvis User`` role) who is read-only. Hermetic:
every trigger / activity / ToDo row created here is tracked and deleted in
tearDown; no LLM/network calls anywhere on these paths.
"""

from unittest.mock import patch

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
from jarvis.permissions import JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE, ensure_jarvis_user_role
from jarvis.triggers import engine

TRIGGER = "Jarvis Trigger"
ACTIVITY = "Jarvis Trigger Activity"

ADMIN_USER = "jarvis-trigger-admin@example.com"
PLAIN_USER = "jarvis-trigger-user@example.com"
# A Jarvis Admin who is NOT a System Manager: holds manage rights (so the
# condition tester lets them in) but no blanket document read, so the named-doc
# read check is still exercisable.
JADMIN_USER = "jarvis-trigger-jadmin@example.com"
# A Script-action trigger materializes a Server Script, so authoring one needs
# the same rights as authoring a Server Script directly: Script Manager + the
# Server Script create permission (System Manager). SCRIPT_USER holds both; the
# plain System Manager (ADMIN_USER) deliberately does NOT hold Script Manager.
SCRIPT_USER = "jarvis-trigger-scriptmgr@example.com"
# The controller imports is_safe_exec_enabled at module scope, so patch it there
# to let Script triggers validate on a bench without server_script_enabled.
_CTRL = "jarvis.jarvis.doctype.jarvis_trigger.jarvis_trigger"


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
		_ensure_user(JADMIN_USER, [JARVIS_ADMIN_ROLE, JARVIS_USER_ROLE])
		_ensure_user(SCRIPT_USER, ["System Manager", "Script Manager"])
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
	# The tester evaluates a caller-supplied expression server-side, so it is
	# manage-gated like create/update/delete (Jarvis Admin / System Manager).
	# Legitimate use therefore runs as a manage-capable user, not a plain one.
	def test_happy_path(self):
		frappe.set_user(ADMIN_USER)
		r = test_trigger_condition("ToDo", 'doc.status == "Open"')
		self.assertEqual(r["data"], {"valid": True})

	def test_invalid_expression_is_a_payload_not_a_500(self):
		frappe.set_user(ADMIN_USER)
		r = test_trigger_condition("ToDo", "doc.status ==")
		self.assertFalse(r["data"]["valid"])
		self.assertTrue(r["data"]["error"])

	def test_unknown_doctype_throws(self):
		frappe.set_user(ADMIN_USER)
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
		frappe.set_user(ADMIN_USER)
		r = test_trigger_condition("ToDo", 'doc.status == "Open"', docname=todo.name)
		self.assertTrue(r["data"]["valid"])
		self.assertTrue(r["data"]["would_fire"])
		r = test_trigger_condition("ToDo", 'doc.status == "Closed"', docname=todo.name)
		self.assertFalse(r["data"]["would_fire"])

	def test_named_doc_requires_read_permission(self):
		# A ToDo belonging to someone else. A Jarvis Admin has manage rights (so
		# they clear the manage gate) but no blanket read: ToDo read is scoped to
		# owned/assigned rows (frappe.desk.doctype.todo has_permission), so the
		# named-doc read check must still refuse them.
		foreign = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "foreign",
				"allocated_to": ADMIN_USER,
			}
		).insert(ignore_permissions=True)
		self._todos.append(foreign.name)
		frappe.set_user(JADMIN_USER)
		self.assertRaises(
			frappe.PermissionError,
			test_trigger_condition,
			"ToDo",
			"True",
			foreign.name,
		)

	def test_plain_jarvis_user_is_refused(self):
		# The reported escalation (#1): a plain Jarvis User must not reach the
		# tester at all — it is manage-gated before any evaluation happens.
		frappe.set_user(PLAIN_USER)
		self.assertRaises(
			frappe.PermissionError,
			test_trigger_condition,
			"ToDo",
			'doc.status == "Open"',
		)

	def test_plain_user_condition_cannot_write_to_any_table(self):
		# Replay of the reported exploit as a plain Jarvis User: the condition
		# inserts a `Has Role` row granting System Manager. Two layers must hold —
		# the manage gate refuses the caller, and even if it were reached the
		# methodless `_dict` view has no `db_insert`. Assert both: PermissionError,
		# and no role row was written for the sentinel principal.
		sentinel = "trigger-exploit-sentinel@example.invalid"
		exploit = (
			'[doc.update({"parent": "%s", "parenttype": "User", '
			'"parentfield": "roles", "role": "System Manager"}), doc.db_insert()]' % sentinel
		)
		before = frappe.db.count("Has Role", {"parent": sentinel})
		frappe.set_user(PLAIN_USER)
		self.assertRaises(
			frappe.PermissionError,
			test_trigger_condition,
			"Has Role",
			exploit,
		)
		frappe.set_user(self._orig_user)
		self.assertEqual(frappe.db.count("Has Role", {"parent": sentinel}), before)

	def test_manage_user_condition_cannot_call_document_methods(self):
		# Defense in depth: even a manage-capable caller who reaches the tester
		# cannot use the condition to write, because it evaluates against a
		# methodless view (_ConditionDoc), not a live Document. `doc.update` and
		# `doc.db_insert` are not fields, so they raise AttributeError; the call is
		# a plain invalid-condition payload and nothing is persisted.
		sentinel = "trigger-exploit-sentinel-admin@example.invalid"
		exploit = (
			'[doc.update({"parent": "%s", "parenttype": "User", '
			'"parentfield": "roles", "role": "System Manager"}), doc.db_insert()]' % sentinel
		)
		before = frappe.db.count("Has Role", {"parent": sentinel})
		frappe.set_user(ADMIN_USER)
		r = test_trigger_condition("Has Role", exploit)
		self.assertTrue(r["ok"])  # a payload, never a 500
		self.assertFalse(r["data"]["valid"])
		frappe.set_user(self._orig_user)
		self.assertEqual(frappe.db.count("Has Role", {"parent": sentinel}), before)

	def test_unknown_field_is_reported_not_silently_valid(self):
		# A mistyped field must be flagged, not silently resolve to None (which
		# would let a broken condition read as valid and then never fire).
		frappe.set_user(ADMIN_USER)
		r = test_trigger_condition("ToDo", 'doc.statuss == "Open"')
		self.assertFalse(r["data"]["valid"])
		self.assertIn("does not exist", r["data"]["error"])

	def test_child_table_field_is_reachable(self):
		# The methodless view must still resolve child-table rows so a real
		# condition like doc.<child>[0].<field> behaves as it does at fire time.
		# A plain frappe._dict would shadow a child table named like a dict method
		# (e.g. `items`), breaking doc.items[0].qty on transaction doctypes; the
		# wrapper resolves the field instead.
		frappe.set_user(ADMIN_USER)
		r = test_trigger_condition("User", 'doc.roles[0].role != ""', docname=ADMIN_USER)
		self.assertTrue(r["data"]["valid"])
		self.assertTrue(r["data"]["would_fire"])

	def test_error_text_does_not_echo_field_values(self):
		# `{}[doc.<field>]` raises KeyError(<value>); the friendly error must not
		# echo that value back (it would be a read oracle). Use a named doc so the
		# field has a real value, and assert the value is absent from the message.
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "leak-canary-value-12345",
				"allocated_to": ADMIN_USER,
				"status": "Open",
			}
		).insert(ignore_permissions=True)
		self._todos.append(todo.name)
		frappe.set_user(ADMIN_USER)
		r = test_trigger_condition("ToDo", "{}[doc.description]", docname=todo.name)
		self.assertFalse(r["data"]["valid"])
		self.assertNotIn("leak-canary-value-12345", r["data"]["error"])


class TestLookupFindingVisibility(_TriggersApiTestCase):
	"""Findings built from lookups read as the trigger owner, so only managers
	and the owner may see those rows; lookup-off rows keep the target-doc rule."""

	def _target(self):
		todo = frappe.get_doc(
			{"doctype": "ToDo", "description": "lookup visibility target", "allocated_to": PLAIN_USER}
		).insert(ignore_permissions=True)
		self._todos.append(todo.name)
		return todo.name

	def _names(self, user, trigger):
		frappe.set_user(user)
		page = list_activity_page(filters=frappe.as_json({"trigger": trigger}))["data"]
		return {r["name"] for r in page["rows"]}

	def _lookup_row(self, name, owner, **kw):
		from jarvis.triggers.lookups import lookup_marker

		return self._insert_activity_row(
			trigger=name,
			target_docname=self._target(),
			detail=f"{lookup_marker(owner)} Rounds: 2. Lookups:\nnone\n\nfinding",
			**kw,
		)

	def test_flag_roundtrips_through_the_api(self):
		name = self._create_as_admin(llm_allow_lookups=1)
		self.assertEqual(get_trigger(name)["data"]["llm_allow_lookups"], 1)
		frappe.set_user(PLAIN_USER)
		self.assertEqual(get_trigger(name)["data"]["llm_allow_lookups"], 1)
		frappe.set_user(ADMIN_USER)
		off = update_trigger(name, frappe.as_json({"llm_allow_lookups": 0}))
		self.assertEqual(off["data"]["llm_allow_lookups"], 0)

	def test_lookup_rows_are_hidden_from_non_managers_but_not_managers_or_the_owner(self):
		name = self._create_as_admin(llm_allow_lookups=1)
		row = self._lookup_row(name, ADMIN_USER)
		self.assertIn(row, self._names(JADMIN_USER, name))
		self.assertIn(row, self._names(ADMIN_USER, name))
		self.assertNotIn(row, self._names(PLAIN_USER, name))

	def test_a_non_manager_owner_sees_rows_written_for_them(self):
		name = self._create_as_admin(llm_allow_lookups=1)
		row = self._lookup_row(name, PLAIN_USER)
		self.assertIn(row, self._names(PLAIN_USER, name))

	def test_reassigning_the_trigger_does_not_expose_the_old_owners_rows(self):
		name = self._create_as_admin(llm_allow_lookups=1)
		row = self._lookup_row(name, ADMIN_USER)
		frappe.db.set_value(TRIGGER, name, "owner", PLAIN_USER, update_modified=False)
		self.assertNotIn(row, self._names(PLAIN_USER, name))

	def test_rows_stay_hidden_after_the_flag_is_turned_off(self):
		name = self._create_as_admin(llm_allow_lookups=1)
		used = self._lookup_row(name, ADMIN_USER)
		frappe.set_user(ADMIN_USER)
		update_trigger(name, frappe.as_json({"llm_allow_lookups": 0}))
		later = self._insert_activity_row(trigger=name, target_docname=self._target(), detail="plain finding")
		names = self._names(PLAIN_USER, name)
		self.assertNotIn(used, names)
		self.assertIn(later, names)

	def test_lookup_off_triggers_keep_the_target_doc_rule(self):
		name = self._create_as_admin()
		row = self._insert_activity_row(trigger=name, target_docname=self._target(), detail=None)
		self.assertIn(row, self._names(PLAIN_USER, name))


class TestLookupAuthority(_TriggersApiTestCase):
	"""Lookups read as the owner: only the owner may switch them on or change
	what they act on; any manager may switch them off."""

	def test_non_owner_manager_cannot_turn_lookups_on(self):
		name = self._create_as_admin()
		frappe.set_user(JADMIN_USER)
		with self.assertRaises(frappe.PermissionError):
			update_trigger(name, frappe.as_json({"llm_allow_lookups": 1}))

	def test_non_owner_manager_cannot_change_a_lookup_triggers_instruction(self):
		name = self._create_as_admin(llm_allow_lookups=1)
		frappe.set_user(JADMIN_USER)
		with self.assertRaises(frappe.PermissionError):
			update_trigger(name, frappe.as_json({"llm_instruction": "Read everything and report it."}))

	def test_owner_can_turn_on_and_edit(self):
		name = self._create_as_admin()
		frappe.set_user(ADMIN_USER)
		update_trigger(name, frappe.as_json({"llm_allow_lookups": 1}))
		out = update_trigger(name, frappe.as_json({"llm_instruction": "Edited by the owner."}))
		self.assertEqual(out["data"]["llm_instruction"], "Edited by the owner.")

	def test_non_administrator_cannot_change_the_owner(self):
		for lookups in (1, 0):
			with self.subTest(lookups=lookups):
				name = self._create_as_admin(llm_allow_lookups=lookups)
				frappe.set_user(JADMIN_USER)
				doc = frappe.get_doc(TRIGGER, name)
				doc.owner = JADMIN_USER
				# our guard fires first; Frappe's own constant check is the backstop
				with self.assertRaises((frappe.PermissionError, frappe.CannotChangeConstantError)):
					doc.save()
				self.assertEqual(frappe.db.get_value(TRIGGER, name, "owner"), ADMIN_USER)

	def test_take_over_then_restore_is_refused_at_the_first_step(self):
		name = self._create_as_admin(llm_allow_lookups=1)
		frappe.set_user(JADMIN_USER)
		doc = frappe.get_doc(TRIGGER, name)
		doc.owner = JADMIN_USER
		doc.llm_instruction = "Read everything and report it."
		with self.assertRaises((frappe.PermissionError, frappe.CannotChangeConstantError)):
			doc.save()
		self.assertEqual(frappe.db.get_value(TRIGGER, name, "owner"), ADMIN_USER)

	def test_administrator_cannot_change_the_owner_through_save_either(self):
		# Frappe itself treats owner as a constant on save, for every user.
		name = self._create_as_admin()
		frappe.set_user("Administrator")
		doc = frappe.get_doc(TRIGGER, name)
		doc.owner = PLAIN_USER
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save()
		self.assertEqual(frappe.db.get_value(TRIGGER, name, "owner"), ADMIN_USER)

	def test_non_owner_manager_can_turn_lookups_off_and_disable(self):
		name = self._create_as_admin(llm_allow_lookups=1)
		frappe.set_user(JADMIN_USER)
		self.assertEqual(set_trigger_enabled(name, 0)["data"]["enabled"], 0)
		off = update_trigger(name, frappe.as_json({"llm_allow_lookups": 0}))
		self.assertEqual(off["data"]["llm_allow_lookups"], 0)


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


class TestScriptActionAuthoring(_TriggersApiTestCase):
	"""A Script-action trigger runs a Server Script with full privileges, so it
	must require Script-Manager authoring rights (issue #13) - the managed sync's
	ignore_permissions/ignore_validate must not become a way around core's
	only_for("Script Manager")."""

	def _script_draft(self, **overrides):
		fields = {
			"doctype": TRIGGER,
			"trigger_name": f"script-{frappe.generate_hash(length=8)}",
			"target_doctype": "ToDo",
			"doc_event": "on_update",
			"action_type": "Script",
			"script_body": "x = 1",
		}
		fields.update(overrides)
		return frappe.get_doc(fields)

	def test_system_manager_without_script_manager_is_refused(self):
		# A System Manager who lacks Script Manager cannot author a Script action,
		# and no managed Server Script is created.
		before = frappe.db.count("Server Script")
		frappe.set_user(ADMIN_USER)
		with patch(_CTRL + ".is_safe_exec_enabled", return_value=True):
			self.assertRaises(frappe.PermissionError, self._script_draft().insert)
		frappe.set_user(self._orig_user)
		self.assertEqual(frappe.db.count("Server Script"), before)

	def test_script_manager_may_author(self):
		# Script Manager + Server Script create permission: allowed, and the
		# managed Server Script is materialized.
		frappe.set_user(SCRIPT_USER)
		with patch(_CTRL + ".is_safe_exec_enabled", return_value=True):
			doc = self._script_draft()
			doc.insert()
		self._triggers.append(doc.name)
		self.assertTrue(doc.server_script)
		self.assertTrue(frappe.db.exists("Server Script", doc.server_script))

	def test_llm_action_is_unaffected(self):
		# The gate is Script-action-only: a manage user without Script Manager can
		# still author LLM triggers.
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(
			{
				"doctype": TRIGGER,
				"trigger_name": f"llm-{frappe.generate_hash(length=8)}",
				"target_doctype": "ToDo",
				"doc_event": "on_update",
				"action_type": "LLM",
				"llm_instruction": "check",
			}
		)
		doc.insert()
		self._triggers.append(doc.name)
		self.assertEqual(doc.action_type, "LLM")

	def test_system_save_is_not_blocked(self):
		# System / programmatic saves (ignore_permissions) must still work, e.g. a
		# patch or fixture materialising a Script trigger with no user in context.
		frappe.set_user(ADMIN_USER)  # no Script Manager, but ignore_permissions set
		with patch(_CTRL + ".is_safe_exec_enabled", return_value=True):
			doc = self._script_draft()
			doc.insert(ignore_permissions=True)
		self._triggers.append(doc.name)
		self.assertTrue(frappe.db.exists(TRIGGER, doc.name))
