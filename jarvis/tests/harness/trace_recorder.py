"""TraceRecorder — per-turn timestamped bench-event traces.

Captures, for every run, the ordered sequence of things the bench does during
a turn — the diffable unit for legacy-vs-pump equivalence (Stage B):

  * job lifecycle        submit / start / end  (worker-pool executor)
  * gateway server-side  ack / lane_admit / first_frame / terminal
                         (merged from the FakeGateway RunTimeline at dump)
  * realtime publishes   assistant:delta / tool:start / tool:end / run:error /
                         run:recovering / ... (the REAL turn_handler fan-out,
                         captured by stubbing jarvis.chat.worker.publish_to_user)
  * DB-write summaries   msg.content.flush (real _AssistantContentBatcher
                         commits) + tool-row writes (encoded 1:1 by the
                         tool:start/tool:end publishes that carry a message_id)

Each run also gets a normalized ``signature`` — ordered publish kinds, DB-write
count, tool count, terminal kind — the whitelist-diffable equivalence key
(timing + batch boundaries allowed to differ; content / kinds / terminal /
side-effect counts must match).

Thread-safe: worker threads append concurrently; timestamps are monotonic
(ordering within a run) plus wall-clock (human reading).
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class _Event:
	run_id: str
	seq: int
	t_mono: float
	t_wall: float
	source: str  # job | gateway | publish | db
	kind: str
	detail: dict | None = None


@dataclass
class TraceRecorder:
	label: str = ""
	_events: list[_Event] = field(default_factory=list)
	_lock: threading.Lock = field(default_factory=threading.Lock)
	_seq: int = 0
	t0_mono: float = field(default_factory=time.monotonic)

	def record(
		self, run_id: str, source: str, kind: str, detail: dict | None = None, t_mono: float | None = None
	) -> None:
		ev = _Event(
			run_id=run_id,
			seq=0,
			t_mono=t_mono if t_mono is not None else time.monotonic(),
			t_wall=time.time(),
			source=source,
			kind=kind,
			detail=detail,
		)
		with self._lock:
			self._seq += 1
			ev.seq = self._seq
			self._events.append(ev)

	# convenience wrappers
	def job(self, run_id: str, kind: str, detail: dict | None = None, t_mono: float | None = None) -> None:
		self.record(run_id, "job", kind, detail, t_mono)

	def publish(self, run_id: str, kind: str, detail: dict | None = None) -> None:
		self.record(run_id, "publish", kind, detail)

	def db(self, run_id: str, kind: str, detail: dict | None = None) -> None:
		self.record(run_id, "db", kind, detail)

	# ---- gateway merge ----------------------------------------------------

	def attach_gateway(self, gateway) -> None:
		"""Merge each run's server-side timeline (ack/lane/first-frame/terminal)
		into the trace, using the gateway's monotonic clock (same process)."""
		for run_id in self.run_ids():
			tl = gateway.timeline(run_id)
			if not tl:
				continue
			if tl.ack_ts is not None:
				self.record(run_id, "gateway", "ack", {"transcript": tl.transcript}, t_mono=tl.ack_ts)
			if tl.lane_admit_ts is not None:
				self.record(
					run_id, "gateway", "lane_admit", {"dwell_ms": tl.dwell_ms()}, t_mono=tl.lane_admit_ts
				)
			if tl.first_frame_ts is not None:
				self.record(run_id, "gateway", "first_frame", None, t_mono=tl.first_frame_ts)
			if tl.terminal_ts is not None:
				self.record(
					run_id,
					"gateway",
					"terminal",
					{"kind": tl.terminal_kind, "frames": tl.frames_sent},
					t_mono=tl.terminal_ts,
				)

	# ---- queries ----------------------------------------------------------

	def run_ids(self) -> list[str]:
		with self._lock:
			seen = []
			s = set()
			for ev in self._events:
				if ev.run_id not in s:
					s.add(ev.run_id)
					seen.append(ev.run_id)
		return seen

	def events_for(self, run_id: str) -> list[_Event]:
		with self._lock:
			evs = [e for e in self._events if e.run_id == run_id]
		return sorted(evs, key=lambda e: (e.t_mono, e.seq))

	def signature(self, run_id: str) -> dict:
		"""The Stage-B equivalence key: content-bearing, timing-free."""
		evs = self.events_for(run_id)
		publishes = [e.kind for e in evs if e.source == "publish"]
		db_writes = sum(1 for e in evs if e.source == "db")
		tools = sum(1 for e in evs if e.source == "publish" and e.kind == "tool:start")
		terminal = next(
			(
				e.detail.get("kind")
				for e in reversed(evs)
				if e.source == "gateway" and e.kind == "terminal" and e.detail
			),
			None,
		)
		relay_terminal = next(
			(
				e.detail.get("relay_kind")
				for e in reversed(evs)
				if e.source == "job" and e.kind == "end" and e.detail
			),
			None,
		)
		return {
			"publish_kinds": publishes,
			"publish_kind_counts": dict(Counter(publishes)),
			"db_write_count": db_writes,
			"tool_count": tools,
			"gateway_terminal": terminal,
			"relay_terminal": relay_terminal,
		}

	# ---- dump -------------------------------------------------------------

	def to_dict(self) -> dict:
		runs = {}
		for run_id in self.run_ids():
			evs = self.events_for(run_id)
			base = evs[0].t_mono if evs else 0.0
			runs[run_id] = {
				"events": [
					{
						"rel_ms": round((e.t_mono - base) * 1000.0, 3),
						"t_wall": e.t_wall,
						"source": e.source,
						"kind": e.kind,
						"detail": e.detail,
					}
					for e in evs
				],
				"signature": self.signature(run_id),
			}
		return {"label": self.label, "run_count": len(runs), "runs": runs}

	def dump(self, path: str) -> str:
		with open(path, "w") as fh:
			json.dump(self.to_dict(), fh, indent=2, default=str)
		return path
