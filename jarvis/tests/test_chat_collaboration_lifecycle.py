"""Delete/retention retire capabilities, retaining immutable audit identity."""

import uuid
from unittest.mock import Mock, patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import turn_state as ts
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import OWNER, as_user


class TestCollaborationLifecycle(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		frappe.db.set_value(ts.TURN, self.turn.name, {"state": "done", "reserved": 0, "usage_recorded": 1})
		frappe.db.commit()

	def retire(self):
		from jarvis.chat.collaboration_lifecycle import retire_conversation

		return retire_conversation(self.conversation)

	def test_drained_retirement_removes_auth_but_preserves_binding_and_actor(self):
		self.assertTrue(self.retire())
		frappe.db.commit()
		self.assertFalse(frappe.db.exists(c.SESSION, {"collaboration_binding": self.bound.name}))
		row = frappe.get_doc(c.BINDING, self.bound.name)
		self.assertEqual(
			(row.state, row.actor, row.logical_run_id),
			("Retired", self.bound.actor, self.bound.logical_run_id),
		)
		with self.assertRaises(frappe.PermissionError):
			c.resolve_binding(self.bound.session_key)
		self.assertTrue(self.retire())

	def test_unknown_callback_and_pending_human_work_keep_retirement_closed(self):
		for values in (
			{"state": "recovering", "collaboration_dispatches": None},
			{"state": "done", "collaboration_dispatches": '{"a":{"state":"Running","tool":"get_doc"}}'},
		):
			frappe.db.set_value(ts.TURN, self.turn.name, values)
			frappe.db.commit()
			with self.assertRaises(frappe.ValidationError):
				self.retire()
			frappe.db.rollback()
			self.assertTrue(frappe.db.exists(c.SESSION, {"collaboration_binding": self.bound.name}))

	def test_delete_keeps_immutable_binding_evidence_without_live_capability(self):
		from jarvis.chat.api import _DELETED, _delete_idle_conversation

		name = self.conversation
		with as_user(OWNER):
			self.assertEqual(_delete_idle_conversation(name), _DELETED)
		frappe.db.commit()
		self.assertFalse(frappe.db.exists(c.CONVERSATION, name))
		self.assertTrue(frappe.db.exists(c.BINDING, self.bound.name))
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Retired")
		self.assertFalse(frappe.db.exists(c.SESSION, {"collaboration_binding": self.bound.name}))
		# The fixture may clean its own immutable ledger after this assertion.
		frappe.db.delete(c.BINDING, {"name": self.bound.name})
		frappe.db.commit()

	def test_delete_during_unknown_work_is_refused_before_turns_or_messages_are_removed(self):
		frappe.db.set_value(ts.TURN, self.turn.name, "state", "recovering")
		frappe.db.commit()
		before = frappe.db.count(ts.MSG, {"conversation": self.conversation})
		with self.assertRaisesRegex(frappe.ValidationError, "Finish|pending|unknown|reconcile"):
			frappe.delete_doc(c.CONVERSATION, self.conversation, ignore_permissions=True)
		frappe.db.rollback()
		self.assertTrue(frappe.db.exists(ts.TURN, self.turn.name))
		self.assertEqual(frappe.db.count(ts.MSG, {"conversation": self.conversation}), before)

	def test_retention_uses_existing_disabled_setting_without_retiring_anything(self):
		from jarvis.chat.collaboration_lifecycle import sweep

		with patch("jarvis.chat.session_lifecycle._retention_days", return_value=0):
			self.assertEqual(sweep(), {"retired": 0})
		self.assertTrue(frappe.db.exists(c.SESSION, {"collaboration_binding": self.bound.name}))

	def plant_decisions(self):
		action = frappe.get_doc(
			{
				"doctype": "Jarvis Pending Action",
				"name": uuid.uuid4().hex,
				"kind": "chat",
				"conversation": self.conversation,
				"status": "Pending",
				"owner_user": self.bound.actor,
				"exec_user": self.bound.actor,
				"actor": self.bound.actor,
				"collaboration_binding": self.bound.name,
				"run_id": self.turn.name,
				"tool": "create_doc",
			}
		)
		action.db_insert()
		question = frappe.get_doc(
			{
				"doctype": "Jarvis Approval Request",
				"name": uuid.uuid4().hex,
				"owner": self.bound.actor,
				"actor": self.bound.actor,
				"status": "Pending",
				"source": "Chat",
				"conversation": self.conversation,
				"collaboration_binding": self.bound.name,
				"collaboration_run_id": self.turn.name,
				"question": "Choose a warehouse",
			}
		)
		question.db_insert()
		frappe.db.commit()
		return action.name, question.name

	def test_current_decisions_block_retirement_and_are_never_executed(self):
		action, question = self.plant_decisions()
		with self.assertRaises(frappe.ValidationError):
			self.retire()
		frappe.db.rollback()
		self.assertEqual(frappe.db.get_value("Jarvis Pending Action", action, "status"), "Pending")
		self.assertEqual(frappe.db.get_value("Jarvis Approval Request", question, "status"), "Pending")

	def test_revoked_grant_decisions_retire_inertly_without_continuations(self):
		action, question = self.plant_decisions()
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.set_value(c.BINDING, self.bound.name, "pending_agent_notes", '[{"text":"old decision"}]')
		frappe.db.commit()
		with patch("frappe.enqueue") as enqueue:
			self.assertTrue(self.retire())
			frappe.db.commit()
			enqueue.assert_not_called()
		self.assertEqual(frappe.db.get_value("Jarvis Pending Action", action, "status"), "Cancelled")
		self.assertTrue(frappe.db.get_value("Jarvis Pending Action", action, "settled"))
		self.assertEqual(frappe.db.get_value("Jarvis Approval Request", question, "status"), "Dismissed")
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Retired")
		self.assertTrue(frappe.db.get_value(c.BINDING, self.bound.name, "pending_agent_notes"))

	def test_retention_retires_old_binding_in_an_active_conversation(self):
		from jarvis.chat.collaboration_lifecycle import sweep
		from jarvis.tests.race_harness import as_job

		frappe.db.set_value(c.BINDING, self.bound.name, "creation", frappe.utils.add_to_date(None, days=-40))
		frappe.db.commit()
		with as_job(), patch("jarvis.chat.session_lifecycle._retention_days", return_value=30):
			self.assertEqual(sweep(limit=1), {"retired": 1})
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Retired")
		self.assertEqual(frappe.db.get_value(c.CONVERSATION, self.conversation, "status"), "Active")

	def native_session(self, *, physical="physical-owned", active=False):
		frappe.db.set_value(c.BINDING, self.bound.name, "runtime_session_id", "physical-owned")
		self.retire()
		frappe.db.commit()
		sess = Mock()

		def request(method, params, **kwargs):
			self.assertEqual(frappe.db.transaction_writes, 0)
			self.assertEqual(params.get("key", self.bound.session_key), self.bound.session_key)
			if method == "sessions.list":
				return {
					"payload": {
						"sessions": [
							{"key": self.bound.session_key, "sessionId": physical, "hasActiveRun": active}
						]
					}
				}
			self.assertEqual(method, "sessions.delete")
			self.assertEqual(params, {"key": self.bound.session_key, "expectedSessionId": "physical-owned"})
			return {"ok": True}

		sess._request.side_effect = request
		return sess

	def test_native_cleanup_uses_the_exact_retired_uuid_without_database_locks(self):
		from jarvis.chat.collaboration_lifecycle import reclaim_runtime

		sess = self.native_session()
		self.assertTrue(reclaim_runtime(self.bound.name, sess))
		self.assertTrue(frappe.db.get_value(c.BINDING, self.bound.name, "runtime_retired_at"))
		self.assertEqual(sess._request.call_args_list[-1].args[0], "sessions.delete")
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "actor"), self.bound.actor)

	def test_native_cleanup_refuses_active_or_replaced_runtime(self):
		from jarvis.chat.collaboration_lifecycle import reclaim_runtime

		for physical, active in (("physical-owned", True), ("replacement-uuid", False)):
			sess = self.native_session(physical=physical, active=active)
			self.assertFalse(reclaim_runtime(self.bound.name, sess))
			self.assertEqual([call.args[0] for call in sess._request.call_args_list], ["sessions.list"])
			self.assertFalse(frappe.db.get_value(c.BINDING, self.bound.name, "runtime_retired_at"))

	def test_native_cleanup_recovers_when_delete_succeeded_but_its_ack_was_lost(self):
		from jarvis.chat.collaboration_lifecycle import reclaim_runtime

		sess = self.native_session()
		sess._request.side_effect = [
			{"payload": {"sessions": []}},
			{"payload": {"session": None}},
		]
		self.assertTrue(reclaim_runtime(self.bound.name, sess))
		self.assertEqual(
			[call.args[0] for call in sess._request.call_args_list], ["sessions.list", "sessions.describe"]
		)
		self.assertTrue(frappe.db.get_value(c.BINDING, self.bound.name, "runtime_retired_at"))

	def test_abandoned_unsent_provision_is_reconciled_before_exact_uuid_cleanup(self):
		from jarvis.chat.collaboration_lifecycle import reclaim_runtime

		sess = self.native_session()
		frappe.db.set_value(c.BINDING, self.bound.name, "runtime_session_id", None)
		frappe.db.set_value(ts.TURN, self.turn.name, "collaboration_send_started_at", None)
		frappe.db.commit()
		self.assertTrue(reclaim_runtime(self.bound.name, sess))
		self.assertEqual(
			frappe.db.get_value(c.BINDING, self.bound.name, "runtime_session_id"), "physical-owned"
		)
		self.assertTrue(frappe.db.get_value(c.BINDING, self.bound.name, "runtime_retired_at"))

	def test_missing_provision_uuid_never_reconciles_a_possibly_sent_run(self):
		from jarvis.chat.collaboration_lifecycle import reclaim_runtime

		sess = self.native_session()
		frappe.db.set_value(c.BINDING, self.bound.name, "runtime_session_id", None)
		frappe.db.commit()
		self.assertFalse(reclaim_runtime(self.bound.name, sess))
		sess._request.assert_not_called()
