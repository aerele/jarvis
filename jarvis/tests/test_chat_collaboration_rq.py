"""The RQ entry point must use the same actor, context and delivery fences."""

import json
import uuid
from contextlib import contextmanager
from unittest.mock import Mock, patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import pump, worker
from jarvis.chat import turn_state as ts
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests._share_helpers import OUTSIDER, OWNER, RECIPIENT, as_user
from jarvis.tests.race_harness import as_job
from jarvis.tests.test_chat_collaboration_runtime_plan import PlanningGateway


def setUpModule():
	install_synthetic_runtime_profile()


class RQGateway(PlanningGateway):
	fail_ack = False
	before_reply = None
	before_tool = None
	reply = "Actor reply"

	def __init__(self):
		super().__init__()
		self.aborts = []
		self.history = []
		self.active = False
		self.last_run = None

	def list_sessions(self, **options):
		return [
			{
				"key": self.key,
				"sessionId": self.entry["sessionId"],
				"lastRunId": self.last_run,
				"hasActiveRun": self.active,
				"totalTokensFresh": True,
				"inputTokens": 0,
				"outputTokens": 0,
				"contextTokens": self.capacity,
				"model": self.model,
				"modelProvider": "provider",
			}
		]

	def get_session_messages(self, key, **options):
		return self.history

	def chat_abort(self, key, run_id, **options):
		self.aborts.append((key, run_id))
		return {"aborted": True}

	def subscribe_session(self, key, **options):
		return {}

	def chat_send(self, key, message, run_id, **options):
		self.sends.append((key, message, run_id, options))
		self.last_run = run_id
		if self.before_tool:
			self.before_tool()
		observer = options.get("event_observer")
		if observer:
			observer(
				{
					"kind": "tool",
					"phase": "start",
					"tool_name": "jarvis__get_schema",
					"tool_call_id": "rq-observed" if len(self.sends) == 1 else "rq-observed-" + run_id,
				}
			)
		if self.fail_ack:
			raise AgentUnreachableError("chat.send timed out", code="ack-timeout")
		return {"runId": run_id}

	def relay_turn_events(self, key, run_id, **options):
		if self.before_reply:
			self.before_reply()
		yield {"kind": "assistant", "text": self.reply, "delta": self.reply}
		if self.aborts:
			yield {"kind": "relay:error", "state": "aborted", "text": self.reply}
		else:
			yield {"kind": "relay:final", "text": self.reply}


class RQCase(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		self.target = "collaboration-rq-" + uuid.uuid4().hex[:12]
		ts._ensure_control_row(self.target)
		frappe.db.set_value(ts.PUMP, self.target, "transport_mode", "legacy")
		frappe.db.set_value(
			ts.TURN,
			self.turn.name,
			{
				"relay_target_id": self.target,
				"reserved": 1,
				"collaboration_executor": "rq",
				"collaboration_send_started_at": None,
			},
		)
		frappe.db.set_value(
			c.CONVERSATION,
			self.conversation,
			{
				"session_key": "OWNER_PRIVATE_SESSION",
				"model_override": "OWNER_PRIVATE_MODEL",
				"thinking_override": "high",
			},
		)
		frappe.db.commit()
		self.gateway = RQGateway()
		self.enqueue_finalize = Mock()
		self.notice_jobs = Mock()

		def enqueue(*args, **kwargs):
			method = args[0] if args else kwargs.get("method")
			if method == "jarvis.chat.collaboration_realtime.publish_notice":
				return self.notice_jobs(*args, **kwargs)
			return self.enqueue_finalize(*args, **kwargs)

		@contextmanager
		def checkout(*args, **kwargs):
			yield self.gateway

		for item in (
			patch("jarvis.chat.agent_session_pool.checkout", checkout),
			patch("jarvis.chat.policy.validate_can_send", return_value=(True, None)),
			patch("frappe.publish_realtime"),
			patch("frappe.enqueue", side_effect=enqueue),
			patch.dict(
				frappe.conf,
				{"chat_collaboration_model_budgets": {"provider/actor-model": {"runtime_reserve": 1024}}},
			),
		):
			item.start()
			self.addCleanup(item.stop)

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.delete(ts.PUMP, {"name": self.target})
		frappe.db.delete("Jarvis Turn Effect", {"turn": self.turn.name})
		frappe.db.commit()
		super().tearDown()

	def drive(self):
		with as_job():
			worker.run_agent_turn(self.conversation, self.turn.seed_message, self.turn.name)


class TestCollaborationRQ(RQCase):
	def test_definite_send_rejection_finishes_without_unknown_recovery(self):
		error = AgentUnreachableError("planted gateway rejection", code="INVALID_REQUEST")
		error.rejected_method = "chat.send"
		with patch.object(self.gateway, "chat_send", side_effect=error):
			self.drive()
		row = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual(row.state, "errored")
		self.assertTrue(row.collaboration_send_started_at)
		self.assertFalse(row.collaboration_retry_allowed)
		self.assertFalse(row.recovery_started_at)
		self.assertEqual(frappe.db.get_value(c.BINDING, self.bound.name, "state"), "Active")

	def test_stop_preserves_the_actual_aborted_reply_without_secret_streaming(self):
		from jarvis.chat.collaboration_api import stop_run

		self.gateway.reply = "Useful completed portion api_key=SECRET_SENTINEL"

		def stop():
			with as_user(RECIPIENT):
				stop_run(self.conversation, self.turn.name)

		self.gateway.before_reply = stop
		self.drive()
		row = frappe.get_doc(ts.TURN, self.turn.name)
		content = frappe.db.get_value(ts.MSG, row.assistant_message, "content")
		self.assertIn("Useful completed portion", content)
		self.assertNotIn("SECRET_SENTINEL", content)
		self.assertEqual(row.state, "cancelled")
		self.assertEqual(len(self.gateway.sends), 1)

	def test_rq_send_and_reply_are_bound_to_recipient_and_canonical_runtime(self):
		self.drive()
		self.assertEqual(len(self.gateway.sends), 1)
		key, message, run, options = self.gateway.sends[0]
		self.assertEqual((key, run), (self.bound.session_key, self.turn.name))
		self.assertEqual(options["verified_session_id"], "physical-1")
		self.assertIn("chat user: " + RECIPIENT, message)
		self.assertNotIn("OWNER_PRIVATE", message)
		turn = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual(turn.state, "finalizing")
		self.assertTrue(turn.collaboration_send_started_at)
		self.assertEqual(json.loads(turn.collaboration_tools)["rq-observed"], "get_schema")
		row = frappe.get_doc("Jarvis Chat Message", turn.assistant_message)
		self.assertEqual(
			(row.actor, row.owner, row.collaboration_binding), (RECIPIENT, RECIPIENT, self.bound.name)
		)
		self.assertEqual(row.content, "Actor reply")
		self.assertFalse(row.streaming)

	def test_duplicate_rq_job_never_sends_a_second_model_request(self):
		self.drive()
		self.drive()
		self.assertEqual(len(self.gateway.sends), 1)

	def test_revoked_request_fails_without_gateway_or_owner_fallback(self):
		frappe.db.delete(c.SHARE, {"name": self.share})
		frappe.db.commit()
		self.drive()
		self.assertEqual(self.gateway.calls, [])
		self.assertEqual(self.gateway.sends, [])
		turn = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertIn(turn.state, {"errored", "cancelled"})
		self.assertFalse(turn.reserved)

	def test_ambiguous_ack_keeps_identity_and_never_blindly_resends(self):
		self.gateway.fail_ack = True
		self.drive()
		self.drive()
		self.assertEqual(len(self.gateway.sends), 1)
		turn = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual(turn.collaboration_binding, self.bound.name)
		self.assertEqual(turn.state, "recovering")

	def test_stop_before_send_claim_keeps_seed_and_makes_no_model_request(self):
		from jarvis.chat import collaboration_api as api
		from jarvis.chat import collaboration_rq

		original = collaboration_rq._claim_send
		stops = []

		def stop_then_claim(*args, **kwargs):
			with as_user(RECIPIENT):
				stops.append(api.stop_run(self.conversation, self.turn.name))
			return original(*args, **kwargs)

		with patch.object(collaboration_rq, "_claim_send", stop_then_claim):
			self.drive()
		self.assertEqual(len(stops), 1)
		self.assertTrue(stops[0]["ok"])
		self.assertEqual(self.gateway.sends, [])
		turn = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual(turn.state, "cancelled")
		self.assertFalse(turn.reserved)
		self.assertTrue(frappe.db.exists(ts.MSG, turn.seed_message))

	def test_stop_after_ack_aborts_own_exact_run_without_usage_or_resend(self):
		from jarvis.chat import collaboration_api as api

		stops = []

		def stop():
			with as_user(RECIPIENT):
				stops.append(api.stop_run(self.conversation, self.turn.name))

		self.gateway.before_reply = stop
		self.drive()
		self.assertEqual(len(stops), 1)
		self.assertTrue(stops[0]["ok"])
		self.assertEqual(self.gateway.aborts, [(self.bound.session_key, self.turn.name)])
		turn = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual(turn.state, "cancelled")
		self.assertFalse(turn.reserved)
		self.assertFalse(turn.usage_recorded)
		self.assertEqual(len(self.gateway.sends), 1)

	def test_owner_can_stop_a_dispatched_request_after_revoking_its_actor(self):
		from jarvis.chat import collaboration_api as api

		def revoke_then_stop():
			frappe.db.delete(c.SHARE, {"name": self.share})
			frappe.db.commit()
			with as_user(OWNER):
				api.stop_run(self.conversation, self.turn.name)

		self.gateway.before_reply = revoke_then_stop
		self.drive()
		self.assertEqual(self.gateway.aborts, [(self.bound.session_key, self.turn.name)])
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, "state"), "cancelled")

	def test_foreign_participant_cannot_stop_another_actors_request(self):
		from jarvis.chat import collaboration_api as api

		before = frappe.db.get_value(ts.TURN, self.turn.name, ["state", "version"])
		with as_user(OUTSIDER), self.assertRaises(frappe.PermissionError):
			api.stop_run(self.conversation, self.turn.name)
		self.assertEqual(frappe.db.get_value(ts.TURN, self.turn.name, ["state", "version"]), before)
		self.assertEqual(self.gateway.aborts, [])

	def test_observer_database_failure_after_send_is_unknown_and_never_replayed(self):
		with patch(
			"jarvis.chat.collaboration_callbacks.record_gateway_tool", side_effect=RuntimeError("planted")
		):
			self.drive()
		self.drive()
		turn = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertTrue(turn.collaboration_send_started_at)
		self.assertEqual(turn.state, "recovering")
		self.assertEqual(len(self.gateway.sends), 1)

	def test_non_job_delivery_never_claims_or_cancels_a_shared_request(self):
		worker.run_agent_turn(self.conversation, self.turn.seed_message, self.turn.name)
		turn = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual(turn.state, "dispatching")
		self.assertFalse(turn.preparing_at)
		self.assertEqual(self.gateway.calls, [])

	def test_python_realtime_backend_still_dispatches_shared_legacy_through_rq(self):
		from jarvis.chat import api, dispatch

		with (
			patch.dict(frappe.conf, {"socketio_backend": "python"}),
			patch.object(dispatch, "subscriber_count", return_value=1),
			patch.object(dispatch, "publish_chat_send") as publish,
		):
			api._dispatch_turn(
				{
					"conversation_id": self.conversation,
					"message_id": self.turn.seed_message,
					"run_id": self.turn.name,
				}
			)
			frappe.db.commit()
		self.enqueue_finalize.assert_called_once()
		self.assertEqual(
			self.enqueue_finalize.call_args.kwargs["method"], "jarvis.chat.worker.run_agent_turn"
		)
		publish.assert_not_called()

	def test_wrong_job_seed_does_not_cancel_or_mutate_a_legitimate_request(self):
		with as_job():
			worker.run_agent_turn(self.conversation, "not-the-seed", self.turn.name)
		turn = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual(turn.state, "dispatching")
		self.assertFalse(turn.preparing_at)
		self.assertEqual(self.gateway.calls, [])

	def test_terminal_metadata_is_scrubbed_before_durable_storage(self):
		self.gateway.reply = "Completed api_key=SECRET_SENTINEL"
		self.drive()
		turn = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertEqual(turn.state, "finalizing")
		self.assertNotIn("SECRET_SENTINEL", turn.terminal_payload)
		self.assertNotIn("SECRET_SENTINEL", frappe.db.get_value(ts.MSG, turn.assistant_message, "content"))

	def test_changed_durable_reply_pointer_never_licenses_a_tool_callback(self):
		def replace_pointer():
			frappe.db.set_value(ts.TURN, self.turn.name, "assistant_message", self.turn.seed_message)
			frappe.db.commit()

		self.gateway.before_tool = replace_pointer
		self.drive()
		turn = frappe.get_doc(ts.TURN, self.turn.name)
		self.assertFalse(turn.collaboration_tools)
