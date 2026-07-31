"""Unit tests for OpenclawSession.relay_turn_events + set_session_model.

Mirror of test_openclaw_native_rpcs.py's harness: bypass __init__ via
OpenclawSession.__new__ and stub _recv (fed a scripted frame list; an
Exception instance in the list is raised) / _request.

relay_turn_events is the consumer half of the openclaw-native turn model:
token/tool streaming comes from broadcast "agent" event frames retagged
with the chat.send clientRunId == run_id; completion comes ONLY from the
run-scoped "chat" event (state final|aborted|error). agent lifecycle
frames are dropped.
"""

from frappe.tests.utils import FrappeTestCase

from jarvis.chat.openclaw_client import OpenclawSession
from jarvis.exceptions import OpenclawUnreachableError


def _agent_frame(run_id, stream, data):
	return {
		"type": "event",
		"event": "agent",
		"payload": {
			"runId": run_id,
			"stream": stream,
			"data": data,
		},
	}


def _chat_frame(run_id, session_key, state, **extra):
	payload = {"runId": run_id, "sessionKey": session_key, "state": state}
	payload.update(extra)
	return {"type": "event", "event": "chat", "payload": payload}


class TestRelayTurnEvents(FrappeTestCase):
	def _sess(self, frames):
		sess = OpenclawSession.__new__(OpenclawSession)  # bypass __init__/WS
		queue = list(frames)

		def fake_recv(_timeout):
			frame = queue.pop(0)
			if isinstance(frame, Exception):
				raise frame
			return frame

		sess._recv = fake_recv
		return sess

	def test_assistant_and_tool_frames_then_final_joined_text(self):
		sess = self._sess(
			[
				_agent_frame("r1", "assistant", {"text": "Hi there", "delta": "there"}),
				_agent_frame(
					"r1",
					"item",
					{
						"kind": "tool",
						"phase": "start",
						"name": "read_file",
						"toolCallId": "tc1",
					},
				),
				_chat_frame(
					"r1",
					"sk",
					"final",
					message={
						"content": [{"type": "text", "text": "Hi there"}],
					},
				),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(out[0], {"kind": "assistant", "text": "Hi there", "delta": "there"})
		self.assertEqual(
			out[1],
			{
				"kind": "tool",
				"phase": "start",
				"tool_name": "read_file",
				"tool_call_id": "tc1",
			},
		)
		self.assertEqual(out[2], {"kind": "relay:final", "text": "Hi there"})
		self.assertEqual(len(out), 3)

	def test_discards_frames_for_other_run_or_session_and_non_event_frames(self):
		sess = self._sess(
			[
				_agent_frame("other-run", "assistant", {"text": "nope", "delta": "nope"}),
				{"type": "res", "id": "x", "ok": True},  # non-event frame
				None,  # soft-timeout / non-JSON noise
				_chat_frame("r1", "other-session", "final"),  # wrong sessionKey
				_chat_frame("other-run", "sk", "final"),  # wrong runId
				_agent_frame("r1", "assistant", {"text": "ok", "delta": "ok"}),
				_chat_frame("r1", "sk", "final"),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(
			out,
			[
				{"kind": "assistant", "text": "ok", "delta": "ok"},
				{"kind": "relay:final", "text": None},
			],
		)

	def test_agent_lifecycle_frames_never_yielded(self):
		sess = self._sess(
			[
				_agent_frame("r1", "lifecycle", {"phase": "start"}),
				_agent_frame("r1", "lifecycle", {"phase": "end"}),
				_chat_frame("r1", "sk", "final"),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(out, [{"kind": "relay:final", "text": None}])

	def test_chat_delta_state_ignored(self):
		sess = self._sess(
			[
				_chat_frame("r1", "sk", "delta", message={"content": "partial"}),
				_chat_frame("r1", "sk", "delta", message={"content": "partial partial"}),
				_chat_frame("r1", "sk", "final", message={"content": "done"}),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(out, [{"kind": "relay:final", "text": "done"}])

	def test_final_with_no_message_yields_text_none(self):
		sess = self._sess([_chat_frame("r1", "sk", "final")])
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(out, [{"kind": "relay:final", "text": None}])

	def test_final_with_string_content_yields_text(self):
		sess = self._sess([_chat_frame("r1", "sk", "final", message={"content": "plain text"})])
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(out, [{"kind": "relay:final", "text": "plain text"}])

	def test_chat_error_state_yields_relay_error_with_message(self):
		sess = self._sess(
			[
				_chat_frame("r1", "sk", "error", errorMessage="provider timed out"),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(
			out,
			[
				{"kind": "relay:error", "state": "error", "error": "provider timed out"},
			],
		)

	def test_chat_aborted_state_falls_back_to_state_string(self):
		sess = self._sess([_chat_frame("r1", "sk", "aborted")])
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(
			out,
			[
				{"kind": "relay:error", "state": "aborted", "error": "aborted"},
			],
		)

	def test_final_stop_reason_error_empty_content_yields_relay_error(self):
		# A terminal agent failure (e.g. a precheck context overflow that then
		# deletes the session) is broadcast as state="final" with an EMPTY,
		# sentinel-stripped message that keeps stopReason="error". It must NOT
		# read as a silent successful empty final; it must surface as an error
		# so the turn handler stamps the assistant row's `error` field.
		from jarvis.chat.openclaw_client import FAILED_FINAL_ERROR

		sess = self._sess(
			[
				_chat_frame(
					"r1",
					"sk",
					"final",
					message={"role": "assistant", "content": [], "stopReason": "error"},
				),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(
			out,
			[{"kind": "relay:error", "state": "failed_final", "error": FAILED_FINAL_ERROR}],
		)

	def test_final_stream_error_sentinel_content_yields_relay_error(self):
		# Some openclaw builds leave the sentinel text block in the projected
		# final message instead of stripping it. That is still a failed turn,
		# not a real answer, so it maps to the same failed_final error rather
		# than rendering the raw sentinel as the assistant's reply.
		from jarvis.chat.openclaw_client import FAILED_FINAL_ERROR

		sess = self._sess(
			[
				_chat_frame(
					"r1",
					"sk",
					"final",
					message={
						"role": "assistant",
						"content": [
							{
								"type": "text",
								"text": "[assistant turn failed before producing content]",
							}
						],
					},
				),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(
			out,
			[{"kind": "relay:error", "state": "failed_final", "error": FAILED_FINAL_ERROR}],
		)

	def test_final_empty_content_without_error_stop_reason_stays_final(self):
		# A genuinely successful turn that produced only rich outputs (no prose)
		# has empty text and a non-error stopReason. It must stay a normal
		# empty-text relay:final, never be misclassified as a failure.
		sess = self._sess(
			[
				_chat_frame(
					"r1",
					"sk",
					"final",
					message={"role": "assistant", "content": [], "stopReason": "end_turn"},
				),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(out, [{"kind": "relay:final", "text": None}])

	def test_final_with_no_message_at_all_yields_relay_error(self):
		# #543, the LIVE failure shape. openclaw's relay emitters omit ``message``
		# entirely when the turn produced no assistant text, so a failed turn
		# arrives as a bare {"state": "final"}. Both live reproductions settled
		# this way and wrote an assistant row with no content AND no error.
		from jarvis.chat.openclaw_client import FAILED_FINAL_ERROR

		sess = self._sess([_chat_frame("r1", "sk", "final")])
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(
			out,
			[{"kind": "relay:error", "state": "failed_final", "error": FAILED_FINAL_ERROR}],
		)

	def test_final_top_level_stop_reason_error_yields_relay_error(self):
		# The same emitters put ``stopReason`` at the TOP LEVEL of the chat
		# payload, NOT inside ``message`` - reading it only from the message is
		# what made the original guard dead code on the live wire.
		from jarvis.chat.openclaw_client import FAILED_FINAL_ERROR

		sess = self._sess([_chat_frame("r1", "sk", "final", stopReason="error")])
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(
			out,
			[{"kind": "relay:error", "state": "failed_final", "error": FAILED_FINAL_ERROR}],
		)

	def test_failed_final_names_the_provider_reason_from_the_lifecycle_frame(self):
		# The lifecycle error frame is the ONLY place openclaw names the failure.
		# It is dropped from the terminal path (the chat event stays the single
		# terminal) but kept, so the customer is told what to fix.
		detail = (
			"Google Generative AI API error (429): You exceeded your current quota, "
			"please check your plan and billing details."
		)
		sess = self._sess(
			[
				_agent_frame("r1", "lifecycle", {"phase": "error", "error": detail}),
				_chat_frame("r1", "sk", "final"),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["state"], "failed_final")
		self.assertIn("429", out[0]["error"])
		self.assertIn("quota", out[0]["error"])

	def test_failed_final_detail_that_would_reroute_falls_back_to_generic_copy(self):
		# turn_handler parks a relay:error mentioning "context overflow" for
		# snapshot recovery. That never lands for a TERMINAL failed final, so a
		# provider detail carrying the marker must not steer control flow - the
		# generic copy is used instead and the turn stays terminal.
		from jarvis.chat.openclaw_client import FAILED_FINAL_ERROR

		sess = self._sess(
			[
				_agent_frame(
					"r1",
					"lifecycle",
					{"phase": "error", "error": "Context overflow: prompt too large for the model"},
				),
				_chat_frame("r1", "sk", "final"),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(out[0]["error"], FAILED_FINAL_ERROR)

	def test_lifecycle_error_then_a_real_answer_stays_a_relay_final(self):
		# openclaw emits a lifecycle error per FAILED candidate and then fails
		# over. A turn the fallback model rescued must keep its answer.
		sess = self._sess(
			[
				_agent_frame("r1", "lifecycle", {"phase": "error", "error": "rate limited"}),
				_chat_frame(
					"r1",
					"sk",
					"final",
					message={"role": "assistant", "content": [{"type": "text", "text": "PONG"}]},
				),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(out, [{"kind": "relay:final", "text": "PONG"}])

	def test_final_real_text_with_error_stop_reason_keeps_the_answer(self):
		# A partial answer that also flagged an error must NOT be hidden behind
		# an error bubble: the streamed reply wins and stays a relay:final.
		sess = self._sess(
			[
				_chat_frame(
					"r1",
					"sk",
					"final",
					message={
						"role": "assistant",
						"content": [{"type": "text", "text": "here is the partial answer"}],
						"stopReason": "error",
					},
				),
			]
		)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(out, [{"kind": "relay:final", "text": "here is the partial answer"}])

	def test_transport_drop_yields_interrupted_transport_and_does_not_raise(self):
		sess = self._sess([OpenclawUnreachableError("ws closed mid-stream")])
		# The generator swallows the exception, so the pool's
		# discard-on-exception contract never fires; the consumer must close
		# the dead WS itself so the pool healthcheck evicts it instead of
		# handing the corpse to the next turn.
		closed = []
		sess.close = lambda: closed.append(True)
		out = list(sess.relay_turn_events("sk", "r1"))
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["kind"], "relay:interrupted")
		self.assertEqual(out[0]["reason"], "transport")
		self.assertIn("ws closed mid-stream", out[0]["detail"])
		self.assertEqual(closed, [True])

	def test_deadline_yields_interrupted_deadline_on_stalling_recv(self):
		sess = OpenclawSession.__new__(OpenclawSession)

		def stalling_recv(_timeout):
			import time

			time.sleep(0.05)
			return None

		sess._recv = stalling_recv
		out = list(sess.relay_turn_events("sk", "r1", soft_deadline_s=0.01))
		self.assertEqual(out, [{"kind": "relay:interrupted", "reason": "deadline"}])


class TestSetSessionModel(FrappeTestCase):
	def _capture(self, *args, **kwargs):
		sess = OpenclawSession.__new__(OpenclawSession)
		captured = {}

		def fake_request(method, params, *, timeout_s):
			captured["method"] = method
			captured["params"] = params
			return {"ok": True, "payload": {}}

		sess._request = fake_request
		sess.set_session_model(*args, **kwargs)
		return captured

	def test_sends_exact_sessions_patch_params(self):
		captured = self._capture("sk", "openai/gpt-4o")
		self.assertEqual(captured["method"], "sessions.patch")
		self.assertEqual(captured["params"], {"key": "sk", "model": "openai/gpt-4o"})

	def test_none_sends_an_explicit_null_to_reset_the_session(self):
		"""None must reach the gateway as ``"model": null`` -- PRESENT, not omitted.

		openclaw only clears a session's modelOverride when it actually sees
		``model: null`` (its isDefault branch). Dropping the key instead would leave a
		stale pin live -- the jarvis#299 bug -- and would leave the session overridden,
		which is what disables the fallback chain.

		This asserts only what we put ON THE WIRE. It stubs _request, so it cannot see
		gateway schema validation: an earlier attempt to add a ``modelOverrideSource``
		field passed a test shaped exactly like this one and was rejected live with
		INVALID_REQUEST. Payload-shape changes need a real container to verify.
		"""
		captured = self._capture("sk", None)
		self.assertIn("model", captured["params"], "the key must be PRESENT, not dropped")
		self.assertEqual(captured["params"], {"key": "sk", "model": None})
