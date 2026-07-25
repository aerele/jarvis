"""run_pump_e2e — the owed real-socket Relay-Pump end-to-end (WP-1e / WP-1b debt).

WP-1b's mux tests drive an IN-PROCESS transport double (the suite's hard network
guard refuses even a loopback socket). This script pays the ruled debt: ONE
deliberate real-socket run of the WHOLE pump against the harness ``FakeGateway``
listening on a real ``127.0.0.1`` socket, covering

  1. one full pump-mode turn: accept(pump) -> promote -> REAL prepare (session
     bootstrap over a real pooled socket) -> pump dispatch/stream/terminal over a
     REAL mux socket -> REAL settlement -> REAL finalize -> done; and
  2. a WS-drop MID-STREAM -> hop ends -> reconnect (a fresh hop, fresh socket) ->
     snapshot recovery: reconcile sees the gateway session no longer active, pulls
     the durable answer via ``sessions.get``, settles to final (was_recovered), and
     finalizes to done.

Run it as a SCRIPT outside the unit suite (like Stage A's run_baseline), with the
sanctioned escape hatch (it never touches a real tenant — the gateway is a local
fake, Jarvis Settings is never written, the pump make_mux + pool checkout are
redirected to the fake by injection, and all rows are the harness user's, cleaned
on teardown):

    JARVIS_ALLOW_REAL_NETWORK_IN_TESTS=1 \\
      env/bin/python apps/jarvis/jarvis/tests/harness/run_pump_e2e.py \\
      [--site patterntest.localhost] [--out <dir>]

Evidence (the transcript + a machine-readable summary) is written to
``implementation/wp-1/e2e-evidence/`` in the design repo.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("JARVIS_ALLOW_REAL_NETWORK_IN_TESTS", "1")

from jarvis.tests.harness import CONV, MSG, TURN, bootstrap
from jarvis.tests.harness.fake_gateway import FakeGateway

HARNESS_USER = "jarvis-pumpe2e@example.com"
EFFECT = "Jarvis Turn Effect"
SESSION = "Jarvis Chat Session"
PUMP = "Jarvis Relay Pump"

_LINES: list[str] = []


def log(msg: str) -> None:
	line = f"[{time.strftime('%H:%M:%S')}] {msg}"
	_LINES.append(line)
	print(line, flush=True)


class E2E:
	def __init__(self, site: str, out_dir: str):
		self.site = site
		self.out_dir = out_dir
		self.frappe = None
		self.gateway: FakeGateway | None = None
		self.target = f"pmpe2e_{int(time.time())}"
		self.pubs: list[dict] = []
		self.results: dict = {"scenarios": {}, "ok": False}
		self._pool_sess = None

	# ---- lifecycle ----

	def setup(self):
		self.frappe = bootstrap(self.site)
		# Stub device pairing (ensure_paired): OpenclawSession.connect otherwise pairs
		# via the admin, which patterntest is not onboarded for. Same stub run_baseline
		# uses; it never touches a real tenant. (It also rebinds worker.publish_to_user,
		# which is independent of turn_state.publish_to_user that we capture below.)
		from jarvis.tests.harness import turn_runner as R

		self._restore_stubs = R.install_stubs()
		self._ensure_user()
		self._cleanup()
		self.frappe.set_user(HARNESS_USER)
		from jarvis.chat import turn_state as ts

		ts._ensure_control_row(self.target)
		self.frappe.db.commit()
		self.gateway = FakeGateway(cadence_ms=15.0, max_concurrent=4, lane_sim=True).start()
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
				"first_name": "PumpE2E",
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

	# ---- row helpers ----

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

	def state(self, rid) -> str:
		return self.frappe.db.get_value(TURN, rid, "state")

	def val(self, rid, field):
		return self.frappe.db.get_value(TURN, rid, field)

	def pub_kinds(self) -> list[str]:
		return [p.get("kind") for p in self.pubs]

	# ---- real-socket deps + patches ----

	@contextmanager
	def _pool_to_gateway(self):
		"""Redirect the openclaw pool checkout to a REAL OpenclawSession on the fake
		gateway (a real socket) — prepare's session bootstrap + finalize's usage poll
		then create/reference the session ON the gateway, so sessions.list reflects it."""
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
		"""Stub the enrichment BOUNDARIES (external / RQ-enqueuing effects) so the
		finalize LEDGER runs for real while the transport stays the thing under test."""
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
		# The e2e drives each hop MANUALLY (run_pump_hop below), so the successor a
		# transport-exit / handoff enqueues must NOT go to a real RQ worker — no-op it.
		# CDX-1 succession is exercised by the REAL atomic lease transfer (which
		# force-releases the lease), NOT by a manual lease expiry (see hop1->hop2).
		deps.enqueue_pump_job = lambda **kw: None
		# invoke_settlement / snapshot / apply_tool keep their real defaults.
		return deps

	@contextmanager
	def _pump_flags_on(self):
		from jarvis.chat import admission, pump
		from jarvis.chat import turn_state as ts

		self._orig_pub = ts.publish_to_user

		def _cap(user, payload):
			self.pubs.append(payload)

		with (
			patch.object(pump, "pump_mode_active", return_value=True),
			patch.object(pump, "pump_configured", return_value=True),
			patch.object(pump, "pump_draining", return_value=False),
			patch.object(admission, "relay_target_id", lambda conversation=None: self.target),
			patch.object(pump, "ensure_pump", lambda *a, **k: {"enqueued": False}),
			patch.object(pump, "lpush_wake", lambda *a, **k: None),
			patch.object(ts, "publish_to_user", _cap),
		):
			yield

	def _accept(self, conv, rid) -> dict:
		from jarvis.chat import admission

		seed = self.mk_seed(conv)
		return admission.accept_or_queue(
			conversation=conv, run_id=rid, seed_message=seed, dispatch=lambda: None
		)

	# ---- scenarios ----

	def scenario_full_turn(self, deps):
		from jarvis.chat import pump

		log("SCENARIO 1 — one full pump-mode turn over a real socket")
		conv = self.mk_conv()
		rid = "e2e_full"
		with self._pump_flags_on():
			adm = self._accept(conv, rid)
			assert adm.get("pump"), f"expected pump accept, got {adm}"
			assert self.state(rid) == "queued", self.state(rid)
			log(f"  accepted: pump={adm.get('pump')} state={self.state(rid)}")
			# One real hop drives promote(prepare)->dispatch->stream->terminal->settle->finalize.
			out = pump.run_pump_hop(
				self.target, deps=deps, soft_budget_s=20, hard_deadline_s=25, max_slices=400
			)
		log(f"  hop exit={out.get('exit')} epoch={out.get('epoch')}")
		st = self.state(rid)
		amsg = self.val(rid, "assistant_message")
		content = self.frappe.db.get_value(MSG, amsg, "content") if amsg else None
		kinds = self.pub_kinds()
		res = {
			"final_state": st,
			"gateway_run_id": self.val(rid, "gateway_run_id"),
			"answer_len": len(content or ""),
			"answer_head": (content or "")[:80],
			"publish_kinds": sorted(set(kinds)),
			"delta_publishes": kinds.count("assistant:delta"),
			"has_run_start": "run:start" in kinds,
			"has_assistant_delta": "assistant:delta" in kinds,
			"has_run_end": "run:end" in kinds,
			"has_message_enriched": "message:enriched" in kinds,
		}
		ok = (
			st == "done"
			and res["has_run_start"]
			and res["has_assistant_delta"]
			and res["has_run_end"]
			and res["has_message_enriched"]
			and res["answer_len"] > 0
		)
		res["PASS"] = ok
		log(f"  -> state={st} deltas={res['delta_publishes']} kinds={res['publish_kinds']} PASS={ok}")
		self.results["scenarios"]["full_turn"] = res
		return ok

	def scenario_ws_drop_recovery(self, deps):
		from jarvis.chat import pump

		log("SCENARIO 2 — WS-drop mid-stream + reconnect + snapshot recovery over real sockets")
		self.pubs.clear()
		conv = self.mk_conv()
		rid = "e2e_drop"
		with self._pump_flags_on():
			adm = self._accept(conv, rid)
			assert adm.get("pump"), adm
			# Arm the 'recovered' transcript: the stream is cut mid-way but the durable
			# transcript (sessions.get) still holds the complete answer.
			self.gateway.arm(rid, "recovered")
			log("  armed 'recovered' (drops mid-stream; durable history holds the full answer)")

			# HOP 1: dispatch + partial stream, then the gateway drops the socket. The
			# dead socket ends the hop (D5 §5-d: transport_closed), leaving the turn streaming.
			out1 = pump.run_pump_hop(
				self.target, deps=deps, soft_budget_s=20, hard_deadline_s=25, max_slices=400
			)
			st1 = self.state(rid)
			amsg = self.val(rid, "assistant_message")
			partial = self.frappe.db.get_value(MSG, amsg, "content") if amsg else ""
			log(f"  hop1 exit={out1.get('exit')} state={st1} partial_len={len(partial or '')}")

			# CDX-1: do NOT manually expire the lease here — that bypassed the
			# clean-handoff race and hid the stranded-turn defect. HOP 1 exited via
			# ``transport_closed`` (the socket died mid-stream), whose exit path now
			# performs the REAL epoch-guarded atomic lease RELEASE (force-expire +
			# hop_counter bump + successor enqueue). So the lease is already acquirable
			# and HOP 2 takes over through the genuine succession path. We only clear
			# the fast NO-OP redis mirror belt-and-suspenders (the manual run below
			# calls lease_acquire directly, which ignores the mirror anyway).
			pump._clear_lease_mirror(self.target)

			# HOP 2: reconnect (fresh lease, fresh socket). reconcile-on-start sees the
			# gateway session no longer active -> snapshot recovery via sessions.get.
			out2 = pump.run_pump_hop(
				self.target, deps=deps, soft_budget_s=20, hard_deadline_s=25, max_slices=400
			)
		st2 = self.state(rid)
		amsg = self.val(rid, "assistant_message")
		final = self.frappe.db.get_value(MSG, amsg, "content") if amsg else ""
		kinds = self.pub_kinds()
		# The end publish that carried the recovery flag.
		end = next((p for p in self.pubs if p.get("kind") == "run:end"), {})
		res = {
			"hop1_exit": out1.get("exit"),
			"hop1_state": st1,
			"partial_len": len(partial or ""),
			"hop2_exit": out2.get("exit"),
			"final_state": st2,
			"was_recovered_flag": int(self.val(rid, "was_recovered") or 0),
			"run_end_was_recovered": bool(end.get("was_recovered")),
			"recovered_answer_len": len(final or ""),
			"recovered_answer_head": (final or "")[:80],
			"publish_kinds": sorted(set(kinds)),
		}
		ok = (
			st2 == "done"
			and res["was_recovered_flag"] == 1
			and res["recovered_answer_len"] > res["partial_len"]
			and "message:enriched" in kinds
		)
		res["PASS"] = ok
		log(f"  -> hop1={st1} hop2={st2} was_recovered={res['was_recovered_flag']} PASS={ok}")
		self.results["scenarios"]["ws_drop_recovery"] = res
		return ok

	# ---- telemetry evidence ----

	def collect_telemetry_tail(self) -> list[str]:
		"""Tail the latency log for the pump C-series lines this run emitted."""
		try:
			path = self.frappe.utils.get_site_path("logs", "jarvis.chat.latency.log")
		except Exception:
			path = os.path.join(self.site, "logs", "jarvis.chat.latency.log")
		try:
			with open(path) as fh:
				lines = fh.readlines()
		except Exception:
			return []
		wanted = (
			"first_token_ms",
			"flush_gap_ms",
			"dwell_ms",
			"pump promote",
			"pump hop",
			"cards_open",
			"snapshot_recover",
		)
		tail = [ln.strip() for ln in lines[-400:] if any(w in ln for w in wanted)]
		return tail[-40:]


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--site", default="patterntest.localhost")
	ap.add_argument(
		"--out",
		default="/home/vignesh/jarvis/jarvis-chat-concurrency-design/implementation/wp-1/e2e-evidence",
	)
	args = ap.parse_args()
	os.makedirs(args.out, exist_ok=True)

	e = E2E(args.site, args.out)
	e.setup()
	ok1 = ok2 = False
	try:
		deps = e._make_deps()
		ok1 = e.scenario_full_turn(deps)
		ok2 = e.scenario_ws_drop_recovery(deps)
		e.results["telemetry_tail"] = e.collect_telemetry_tail()
	finally:
		e.teardown()

	e.results["ok"] = bool(ok1 and ok2)
	e.results["meta"] = {
		"site": args.site,
		"when": time.strftime("%Y-%m-%d %H:%M:%S"),
		"gateway": "harness FakeGateway on a real 127.0.0.1 socket",
		"transport": "REAL OpenclawSession + RelayMux over real sockets (pump mux + prepare/finalize pool)",
	}
	summary_path = os.path.join(args.out, "pump_e2e_results.json")
	with open(summary_path, "w") as fh:
		json.dump(e.results, fh, indent=2, default=str)
	transcript_path = os.path.join(args.out, "pump_e2e_transcript.txt")
	with open(transcript_path, "w") as fh:
		fh.write("\n".join(_LINES) + "\n\nRESULTS:\n" + json.dumps(e.results, indent=2, default=str) + "\n")
	log(f"VERDICT: {'PASS' if e.results['ok'] else 'FAIL'}  (full_turn={ok1} ws_drop_recovery={ok2})")
	log(f"wrote {summary_path} and {transcript_path}")
	raise SystemExit(0 if e.results["ok"] else 1)


if __name__ == "__main__":
	main()
