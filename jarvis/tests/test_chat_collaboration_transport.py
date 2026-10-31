"""All gateway send surfaces must enforce the shared physical-session fence."""

import unittest
from unittest.mock import Mock, patch

from jarvis.chat.agent_client import AgentSession
from jarvis.chat.relay_mux import LaneHandler, RelayMux
from jarvis.exceptions import AgentUnreachableError

KEY = "agent:main:subagent:dashboard:collab:" + "c" * 32


class TestCollaborationTransport(unittest.TestCase):
	def test_structured_rpc_rejection_identifies_the_rejected_method(self):
		session = AgentSession(Mock(), Mock())
		with (
			patch.object(session, "_send", return_value="rpc"),
			patch.object(
				session,
				"_recv",
				return_value={"type": "res", "id": "rpc", "ok": False, "error": {"code": "INVALID_REQUEST"}},
			),
			self.assertRaises(AgentUnreachableError) as caught,
		):
			session.chat_send(KEY, "request", "run", verified_session_id="physical-qa")
		self.assertEqual(getattr(caught.exception, "rejected_method", None), "chat.send")
		self.assertIsNone(getattr(AgentUnreachableError("lost socket"), "rejected_method", None))

	def test_one_shot_agent_rpc_cannot_send_a_shared_session(self):
		session = AgentSession(Mock(), Mock())
		with patch.object(session, "_send") as send, self.assertRaises(AgentUnreachableError):
			next(session.stream_agent_turn(KEY, "request", "logical-run"))
		send.assert_not_called()

	def test_mux_refuses_before_registering_an_unverified_shared_lane(self):
		mux = RelayMux.__new__(RelayMux)
		with patch.object(mux, "register_run") as register, patch.object(mux, "issue_rpc") as send:
			with self.assertRaises(AgentUnreachableError):
				mux.send_chat(KEY, "request", "logical-run", LaneHandler())
			register.assert_not_called()
			send.assert_not_called()

	def test_mux_send_carries_atomic_physical_identity_and_policy_fences(self):
		mux = RelayMux.__new__(RelayMux)
		with patch.object(mux, "register_run"), patch.object(mux, "issue_rpc") as send:
			mux.send_chat(KEY, "request", "logical-run", LaneHandler(), verified_session_id="physical-qa")
		params = send.call_args.args[1]
		self.assertEqual(params["sessionId"], "physical-qa")
		self.assertEqual(params["expectedPermissionMode"], "read-only")
		self.assertNotIn("expectedLeafEntryId", params)

	def test_direct_send_can_continue_a_nonempty_verified_session(self):
		session = AgentSession(Mock(), Mock())
		leaf = None

		def gateway(method, params, **options):
			nonlocal leaf
			if "expectedLeafEntryId" in params and params["expectedLeafEntryId"] != leaf:
				raise AgentUnreachableError("active-leaf-changed", code="INVALID_REQUEST")
			leaf = params["idempotencyKey"]
			return {"payload": {"runId": leaf}}

		with patch.object(session, "_request", side_effect=gateway):
			for run in ("parent", "confirmation-child"):
				self.assertEqual(
					session.chat_send(KEY, "request", run, verified_session_id="physical-qa")["runId"], run
				)

	def test_direct_send_observes_matching_trusted_tool_events_before_ack(self):
		frames = [
			{
				"type": "event",
				"event": "agent",
				"payload": {
					"runId": "other-run",
					"sessionKey": KEY,
					"stream": "item",
					"data": {
						"kind": "tool",
						"phase": "start",
						"name": "jarvis__get_doc",
						"toolCallId": "wrong-call",
					},
				},
			},
			{
				"type": "event",
				"event": "agent",
				"payload": {
					"runId": "logical-run",
					"sessionKey": KEY,
					"stream": "item",
					"data": {
						"kind": "tool",
						"phase": "start",
						"name": "jarvis__get_doc",
						"toolCallId": "trusted-call",
					},
				},
			},
			{"type": "res", "id": "request-qa", "ok": True, "payload": {"runId": "logical-run"}},
		]
		session = AgentSession(Mock(), Mock())
		observed = []
		with (
			patch.object(session, "_send", return_value="request-qa"),
			patch.object(session, "_recv", side_effect=frames),
		):
			ack = session.chat_send(
				KEY,
				"request",
				"logical-run",
				verified_session_id="physical-qa",
				event_observer=observed.append,
			)
		self.assertEqual(ack["runId"], "logical-run")
		self.assertEqual(len(observed), 1)
		self.assertEqual(observed[0]["tool_call_id"], "trusted-call")
		self.assertEqual(observed[0]["kind"], "tool")
