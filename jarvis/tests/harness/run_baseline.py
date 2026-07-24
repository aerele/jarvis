"""run_baseline — the orchestrated LEGACY baseline (Stage A).

Runs the current worker-per-turn transport against the FakeGateway on a 2-worker
config and captures the C1/C3/C4/C5/C6 series + the trace corpus + the incident
scenario (10-send burst) BOTH with Phase-0 admission OFF and ON, so the
before/after Phase-0 story is on record before the pump exists.

Usage (from the bench):
    JARVIS_ALLOW_REAL_NETWORK_IN_TESTS=1 \\
      env/bin/python apps/jarvis/jarvis/tests/harness/run_baseline.py \\
      [--quick] [--full] [--site patterntest.localhost] [--out <dir>]

The env var is required because the jarvis test-network guard blocks even the
LOOPBACK socket to the fake gateway (127.0.0.1); the harness never touches a
real tenant. Nothing durable is mutated: the admission flag + cap are process-
only overrides, ensure_paired is stubbed process-wide, and the gateway URL is
passed straight to the transport (Jarvis Settings is never written). Teardown
restores everything and clears the harness's own rows.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
import uuid

os.environ.setdefault("JARVIS_ALLOW_REAL_NETWORK_IN_TESTS", "1")

from jarvis.tests.harness import (
	CONV,
	DEFAULT_SITE,
	HARNESS_USER,
	MSG,
	TURN,
	bootstrap,
)
from jarvis.tests.harness import probes as P
from jarvis.tests.harness import transcripts as T
from jarvis.tests.harness import turn_runner as R
from jarvis.tests.harness.fake_gateway import FakeGateway
from jarvis.tests.harness.trace_recorder import TraceRecorder
from jarvis.tests.harness.turn_runner import TurnSpec

DESK_URL = "http://127.0.0.1:8000/api/method/ping"
DESK_HOST = "patterntest.localhost"


# --------------------------------------------------------------------------- #
# fixtures / setup / teardown
# --------------------------------------------------------------------------- #


class Harness:
	def __init__(self, site: str, out_dir: str, *, quick: bool = False):
		self.site = site
		self.out_dir = out_dir
		self.quick = quick
		self.frappe = None
		self._restore_stubs = None
		self.results: dict = {}
		self.caveats: list[str] = []
		self.durable_flag = None
		self.durable_cap = None
		self._seq_counter = 1000
		self._seq_lock = threading.Lock()

	# ---- lifecycle ----

	def setup(self):
		self.frappe = bootstrap(self.site)
		frappe = self.frappe
		if self.site != "patterntest.localhost":
			self.caveats.append(
				f"Ran on {self.site}, not patterntest.localhost — the site-wide-shard cleanup "
				"was NOT applied (guarded to patterntest); stray rows could perturb admission counts."
			)
		self._ensure_user()
		self._restore_stubs = R.install_stubs()
		from jarvis.chat import admission

		self.durable_flag = frappe.conf.get(admission.FLAG)
		self.durable_cap = frappe.conf.get("jarvis_site_max_inflight_turns")
		self._cleanup()

	def teardown(self):
		try:
			self._cleanup()
		finally:
			if self._restore_stubs:
				self._restore_stubs()

	def _ensure_user(self):
		frappe = self.frappe
		if frappe.db.exists("User", HARNESS_USER):
			return
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": HARNESS_USER,
				"first_name": "Harness",
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
		for name in frappe.get_all(CONV, filters={"owner": HARNESS_USER}, pluck="name"):
			frappe.db.delete(TURN, {"conversation": name})
			frappe.db.delete(MSG, {"conversation": name})
			frappe.delete_doc(CONV, name, ignore_permissions=True, force=True)
		if self.site == "patterntest.localhost":
			# site-wide shard baseline (dedicated test site only), mirrors WP-0 _cleanup
			from jarvis.chat import admission

			frappe.db.delete(TURN, {"state": ["in", ("queued", "dispatching")]})
			stale = frappe.utils.add_to_date(None, seconds=-(admission._INFLIGHT_FRESH_SECONDS + 600))
			frappe.db.sql(
				"""UPDATE `tabJarvis Chat Message` SET modified=%(old)s
                   WHERE role='assistant' AND streaming=1 AND recovering=0 AND modified > %(fresh)s""",
				{"old": stale, "fresh": admission._fresh_cutoff()},
			)
		frappe.db.commit()

	def reset_shard_baseline(self):
		"""Clear the site-wide admission shard of stray non-terminal Turn rows
		and age stray fresh-streaming placeholders out of the freshness window,
		so admission counts (dual-signal inflight) start clean for an
		admission-sensitive scenario. patterntest (dedicated dev site) only —
		this is exactly the WP-0 test _cleanup shard-baseline step. On any other
		site it is a no-op and a caveat is recorded."""
		if self.site != "patterntest.localhost":
			return
		from jarvis.chat import admission

		frappe = self.frappe
		frappe.db.delete(TURN, {"state": ["in", ("queued", "dispatching")]})
		stale = frappe.utils.add_to_date(None, seconds=-(admission._INFLIGHT_FRESH_SECONDS + 600))
		frappe.db.sql(
			"""UPDATE `tabJarvis Chat Message` SET modified=%(old)s
               WHERE role='assistant' AND streaming=1 AND recovering=0 AND modified > %(fresh)s""",
			{"old": stale, "fresh": admission._fresh_cutoff()},
		)
		frappe.db.commit()

	# ---- row helpers ----

	def mk_conv(self) -> str:
		frappe = self.frappe
		frappe.set_user(HARNESS_USER)
		doc = frappe.get_doc({"doctype": CONV, "title": "harness", "status": "Active"})
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		return doc.name

	def mk_seed(self, conv: str, seq: int | None = None) -> str:
		frappe = self.frappe
		if seq is None:
			with self._seq_lock:
				self._seq_counter += 1
				seq = self._seq_counter
		doc = frappe.get_doc(
			{"doctype": MSG, "conversation": conv, "seq": seq, "role": "user", "content": "hi"}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		return doc.name

	def warm_session(self, conv: str, gateway: FakeGateway):
		"""Pre-create the gateway session so the measured turn is WARM (skips
		session-create)."""
		from jarvis.chat.openclaw_client import OpenclawSession

		sess = OpenclawSession.connect(gateway.ws_url)
		try:
			sk = sess.create_session()
			self.frappe.db.set_value(CONV, conv, "session_key", sk)
			self.frappe.db.commit()
		finally:
			sess.close()

	# ---- run a synchronous wave on the pool ----

	def run_wave(self, pool, specs: list[TurnSpec], timeout=120) -> list:
		events = []
		for s in specs:
			ev = threading.Event()
			events.append((s, ev))
			pool.submit(s, ev)
		for s, ev in events:
			ev.wait(timeout)
		return [pool.result(s.run_id) for s, _ in events]

	def rid(self, tag: str) -> str:
		return f"{tag}-{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #


def scenario_concurrency(h: Harness, gateway: FakeGateway, *, transcript="success"):
	"""C1 (N=1 warm), C3 (flush-gap), C4 (dwell), C5-shape (N=4,6): first-token
	at 1/2/4/6 concurrent sends on a 2-worker pool."""
	levels = [1, 2, 4, 6]
	target = 20 if h.quick else 48
	rec = TraceRecorder(label="concurrency")
	R.set_active_recorder(rec)
	pool = R.WorkerPool(2, h.site, gateway, rec, flag_value=0).start()

	out = {}
	for N in levels:
		reps = max(4, math.ceil(target / N))
		# distinct warm convs, reused across reps
		convs = []
		for _ in range(N):
			c = h.mk_conv()
			h.mk_seed(c)
			h.warm_session(c, gateway)
			convs.append(c)
		results = []
		for _ in range(reps):
			specs = [
				TurnSpec(
					run_id=h.rid(f"conc{N}"),
					conversation_id=c,
					seed_message=h.mk_seed(c),
					transcript=transcript,
					soft_deadline_s=30,
				)
				for i, c in enumerate(convs)
			]
			results.extend(r for r in h.run_wave(pool, specs) if r is not None)
		run_ids = [r.run_id for r in results]
		out[f"N={N}"] = {
			"samples": len(results),
			"reps": reps,
			"first_token_from_submit_ms": P.summarize(P.first_token_from_submit(results)),
			"first_token_from_send_ms": P.summarize(P.first_token_from_send(results)),
			"total_ms": P.summarize(P.total(results)),
			"flush_gap_ms_C3": P.summarize(P.all_flush_gaps(rec, run_ids)),
			"publish_gap_ms": P.summarize(P.all_publish_gaps(rec, run_ids)),
			"dwell_ms_C4": P.summarize(P.dwell_series(gateway, run_ids)),
			"terminals": _terminal_counts(results),
		}
	pool.stop()
	rec.attach_gateway(gateway)
	rec.dump(os.path.join(h.out_dir, "trace_concurrency.json"))
	return out


def scenario_burst_incident(h: Harness, gateway: FakeGateway):
	"""The incident: a 10-send burst on a 2-worker pool, measured BOTH with
	Phase-0 admission OFF (pure legacy) and ON (real accept_or_queue, cap 4).
	Reports first-token-from-request (the full user-perceived wait) for each,
	plus the paced 10-in-60s variant (C5 shape)."""
	n = 10
	reps = 2 if h.quick else 5
	from jarvis.chat import admission
	from jarvis.chat import api as chat_api

	def _one_burst_flag_off(rec, pool, paced_window_s=None):
		convs = [h.mk_conv() for _ in range(n)]
		for c in convs:
			h.mk_seed(c)
			h.warm_session(c, gateway)
		specs = []
		req_times = {}
		for i, c in enumerate(convs):
			rid = h.rid("burstoff")
			specs.append(
				TurnSpec(
					run_id=rid,
					conversation_id=c,
					seed_message=h.mk_seed(c),
					transcript="success",
					soft_deadline_s=30,
				)
			)
		events = []
		for i, s in enumerate(specs):
			req_times[s.run_id] = time.monotonic()
			ev = threading.Event()
			events.append((s, ev))
			pool.submit(s, ev)
			if paced_window_s:
				time.sleep(paced_window_s / n)
		for s, ev in events:
			ev.wait(120)
		results = [pool.result(s.run_id) for s, _ in events]
		return [r for r in results if r], req_times

	def _one_burst_flag_on(rec, pool):
		# real admission: accept_or_queue (cap 4) -> dispatch to pool; settle on
		# terminal promotes the next queued turn (also to the pool).
		h.reset_shard_baseline()  # clean cap-4 admission each rep
		frappe = h.frappe
		frappe.set_user(HARNESS_USER)
		frappe.local.conf[admission.FLAG] = 1
		frappe.local.conf["jarvis_site_max_inflight_turns"] = 4
		admission._ensure_control_row(admission.DEFAULT_RELAY_TARGET)

		run_tmpl: dict = {}
		req_times: dict = {}

		def pool_dispatch(kwargs, interactive=True):
			rid = kwargs["run_id"]
			spec = TurnSpec(
				run_id=rid,
				conversation_id=kwargs["conversation_id"],
				seed_message=kwargs["message_id"],
				transcript=run_tmpl.get(rid, "success"),
				turn_class="interactive" if interactive else "background",
				settle_on_terminal=True,
				soft_deadline_s=30,
			)
			pool.submit(spec)

		orig_dispatch = chat_api._dispatch_turn
		chat_api._dispatch_turn = pool_dispatch
		admit_count = queue_count = reject_count = 0
		try:
			convs = [h.mk_conv() for _ in range(n)]
			for c in convs:
				h.warm_session(c, gateway)
			for i, c in enumerate(convs):
				rid = h.rid("burston")
				run_tmpl[rid] = "success"
				seed = h.mk_seed(c)
				req_times[rid] = time.monotonic()
				res = admission.accept_or_queue(
					conversation=c,
					run_id=rid,
					seed_message=seed,
					turn_class="interactive",
					dispatch=(
						lambda kw={"run_id": rid, "conversation_id": c, "message_id": seed}: pool_dispatch(
							kw, interactive=True
						)
					),
				)
				if not res.get("ok"):
					reject_count += 1
				elif res.get("dispatched"):
					admit_count += 1
				else:
					queue_count += 1
			pool.drain(180)
		finally:
			chat_api._dispatch_turn = orig_dispatch
		results = [pool.result(rid) for rid in req_times if pool.result(rid)]
		return results, req_times, {"admitted": admit_count, "queued": queue_count, "rejected": reject_count}

	# --- FLAG OFF (near-simultaneous burst) ---
	h.reset_shard_baseline()
	rec_off = TraceRecorder(label="burst_flag_off")
	R.set_active_recorder(rec_off)
	pool_off = R.WorkerPool(2, h.site, gateway, rec_off, flag_value=0).start()
	off_ft_req, off_ft_send = [], []
	for _ in range(reps):
		results, req_times = _one_burst_flag_off(rec_off, pool_off)
		off_ft_req += [_from_req(r, req_times) for r in results]
		off_ft_send += [r.first_token_from_send_ms() for r in results]
	# paced 10-in-60s (C5 shape) — one pass
	paced_window = 6 if h.quick else 60
	paced_results, paced_req = _one_burst_flag_off(rec_off, pool_off, paced_window_s=paced_window)
	pool_off.stop()
	rec_off.attach_gateway(gateway)
	rec_off.dump(os.path.join(h.out_dir, "trace_burst_flag_off.json"))

	# --- FLAG ON (real admission cap 4) ---
	h.reset_shard_baseline()
	rec_on = TraceRecorder(label="burst_flag_on")
	R.set_active_recorder(rec_on)
	pool_on = R.WorkerPool(2, h.site, gateway, rec_on, flag_value=1).start()
	on_ft_req, on_admit = [], {"admitted": 0, "queued": 0, "rejected": 0}
	on_qwait = []
	for _ in range(reps):
		results, req_times, counts = _one_burst_flag_on(rec_on, pool_on)
		on_ft_req += [_from_req(r, req_times) for r in results]
		on_qwait += [r.queue_wait_ms() for r in results]
		for k in on_admit:
			on_admit[k] += counts[k]
	pool_on.stop()
	rec_on.attach_gateway(gateway)
	rec_on.dump(os.path.join(h.out_dir, "trace_burst_flag_on.json"))
	# restore process cap override
	h.frappe.local.conf.pop("jarvis_site_max_inflight_turns", None)
	h.frappe.local.conf[admission.FLAG] = h.durable_flag

	return {
		"n_per_burst": n,
		"reps": reps,
		"flag_off": {
			"first_token_from_request_ms": P.summarize(off_ft_req),
			"first_token_from_send_ms": P.summarize(off_ft_send),
			"samples": len(off_ft_req),
		},
		"flag_on_phase0": {
			"first_token_from_request_ms": P.summarize(on_ft_req),
			"pool_wait_ms": P.summarize(on_qwait),
			"admission_disposition_totals": on_admit,
			"samples": len(on_ft_req),
		},
		"paced_10_in_60s_C5": {
			"window_s": paced_window,
			"first_token_from_request_ms": P.summarize([_from_req(r, paced_req) for r in paced_results]),
			"samples": len(paced_results),
		},
	}


def scenario_confirmation_storm(h: Harness, gateway: FakeGateway):
	"""Confirmation-card storm: many confirm turns concurrently on 2 workers."""
	n = 6 if h.quick else 10
	reps = 2 if h.quick else 4
	rec = TraceRecorder(label="confirmation_storm")
	R.set_active_recorder(rec)
	pool = R.WorkerPool(2, h.site, gateway, rec, flag_value=0).start()
	convs = [h.mk_conv() for _ in range(n)]
	for c in convs:
		h.warm_session(c, gateway)
	results = []
	for _ in range(reps):
		specs = [
			TurnSpec(
				run_id=h.rid("confstorm"),
				conversation_id=c,
				seed_message=h.mk_seed(c),
				transcript="confirmation-card",
				soft_deadline_s=30,
			)
			for i, c in enumerate(convs)
		]
		results.extend(r for r in h.run_wave(pool, specs) if r is not None)
	pool.stop()
	rec.attach_gateway(gateway)
	rec.dump(os.path.join(h.out_dir, "trace_confirmation_storm.json"))
	run_ids = [r.run_id for r in results]
	return {
		"n_per_storm": n,
		"reps": reps,
		"samples": len(results),
		"first_token_from_submit_ms": P.summarize(P.first_token_from_submit(results)),
		"total_ms": P.summarize(P.total(results)),
		"flush_gap_ms_C3": P.summarize(P.all_flush_gaps(rec, run_ids)),
		"terminals": _terminal_counts(results),
	}


def scenario_canary_c6(h: Harness, gateway: FakeGateway):
	"""C6: real short+long RQ canaries + Desk HTTP every ~7s, idle baseline vs a
	sustained REAL RQ backlog (FloodPump) on jarvis_chat (chat-load model) +
	long (probe-validity: shows the canary CAN detect a backed-up queue). Wait is
	read from each job's own RQ enqueued_at/started_at timestamps (the running
	workers predate this module and cannot import harness code — see probes note).
	In-process tool-heavy turns run during load so the DB the Desk probe also
	uses is under real chat-shaped write pressure."""
	idle_s = 20 if h.quick else 56
	load_s = 20 if h.quick else 56
	cadence = 7.0

	canary = P.CanaryProbe(h.site, cadence_s=cadence, desk_url=DESK_URL, desk_host=DESK_HOST).start()

	# idle baseline window
	time.sleep(idle_s)
	with canary._lock:
		idle_ct = len(canary.samples)
		idle_samples = list(canary.samples)

	# load window: sustained RQ backlog + in-process chat-shaped turns
	rec = TraceRecorder(label="c6_load")
	R.set_active_recorder(rec)
	pool = R.WorkerPool(2, h.site, gateway, rec, flag_value=0).start()
	flood = P.FloodPump(
		h.site, ["jarvis_chat", "long"], per_burst=(30 if h.quick else 80), interval_s=2.0
	).start()
	convs = [h.mk_conv() for _ in range(4)]
	for c in convs:
		h.warm_session(c, gateway)
	t_end = time.time() + load_s
	while time.time() < t_end:
		specs = [
			TurnSpec(
				run_id=h.rid("c6turn"),
				conversation_id=c,
				seed_message=h.mk_seed(c),
				transcript="tool-heavy",
				soft_deadline_s=30,
			)
			for c in convs
		]
		h.run_wave(pool, specs, timeout=60)
	flood.stop()
	pool.stop()
	canary.stop()

	with canary._lock:
		all_samples = list(canary.samples)
	load_samples = all_samples[idle_ct:]

	def _agg(samples, key, sub="wait_ms"):
		if key == "desk":
			return P.summarize([s["desk"]["ms"] for s in samples if s["desk"].get("ok")])
		return P.summarize([s[key][sub] for s in samples])

	def _starved(samples):
		return sum(1 for s in samples if s["short"].get("starved") or s["long"].get("starved"))

	return {
		"idle_window_s": idle_s,
		"load_window_s": load_s,
		"canary_cadence_s": cadence,
		"flood_queues": ["jarvis_chat", "long"],
		"flood_jobs_total": flood.total,
		"wait_ms_derivation": "RQ job started_at - enqueued_at (worker-populated); no harness code runs in the worker",
		"idle": {
			"short_wait_ms": _agg(idle_samples, "short"),
			"long_wait_ms": _agg(idle_samples, "long"),
			"desk_ms": _agg(idle_samples, "desk"),
			"samples": len(idle_samples),
			"starved": _starved(idle_samples),
		},
		"under_load": {
			"short_wait_ms": _agg(load_samples, "short"),
			"long_wait_ms": _agg(load_samples, "long"),
			"desk_ms": _agg(load_samples, "desk"),
			"samples": len(load_samples),
			"starved": _starved(load_samples),
		},
	}


def scenario_transcript_smoke(h: Harness, gateway: FakeGateway):
	"""Correctness smoke: each of the 8 transcripts produces its expected
	terminal kind + a stop->visible-stop measurement (SUX-12)."""
	rec = TraceRecorder(label="transcript_smoke")
	R.set_active_recorder(rec)
	pool = R.WorkerPool(1, h.site, gateway, rec, flag_value=0).start()
	out = {}
	for name in T.NAMES:
		c = h.mk_conv()
		h.warm_session(c, gateway)
		seed = h.mk_seed(c)
		spec = TurnSpec(
			run_id=h.rid(f"smoke-{name}"),
			conversation_id=c,
			seed_message=seed,
			transcript=name,
			soft_deadline_s=15,
			ack_timeout_s=(0.5 if name == "ack-timeout" else None),
			overrides=({"ack_timeout_hold_ms": 1500} if name == "ack-timeout" else {}),
		)
		res = h.run_wave(pool, [spec])[0]
		out[name] = {
			"terminal": res.terminal if res else None,
			"relay_state": res.relay_state if res else None,
		}
	pool.stop()
	rec.attach_gateway(gateway)
	rec.dump(os.path.join(h.out_dir, "trace_transcript_smoke.json"))

	stop = P.measure_stop_visible(gateway)
	out["_stop_click_visible_stop_SUX12"] = stop
	return out


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _from_req(result, req_times):
	if result is None or result.t_first_frame is None:
		return None
	t_req = req_times.get(result.run_id)
	if t_req is None:
		return None
	return (result.t_first_frame - t_req) * 1000.0


def _terminal_counts(results) -> dict:
	from collections import Counter

	return dict(Counter(r.terminal for r in results))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--site", default=DEFAULT_SITE)
	ap.add_argument("--out", default=None)
	ap.add_argument("--quick", action="store_true", help="small N for iteration")
	ap.add_argument("--full", action="store_true", help="recorded-baseline N")
	ap.add_argument("--only", default=None, help="comma list: concurrency,burst,storm,canary,smoke")
	args = ap.parse_args()

	out_dir = args.out or "/home/vignesh/jarvis/jarvis-chat-concurrency-design/implementation/wp-2/baseline"
	os.makedirs(out_dir, exist_ok=True)
	quick = args.quick and not args.full

	h = Harness(args.site, out_dir, quick=quick)
	h.setup()
	gateway = FakeGateway(cadence_ms=25.0, max_concurrent=4, lane_sim=True).start()
	only = set(args.only.split(",")) if args.only else None
	started = time.time()
	try:
		h.results["meta"] = {
			"site": args.site,
			"started_wall": time.strftime("%Y-%m-%d %H:%M:%S"),
			"mode": "quick" if quick else "full",
			"pool_size": 2,
			"gateway": {"cadence_ms": 25.0, "max_concurrent": 4, "lane_sim": True},
			"durable_flag": h.durable_flag,
			"durable_cap": h.durable_cap,
			"batch_interval_ms": _batch_const("_ASSISTANT_BATCH_INTERVAL_MS"),
			"batch_size": _batch_const("_ASSISTANT_BATCH_SIZE"),
		}
		if not only or "smoke" in only:
			h.results["transcript_smoke"] = scenario_transcript_smoke(h, gateway)
		if not only or "concurrency" in only:
			h.results["concurrency"] = scenario_concurrency(h, gateway)
		if not only or "burst" in only:
			h.results["burst_incident"] = scenario_burst_incident(h, gateway)
		if not only or "storm" in only:
			h.results["confirmation_storm"] = scenario_confirmation_storm(h, gateway)
		if not only or "canary" in only:
			h.results["canary_c6"] = scenario_canary_c6(h, gateway)
		h.results["meta"]["gateway_max_observed_main_lane"] = gateway.max_observed_main()
		h.results["meta"]["elapsed_s"] = round(time.time() - started, 1)
		h.results["caveats"] = h.caveats
	finally:
		gateway.stop()
		h.teardown()

	res_path = os.path.join(out_dir, "baseline_results.json")
	with open(res_path, "w") as fh:
		json.dump(h.results, fh, indent=2, default=str)
	print("wrote", res_path, f"({h.results['meta'].get('elapsed_s')}s)")
	print(json.dumps(h.results, indent=2, default=str)[:4000])


def _batch_const(name):
	from jarvis.chat import turn_handler

	return getattr(turn_handler, name, None)


if __name__ == "__main__":
	main()
