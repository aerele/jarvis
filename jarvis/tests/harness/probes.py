"""Metric probes for the six production criteria (C1–C6).

Two kinds:

  * Trace extractors — pure functions over TurnResult lists + the TraceRecorder:
      first_token (from submit AND from send), queue_wait, flush-gap series
      (C3, DB-commit cadence + publish cadence), dispatch->first-event dwell
      (C4), and the p50/p95/p99 summarizer.

  * Live probes — real work against the running bench:
      CanaryProbe  — enqueues short+long RQ canaries every 10s to the REAL
                     workers and times their wait, plus a real Desk HTTP GET
                     (C6). background_flood() generates real RQ occupancy so
                     the canary is measured under load, not just idle.
      measure_stop_visible — stop-click -> visible aborted terminal (SUX-12).

Percentiles use linear interpolation (numpy-free). All wall-clock probes are
repeated; the caller states N in the report.
"""

from __future__ import annotations

import itertools
import threading
import time
import urllib.request
import uuid

# --------------------------------------------------------------------------- #
# percentile + summary
# --------------------------------------------------------------------------- #


def percentile(values, p: float):
	xs = sorted(v for v in values if v is not None)
	if not xs:
		return None
	if len(xs) == 1:
		return xs[0]
	k = (len(xs) - 1) * (p / 100.0)
	lo = int(k)
	hi = min(lo + 1, len(xs) - 1)
	frac = k - lo
	return xs[lo] + (xs[hi] - xs[lo]) * frac


def summarize(values) -> dict:
	xs = [v for v in values if v is not None]
	if not xs:
		return {"n": 0, "min": None, "p50": None, "p95": None, "p99": None, "max": None, "mean": None}
	return {
		"n": len(xs),
		"min": round(min(xs), 2),
		"p50": round(percentile(xs, 50), 2),
		"p95": round(percentile(xs, 95), 2),
		"p99": round(percentile(xs, 99), 2),
		"max": round(max(xs), 2),
		"mean": round(sum(xs) / len(xs), 2),
	}


# --------------------------------------------------------------------------- #
# trace extractors
# --------------------------------------------------------------------------- #


def first_token_from_submit(results) -> list:
	return [r.first_token_from_submit_ms() for r in results]


def first_token_from_send(results) -> list:
	return [r.first_token_from_send_ms() for r in results]


def queue_wait(results) -> list:
	return [r.queue_wait_ms() for r in results]


def total(results) -> list:
	return [r.total_ms() for r in results]


def _gap_series(recorder, run_id, source, kind) -> list:
	evs = [e for e in recorder.events_for(run_id) if e.source == source and (kind is None or e.kind == kind)]
	gaps = []
	for a, b in itertools.pairwise(evs):
		gaps.append((b.t_mono - a.t_mono) * 1000.0)
	return gaps


def flush_gap_series(recorder, run_id) -> list:
	"""C3: inter-DB-commit gap between successive assistant content flushes
	(the coalesced batcher cadence, _ASSISTANT_BATCH_INTERVAL_MS=250)."""
	return _gap_series(recorder, run_id, "db", "msg.content.flush")


def publish_gap_series(recorder, run_id) -> list:
	"""Companion to C3: inter-publish gap (assistant:delta fires per token)."""
	return _gap_series(recorder, run_id, "publish", "assistant:delta")


def all_flush_gaps(recorder, run_ids) -> list:
	out = []
	for rid in run_ids:
		out.extend(flush_gap_series(recorder, rid))
	return out


def all_publish_gaps(recorder, run_ids) -> list:
	out = []
	for rid in run_ids:
		out.extend(publish_gap_series(recorder, rid))
	return out


def dwell_series(gateway, run_ids) -> list:
	"""C4: per-run gateway dwell (ack -> lane admit), the main-lane queueing
	proxy under the simulated maxConcurrent cap."""
	out = []
	for rid in run_ids:
		tl = gateway.timeline(rid)
		if tl:
			d = tl.dwell_ms()
			if d is not None:
				out.append(d)
	return out


def ack_to_first_frame_series(gateway, run_ids) -> list:
	out = []
	for rid in run_ids:
		tl = gateway.timeline(rid)
		if tl:
			d = tl.ack_to_first_frame_ms()
			if d is not None:
				out.append(d)
	return out


# --------------------------------------------------------------------------- #
# C6 live canary — REAL RQ jobs + Desk HTTP
# --------------------------------------------------------------------------- #
#
# IMPORTANT: the canary + flood targets must be CORE-frappe functions, not
# harness code. The running RQ workers were started before this harness module
# existed and RQ workers do not hot-reload, so they cannot import
# jarvis.tests.harness.*. We therefore enqueue a trivial core function and read
# the wait from the RQ job's own enqueued_at/started_at timestamps (populated by
# the worker) rather than having our code run in the worker. This keeps C6 real
# without restarting the pool's workers (which would mutate running state).

_CORE_NOOP = "frappe.utils.data.now_datetime"  # pure, present in every frappe worker


def _enqueue_canary(queue_name: str) -> dict:
	import frappe

	token = uuid.uuid4().hex[:12]
	job = frappe.enqueue(_CORE_NOOP, queue=queue_name, job_id=f"harness-canary-{token}")
	deadline = time.time() + 65
	while time.time() < deadline:
		try:
			job.refresh()
		except Exception:
			break
		st = str(job.get_status())
		if st.endswith("FINISHED") or st.endswith("FAILED") or st in ("finished", "failed"):
			break
		time.sleep(0.05)
	enq, started = getattr(job, "enqueued_at", None), getattr(job, "started_at", None)
	if not enq or not started:
		return {"queue": queue_name, "wait_ms": None, "starved": True}
	wait_ms = (started - enq).total_seconds() * 1000.0
	return {"queue": queue_name, "wait_ms": max(0.0, wait_ms), "starved": wait_ms > 60000}


def desk_http_probe(url: str, host: str) -> dict:
	req = urllib.request.Request(url, headers={"Host": host})
	t0 = time.monotonic()
	try:
		with urllib.request.urlopen(req, timeout=15) as r:
			code = getattr(r, "status", r.getcode())
			r.read(256)
		return {"ok": True, "code": code, "ms": (time.monotonic() - t0) * 1000.0}
	except Exception as e:
		return {"ok": False, "err": str(e), "ms": (time.monotonic() - t0) * 1000.0}


class CanaryProbe:
	"""Every ``cadence_s`` (default 10, per C6): a short + long RQ canary and a
	Desk HTTP GET, timing the wait. Runs in its own thread + frappe connection."""

	def __init__(self, site: str, *, cadence_s: float = 10.0, desk_url: str, desk_host: str):
		self.site = site
		self.cadence_s = cadence_s
		self.desk_url = desk_url
		self.desk_host = desk_host
		self._stop = threading.Event()
		self._thread = None
		self.samples: list[dict] = []
		self._lock = threading.Lock()

	def _loop(self) -> None:
		import frappe

		frappe.init(site=self.site)
		frappe.connect()
		try:
			while not self._stop.is_set():
				t_wall = time.time()
				short = _enqueue_canary("short")
				long_ = _enqueue_canary("long")
				desk = desk_http_probe(self.desk_url, self.desk_host)
				with self._lock:
					self.samples.append({"t": t_wall, "short": short, "long": long_, "desk": desk})
				self._stop.wait(self.cadence_s)
		finally:
			frappe.destroy()

	def start(self) -> "CanaryProbe":
		self._thread = threading.Thread(target=self._loop, name="harness-canary", daemon=True)
		self._thread.start()
		return self

	def stop(self) -> None:
		self._stop.set()
		if self._thread:
			self._thread.join(timeout=70)

	def series(self) -> dict:
		with self._lock:
			s = list(self.samples)
		return {
			"short_wait_ms": [x["short"]["wait_ms"] for x in s],
			"long_wait_ms": [x["long"]["wait_ms"] for x in s],
			"desk_ms": [x["desk"]["ms"] for x in s if x["desk"].get("ok")],
			"desk_failures": sum(1 for x in s if not x["desk"].get("ok")),
			"starved": sum(1 for x in s if x["short"].get("starved") or x["long"].get("starved")),
			"count": len(s),
		}


def background_flood(site: str, n: int, *, queue_name: str = "jarvis_chat", work_ms: int = 0) -> int:
	"""Enqueue n core no-op jobs to build a real FIFO backlog on ``queue_name``
	(C6 load). Uses a core function (see _CORE_NOOP note) so the pre-existing
	workers can run it. ``work_ms`` is accepted for signature stability but the
	core no-op is quick; sustained load comes from re-flooding (FloodPump)."""
	import frappe

	for _ in range(n):
		frappe.enqueue(_CORE_NOOP, queue=queue_name, job_id=f"harness-flood-{uuid.uuid4().hex[:12]}")
	return n


class FloodPump:
	"""Sustains a real RQ backlog for the load window by re-flooding target
	queues every ``interval_s``. Its own thread + frappe connection."""

	def __init__(self, site: str, queues: list[str], *, per_burst: int = 120, interval_s: float = 1.0):
		self.site = site
		self.queues = queues
		self.per_burst = per_burst
		self.interval_s = interval_s
		self._stop = threading.Event()
		self._thread = None
		self.total = 0

	def _loop(self):
		import frappe

		frappe.init(site=self.site)
		frappe.connect()
		try:
			while not self._stop.is_set():
				for q in self.queues:
					self.total += background_flood(self.site, self.per_burst, queue_name=q)
				self._stop.wait(self.interval_s)
		finally:
			frappe.destroy()

	def start(self):
		self._thread = threading.Thread(target=self._loop, name="harness-flood", daemon=True)
		self._thread.start()
		return self

	def stop(self):
		self._stop.set()
		if self._thread:
			self._thread.join(timeout=10)


# --------------------------------------------------------------------------- #
# stop-click -> visible-stop (SUX-12)
# --------------------------------------------------------------------------- #


def measure_stop_visible(gateway, *, cadence_ms: float = 25.0, abort_after_frames: int = 2) -> dict:
	"""Start an abort-transcript turn, let a few frames stream, send chat.abort
	from a SECOND connection (web process aborting the worker's run), and
	measure the time to the visible aborted terminal."""
	from jarvis.chat.openclaw_client import OpenclawSession, OpenclawUnreachableError

	run_id = f"stopvis-{uuid.uuid4().hex[:8]}"
	gateway.arm(run_id, "abort", cadence_ms=cadence_ms)
	sess = OpenclawSession.connect(gateway.ws_url)
	try:
		sk = sess.create_session()
		ack = sess.chat_send(sk, "export please", run_id)
		t_abort = None
		stop_ms = None
		seen = 0
		for ev in sess.relay_turn_events(sk, ack.get("runId") or run_id, soft_deadline_s=10):
			kind = ev.get("kind")
			if kind in ("assistant", "tool"):
				seen += 1
				if seen == abort_after_frames and t_abort is None:
					t_abort = time.monotonic()
					s2 = OpenclawSession.connect(gateway.ws_url)
					try:
						s2.chat_abort(sk)
					finally:
						s2.close()
			if str(kind or "").startswith("relay:"):
				if t_abort is not None:
					stop_ms = (time.monotonic() - t_abort) * 1000.0
				return {"terminal": kind, "state": ev.get("state"), "stop_visible_ms": stop_ms}
		return {"terminal": None, "stop_visible_ms": None}
	except OpenclawUnreachableError as e:
		return {"terminal": "unreachable", "error": str(e), "stop_visible_ms": None}
	finally:
		sess.close()
