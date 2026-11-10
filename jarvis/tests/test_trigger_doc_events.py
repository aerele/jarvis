"""One trigger, several doc events (jarvis-admin-v2#825).

``Jarvis Trigger.doc_events`` lists every event a trigger runs on, one per line;
``doc_event`` stays as the first of them. Covers the parsing helpers, the
controller's normalisation (including callers that only know ``doc_event``), the
LLM event check, the engine map and real dispatch on each event, and the
duplicate guard (#816) on overlapping event sets.
"""

from unittest.mock import patch

import frappe

from jarvis.tests.test_triggers_engine import CTRL_MOD, EXEC_FLAG, TRIGGER, _TriggerTestCase
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
		trig = self._script_trigger("on_update\non_trash")
		with patch(EXEC_FLAG, return_value=True):
			todo = self._make_todo()
			engine.dispatch(todo, "on_update")
			engine.dispatch(todo, "on_trash")
			engine.dispatch(todo, "after_insert")  # not one of its events
		rows = frappe.get_all(
			"Jarvis Trigger Activity",
			filters={"trigger": trig.name},
			fields=["doc_event", "status"],
			order_by="creation asc",
		)
		# the real hooks also fire on_update when the ToDo is inserted
		events = [r.doc_event for r in rows]
		self.assertIn("on_trash", events)
		self.assertIn("on_update", events)
		self.assertNotIn("after_insert", events)
		self.assertTrue(all(r.status == "Success" for r in rows))

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
