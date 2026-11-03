"""Shared dispatch checks asynchronously, then claims as the immutable actor."""

import json
import uuid
from unittest.mock import Mock, patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import pump
from jarvis.chat import turn_state as ts
from jarvis.chat.collaboration_runtime import RuntimePolicy, provision
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import RECIPIENT
from jarvis.tests.test_chat_collaboration_runtime import Gateway


class Future:
	done = False

	def __init__(self, response):
		self.response = response

	def result(self, timeout=0):
		if not self.done:
			raise AssertionError("A reactor must not block on an unready policy RPC")
		return self.response


class TestCollaborationPump(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		from jarvis.chat.collaboration_bindings import insert_message

		# Real preparation always creates the bound placeholder before ready.
		self.reply = insert_message(self.bound, {"role": "assistant", "content": "", "streaming": 1})
		frappe.db.set_value(ts.TURN, self.turn.name, "assistant_message", self.reply.name)
		frappe.db.set_value(ts.TURN, self.turn.name, "collaboration_send_started_at", None)
		self.gateway = Gateway()
		self.policy = RuntimePolicy("/workspace/skills", "provider/actor-model")
		physical = provision(self.gateway, self.bound, self.policy)
		self.target = "collab-qa-" + uuid.uuid4().hex[:12]
		ts._ensure_control_row(self.target)
		payload = {
			"session_key": self.bound.session_key,
			"message": "actor request",
			"chat_user": RECIPIENT,
			"collaboration_runtime": {
				"skills_root": self.policy.skills_root,
				"model_ref": self.policy.model_ref,
				"physical_id": physical,
				"context_tokens": 8192,
				"runtime_reserve": 1024,
			},
		}
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"state": "ready",
				"relay_target_id": self.target,
				"reserved": 1,
				"collaboration_send_started_at": None,
				"dispatch_payload": json.dumps(payload),
			},
		)
		frappe.db.commit()
		won, epoch = ts.lease_acquire(self.target, "shared-test-holder")
		self.assertTrue(won)
		self.context = pump.PumpContext(
			self.target, epoch, "shared-test-holder", 0, frappe.local.site, pump.PumpDeps()
		)
		self.mux = self.context.mux = Mock()

		def issue(method, params, **kwargs):
			self.preflight = Future(self.gateway._request(method, params, **kwargs))
			return self.preflight

		self.mux.issue_rpc.side_effect = issue
		self.mux.send_chat.return_value = Future({"payload": {"runId": self.turn.name}})

	def tearDown(self):
		frappe.db.delete(ts.PUMP, {"name": self.target})
		frappe.db.commit()
		super().tearDown()

	def start_check(self):
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		self.mux.send_chat.assert_not_called()
		self.preflight.done = True

	def test_policy_read_never_blocks_the_reactor_and_send_is_actor_bound(self):
		self.start_check()
		self.assertTrue(pump._dispatch_one(self.context, self.turn.name))
		args, kwargs = self.mux.send_chat.call_args
		self.assertEqual(args[0], self.bound.session_key)
		self.assertEqual(args[2], self.turn.name)
		self.assertEqual(kwargs["verified_session_id"], "physical-1")
		self.assertEqual(self.context.runs[self.turn.name].owner, RECIPIENT)
		self.assertTrue(frappe.db.get_value(ts.TURN, self.turn.name, "collaboration_send_started_at"))

	def test_transient_preflight_keeps_the_unsent_request_retryable(self):
		self.start_check()
		self.preflight.result = Mock(side_effect=AgentUnreachableError("temporary", code="ack-timeout"))
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		row = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertFalse(row.collaboration_send_started_at)
		self.assertTrue(row.collaboration_retry_allowed)
		self.mux.send_chat.assert_not_called()

	def test_actual_stop_terminal_survives_the_cancel_version_change(self):
		self.start_check()
		self.assertTrue(pump._dispatch_one(self.context, self.turn.name))
		self.mux.send_chat.return_value.done = True
		pump._poll_acks(self.context)
		rs = self.context.runs[self.turn.name]
		self.assertTrue(ts.request_cancel(self.turn.name, rs.version))
		frappe.db.commit()
		with patch("frappe.enqueue"), patch("frappe.publish_realtime"):
			handler = self.mux.send_chat.call_args.args[3]
			handler.on_terminal(
				"relay:error", {"state": "aborted", "text": "Useful stopped text api_key=SECRET_SENTINEL"}
			)
		row = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual(row.state, "cancelled")
		content = frappe.db.get_value(ts.MSG, row.assistant_message, "content")
		self.assertIn("Useful stopped text", content)
		self.assertNotIn("SECRET_SENTINEL", content)
		self.assertNotIn("SECRET_SENTINEL", row.terminal_payload)

	def test_revoke_between_preflight_and_claim_prevents_model_send(self):
		self.start_check()
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		self.mux.send_chat.assert_not_called()
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")

	def test_changed_native_policy_is_never_sent_or_reapplied(self):
		self.gateway.entry["inheritedToolAllow"] = ["*"]
		self.start_check()
		self.assertFalse(pump._dispatch_one(self.context, self.turn.name))
		self.mux.send_chat.assert_not_called()
		self.assertEqual(self.gateway.entry["inheritedToolAllow"], ["*"])

	def test_actual_fenced_tool_event_registers_the_callback_run(self):
		from jarvis.chat.collaboration_callbacks import authorize_callback

		self.start_check()
		self.assertTrue(pump._dispatch_one(self.context, self.turn.name))
		self.mux.send_chat.return_value.done = True
		pump._poll_acks(self.context)
		handler = self.mux.send_chat.call_args.args[3]
		handler.on_tool(
			{
				"phase": "start",
				"tool_name": "jarvis__get_schema",
				"tool_call_id": "pump-observed",
				"event_seq": 1,
			}
		)
		proof = authorize_callback(self.bound.session_key, "get_schema", "pump-observed", wait_s=0)
		self.assertEqual((proof.binding.actor, proof.run_id), (RECIPIENT, self.turn.name))

	def test_early_tool_event_is_correlated_while_the_send_ack_is_pending(self):
		from jarvis.chat.collaboration_callbacks import authorize_callback

		self.start_check()
		self.assertTrue(pump._dispatch_one(self.context, self.turn.name))
		handler = self.mux.send_chat.call_args.args[3]
		handler.on_tool(
			{
				"phase": "start",
				"tool_name": "jarvis__get_schema",
				"tool_call_id": "early-observed",
				"event_seq": 1,
			}
		)
		proof = authorize_callback(self.bound.session_key, "get_schema", "early-observed", wait_s=0)
		self.assertEqual(proof.run_id, self.turn.name)
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "dispatching")
		frappe.db.rollback()  # the independently authorized callback request ends
		self.mux.send_chat.return_value.done = True
		pump._poll_acks(self.context)
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "streaming")

	def test_note_cleanup_failure_keeps_ack_streaming_and_logs_no_raw_error(self):
		self.start_check()
		self.assertTrue(pump._dispatch_one(self.context, self.turn.name))
		self.mux.send_chat.return_value.done = True
		with (
			patch(
				"jarvis.chat.collaboration_notes.clear_after_ack", side_effect=RuntimeError("SECRET_SENTINEL")
			),
			patch("frappe.log_error") as log,
		):
			pump._poll_acks(self.context)
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "streaming")
		log.assert_called_once()
		self.assertNotIn("SECRET_SENTINEL", str(log.call_args))
