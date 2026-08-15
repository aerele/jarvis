"""Tests for the Jarvis Triggers backend: the ``Jarvis Trigger`` doctype
controller (validation + managed Server Script materialization) and the
dispatch engine (``jarvis.triggers.engine`` / ``jarvis.triggers.llm_action``).

Hermetic: every trigger / ToDo / activity row created here is tracked and
deleted in tearDown; no network or LLM calls (``openrouter_complete`` is
patched); Script execution runs with ``is_safe_exec_enabled`` patched ON so
the suite passes on benches without ``server_script_enabled``.

NOTE for the integrator: the "script throw blocks the save" test exercises the
real ``doc_events "*"`` hook wiring, so it needs the app's hooks loaded (bench
migrate + restart after deploying this branch).
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.triggers import engine, llm_action

TRIGGER = "Jarvis Trigger"
ACTIVITY = "Jarvis Trigger Activity"

# The controller module (its imported is_safe_exec_enabled is patched so
# Script triggers validate on benches without server_script_enabled).
CTRL_MOD = "jarvis.jarvis.doctype.jarvis_trigger.jarvis_trigger"
# safe_exec checks the flag again at execution time via its own module global.
EXEC_FLAG = "frappe.utils.safe_exec.is_safe_exec_enabled"


class _TriggerTestCase(FrappeTestCase):
	"""Base: tracked fixtures + queue/depth/cache hygiene."""

	def setUp(self):
		self._triggers: list[str] = []
		self._todos: list[str] = []
		self._server_scripts: list[str] = []
		self._clear_llm_queue()
		frappe.flags._jarvis_trigger_depth = 0
		engine.clear_cache()

	def tearDown(self):
		self._clear_llm_queue()
		frappe.flags._jarvis_trigger_depth = 0
		for name in self._triggers:
			frappe.db.delete(ACTIVITY, {"trigger": name})
			if frappe.db.exists(TRIGGER, name):
				frappe.delete_doc(TRIGGER, name, ignore_permissions=True, force=True)
		for name in self._todos:
			if frappe.db.exists("ToDo", name):
				frappe.delete_doc("ToDo", name, ignore_permissions=True, force=True)
		for name in getattr(self, "_server_scripts", []):
			if frappe.db.exists("Server Script", name):
				frappe.delete_doc("Server Script", name, ignore_permissions=True, force=True)
		engine.clear_cache()
		frappe.db.commit()

	def _clear_llm_queue(self):
		if getattr(frappe.local, "_jarvis_trigger_llm_queue", None):
			del frappe.local._jarvis_trigger_llm_queue

	def _llm_queue(self):
		return getattr(frappe.local, "_jarvis_trigger_llm_queue", None) or []

	def _make_todo(self, description="triggers test todo", **kwargs):
		doc = frappe.get_doc({"doctype": "ToDo", "description": description, **kwargs})
		doc.insert(ignore_permissions=True)
		self._todos.append(doc.name)
		return doc

	def _make_llm_trigger(self, condition="", event="on_update", cap=100, enabled=1):
		doc = frappe.get_doc(
			{
				"doctype": TRIGGER,
				"trigger_name": f"llm-{frappe.generate_hash(length=8)}",
				"target_doctype": "ToDo",
				"doc_event": event,
				"condition": condition,
				"action_type": "LLM",
				"llm_instruction": "Check this todo for problems.",
				"llm_daily_cap": cap,
				"enabled": enabled,
			}
		)
		doc.insert(ignore_permissions=True)
		self._triggers.append(doc.name)
		return doc

	def _make_script_trigger(self, body="x = 1", event="on_update", condition=""):
		with patch(CTRL_MOD + ".is_safe_exec_enabled", return_value=True):
			doc = frappe.get_doc(
				{
					"doctype": TRIGGER,
					"trigger_name": f"script-{frappe.generate_hash(length=8)}",
					"target_doctype": "ToDo",
					"doc_event": event,
					"condition": condition,
					"action_type": "Script",
					"script_body": body,
				}
			)
			doc.insert(ignore_permissions=True)
		self._triggers.append(doc.name)
		return doc

	def _activities(self, trigger, **extra_filters):
		return frappe.get_all(
			ACTIVITY,
			filters={"trigger": trigger, **extra_filters},
			fields=["name", "status", "summary", "detail", "target_docname", "action_type"],
			order_by="creation asc",
		)


# --------------------------------------------------------------------------- #
# Doctype validation
# --------------------------------------------------------------------------- #
class TestTriggerValidation(_TriggerTestCase):
	def _draft(self, **overrides):
		fields = {
			"doctype": TRIGGER,
			"trigger_name": f"val-{frappe.generate_hash(length=8)}",
			"target_doctype": "ToDo",
			"doc_event": "on_update",
			"action_type": "LLM",
			"llm_instruction": "check",
		}
		fields.update(overrides)
		return frappe.get_doc(fields)

	def test_denylisted_doctype_is_rejected(self):
		for dt in ("Jarvis Trigger", "Server Script", "Error Log", "Jarvis Chat Message"):
			with self.assertRaises(frappe.ValidationError):
				self._draft(target_doctype=dt).insert(ignore_permissions=True)

	def test_core_plumbing_doctypes_are_denylisted(self):
		# A blockable/LLM trigger on these would fire on every upload / login /
		# notification and could wedge a core daily workflow (review P1).
		for dt in ("File", "User", "Notification Log", "Email Queue", "Version"):
			with self.assertRaises(frappe.ValidationError):
				self._draft(target_doctype=dt).insert(ignore_permissions=True)

	def test_numeric_threshold_condition_on_blank_doc_is_accepted(self):
		# Review P0: `doc.grand_total > 100000` errors (None > int) when eval'd
		# against a blank doc, but is a valid, savable condition — a real fired
		# document has the value. Use a numeric ToDo field with no default.
		# assigned_by is a Link with no default -> None on a blank ToDo, so the
		# comparison raises TypeError at validation (None > str), exactly like a
		# numeric threshold on an empty currency field.
		trig = self._make_llm_trigger(condition='doc.assigned_by > "zzz"')
		self.assertTrue(frappe.db.exists(TRIGGER, trig.name))

	def test_condition_with_unknown_field_is_still_rejected(self):
		# AttributeError (a typo'd fieldname) must NOT be swallowed by the
		# TypeError tolerance — only None-comparison TypeErrors are accepted.
		with self.assertRaises(frappe.ValidationError):
			self._draft(condition="doc.no_such_field_zzz == 1").insert(ignore_permissions=True)

	def test_condition_using_frappe_is_rejected(self):
		# NameError: only doc + utils exist in the sandbox.
		with self.assertRaises(frappe.ValidationError):
			self._draft(condition='frappe.db.get_value("X","y","z")').insert(ignore_permissions=True)

	def test_foreign_server_script_link_is_stripped(self):
		# Review P0 (security): a client-supplied server_script pointing at an
		# arbitrary Server Script must be rejected, never acted on.
		import frappe as _f

		victim = _f.get_doc(
			{
				"doctype": "Server Script",
				"name": f"qa-victim-{_f.generate_hash(length=6)}",
				"script_type": "DocType Event",
				"reference_doctype": "ToDo",
				"doctype_event": "After Save",
				"script": "x = 1",
				"disabled": 1,
			}
		)
		victim.flags.ignore_validate = True
		victim.insert(ignore_permissions=True)
		self._server_scripts = getattr(self, "_server_scripts", [])
		self._server_scripts.append(victim.name)
		# LLM trigger (would delete server_script on sync) with a foreign link
		draft = self._draft(server_script=victim.name)
		draft.insert(ignore_permissions=True)
		self._triggers.append(draft.name)
		# the foreign link was stripped and the victim survives untouched
		self.assertNotEqual(draft.server_script, victim.name)
		self.assertTrue(frappe.db.exists("Server Script", victim.name))

	def test_single_doctype_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._draft(target_doctype="System Settings").insert(ignore_permissions=True)

	def test_child_table_doctype_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._draft(target_doctype="Has Role").insert(ignore_permissions=True)

	def test_unknown_doctype_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._draft(target_doctype="No Such Doctype Zzz").insert(ignore_permissions=True)

	def test_llm_action_rejected_on_blockable_events(self):
		for event in ("validate", "before_submit"):
			with self.assertRaises(frappe.ValidationError):
				self._draft(doc_event=event).insert(ignore_permissions=True)

	def test_script_allowed_on_validate(self):
		trig = self._make_script_trigger(event="validate")
		self.assertTrue(frappe.db.exists(TRIGGER, trig.name))

	def test_invalid_condition_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._draft(condition="doc.status ==").insert(ignore_permissions=True)

	def test_valid_condition_passes(self):
		trig = self._make_llm_trigger(condition='doc.status == "Open"')
		self.assertTrue(frappe.db.exists(TRIGGER, trig.name))

	def test_script_requires_body(self):
		with patch(CTRL_MOD + ".is_safe_exec_enabled", return_value=True):
			with self.assertRaises(frappe.ValidationError):
				self._draft(action_type="Script", script_body="  ").insert(ignore_permissions=True)

	def test_script_requires_safe_exec_enabled(self):
		with patch(CTRL_MOD + ".is_safe_exec_enabled", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				self._draft(action_type="Script", script_body="x = 1").insert(ignore_permissions=True)

	def test_script_compile_error_throws(self):
		# frappe core only warns on Server Script compile errors; we throw.
		with patch(CTRL_MOD + ".is_safe_exec_enabled", return_value=True):
			with self.assertRaises(frappe.ValidationError):
				self._draft(action_type="Script", script_body="def broken(:\n\tpass").insert(
					ignore_permissions=True
				)

	def test_llm_requires_instruction(self):
		with self.assertRaises(frappe.ValidationError):
			self._draft(llm_instruction="").insert(ignore_permissions=True)

	def test_llm_daily_cap_clamps(self):
		self.assertEqual(self._make_llm_trigger(cap=0).llm_daily_cap, 100)
		self.assertEqual(self._make_llm_trigger(cap=9999).llm_daily_cap, 2000)
		self.assertEqual(self._make_llm_trigger(cap=-5).llm_daily_cap, 1)
		self.assertEqual(self._make_llm_trigger(cap=250).llm_daily_cap, 250)


# --------------------------------------------------------------------------- #
# Managed Server Script materialization
# --------------------------------------------------------------------------- #
class TestManagedServerScript(_TriggerTestCase):
	def test_script_trigger_materializes_disabled_server_script(self):
		trig = self._make_script_trigger(body="x = 1", event="validate")
		trig.reload()
		self.assertEqual(trig.server_script, f"jarvis-trigger-{trig.name}")
		ss = frappe.get_doc("Server Script", trig.server_script)
		self.assertEqual(int(ss.disabled), 1)  # ALWAYS disabled: the engine dispatches it
		self.assertEqual(ss.script_type, "DocType Event")
		self.assertEqual(ss.reference_doctype, "ToDo")
		self.assertEqual(ss.doctype_event, "Before Save")
		self.assertEqual(ss.script, "x = 1")

	def test_event_mapping_on_update_after_submit(self):
		trig = self._make_script_trigger(event="on_update_after_submit")
		ss = frappe.get_doc("Server Script", f"jarvis-trigger-{trig.name}")
		self.assertEqual(ss.doctype_event, "After Save (Submitted Document)")

	def test_switch_to_llm_deletes_managed_script(self):
		trig = self._make_script_trigger(event="on_update")
		ss_name = f"jarvis-trigger-{trig.name}"
		self.assertTrue(frappe.db.exists("Server Script", ss_name))
		trig.reload()
		trig.action_type = "LLM"
		trig.llm_instruction = "check it"
		trig.save(ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Server Script", ss_name))
		trig.reload()
		self.assertFalse(trig.server_script)

	def test_trash_deletes_managed_script(self):
		trig = self._make_script_trigger(event="on_update")
		ss_name = f"jarvis-trigger-{trig.name}"
		self.assertTrue(frappe.db.exists("Server Script", ss_name))
		frappe.delete_doc(TRIGGER, trig.name, ignore_permissions=True, force=True)
		self.assertFalse(frappe.db.exists("Server Script", ss_name))


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
class TestDispatch(_TriggerTestCase):
	def test_condition_true_fires(self):
		trig = self._make_llm_trigger(condition='doc.status == "Open"')
		todo = self._make_todo(status="Open")
		self._clear_llm_queue()
		engine.dispatch(todo, "on_update")
		mine = [j for j in self._llm_queue() if j.trigger == trig.name]
		self.assertEqual(len(mine), 1)
		self.assertEqual(mine[0].docname, todo.name)

	def test_condition_false_skips(self):
		trig = self._make_llm_trigger(condition='doc.status == "Open"')
		todo = self._make_todo(status="Closed")
		self._clear_llm_queue()
		engine.dispatch(todo, "on_update")
		self.assertEqual([j for j in self._llm_queue() if j.trigger == trig.name], [])
		# no activity for a plain condition miss
		self.assertEqual(self._activities(trig.name), [])

	def test_condition_eval_error_is_fail_open(self):
		trig = self._make_llm_trigger(condition='doc.status == "Open"')
		# Fixture todo goes in while the condition is still valid (its insert
		# dispatches through the live hooks); THEN plant a condition that
		# errors at dispatch time, bypassing validate.
		todo = self._make_todo()
		frappe.db.set_value(
			TRIGGER, trig.name, "condition", "doc.no_such_attr_zz == 1", update_modified=False
		)
		engine.clear_cache()
		self._clear_llm_queue()
		engine.dispatch(todo, "on_update")  # must not raise
		rows = self._activities(trig.name, status="Failed")
		self.assertEqual(len(rows), 1)
		self.assertIn("condition error", rows[0].summary)

	def test_disabled_trigger_does_not_fire(self):
		trig = self._make_llm_trigger(enabled=0)
		todo = self._make_todo()
		self._clear_llm_queue()
		engine.dispatch(todo, "on_update")
		self.assertEqual([j for j in self._llm_queue() if j.trigger == trig.name], [])

	def test_script_success_writes_success_activity(self):
		todo = self._make_todo()
		trig = self._make_script_trigger(body="x = 1", event="on_update")
		with patch(EXEC_FLAG, return_value=True):
			engine.dispatch(todo, "on_update")
		rows = self._activities(trig.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].status, "Success")
		self.assertEqual(rows[0].target_docname, todo.name)

	def test_script_throw_blocks_save_and_enqueues_blocked_activity(self):
		# End-to-end through the real doc_events wiring: a validate-event
		# script that frappe.throw()s must block the ToDo insert, and the
		# Blocked activity must ride an enqueue (it survives the rollback).
		trig = self._make_script_trigger(body='frappe.throw("blocked by trigger test")', event="validate")
		todo = frappe.get_doc({"doctype": "ToDo", "description": "should be blocked"})
		with patch(EXEC_FLAG, return_value=True), patch("frappe.enqueue") as enq:
			with self.assertRaises(frappe.ValidationError):
				todo.insert(ignore_permissions=True)
		blocked = [
			c for c in enq.call_args_list if c.args and c.args[0] == "jarvis.triggers.engine.write_activity"
		]
		self.assertEqual(len(blocked), 1)
		self.assertEqual(blocked[0].kwargs.get("status"), "Blocked")
		self.assertEqual(blocked[0].kwargs.get("trigger"), trig.name)
		self.assertIn("blocked by trigger test", blocked[0].kwargs.get("summary") or "")

	def test_script_runtime_error_is_fail_open(self):
		todo = self._make_todo()
		trig = self._make_script_trigger(body="x = 1 / 0", event="on_update")
		with patch(EXEC_FLAG, return_value=True):
			engine.dispatch(todo, "on_update")  # must not raise
		rows = self._activities(trig.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].status, "Failed")

	def test_script_throw_on_nonblockable_event_fails_open(self):
		# Review P2: a ValidationError raised on a POST event (on_update) must
		# NOT block/rollback the user's save — only validate/before_submit block.
		todo = self._make_todo()
		trig = self._make_script_trigger(body='frappe.throw("would block")', event="on_update")
		with patch(EXEC_FLAG, return_value=True):
			engine.dispatch(todo, "on_update")  # must not raise
		rows = self._activities(trig.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].status, "Failed")

	def test_blocked_message_is_prefixed_with_trigger_name(self):
		# Review P2: the blocked user should be told which automation stopped them.
		self._make_script_trigger(body='frappe.throw("no discounts")', event="validate")
		todo = frappe.get_doc({"doctype": "ToDo", "description": "blocked"})
		with patch(EXEC_FLAG, return_value=True), patch("frappe.enqueue"):
			try:
				todo.insert(ignore_permissions=True)
				self.fail("expected the save to be blocked")
			except frappe.ValidationError as e:
				self.assertIn("[Automation:", str(e))
				self.assertIn("no discounts", str(e))

	def test_llm_after_commit_enqueue_dedupes_to_last_snapshot(self):
		trig = self._make_llm_trigger()
		todo = self._make_todo(description="first-marker")
		self._clear_llm_queue()
		engine.dispatch(todo, "on_update")
		todo.description = "second-marker"
		engine.dispatch(todo, "on_update")
		self.assertGreaterEqual(len(self._llm_queue()), 2)
		with patch("frappe.enqueue") as enq:
			engine._flush_llm_queue()
		self.assertEqual(enq.call_count, 1)
		kwargs = enq.call_args.kwargs
		self.assertEqual(enq.call_args.args[0], "jarvis.triggers.llm_action.run_llm_action")
		self.assertEqual(kwargs["trigger"], trig.name)
		self.assertEqual(kwargs["docname"], todo.name)
		self.assertEqual(kwargs["doc_event"], "on_update")
		self.assertEqual(kwargs["timeout"], 180)
		self.assertIn("second-marker", kwargs["snapshot_json"])
		self.assertNotIn("first-marker", kwargs["snapshot_json"])
		# flush cleared the request queue
		self.assertFalse(getattr(frappe.local, "_jarvis_trigger_llm_queue", None))

	def test_llm_snapshot_filters_private_keys(self):
		self._make_llm_trigger()
		todo = self._make_todo()
		self._clear_llm_queue()
		engine.dispatch(todo, "on_update")
		snapshot = self._llm_queue()[-1].snapshot_json
		self.assertNotIn('"__islocal"', snapshot)
		self.assertNotIn('"_user_tags"', snapshot)

	def test_depth_guard_stops_recursion(self):
		trig = self._make_llm_trigger()
		todo = self._make_todo()
		self._clear_llm_queue()
		frappe.flags._jarvis_trigger_depth = 3
		try:
			engine.dispatch(todo, "on_update")
		finally:
			frappe.flags._jarvis_trigger_depth = 0
		self.assertEqual([j for j in self._llm_queue() if j.trigger == trig.name], [])

	def test_unsupported_event_is_ignored(self):
		self._make_llm_trigger()
		todo = self._make_todo()
		self._clear_llm_queue()
		engine.dispatch(todo, "on_change")
		self.assertEqual(self._llm_queue(), [])

	def test_cache_busts_on_trigger_change(self):
		trig = self._make_llm_trigger()
		rows = (engine._triggers_map().get("ToDo") or {}).get("on_update") or []
		self.assertIn(trig.name, [r["name"] for r in rows])
		trig.reload()
		trig.enabled = 0
		trig.save(ignore_permissions=True)
		rows = (engine._triggers_map().get("ToDo") or {}).get("on_update") or []
		self.assertNotIn(trig.name, [r["name"] for r in rows])


# --------------------------------------------------------------------------- #
# LLM action job
# --------------------------------------------------------------------------- #
class TestRunLLMAction(_TriggerTestCase):
	def _run(self, trig, snapshot='{"description": "x"}'):
		llm_action.run_llm_action(
			trigger=trig.name,
			doctype="ToDo",
			docname="some-todo",
			doc_event="on_update",
			snapshot_json=snapshot,
			fired_by="Administrator",
		)

	def test_success_writes_success_activity(self):
		trig = self._make_llm_trigger()
		with patch("jarvis.chat.voice.openrouter_complete", return_value="All good.") as oc:
			self._run(trig)
		self.assertEqual(oc.call_count, 1)
		messages = oc.call_args.args[0]
		self.assertEqual(messages[0]["role"], "system")
		self.assertIn("<untrusted-data", messages[1]["content"])
		rows = self._activities(trig.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].status, "Success")
		self.assertEqual(rows[0].summary, "All good.")

	def test_failure_writes_failed_activity(self):
		trig = self._make_llm_trigger()
		with patch(
			"jarvis.chat.voice.openrouter_complete",
			side_effect=frappe.ValidationError("Speech-to-text is not configured on this site."),
		):
			self._run(trig)
		rows = self._activities(trig.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].status, "Failed")
		self.assertIn("not configured", rows[0].summary)

	def test_daily_cap_skips_with_one_skipped_marker(self):
		trig = self._make_llm_trigger(cap=1)
		with patch("jarvis.chat.voice.openrouter_complete", return_value="ok") as oc:
			self._run(trig)  # 1 -> runs
			self._run(trig)  # 2 == cap+1 -> one Skipped marker
			self._run(trig)  # 3 -> silent
		self.assertEqual(oc.call_count, 1)
		rows = self._activities(trig.name)
		self.assertEqual(sorted(r.status for r in rows), ["Skipped", "Success"])
		skipped = next(r for r in rows if r.status == "Skipped")
		self.assertIn("daily LLM cap reached (1)", skipped.summary)

	def test_missing_or_disabled_trigger_is_silent(self):
		with patch("jarvis.chat.voice.openrouter_complete", return_value="ok") as oc:
			llm_action.run_llm_action(
				trigger="no-such-trigger-zz",
				doctype="ToDo",
				docname="x",
				doc_event="on_update",
				snapshot_json="{}",
				fired_by="Administrator",
			)
			trig = self._make_llm_trigger()
			frappe.db.set_value(TRIGGER, trig.name, "enabled", 0, update_modified=False)
			self._run(trig)
		self.assertEqual(oc.call_count, 0)
		self.assertEqual(self._activities(trig.name), [])
