"""A lost callback receipt and an unconsumed discard note eventually drain."""

import json
import uuid

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import turn_state as ts
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests.race_harness import as_job


class TestCollaborationClaimRecovery(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		frappe.db.set_value(ts.TURN, self.turn.name, {"state": "done", "reserved": 0, "usage_recorded": 1})
		frappe.db.commit()

	def plant(self, *, age=2000, state="done"):
		ledger = {
			"call-1": {
				"tool": "get_schema",
				"state": "Running",
				"started_at": str(frappe.utils.add_to_date(None, seconds=-age)),
			}
		}
		frappe.db.set_value(
			ts.TURN, self.turn.name, {"state": state, "collaboration_dispatches": json.dumps(ledger)}
		)
		frappe.db.commit()

	def reconcile(self):
		from jarvis.chat.collaboration_tool_claims import reconcile

		with as_job():
			return reconcile(limit=10, conversation=self.conversation)

	def test_terminal_abandoned_read_claim_drains_without_reexecuting(self):
		from jarvis.chat.collaboration_tool_claims import unsettled

		self.plant()
		self.assertEqual(self.reconcile(), {"reconciled": 1})
		raw = frappe.db.get_value(ts.TURN, self.turn.name, "collaboration_dispatches")
		self.assertEqual(json.loads(raw)["call-1"]["state"], "Abandoned")
		self.assertFalse(unsettled(raw))
		self.assertEqual(self.reconcile(), {"reconciled": 0})

	def test_recent_or_nonterminal_claim_keeps_retirement_closed(self):
		from jarvis.chat.collaboration_tool_claims import unsettled

		for age, state in ((0, "done"), (2000, "streaming")):
			self.plant(age=age, state=state)
			self.assertEqual(self.reconcile(), {"reconciled": 0})
			self.assertTrue(
				unsettled(frappe.db.get_value(ts.TURN, self.turn.name, "collaboration_dispatches"))
			)

	def test_park_commit_gap_links_the_action_but_never_executes_it(self):
		import hashlib

		self.plant()
		ledger = json.loads(frappe.db.get_value(ts.TURN, self.turn.name, "collaboration_dispatches"))
		ledger["call-1"]["tool"] = "create_doc"
		frappe.db.set_value(ts.TURN, self.turn.name, "collaboration_dispatches", json.dumps(ledger))
		action = frappe.get_doc(
			{
				"doctype": "Jarvis Pending Action",
				"name": uuid.uuid4().hex,
				"kind": "chat",
				"conversation": self.conversation,
				"status": "Executing",
				"actor": self.bound.actor,
				"owner_user": self.bound.actor,
				"exec_user": self.bound.actor,
				"collaboration_binding": self.bound.name,
				"run_id": self.turn.name,
				"tool": "create_doc",
				"collaboration_call_key": hashlib.sha256((self.bound.name + "\0call-1").encode()).hexdigest(),
			}
		)
		action.db_insert()
		frappe.db.commit()
		self.assertEqual(self.reconcile(), {"reconciled": 0})
		frappe.db.set_value("Jarvis Pending Action", action.name, "status", "Pending")
		frappe.db.commit()
		self.assertEqual(self.reconcile(), {"reconciled": 1})
		entry = json.loads(frappe.db.get_value(ts.TURN, self.turn.name, "collaboration_dispatches"))["call-1"]
		self.assertEqual(entry["pending_action"], action.name)
		self.assertEqual(frappe.db.get_value("Jarvis Pending Action", action.name, "status"), "Pending")
		self.assertTrue(c.has_undrained_work(conversation=self.conversation))

	def test_late_actual_receipt_replaces_abandoned_audit_without_a_new_call(self):
		from jarvis.chat.collaboration_bindings import insert_message
		from jarvis.chat.collaboration_callbacks import CallbackPermit
		from jarvis.chat.collaboration_tool_claims import complete, unsettled
		from jarvis.chat.collaboration_turn import TurnContext

		self.plant()
		self.assertEqual(self.reconcile(), {"reconciled": 1})
		message = insert_message(
			self.bound, {"role": "tool", "content": "Late actual read result"}, settled_run_id=self.turn.name
		)
		frappe.db.commit()
		permit = CallbackPermit(TurnContext(self.bound, self.turn.name), "get_schema", "call-1")
		with as_job():
			complete(permit, message.name)
		raw = frappe.db.get_value(ts.TURN, self.turn.name, "collaboration_dispatches")
		self.assertEqual(json.loads(raw)["call-1"]["state"], "Done")
		self.assertEqual(json.loads(raw)["call-1"]["message"], message.name)
		self.assertFalse(unsettled(raw))

	def test_current_discard_note_is_audit_after_its_last_consumer_drains(self):
		from jarvis.chat.collaboration_lifecycle import retire_conversation

		notes = '[{"text":"Action discarded; never treat it as approved."}]'
		frappe.db.set_value(c.BINDING, self.bound.name, "pending_agent_notes", notes)
		frappe.db.commit()
		self.assertFalse(c.has_undrained_work(conversation=self.conversation))
		self.assertTrue(retire_conversation(self.conversation))
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Retired")
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "pending_agent_notes"), notes)
