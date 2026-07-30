"""Openclaw gateway client - direct WebSocket with device-paired auth.

Previously this module shelled out to `docker compose exec ... node -e <script>`
so the WS connection would appear as loopback inside the container - openclaw
strips self-declared `operator.write` scopes from non-loopback token-only
clients, and the chat worker needed write scope for sessions.create.

Now we do it the way openclaw was designed for: pair the customer bench as
a device once (via jarvis.chat.device.ensure_paired → admin → fleet-agent),
and present an Ed25519-signed v3 device-auth envelope at every connect.
openclaw verifies the signature against the registered public key, grants
the requested scopes, and the rest of the protocol (sessions.create, agent,
event streaming) is identical to what the Node script used to do - only the
transport changed from subprocess-pipes to direct WS frames.

Public surface: AgentSession.connect / create_session / chat_send /
relay_turn_events / set_session_model / subscribe_session / get_history /
get_session_messages / is_run_active / fire_agent / stream_agent_turn /
close. The managed chat path now uses the relay pair (chat_send +
relay_turn_events): the turn is owned by openclaw after the chat.send ack,
streaming rides the broadcast agent frames, completion comes from the
run-scoped chat event, and relay_turn_events never raises after entry -
interruptions degrade to snapshot recovery instead of errors.
stream_agent_turn remains for auto-title; fire_agent for prewarm.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import websocket

_logger = logging.getLogger(__name__)

from jarvis.chat.device import (
	ChatDeviceCredentials,
	build_payload_v3,
	clear_credentials,
	ensure_paired,
	sign_payload,
	update_device_token,
)
from jarvis.chat.events import parse_event

# Openclaw gateway's WS frame format. Looks JSON-RPC-shaped but has its
# own type discriminator (``"type": "req"|"res"|"event"``) rather than
# JSON-RPC 2.0's ``"jsonrpc": "2.0"`` field, so a generic JSON-RPC
# library doesn't fit. The frame builders here are the (small,
# explicit) protocol seam between the bench and openclaw's WS;
# centralised so the next time we need to add a frame field (e.g.
# trace IDs, span context) it lands in one place rather than at every
# ws.send site. Punch-list item "JSON-RPC framing reinvented" from
# the 2026-06-16 review.


def _build_request_frame(method: str, params: dict, *, req_id: str | None = None) -> tuple[str, str]:
	"""Build a serialised openclaw request frame.

	Returns (json_payload, req_id) - caller passes json_payload to
	ws.send and uses req_id to match the response frame.
	"""
	rid = req_id or uuid.uuid4().hex
	return json.dumps(
		{
			"type": "req",
			"id": rid,
			"method": method,
			"params": params,
		}
	), rid


def _is_response_frame(frame: dict, req_id: str) -> bool:
	"""True when ``frame`` is the response to the request that issued
	``req_id``. Encapsulates the (type, id) discrimination so the two
	caller sites don't open-code it independently."""
	return frame.get("type") == "res" and frame.get("id") == req_id


from jarvis.exceptions import AgentUnreachableError

CONNECT_TIMEOUT_SECONDS = 10

# A dormant / just-recreated container's gateway is unreachable for ~10-30s
# while `compose up -d` recreates it (see jarvis/api.py rotate_agent_token) or
# while a rotated-dormant container spins back up. A single 10s connect with no
# retry guarantees a first-turn failure ("WS open failed: Connection timed out")
# against a cold container, so we retry the WS OPEN - network-level failures
# only, NOT handshake/auth - until a total deadline, sleeping briefly between
# attempts. A warm container connects on the first attempt (no added latency);
# only a genuinely cold/dormant one pays the retry window.
CONNECT_OPEN_DEADLINE_SECONDS = 25
CONNECT_OPEN_RETRY_BACKOFF_SECONDS = 2.0

# openclaw v2026.6.8 (issue #29385) enforces gateway.controlUi.allowedOrigins on
# LAN binds since v2026.2.26 and no longer honors a "*" wildcard: a
# `--bind lan` gateway seeds ["http://localhost:18789", "http://127.0.0.1:18789"]
# (its own internal port, a fixed constant) as the ONLY allowed origins. Our
# server-side WS client isn't a browser, so it just has to present an Origin
# openclaw accepts — send one of the seeded values, NOT a bare "http://localhost"
# (which the old "*" config used to wave through and now gets rejected).
_GATEWAY_ORIGIN = "http://127.0.0.1:18789"
# Wall-clock cap on one agent turn waiting for the WS lifecycle to
# complete (final lifecycle "end" frame from openclaw). Was 180s when
# the bench + openclaw container ran on the same host (sub-ms WS
# RTT); bumped to 600s after a Frappe-Cloud-hosted bench + Hetzner-
# hosted openclaw deploy hit the 180s cap on multi-recipient
# announcement turns. Each tool call now crosses the public internet
# (~50-200ms WAN RTT depending on FC↔Hetzner region pairing); a
# typical multi-step turn with ~30 tool calls + a 26-recipient batch
# send hit ~4-8s of pure network overhead alone, then ran past the
# 180s ceiling before openclaw emitted lifecycle-end. The row-guard
# walk-back (jarvis#134) cut the tool-call count meaningfully but
# WAN latency is a structural floor; the timeout has to absorb it.
# The matching RQ envelope ``_AGENT_TURN_WORKER_TIMEOUT`` in
# jarvis/chat/api.py was bumped in lockstep so the worker has room
# for this turn + pair + WS connect overhead.
TURN_TIMEOUT_SECONDS = 600

# --- pinned one-shots (issue #531) ------------------------------------------
#
# ``fire_agent`` / ``stream_agent_turn`` are the only two RPCs here that carry
# an explicit model/provider, and passing either one COSTS the run its whole
# failover chain. Verified against the shipped bundle of
# ghcr.io/openclaw/openclaw:2026.6.8:
#
#   agent-command: hasExplicitRunOverride = Boolean(opts.provider || opts.model)
#                  resolveEffectiveModelFallbacks({
#                      hasSessionModelOverride: hasExplicitRunOverride || ...,
#                      modelOverrideSource: hasExplicitRunOverride ? "user" : ...,
#                  })
#   agent-scope:   if (!(modelOverrideSource === "auto" || ...)) return [];
#
# So an explicit per-request model hardcodes source "user" and the effective
# fallback list collapses to []. There is no way to opt back in: AgentParamsSchema
# is ``additionalProperties: false`` over {message, agentId, provider, model, ...}
# with no modelOverrideSource / allowFallbacks knob, and sessions.patch rejects
# the marker outright (closed PR #517).
#
# The pin is deliberate and must NOT be "fixed" by dropping it: prewarm exists
# to warm ONE model's prefix cache (a failover would warm the wrong one), and
# auto-title deliberately bills against a cheap model rather than the pool
# primary. What was wrong was that the resulting openclaw log
# (``decision=surface_error ... next=none``) read exactly like a dead chain.
#
# Both halves of the fix live here, at the boundary that actually puts model on
# the wire, so they cannot drift from the RPC params:
#
#  1. a pinned run id is prefixed differently from an unpinned one, which makes
#     openclaw's own ``embedded run failover decision: runId=... next=none``
#     line self-describing without cross-referencing anything;
#  2. the bench logs one line per pinned turn naming the run id and the pinned
#     model, so the jarvis-side log answers the question on its own too.
#
# openclaw uses the idempotency key verbatim as the run id
# (``const idem = request.idempotencyKey; const runId = idem;``), so the prefix
# survives to every failover log line. Both must stay clear of openclaw's own
# ``exec-approval-followup:`` key namespace, and stay well under the 200-char
# console truncation.
#
# The two prefixes have to track the RPC params exactly. A one-shot whose model
# resolved empty is NOT pinned - it keeps its fallbacks - so a ``next=none``
# under ONESHOT_RUN_PREFIX really does mean the chain is dead, and claiming
# otherwise would recreate the very ambiguity this exists to remove.
PINNED_ONESHOT_RUN_PREFIX = "jarvis-pinned-"
ONESHOT_RUN_PREFIX = "jarvis-oneshot-"


def oneshot_run_id(kind: str, unique: str, *, model: str | None, provider: str | None) -> str:
	"""Idempotency key (== openclaw run id) for a throwaway one-shot turn.

	``kind`` names the feature (title / polish / prewarm); ``unique`` is
	whatever already made the caller's key unique, kept so the run id still
	points back at its session. openclaw dedupes on this key, so ``unique``
	must stay per-call. Pass the same model/provider the turn will send: they
	decide which prefix the run id gets."""
	prefix = PINNED_ONESHOT_RUN_PREFIX if (model or provider) else ONESHOT_RUN_PREFIX
	return f"{prefix}{kind}:{unique}"


def _log_pinned_oneshot(run_id: str, model: str | None, provider: str | None) -> None:
	"""Record that this turn traded failover away for a pinned model.

	Keyed on the run id so it joins straight onto openclaw's
	``embedded run failover decision: runId=...`` line. Without this a
	``next=none`` from a pinned one-shot is indistinguishable from an
	exhausted chain (issue #531)."""
	_logger.info(
		"pinned one-shot turn: runId=%s model=%s provider=%s fallbacks=none-by-design "
		"reason=explicit-model-on-agent-rpc",
		run_id,
		model or "-",
		provider or "-",
	)


# Scopes the chat path needs: operator.write for sessions.create + agent;
# operator.admin so the same connection can also read state (status snapshots,
# etc.) without re-pairing.
_REQUESTED_SCOPES = ["operator.write", "operator.admin"]
_CLIENT_ID = "gateway-client"
_CLIENT_MODE = "backend"
_ROLE = "operator"
_PLATFORM = "linux"  # informational; only affects the v3 signature payload

# openclaw rejection codes that indicate the customer's stored pairing
# is stale for the CURRENT container (typically because admin re-provisioned
# the tenant and the new container has no record of this deviceId). On
# any of these we wipe + re-pair once. OTHER rejection codes
# (signature-invalid, scope-mismatch, etc.) are programming bugs / config
# errors and must NOT trigger a retry - that'd hide the bug behind silent
# re-pair attempts that destroy valid credentials.
#
# Sprint-3 (2026-06-16 review): we used to substring-match on
# ``str(err).lower()`` which would false-positive any future error
# embedding one of these tokens in its message text (think log lines,
# diagnostic dumps, partial-match codes like ``device-not-paired-yet``).
# The classifier now reads the structured ``.code`` attribute populated
# at the raise site from openclaw's response envelope.
_STALE_PAIRING_CODES = frozenset(
	{
		"device-not-paired",
		"token-mismatch",
		"token-revoked",
		"device-id-mismatch",
	}
)

# openclaw's ACTUAL wire shape for device-token auth failures (verified
# 2026-07-09 against the ghcr.io/openclaw/openclaw image from 2026-06-16
# AND current :latest, plus a live reproduction against a running
# gateway): connect is rejected with the GENERIC ``error.code``
# "INVALID_REQUEST" and the machine-readable reason lives in
# ``error.details.authReason``. The gateway maps EVERY explicit-
# deviceToken auth failure - device not paired (container replaced /
# state reset), token mismatch (gateway rotated the stored token),
# token revoked - to the single coarse reason "device_token_mismatch"
# ("unauthorized: device token mismatch (rotate/reissue device token)").
# The hyphenated reasons in _STALE_PAIRING_CODES are openclaw's INTERNAL
# verifyDeviceToken results; they never reach the wire as error.code, so
# the code-only classifier alone never fired and the self-heal below was
# dead code - a replaced tenant container permanently broke chat
# (2026-07-08 post-deploy regression). _STALE_PAIRING_CODES is kept as a
# belt-and-braces layer in case a future openclaw promotes the internal
# reasons to wire codes.
_STALE_PAIRING_AUTH_REASONS = frozenset(
	{
		"device_token_mismatch",
	}
)


def _is_stale_pairing(err: Exception) -> bool:
	"""Return True iff ``err`` is an AgentUnreachableError that openclaw
	classified as a stale device pairing - either via error.code (internal
	reason set) or via error.details.authReason (the real wire shape for
	connect auth failures, see _STALE_PAIRING_AUTH_REASONS above).

	Strictly typed: an error WITHOUT a ``.code``/``.details`` attribute
	(network-level failure, programmer bug) never triggers the repair
	path, and neither does a marker string embedded in the message text.
	"""
	code = getattr(err, "code", None)
	if code in _STALE_PAIRING_CODES:
		return True
	details = getattr(err, "details", None)
	if not isinstance(details, dict):
		return False
	return details.get("authReason") in _STALE_PAIRING_AUTH_REASONS


def _chat_final_text(payload: dict) -> str | None:
	"""Authoritative final text from a chat final event: joined text blocks
	of payload.message.content; None for silent finals."""
	msg = payload.get("message")
	if not isinstance(msg, dict):
		return None
	c = msg.get("content")
	if isinstance(c, str):
		return c or None
	if isinstance(c, list):
		parts = [
			b.get("text", "")
			for b in c
			if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
		]
		joined = "\n".join(p for p in parts if p.strip())
		return joined or None
	return None


# openclaw marks an assistant turn that FAILED before producing any content
# with ``stopReason == "error"`` and (in the raw transcript) this sentinel text
# block. A terminal model failure - notably a precheck "Context overflow:
# prompt too large for the model", which then deletes the session so there is
# no auto-compact retry - is broadcast as a ``chat`` event with
# ``state == "final"`` (NOT ``state == "error"``) whose projected display
# message keeps ``stopReason == "error"`` but has empty (sentinel-stripped)
# content. Without the guard below, ``relay_turn_events`` would map that to a
# plain ``relay:final`` and the turn handler would write a silent, empty,
# error-less assistant bubble (observed live 2026-07-21). Detecting it here, at
# the openclaw->bench event boundary, lets the EXISTING relay:error path stamp
# the row's ``error`` field so the user sees an honest failure.
_STREAM_ERROR_SENTINEL = "[assistant turn failed before producing content]"

# User-facing text stamped on the assistant row's ``error`` field (and shown in
# the chat) for such a failed final. Generic on purpose: a ``state == "final"``
# event carries no ``errorMessage`` (only ``stopReason``), so the exact cause
# cannot be named here; a context window too small for the conversation is the
# common one. Must NOT contain "context overflow" (that substring reroutes the
# turn handler into the auto-compact park-for-recovery branch, which never
# lands for a terminal precheck failure) or "aborted".
FAILED_FINAL_ERROR = (
	"The assistant could not complete this response and the turn ended "
	"without any output. This can happen when the conversation is too long "
	"for the current model, or the model hit an error. Please try again, or "
	"start a new chat."
)


def _chat_final_failed(payload: dict, text: str | None) -> bool:
	"""True when a ``state == "final"`` chat event actually represents a FAILED
	turn that produced NO real answer: its only content is the stream-error
	sentinel, or (with no visible text) the assistant message carries
	``stopReason == "error"``. Such a "final" must surface as an error, not a
	silent empty bubble.

	Guards two non-failures:
	  * a real (possibly partial) answer is kept even when the turn also flagged
	    an error, so a streamed reply is never hidden behind an error;
	  * a genuinely successful turn that emitted only rich outputs (canvas /
	    image, no prose) has no text and ``stopReason`` != "error", so it stays a
	    normal empty-text relay:final."""
	if isinstance(text, str) and text.strip() == _STREAM_ERROR_SENTINEL:
		return True
	if text:
		return False
	msg = payload.get("message")
	return isinstance(msg, dict) and msg.get("stopReason") == "error"


def _persisted_device_id() -> str:
	"""Cheap unauthenticated read of Jarvis Settings.chat_device_id.

	Used by the repair convoy-collapse path to detect "someone else
	already re-paired" without re-running the whole ensure_paired flow.
	Returns "" if Settings is unreadable for any reason (the caller
	treats empty as "no winner yet, proceed").
	"""
	import frappe

	try:
		return (frappe.db.get_single_value("Jarvis Settings", "chat_device_id") or "").strip()
	except Exception:
		return ""


def _persisted_device_token() -> str:
	"""Read of Jarvis Settings.chat_device_token (Password field).

	Used by the repair path to detect "a peer already adopted a gateway-
	reissued token for this same device" - the device_id alone can't,
	because a token rotation keeps the device_id. Returns "" when
	unreadable (the caller treats empty as "nothing newer, proceed")."""
	import frappe

	try:
		s = frappe.get_single("Jarvis Settings")
		return (s.get_password("chat_device_token", raise_exception=False) or "").strip()
	except Exception:
		return ""


class AgentSession:
	"""Direct WebSocket session to one tenant's openclaw gateway.

	Public surface:
	  AgentSession.connect(gateway_url) -> session
	  session.create_session(label=...) -> session_key
	  session.stream_agent_turn(session_key, message, idem) -> iter of parsed events
	  session.close()

	Authentication is via device pairing - the chat device's deviceToken
	from `jarvis.chat.device.ensure_paired()` is the credential. The
	legacy shared bearer-token path is gone.
	"""

	def __init__(self, ws: websocket.WebSocket, creds: ChatDeviceCredentials):
		self._ws = ws
		self._creds = creds
		self._lock = threading.Lock()  # serialize concurrent sends on same WS

	# -- lifecycle --------------------------------------------------------

	@classmethod
	def connect(cls, gateway_url: str) -> AgentSession:
		if not gateway_url:
			raise AgentUnreachableError("agent_url not set on Jarvis Settings")

		# Two-shot self-heal for tenant re-provisioning: on the first attempt
		# we use whatever paired creds the customer has; if openclaw rejects
		# with a "your pairing is stale" marker (typical when admin replaced
		# the container under us - new container has empty pairing state),
		# we wipe + re-pair once and try again. A second stale signal is a
		# real failure, not a retry candidate.
		return cls._attempt_connect(gateway_url, allow_repair=True)

	@classmethod
	def _attempt_connect(cls, gateway_url: str, *, allow_repair: bool) -> AgentSession:
		# Timing breakdown logged so the connection pool's win is
		# measurable in production. Three phases:
		#   - ensure_paired (cache hit on a paired bench, real I/O on
		#     first run or after repair)
		#   - WS open (DNS + TCP + TLS + WS upgrade)
		#   - _handshake (3-phase signed connect over an open WS)
		# The pool reuses connections, so we only pay this on pool
		# miss / stale eviction / first turn in a worker process.
		t_pair_start = time.monotonic()
		creds = ensure_paired()
		t_pair_done = time.monotonic()
		ws = cls._open_ws_with_retry(gateway_url)
		t_ws_done = time.monotonic()

		try:
			reissued_token = cls._handshake(ws, creds)
		except AgentUnreachableError as e:
			try:
				ws.close()
			except Exception:
				pass
			if allow_repair and _is_stale_pairing(e):
				cls._repair_and_reconnect(
					gateway_url,
					stale_device_id=creds.device_id,
					stale_device_token=creds.device_token,
				)
				return cls._attempt_connect(gateway_url, allow_repair=False)
			raise
		except Exception:
			try:
				ws.close()
			except Exception:
				pass
			raise
		if reissued_token:
			creds = cls._adopt_reissued_token(creds, reissued_token)
		t_handshake_done = time.monotonic()
		_logger.info(
			"AgentSession.connect: pair_ms=%d ws_open_ms=%d handshake_ms=%d total_ms=%d gateway=%s",
			int((t_pair_done - t_pair_start) * 1000),
			int((t_ws_done - t_pair_done) * 1000),
			int((t_handshake_done - t_ws_done) * 1000),
			int((t_handshake_done - t_pair_start) * 1000),
			gateway_url,
		)
		return cls(ws, creds)

	@classmethod
	def _open_ws_with_retry(cls, gateway_url: str):
		"""Open the gateway WebSocket, retrying network-level failures.

		Only the WS OPEN phase (DNS + TCP + TLS + upgrade) is retried, and only
		for network-level failures (WebSocketException / OSError, e.g. a connect
		timeout or refused connection) - so a dormant / recreating container
		(unreachable ~10-30s) does not fail the user's first turn. Handshake and
		auth failures happen later and are NOT retried here. Retries stop once
		CONNECT_OPEN_DEADLINE_SECONDS has elapsed; a warm gateway returns on the
		first attempt.
		"""
		deadline = time.monotonic() + CONNECT_OPEN_DEADLINE_SECONDS
		attempt = 0
		while True:
			attempt += 1
			try:
				return websocket.create_connection(
					gateway_url,
					timeout=CONNECT_TIMEOUT_SECONDS,
					# Origin must be in openclaw's controlUi.allowedOrigins, which
					# the LAN-bound gateway enforces + seeds (see _GATEWAY_ORIGIN).
					origin=_GATEWAY_ORIGIN,
				)
			except (websocket.WebSocketException, OSError) as e:
				if time.monotonic() >= deadline:
					raise AgentUnreachableError(
						f"WS open failed after {attempt} attempt(s) in "
						f"{CONNECT_OPEN_DEADLINE_SECONDS}s - the assistant may be "
						f"starting up; please try again in a moment ({e})"
					) from e
				time.sleep(CONNECT_OPEN_RETRY_BACKOFF_SECONDS)

	@classmethod
	def _adopt_reissued_token(
		cls,
		creds: ChatDeviceCredentials,
		reissued_token: str,
	) -> ChatDeviceCredentials:
		"""Adopt a device token the gateway rotated at connect (hello-ok
		``auth.deviceToken``). The rotation is already persisted gateway-
		side, so we persist our half too; failing that, we still use the
		fresh token in-memory for this session and let the stale-pairing
		self-heal recover the NEXT connect (a persistence hiccup must not
		fail the user's current turn)."""
		import dataclasses

		try:
			persisted = update_device_token(
				reissued_token,
				device_id=creds.device_id,
			)
			if not persisted:
				# Another worker re-paired while we were connecting;
				# Settings holds a newer device's creds. Ours still work
				# for THIS session - don't clobber theirs.
				_logger.warning(
					"gateway reissued device token for %s but Settings "
					"moved to a different pairing; kept in-memory only",
					creds.device_id,
				)
		except Exception:
			_logger.warning(
				"failed to persist reissued device token for %s; kept "
				"in-memory only (self-heal will re-pair on next connect)",
				creds.device_id,
				exc_info=True,
			)
		return dataclasses.replace(creds, device_token=reissued_token)

	@classmethod
	def _repair_and_reconnect(
		cls,
		gateway_url: str,
		*,
		stale_device_id: str,
		stale_device_token: str = "",
	) -> None:
		"""Serialize the clear+re-pair window after a stale-pairing rejection.

		Sprint-2 (2026-06-16 review): with N concurrent chat workers
		racing after a tenant re-provision, every one observes the same
		"stale pairing" error, every one called ``clear_credentials() +
		ensure_paired()``, every one generated a different Ed25519
		keypair, and only the last writer's keypair survived in Jarvis
		Settings - the other N-1 workers held in-memory creds the admin
		side didn't know about, then EACH of them re-paired again,
		flapping admin/openclaw state. The Redis lock collapses the
		convoy: one worker re-pairs, the rest wait, then read the fresh
		keypair from Settings.

		``stale_device_id`` is the device_id we just observed as stale.
		If by the time we acquire the lock the persisted device_id no
		longer matches, the winning worker already re-paired - we skip
		the wipe entirely and the caller's next ``ensure_paired`` reads
		the new creds.
		"""
		from jarvis._redis_lock import redis_lock

		with redis_lock(
			"chat_device_pair_repair",
			timeout_s=120,
			blocking_timeout_s=60.0,
		) as acquired:
			# Even if we lost the lock race, we still want to retry the
			# connect once with fresh creds - the holder may have already
			# repaired. Let the caller re-read on its next attempt.
			if not acquired:
				return
			current = _persisted_device_id()
			if current and current != stale_device_id:
				# Another worker already re-paired while we waited. Don't
				# wipe their work; let the caller re-read.
				return
			# A gateway token ROTATION keeps the device_id, so the id check
			# alone can't see that a peer just adopted the reissued token
			# (hello-ok auth.deviceToken) while our rejected connect was in
			# flight with the pre-rotation token. If the persisted token
			# moved on from the one we presented, the pairing is already
			# healed - retrying with the fresh creds beats destroying them
			# and paying a full re-pair round-trip.
			persisted_token = _persisted_device_token()
			if stale_device_token and persisted_token and persisted_token != stale_device_token:
				return
			clear_credentials()
			# Don't prime ensure_paired() here: the caller's next
			# _attempt_connect(allow_repair=False) calls ensure_paired
			# on its own which is what generates+persists the new
			# keypair under our lock. Late convoy followers also call
			# ensure_paired on their own next attempt and see the
			# newly-persisted creds without re-pairing.

	def close(self) -> None:
		try:
			self._ws.close()
		except Exception:
			pass

	# -- protocol methods -------------------------------------------------

	def create_session(self, label: str = "jarvis-chat") -> str:
		res = self._request("sessions.create", {"label": label}, timeout_s=CONNECT_TIMEOUT_SECONDS)
		key = (res.get("payload") or {}).get("key")
		if not key:
			raise AgentUnreachableError(f"sessions.create returned no key: {res}")
		return key

	# -- openclaw-native turn model (chat.send + chat.history + sessions.*) --
	# Mirrors openclaw's own UI gateway client so the bench can drive a turn
	# and reconcile from the durable transcript instead of holding the agent
	# RPC's request stream. Each method below is a plain request/response.

	def chat_send(
		self,
		session_key: str,
		message: str,
		idempotency_key: str,
		*,
		thinking: str | None = None,
		deliver: bool = False,
		attachments: list[dict] | None = None,
		timeout_s: float = CONNECT_TIMEOUT_SECONDS,
	) -> dict:
		"""Start an agent turn via chat.send (openclaw's UI turn-start RPC).

		Returns the ack payload, e.g. {"runId": ..., "status": "in_flight"}.
		The turn's output is delivered as session-scoped "chat" events (consume
		them via the relay after subscribe_session), NOT on this response.
		deliver=False keeps the result session-only (no external channel
		delivery), matching the current chat path."""
		params: dict = {
			"sessionKey": session_key,
			"message": message,
			"idempotencyKey": idempotency_key,
			"deliver": deliver,
		}
		if thinking:
			params["thinking"] = thinking
		if attachments:
			params["attachments"] = attachments
		res = self._request("chat.send", params, timeout_s=timeout_s)
		return res.get("payload") or {}

	def chat_abort(
		self,
		session_key: str,
		run_id: str | None = None,
		*,
		timeout_s: float = CONNECT_TIMEOUT_SECONDS,
	) -> dict:
		"""Abort in-flight run(s) on a session (openclaw chat.abort). With no
		run_id, aborts ALL active runs on the session (safe here: one active run
		per conversation). The gateway authorizes this from any connection
		presenting the shared device id + operator scope, so the web process can
		abort a run the RQ worker started - the two coordinate via the gateway's
		broadcast (the worker's blocked relay receives the aborted event and
		terminates)."""
		params: dict = {"sessionKey": session_key}
		if run_id:
			params["runId"] = run_id
		return self._request("chat.abort", params, timeout_s=timeout_s)

	def subscribe_session(
		self,
		session_key: str,
		*,
		timeout_s: float = CONNECT_TIMEOUT_SECONDS,
	) -> dict:
		"""Subscribe THIS connection to a session's message events via
		sessions.messages.subscribe. After this the WS receives the session's
		message event frames going forward. openclaw keeps no per-run delta
		buffer, so this yields only FUTURE events - catch up via get_history /
		get_session_messages on (re)connect."""
		res = self._request(
			"sessions.messages.subscribe",
			{"key": session_key},
			timeout_s=timeout_s,
		)
		return res.get("payload") or {}

	def get_history(
		self,
		session_key: str,
		*,
		limit: int = 100,
		max_chars: int = 4000,
		timeout_s: float = CONNECT_TIMEOUT_SECONDS,
	) -> dict:
		"""Snapshot the session's DISPLAY transcript via chat.history. Returns
		{"sessionId", "messages", "thinkingLevel"} - the same projected display
		messages openclaw's UI renders. The authoritative source of truth used
		to reconcile after a timeout / reconnect."""
		res = self._request(
			"chat.history",
			{"sessionKey": session_key, "limit": limit, "maxChars": max_chars},
			timeout_s=timeout_s,
		)
		return res.get("payload") or {}

	def get_session_messages(
		self,
		session_key: str,
		*,
		limit: int = 50,
		timeout_s: float = CONNECT_TIMEOUT_SECONDS,
	) -> list:
		"""Raw recent transcript tail via sessions.get. Returns the messages
		list (each: role, content, __openclaw:{seq,id}); [] for an unknown or
		empty session."""
		res = self._request(
			"sessions.get",
			{"key": session_key, "limit": limit},
			timeout_s=timeout_s,
		)
		return (res.get("payload") or {}).get("messages") or []

	def is_run_active(
		self,
		session_key: str,
		*,
		timeout_s: float = CONNECT_TIMEOUT_SECONDS,
	) -> bool:
		"""Non-destructive done-signal: True iff the gateway is tracking an
		in-flight run for session_key. Reads sessions.list and matches
		hasActiveRun on the row whose key == session_key. (Do NOT use
		sessions.abort for this - it kills the run.)"""
		res = self._request("sessions.list", {}, timeout_s=timeout_s)
		sessions = (res.get("payload") or {}).get("sessions") or []
		for s in sessions:
			if s.get("key") == session_key:
				return bool(s.get("hasActiveRun"))
		return False

	def list_sessions(
		self,
		*,
		timeout_s: float = CONNECT_TIMEOUT_SECONDS,
	) -> list[dict]:
		"""Every session the gateway is tracking (sessions.list rows:
		key, hasActiveRun, updatedAt, label, ...). Used by the session
		lifecycle sweep to find orphaned throwaway sessions."""
		res = self._request("sessions.list", {}, timeout_s=timeout_s)
		return (res.get("payload") or {}).get("sessions") or []

	def list_sessions_page(
		self,
		*,
		limit: int = 100,
		offset: int = 0,
		timeout_s: float = CONNECT_TIMEOUT_SECONDS,
	) -> dict:
		"""One page of ``sessions.list`` with the WHOLE payload, not just the
		rows: ``sessions`` plus ``defaults`` (the agent's configured model, i.e.
		what an unpinned session resolves to) and ``hasMore`` / ``nextOffset``.

		``list_sessions`` above drops the envelope and accepts the gateway's own
		100-row cap. The model-pin sweep needs both halves: ``defaults`` is the
		only way to tell a pinned session from an unpinned one over the wire
		(the row carries the RESOLVED model, never the raw override), and a
		tenant with a long history needs the paging cursor."""
		res = self._request(
			"sessions.list",
			{"limit": limit, "offset": offset},
			timeout_s=timeout_s,
		)
		return res.get("payload") or {}

	def clear_session_model(
		self,
		session_key: str,
		*,
		timeout_s: float = CONNECT_TIMEOUT_SECONDS,
	) -> dict:
		"""Drop the session's model override so the agent's configured default -
		and, crucially, its ``model.fallbacks`` chain - applies again.

		``sessions.patch`` with a NULL model takes openclaw's own isDefault
		branch (2026.6.8 ``src/sessions/model-overrides.ts``), which deletes
		``modelOverride``, ``providerOverride`` and ``modelOverrideSource``
		together with the now-stale runtime model/contextTokens fields, then
		persists through the gateway's own store writer. Rewriting
		``sessions.json`` on disk instead would race that writer: the gateway
		holds the store in memory and rewrites it on every session update.

		Returns the gateway's post-patch store entry (``payload.entry``) so the
		caller can VERIFY the override is gone instead of trusting the ack.

		NEVER call this for a key the gateway did not just report. Patching an
		ABSENT key does not fail - openclaw creates the entry, mints a fresh
		sessionId and drops the label."""
		res = self._request(
			"sessions.patch",
			{"key": session_key, "model": None},
			timeout_s=timeout_s,
		)
		return (res.get("payload") or {}).get("entry") or {}

	def delete_session(
		self,
		session_key: str,
		*,
		timeout_s: float = CONNECT_TIMEOUT_SECONDS,
	) -> None:
		"""Remove a session's gateway state via ``sessions.delete``
		(transcript is archived gateway-side first; ``deleteTranscript``
		defaults true there). The gateway refuses to delete the agent's
		main session - callers treat that error as a skip, never retry."""
		self._request(
			"sessions.delete",
			{"key": session_key},
			timeout_s=timeout_s,
		)

	def set_session_model(
		self,
		session_key: str,
		model_ref: str | None,
		*,
		timeout_s: float = CONNECT_TIMEOUT_SECONDS,
	) -> None:
		"""Point the session at ``model_ref`` (``"<provider>/<model>"`` or
		bare ``"<model>"``) via ``sessions.patch``. chat.send has no per-turn
		model param, so per-conversation overrides are applied to the session
		before the send.

		``model_ref=None`` sends ``{"model": null}``, which openclaw's ``isDefault``
		branch answers by DELETING the session's modelOverride, providerOverride and
		modelOverrideSource. Use it to RESET a session to the agent's configured
		default: an unoverridden session is the only shape that keeps
		``agents.defaults.model.fallbacks`` live, because
		``resolveEffectiveModelFallbacks`` returns ``[]`` for any override not marked
		``modelOverrideSource: "auto"`` -- and that marker is not in the sessions.patch
		schema (the gateway rejects it with INVALID_REQUEST), so not overriding is the
		supported way to preserve failover. See ``turn_handler._session_model_for``."""
		self._request(
			"sessions.patch",
			{"key": session_key, "model": model_ref},
			timeout_s=timeout_s,
		)

	def relay_turn_events(
		self,
		session_key: str,
		run_id: str,
		*,
		soft_deadline_s: float = TURN_TIMEOUT_SECONDS,
	) -> Iterator[dict[str, Any]]:
		"""Consume broadcast events for a chat.send run until a terminal
		``chat`` event, the soft deadline, or a transport drop.

		Mirror of openclaw's own UI model: token/tool streaming comes from
		the broadcast ``agent`` frames (retagged with the chat.send
		clientRunId == run_id); completion comes ONLY from the run-scoped
		``chat`` event (state final|aborted|error). agent ``lifecycle``
		frames are dropped so there is a single terminal path.

		NEVER raises after entry. Terminal yields:
		  {"kind": "relay:final", "text": str|None}
		  {"kind": "relay:error", "state": "error"|"aborted"|"failed_final", "error": str}
		  {"kind": "relay:interrupted", "reason": "transport"|"deadline", ...}

		A ``state == "final"`` event whose assistant turn actually failed
		(stopReason error / stream-error sentinel, e.g. a precheck context
		overflow that deletes the session) is remapped to a ``failed_final``
		relay:error so it stamps an honest error instead of a silent empty
		final. See _chat_final_failed.
		"""
		deadline = time.monotonic() + soft_deadline_s
		while True:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				yield {"kind": "relay:interrupted", "reason": "deadline"}
				return
			try:
				frame = self._recv(remaining)
			except AgentUnreachableError as e:
				# Close the dead WS before yielding: this generator swallows
				# the exception by design, so the pool's discard-on-exception
				# contract never fires. Closing flips the connected flag the
				# pool healthcheck reads, so the corpse is not handed to the
				# next turn (which would fail pre-ack as a false run:error).
				try:
					self.close()
				except Exception:
					pass
				yield {"kind": "relay:interrupted", "reason": "transport", "detail": str(e)}
				return
			if frame is None or frame.get("type") != "event":
				continue
			payload = frame.get("payload") or {}
			if frame.get("event") == "chat":
				if payload.get("runId") != run_id or payload.get("sessionKey") != session_key:
					continue
				state = payload.get("state")
				if state == "final":
					text = _chat_final_text(payload)
					# A "final" whose assistant turn actually FAILED (stopReason
					# error / stream-error sentinel) is surfaced as a terminal
					# error, not a silent empty bubble. See _chat_final_failed.
					if _chat_final_failed(payload, text):
						yield {
							"kind": "relay:error",
							"state": "failed_final",
							"error": FAILED_FINAL_ERROR,
						}
						return
					yield {"kind": "relay:final", "text": text}
					return
				if state in ("error", "aborted"):
					yield {
						"kind": "relay:error",
						"state": state,
						"error": payload.get("errorMessage") or state,
					}
					return
				continue  # delta (150ms cumulative mirror) and unknown states: ignore
			if payload.get("runId") != run_id:
				continue
			parsed = parse_event(payload)
			if parsed is None or parsed.get("kind") == "lifecycle":
				continue
			yield parsed

	def fire_agent(
		self,
		session_key: str,
		message: str,
		idempotency_key: str,
		*,
		model: str | None = None,
		provider: str | None = None,
	) -> str:
		"""Send one agent turn and return its runId after the ack, WITHOUT
		consuming the event stream. openclaw keeps running the turn server
		side after we close (the run lane survives client disconnect), so this
		is enough to warm the provider prompt cache. ``deliver`` is False so
		the result stays session-only. ``_request`` drops the interleaved
		event frames and returns the agent ack response.

		Setting ``model`` / ``provider`` disables openclaw's failover for this
		run - see PINNED_ONESHOT_RUN_PREFIX."""
		params = {
			"message": message,
			"sessionKey": session_key,
			"deliver": False,
			"idempotencyKey": idempotency_key,
		}
		if model:
			params["model"] = model
		if provider:
			params["provider"] = provider
		if model or provider:
			_log_pinned_oneshot(idempotency_key, model, provider)
		res = self._request("agent", params, timeout_s=CONNECT_TIMEOUT_SECONDS)
		return (res.get("payload") or {}).get("runId") or ""

	def stream_agent_turn(
		self,
		session_key: str,
		message: str,
		idempotency_key: str,
		*,
		model: str | None = None,
		provider: str | None = None,
		attachments: list[dict] | None = None,
	) -> Iterator[dict[str, Any]]:
		"""Send an `agent` request, then yield parsed events until lifecycle.end.

		`model` / `provider` are optional per-turn overrides. When set, they
		flow into openclaw's agent RPC params; the gateway honours them via
		the operator.admin-scope-gated modelOverride path (our connect already
		declares that scope), and the run loses its failover chain for the
		duration - see PINNED_ONESHOT_RUN_PREFIX. When omitted, openclaw falls
		back to agents.defaults.model.primary from openclaw.json AND keeps its
		fallbacks.

		Yields the same parsed-event shape the worker used to consume from
		the subprocess. Raises AgentUnreachableError on WS drop, agent
		errors, or timeout - all the failure modes worker.py already maps to
		assistant-message error rows."""
		params = {
			"message": message,
			"sessionKey": session_key,
			"deliver": False,
			"idempotencyKey": idempotency_key,
		}
		if model:
			params["model"] = model
		if provider:
			params["provider"] = provider
		if attachments:
			# Native vision input. openclaw's `agent` handler reads these and
			# normalizes them to the active provider's image blocks; the flat
			# {type:"image", mimeType, fileName, content:<base64>} shape is what
			# its gateway normalizer accepts.
			params["attachments"] = attachments
		if model or provider:
			_log_pinned_oneshot(idempotency_key, model, provider)
		agent_id = self._send("agent", params)
		deadline = time.monotonic() + TURN_TIMEOUT_SECONDS

		# 1. Drain frames until we see the agent ack OR an error/event for our run.
		active_run_id: str | None = None
		got_ack = False
		while time.monotonic() < deadline:
			frame = self._recv(deadline - time.monotonic())
			if frame is None:
				continue
			ftype = frame.get("type")
			if ftype == "res" and frame.get("id") == agent_id:
				if not frame.get("ok"):
					err = frame.get("error") or {}
					details = err.get("details")
					raise AgentUnreachableError(
						f"agent rejected: {err.get('code', '?')}: {err.get('message', '')}",
						code=err.get("code"),
						details=details if isinstance(details, dict) else None,
					)
				active_run_id = (frame.get("payload") or {}).get("runId")
				got_ack = True
				break
			# Pre-ack events can arrive; pass them through if they belong to us
			# by lifecycle (no runId yet at this point, drop unrelated noise).
		if not got_ack:
			raise AgentUnreachableError("agent RPC never acknowledged")

		# 2. Stream events for this run until lifecycle.end / .error.
		# The run has started (we have the ack); a WS drop from here is
		# RECOVERABLE - openclaw keeps running and persists the result - so tag
		# it code="turn-timeout" to park for recovery, never a false error (#4).
		while time.monotonic() < deadline:
			try:
				frame = self._recv(deadline - time.monotonic())
			except AgentUnreachableError as e:
				if getattr(e, "code", None):
					raise
				raise AgentUnreachableError(str(e), code="turn-timeout") from e
			if frame is None:
				continue
			ftype = frame.get("type")
			if ftype != "event":
				continue
			payload = frame.get("payload") or {}
			if active_run_id is not None and payload.get("runId") != active_run_id:
				continue
			parsed = parse_event(payload)
			if parsed is not None:
				yield parsed
			# Same lifecycle-phase detection the Node script used.
			if payload.get("stream") == "lifecycle":
				phase = (payload.get("data") or {}).get("phase")
				if phase in ("end", "error"):
					return
		raise AgentUnreachableError(
			"agent turn timed out before lifecycle end",
			code="turn-timeout",
		)

	# -- internals --------------------------------------------------------

	@classmethod
	def _handshake(cls, ws: websocket.WebSocket, creds: ChatDeviceCredentials) -> str | None:
		"""Receive connect.challenge → sign v3 payload → send connect → expect hello-ok.

		Returns the REISSUED device token from hello-ok's ``auth.deviceToken``
		when the gateway rotated it (None otherwise). openclaw's gateway
		replaces the stored device token at connect whenever the existing
		entry no longer lines up with the requested scopes/issuer; the new
		token is already durable on the gateway side when hello-ok arrives,
		so the caller MUST adopt it or every following connect fails with
		"device token mismatch"."""
		deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS

		# 1. Wait for the challenge event.
		nonce: str | None = None
		while time.monotonic() < deadline:
			frame = _recv_with_timeout(ws, deadline - time.monotonic())
			if frame is None:
				continue
			if frame.get("type") == "event" and frame.get("event") == "connect.challenge":
				nonce = (frame.get("payload") or {}).get("nonce")
				break
		if not nonce:
			raise AgentUnreachableError("did not receive connect.challenge before timeout")

		# 2. Sign + send the connect frame.
		signed_at_ms = int(time.time() * 1000)
		payload = build_payload_v3(
			device_id=creds.device_id,
			client_id=_CLIENT_ID,
			client_mode=_CLIENT_MODE,
			role=_ROLE,
			scopes=_REQUESTED_SCOPES,
			signed_at_ms=signed_at_ms,
			device_token=creds.device_token,
			nonce=nonce,
			platform=_PLATFORM,
			device_family="",
		)
		signature_b64u = sign_payload(creds.private_key, payload)
		connect_id = uuid.uuid4().hex
		connect_params = {
			"minProtocol": 4,
			"maxProtocol": 4,
			"client": {
				"id": _CLIENT_ID,
				"version": "0.1.0",
				"platform": _PLATFORM,
				"mode": _CLIENT_MODE,
			},
			"role": _ROLE,
			"scopes": _REQUESTED_SCOPES,
			"auth": {"deviceToken": creds.device_token},
			"device": {
				"id": creds.device_id,
				"publicKey": creds.public_key,
				"signature": signature_b64u,
				"signedAt": signed_at_ms,
				"nonce": nonce,
			},
		}
		connect_payload, _ = _build_request_frame("connect", connect_params, req_id=connect_id)
		ws.send(connect_payload)

		# 3. Wait for the connect response.
		while time.monotonic() < deadline:
			frame = _recv_with_timeout(ws, deadline - time.monotonic())
			if frame is None:
				continue
			if _is_response_frame(frame, connect_id):
				if not frame.get("ok"):
					err = frame.get("error") or {}
					details = err.get("details")
					raise AgentUnreachableError(
						f"connect rejected: {err.get('code', '?')}: {err.get('message', '')}",
						code=err.get("code"),
						details=details if isinstance(details, dict) else None,
					)
				# Shape-defensive: a successful connect must NEVER fail on
				# an unexpected hello-ok payload (pre-change behavior was to
				# ignore the payload entirely).
				payload = frame.get("payload")
				auth = payload.get("auth") if isinstance(payload, dict) else None
				reissued = auth.get("deviceToken") if isinstance(auth, dict) else None
				if isinstance(reissued, str) and reissued and reissued != creds.device_token:
					return reissued
				return None
		raise AgentUnreachableError("no connect response before timeout")

	def _send(self, method: str, params: dict) -> str:
		"""Send a request frame; return the generated request id."""
		payload, req_id = _build_request_frame(method, params)
		with self._lock:
			self._ws.send(payload)
		return req_id

	def _request(self, method: str, params: dict, *, timeout_s: float) -> dict:
		"""Send a request and wait for the matching response frame.

		Drops out-of-order event frames silently - the caller is RPC-style
		and doesn't need them. stream_agent_turn handles its own framing."""
		req_id = self._send(method, params)
		deadline = time.monotonic() + timeout_s
		while time.monotonic() < deadline:
			frame = self._recv(deadline - time.monotonic())
			if frame is None:
				continue
			if _is_response_frame(frame, req_id):
				if not frame.get("ok"):
					err = frame.get("error") or {}
					details = err.get("details")
					raise AgentUnreachableError(
						f"{method} rejected: {err.get('code', '?')}: {err.get('message', '')}",
						code=err.get("code"),
						details=details if isinstance(details, dict) else None,
					)
				return frame
		# Tagged so callers can tell an AMBIGUOUS outcome from a definite one:
		# the request frame was already written, so the peer may well have
		# accepted and acted on it - we just never saw the response. chat.send
		# uses this to park for snapshot recovery instead of reporting a false
		# error (see turn_handler). A rejection or a dead socket is definite
		# and raises without this code.
		raise AgentUnreachableError(f"{method} timed out", code="ack-timeout")

	def _recv(self, timeout_s: float) -> dict | None:
		"""Read one frame from the WS, parse JSON, or return None on a soft
		timeout / non-JSON noise. Raises AgentUnreachableError on hard
		close so the caller can wrap into an assistant-message error row."""
		return _recv_with_timeout(self._ws, timeout_s)


def _recv_with_timeout(ws: websocket.WebSocket, timeout_s: float) -> dict | None:
	if timeout_s <= 0:
		return None
	ws.settimeout(timeout_s)
	try:
		raw = ws.recv()
	except websocket.WebSocketTimeoutException:
		return None
	except websocket.WebSocketConnectionClosedException as e:
		raise AgentUnreachableError(f"openclaw WS closed: {e}") from e
	except websocket.WebSocketException as e:
		raise AgentUnreachableError(f"openclaw WS error: {e}") from e
	if not raw:
		return None
	try:
		return json.loads(raw)
	except json.JSONDecodeError:
		return None
