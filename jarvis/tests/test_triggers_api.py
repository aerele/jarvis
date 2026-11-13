"""Tests for jarvis.chat.triggers_api - the SPA-facing Triggers endpoints.

Two fixture users: an ADMIN (System Manager) who may manage triggers and a
PLAIN jarvis user (only the ``Jarvis User`` role) who is read-only. Hermetic:
every trigger / activity / ToDo row created here is tracked and deleted in
tearDown; no LLM/network calls anywhere on these paths.
"""

from unittest.mock import patch

import frappe
from frappe.exceptions import FrappeTypeError
from frappe.tests.utils import FrappeTestCase
from frappe.utils import strip_html

from jarvis.chat.triggers_api import (
	activity_stats,
	create_trigger,
	delete_trigger,
	delete_triggers_bulk,
	get_activity_detail,
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
# Shared instruction for the duplicate-guard tests (#816); other tests get a unique one.
SAME = "Check this todo."


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
			# Unique per call: identical enabled triggers are refused (#816).
			"llm_instruction": f"Check this todo {frappe.generate_hash(length=8)}.",
		}
		fields.update(overrides)
		return {k: v for k, v in fields.items() if v is not None}

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

	def test_failed_script_summary_does_not_leak_record_values_to_search(self):
		# A script error carries a record value (KeyError(<value>)). The Activity
		# summary is LIKE-searchable by non-managers who can read the target
		# doc, so the Failed row must hold the exception class only.
		from jarvis.triggers.engine import dispatch

		# The target goes in first so its own insert does not fire the trigger.
		todo = frappe.get_doc(
			{"doctype": "ToDo", "description": "leak target", "allocated_to": PLAIN_USER}
		).insert(ignore_permissions=True)
		self._todos.append(todo.name)
		with patch(_CTRL + ".is_safe_exec_enabled", return_value=True):
			trig = frappe.get_doc(
				{
					"doctype": TRIGGER,
					"trigger_name": f"leak-{frappe.generate_hash(length=8)}",
					"target_doctype": "ToDo",
					"doc_event": "on_update",
					"action_type": "Script",
					"script_body": 'd = {}\nd["SECRET-123"]',
				}
			).insert(ignore_permissions=True)
		self._triggers.append(trig.name)
		with patch("frappe.utils.safe_exec.is_safe_exec_enabled", return_value=True):
			dispatch(todo, "on_update")  # fail-open: must not raise

		frappe.set_user(PLAIN_USER)
		flt = frappe.as_json({"trigger": trig.name})
		rows = list_activity_page(filters=flt)["data"]["rows"]
		self.assertEqual(len(rows), 1)  # the plain user can read the target, so sees the row
		self.assertEqual(rows[0]["status"], "Failed")
		self.assertEqual(rows[0]["summary"], "script error: KeyError (save not blocked)")
		found = list_activity_page(search="SECRET-123", filters=flt)["data"]["rows"]
		self.assertEqual(found, [])

	def test_failed_llm_summary_does_not_leak_error_text_to_search(self):
		# Lookups OFF: the row is visible to anyone who can read the target doc, and
		# its summary is LIKE-searchable. The Failed summary is a fixed text.
		from jarvis.triggers import llm_action
		from jarvis.triggers.llm_task_client import LLMTaskError

		todo = frappe.get_doc(
			{"doctype": "ToDo", "description": "llm leak target", "allocated_to": PLAIN_USER}
		).insert(ignore_permissions=True)
		self._todos.append(todo.name)
		name = self._create_as_admin()
		frappe.set_user(self._orig_user)
		with patch(
			"jarvis.triggers.llm_task_client.llm_task_complete",
			side_effect=LLMTaskError("malformed response: SECRET-123"),
		):
			llm_action.run_llm_action(
				trigger=name,
				doctype="ToDo",
				docname=todo.name,
				doc_event="on_update",
				snapshot_json="{}",
				fired_by="Administrator",
			)

		frappe.set_user(PLAIN_USER)
		flt = frappe.as_json({"trigger": name})
		rows = list_activity_page(filters=flt)["data"]["rows"]
		self.assertEqual(len(rows), 1)  # visible without any search term
		self.assertEqual(rows[0]["status"], "Failed")
		self.assertEqual(rows[0]["summary"], llm_action._LLM_FAILED_SUMMARY)
		self.assertNotIn("detail", rows[0])
		found = list_activity_page(search="SECRET-123", filters=flt)["data"]["rows"]
		self.assertEqual(found, [])

	def test_manager_gets_detail_clipped_at_the_cap_keeping_the_head(self):
		from jarvis.chat.triggers_api import _ACTIVITY_DETAIL_CAP

		head = "HEAD-MARKER "
		detail = head + "x" * (_ACTIVITY_DETAIL_CAP + 500)
		name = self._create_as_admin()
		row = self._insert_activity_row(trigger=name, status="Failed", detail=detail)
		short = self._insert_activity_row(trigger=name, status="Failed", detail="short detail")
		frappe.set_user(ADMIN_USER)
		rows = {
			r["name"]: r
			for r in list_activity_page(filters=frappe.as_json({"trigger": name}))["data"]["rows"]
		}
		clipped = rows[row]["detail"]
		self.assertEqual(len(clipped), _ACTIVITY_DETAIL_CAP + 1)
		self.assertTrue(clipped.startswith(head))
		self.assertTrue(clipped.endswith("\u2026"))
		self.assertEqual(rows[short]["detail"], "short detail")  # not cut, no ellipsis

	def test_jarvis_admin_who_is_not_system_manager_gets_detail(self):
		name = self._create_as_admin()
		row = self._insert_activity_row(trigger=name, status="Failed", detail="the raw error")
		frappe.set_user(JADMIN_USER)
		self.assertNotIn("System Manager", frappe.get_roles(JADMIN_USER))
		rows = list_activity_page(filters=frappe.as_json({"trigger": name}))["data"]["rows"]
		self.assertEqual([r["detail"] for r in rows if r["name"] == row], ["the raw error"])

	def test_non_manager_rows_have_no_detail_key(self):
		todo = frappe.get_doc(
			{"doctype": "ToDo", "description": "no detail target", "allocated_to": PLAIN_USER}
		).insert(ignore_permissions=True)
		self._todos.append(todo.name)
		name = self._create_as_admin()
		self._insert_activity_row(
			trigger=name, target_docname=todo.name, status="Failed", detail="the raw error"
		)
		frappe.set_user(PLAIN_USER)
		rows = list_activity_page(filters=frappe.as_json({"trigger": name}))["data"]["rows"]
		self.assertEqual(len(rows), 1)
		self.assertNotIn("detail", rows[0])

	def _manager_rows(self, trigger: str) -> dict:
		frappe.set_user(ADMIN_USER)
		rows = list_activity_page(filters=frappe.as_json({"trigger": trigger}))["data"]["rows"]
		return {r["name"]: r for r in rows}

	def test_manager_detail_truncated_flag_boundary_4000_vs_4001(self):
		from jarvis.chat.triggers_api import _ACTIVITY_DETAIL_CAP

		name = self._create_as_admin()
		exact = self._insert_activity_row(trigger=name, status="Failed", detail="y" * _ACTIVITY_DETAIL_CAP)
		over = self._insert_activity_row(
			trigger=name, status="Failed", detail="y" * (_ACTIVITY_DETAIL_CAP + 1)
		)
		rows = self._manager_rows(name)
		self.assertIs(rows[exact]["detail_truncated"], False)
		self.assertEqual(rows[exact]["detail"], "y" * _ACTIVITY_DETAIL_CAP)
		self.assertIs(rows[over]["detail_truncated"], True)
		self.assertEqual(rows[over]["detail"], "y" * _ACTIVITY_DETAIL_CAP + "\u2026")

	def test_manager_null_detail_is_empty_and_not_truncated(self):
		name = self._create_as_admin()
		row = self._insert_activity_row(trigger=name, status="Failed", detail="x")
		frappe.db.set_value(ACTIVITY, row, "detail", None)
		rows = self._manager_rows(name)
		self.assertEqual(rows[row]["detail"], "")
		self.assertIs(rows[row]["detail_truncated"], False)

	def test_non_manager_rows_have_no_detail_truncated_key(self):
		todo = frappe.get_doc(
			{"doctype": "ToDo", "description": "no flag target", "allocated_to": PLAIN_USER}
		).insert(ignore_permissions=True)
		self._todos.append(todo.name)
		name = self._create_as_admin()
		self._insert_activity_row(trigger=name, target_docname=todo.name, status="Failed", detail="z" * 5000)
		frappe.set_user(PLAIN_USER)
		rows = list_activity_page(filters=frappe.as_json({"trigger": name}))["data"]["rows"]
		self.assertEqual(len(rows), 1)
		self.assertNotIn("detail", rows[0])
		self.assertNotIn("detail_truncated", rows[0])

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


class TestGetActivityDetail(_TriggersApiTestCase):
	def _row(self, detail: str = "the full error") -> str:
		return self._insert_activity_row(trigger=self._create_as_admin(), status="Failed", detail=detail)

	def test_system_manager_gets_the_full_detail(self):
		detail = "head " + "q" * 6000
		row = self._row(detail)
		frappe.set_user(ADMIN_USER)
		out = get_activity_detail(row)
		self.assertEqual(out, {"ok": True, "data": {"name": row, "detail": detail}})

	def test_jarvis_admin_who_is_not_system_manager_gets_the_full_detail(self):
		row = self._row("raw error for jarvis admin")
		frappe.set_user(JADMIN_USER)
		self.assertNotIn("System Manager", frappe.get_roles(JADMIN_USER))
		out = get_activity_detail(row)
		self.assertEqual(out["data"], {"name": row, "detail": "raw error for jarvis admin"})

	def test_null_detail_returns_empty_string(self):
		row = self._row("x")
		frappe.db.set_value(ACTIVITY, row, "detail", None)
		frappe.set_user(ADMIN_USER)
		self.assertEqual(get_activity_detail(row)["data"]["detail"], "")

	def test_non_manager_gets_the_same_permission_error_for_real_and_missing_names(self):
		row = self._row()
		frappe.set_user(PLAIN_USER)
		messages = []
		for name in (row, "no-such-activity-zz"):
			with self.assertRaises(frappe.PermissionError) as ctx:
				get_activity_detail(name)
			messages.append(str(ctx.exception))
		self.assertEqual(messages[0], messages[1])
		self.assertNotIn(row, messages[0])

	def test_manager_missing_name_is_does_not_exist_without_echoing_the_name(self):
		frappe.set_user(ADMIN_USER)
		missing = "no-such-activity-zz"
		with self.assertRaises(frappe.DoesNotExistError) as ctx:
			get_activity_detail(missing)
		self.assertNotIn(missing, str(ctx.exception))

	def test_blank_name_is_an_error(self):
		frappe.set_user(ADMIN_USER)
		for blank in ("", "   "):
			with self.assertRaises(frappe.ValidationError):
				get_activity_detail(blank)

	def test_non_string_name_is_rejected_before_any_read(self):
		# Frappe's whitelist type check (FrappeTypeError) is a no-op for this module:
		# ``from __future__ import annotations`` makes the annotations strings, which
		# frappe.utils.typing_validations skips. So the body must reject a non-str
		# itself (ValidationError). Either error is accepted, as in test_pulse_feedback;
		# call with keyword arguments and the request condition, as Frappe does.
		frappe.set_user(ADMIN_USER)
		with patch.object(frappe, "in_test", True):
			for bad in (None, 7, ["a"], {"a": 1}):
				with self.subTest(name=bad):
					# wraps: the permission gate reads through get_value too
					with patch.object(frappe.db, "get_value", wraps=frappe.db.get_value) as get_value:
						with self.assertRaises((FrappeTypeError, frappe.ValidationError)):
							get_activity_detail(name=bad)
						self.assertFalse(
							[c for c in get_value.call_args_list if c.args and c.args[0] == ACTIVITY],
							"the Activity table must not be read for a non-str name",
						)

	def test_stored_value_longer_than_the_engine_cap_is_returned_whole(self):
		# Frappe sanitizes Long Text that contains a tag (frappe/utils/html_utils.py),
		# and nh3 escapes every bare "<" to "&lt;", so the stored value can be longer
		# than the engine's 20000-char cut. The endpoint returns the DB value as is.
		name = self._create_as_admin()
		raw = "<b>x</b>" + "<" * 19990
		self.assertLessEqual(len(raw), 20000)
		engine._insert_activity(trigger=name, status="Failed", detail=raw)
		row = frappe.get_all(ACTIVITY, filters={"trigger": name}, pluck="name")[0]
		self._activities.append(row)
		stored = frappe.db.get_value(ACTIVITY, row, "detail")
		self.assertGreater(len(stored), 20000)
		frappe.set_user(ADMIN_USER)
		self.assertEqual(get_activity_detail(row)["data"]["detail"], stored)


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


class TestSetEnabledUnsafeAttributes(_TriggersApiTestCase):
	"""Turning a trigger ON re-checks its stored code (#818): a body or condition
	that was saved before the check existed must not be re-armed. set_trigger_enabled
	is a raw db_set, so it runs the same check as validate. Turning OFF never does."""

	BAD_BODY = 'x = "PO {}".format(doc.name)\n'
	BAD_COND = '"{}".format(doc.description) if doc.description else False'

	def _stored_bad_script_trigger(self) -> str:
		with patch(_CTRL + ".is_safe_exec_enabled", return_value=True):
			doc = frappe.get_doc(
				{
					"doctype": TRIGGER,
					"trigger_name": f"script-{frappe.generate_hash(length=8)}",
					"target_doctype": "ToDo",
					"doc_event": "on_update",
					"action_type": "Script",
					"script_body": "x = 1",
				}
			)
			doc.insert(ignore_permissions=True)
		self._triggers.append(doc.name)
		frappe.db.set_value(TRIGGER, doc.name, {"script_body": self.BAD_BODY, "enabled": 1})
		return doc.name

	def test_toggle_on_refuses_stored_format_script_and_off_still_works(self):
		name = self._stored_bad_script_trigger()
		frappe.set_user(ADMIN_USER)
		self.assertEqual(set_trigger_enabled(name, 0)["data"]["enabled"], 0)
		with self.assertRaises(frappe.ValidationError) as cm:
			set_trigger_enabled(name, 1)
		self.assertIn("blocks: line 1", strip_html(str(cm.exception)))
		# the refused toggle changed nothing
		self.assertEqual(int(frappe.db.get_value(TRIGGER, name, "enabled")), 0)

	def test_toggle_off_on_a_bad_enabled_trigger_works(self):
		name = self._stored_bad_script_trigger()
		frappe.set_user(ADMIN_USER)
		self.assertEqual(set_trigger_enabled(name, 0)["data"]["enabled"], 0)

	def test_toggle_on_refuses_llm_trigger_with_stored_format_condition(self):
		name = self._create_as_admin()
		frappe.db.set_value(TRIGGER, name, {"condition": self.BAD_COND, "enabled": 0})
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.ValidationError) as cm:
			set_trigger_enabled(name, 1)
		self.assertIn("Invalid Condition:", str(cm.exception))
		self.assertEqual(int(frappe.db.get_value(TRIGGER, name, "enabled")), 0)

	def test_toggle_on_refuses_script_trigger_with_clean_script_and_bad_stored_condition(self):
		name = self._stored_bad_script_trigger()
		frappe.db.set_value(TRIGGER, name, {"script_body": "x = 1", "condition": self.BAD_COND, "enabled": 0})
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.ValidationError) as cm:
			set_trigger_enabled(name, 1)
		self.assertIn("Invalid Condition:", str(cm.exception))
		self.assertEqual(int(frappe.db.get_value(TRIGGER, name, "enabled")), 0)

	def test_toggle_on_llm_trigger_ignores_leftover_script_body(self):
		# script_body is only code for a Script action; an LLM trigger that still
		# holds an old body must not be refused.
		name = self._create_as_admin()
		frappe.db.set_value(TRIGGER, name, {"script_body": self.BAD_BODY, "enabled": 0})
		frappe.set_user(ADMIN_USER)
		self.assertEqual(set_trigger_enabled(name, 1)["data"]["enabled"], 1)

	def test_toggle_on_clean_trigger_works(self):
		name = self._create_as_admin()
		frappe.db.set_value(TRIGGER, name, "enabled", 0)
		frappe.set_user(ADMIN_USER)
		self.assertEqual(set_trigger_enabled(name, 1)["data"]["enabled"], 1)


class TestDuplicateTriggers(_TriggersApiTestCase):
	"""#816: triggers are site-wide, so two enabled rows with the same target,
	event, condition and action fire twice. Same-type duplicates are refused
	whoever created the first one; a cross-type match only warns."""

	def _script_doc(self, **overrides) -> "frappe.model.document.Document":
		fields = {
			"doctype": TRIGGER,
			"trigger_name": f"dup-{frappe.generate_hash(length=8)}",
			"target_doctype": "ToDo",
			"doc_event": "on_update",
			"action_type": "Script",
			"script_body": "pass",
		}
		fields.update(overrides)
		return frappe.get_doc(fields)

	def test_same_type_duplicate_refused_across_users(self):
		first = self._create_as_admin(condition="doc.priority == 'High'", llm_instruction=SAME)
		frappe.set_user(JADMIN_USER)
		with self.assertRaises(frappe.ValidationError):
			create_trigger(
				frappe.as_json(self._llm_payload(condition="doc.priority == 'High'", llm_instruction=SAME))
			)
		self.assertEqual(
			frappe.db.count(TRIGGER, {"target_doctype": "ToDo", "condition": "doc.priority == 'High'"}), 1
		)
		self.assertTrue(frappe.db.exists(TRIGGER, first))

	def test_refusal_names_existing_trigger_only_if_readable(self):
		# JADMIN (Jarvis Admin) can read the row, so the title is shown.
		self._create_as_admin(
			trigger_name="Flag big discounts", condition="doc.priority == 'Low'", llm_instruction=SAME
		)
		frappe.set_user(JADMIN_USER)
		with self.assertRaises(frappe.ValidationError) as cm:
			create_trigger(
				frappe.as_json(self._llm_payload(condition="doc.priority == 'Low'", llm_instruction=SAME))
			)
		self.assertIn("Flag big discounts", str(cm.exception))
		# A user with no read permission on Jarvis Trigger gets a generic message.
		dup_fields = self._llm_payload(condition="doc.priority == 'Low'", llm_instruction=SAME)
		dup = frappe.get_doc({"doctype": TRIGGER, **dup_fields})
		with patch("frappe.has_permission", return_value=False):
			with self.assertRaises(frappe.ValidationError) as cm:
				dup.insert()
		self.assertNotIn("Flag big discounts", str(cm.exception))

	def test_whitespace_only_condition_difference_is_duplicate(self):
		self._create_as_admin(condition="doc.priority  ==   'High'", llm_instruction=SAME)
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.ValidationError):
			dup = self._llm_payload(condition="  doc.priority == 'High'\n", llm_instruction=SAME)
			create_trigger(frappe.as_json(dup))

	def test_different_instruction_is_not_duplicate(self):
		self._create_as_admin(llm_instruction=SAME)
		name = self._create_as_admin(llm_instruction="Summarise this todo.")
		self.assertTrue(frappe.db.exists(TRIGGER, name))

	def test_disabled_existing_trigger_does_not_block(self):
		first = self._create_as_admin(llm_instruction=SAME)
		set_trigger_enabled(first, 0)
		second = self._create_as_admin(llm_instruction=SAME)
		self.assertTrue(frappe.db.exists(TRIGGER, second))

	def test_enabling_a_duplicate_is_refused(self):
		first = self._create_as_admin(llm_instruction=SAME)
		set_trigger_enabled(first, 0)
		second = self._create_as_admin(llm_instruction=SAME)
		with self.assertRaises(frappe.ValidationError):
			set_trigger_enabled(first, 1)
		self.assertEqual(frappe.db.get_value(TRIGGER, first, "enabled"), 0)
		self.assertTrue(frappe.db.exists(TRIGGER, second))

	def test_editing_does_not_collide_with_itself(self):
		name = self._create_as_admin(condition="doc.priority == 'High'")
		updated = update_trigger(name, frappe.as_json({"description": "edited"}))
		self.assertEqual(updated["data"]["description"], "edited")
		self.assertNotIn("warning", updated)

	def test_edit_into_a_duplicate_is_refused(self):
		self._create_as_admin(llm_instruction=SAME)
		other = self._create_as_admin(llm_instruction="Something else.")
		with self.assertRaises(frappe.ValidationError):
			update_trigger(other, frappe.as_json({"llm_instruction": SAME}))

	def test_cross_type_is_allowed_with_warning(self):
		self._create_as_admin(condition="doc.priority == 'High'")
		frappe.set_user(SCRIPT_USER)
		with patch(_CTRL + ".is_safe_exec_enabled", return_value=True):
			payload = self._llm_payload(
				condition="doc.priority == 'High'", action_type="Script", script_body="pass"
			)
			payload.pop("llm_instruction")
			r = create_trigger(frappe.as_json(payload))
		self._triggers.append(r["data"]["name"])
		self.assertTrue(frappe.db.exists(TRIGGER, r["data"]["name"]))
		self.assertIn("warning", r)
		self.assertTrue(r["warning"])

	def test_cross_type_desk_path_msgprints_orange(self):
		self._create_as_admin(condition="doc.priority == 'High'")
		frappe.set_user(SCRIPT_USER)
		frappe.local.message_log = []
		with patch(_CTRL + ".is_safe_exec_enabled", return_value=True):
			doc = self._script_doc(condition="doc.priority == 'High'")
			doc.insert()
		self._triggers.append(doc.name)
		self.assertTrue(
			any(m.get("indicator") == "orange" for m in map(frappe.parse_json, frappe.local.message_log))
		)


class TestMultipleEvents(_TriggersApiTestCase):
	"""jarvis-admin-v2#825: create/update/read a trigger with several doc events."""

	def test_create_with_a_list_returns_every_event(self):
		name = self._create_as_admin(doc_event=None, doc_events=["on_trash", "on_update"])
		detail = get_trigger(name)["data"]
		self.assertEqual(detail["doc_events"], ["on_update", "on_trash"])
		self.assertEqual(detail["doc_event"], "on_update")

	def test_update_doc_events_from_the_form(self):
		name = self._create_as_admin()
		r = update_trigger(name, frappe.as_json({"doc_events": "after_insert\non_update"}))
		self.assertEqual(r["data"]["doc_events"], ["after_insert", "on_update"])

	def test_list_rows_carry_the_events_and_the_filter_matches_any_of_them(self):
		name = self._create_as_admin(doc_event=None, doc_events=["on_update", "on_trash"])
		for event in ("on_update", "on_trash"):
			rows = list_triggers_page(filters=frappe.as_json({"doc_event": event}))["data"]["rows"]
			self.assertIn(name, [r["name"] for r in rows], event)
		row = next(
			r
			for r in list_triggers_page(filters=frappe.as_json({"doc_event": "on_trash"}))["data"]["rows"]
			if r["name"] == name
		)
		self.assertEqual(row["doc_events"], ["on_update", "on_trash"])
		rows = list_triggers_page(filters=frappe.as_json({"doc_event": "after_insert"}))["data"]["rows"]
		self.assertNotIn(name, [r["name"] for r in rows])
