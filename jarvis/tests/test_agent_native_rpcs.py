"""Unit tests for the openclaw-native RPC methods on AgentSession.

These mirror openclaw's own UI gateway model (chat.send + chat.history +
sessions.list/get), so the bench can drive turns and reconcile from the
durable transcript instead of holding the agent RPC's request stream.

Each method is request/response, so we bypass __init__ and stub _request.
"""

from frappe.tests.utils import FrappeTestCase

from jarvis.chat.agent_client import AgentSession


class TestOpenclawNativeRpcs(FrappeTestCase):
	def _sess(self, response):
		sess = AgentSession.__new__(AgentSession)  # bypass __init__/WS
		captured = {"response": response}

		def fake_request(method, params, *, timeout_s):
			captured["method"] = method
			captured["params"] = params
			return captured["response"]

		sess._request = fake_request
		return sess, captured

	def test_chat_send_required_params_and_returns_payload(self):
		sess, cap = self._sess({"ok": True, "payload": {"runId": "r1", "status": "in_flight"}})
		out = sess.chat_send("sk", "hi", "idem1", thinking="low")
		self.assertEqual(cap["method"], "chat.send")
		self.assertEqual(cap["params"]["sessionKey"], "sk")
		self.assertEqual(cap["params"]["message"], "hi")
		self.assertEqual(cap["params"]["idempotencyKey"], "idem1")
		self.assertEqual(cap["params"]["deliver"], False)
		self.assertEqual(cap["params"]["thinking"], "low")
		self.assertEqual(out, {"runId": "r1", "status": "in_flight"})

	def test_chat_send_omits_optional_when_unset(self):
		sess, cap = self._sess({"ok": True, "payload": {"runId": "r2"}})
		sess.chat_send("sk", "hi", "idem2")
		self.assertNotIn("thinking", cap["params"])
		self.assertNotIn("attachments", cap["params"])

	def test_get_history_params_and_payload(self):
		sess, cap = self._sess(
			{
				"ok": True,
				"payload": {
					"sessionId": "s1",
					"messages": [{"role": "assistant"}],
					"thinkingLevel": "medium",
				},
			}
		)
		out = sess.get_history("sk", limit=50)
		self.assertEqual(cap["method"], "chat.history")
		self.assertEqual(cap["params"], {"sessionKey": "sk", "limit": 50, "maxChars": 4000})
		self.assertEqual(out["messages"], [{"role": "assistant"}])
		self.assertEqual(out["thinkingLevel"], "medium")

	def test_get_session_messages_returns_messages_list(self):
		sess, cap = self._sess({"ok": True, "payload": {"messages": [{"role": "assistant", "content": "x"}]}})
		out = sess.get_session_messages("sk")
		self.assertEqual(cap["method"], "sessions.get")
		self.assertEqual(cap["params"]["key"], "sk")
		self.assertEqual(out, [{"role": "assistant", "content": "x"}])

	def test_get_session_messages_empty(self):
		sess, _ = self._sess({"ok": True, "payload": {}})
		self.assertEqual(sess.get_session_messages("sk"), [])

	def test_is_run_active_matches_session_key(self):
		sess, cap = self._sess(
			{
				"ok": True,
				"payload": {
					"sessions": [
						{"key": "other", "hasActiveRun": True},
						{"key": "sk", "hasActiveRun": True},
					]
				},
			}
		)
		self.assertTrue(sess.is_run_active("sk"))

	def test_is_run_active_false_when_done_or_absent(self):
		sess, cap = self._sess({"ok": True, "payload": {"sessions": [{"key": "sk", "hasActiveRun": False}]}})
		self.assertFalse(sess.is_run_active("sk"))
		cap["response"] = {"ok": True, "payload": {"sessions": []}}
		self.assertFalse(sess.is_run_active("sk"))

	def test_stream_agent_turn_tags_midstream_drop_as_turn_timeout(self):
		# A WS drop AFTER the run is acked must surface code="turn-timeout" so
		# turn_handler parks it for recovery instead of false-erroring (#4).
		from jarvis.chat.agent_client import AgentUnreachableError

		sess = AgentSession.__new__(AgentSession)
		frames = [{"type": "res", "id": "a1", "ok": True, "payload": {"runId": "r1"}}]
		sess._send = lambda method, params: "a1"

		def fake_recv(_timeout):
			if frames:
				return frames.pop(0)
			raise AgentUnreachableError("ws closed mid-stream")

		sess._recv = fake_recv
		with self.assertRaises(AgentUnreachableError) as ctx:
			list(sess.stream_agent_turn("sk", "hi", "idem"))
		self.assertEqual(getattr(ctx.exception, "code", None), "turn-timeout")

	def test_stream_agent_turn_propagates_preack_failure_uncoded(self):
		# A failure BEFORE the ack (run never started) must NOT be turn-timeout,
		# so turn_handler errors it rather than parking a non-existent run.
		from jarvis.chat.agent_client import AgentUnreachableError

		sess = AgentSession.__new__(AgentSession)
		sess._send = lambda method, params: "a1"

		def fake_recv(_timeout):
			raise AgentUnreachableError("ws closed before ack")

		sess._recv = fake_recv
		with self.assertRaises(AgentUnreachableError) as ctx:
			list(sess.stream_agent_turn("sk", "hi", "idem"))
		self.assertNotEqual(getattr(ctx.exception, "code", None), "turn-timeout")

	def test_subscribe_session_params_and_payload(self):
		sess, cap = self._sess({"ok": True, "payload": {"subscribed": True}})
		out = sess.subscribe_session("sk")
		self.assertEqual(cap["method"], "sessions.messages.subscribe")
		self.assertEqual(cap["params"], {"key": "sk"})
		self.assertEqual(out, {"subscribed": True})
