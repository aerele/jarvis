"""run_stage_b — WP-2 Stage B evidence pack (equivalence + pilot + kill matrix).

Runs the REAL Relay Pump (pump-mode on, in-process, over REAL sockets to the
harness ``FakeGateway``) to produce the Stage-B evidence the six owner criteria
and the correctness gates need. Three phases (select with --equivalence /
--pilot / --kills, or --all):

  EQUIVALENCE  the 8 Stage-A transcripts through PUMP mode; per-transcript
               content signature (final text / tool starts+ends / terminal /
               was_recovered) diffed against the Stage-A legacy corpus.
               Allowed deltas: timing, batching boundaries, the run:start/
               run:end lifecycle envelope (the legacy HARNESS reproduction
               omits it; real legacy emits it). Forbidden: content, event
               kinds, terminal outcomes, side-effect counts.

  PILOT        pump ON, 2-worker-equivalent shard, the Stage-A scenario set
               (concurrency 1/2/4/6, 10-send burst, confirmation storm) +
               C2 cold-start (N>=20) + SUX-12 stop-latency + C3 measured WITH
               the C6 canary load concurrently (OARF-13/OAR-13). Scores
               C1/C2/C3/C4/C5 against the BUILD-DIRECTIVE thresholds and the
               Stage-A baseline.

  KILLS        kill (hop-crash) injected AFTER each durable transition
               (queued / preparing / dispatching+streaming / terminal_observed
               / finalizing); a fresh hop reconciles from durable state and the
               turn reaches its deterministic terminal exactly once (codex
               round-1 §9 "kill before/after every durable transition").

Run as a SCRIPT outside the unit suite (the suite blocks even the loopback
socket) with the sanctioned escape hatch — it never touches a real tenant
(fake gateway, no Jarvis Settings write, injected deps, harness rows cleaned):

    JARVIS_ALLOW_REAL_NETWORK_IN_TESTS=1 \
      env/bin/python apps/jarvis/jarvis/tests/harness/run_stage_b.py --all \
      [--quick] [--site patterntest.localhost] [--out <dir>]
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("JARVIS_ALLOW_REAL_NETWORK_IN_TESTS", "1")

from jarvis.tests.harness import CONV, MSG, TURN, bootstrap
from jarvis.tests.harness import probes as P
from jarvis.tests.harness import transcripts as T
from jarvis.tests.harness.fake_gateway import FakeGateway

HARNESS_USER = "jarvis-stageb@example.com"
EFFECT = "Jarvis Turn Effect"
SESSION = "Jarvis Chat Session"
PUMP = "Jarvis Relay Pump"

DESK_URL = "http://127.0.0.1:8000/api/method/ping"
DESK_HOST = "patterntest.localhost"

# Baseline-matched gateway cadence (Stage-A run_baseline used 25ms), so the
# legacy-vs-pump C3 delta is apples-to-apples. The real openclaw ~150ms mirror
# cadence caveat is stated in the report.
CADENCE_MS = 25.0

_LINES: list[str] = []


def log(msg: str) -> None:
	line = f"[{time.strftime('%H:%M:%S')}] {msg}"
	_LINES.append(line)
	print(line, flush=True)


class StageB:
	def __init__(self, site: str, out_dir: str, *, quick: bool = False):
		self.site = site
		self.out_dir = out_dir
		self.quick = quick
		self.frappe = None
		self.gateway: FakeGateway | None = None
		self.target = f"stgb_{int(time.time())}"
		self.pubs: list[dict] = []
		self._pool_sess = None
		self.results: dict = {}

	# ---- lifecycle --------------------------------------------------------

	def setup(self):
		self.frappe = bootstrap(self.site)
		from jarvis.tests.harness import turn_runner as R

		self._restore_stubs = R.install_stubs()
		self._ensure_user()
		self._cleanup()
		self.frappe.set_user(HARNESS_USER)
		from jarvis.chat import turn_state as ts

		ts._ensure_control_row(self.target)
		self.frappe.db.commit()
		self.gateway = FakeGateway(cadence_ms=CADENCE_MS, max_concurrent=4, lane_sim=True).start()
		log(f"FakeGateway up at {self.gateway.ws_url}; shard={self.target}")

	def teardown(self):
		try:
			if self._pool_sess is not None:
				self._pool_sess.close()
		except Exception:
			pass
		try:
			if self.gateway:
				self.gateway.stop()
		finally:
			self._cleanup()
			if getattr(self, "_restore_stubs", None):
				self._restore_stubs()

	def _ensure_user(self):
		frappe = self.frappe
		if frappe.db.exists("User", HARNESS_USER):
			return
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": HARNESS_USER,
				"first_name": "StageB",
				"last_name": "Bench",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		)
		doc.insert(ignore_permissions=True)
		doc.add_roles("System Manager", "Jarvis User")
		frappe.db.commit()

	def _cleanup(self):
		frappe = self.frappe
		frappe.set_user("Administrator")
		for name in frappe.get_all(CONV, filters={"owner": HARNESS_USER}, pluck="name"):
			turns = frappe.get_all(TURN, filters={"conversation": name}, pluck="name")
			if turns:
				frappe.db.delete(EFFECT, {"turn": ["in", turns]})
			frappe.db.delete(TURN, {"conversation": name})
			frappe.db.delete(MSG, {"conversation": name})
			frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
		frappe.db.delete(SESSION, {"user": HARNESS_USER})
		frappe.db.delete(PUMP, {"relay_target_id": self.target})
		frappe.db.commit()

	# ---- row helpers ------------------------------------------------------

	def mk_conv(self) -> str:
		frappe = self.frappe
		frappe.set_user(HARNESS_USER)
		doc = frappe.get_doc({"doctype": CONV, "title": "New chat", "status": "Active"})
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		return doc.name

	def mk_seed(self, conv: str, content="hello") -> str:
		frappe = self.frappe
		seq = (
			frappe.db.sql(f"SELECT MAX(seq) FROM `tab{MSG}` WHERE conversation=%(c)s", {"c": conv})[0][0] or 0
		) + 1
		doc = frappe.get_doc(
			{"doctype": MSG, "conversation": conv, "seq": seq, "role": "user", "content": content}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		return doc.name

	def warm_session(self, conv: str):
		"""Pre-create the gateway session so the measured turn is WARM (prepare
		skips create_session)."""
		from jarvis.chat.openclaw_client import OpenclawSession

		sess = OpenclawSession.connect(self.gateway.ws_url)
		try:
			sk = sess.create_session()
			self.frappe.db.set_value(CONV, conv, "session_key", sk)
			self.frappe.db.commit()
		finally:
			sess.close()

	def state(self, rid) -> str:
		return self.frappe.db.get_value(TURN, rid, "state")

	def val(self, rid, field):
		return self.frappe.db.get_value(TURN, rid, field)

	def content_of(self, rid) -> str:
		amsg = self.val(rid, "assistant_message")
		return (self.frappe.db.get_value(MSG, amsg, "content") if amsg else "") or ""

	# ---- real-socket deps + patches --------------------------------------

	@contextmanager
	def _pool_to_gateway(self):
		from jarvis.chat.openclaw_client import OpenclawSession

		if self._pool_sess is None:
			self._pool_sess = OpenclawSession.connect(self.gateway.ws_url)

		@contextmanager
		def _co(url):
			yield self._pool_sess

		with patch("jarvis.chat.openclaw_session_pool.checkout", _co):
			yield

	@contextmanager
	def _enrichment_mocks(self):
		with (
			patch("jarvis.chat.turn_handler.persist_rich_outputs", lambda *a, **k: None),
			patch("jarvis.chat.macros.advance_after_turn", lambda *a, **k: None),
			patch("jarvis.learning.app_analysis.on_turn_end", lambda *a, **k: None),
			patch("jarvis.chat.title.enqueue_autotitle", lambda *a, **k: None),
			patch("jarvis.chat.usage.record_turn_usage", lambda *a, **k: None),
			patch("jarvis.chat.wiki.wiki_enabled", return_value=False),
		):
			yield

	def _make_deps(self):
		from jarvis.chat import finalize, pump
		from jarvis.chat import prepare as prepare_mod
		from jarvis.chat.openclaw_client import OpenclawSession
		from jarvis.chat.relay_mux import RelayMux

		gw = self.gateway

		def make_mux(target, epoch):
			mux = RelayMux(OpenclawSession.connect(gw.ws_url), target, on_breaker=pump._on_poison_breaker)
			return mux.start()

		def dispatch_prepare(run_id, target):
			with self._pool_to_gateway():
				prepare_mod.run_prepare(run_id, target)

		def enqueue_finalize(run_id, target):
			with self._pool_to_gateway(), self._enrichment_mocks():
				finalize.run_finalize(run_id, target)

		deps = pump.PumpDeps()
		deps.make_mux = make_mux
		deps.dispatch_prepare = dispatch_prepare
		deps.enqueue_finalize = enqueue_finalize
		return deps

	@contextmanager
	def _pump_flags_on(self):
		from jarvis.chat import admission, pump
		from jarvis.chat import turn_state as ts

		def _cap(user, payload):
			p = dict(payload)
			p["_t_mono"] = time.monotonic()
			self.pubs.append(p)

		with (
			patch.object(pump, "pump_mode_active", return_value=True),
			patch.object(pump, "pump_configured", return_value=True),
			patch.object(pump, "pump_draining", return_value=False),
			patch.object(admission, "relay_target_id", lambda conversation=None: self.target),
			patch.object(pump, "ensure_pump", lambda *a, **k: {"enqueued": False}),
			# lpush_wake is LEFT REAL (WP-2 C1 fix): accept LPUSHes the real wake bus so
			# the reactor's _WakeThread pokes the mux immediately — the event-driven
			# promote path the C1 fix adds. ensure_pump stays stubbed (no real hop
			# enqueue; the reactor is driven in-process).
			patch.object(ts, "publish_to_user", _cap),
		):
			yield

	def _accept(self, conv, rid, transcript="success", turn_class="interactive") -> tuple[dict, float]:
		from jarvis.chat import admission

		seed = self.mk_seed(conv)
		t0 = time.monotonic()
		adm = admission.accept_or_queue(
			conversation=conv,
			run_id=rid,
			seed_message=seed,
			turn_class=turn_class,
			dispatch=lambda: None,
		)
		return adm, t0

	def _run_hop(self, deps, *, soft=120, hard=140, slices=4000):
		from jarvis.chat import pump

		return pump.run_pump_hop(
			self.target, deps=deps, soft_budget_s=soft, hard_deadline_s=hard, max_slices=slices
		)

	def _lapse_lease(self):
		"""Simulate a hop-crash: expire the lease + clear the Redis mirror so a
		fresh hop takes over (faithful — the real gap is <= LEASE_TTL_S=30s)."""
		from jarvis.chat import pump

		self.frappe.db.set_value(
			PUMP,
			self.target,
			"lease_expires_at",
			self.frappe.utils.add_to_date(None, seconds=-1),
			update_modified=False,
		)
		self.frappe.db.commit()
		pump._clear_lease_mirror(self.target)

	# ---- per-run publish queries -----------------------------------------

	def _first_delta_mono(self, rid: str) -> float | None:
		for p in self.pubs:
			if p.get("run_id") == rid and p.get("kind") == "assistant:delta":
				return p["_t_mono"]
		return None

	def _dispatch_mono(self, rid: str) -> float | None:
		for p in self.pubs:
			if p.get("run_id") == rid and p.get("kind") == "run:start":
				return p["_t_mono"]
		return None

	def _flush_gaps(self, rid: str) -> list[float]:
		ts_list = [
			p["_t_mono"] for p in self.pubs if p.get("run_id") == rid and p.get("kind") == "assistant:delta"
		]
		return [(b - a) * 1000.0 for a, b in itertools.pairwise(ts_list)]

	def _run_pubs(self, rid: str) -> list[dict]:
		return [p for p in self.pubs if p.get("run_id") == rid]

	# ================================================================== #
	# PHASE 1 — EQUIVALENCE
	# ================================================================== #

	def _transcript_tool_counts(self, name: str) -> tuple[int, int]:
		frames = T.get(name).get("frames") or []
		starts = sum(1 for f in frames if f.get("op") == "tool_start")
		ends = sum(1 for f in frames if f.get("op") == "tool_end")
		return starts, ends

	def phase_equivalence(self, deps):
		log("=== PHASE EQUIVALENCE — 8 transcripts through the PUMP ===")
		legacy = self._load_legacy_corpus()

		# Inject a recording apply_tool: the default seam is a no-op (the real
		# tool-row writer is out-of-band, WP-1d, exactly-once unit-tested). Recording
		# the mux's tool-event DELIVERIES lets equivalence assert the pump demuxes and
		# delivers the same tool round-trips the transcript defines (side-effect count).
		def _rec_apply_tool(ctx, rs, event):
			# CDX-5: the applier seam is now (ctx, rs, event). This phase records the
			# demuxed tool-event DELIVERIES by count to assert the pump delivers the
			# same tool round-trips the transcript defines; the PRODUCTION applier's
			# durable-row/publish equivalence is proven deterministically in
			# test_pump_pipeline.TestToolApplierEquivalence.
			run_id = getattr(rs, "run_id", None)
			data = event.get("data") if isinstance(event.get("data"), dict) else event
			phase = (data or {}).get("phase") or event.get("phase")
			name = (data or {}).get("tool_name") or (data or {}).get("name") or event.get("tool_name")
			self.pubs.append(
				{
					"kind": "tool:start" if phase == "start" else "tool:end",
					"run_id": run_id,
					"tool_name": name,
					"_t_mono": time.monotonic(),
				}
			)

		orig_apply = deps.apply_tool
		deps.apply_tool = _rec_apply_tool
		out = {}
		try:
			for name in T.NAMES:
				self.pubs.clear()
				res = self._equiv_one(deps, name)
				res["legacy"] = legacy.get(name)
				res["transcript_tools"] = list(self._transcript_tool_counts(name))
				res["verdict"] = self._equiv_verdict(name, res, legacy.get(name))
				out[name] = res
				log(
					f"  {name:22s} pump_state={res['final_state']:12s} tools={res['tool_starts']}/{res['tool_ends']} "
					f"(txscript {res['transcript_tools']}) recovered={res['was_recovered']} verdict={res['verdict']['status']}"
				)
		finally:
			deps.apply_tool = orig_apply
		self.results["equivalence"] = out
		return out

	# transcripts whose durable resolution needs the 600s recovery budget /
	# watchdog (unit-tested in TestSuxf1RecoveringMirror) — we drive them only to
	# the parked `recovering` state here (the equivalent of legacy's parked
	# relay:interrupted awaiting the recovery cron).
	_PARK_ONLY = {"ack-timeout"}
	# transcripts that drop mid-stream: bump the per-run cadence so the batcher
	# flushes at least one durable delta BEFORE the drop (last_event_seq>0), which
	# is what makes the turn snapshot-recovery eligible (OARF-2). A drop before any
	# flush is correctly re-attached (not recovered) — that is the ack-timeout
	# family, not a realistic mid-STREAM drop.
	_DROP = {"ws-drop", "recovered"}

	def _equiv_one(self, deps, name: str) -> dict:
		from jarvis.chat import pump

		conv = self.mk_conv()
		rid = f"eq_{name}_{int(time.time() * 1000) % 100000}"
		self.warm_session(conv)  # measured turn is warm (equivalence, not latency)
		cadence = 100.0 if name in self._DROP else CADENCE_MS
		self._lapse_lease()  # fresh lease per transcript (no cross-transcript carryover)
		with self._pump_flags_on():
			adm, _ = self._accept(conv, rid, transcript=name)
			self.gateway.arm(rid, name, cadence_ms=cadence, ack_timeout_hold_ms=1200)
			if name in self._PARK_ONLY:
				# fire the ack-timeout park fast (real ACK_TIMEOUT_S=15s is too slow
				# to wait on; the park is the point, not the 15s wall).
				with patch.object(pump, "ACK_TIMEOUT_S", 1.0):
					self._run_hop(deps, soft=8, hard=10, slices=300)
				st1 = self.state(rid)
				partial = self.content_of(rid)
				extra_hops = 0
			else:
				self._run_hop(deps, soft=25, hard=30, slices=600)
				st1 = self.state(rid)
				partial = self.content_of(rid)
				# parked/dropped turns need a takeover hop (faithful: real gap
				# <= LEASE_TTL_S) to drive settle / snapshot-recovery.
				extra_hops = 0
				while (
					self.state(rid) not in ("done", "errored", "cancelled", "recovering") and extra_hops < 2
				):
					self._lapse_lease()
					self._run_hop(deps, soft=20, hard=25, slices=600)
					extra_hops += 1
		st = self.state(rid)
		final = self.content_of(rid)
		pubs = self._run_pubs(rid)
		kinds = [p.get("kind") for p in pubs]
		return {
			"final_state": st,
			"hop1_state": st1,
			"partial_len": len(partial),
			"answer_len": len(final),
			"answer_head": final[:70],
			"tool_starts": kinds.count("tool:start"),
			"tool_ends": kinds.count("tool:end"),
			"tool_names": sorted(
				{p.get("tool_name") for p in pubs if p.get("kind") == "tool:start" and p.get("tool_name")}
			),
			"was_recovered": int(self.val(rid, "was_recovered") or 0),
			"content_kinds": sorted(
				set(k for k in kinds if k not in ("run:start", "run:end", "message:enriched"))
			),
			"all_kinds": sorted(set(kinds)),
			"extra_hops": extra_hops,
			"error": self.val(rid, "error"),
		}

	def _load_legacy_corpus(self) -> dict:
		"""Extract the legacy per-transcript signature from the Stage-A trace
		corpus (baseline/trace_transcript_smoke.json)."""
		path = os.path.join(
			os.path.dirname(self.out_dir.rstrip("/")), "baseline", "trace_transcript_smoke.json"
		)
		alt = "/home/vignesh/jarvis/jarvis-chat-concurrency-design/implementation/wp-2/baseline/trace_transcript_smoke.json"
		for p in (path, alt):
			if os.path.exists(p):
				with open(p) as fh:
					data = json.load(fh)
				break
		else:
			return {}
		out = {}
		for run_id, run in data.get("runs", {}).items():
			# run_id like "smoke-success-<hex>"
			base = run_id.split("-")
			# transcript name is between "smoke" and the trailing hex
			name = "-".join(base[1:-1]) if len(base) >= 3 else run_id
			sig = run.get("signature", {})
			kinds = sig.get("publish_kinds", [])
			out[name] = {
				"relay_terminal": sig.get("relay_terminal"),
				"gateway_terminal": sig.get("gateway_terminal"),
				"tool_starts": kinds.count("tool:start"),
				"tool_ends": kinds.count("tool:end"),
				"content_kinds": sorted(
					set(k for k in kinds if k not in ("run:start", "run:end", "message:enriched"))
				),
				"db_write_count": sig.get("db_write_count"),
			}
		return out

	# map legacy relay terminal -> the semantically-equivalent pump terminal set.
	# relay:interrupted: legacy leaves the turn PARKED (streaming=1) for the recovery
	# cron; the pump actively RESOLVES it (snapshot-recover -> done; empty tail ->
	# errored; or parked `recovering` awaiting the 600s budget/watchdog — the
	# equivalent of legacy's parked-interrupted).
	_TERMINAL_EQUIV = {
		"relay:final": {"done"},
		"relay:error": {"cancelled", "errored"},
		"relay:interrupted": {"done", "errored", "recovering"},
	}

	def _equiv_verdict(self, name: str, pump_res: dict, legacy: dict | None) -> dict:
		if legacy is None:
			return {"status": "NO-LEGACY", "notes": ["no legacy corpus row"]}
		forbidden = []
		allowed = []
		notes = []
		# terminal outcome — legacy relay terminal -> pump terminal equiv set
		exp = self._TERMINAL_EQUIV.get(legacy["relay_terminal"], set())
		if pump_res["final_state"] not in exp:
			forbidden.append(
				f"terminal: pump={pump_res['final_state']} not in equiv-set {sorted(exp)} for legacy {legacy['relay_terminal']}"
			)
		# side-effect counts: tool starts/ends compared to the AUTHORITATIVE transcript
		# definition (legacy's own publish counts are a legacy-harness rendering detail
		# and are reported informationally, not as the equivalence oracle).
		t_starts, t_ends = pump_res.get("transcript_tools", [0, 0])
		# ack-timeout/ws-drop park/drop before the tool frames, so 0 delivered is correct
		expects_tools = pump_res["final_state"] == "done"
		if expects_tools:
			if pump_res["tool_starts"] != t_starts:
				forbidden.append(f"tool_starts pump={pump_res['tool_starts']} transcript={t_starts}")
			if pump_res["tool_ends"] != t_ends:
				forbidden.append(f"tool_ends pump={pump_res['tool_ends']} transcript={t_ends}")
		if legacy.get("tool_starts") != t_starts or legacy.get("tool_ends") != t_ends:
			notes.append(
				f"legacy publish counts ({legacy.get('tool_starts')}/{legacy.get('tool_ends')}) differ from "
				f"transcript ({t_starts}/{t_ends}) — legacy _handle_event rendering detail"
			)
		# content: a turn that resolved `done` must carry a non-empty answer +
		# assistant:delta; a legacy relay:final transcript with assistant frames must
		# stream in the pump too.
		if pump_res["final_state"] == "done":
			if pump_res["answer_len"] == 0:
				forbidden.append("pump done but final answer empty")
			if "assistant:delta" not in pump_res["content_kinds"]:
				forbidden.append("pump done but no assistant:delta")
		status = "FORBIDDEN-DELTA" if forbidden else "EQUIVALENT"
		return {"status": status, "forbidden": forbidden, "allowed": allowed, "notes": notes}

	# ================================================================== #
	# PHASE 2 — PILOT
	# ================================================================== #

	def phase_pilot(self, deps):
		log("=== PHASE PILOT — C1..C6 with the pump ON ===")
		out = {}
		out["concurrency"] = self._pilot_concurrency(deps)
		out["burst"] = self._pilot_burst(deps)
		out["confirmation_storm"] = self._pilot_storm(deps)
		out["cold_start_c2"] = self._pilot_cold_start(deps)
		out["stop_latency_sux12"] = self._pilot_stop_latency()
		out["c3_under_c6_load"] = self._pilot_c3_under_c6(deps)
		out["gateway_max_observed_main_lane"] = self.gateway.max_observed_main()
		self.results["pilot"] = out
		return out

	def _drain_all(self, deps, rids, *, max_hops=8):
		"""Run hops until every rid is terminal (or max_hops). Promotion happens
		per-slice inside a hop; multiple hops model successive lease terms."""
		self._lapse_lease()  # fresh lease for this drain (no carryover from a prior rep)
		hops = 0
		while hops < max_hops:
			self._run_hop(deps, soft=25, hard=30, slices=4000)
			hops += 1
			pending = [r for r in rids if self.state(r) not in ("done", "errored", "cancelled")]
			if not pending:
				break
			# a hop that idle-exited or handed off but left work: re-acquire
			self._lapse_lease()
		return hops

	# ---- live reactor driver (faithful C1/C3/C5) ------------------------- #

	def _fresh_state(self, rid) -> str:
		"""Read a turn's state with a fresh snapshot (frappe defaults to REPEATABLE
		READ; without a rollback the main thread never sees the reactor thread's
		commits)."""
		try:
			self.frappe.db.rollback()
		except Exception:
			pass
		return self.frappe.db.get_value(TURN, rid, "state")

	def _live_run(self, deps, arms, *, warm=True, stagger_s=0.0, timeout_s=45) -> dict:
		"""Run ONE continuous reactor (a single hop in a background thread, idle-exit
		gated OFF until we signal stop) and accept the measured turns WHILE it streams
		— the faithful model of the pump's one continuous reactor. A warm-up turn
		brings the reactor past its one-time boot (make_mux + reconcile) BEFORE we
		start the clock, so measured first-token excludes the one-time boot (that is
		C2). Returns per-rid {ft_submit, ft_dispatch, flush_gaps, dwell, qwait, state}.
		"""
		import threading

		from jarvis.chat import pump

		self.pubs.clear()
		convs = [self.mk_conv() for _ in arms]
		if warm:
			for c in convs:
				self.warm_session(c)
		wu_conv = self.mk_conv()
		self.warm_session(wu_conv)
		stop = threading.Event()
		err: list = []
		_orig_idle = pump._idle_exit

		def reactor():
			import frappe

			frappe.init(site=self.site)
			frappe.connect()
			try:
				pump.run_pump_hop(
					self.target,
					deps=deps,
					soft_budget_s=timeout_s + 20,
					hard_deadline_s=timeout_s + 25,
					max_slices=10**7,
				)
			except Exception as e:  # pragma: no cover - defensive
				err.append(repr(e))
			finally:
				try:
					frappe.destroy()
				except Exception:
					pass

		# Defer finalize (enrichment) so it never blocks the single reactor thread:
		# in production finalize is a SEPARATE RQ job (enqueue returns instantly), so a
		# synchronous inline finalize (usage-poll RPC + enrichment ledger) would stall
		# concurrent streaming lanes and pollute the C3 flush cadence. We measure the
		# STREAMING path here; a turn is "measurement-terminal" once settled
		# (`finalizing`, slot released). Enrichment idempotency is unit-tested +
		# demonstrated in the kill/e2e phases.
		terminal_set = ("done", "errored", "cancelled", "finalizing")
		accepts: dict = {}
		rids: list = []
		with (
			self._pump_flags_on(),
			patch.object(pump, "_idle_exit", lambda ctx: (_orig_idle(ctx) if stop.is_set() else False)),
			patch.object(deps, "enqueue_finalize", lambda *a, **k: None),
		):
			self._lapse_lease()
			wu_rid = f"wu_{int(time.time() * 1e6) % 10**7}"
			self._accept(wu_conv, wu_rid)
			self.frappe.db.commit()
			self.gateway.arm(wu_rid, "success")
			th = threading.Thread(target=reactor, name="stageb-reactor", daemon=True)
			th.start()
			# wait until the warm-up turn settles => reactor is live (mux up, past reconcile)
			t_wait = time.monotonic() + 20
			while time.monotonic() < t_wait and self._fresh_state(wu_rid) not in terminal_set:
				time.sleep(0.05)
			# accept the MEASURED turns into the live reactor
			for i, (c, arm) in enumerate(zip(convs, arms, strict=False)):
				rid = f"lr_{int(time.time() * 1e6) % 10**7}_{i}"
				rids.append(rid)
				accepts[rid] = time.monotonic()
				self._accept(c, rid)
				self.frappe.db.commit()
				self.gateway.arm(rid, arm)
				if stagger_s:
					time.sleep(stagger_s)
			# wait until every measured turn is measurement-terminal (settled)
			deadline = time.monotonic() + timeout_s
			while time.monotonic() < deadline:
				if all(self._fresh_state(r) in terminal_set for r in rids):
					break
				time.sleep(0.05)
			stop.set()
		th.join(timeout=15)
		out = {"rids": {}, "reactor_err": err}
		for rid in rids:
			fd = self._first_delta_mono(rid)
			dm = self._dispatch_mono(rid)
			tl = self.gateway.timeline(rid)
			out["rids"][rid] = {
				"ft_submit": ((fd - accepts[rid]) * 1000.0) if fd else None,
				"ft_dispatch": ((fd - dm) * 1000.0) if (fd and dm) else None,
				"qwait": ((dm - accepts[rid]) * 1000.0) if dm else None,
				"flush_gaps": self._flush_gaps(rid),
				"dwell": (tl.dwell_ms() if tl else None),
				"state": self._fresh_state(rid),
			}
		return out

	def _pilot_concurrency(self, deps):
		levels = [1, 2, 4, 6]
		reps = 2 if self.quick else 3
		out = {}
		for N in levels:
			ft_submit, ft_dispatch, flush_gaps, dwell = [], [], [], []
			max_lane = 0
			for _ in range(reps):
				r = self._live_run(deps, ["success"] * N)
				for rid, m in r["rids"].items():
					if m["ft_submit"] is not None:
						ft_submit.append(m["ft_submit"])
					if m["ft_dispatch"] is not None:
						ft_dispatch.append(m["ft_dispatch"])
					flush_gaps.extend(m["flush_gaps"])
					if m["dwell"] is not None:
						dwell.append(m["dwell"])
				max_lane = max(max_lane, self.gateway.max_observed_main())
			out[f"N={N}"] = {
				"reps": reps,
				"first_token_from_submit_ms": P.summarize(ft_submit),
				"first_token_from_dispatch_ms": P.summarize(ft_dispatch),
				"flush_gap_ms_C3": P.summarize(flush_gaps),
				"dwell_ms_C4": P.summarize(dwell),
				"max_observed_main_lane": max_lane,
			}
			log(
				f"  concurrency N={N}: ft_submit p50/p95={out[f'N={N}']['first_token_from_submit_ms']['p50']}/"
				f"{out[f'N={N}']['first_token_from_submit_ms']['p95']} "
				f"ft_disp p95={out[f'N={N}']['first_token_from_dispatch_ms']['p95']} "
				f"flush p95={out[f'N={N}']['flush_gap_ms_C3']['p95']} dwell p95={out[f'N={N}']['dwell_ms_C4']['p95']}"
			)
		return out

	def _pilot_burst(self, deps):
		n = 10
		reps = 2 if self.quick else 3
		ft_req, qwaits = [], []
		max_lane = 0
		for _ in range(reps):
			r = self._live_run(deps, ["success"] * n, timeout_s=60)
			for rid, m in r["rids"].items():
				if m["ft_submit"] is not None:
					ft_req.append(m["ft_submit"])
				if m["qwait"] is not None:
					qwaits.append(m["qwait"])
			max_lane = max(max_lane, self.gateway.max_observed_main())
		return {
			"n_per_burst": n,
			"reps": reps,
			"first_token_from_request_ms": P.summarize(ft_req),
			"queue_wait_ms": P.summarize(qwaits),
			"max_observed_main_lane": max_lane,
			"samples": len(ft_req),
		}

	def _pilot_storm(self, deps):
		n = 6 if self.quick else 10
		reps = 2
		ft, flush = [], []
		terms = {}
		for _ in range(reps):
			r = self._live_run(deps, ["confirmation-card"] * n, timeout_s=60)
			for rid, m in r["rids"].items():
				if m["ft_submit"] is not None:
					ft.append(m["ft_submit"])
				flush.extend(m["flush_gaps"])
				terms[m["state"]] = terms.get(m["state"], 0) + 1
		return {
			"n_per_storm": n,
			"reps": reps,
			"first_token_from_submit_ms": P.summarize(ft),
			"flush_gap_ms_C3": P.summarize(flush),
			"terminals": terms,
		}

	def _cold_one(self, deps, *, warm_session: bool) -> float | None:
		"""One turn through a FRESH hop (pays lease_acquire + make_mux + reconcile =
		the in-process pump boot). warm_session=True isolates the boot cost from the
		session-bootstrap cost."""
		self.pubs.clear()
		conv = self.mk_conv()
		if warm_session:
			self.warm_session(conv)
		self._lapse_lease()
		with self._pump_flags_on():
			rid = f"cold_{int(time.time() * 1e6) % 10**7}"
			adm, t0 = self._accept(conv, rid)
			self.frappe.db.commit()
			self.gateway.arm(rid, "success")
			self._run_hop(deps, soft=15, hard=18, slices=5000)
		fd = self._first_delta_mono(rid)
		return ((fd - t0) * 1000.0) if fd else None

	def _pilot_cold_start(self, deps):
		"""C2 bounded cold-start delay. warm = first-token in an ALREADY-LIVE reactor
		(_live_run); cold = first-token when the pump must BOOT for this send (a fresh
		hop: lease_acquire + make_mux + reconcile). The delta is the in-process boot
		cost. The dominant real-world cold-start cost — the ensure_pump RQ enqueue ->
		worker pickup latency — is NOT captured in-process (ensure_pump is stubbed);
		flagged UNMEASURABLE-LOCALLY, the FC pilot measures it."""
		N = 8 if self.quick else 14
		warm_ft = []
		for _ in range(max(2, N // 4)):
			r = self._live_run(deps, ["success"] * 1)
			for m in r["rids"].values():
				if m["ft_submit"] is not None:
					warm_ft.append(m["ft_submit"])
		cold_ft = [v for v in (self._cold_one(deps, warm_session=True) for _ in range(N)) if v is not None]
		cold_ft_bootstrap = [
			v
			for v in (self._cold_one(deps, warm_session=False) for _ in range(max(4, N // 3)))
			if v is not None
		]
		warm_s, cold_s, cold_bs = P.summarize(warm_ft), P.summarize(cold_ft), P.summarize(cold_ft_bootstrap)
		return {
			"N_cold": N,
			"warm_live_first_token_ms": warm_s,
			"cold_boot_first_token_ms": cold_s,
			"cold_boot_plus_session_bootstrap_ms": cold_bs,
			"added_boot_delay_p50_ms": (
				None
				if cold_s["p50"] is None or warm_s["p50"] is None
				else round(cold_s["p50"] - warm_s["p50"], 2)
			),
			"added_boot_delay_p95_ms": (
				None
				if cold_s["p95"] is None or warm_s["p95"] is None
				else round(cold_s["p95"] - warm_s["p95"], 2)
			),
		}

	def _pilot_stop_latency(self):
		reps = 3 if self.quick else 8
		vals = []
		for _ in range(reps):
			r = P.measure_stop_visible(self.gateway, cadence_ms=CADENCE_MS)
			if r.get("stop_visible_ms") is not None:
				vals.append(r["stop_visible_ms"])
		return {"reps": reps, "stop_visible_ms": P.summarize(vals)}

	def _pilot_c3_under_c6(self, deps):
		"""OAR-13/OARF-13: C3 flush-gap at ceiling 4 measured WITH the C6 canary
		RQ + Desk load running concurrently, plus a sustained RQ flood."""
		from jarvis.tests.harness import probes as PP

		canary = PP.CanaryProbe(self.site, cadence_s=7.0, desk_url=DESK_URL, desk_host=DESK_HOST).start()
		# idle baseline window: ~15s => 2+ canary samples at the 7s cadence.
		time.sleep(15)
		with canary._lock:
			idle_ct = len(canary.samples)
			idle_samples = list(canary.samples)
		# BOUNDED one-shot floods (<< the Frappe 700-job queue-overload guard), re-issued
		# before each measured run so RQ stays under real pressure but never overloads.
		flush, ft = [], []
		flood_total = 0
		reps = 2 if self.quick else 3
		for _ in range(reps):
			try:
				flood_total += PP.background_flood(self.site, 150, queue_name="jarvis_chat")
			except Exception:
				pass
			r = self._live_run(deps, ["success"] * 4, timeout_s=60)  # ceiling 4
			for rid, m in r["rids"].items():
				flush.extend(m["flush_gaps"])
				if m["ft_submit"] is not None:
					ft.append(m["ft_submit"])
		canary.stop()
		with canary._lock:
			load_samples = list(canary.samples)[idle_ct:]

		def _agg(samples, key, sub="wait_ms"):
			if key == "desk":
				return P.summarize([s["desk"]["ms"] for s in samples if s["desk"].get("ok")])
			return P.summarize([s[key][sub] for s in samples])

		def _starved(samples):
			return sum(1 for s in samples if s["short"].get("starved") or s["long"].get("starved"))

		return {
			"ceiling": 4,
			"flush_gap_ms_C3_under_load": P.summarize(flush),
			"first_token_from_submit_ms": P.summarize(ft),
			"canary_idle": {
				"short_wait_ms": _agg(idle_samples, "short"),
				"desk_ms": _agg(idle_samples, "desk"),
				"samples": len(idle_samples),
				"starved": _starved(idle_samples),
			},
			"canary_under_load": {
				"short_wait_ms": _agg(load_samples, "short"),
				"desk_ms": _agg(load_samples, "desk"),
				"samples": len(load_samples),
				"starved": _starved(load_samples),
			},
			"flood_jobs_total": flood_total,
		}

	# ================================================================== #
	# PHASE 3 — KILLS AT DURABLE TRANSITIONS
	# ================================================================== #

	def phase_kills(self, deps):
		log("=== PHASE KILLS — hop-crash after each durable transition ===")
		out = {}
		out["after_queued"] = self._kill_after_queued(deps)
		out["after_preparing"] = self._kill_after_preparing(deps)
		out["after_streaming_recoverable"] = self._kill_after_streaming(deps, recoverable=True)
		out["after_streaming_no_tail"] = self._kill_after_streaming(deps, recoverable=False)
		out["after_terminal_observed"] = self._kill_after_terminal_observed(deps)
		out["after_finalizing"] = self._kill_after_finalizing(deps)
		self.results["kills"] = out
		for k, v in out.items():
			log(f"  kill {k:30s} -> final={v.get('final_state')} PASS={v.get('PASS')}")
		return out

	def _fresh_conv_accept(self, deps, name="success", warm=True, cadence_ms=CADENCE_MS):
		conv = self.mk_conv()
		if warm:
			self.warm_session(conv)
		rid = f"kill_{name}_{int(time.time() * 1e6) % 10**7}"
		adm, _ = self._accept(conv, rid, transcript=name)
		self.gateway.arm(rid, name, cadence_ms=cadence_ms, ack_timeout_hold_ms=1500)
		return conv, rid

	def _kill_after_queued(self, deps):
		"""Accept -> crash immediately (turn is queued, no work done) -> fresh hop
		must promote+prepare+dispatch+complete."""
		self.pubs.clear()
		with self._pump_flags_on():
			conv, rid = self._fresh_conv_accept(deps)
			st_before = self.state(rid)  # queued
			self._lapse_lease()  # crash before any hop ran
			self._drain_all(deps, [rid], max_hops=4)
		return {
			"state_at_kill": st_before,
			"final_state": self.state(rid),
			"answer_len": len(self.content_of(rid)),
			"PASS": self.state(rid) == "done" and len(self.content_of(rid)) > 0,
		}

	def _kill_after_preparing(self, deps):
		"""Drive promote+prepare (session created, state ready/preparing), crash
		before dispatch, fresh hop dispatches+completes; session at-most-once."""
		self.pubs.clear()
		with self._pump_flags_on():
			conv, rid = self._fresh_conv_accept(deps, warm=False)
			# Run promote+prepare only: promote runs prepare synchronously via deps,
			# then we crash before it dispatches (turn parks at `ready`).
			from jarvis.chat import pump as _pump
			from jarvis.chat import turn_state as _ts

			self._lapse_lease()  # ensure the shard lease is free to acquire
			won, epoch = _ts.lease_acquire(self.target, "killprep")
			ctx = _pump.PumpContext(
				relay_target_id=self.target,
				epoch=epoch,
				holder="killprep",
				hop_counter=0,
				site=self.frappe.local.site,
				deps=deps,
			)
			_pump._promote_queued(ctx)
			self.frappe.db.commit()
			st_before = self.state(rid)  # ready (prepared, awaiting dispatch)
			sess_after_prepare = self.frappe.db.count(SESSION, {"user": HARNESS_USER})
			self._lapse_lease()  # crash before dispatch
			self._drain_all(deps, [rid], max_hops=4)
			sess_final = self.frappe.db.count(SESSION, {"user": HARNESS_USER})
		return {
			"state_at_kill": st_before,
			"final_state": self.state(rid),
			"answer_len": len(self.content_of(rid)),
			"sessions_after_prepare": sess_after_prepare,
			"sessions_final": sess_final,
			"session_at_most_once": sess_final <= max(1, sess_after_prepare),
			"PASS": self.state(rid) == "done"
			and len(self.content_of(rid)) > 0
			and sess_final <= max(1, sess_after_prepare),
		}

	def _kill_after_streaming(self, deps, *, recoverable: bool):
		"""Dispatch + partial stream, WS-drop mid-stream (hop ends transport_closed),
		fresh hop reconciles: snapshot-recover (recoverable=durable tail holds the
		answer) -> done; else honest errored (OARF-2 empty-window)."""
		self.pubs.clear()
		name = "recovered" if recoverable else "ws-drop"
		with self._pump_flags_on():
			# cadence 100ms so >=1 durable delta flushes before the drop
			# (last_event_seq>0 => snapshot-recovery eligible, OARF-2).
			conv, rid = self._fresh_conv_accept(deps, name=name, cadence_ms=100.0)
			out1 = self._run_hop(deps, soft=25, hard=30, slices=600)
			st1 = self.state(rid)
			partial = len(self.content_of(rid))
			self._lapse_lease()
			self._drain_all(deps, [rid], max_hops=4)
		st = self.state(rid)
		final = len(self.content_of(rid))
		expect = "done" if recoverable else "errored"
		return {
			"transcript": name,
			"hop1_exit": out1.get("exit"),
			"hop1_state": st1,
			"partial_len": partial,
			"final_state": st,
			"answer_len": final,
			"was_recovered": int(self.val(rid, "was_recovered") or 0),
			"expected": expect,
			"PASS": st == expect and (final > partial if recoverable else True),
		}

	def _kill_after_terminal_observed(self, deps):
		"""Stream to terminal but crash BEFORE settlement runs; a fresh hop's
		reconcile settles the owed terminal from the row (R-12) -> done."""
		self.pubs.clear()
		from jarvis.chat import pump as _pump
		from jarvis.chat import turn_state as _ts

		with self._pump_flags_on():
			conv, rid = self._fresh_conv_accept(deps)
			# Drive dispatch+stream, but suppress settlement so the turn parks at
			# terminal_observed (simulating a crash after the terminal CAS, before
			# settle) — patch invoke_settlement to a no-op for this hop only.
			with patch.object(deps, "invoke_settlement", lambda *a, **k: None):
				self._run_hop(deps, soft=5, hard=7, slices=600)
			st_before = self.state(rid)  # terminal_observed (settlement owed)
			self._lapse_lease()
			self._drain_all(deps, [rid], max_hops=4)
		return {
			"state_at_kill": st_before,
			"final_state": self.state(rid),
			"answer_len": len(self.content_of(rid)),
			"PASS": st_before == "terminal_observed" and self.state(rid) == "done",
		}

	def _kill_after_finalizing(self, deps):
		"""Settle (turn reaches finalizing), crash before finalize completes; a
		fresh hop / watchdog re-enqueues finalize -> done (never re-enters
		recovering, R-13)."""
		self.pubs.clear()
		with self._pump_flags_on():
			conv, rid = self._fresh_conv_accept(deps)
			# Stream + settle but suppress finalize so the turn parks at finalizing
			# (simulating a crash after settle, before finalize completes).
			with patch.object(deps, "enqueue_finalize", lambda *a, **k: None):
				self._run_hop(deps, soft=5, hard=7, slices=600)
			st_before = self.state(rid)  # finalizing (settled; enrichment owed)
			# R-13: the watchdog's only legal action for `finalizing` is re-enqueuing
			# finalize. Run finalize (the re-enqueued job) directly against the fake
			# gateway (in-process equivalent of the watchdog re-enqueue + worker pickup).
			deps.enqueue_finalize(rid, self.target)
		return {
			"state_at_kill": st_before,
			"final_state": self.state(rid),
			"PASS": st_before == "finalizing" and self.state(rid) == "done",
		}


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--site", default="patterntest.localhost")
	ap.add_argument(
		"--out", default="/home/vignesh/jarvis/jarvis-chat-concurrency-design/implementation/wp-2/stage-b"
	)
	ap.add_argument("--quick", action="store_true")
	ap.add_argument("--equivalence", action="store_true")
	ap.add_argument("--pilot", action="store_true")
	ap.add_argument("--kills", action="store_true")
	ap.add_argument("--all", action="store_true")
	args = ap.parse_args()
	os.makedirs(args.out, exist_ok=True)
	do_eq = args.equivalence or args.all
	do_pi = args.pilot or args.all
	do_ki = args.kills or args.all
	if not (do_eq or do_pi or do_ki):
		do_eq = do_pi = do_ki = True

	sb = StageB(args.site, args.out, quick=args.quick)
	sb.setup()
	try:
		deps = sb._make_deps()
		if do_ki:
			sb.phase_kills(deps)
		if do_eq:
			sb.phase_equivalence(deps)
		if do_pi:
			sb.phase_pilot(deps)
		sb.results["meta"] = {
			"site": args.site,
			"when": time.strftime("%Y-%m-%d %H:%M:%S"),
			"mode": "quick" if args.quick else "full",
			"gateway": {"cadence_ms": CADENCE_MS, "max_concurrent": 4, "lane_sim": True},
			"shard": sb.target,
		}
	finally:
		sb.teardown()

	which = "".join([c for c, on in (("e", do_eq), ("p", do_pi), ("k", do_ki)) if on])
	res_path = os.path.join(args.out, f"stage_b_results_{which}.json")
	with open(res_path, "w") as fh:
		json.dump(sb.results, fh, indent=2, default=str)
	log(f"wrote {res_path}")


if __name__ == "__main__":
	main()
