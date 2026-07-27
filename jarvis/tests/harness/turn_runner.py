"""turn_runner — the faithful legacy managed-relay reproduction + worker pool.

A harness "turn" runs the REAL bench transport and streaming path:

  checkout/connect (real OpenclawSession)
    -> create_session / watermark read (faithful pre-send RPCs)
    -> chat_send                       (real ack)
    -> relay_turn_events               (real relay)
       feeding real turn_handler._handle_event + a recording subclass of the
       real _AssistantContentBatcher (real DB commits + real publish fan-out)
    -> terminal finalize               (streaming=0 / _mark_errored)

The only things NOT exercised are the ~1000-line pre-stream preamble of
handle_chat_send (model resolution, vision, context injection, admission) —
one-time per-conversation setup, not the streaming latency the C-criteria
measure. Admission (flag ON) is driven separately by the orchestrator through
the REAL accept_or_queue chokepoint.

A "worker" is a thread holding one turn end-to-end (legacy worker-per-turn).
``WorkerPool(size=2)`` is therefore the 2-background-worker starvation bed.
"""

from __future__ import annotations

import base64
import hashlib
import queue
import threading
import time
from dataclasses import dataclass, field

from jarvis.tests.harness import CONV, HARNESS_USER, MSG

# ---- process-wide stubs (thread-safe: plain module-attribute swaps) --------


def _b64u(raw: bytes) -> str:
	return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_creds():
	from cryptography.hazmat.primitives import serialization
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

	from jarvis.chat.device import ChatDeviceCredentials

	priv = Ed25519PrivateKey.generate()
	pub_raw = priv.public_key().public_bytes(
		encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
	)
	return ChatDeviceCredentials(
		device_id=hashlib.sha256(pub_raw).hexdigest(),
		public_key=_b64u(pub_raw),
		private_key=priv,
		device_token="tok-harness",
	)


# The publish stub records into whichever recorder is active for the current
# scenario. A module global lets the process-wide stub target per-scenario
# recorders without re-patching each time (thread-safe: single reference swap).
_ACTIVE_RECORDER = None


def set_active_recorder(recorder) -> None:
	global _ACTIVE_RECORDER
	_ACTIVE_RECORDER = recorder


def install_stubs():
	"""Swap ensure_paired (avoid real device pairing) + publish_to_user (record
	the realtime fan-out instead of hitting socketio). Returns a restore fn.
	Point the recorder with set_active_recorder() per scenario."""
	import jarvis.chat.openclaw_client as oc
	import jarvis.chat.worker as worker

	orig_ensure = oc.ensure_paired
	orig_publish = worker.publish_to_user

	def _stub_ensure():
		return make_creds()

	def _rec_publish(user, payload):
		rec = _ACTIVE_RECORDER
		if rec is None:
			return
		kind = payload.get("kind", "?")
		run_id = payload.get("run_id") or "?"
		detail = {"user": user}
		if "tool_name" in payload:
			detail["tool_name"] = payload.get("tool_name")
		if payload.get("message_id") is not None:
			detail["message_id"] = payload.get("message_id")
		if kind.startswith("assistant"):
			detail["len"] = len(payload.get("text") or "")
		rec.publish(run_id, kind, detail)

	oc.ensure_paired = _stub_ensure
	# turn_handler dereferences publish through the worker module at call time,
	# so rebinding here redirects every outbound publish (documented in worker.py).
	worker.publish_to_user = _rec_publish

	def restore():
		oc.ensure_paired = orig_ensure
		worker.publish_to_user = orig_publish

	return restore


# ---- recording batcher (real DB write, records the flush) ------------------


def _recording_batcher(msg_name, run_id, recorder):
	from jarvis.chat.turn_handler import _AssistantContentBatcher

	class _RecordingBatcher(_AssistantContentBatcher):
		def flush(self) -> bool:
			did = super().flush()
			if did:
				recorder.db(run_id, "msg.content.flush", {"msg": msg_name})
			return did

	return _RecordingBatcher(msg_name)


# ---- turn spec / result ----------------------------------------------------


@dataclass
class TurnSpec:
	run_id: str
	conversation_id: str
	seed_message: str
	transcript: str = "success"
	turn_class: str = "interactive"
	user: str = HARNESS_USER
	overrides: dict = field(default_factory=dict)
	soft_deadline_s: float = 30.0
	ack_timeout_s: float | None = None  # short client ack window (ack-timeout tests)
	settle_on_terminal: bool = False  # flag-ON: call admission.settle_turn
	connect_mode: str = "per_turn"  # per_turn (fork-per-job) | pool (nofork)
	t_submit: float | None = None


@dataclass
class TurnResult:
	run_id: str
	transcript: str
	conversation: str
	t_submit: float | None = None
	t_start: float | None = None
	t_send: float | None = None
	t_first_frame: float | None = None
	t_end: float | None = None
	terminal: str | None = None
	relay_state: str | None = None
	error: str | None = None

	def first_token_from_submit_ms(self):
		if self.t_submit is None or self.t_first_frame is None:
			return None
		return (self.t_first_frame - self.t_submit) * 1000.0

	def first_token_from_send_ms(self):
		if self.t_send is None or self.t_first_frame is None:
			return None
		return (self.t_first_frame - self.t_send) * 1000.0

	def queue_wait_ms(self):
		if self.t_submit is None or self.t_start is None:
			return None
		return (self.t_start - self.t_submit) * 1000.0

	def total_ms(self):
		if self.t_submit is None or self.t_end is None:
			return None
		return (self.t_end - self.t_submit) * 1000.0


# ---- the faithful turn -----------------------------------------------------


def run_turn(spec: TurnSpec, gateway, recorder) -> TurnResult:
	import frappe

	from jarvis.chat import openclaw_session_pool, turn_handler
	from jarvis.chat.openclaw_client import OpenclawSession
	from jarvis.exceptions import OpenclawUnreachableError

	res = TurnResult(run_id=spec.run_id, transcript=spec.transcript, conversation=spec.conversation_id)
	res.t_submit = spec.t_submit
	res.t_start = time.monotonic()
	recorder.job(
		spec.run_id,
		"start",
		{"conversation": spec.conversation_id, "transcript": spec.transcript},
		t_mono=res.t_start,
	)

	frappe.set_user(spec.user)
	conv = frappe.get_doc(CONV, spec.conversation_id)
	assistant = turn_handler._create_assistant_placeholder(conv)
	recorder.db(spec.run_id, "assistant.placeholder.insert", {"msg": assistant.name})

	gateway.arm(spec.run_id, spec.transcript, **(spec.overrides or {}))

	def _do(sess: "OpenclawSession") -> None:
		if not conv.session_key:
			sk = sess.create_session()
			frappe.db.set_value(CONV, conv.name, "session_key", sk)
			frappe.db.commit()
			conv.session_key = sk
		# faithful pre-send watermark read
		try:
			sess.get_session_messages(conv.session_key, limit=5)
		except OpenclawUnreachableError:
			raise

		res.t_send = time.monotonic()
		ack_to = spec.ack_timeout_s if spec.ack_timeout_s is not None else 30.0
		try:
			ack = sess.chat_send(conv.session_key, "harness turn", spec.run_id, timeout_s=ack_to) or {}
		except OpenclawUnreachableError as e:
			if getattr(e, "code", None) != "ack-timeout":
				raise
			res.terminal = "relay:interrupted"
			res.relay_state = "ack-timeout"
			recorder.job(
				spec.run_id, "end", {"relay_kind": "relay:interrupted", "relay_state": "ack-timeout"}
			)
			return

		batcher = _recording_batcher(assistant.name, spec.run_id, recorder)
		tool_map: dict[str, str] = {}
		terminal = {"kind": "relay:interrupted", "reason": "stream-exhausted"}
		for ev in sess.relay_turn_events(
			conv.session_key, ack.get("runId") or spec.run_id, soft_deadline_s=spec.soft_deadline_s
		):
			kind = ev.get("kind")
			if str(kind or "").startswith("relay:"):
				terminal = ev
				break
			if res.t_first_frame is None and kind in ("assistant", "tool"):
				res.t_first_frame = time.monotonic()
			turn_handler._handle_event(
				ev,
				conversation_id=conv.name,
				assistant_msg_name=assistant.name,
				tool_msg_by_call_id=tool_map,
				user=spec.user,
				run_id=spec.run_id,
				batcher=batcher,
			)
		batcher.flush()
		res.terminal = terminal.get("kind")
		res.relay_state = terminal.get("state") or terminal.get("reason")
		_finalize(assistant.name, terminal, recorder, spec.run_id)
		recorder.job(spec.run_id, "end", {"relay_kind": res.terminal, "relay_state": res.relay_state})

	try:
		if spec.connect_mode == "pool":
			with openclaw_session_pool.checkout(gateway.ws_url) as sess:
				_do(sess)
		else:
			sess = OpenclawSession.connect(gateway.ws_url)
			try:
				_do(sess)
			finally:
				sess.close()
	except OpenclawUnreachableError as e:
		res.terminal = "unreachable"
		res.error = str(e)
		recorder.job(spec.run_id, "end", {"relay_kind": "unreachable", "error": str(e)})
	except Exception as e:  # never let one turn kill the pool
		res.terminal = "harness-error"
		res.error = repr(e)
		recorder.job(spec.run_id, "end", {"relay_kind": "harness-error", "error": repr(e)})

	res.t_end = time.monotonic()

	if spec.settle_on_terminal:
		_settle(spec, res)
	return res


def _finalize(msg_name: str, terminal: dict, recorder, run_id: str) -> None:
	"""Mirror the worker's terminal DB write (streaming=0 / _mark_errored)."""
	import frappe

	from jarvis.chat import turn_handler

	kind = terminal.get("kind")
	if kind == "relay:final":
		frappe.db.set_value(MSG, msg_name, "streaming", 0)
		frappe.db.commit()
		recorder.db(run_id, "msg.finalize", {"msg": msg_name})
	elif kind == "relay:error":
		turn_handler._mark_errored(msg_name, terminal.get("error") or terminal.get("state") or "error")
		recorder.db(run_id, "msg.errored", {"msg": msg_name})
	# relay:interrupted -> leave streaming=1 (recovery/sweep territory)


def _settle(spec: TurnSpec, res: TurnResult) -> None:
	from jarvis.chat import admission

	kind, state = res.terminal, (res.relay_state or "")
	if kind == "relay:final":
		term = "done"
	elif kind == "relay:error" and state == "aborted":
		term = "cancelled"
	elif kind in ("relay:error", "unreachable", "harness-error"):
		term = "errored"
	else:  # interrupted / parked
		term = "errored"
	try:
		admission.settle_turn(spec.run_id, term)
	except Exception:
		pass


# ---- worker pool -----------------------------------------------------------


class WorkerPool:
	"""Bounded thread pool; each worker holds one turn end-to-end (legacy
	worker-per-turn). Each worker thread owns its own frappe DB connection and
	an optional per-process admission flag override."""

	def __init__(self, size: int, site: str, gateway, recorder, *, flag_value: int | None = None):
		self.size = size
		self.site = site
		self.gateway = gateway
		self.recorder = recorder
		self.flag_value = flag_value
		self._q: "queue.Queue" = queue.Queue()
		self._threads: list[threading.Thread] = []
		self._results: dict[str, TurnResult] = {}
		self._rlock = threading.Lock()
		self._started = False
		self._submitted = 0
		self._completed = 0
		self._clock = threading.Lock()

	def start(self) -> "WorkerPool":
		for i in range(self.size):
			th = threading.Thread(target=self._loop, name=f"harness-worker-{i}", daemon=True)
			th.start()
			self._threads.append(th)
		self._started = True
		return self

	def _loop(self) -> None:
		import frappe

		frappe.init(site=self.site)
		frappe.connect()
		if self.flag_value is not None:
			from jarvis.chat import admission

			frappe.local.conf[admission.FLAG] = self.flag_value
		try:
			while True:
				item = self._q.get()
				if item is None:
					return
				spec, done = item
				try:
					res = run_turn(spec, self.gateway, self.recorder)
					with self._rlock:
						self._results[spec.run_id] = res
				except Exception as e:  # pragma: no cover - defensive
					with self._rlock:
						self._results[spec.run_id] = TurnResult(
							run_id=spec.run_id,
							transcript=spec.transcript,
							conversation=spec.conversation_id,
							terminal="pool-error",
							error=repr(e),
						)
				finally:
					try:
						frappe.db.commit()
					except Exception:
						pass
					with self._clock:
						self._completed += 1
					if done is not None:
						done.set()
		finally:
			frappe.destroy()

	def submit(self, spec: TurnSpec, done: threading.Event | None = None) -> None:
		spec.t_submit = time.monotonic()
		with self._clock:
			self._submitted += 1
		self.recorder.job(
			spec.run_id,
			"submit",
			{"transcript": spec.transcript, "class": spec.turn_class},
			t_mono=spec.t_submit,
		)
		self._q.put((spec, done))

	def result(self, run_id: str) -> TurnResult | None:
		with self._rlock:
			return self._results.get(run_id)

	def all_results(self) -> dict[str, TurnResult]:
		with self._rlock:
			return dict(self._results)

	def drain(self, timeout: float = 120.0) -> bool:
		"""Wait until every submitted turn has completed. Promotion (flag ON)
		submits follow-on turns from inside run_turn BEFORE the completing turn
		increments _completed, so _completed == _submitted only when quiescent."""
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			with self._clock:
				quiescent = self._completed >= self._submitted and self._q.empty()
			if quiescent:
				time.sleep(0.05)
				with self._clock:
					if self._completed >= self._submitted and self._q.empty():
						return True
			time.sleep(0.02)
		return False

	def stop(self) -> None:
		for _ in self._threads:
			self._q.put(None)
		for th in self._threads:
			th.join(timeout=5)
