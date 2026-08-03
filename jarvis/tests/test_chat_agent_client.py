"""Tests for the direct-WS chat client (jarvis.chat.agent_client).

The transport is a real websocket-client connection to agent's gateway.
Tests fake it out at the create_connection level: a scripted WS whose recv()
returns a sequence of JSON frames and whose send() captures the client's
outbound frames for assertion.

What's verified here:
- Connect handshake: receives connect.challenge, sends a v3-signed connect,
  succeeds on a positive hello-ok response.
- create_session round-trip.
- stream_agent_turn yields parsed events between agent ack and lifecycle end.
- Close is forgiving (no raise on already-closed sockets).

Pairing itself is mocked here - see test_chat_device.py for the device.py
half of the integration (keypair + admin call).
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import websocket
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.agent_client import AgentSession
from jarvis.chat.device import ChatDeviceCredentials
from jarvis.exceptions import AgentUnreachableError

# --- helpers --------------------------------------------------------------


def _b64u(raw: bytes) -> str:
	return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_creds() -> ChatDeviceCredentials:
	import hashlib

	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PublicFormat.Raw,
	)
	device_id = hashlib.sha256(pub_raw).hexdigest()
	return ChatDeviceCredentials(
		device_id=device_id,
		public_key=_b64u(pub_raw),
		private_key=priv,
		device_token="tok-test",
	)


class _ScriptedWS:
	"""Stand-in for a websocket.WebSocket.

	frames_to_recv is a list of JSON-string frames OR callables that return
	a JSON-string frame at recv time. Callables let a frame respond to the
	id the client sends (which we can't know ahead of time)."""

	def __init__(self, frames_to_recv: list):
		self._frames = list(frames_to_recv)
		self.sent: list[dict] = []
		self.closed = False

	def settimeout(self, _seconds):
		pass

	def recv(self):
		if not self._frames:
			raise websocket.WebSocketTimeoutException("no more frames")
		item = self._frames.pop(0)
		return item() if callable(item) else item

	def send(self, raw):
		self.sent.append(json.loads(raw))

	def close(self):
		self.closed = True


def _frame(d: dict) -> str:
	return json.dumps(d)


def _challenge(nonce: str = "nonce-test") -> str:
	return _frame({"type": "event", "event": "connect.challenge", "payload": {"nonce": nonce}})


def _build_session(creds=None) -> tuple[AgentSession, _ScriptedWS]:
	"""Spin up a fully-handshaken AgentSession with no real WS. Returns the
	session and the underlying scripted WS so tests can extend its frames
	and inspect sent frames."""
	creds = creds or _make_creds()

	def _ok():
		req_id = scripted.sent[-1]["id"]
		return _frame(
			{
				"type": "res",
				"id": req_id,
				"ok": True,
				"payload": {
					"auth": {"scopes": ["operator.write", "operator.admin"]},
				},
			}
		)

	scripted = _ScriptedWS([_challenge(), _ok])
	with (
		patch("jarvis.chat.agent_client.websocket.create_connection", return_value=scripted),
		patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
	):
		sess = AgentSession.connect("ws://test")
	scripted.sent.clear()
	return sess, scripted


# --- TestConnect ----------------------------------------------------------


class TestConnect(FrappeTestCase):
	def test_handshake_sends_v3_signed_connect(self):
		creds = _make_creds()

		def _ok():
			req_id = scripted.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": True, "payload": {}})

		scripted = _ScriptedWS([_challenge("nonce-xyz"), _ok])
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", return_value=scripted),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
		):
			sess = AgentSession.connect("ws://t")

		self.assertEqual(len(scripted.sent), 1)
		req = scripted.sent[0]
		self.assertEqual(req["type"], "req")
		self.assertEqual(req["method"], "connect")
		params = req["params"]
		self.assertEqual(params["role"], "operator")
		self.assertIn("operator.write", params["scopes"])
		self.assertEqual(params["auth"]["deviceToken"], creds.device_token)
		self.assertEqual(params["device"]["id"], creds.device_id)
		self.assertEqual(params["device"]["nonce"], "nonce-xyz")
		# Signature must be a non-empty base64url string (no padding).
		self.assertTrue(params["device"]["signature"])
		self.assertNotIn("=", params["device"]["signature"])
		import time

		self.assertGreater(params["device"]["signedAt"], int(time.time() * 1000) - 60_000)
		sess.close()

	def test_connect_uses_an_origin_agent_allows_on_lan_bind(self):
		"""agent >= v2026.2.26 enforces gateway.controlUi.allowedOrigins on LAN
		binds and seeds ["http://localhost:18789", "http://127.0.0.1:18789"]; the WS
		Origin we send MUST be one of those or the gateway rejects every connect
		('origin not allowed'). Regression for the agent 2026.6.8 bump, which
		stopped honoring the old "*" wildcard that a bare http://localhost relied on."""
		from jarvis.chat.agent_client import _GATEWAY_ORIGIN

		creds = _make_creds()

		def _ok():
			req_id = scripted.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": True, "payload": {}})

		scripted = _ScriptedWS([_challenge(), _ok])
		cc = MagicMock(return_value=scripted)
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", cc),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
		):
			AgentSession.connect("ws://t")

		self.assertEqual(cc.call_args.kwargs["origin"], _GATEWAY_ORIGIN)
		# Loopback host + the gateway's own internal port (18789) — matches what
		# agent seeds for a LAN bind, NOT a bare http://localhost.
		self.assertIn(_GATEWAY_ORIGIN, ("http://127.0.0.1:18789", "http://localhost:18789"))

	def test_connect_rejection_raises_unreachable(self):
		creds = _make_creds()

		def _nack():
			req_id = scripted.sent[-1]["id"]
			return _frame(
				{
					"type": "res",
					"id": req_id,
					"ok": False,
					"error": {"code": "UNAUTHORIZED", "message": "bad token"},
				}
			)

		scripted = _ScriptedWS([_challenge(), _nack])
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", return_value=scripted),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
		):
			with self.assertRaises(AgentUnreachableError) as cm:
				AgentSession.connect("ws://t")
		self.assertIn("UNAUTHORIZED", str(cm.exception))
		self.assertTrue(scripted.closed)

	def test_missing_challenge_times_out(self):
		creds = _make_creds()
		with (
			patch("jarvis.chat.agent_client.CONNECT_TIMEOUT_SECONDS", 0.05),
			patch("jarvis.chat.agent_client.websocket.create_connection", return_value=_ScriptedWS([])),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
		):
			with self.assertRaises(AgentUnreachableError):
				AgentSession.connect("ws://t")

	def test_empty_gateway_url_raises(self):
		with patch("jarvis.chat.agent_client.ensure_paired", return_value=_make_creds()):
			with self.assertRaises(AgentUnreachableError):
				AgentSession.connect("")

	def test_ws_open_retries_network_failure_then_returns(self):
		# A cold / recreating container refuses the first attempts, then accepts;
		# the WS open rides it out instead of failing the first turn.
		sentinel = object()
		cc = MagicMock(side_effect=[OSError("timed out"), OSError("connection refused"), sentinel])
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", cc),
			patch("jarvis.chat.agent_client.time.sleep") as sleep,
		):
			ws = AgentSession._open_ws_with_retry("ws://t")
		self.assertIs(ws, sentinel)
		self.assertEqual(cc.call_count, 3)
		self.assertEqual(sleep.call_count, 2)

	def test_ws_open_gives_up_after_deadline(self):
		# Deadline 0 -> the first failure is terminal (no retry) and the error
		# carries the "starting up" hint instead of the raw timeout.
		cc = MagicMock(side_effect=OSError("timed out"))
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", cc),
			patch("jarvis.chat.agent_client.time.sleep"),
			patch("jarvis.chat.agent_client.CONNECT_OPEN_DEADLINE_SECONDS", 0),
		):
			with self.assertRaises(AgentUnreachableError) as cm:
				AgentSession._open_ws_with_retry("ws://t")
		self.assertEqual(cc.call_count, 1)
		self.assertIn("starting up", str(cm.exception))


# --- TestCreateSession ----------------------------------------------------


class TestCreateSession(FrappeTestCase):
	def test_returns_key_on_ok(self):
		sess, ws = _build_session()

		def _resp():
			req_id = ws.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": True, "payload": {"key": "session-abc"}})

		ws._frames.append(_resp)
		key = sess.create_session()
		self.assertEqual(key, "session-abc")

	def test_rejection_raises(self):
		sess, ws = _build_session()

		def _resp():
			req_id = ws.sent[-1]["id"]
			return _frame(
				{"type": "res", "id": req_id, "ok": False, "error": {"code": "BAD", "message": "no"}}
			)

		ws._frames.append(_resp)
		with self.assertRaises(AgentUnreachableError):
			sess.create_session()


# --- TestStreamAgentTurn --------------------------------------------------


class TestStreamAgentTurn(FrappeTestCase):
	def test_completes_on_lifecycle_end(self):
		sess, ws = _build_session()

		def _ack():
			req_id = ws.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": True, "payload": {"runId": "run-1"}})

		ws._frames.append(_ack)
		ws._frames.append(
			_frame(
				{
					"type": "event",
					"event": "agent.event",
					"payload": {"runId": "run-1", "stream": "lifecycle", "data": {"phase": "end"}},
				}
			)
		)
		# Streams to completion (no items required to be yielded for the
		# completion-path test - parse_event filters its own shapes).
		list(sess.stream_agent_turn("session-x", "hi", "idem-1"))

	def test_agent_rejection_raises(self):
		sess, ws = _build_session()

		def _nack():
			req_id = ws.sent[-1]["id"]
			return _frame(
				{"type": "res", "id": req_id, "ok": False, "error": {"code": "BAD_REQ", "message": "x"}}
			)

		ws._frames.append(_nack)
		with self.assertRaises(AgentUnreachableError):
			list(sess.stream_agent_turn("s", "hi", "i"))

	def test_other_runs_dropped_during_streaming(self):
		sess, ws = _build_session()

		def _ack():
			req_id = ws.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": True, "payload": {"runId": "run-A"}})

		ws._frames.append(_ack)
		ws._frames.append(
			_frame(
				{
					"type": "event",
					"event": "agent.event",
					"payload": {"runId": "run-B", "stream": "text", "data": {"delta": "wrong run"}},
				}
			)
		)
		ws._frames.append(
			_frame(
				{
					"type": "event",
					"event": "agent.event",
					"payload": {"runId": "run-A", "stream": "lifecycle", "data": {"phase": "end"}},
				}
			)
		)
		# Should terminate cleanly; cross-run event silently dropped.
		list(sess.stream_agent_turn("s", "hi", "i"))

	def test_agent_payload_omits_model_by_default(self):
		"""Backward-compat: omitting model/provider sends the same payload as before."""
		sess, ws = _build_session()

		def _ack():
			req_id = ws.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": True, "payload": {"runId": "r1"}})

		ws._frames.append(_ack)
		ws._frames.append(
			_frame(
				{
					"type": "event",
					"event": "agent.event",
					"payload": {"runId": "r1", "stream": "lifecycle", "data": {"phase": "end"}},
				}
			)
		)
		list(sess.stream_agent_turn("sess-1", "hi", "idem-1"))
		agent_req = [s for s in ws.sent if s.get("method") == "agent"][0]
		params = agent_req["params"]
		self.assertNotIn("model", params)
		self.assertNotIn("provider", params)
		self.assertEqual(params["message"], "hi")
		self.assertEqual(params["sessionKey"], "sess-1")

	def test_agent_payload_includes_model_and_provider_when_set(self):
		"""Per-turn model override threads into the agent RPC params."""
		sess, ws = _build_session()

		def _ack():
			req_id = ws.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": True, "payload": {"runId": "r2"}})

		ws._frames.append(_ack)
		ws._frames.append(
			_frame(
				{
					"type": "event",
					"event": "agent.event",
					"payload": {"runId": "r2", "stream": "lifecycle", "data": {"phase": "end"}},
				}
			)
		)
		list(
			sess.stream_agent_turn(
				"sess-2",
				"hi",
				"idem-2",
				model="gpt-5.4-mini",
				provider="openai-codex",
			)
		)
		agent_req = [s for s in ws.sent if s.get("method") == "agent"][0]
		params = agent_req["params"]
		self.assertEqual(params["model"], "gpt-5.4-mini")
		self.assertEqual(params["provider"], "openai-codex")

	def test_agent_payload_includes_only_model_when_provider_omitted(self):
		"""Either kwarg may be set independently; absent ones aren't emitted."""
		sess, ws = _build_session()

		def _ack():
			req_id = ws.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": True, "payload": {"runId": "r3"}})

		ws._frames.append(_ack)
		ws._frames.append(
			_frame(
				{
					"type": "event",
					"event": "agent.event",
					"payload": {"runId": "r3", "stream": "lifecycle", "data": {"phase": "end"}},
				}
			)
		)
		list(sess.stream_agent_turn("sess-3", "hi", "idem-3", model="gpt-5.5"))
		agent_req = [s for s in ws.sent if s.get("method") == "agent"][0]
		params = agent_req["params"]
		self.assertEqual(params["model"], "gpt-5.5")
		self.assertNotIn("provider", params)


# --- TestClose ------------------------------------------------------------


class TestClose(FrappeTestCase):
	def test_close_is_safe_to_call_twice(self):
		sess, ws = _build_session()
		sess.close()
		sess.close()
		self.assertTrue(ws.closed)


# --- TestSelfHeal ---------------------------------------------------------


class TestSelfHealOnStalePairing(FrappeTestCase):
	"""When admin re-provisions a tenant, the new container has no record of
	the customer's existing chat_device_*. The first WS connect fails with
	one of agent's pairing-stale errors. agent_client should detect
	that, clear local creds, re-pair via ensure_paired, and retry the WS
	once. A second stale signal is a real failure (no infinite loop)."""

	def _build_two_ws(self, first_reject_marker: str, second_ok: bool, first_error: dict | None = None):
		"""Return two scripted WS instances: first one rejects connect with
		`first_reject_marker` in the error message (or the full `first_error`
		dict when given); second one accepts."""

		# Sprint-3 (2026-06-16 review): agent's rejection payload puts
		# the marker in error.code, not error.message. The classifier on
		# the client side now reads the structured code rather than
		# substring-matching the message text, so the scripted fixture
		# must place the marker accordingly.
		error = first_error or {"code": first_reject_marker, "message": "stale"}

		def _first_nack():
			req_id = first.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": False, "error": error})

		def _second_ok():
			req_id = second.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": True, "payload": {}})

		def _second_nack():
			req_id = second.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": False, "error": error})

		first = _ScriptedWS([_challenge(), _first_nack])
		second_response = _second_ok if second_ok else _second_nack
		second = _ScriptedWS([_challenge(), second_response])
		return first, second

	def _connect_with_two_ws(self, first_ws, second_ws):
		ws_iter = iter([first_ws, second_ws])
		clear_called: list = []

		def _fake_clear():
			clear_called.append(True)

		creds = _make_creds()
		ensure_paired_calls: list = []

		def _fake_ensure_paired():
			ensure_paired_calls.append(True)
			return creds

		# _repair_and_reconnect's convoy guards read the REAL site's
		# Jarvis Settings (chat_device_id + chat_device_token); on a dev
		# site with a live pairing they differ from the test creds and
		# the guards skip the wipe ("another worker already re-paired" /
		# "a peer already adopted a rotated token"). Pin both to "no
		# winner yet" so these tests are hermetic instead of depending
		# on whatever the local site happens to hold.
		with (
			patch(
				"jarvis.chat.agent_client.websocket.create_connection",
				side_effect=lambda *a, **kw: next(ws_iter),
			),
			patch("jarvis.chat.agent_client.ensure_paired", side_effect=_fake_ensure_paired),
			patch("jarvis.chat.agent_client._persisted_device_id", return_value=""),
			patch("jarvis.chat.agent_client._persisted_device_token", return_value=""),
			patch("jarvis.chat.agent_client.clear_credentials", side_effect=_fake_clear),
		):
			result = AgentSession.connect("ws://t")
		return result, clear_called, ensure_paired_calls

	def test_device_not_paired_triggers_repair_and_retry_succeeds(self):
		first_ws, second_ws = self._build_two_ws("device-not-paired", second_ok=True)
		sess, clears, ensure_calls = self._connect_with_two_ws(first_ws, second_ws)
		# clear_credentials was called once between the two attempts.
		self.assertEqual(len(clears), 1)
		# ensure_paired was called twice (once per attempt).
		self.assertEqual(len(ensure_calls), 2)
		self.assertTrue(first_ws.closed)
		self.assertFalse(second_ws.closed)
		sess.close()

	def test_token_revoked_also_triggers_repair(self):
		first_ws, second_ws = self._build_two_ws("token-revoked", second_ok=True)
		sess, clears, _ = self._connect_with_two_ws(first_ws, second_ws)
		self.assertEqual(len(clears), 1)
		sess.close()

	# agent's REAL wire shape for a device-token auth rejection, captured
	# verbatim from a live gateway (2026-07-09, reproduction against
	# ghcr.io/openclaw/openclaw): the generic INVALID_REQUEST code with the
	# machine-readable reason only in error.details.authReason. This is what
	# a bench actually receives after its tenant container is replaced (the
	# gateway maps device-not-paired / token-mismatch / token-revoked all to
	# this one frame for explicit-deviceToken connects).
	_WIRE_AUTH_REJECTION = {
		"code": "INVALID_REQUEST",
		"message": "unauthorized: device token mismatch (rotate/reissue device token)",
		"details": {
			"code": "AUTH_DEVICE_TOKEN_MISMATCH",
			"authReason": "device_token_mismatch",
			"canRetryWithDeviceToken": False,
			"recommendedNextStep": "update_auth_credentials",
		},
	}

	def test_wire_shape_invalid_request_auth_reason_triggers_repair(self):
		"""The 2026-07-08 post-deploy regression: agent rejects with
		code=INVALID_REQUEST + details.authReason=device_token_mismatch.
		The code-only classifier missed it and chat stayed permanently
		broken after a tenant container was replaced. The classifier must
		fire on details.authReason."""
		first_ws, second_ws = self._build_two_ws(
			"",
			second_ok=True,
			first_error=self._WIRE_AUTH_REJECTION,
		)
		sess, clears, ensure_calls = self._connect_with_two_ws(first_ws, second_ws)
		self.assertEqual(len(clears), 1)
		self.assertEqual(len(ensure_calls), 2)
		sess.close()

	def test_peer_adopted_rotation_skips_the_wipe_but_still_retries(self):
		"""A gateway token ROTATION keeps the device_id, so when worker B
		is rejected (it signed with the pre-rotation token) while worker A
		already adopted the reissued token, the repair path must NOT wipe
		the healed pairing - but must still retry with fresh creds."""
		first_ws, second_ws = self._build_two_ws(
			"",
			second_ok=True,
			first_error=self._WIRE_AUTH_REJECTION,
		)
		ws_iter = iter([first_ws, second_ws])
		creds = _make_creds()
		clear_called: list = []
		with (
			patch(
				"jarvis.chat.agent_client.websocket.create_connection",
				side_effect=lambda *a, **kw: next(ws_iter),
			),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
			patch("jarvis.chat.agent_client._persisted_device_id", return_value=creds.device_id),
			patch("jarvis.chat.agent_client._persisted_device_token", return_value="tok-rotated-by-peer"),
			patch(
				"jarvis.chat.agent_client.clear_credentials", side_effect=lambda: clear_called.append(True)
			),
		):
			sess = AgentSession.connect("ws://t")
		self.assertFalse(
			clear_called,
			"a pairing healed by a peer's token adoption must not be wiped",
		)
		sess.close()

	def test_invalid_request_without_auth_details_does_not_trigger_repair(self):
		"""INVALID_REQUEST covers every malformed-request rejection (bad
		params, protocol mismatch, ...). Without details.authReason in the
		stale set it must NOT wipe valid credentials - even when the
		message text happens to mention a token mismatch."""
		creds = _make_creds()

		def _nack():
			req_id = first_ws.sent[-1]["id"]
			return _frame(
				{
					"type": "res",
					"id": req_id,
					"ok": False,
					"error": {
						"code": "INVALID_REQUEST",
						"message": "unauthorized: device token mismatch (rotate/reissue device token)",
					},
				}
			)

		first_ws = _ScriptedWS([_challenge(), _nack])
		clear_called: list = []
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", return_value=first_ws),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
			patch(
				"jarvis.chat.agent_client.clear_credentials", side_effect=lambda: clear_called.append(True)
			),
		):
			with self.assertRaises(AgentUnreachableError):
				AgentSession.connect("ws://t")
		self.assertFalse(
			clear_called,
			"INVALID_REQUEST without details.authReason must not wipe creds",
		)

	def test_second_failure_does_not_loop(self):
		"""After one repair attempt, a second stale-pairing signal must
		propagate as a real failure - not a third retry."""
		first_ws, second_ws = self._build_two_ws("device-not-paired", second_ok=False)
		with self.assertRaises(AgentUnreachableError):
			self._connect_with_two_ws(first_ws, second_ws)

	def test_signature_invalid_does_not_trigger_repair(self):
		"""A signing bug must surface, not get masked by a silent re-pair.
		device-signature-invalid means our client code is broken; clearing
		creds won't help and the operator needs to see the original error.

		Sprint-3 (2026-06-16): the classifier now reads the structured
		``code`` attribute, not the message. Putting the marker in
		``error.code`` is the production wire shape; this test pins that
		signature-invalid is NOT in the stale-pairing code set."""
		creds = _make_creds()

		def _nack():
			req_id = first_ws.sent[-1]["id"]
			return _frame(
				{
					"type": "res",
					"id": req_id,
					"ok": False,
					"error": {"code": "device-signature-invalid", "message": "bad sig"},
				}
			)

		first_ws = _ScriptedWS([_challenge(), _nack])
		clear_called: list = []
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", return_value=first_ws),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
			patch(
				"jarvis.chat.agent_client.clear_credentials", side_effect=lambda: clear_called.append(True)
			),
		):
			with self.assertRaises(AgentUnreachableError) as cm:
				AgentSession.connect("ws://t")
		self.assertFalse(clear_called, "should not clear creds for signing bugs")
		self.assertEqual(cm.exception.code, "device-signature-invalid")

	def test_message_substring_does_not_trigger_repair(self):
		"""Sprint-3: pre-fix bug class - the classifier USED to substring-
		match str(err).lower(), so any future error whose message text
		happened to embed one of the stale-pairing markers (a log dump,
		a partial-match code like 'device-not-paired-yet') would
		false-positive and wipe valid credentials. The structured-code
		classifier prevents that."""
		creds = _make_creds()

		def _nack():
			req_id = first_ws.sent[-1]["id"]
			return _frame(
				{
					"type": "res",
					"id": req_id,
					"ok": False,
					"error": {"code": "diagnostic-dump", "message": "context: device-not-paired log line"},
				}
			)

		first_ws = _ScriptedWS([_challenge(), _nack])
		clear_called: list = []
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", return_value=first_ws),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
			patch(
				"jarvis.chat.agent_client.clear_credentials", side_effect=lambda: clear_called.append(True)
			),
		):
			with self.assertRaises(AgentUnreachableError):
				AgentSession.connect("ws://t")
		self.assertFalse(
			clear_called,
			"a substring match in the message must NOT trigger the repair "
			"path - we read the structured error.code",
		)


class TestReissuedTokenAdoption(FrappeTestCase):
	"""agent's hello-ok can carry a REISSUED auth.deviceToken: the
	gateway rotates the stored device token at connect whenever the
	existing entry no longer lines up with the requested scopes/issuer,
	and the rotation is already durable gateway-side when hello-ok
	arrives. A client that drops it signs every FOLLOWING connect with
	the dead token -> "device token mismatch". The client must persist
	and adopt it."""

	def _connect(self, hello_auth: dict, creds=None, update_mock=None):
		creds = creds or _make_creds()
		update_mock = update_mock if update_mock is not None else MagicMock(return_value=True)

		def _ok():
			req_id = ws.sent[-1]["id"]
			return _frame({"type": "res", "id": req_id, "ok": True, "payload": {"auth": hello_auth}})

		ws = _ScriptedWS([_challenge(), _ok])
		with (
			patch("jarvis.chat.agent_client.websocket.create_connection", return_value=ws),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
			patch("jarvis.chat.agent_client.update_device_token", update_mock),
		):
			sess = AgentSession.connect("ws://t")
		return sess, creds, update_mock

	def test_reissued_token_is_persisted_and_adopted(self):
		sess, creds, update_mock = self._connect(
			{"scopes": ["operator.write"], "deviceToken": "tok-rotated"},
		)
		update_mock.assert_called_once_with(
			"tok-rotated",
			device_id=creds.device_id,
		)
		self.assertEqual(sess._creds.device_token, "tok-rotated")
		sess.close()

	def test_no_device_token_in_hello_ok_is_a_noop(self):
		sess, creds, update_mock = self._connect(
			{"scopes": ["operator.write", "operator.admin"]},
		)
		update_mock.assert_not_called()
		self.assertEqual(sess._creds.device_token, creds.device_token)
		sess.close()

	def test_unchanged_token_is_a_noop(self):
		creds = _make_creds()
		sess, _, update_mock = self._connect(
			{"deviceToken": creds.device_token},
			creds=creds,
		)
		update_mock.assert_not_called()
		sess.close()

	def test_persist_refusal_still_adopts_in_memory(self):
		"""update_device_token returns False when another worker re-paired
		mid-connect (Settings holds a different device). The reissued token
		is still the right credential for THIS session's device."""
		sess, _, update_mock = self._connect(
			{"deviceToken": "tok-rotated"},
			update_mock=MagicMock(return_value=False),
		)
		self.assertEqual(sess._creds.device_token, "tok-rotated")
		sess.close()

	def test_persist_failure_does_not_fail_the_turn(self):
		"""A persistence hiccup must not kill the user's current turn; the
		stale-pairing self-heal recovers the NEXT connect."""
		sess, _, update_mock = self._connect(
			{"deviceToken": "tok-rotated"},
			update_mock=MagicMock(side_effect=RuntimeError("db down")),
		)
		self.assertEqual(sess._creds.device_token, "tok-rotated")
		sess.close()

	def test_malformed_hello_ok_payload_never_fails_the_connect(self):
		"""A successful connect must tolerate ANY hello-ok payload shape
		(pre-adoption behavior ignored the payload entirely): payload as a
		list, auth as a scalar, deviceToken as a non-string."""
		creds = _make_creds()
		for payload in (
			["unexpected"],
			{"auth": True},
			{"auth": "tok"},
			{"auth": {"deviceToken": 42}},
			None,
			"str",
		):

			def _ok(payload=payload):
				req_id = ws.sent[-1]["id"]
				return _frame({"type": "res", "id": req_id, "ok": True, "payload": payload})

			ws = _ScriptedWS([_challenge(), _ok])
			update_mock = MagicMock(return_value=True)
			with (
				patch("jarvis.chat.agent_client.websocket.create_connection", return_value=ws),
				patch("jarvis.chat.agent_client.ensure_paired", return_value=creds),
				patch("jarvis.chat.agent_client.update_device_token", update_mock),
			):
				sess = AgentSession.connect("ws://t")
			update_mock.assert_not_called()
			self.assertEqual(sess._creds.device_token, creds.device_token)
			sess.close()


class TestRepairConvoyCollapse(FrappeTestCase):
	"""Sprint-2 (2026-06-16): N concurrent workers all observe a stale
	pairing after tenant re-provision. Without the Redis lock, every
	worker calls clear_credentials() + ensure_paired() independently,
	each generating a different Ed25519 keypair, and only the last
	writer's keypair survives in Jarvis Settings - the others hold
	in-memory creds the admin side doesn't know about. The lock
	collapses the convoy: one worker pairs, the rest detect the new
	device_id on disk and skip the wipe."""

	def _build_stale_then_ok(self) -> tuple[_ScriptedWS, _ScriptedWS]:
		"""Two scripted WS: first rejects with device-not-paired, second
		accepts. Caller wires per-test patches on top."""
		first = _ScriptedWS([_challenge(), None])
		second = _ScriptedWS([_challenge(), None])
		first._frames[1] = lambda: _frame(
			{
				"type": "res",
				"id": first.sent[-1]["id"],
				"ok": False,
				# Sprint-3: marker in code, not message - matches agent's wire shape.
				"error": {"code": "device-not-paired", "message": "stale"},
			}
		)
		second._frames[1] = lambda: _frame(
			{
				"type": "res",
				"id": second.sent[-1]["id"],
				"ok": True,
				"payload": {},
			}
		)
		return first, second

	def test_repair_skipped_when_persisted_device_id_diverges(self):
		"""Late arrival: the lock-holder already re-paired and wrote a
		new device_id to Settings. The convoy follower sees current !=
		its stale_device_id and SHOULD NOT wipe the winner's work."""
		stale_creds = _make_creds()
		winner_device_id = "winner-device-id-from-other-worker"
		clear_called: list = []

		first_ws, second_ws = self._build_stale_then_ok()
		ws_iter = iter([first_ws, second_ws])
		with (
			patch(
				"jarvis.chat.agent_client.websocket.create_connection",
				side_effect=lambda *a, **kw: next(ws_iter),
			),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=stale_creds),
			patch(
				"jarvis.chat.agent_client.clear_credentials", side_effect=lambda: clear_called.append(True)
			),
			patch("jarvis.chat.agent_client._persisted_device_id", return_value=winner_device_id),
		):
			sess = AgentSession.connect("ws://t")
		self.assertEqual(clear_called, [], "convoy follower must NOT clear the winner's freshly-paired creds")
		sess.close()

	def test_repair_proceeds_when_persisted_id_matches_stale(self):
		"""Lock-holder path: nobody else has re-paired yet (current ==
		stale OR current empty). Standard wipe + re-pair."""
		stale_creds = _make_creds()
		clear_called: list = []

		first_ws, second_ws = self._build_stale_then_ok()
		ws_iter = iter([first_ws, second_ws])
		with (
			patch(
				"jarvis.chat.agent_client.websocket.create_connection",
				side_effect=lambda *a, **kw: next(ws_iter),
			),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=stale_creds),
			patch(
				"jarvis.chat.agent_client.clear_credentials", side_effect=lambda: clear_called.append(True)
			),
			patch("jarvis.chat.agent_client._persisted_device_id", return_value=stale_creds.device_id),
			patch("jarvis.chat.agent_client._persisted_device_token", return_value=stale_creds.device_token),
		):
			sess = AgentSession.connect("ws://t")
		self.assertEqual(
			len(clear_called), 1, "lock-holder must wipe + re-pair when persisted id matches stale"
		)
		sess.close()

	def test_repair_skipped_when_redis_lock_unavailable(self):
		"""Defensive: if the lock acquire times out (Redis down,
		extreme contention), we DON'T wipe creds blindly. The caller
		retries once with allow_repair=False against the same gateway."""
		from contextlib import contextmanager

		stale_creds = _make_creds()
		clear_called: list = []

		@contextmanager
		def _never_acquired(*_a, **_kw):
			yield False

		first_ws, second_ws = self._build_stale_then_ok()
		ws_iter = iter([first_ws, second_ws])
		with (
			patch(
				"jarvis.chat.agent_client.websocket.create_connection",
				side_effect=lambda *a, **kw: next(ws_iter),
			),
			patch("jarvis.chat.agent_client.ensure_paired", return_value=stale_creds),
			patch(
				"jarvis.chat.agent_client.clear_credentials", side_effect=lambda: clear_called.append(True)
			),
			patch("jarvis._redis_lock.redis_lock", side_effect=_never_acquired),
		):
			sess = AgentSession.connect("ws://t")
		self.assertEqual(clear_called, [], "no clear when we never held the lock")
		sess.close()


# --- TestSessionsListModelIdentity ----------------------------------------


class TestSessionsListModelIdentity(FrappeTestCase):
	"""jarvis#560: the model that answered a turn crosses the wire ONLY on the
	``sessions.list`` row, so this pins that frame.

	No live event carries it: both live ``emitChatFinal`` implementations in
	ghcr.io/openclaw/openclaw:2026.6.8 build the terminal chat message fresh as
	``{role, content, timestamp}``, and the lifecycle end/error frame carries only
	``phase`` plus terminal metadata. The runtime logs
	``model=... provider=...`` at agent end but never emits it. The durable
	session row is therefore the earliest honest source, and these frames are the
	shape ``buildGatewaySessionRow`` actually sends.
	"""

	def _sessions_list_frame(self, ws, rows):
		def _resp():
			req_id = ws.sent[-1]["id"]
			return _frame(
				{
					"type": "res",
					"id": req_id,
					"ok": True,
					"payload": {
						"ts": 1785348175440,
						"count": len(rows),
						"defaults": {"model": "gemini-3.6-flash", "provider": "gemini"},
						"sessions": rows,
					},
				}
			)

		return _resp

	def test_row_carries_model_and_provider_through_the_client(self):
		sess, ws = _build_session()
		row = {
			"key": "agent:main",
			"hasActiveRun": False,
			"model": "glm-4.7",
			"modelProvider": "openai_compat",
			"totalTokens": 41230,
			"totalTokensFresh": True,
			"inputTokens": 39102,
			"outputTokens": 812,
		}
		ws._frames.append(self._sessions_list_frame(ws, [row]))
		rows = sess.list_sessions()
		self.assertEqual(ws.sent[-1]["method"], "sessions.list")
		self.assertEqual(len(rows), 1)

		from jarvis.chat.usage import resolved_model_identity

		self.assertEqual(
			resolved_model_identity(rows[0]),
			("glm-4.7", "openai_compat"),
			"the model/provider pair survives the frame and decodes off the wire names",
		)

	def test_row_that_names_no_model_decodes_as_blank(self):
		# An agent-direct pool that never resolved a model omits both fields;
		# the decoder must answer "" rather than invent one, so the assistant row
		# is left unattributed instead of falsely attributed.
		sess, ws = _build_session()
		row = {"key": "agent:main", "hasActiveRun": False, "totalTokensFresh": True}
		ws._frames.append(self._sessions_list_frame(ws, [row]))
		rows = sess.list_sessions()

		from jarvis.chat.usage import resolved_model_identity

		self.assertEqual(resolved_model_identity(rows[0]), ("", ""))
		self.assertEqual(resolved_model_identity(None), ("", ""))
