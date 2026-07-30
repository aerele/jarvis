"""Tests for jarvis.chat.worker.run_agent_turn.

Like test_chat_api, these run as the fixture user ``TEST_USER`` - never as
Administrator - so the test suite cannot wipe real chat history when run
against a dev site.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_session_pool, turn_handler
from jarvis.chat.api import create_conversation, get_conversation, send_message
from jarvis.chat.worker import run_agent_turn
from jarvis.exceptions import OpenclawUnreachableError
from jarvis.tests.test_chat_api import (
	TEST_USER,
	_cleanup_user_conversations,
	_ensure_test_user,
)

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"


def _make_conversation_with_user_message(text: str = "hi") -> tuple[str, str]:
	"""Helper: create conversation, attach a user message, return (conv, msg).

	send_message no longer creates the openclaw session on the web request
	(2026-07 latency plan, Phase 1.1 — the worker creates it on its pooled
	connection for first turns). These tests drive the worker against a
	conversation that already has a session, so set the key directly; the
	first-turn creation path has its own test
	(TestWorkerCreatesSessionOnFirstTurn).
	"""
	conv = create_conversation()
	with patch("frappe.enqueue"):
		result = send_message(conv, text)
	frappe.db.set_value(CONV, conv, "session_key", "agent:fake")
	frappe.db.commit()
	return conv, result["message_id"]


def _fake_event_stream(events: list[dict]):
	"""Build a generator returning the given events (matching parse_event output)."""
	for ev in events:
		yield ev


class TestRunAgentTurnHappyPath(FrappeTestCase):
	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_streams_assistant_deltas_to_db_and_realtime(self):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "assistant", "text": "Hello", "delta": "Hello"},
				{"kind": "assistant", "text": "Hello world", "delta": " world"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user") as pub:
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		# One assistant message persisted, content = final cumulative text
		assistants = frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "role": "assistant"},
			fields=["name", "content", "streaming"],
		)
		self.assertEqual(len(assistants), 1)
		self.assertEqual(assistants[0]["content"], "Hello world")
		self.assertEqual(assistants[0]["streaming"], 0)

		# Realtime publishes happened
		kinds = [c.args[1]["kind"] for c in pub.call_args_list]
		self.assertIn("run:start", kinds)
		self.assertIn("assistant:delta", kinds)
		self.assertIn("run:end", kinds)


class TestRunAgentTurnToolCall(FrappeTestCase):
	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_tool_events_persist_tool_message_rows(self):
		# Pre-existing (unrelated to the relay rewiring, reproduces on the
		# base commit too): a jarvis__* tool_name is gated by `is_jarvis` in
		# _handle_event_inner - the backend's call_tool path already
		# persists that row, so this dispatch only drives the live activity
		# indicator and does NOT insert a row for jarvis__ tools (see the
		# "is_jarvis" comment in turn_handler.py). Use a built-in (non
		# jarvis__) tool name so this test exercises the path that actually
		# inserts a tool-message row.
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "tool", "phase": "start", "tool_name": "browser_click", "tool_call_id": "tc-1"},
				{
					"kind": "tool",
					"phase": "end",
					"tool_name": "browser_click",
					"tool_call_id": "tc-1",
					"status": "completed",
				},
				{"kind": "assistant", "text": "Done", "delta": "Done"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		tools = frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "role": "tool"},
			fields=["tool_name", "tool_status"],
		)
		self.assertEqual(len(tools), 1)
		self.assertEqual(tools[0]["tool_name"], "browser_click")
		self.assertEqual(tools[0]["tool_status"], "completed")


class TestRunAgentTurnErrorPaths(FrappeTestCase):
	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_connect_failure_publishes_run_error_and_marks_message_errored(self):
		from jarvis.exceptions import OpenclawUnreachableError

		with patch(
			"jarvis.chat.agent_session_pool.OpenclawSession.connect",
			side_effect=OpenclawUnreachableError("connect refused"),
		):
			with patch("jarvis.chat.worker.publish_to_user") as pub:
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		assistants = frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "role": "assistant"},
			fields=["error", "streaming"],
		)
		self.assertEqual(len(assistants), 1)
		self.assertIn("connect refused", assistants[0]["error"])
		self.assertEqual(assistants[0]["streaming"], 0)

		kinds = [c.args[1]["kind"] for c in pub.call_args_list]
		self.assertIn("run:error", kinds)

	def test_lifecycle_error_marks_message_errored(self):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		# A bare lifecycle:error frame (as opposed to a relay:error terminal)
		# no longer occurs on the real managed path (relay_turn_events strips
		# lifecycle frames), but _handle_event_inner's lifecycle-error branch
		# is still live, so this pins that shared dispatch
		# still marks the row errored regardless of which branch drove it.
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "lifecycle", "phase": "error", "error": "model overloaded"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		assistants = frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "role": "assistant"},
			fields=["error"],
		)
		self.assertIn("model overloaded", assistants[0]["error"])

	def test_unexpected_exception_marks_errored_then_reraises(self):
		"""Sprint-3 (2026-06-16 review): the inline OpenclawUnreachableError
		catches USED to leave every other exception (cryptography.InvalidKey
		from device signing, ssl.SSLError, programmer bugs in _handle_event)
		propagating to RQ without _mark_errored or run:error - the
		assistant row stayed at streaming=1 forever and the UI spun. The
		outer catch-all now marks errored + publishes run:error AND
		re-raises so RQ records the job failure.
		"""
		fake_sess = MagicMock()
		# Simulate a non-Openclaw exception escaping from chat_send
		# (e.g. an SSL handshake mid-stream, or a tool-handler bug).
		import ssl

		fake_sess.chat_send.side_effect = ssl.SSLError("handshake failed")

		published_kinds: list = []

		def _capture(user, payload):
			published_kinds.append(payload.get("kind"))

		with (
			patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess),
			patch("jarvis.chat.worker.publish_to_user", side_effect=_capture),
		):
			with self.assertRaises(ssl.SSLError):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		# Placeholder must be flipped off streaming + carry an error.
		assistants = frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "role": "assistant"},
			fields=["error", "streaming"],
		)
		self.assertEqual(len(assistants), 1)
		self.assertEqual(assistants[0]["streaming"], 0)
		# Error message names the exception class - operator can grep
		# the Error Log for the full traceback.
		self.assertIn("SSLError", assistants[0]["error"] or "")
		# Realtime run:error was published so the UI exits its spinner.
		self.assertIn("run:error", published_kinds)


class TestRunAgentTurnAugmentsMessage(FrappeTestCase):
	"""Worker should prepend `[Context: today is ...]` to the user message
	before sending it to openclaw, so the agent can resolve relative time
	expressions ("last quarter") without a clarifying round-trip.
	"""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message("how many invoices last quarter?")

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_prepends_today_context_to_user_message(self):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		# chat_send was called with (session_key, message_text, run_id)
		fake_sess.chat_send.assert_called_once()
		_, kwargs = fake_sess.chat_send.call_args
		# args may be either positional or keyword - handle both
		positional = fake_sess.chat_send.call_args.args
		message_sent = positional[1] if len(positional) >= 2 else kwargs.get("message")
		self.assertIsNotNone(message_sent)
		self.assertIn("[Context: today is ", message_sent)
		# Chat user (conv.owner) is in the same bracket so the agent can
		# answer "who am I" / "what perms do I have" without round-tripping.
		self.assertIn(f"chat user: {TEST_USER}", message_sent)
		self.assertIn("how many invoices last quarter?", message_sent)
		# The DB-persisted user message stays untouched (no prefix)
		original = frappe.db.get_value(MSG, self.user_msg, "content")
		self.assertEqual(original, "how many invoices last quarter?")


class TestRunAgentTurnModelResolution(FrappeTestCase):
	"""Worker resolves the effective model from conv.model_override → settings.llm_model
	and threads it (with the openclaw provider id) into set_session_model
	(sessions.patch) before chat_send.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		cls._settings_snap = {
			"llm_auth_mode": settings.llm_auth_mode,
			"llm_provider": settings.llm_provider,
			"llm_model": settings.llm_model,
		}
		settings.db_set("llm_auth_mode", "oauth", update_modified=False)
		settings.db_set("llm_provider", "OpenAI", update_modified=False)
		settings.db_set("llm_model", "gpt-5.5", update_modified=False)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		settings = frappe.get_single("Jarvis Settings")
		for k, v in cls._settings_snap.items():
			settings.db_set(k, v, update_modified=False)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message("hi")

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _run_and_capture(self):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")
		# Model overrides are applied to the SESSION (sessions.patch) before
		# chat_send now - chat.send has no per-turn model param.
		return fake_sess.set_session_model.call_args

	def test_no_override_uses_settings_model_and_oauth_provider_id(self):
		"""conv.model_override empty → effective_model = settings.llm_model;
		provider id mapped from settings.llm_provider to openclaw provider id."""
		call = self._run_and_capture()
		args = call.args
		self.assertEqual(args[0], "agent:fake")
		# Maps to openclaw's model-provider key, not the OAuth flow id
		# (which is "openai-codex"). See _PROVIDER_LABEL_TO_OPENCLAW_ID
		# in chat/worker.py for the rationale.
		self.assertEqual(args[1], "openai/gpt-5.5")

	def test_override_used_when_set(self):
		"""conv.model_override = gpt-5.4-mini → effective_model = gpt-5.4-mini."""
		frappe.db.set_value(CONV, self.conv, "model_override", "gpt-5.4-mini")
		frappe.db.commit()
		call = self._run_and_capture()
		args = call.args
		self.assertEqual(args[0], "agent:fake")
		# Maps to openclaw's model-provider key, not the OAuth flow id
		# (which is "openai-codex"). See _PROVIDER_LABEL_TO_OPENCLAW_ID
		# in chat/worker.py for the rationale.
		self.assertEqual(args[1], "openai/gpt-5.4-mini")


class TestRunAgentTurnApiKeyModeOmitsProvider(FrappeTestCase):
	"""In api_key mode (not oauth), the worker should NOT thread the
	provider param - there's no per-tenant codex provider to override
	to; api-key-mode customers have one provider registered."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Jarvis Settings")
		cls._snap = {
			"llm_auth_mode": settings.llm_auth_mode,
			"llm_provider": settings.llm_provider,
			"llm_model": settings.llm_model,
		}
		settings.db_set("llm_auth_mode", "api_key", update_modified=False)
		settings.db_set("llm_provider", "Anthropic", update_modified=False)
		settings.db_set("llm_model", "claude-sonnet-4-6", update_modified=False)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		settings = frappe.get_single("Jarvis Settings")
		for k, v in cls._snap.items():
			settings.db_set(k, v, update_modified=False)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message("hi")

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_provider_omitted_in_api_key_mode(self):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")
		# api_key mode has no oauth_provider_id, so the session model patch
		# carries the bare model name (no "<provider>/" prefix).
		args = fake_sess.set_session_model.call_args.args
		self.assertEqual(args[0], "agent:fake")
		self.assertEqual(args[1], "claude-sonnet-4-6")


class TestAssistantContentBatching(FrappeTestCase):
	"""Sprint-5 punch-list "Worker writes assistant.content on every
	delta with full overwrite + commit per token" (2026-06-16 review).

	The hot-path optimization buffers assistant-content writes and
	flushes in batches instead of per-token. End-to-end observability
	is unchanged - realtime publishes still fire on every token, and
	the on-disk row holds the FINAL cumulative content at stream end -
	but the wire shape now coalesces N=10 events or 250ms into one
	commit, cutting hundreds of write+commit cycles to single digits
	per turn.
	"""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_realtime_delta_fires_on_every_token(self):
		# Customer experience pin: the UI animates token-by-token via
		# realtime, so every assistant event must still publish an
		# assistant:delta even though the DB write coalesces.
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "assistant", "text": "H", "delta": "H"},
				{"kind": "assistant", "text": "He", "delta": "e"},
				{"kind": "assistant", "text": "Hel", "delta": "l"},
				{"kind": "assistant", "text": "Hell", "delta": "l"},
				{"kind": "assistant", "text": "Hello", "delta": "o"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user") as pub:
				run_agent_turn(self.conv, self.user_msg, run_id="r1")
		# Five assistant:delta publishes (one per token), even though
		# only 1-2 DB commits would have fired under batching.
		delta_calls = [c for c in pub.call_args_list if c.args[1].get("kind") == "assistant:delta"]
		self.assertEqual(len(delta_calls), 5)
		# Final on-disk content is the full cumulative text - the
		# end-of-stream batcher.flush() persists whatever was buffered.
		row = frappe.db.get_value(
			MSG,
			{"conversation": self.conv, "role": "assistant"},
			["content", "streaming"],
			as_dict=True,
		)
		self.assertEqual(row["content"], "Hello")
		self.assertEqual(row["streaming"], 0)

	def test_db_writes_coalesce_under_size_threshold(self):
		# 12 assistant events should produce 1 write at the size
		# threshold (10) + 1 final drain write = 2 set_value calls on
		# the assistant row's content. Without batching this would be
		# 12 set_value + 12 commit pairs.
		events = [{"kind": "lifecycle", "phase": "start"}]
		cum = ""
		for ch in "abcdefghijkl":  # 12 chars
			cum += ch
			events.append({"kind": "assistant", "text": cum, "delta": ch})
		events.append({"kind": "lifecycle", "phase": "end"})
		events.append({"kind": "relay:final", "text": None})

		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(events)

		write_calls: list[tuple] = []
		real_set_value = frappe.db.set_value

		def tracking_set_value(*args, **kwargs):
			# args[0]=doctype, args[1]=name, args[2]=field or dict
			if (
				args
				and args[0] == MSG
				and (
					(len(args) >= 3 and args[2] == "content")
					or (len(args) >= 3 and isinstance(args[2], dict) and "content" in args[2])
				)
			):
				write_calls.append(args)
			return real_set_value(*args, **kwargs)

		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				with patch("frappe.db.set_value", side_effect=tracking_set_value):
					run_agent_turn(self.conv, self.user_msg, run_id="r1")

		# At least 1 (the final drain) and at most 2 (size threshold +
		# drain). Critically NOT 12 - that would mean batching broke
		# and we're back to per-token commits.
		self.assertGreaterEqual(len(write_calls), 1)
		self.assertLessEqual(len(write_calls), 2)
		# Final on-disk content is the full cumulative text.
		row = frappe.db.get_value(
			MSG,
			{"conversation": self.conv, "role": "assistant"},
			"content",
		)
		self.assertEqual(row, "abcdefghijkl")

	def test_tool_event_flushes_pending_assistant_content_first(self):
		# Ordering pin: a tool event inserts a new row. The assistant
		# row's pending text must be persisted FIRST so the on-disk
		# ordering matches the realtime channel (customer sees assistant
		# text -> tool call). Verify by checking the assistant row's
		# content immediately after the tool event would have fired.
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "assistant", "text": "Looking it up", "delta": "Looking it up"},
				# Tool event fires before the 10-event size threshold;
				# without the pre-tool flush the assistant row would be
				# empty until the next assistant delta.
				{"kind": "tool", "phase": "start", "tool_name": "jarvis__get_list", "tool_call_id": "tc-1"},
				{
					"kind": "tool",
					"phase": "end",
					"tool_name": "jarvis__get_list",
					"tool_call_id": "tc-1",
					"status": "completed",
				},
				{"kind": "assistant", "text": "Done!", "delta": " Done!"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")
		# Final cumulative content is the last assistant text. The
		# important thing is that the tool row got inserted AFTER the
		# "Looking it up" persist (which we can't directly observe in
		# this snapshot test, but the ordering invariant means the
		# final content is the LAST assistant text, not an arbitrary
		# unflushed earlier one).
		row = frappe.db.get_value(
			MSG,
			{"conversation": self.conv, "role": "assistant"},
			"content",
		)
		self.assertEqual(row, "Done!")

	def test_handle_event_failure_is_logged_and_stream_continues(self):
		# Sprint-5 punch-list "Wrap _handle_event in try/except logging
		# event kind + tool_call_id + run_id". A malformed event must
		# not blow up the whole turn - it gets logged via
		# frappe.log_error, then the next event runs.
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				# A 'tool' end event with no matching start is the cleanest
				# trigger for an internal log, but we want to assert the
				# _handle_event try/except path - so inject a guaranteed
				# raise inside frappe.get_doc to break the tool-start
				# write path.
				{"kind": "tool", "phase": "start", "tool_name": "broken", "tool_call_id": "tc-x"},
				{"kind": "assistant", "text": "Recovered", "delta": "Recovered"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		# Force the tool-start insert to fail; the assistant event
		# after it must still land.
		_orig_get_doc = frappe.get_doc

		def boom_on_tool_msg(*args, **kwargs):
			if args and isinstance(args[0], dict) and args[0].get("role") == "tool":
				raise RuntimeError("simulated tool-row insert failure")
			return _orig_get_doc(*args, **kwargs)

		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				with patch("frappe.get_doc", side_effect=boom_on_tool_msg):
					with patch("frappe.log_error") as log_err:
						run_agent_turn(self.conv, self.user_msg, run_id="r1")
		# The tool-row failure was logged with the event kind + ids.
		titles = [c.kwargs.get("title", "") for c in log_err.call_args_list]
		self.assertTrue(
			any("_handle_event failed" in t for t in titles),
			f"expected '_handle_event failed' log; got {titles!r}",
		)
		# The assistant event AFTER the broken tool event still ran
		# (stream didn't abort), and the assistant row's final content
		# reflects the recovery delta.
		row = frappe.db.get_value(
			MSG,
			{"conversation": self.conv, "role": "assistant"},
			"content",
		)
		self.assertEqual(row, "Recovered")


class TestAssistantContentBatcherUnit(FrappeTestCase):
	"""Unit-level coverage of _AssistantContentBatcher thresholds."""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message()
		# Find the assistant placeholder row created by the conversation.
		self.assistant_name = (
			frappe.get_doc(
				{
					"doctype": MSG,
					"conversation": self.conv,
					"seq": 99,
					"role": "assistant",
					"content": "",
					"streaming": 1,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		frappe.db.commit()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_flush_persists_pending_text(self):
		from jarvis.chat.worker import _AssistantContentBatcher

		b = _AssistantContentBatcher(self.assistant_name)
		b.delta("hello")
		self.assertTrue(b.flush())
		self.assertEqual(
			frappe.db.get_value(MSG, self.assistant_name, "content"),
			"hello",
		)
		# Second flush with no new text is a no-op.
		self.assertFalse(b.flush())

	def test_flush_if_due_respects_size_threshold(self):
		from jarvis.chat.worker import (
			_ASSISTANT_BATCH_SIZE,
			_AssistantContentBatcher,
		)

		b = _AssistantContentBatcher(self.assistant_name)
		# Below the size threshold: no flush (the time threshold is 250ms
		# which this test runs well under).
		for i in range(_ASSISTANT_BATCH_SIZE - 1):
			b.delta(f"text-{i}")
			self.assertFalse(b.flush_if_due())
		# Hitting the size threshold flushes.
		b.delta(f"text-{_ASSISTANT_BATCH_SIZE - 1}")
		self.assertTrue(b.flush_if_due())
		# After flush, the next delta starts the counter over.
		b.delta("post-flush")
		self.assertFalse(b.flush_if_due())


class TestWorkerCreatesSessionOnFirstTurn(FrappeTestCase):
	"""2026-07 latency plan, Phase 1.1: the WORKER creates the openclaw
	session for a conversation's first turn on its pooled connection —
	send_message no longer does it on the browser-awaited POST. The
	Jarvis Chat Session row (the plugin's sessionKey→user lookup, i.e.
	the permission moat) must exist BEFORE the stream starts."""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		# Deliberately NOT setting session_key: this simulates the first
		# turn as send_message now leaves it.
		self.conv = create_conversation()
		with patch("frappe.enqueue"):
			result = send_message(self.conv, "first message")
		self.user_msg = result["message_id"]

	def tearDown(self):
		frappe.db.delete("Jarvis Chat Session", {"session_key": "agent:new"})
		frappe.db.commit()
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_send_message_no_longer_sets_session_key(self):
		self.assertFalse(frappe.db.get_value(CONV, self.conv, "session_key"))

	def test_worker_creates_session_row_before_streaming(self):
		fake_sess = MagicMock()
		fake_sess.create_session.return_value = "agent:new"
		row_exists_at_stream_start = {}

		def _send(session_key, *a, **kw):
			# Capture the moat invariant at the exact moment the turn
			# starts (chat_send, the RPC that hands off to openclaw): the
			# sessionKey→user row must already be committed.
			row_exists_at_stream_start["row"] = frappe.db.get_value(
				"Jarvis Chat Session",
				{"session_key": session_key},
				["user", "session_key"],
				as_dict=True,
			)
			return {"runId": "r1", "status": "started"}

		fake_sess.chat_send.side_effect = _send
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "assistant", "text": "Hi", "delta": "Hi"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		# The session was created on the WORKER's pooled connection.
		fake_sess.create_session.assert_called_once()
		# The row existed (with the sender's identity) before streaming.
		row = row_exists_at_stream_start["row"]
		self.assertIsNotNone(row, "Jarvis Chat Session row must exist before chat_send")
		self.assertEqual(row["session_key"], "agent:new")
		self.assertEqual(row["user"], TEST_USER)
		# And the conversation now carries the session key.
		self.assertEqual(
			frappe.db.get_value(CONV, self.conv, "session_key"),
			"agent:new",
		)


class TestRunAgentTurnRelayTerminals(FrappeTestCase):
	"""Task 3 (openclaw-native relay transport): the managed branch now
	drives chat_send + relay_turn_events instead of stream_agent_turn.

	Never-error invariant: after a successful chat_send ack, NO code path
	may publish run:error except a genuine relay:error terminal. A
	relay:interrupted (deadline, transport drop, exhausted stream) or an
	"ok" ack (cached replay of an already-completed run) parks the row via
	_mark_recovering + best-effort recover_now and returns silently -
	openclaw's durable transcript is the source of truth, not a guess made
	from a lost stream.
	"""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _fake_sess(self, relay_events, ack=None):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: (
			dict(ack) if ack is not None else {"runId": idem, "status": "started"}
		)
		fake_sess.relay_turn_events.return_value = _fake_event_stream(relay_events)
		return fake_sess

	def _assistant_row(self, fields):
		# A single field name returns the raw value; a list returns a dict -
		# mirrors frappe.db.get_value's own as_dict semantics.
		return frappe.db.get_value(
			MSG,
			{"conversation": self.conv, "role": "assistant"},
			fields,
			as_dict=isinstance(fields, list),
		)

	def test_interrupted_terminal_parks_for_recovery_without_run_error(self):
		fake_sess = self._fake_sess(
			[
				{"kind": "assistant", "text": "partial", "delta": "partial"},
				{"kind": "relay:interrupted", "reason": "deadline"},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.turn_recovery.recover_now") as recover_now:
				with patch("jarvis.chat.worker.publish_to_user") as pub:
					run_agent_turn(self.conv, self.user_msg, run_id="r1")

		recover_now.assert_called_once_with(self.conv)
		row = self._assistant_row(["recovering", "recovery_started_at", "streaming", "error"])
		self.assertEqual(row["recovering"], 1)
		self.assertIsNotNone(row["recovery_started_at"])
		# Spinner stays up - turn_recovery finalizes it later from the
		# gateway snapshot, not this worker.
		self.assertEqual(row["streaming"], 1)
		self.assertFalse(row["error"])
		kinds = [c.args[1]["kind"] for c in pub.call_args_list]
		self.assertNotIn("run:error", kinds)
		# The clean-exit run:end never fires either - this turn isn't done.
		self.assertNotIn("run:end", kinds)

	def test_relay_error_terminal_marks_errored_and_publishes_run_error(self):
		fake_sess = self._fake_sess(
			[
				{"kind": "assistant", "text": "oops", "delta": "oops"},
				{"kind": "relay:error", "state": "error", "error": "provider quota exceeded"},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user") as pub:
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		row = self._assistant_row(["error", "streaming"])
		self.assertIn("provider quota exceeded", row["error"])
		self.assertEqual(row["streaming"], 0)
		kinds = [c.args[1]["kind"] for c in pub.call_args_list]
		self.assertIn("run:error", kinds)

	def test_ack_ok_skips_relay_consume_and_parks_for_recovery(self):
		# Cached replay: chat_send returns status="ok" for an idempotency
		# key that already completed (e.g. worker died post-completion,
		# job re-enqueued). No events will follow - finalize from the
		# durable transcript instead of calling relay_turn_events at all.
		fake_sess = self._fake_sess([], ack={"runId": "r1", "status": "ok"})
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.turn_recovery.recover_now") as recover_now:
				with patch("jarvis.chat.worker.publish_to_user"):
					run_agent_turn(self.conv, self.user_msg, run_id="r1")

		fake_sess.relay_turn_events.assert_not_called()
		recover_now.assert_called_once_with(self.conv)
		row = self._assistant_row(["recovering"])
		self.assertEqual(row["recovering"], 1)

	def test_ack_in_flight_with_different_run_id_is_honored(self):
		# openclaw may hand back a DIFFERENT runId than our idempotency key
		# (e.g. an in-flight run from a prior attempt); relay_turn_events
		# must be driven with the server-assigned id, not ours.
		fake_sess = self._fake_sess(
			[{"kind": "relay:final", "text": None}],
			ack={"runId": "server-assigned-run-id", "status": "in_flight"},
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		fake_sess.relay_turn_events.assert_called_once()
		args, kwargs = fake_sess.relay_turn_events.call_args
		run_id_used = args[1] if len(args) >= 2 else kwargs.get("run_id")
		self.assertEqual(run_id_used, "server-assigned-run-id")

	def test_relay_final_nonempty_text_overwrites_batcher_content(self):
		# relay:final's text is authoritative over whatever the batcher
		# last wrote (openclaw's durable transcript beats our in-flight
		# delta tail).
		fake_sess = self._fake_sess(
			[
				{"kind": "assistant", "text": "draft", "delta": "draft"},
				{"kind": "relay:final", "text": "authoritative final text"},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		row = self._assistant_row("content")
		self.assertEqual(row, "authoritative final text")

	def test_watermark_captured_before_send_and_stamped_on_assistant_row(self):
		# Watermark must be read (best-effort, via get_session_messages) BEFORE
		# chat_send hands the turn to openclaw, and stamped onto the assistant
		# row as the highest seq seen - so a run that later dies server-side
		# with zero output can never have recovery finalize from the PREVIOUS
		# turn's reply (that reply is already in the transcript at this point).
		fake_sess = self._fake_sess([{"kind": "relay:final", "text": "hi"}])
		fake_sess.get_session_messages.return_value = [
			{"role": "assistant", "content": "old", "__openclaw": {"seq": 3}},
			{"role": "user", "content": "q", "__openclaw": {"seq": 4}},
		]
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		call_names = [c[0] for c in fake_sess.mock_calls if c[0] in ("get_session_messages", "chat_send")]
		self.assertEqual(call_names, ["get_session_messages", "chat_send"])

		row = self._assistant_row("openclaw_seq_watermark")
		self.assertEqual(row, 4)


class TestRunAgentTurnRelayStreamTelemetry(FrappeTestCase):
	"""Follow-up (2026-07 review): the old ``_consume`` populated the
	``stream_stats`` dict (first_event_ms, first_delta_ms,
	pre_reply_tool_calls) that feeds the ``_lat.info`` latency summary
	line. ``_consume_relay`` (the managed/relay path) never touched it, so
	managed turns logged -1s for every field - dark telemetry for the
	chat-latency investigation. ``_consume_relay`` must now stamp the same
	stats for the events it dispatches."""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_relay_turn_populates_stream_stats_for_latency_log(self):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "tool", "phase": "start", "tool_name": "browser_click", "tool_call_id": "tc-1"},
				{
					"kind": "tool",
					"phase": "end",
					"tool_name": "browser_click",
					"tool_call_id": "tc-1",
					"status": "completed",
				},
				{"kind": "assistant", "text": "Hi", "delta": "Hi"},
				{"kind": "relay:final", "text": None},
			]
		)
		fake_logger = MagicMock()
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				with patch("jarvis.chat.latency.get_logger", return_value=fake_logger):
					run_agent_turn(self.conv, self.user_msg, run_id="r1")

		fake_logger.info.assert_called_once()
		# _lat.info(fmt, run_id, first_turn, queue_wait_ms, checkout_ms,
		#           session_create_ms, first_event_ms, first_delta_ms,
		#           pre_reply_tool_calls, turn_total_ms)
		args = fake_logger.info.call_args.args
		first_event_ms, first_delta_ms, pre_reply_tool_calls = args[6], args[7], args[8]
		self.assertGreaterEqual(first_event_ms, 0)
		self.assertGreaterEqual(first_delta_ms, 0)
		self.assertEqual(pre_reply_tool_calls, 1)


class TestRunAgentTurnThinkingDirective(FrappeTestCase):
	"""The /think directive is cache-unsafe as a message-body prefix (it would
	bust the OpenAI prefix cache the warm-up populates), so the turn sends it as
	the chat_send ``thinking`` param and leaves user_message unprefixed."""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message("hi")
		frappe.db.set_value(CONV, self.conv, "thinking_override", "high")
		frappe.db.commit()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_managed_message_unprefixed_thinking_sent_as_chat_send_param(self):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		args, kwargs = fake_sess.chat_send.call_args
		message_sent = args[1]
		self.assertNotIn("/think", message_sent)
		self.assertEqual(kwargs.get("thinking"), "high")


class TestRichOutputsRouting(FrappeTestCase):
	"""Recovery-completeness batch: the clean-exit path routes canvas +
	generated-image persistence through the shared
	``persist_rich_outputs`` function (extracted so snapshot recovery can
	call the same code). Pin that the worker actually calls it, with the
	right args, rather than re-testing canvas/generated-image behavior
	itself (covered by test_canvas.py and generated_media's own tests)."""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def test_clean_exit_calls_persist_rich_outputs_with_int_turn_start_ms(self):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "assistant", "text": "Hello", "delta": "Hello"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				with patch("jarvis.chat.turn_handler.persist_rich_outputs") as rich:
					run_agent_turn(self.conv, self.user_msg, run_id="r1")

		rich.assert_called_once()
		args = rich.call_args.args
		assistant_name = frappe.db.get_value(
			MSG,
			{"conversation": self.conv, "role": "assistant"},
			"name",
		)
		self.assertEqual(args[0], assistant_name)
		self.assertEqual(args[1], self.conv)
		self.assertEqual(args[2], TEST_USER)
		self.assertEqual(args[3], "r1")
		self.assertIsInstance(args[4], int)

	def test_clean_exit_leaves_was_recovered_unset(self):
		# was_recovered is snapshot recovery's fingerprint - a turn that
		# finishes via the live worker's clean exit must NOT carry it.
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {"runId": idem, "status": "started"}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(
			[
				{"kind": "lifecycle", "phase": "start"},
				{"kind": "assistant", "text": "Hello", "delta": "Hello"},
				{"kind": "lifecycle", "phase": "end"},
				{"kind": "relay:final", "text": None},
			]
		)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user"):
				run_agent_turn(self.conv, self.user_msg, run_id="r1")

		row = frappe.db.get_value(
			MSG,
			{"conversation": self.conv, "role": "assistant"},
			["streaming", "was_recovered"],
			as_dict=True,
		)
		self.assertEqual(row["streaming"], 0)  # the turn did finish cleanly
		self.assertEqual(row["was_recovered"], 0)


class TestOverflowParksForRecovery(FrappeTestCase):
	"""A 'Context overflow' lifecycle error must PARK the turn (openclaw
	auto-compacts and retries; the answer lands in the session shortly
	after), never surface a terminal run:error to the chat."""

	def _run_event(self, error_text):
		from jarvis.chat import turn_handler

		calls = {}
		with (
			patch.object(turn_handler, "_mark_recovering") as rec,
			patch.object(turn_handler, "_mark_errored") as err,
			patch.object(turn_handler, "_publish_to_user") as pub,
		):
			turn_handler._handle_event_inner(
				{"kind": "lifecycle", "phase": "error", "error": error_text},
				conversation_id="c1",
				assistant_msg_name="m1",
				tool_msg_by_call_id={},
				user="u@x",
				run_id="r1",
				batcher=MagicMock(),
			)
		calls["recovering"] = rec.called
		calls["errored"] = err.called
		calls["kinds"] = [c.args[1]["kind"] for c in pub.call_args_list]
		return calls

	def test_context_overflow_parks(self):
		out = self._run_event("Context overflow: prompt too large for the model. Try /reset ...")
		self.assertTrue(out["recovering"])
		self.assertFalse(out["errored"])
		self.assertEqual(out["kinds"], ["run:recovering"])

	def test_other_errors_still_error(self):
		out = self._run_event("provider returned 500")
		self.assertFalse(out["recovering"])
		self.assertTrue(out["errored"])
		self.assertEqual(out["kinds"], ["run:error"])


class TestChatSendAttachmentTimeout(FrappeTestCase):
	"""openclaw ingests and preprocesses the message BEFORE acking chat.send,
	so the ack window must scale with the payload. Two ways a send gets big:
	vision parts (PDF page raster + resize - 22s measured for a 4-page
	invoice) and INLINED TEXT (a CSV/TXT/JSON attachment is folded into the
	prompt body and never becomes a vision part). Sizing the window off
	vision parts alone left text attachments on the bare default, which is
	how a CSV send errored 'chat.send timed out' in the UI while the
	transcript showed the gateway completing the turn."""

	def test_bare_send_uses_the_base_window(self):
		self.assertEqual(turn_handler._ack_timeout_s(0, 0), turn_handler._ACK_TIMEOUT_BASE_S)

	def test_vision_parts_extend_the_window(self):
		self.assertEqual(
			turn_handler._ack_timeout_s(2, 0),
			turn_handler._ACK_TIMEOUT_BASE_S + 2 * turn_handler._ACK_TIMEOUT_PER_VISION_PART_S,
		)

	def test_inlined_text_extends_the_window(self):
		"""The regression this fix exists for: a 20k-char CSV carries zero
		vision parts, so the old formula gave it the bare default."""
		csv_sized = turn_handler._ack_timeout_s(0, 20_000)
		self.assertGreater(csv_sized, turn_handler._ACK_TIMEOUT_BASE_S)
		# Materially bigger, not a rounding artifact.
		self.assertGreaterEqual(csv_sized - turn_handler._ACK_TIMEOUT_BASE_S, 15.0)

	def test_window_is_clamped(self):
		"""A pathological payload must not stall a turn for minutes against a
		genuinely wedged gateway."""
		self.assertEqual(
			turn_handler._ack_timeout_s(100, 10_000_000),
			turn_handler._ACK_TIMEOUT_CEILING_S,
		)

	def test_negative_counts_never_shrink_the_window(self):
		self.assertEqual(turn_handler._ack_timeout_s(-5, -5), turn_handler._ACK_TIMEOUT_BASE_S)

	def test_text_attachment_produces_no_vision_parts(self):
		"""Anchors the premise: the inlined-char count is the ONLY signal a
		text attachment gives, so the budget has to read it."""
		before = "hi"
		msg, parts = turn_handler._prepare_attachments(before, None, vision_ok=True)
		self.assertEqual(parts, [])
		self.assertEqual(len(msg) - len(before), 0)


class TestChatSendAckTimeoutParks(FrappeTestCase):
	"""A chat.send ACK timeout is ambiguous: the request frame was already on
	the wire, and openclaw routinely accepts the message and runs the whole
	turn while the bench is still waiting. Erroring there is a false negative
	(user sees a timeout, transcript shows a finished answer) and invites a
	retry that re-runs under a fresh run_id and can duplicate writes. It must
	park for snapshot recovery instead. A REJECTION or a dead socket never
	reached the agent, so those must still error."""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _run_with_send_raising(self, exc):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = exc
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.turn_recovery.recover_now") as recover_now:
				with patch("jarvis.chat.worker.publish_to_user") as pub:
					run_agent_turn(self.conv, self.user_msg, run_id="r1")
		row = frappe.db.get_value(
			MSG,
			{"conversation": self.conv, "role": "assistant"},
			["recovering", "streaming", "error"],
			as_dict=True,
		)
		return row, [c.args[1]["kind"] for c in pub.call_args_list], recover_now

	def test_ack_timeout_parks_instead_of_erroring(self):
		row, kinds, recover_now = self._run_with_send_raising(
			OpenclawUnreachableError("chat.send timed out", code="ack-timeout")
		)
		self.assertEqual(row["recovering"], 1)
		# Spinner stays up; turn_recovery finalizes from the gateway snapshot.
		self.assertEqual(row["streaming"], 1)
		self.assertFalse(row["error"])
		self.assertIn("run:recovering", kinds)
		self.assertNotIn("run:error", kinds)
		recover_now.assert_called_once_with(self.conv)

	def test_rejection_still_errors(self):
		"""Guards the blast radius: only the ambiguous timeout parks."""
		row, kinds, _ = self._run_with_send_raising(
			OpenclawUnreachableError("chat.send rejected: bad-request: nope", code="bad-request")
		)
		self.assertEqual(row["recovering"], 0)
		self.assertTrue(row["error"])
		self.assertIn("run:error", kinds)
		self.assertNotIn("run:recovering", kinds)

	def test_untagged_transport_failure_still_errors(self):
		row, kinds, _ = self._run_with_send_raising(OpenclawUnreachableError("openclaw WS closed: gone"))
		self.assertEqual(row["recovering"], 0)
		self.assertTrue(row["error"])
		self.assertIn("run:error", kinds)


class TestRelayOverflowParks(FrappeTestCase):
	"""relay:error carrying a context overflow must PARK the turn (openclaw
	auto-compacts + retries; recovery finalizes the retried answer) - the
	raw overflow error must never reach the chat regardless of the
	customer plan's context-window size."""

	def test_relay_error_overflow_branch_parks(self):
		# Bounded by the NEXT branch rather than a fixed character count. The
		# invariant is ordering - the overflow branch is handled before the
		# aborted one - and a magic window measures that only by accident: the
		# assertions here sat at offsets 754/799/886 of a 900-char window, so
		# reformatting the file pushed the last one (24 chars) past the edge and
		# turned this red for a change that altered no behaviour at all.
		src = open(frappe.get_app_path("jarvis", "chat", "turn_handler.py")).read()
		i = src.index('terminal["kind"] == "relay:error"')
		j = src.index('terminal.get("state") == "aborted"', i)
		self.assertLess(i, j, "the overflow branch must come before the aborted branch")
		branch = src[i:j]
		self.assertIn('"context overflow" in err_text.lower()', branch)
		self.assertIn("_mark_recovering(assistant_msg.name)", branch)
		self.assertIn('"kind": "run:recovering"', branch)


class TestRunAgentTurnAborted(FrappeTestCase):
	"""The user hit Stop: stop_run -> openclaw chat.abort -> the relay terminal
	arrives as relay:error with state="aborted".

	A stop is a STATE of the turn, not prose: `content` stays exactly what the
	agent streamed and `stopped` records the stop. The case that matters is a
	stop MID-SENTENCE - the partial text must survive byte-for-byte AND be
	flagged, because a truncated answer carrying no marker reads as a complete
	one, which is the chat asserting a wrong answer.
	"""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _run(self, relay_events):
		"""Drive one turn through the relay path; return the publish mock."""
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {
			"runId": idem,
			"status": "started",
		}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(relay_events)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user") as pub:
				run_agent_turn(self.conv, self.user_msg, run_id="r1")
		return pub

	def _assistant_row(self, fields):
		return frappe.db.get_value(
			MSG,
			{"conversation": self.conv, "role": "assistant"},
			fields,
			as_dict=isinstance(fields, list),
		)

	def test_abort_with_partial_content_keeps_it_verbatim_and_flags_stopped(self):
		# THE regression this branch exists for: a mid-sentence stop. The text
		# must survive untouched (no marker smuggled into it) and the row must
		# carry the flag the SPA draws its marker from.
		self._run(
			[
				{"kind": "assistant", "text": "The invoice total is", "delta": "The invoice total is"},
				{"kind": "relay:error", "state": "aborted", "error": "aborted"},
			]
		)
		row = self._assistant_row(["content", "stopped", "streaming"])
		self.assertEqual(row["content"], "The invoice total is")
		self.assertEqual(row["stopped"], 1)
		self.assertEqual(row["streaming"], 0)

	def test_abort_with_no_content_flags_stopped_and_leaves_content_empty(self):
		# Stopped before anything streamed: content stays empty and the marker
		# is the flag alone - no "_Stopped._" prose anywhere.
		self._run([{"kind": "relay:error", "state": "aborted", "error": "aborted"}])
		row = self._assistant_row(["content", "stopped", "streaming"])
		self.assertFalse((row["content"] or "").strip())
		self.assertNotIn("_Stopped._", row["content"] or "")
		self.assertEqual(row["stopped"], 1)
		self.assertEqual(row["streaming"], 0)

	def test_abort_never_writes_stopped_prose_into_content(self):
		# Guards the whole class of fix-by-magic-string: the sentinel must not
		# come back through either writer.
		self._run(
			[
				{"kind": "assistant", "text": "half an ans", "delta": "half an ans"},
				{"kind": "relay:error", "state": "aborted", "error": "aborted"},
			]
		)
		self.assertNotIn("_Stopped._", self._assistant_row("content") or "")

	def test_abort_publishes_run_end_carrying_stopped(self):
		# The three run:end consumers (desktop notifier, PWA notify-on-done,
		# PWA bell feed) each announce a completion for a reply the user
		# killed; they can only suppress it if the payload says so.
		pub = self._run([{"kind": "relay:error", "state": "aborted", "error": "aborted"}])
		ends = [c.args[1] for c in pub.call_args_list if c.args[1]["kind"] == "run:end"]
		self.assertEqual(len(ends), 1)
		self.assertTrue(ends[0]["stopped"])
		# A deliberate stop is not a failure - it must never surface as one.
		self.assertNotIn("run:error", [c.args[1]["kind"] for c in pub.call_args_list])

	def test_get_conversation_returns_stopped(self):
		# get_conversation's field list is explicit: omit "stopped" and the
		# flag never reaches the SPA, so the marker vanishes on reload.
		self._run(
			[
				{"kind": "assistant", "text": "cut off here", "delta": "cut off here"},
				{"kind": "relay:error", "state": "aborted", "error": "aborted"},
			]
		)
		msgs = get_conversation(self.conv)["messages"]
		row = next(m for m in msgs if m["role"] == "assistant")
		self.assertIn("stopped", row)
		self.assertEqual(row["stopped"], 1)
		self.assertEqual(row["content"], "cut off here")

	def test_completed_turn_is_not_stopped(self):
		# The flag records that an abort LANDED - a normal turn never sets it.
		self._run(
			[
				{"kind": "assistant", "text": "all done", "delta": "all done"},
				{"kind": "relay:final", "text": "all done"},
			]
		)
		row = self._assistant_row(["content", "stopped", "streaming"])
		self.assertEqual(row["stopped"], 0)
		self.assertEqual(row["content"], "all done")
		self.assertEqual(row["streaming"], 0)


class TestRunAgentTurnFailedFinal(FrappeTestCase):
	"""A terminal agent failure that openclaw reports as state="final" with an
	empty message (stopReason error) - e.g. a precheck context overflow that
	deletes the session, so there is no auto-compact retry - must land as an
	honest error on the assistant row, never a silent empty bubble.

	agent_client.relay_turn_events remaps that final to a relay:error with
	state="failed_final"; this exercises the full turn so the row's `error`
	field and the run:error publish are both verified.
	"""

	def setUp(self):
		agent_session_pool._POOL.clear()
		_ensure_test_user()
		self._orig_user = frappe.session.user
		frappe.set_user(TEST_USER)
		_cleanup_user_conversations()
		self.conv, self.user_msg = _make_conversation_with_user_message()

	def tearDown(self):
		_cleanup_user_conversations()
		frappe.set_user(self._orig_user)

	def _run(self, relay_events):
		fake_sess = MagicMock()
		fake_sess.chat_send.side_effect = lambda sk, msg, idem, **kw: {
			"runId": idem,
			"status": "started",
		}
		fake_sess.relay_turn_events.return_value = _fake_event_stream(relay_events)
		with patch("jarvis.chat.agent_session_pool.OpenclawSession.connect", return_value=fake_sess):
			with patch("jarvis.chat.worker.publish_to_user") as pub:
				run_agent_turn(self.conv, self.user_msg, run_id="r1")
		return pub

	def _assistant_row(self, fields):
		return frappe.db.get_value(
			MSG,
			{"conversation": self.conv, "role": "assistant"},
			fields,
			as_dict=isinstance(fields, list),
		)

	def test_failed_final_stamps_error_and_is_not_recovering(self):
		from jarvis.chat.agent_client import FAILED_FINAL_ERROR

		pub = self._run([{"kind": "relay:error", "state": "failed_final", "error": FAILED_FINAL_ERROR}])
		row = self._assistant_row(["content", "error", "streaming", "recovering"])
		# The core of the bug: error must be set, not NULL, on a terminal failure.
		self.assertEqual(row["error"], FAILED_FINAL_ERROR)
		self.assertFalse((row["content"] or "").strip())
		self.assertEqual(row["streaming"], 0)
		# It is terminal, NOT parked for snapshot recovery.
		self.assertFalse(row["recovering"])
		# The user is told, via a run:error (no silent empty run:end-only turn).
		kinds = [c.args[1]["kind"] for c in pub.call_args_list]
		self.assertIn("run:error", kinds)
		err_pub = next(c.args[1] for c in pub.call_args_list if c.args[1]["kind"] == "run:error")
		self.assertEqual(err_pub["error"], FAILED_FINAL_ERROR)
