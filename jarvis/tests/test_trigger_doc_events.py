"""One trigger, several doc events (jarvis-admin-v2#825).

``Jarvis Trigger.doc_events`` lists every event a trigger runs on, one per line;
``doc_event`` stays as the first of them. Covers the parsing helpers, the
controller's normalisation (including callers that only know ``doc_event``), the
LLM event check, the engine map and real dispatch on each event, and the
duplicate guard (#816) on overlapping event sets.
"""

from unittest.mock import patch

import frappe
from frappe.core.doctype.doctype.test_doctype import new_doctype

from jarvis.patches import v2_32_fill_trigger_doc_events
from jarvis.tests.test_triggers_engine import ACTIVITY, CTRL_MOD, EXEC_FLAG, TRIGGER, _TriggerTestCase
from jarvis.triggers import engine
from jarvis.triggers.engine import split_events, trigger_events


class TestEventHelpers(_TriggerTestCase):
	def test_split_events_accepts_lines_lists_and_commas(self):
		self.assertEqual(split_events("on_submit\non_cancel"), ["on_submit", "on_cancel"])
		self.assertEqual(split_events(["on_cancel", "on_submit", "on_cancel"]), ["on_cancel", "on_submit"])
		self.assertEqual(split_events(" on_update, on_trash "), ["on_update", "on_trash"])
		self.assertEqual(split_events(None), [])
		self.assertEqual(split_events(""), [])

	def test_trigger_events_orders_and_falls_back_to_doc_event(self):
		self.assertEqual(trigger_events("on_cancel\non_submit"), ["on_submit", "on_cancel"])
		self.assertEqual(trigger_events("", "on_update"), ["on_update"])
		self.assertEqual(trigger_events(None, "on_trash"), ["on_trash"])
		self.assertEqual(trigger_events("bogus\non_update"), ["on_update"])


class TestDocEventsField(_TriggerTestCase):
	def _insert(self, **fields):
		doc = frappe.get_doc(
			{
				"doctype": TRIGGER,
				"trigger_name": f"multi-{frappe.generate_hash(length=8)}",
				"target_doctype": "ToDo",
				"action_type": "LLM",
				"llm_instruction": f"Check {frappe.generate_hash(length=8)}.",
				**fields,
			}
		)
		doc.insert(ignore_permissions=True)
		self._triggers.append(doc.name)
		return doc

	def test_several_events_are_stored_in_order_with_the_first_as_doc_event(self):
		doc = self._insert(doc_events=["on_trash", "after_insert", "on_update"])
		self.assertEqual(doc.doc_events, "after_insert\non_update\non_trash")
		self.assertEqual(doc.doc_event, "after_insert")
		self.assertEqual(doc.events(), ["after_insert", "on_update", "on_trash"])

	def test_a_caller_that_only_sets_doc_event_still_works(self):
		doc = self._insert(doc_event="on_update")
		self.assertEqual(doc.doc_events, "on_update")
		self.assertEqual(doc.events(), ["on_update"])

	def test_editing_doc_event_alone_swaps_that_event(self):
		doc = self._insert(doc_events="after_insert\non_trash")
		doc.reload()
		doc.doc_event = "on_update"
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.doc_events, "on_update\non_trash")
		self.assertEqual(doc.doc_event, "on_update")

	def test_editing_doc_events_wins_over_a_stale_doc_event(self):
		doc = self._insert(doc_events="on_update")
		doc.reload()
		doc.doc_events = "on_trash\nafter_insert"
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.doc_events, "after_insert\non_trash")
		self.assertEqual(doc.doc_event, "after_insert")

	def test_an_unknown_event_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._insert(doc_events="on_update\non_payment")
		self.assertIn("on_payment", str(cm.exception))

	def test_no_event_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._insert(doc_event="", doc_events="")

	def test_llm_refuses_an_in_transaction_event_among_several(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._insert(doc_events="validate\non_update")
		self.assertIn("validate", str(cm.exception))


class TestMultiEventDispatch(_TriggerTestCase):
	def _script_trigger(self, events, body="x = 1"):
		with patch(CTRL_MOD + ".is_safe_exec_enabled", return_value=True):
			doc = frappe.get_doc(
				{
					"doctype": TRIGGER,
					"trigger_name": f"multi-script-{frappe.generate_hash(length=8)}",
					"target_doctype": "ToDo",
					"doc_events": events,
					"action_type": "Script",
					"script_body": body,
				}
			)
			doc.insert(ignore_permissions=True)
		self._triggers.append(doc.name)
		return doc

	def test_the_map_lists_the_trigger_under_each_event(self):
		trig = self._script_trigger("after_insert\non_trash")
		engine.clear_cache()
		by_event = engine._triggers_map().get("ToDo") or {}
		for event in ("after_insert", "on_trash"):
			self.assertIn(trig.name, [r["name"] for r in by_event.get(event, [])])
		self.assertNotIn(trig.name, [r["name"] for r in by_event.get("on_update", [])])

	def test_the_script_runs_on_each_of_its_events(self):
		# The ToDo exists BEFORE the trigger, so its insert fires nothing: every
		# activity row below comes from the later real save and the real delete.
		todo = self._make_todo()
		trig = self._script_trigger("on_update\non_trash")
		with patch(EXEC_FLAG, return_value=True):
			todo.reload()
			todo.description = "edited after the trigger exists"
			todo.save(ignore_permissions=True)
			self.assertEqual(self._events(trig.name), ["on_update"])
			frappe.delete_doc("ToDo", todo.name, ignore_permissions=True, force=True)
		self.assertEqual(self._events(trig.name), ["on_update", "on_trash"])
		self.assertTrue(all(r.status == "Success" for r in self._activities(trig.name)))

	def _events(self, trigger):
		return frappe.get_all(
			"Jarvis Trigger Activity",
			filters={"trigger": trigger},
			pluck="doc_event",
			order_by="creation asc",
		)

	def test_an_llm_trigger_queues_on_each_event(self):
		trig = frappe.get_doc(
			{
				"doctype": TRIGGER,
				"trigger_name": f"multi-llm-{frappe.generate_hash(length=8)}",
				"target_doctype": "ToDo",
				"doc_events": ["on_update", "on_trash"],
				"action_type": "LLM",
				"llm_instruction": "Check this todo.",
			}
		).insert(ignore_permissions=True)
		self._triggers.append(trig.name)
		engine.clear_cache()
		todo = self._make_todo()
		self._clear_llm_queue()
		engine.dispatch(todo, "on_trash")
		self.assertEqual([j.event for j in self._llm_queue() if j.trigger == trig.name], ["on_trash"])


class TestDuplicateGuardOnEventSets(_TriggerTestCase):
	BODY = "Look for a missing date."

	def _insert(self, events, enabled=1):
		doc = frappe.get_doc(
			{
				"doctype": TRIGGER,
				"trigger_name": f"dup-{frappe.generate_hash(length=8)}",
				"target_doctype": "ToDo",
				"doc_events": events,
				"action_type": "LLM",
				"llm_instruction": self.BODY,
				"enabled": enabled,
			}
		)
		doc.insert(ignore_permissions=True)
		self._triggers.append(doc.name)
		return doc

	def test_a_shared_event_is_a_duplicate(self):
		self._insert("on_update\non_trash")
		with self.assertRaises(frappe.ValidationError):
			self._insert("on_trash")

	def test_disjoint_events_are_not(self):
		self._insert("on_update\non_trash")
		self._insert("after_insert")


class TestLegacyRowsAndRollout(_TriggerTestCase):
	"""A trigger saved before ``doc_events`` existed has it blank. Until the
	patch fills it, the engine must still fire it on its single ``doc_event``;
	the patch fills such rows once and leaves everything else alone."""

	def _legacy_script_trigger(self, event="on_update", body="x = 1"):
		trig = self._make_script_trigger(body=body, event=event)
		# what an existing row looks like before the field existed
		frappe.db.set_value(TRIGGER, trig.name, "doc_events", "", update_modified=False)
		engine.clear_cache()
		return trig

	def test_a_legacy_row_is_mapped_and_dispatched_on_its_doc_event(self):
		todo = self._make_todo()
		trig = self._legacy_script_trigger("on_update")
		self.assertEqual(frappe.db.get_value(TRIGGER, trig.name, "doc_events"), "")
		by_event = engine._build_triggers_map().get("ToDo") or {}
		self.assertIn(trig.name, [r["name"] for r in by_event.get("on_update", [])])
		for event, rows in by_event.items():
			if event != "on_update":
				self.assertNotIn(trig.name, [r["name"] for r in rows], event)
		with patch(EXEC_FLAG, return_value=True):
			engine.dispatch(todo, "on_update")
			engine.dispatch(todo, "on_trash")
		self.assertEqual(
			frappe.get_all(ACTIVITY, filters={"trigger": trig.name}, pluck="doc_event"), ["on_update"]
		)

	def test_the_patch_fills_blank_rows_only_and_is_idempotent(self):
		legacy = self._legacy_script_trigger("on_trash")
		# distinct bodies, so the duplicate guard (#816) does not refuse the fixtures
		populated = self._make_script_trigger(body="x = 2", event="on_update")
		frappe.db.set_value(
			TRIGGER, populated.name, "doc_events", "on_update\non_trash", update_modified=False
		)
		# an Intake rule runs from uploads, never from doc events: left blank
		intake = self._legacy_script_trigger("on_update", body="x = 3")
		frappe.db.set_value(TRIGGER, intake.name, "action_type", "Intake", update_modified=False)

		def stored():
			return {
				n: frappe.db.get_value(TRIGGER, n, ["doc_events", "modified"])
				for n in (legacy.name, populated.name, intake.name)
			}

		before = stored()
		v2_32_fill_trigger_doc_events.execute()
		after = stored()
		self.assertEqual(after[legacy.name][0], "on_trash")
		self.assertEqual(after[populated.name], before[populated.name])
		self.assertEqual(after[intake.name], before[intake.name])
		v2_32_fill_trigger_doc_events.execute()
		self.assertEqual(stored(), after)


class TestBlockingScriptOnSeveralEvents(_TriggerTestCase):
	"""A Script trigger on validate + before_submit: both run through the real
	save and submit, and a throw blocks on each of them."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.doctype = new_doctype(custom=1, is_submittable=1).insert(ignore_permissions=True).name

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("DocType", cls.doctype, force=True, ignore_permissions=True)
		super().tearDownClass()

	def _trigger(self, body):
		with patch(CTRL_MOD + ".is_safe_exec_enabled", return_value=True):
			doc = frappe.get_doc(
				{
					"doctype": TRIGGER,
					"trigger_name": f"blocking-{frappe.generate_hash(length=8)}",
					"target_doctype": self.doctype,
					"doc_events": ["validate", "before_submit"],
					"action_type": "Script",
					"script_body": body,
				}
			).insert(ignore_permissions=True)
		self._triggers.append(doc.name)
		engine.clear_cache()
		return doc

	def _new(self, value="multi-event test"):
		return frappe.get_doc({"doctype": self.doctype, "some_fieldname": value})

	def test_both_events_run_on_a_real_insert_and_submit(self):
		trig = self._trigger("x = 1")
		with patch(EXEC_FLAG, return_value=True):
			doc = self._new().insert(ignore_permissions=True)
			doc.submit()
		events = frappe.get_all(ACTIVITY, filters={"trigger": trig.name}, pluck="doc_event")
		# validate runs on the insert and again on the submit, then before_submit
		self.assertEqual(sorted(events), ["before_submit", "validate", "validate"])

	def test_a_throw_blocks_the_insert_on_validate(self):
		trig = self._trigger('frappe.throw("stopped by the multi-event trigger")')
		with patch(EXEC_FLAG, return_value=True), patch("frappe.enqueue") as enq:
			with self.assertRaises(frappe.ValidationError):
				self._new().insert(ignore_permissions=True)
		self.assertEqual(self._blocked(enq, trig.name), ["validate"])

	def test_a_throw_blocks_on_before_submit(self):
		trig = self._trigger('frappe.throw("stopped by the multi-event trigger")')
		doc = self._new()
		doc.docstatus = 1
		with patch(EXEC_FLAG, return_value=True), patch("frappe.enqueue") as enq:
			with self.assertRaises(frappe.ValidationError):
				engine.dispatch(doc, "before_submit")
		self.assertEqual(self._blocked(enq, trig.name), ["before_submit"])

	def test_a_submit_blocked_by_the_trigger_stays_a_draft(self):
		self._trigger('if doc.docstatus == 1:\n\tfrappe.throw("no submit")')
		with patch(EXEC_FLAG, return_value=True):
			doc = self._new().insert(ignore_permissions=True)
			with patch("frappe.enqueue"), self.assertRaises(frappe.ValidationError):
				doc.submit()
		self.assertEqual(frappe.db.get_value(self.doctype, doc.name, "docstatus"), 0)

	@staticmethod
	def _blocked(enq, trigger):
		return [
			c.kwargs.get("doc_event")
			for c in enq.call_args_list
			if c.args
			and c.args[0] == "jarvis.triggers.engine.write_activity"
			and c.kwargs.get("trigger") == trigger
			and c.kwargs.get("status") == "Blocked"
		]
