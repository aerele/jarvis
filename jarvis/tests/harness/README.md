# WP-2 chat-concurrency measurement harness (Stage A)

New-files-only measurement machinery for the owner's six production criteria
(C1–C6, `BUILD-DIRECTIVE.md §1`) + the differential-trace corpus. It measures
the **current (legacy, worker-per-turn)** chat transport against a deterministic
local WS gateway, so the Relay Pump (WP-1) has a clean baseline to beat.

Zero product-code edits. Everything lives under `jarvis/tests/harness/`.

## What's here

| file | role |
|---|---|
| `fake_gateway.py` | Real `websockets` server on `127.0.0.1:<port>` speaking the openclaw protocol subset (S2). Deterministic transcript playback; configurable token cadence, ack delay, ack-timeout, WS-drop, and a per-session-FIFO → global-lane (`maxConcurrent=4`) admission sim that produces the C4 dwell. Ack fires BEFORE lane admission (S2 (a)). |
| `transcripts.py` + `fixtures/transcripts/*.json` | The 8 scripted transcripts (success, tool-heavy, confirmation-card, overflow-compaction, abort, ack-timeout, ws-drop, recovered) as fixture data. `python -m ...transcripts` re-exports the JSON. |
| `trace_recorder.py` | Per-turn timestamped trace (job lifecycle + gateway ack/lane/first-frame/terminal + REAL realtime publishes + DB-write summaries) → JSON. Each run gets a timing-free `signature` = the Stage-B equivalence key. |
| `turn_runner.py` | The faithful legacy turn: REAL `OpenclawSession` transport → REAL `_AssistantContentBatcher` + `_handle_event` (real DB commits + real publish fan-out). `WorkerPool(size=2)` = the 2-background-worker starvation bed. |
| `probes.py` | Metric probes: first_token (from submit + from send), queue_wait, flush-gap (C3) + publish-gap, dwell (C4), the p50/p95/p99 summarizer, a REAL RQ canary (short+long every 10s) + Desk HTTP probe (C6), a real background flood, and stop-click→visible-stop (SUX-12). |
| `run_baseline.py` | The orchestrator: all scenarios, the flag ON/OFF incident via the REAL `accept_or_queue` chokepoint, machine-readable + human results. |
| `probe_real_gateway.py` | R-21 guard: read-only assertion of the protocol facts against the pinned openclaw 2026.6.8 image in a THROWAWAY `--network none --rm` container. |

## How to re-run everything (the pilot will)

All commands from the bench root `/home/vignesh/frappe-bench`. The
`JARVIS_ALLOW_REAL_NETWORK_IN_TESTS=1` env var is **required**: the jarvis
test-network guard blocks even the loopback socket to the fake gateway
(`127.0.0.1`). The harness only ever connects to that loopback fake — never a
real tenant.

### 1. The full legacy baseline (writes JSON + traces to `--out`)

```bash
JARVIS_ALLOW_REAL_NETWORK_IN_TESTS=1 \
  env/bin/python apps/jarvis/jarvis/tests/harness/run_baseline.py --full \
  --out /home/vignesh/jarvis/jarvis-chat-concurrency-design/implementation/wp-2/baseline
```

Flags: `--quick` (small N for iteration), `--full` (recorded-baseline N),
`--only concurrency,burst,storm,canary,smoke` (subset), `--site` (default
`patterntest.localhost`). Runtime ≈ 6–7 min full, ≈ 90 s quick-minus-canary.

Outputs in `--out`:
- `baseline_results.json` — every scenario's p50/p95/p99 + meta.
- `trace_*.json` — the per-turn trace corpus (Stage-B diff unit).
- `r21_real_gateway_probe.json` — the R-21 result.

### 2. The R-21 real-gateway guard (once per run; needs docker + the image)

```bash
env/bin/python apps/jarvis/jarvis/tests/harness/probe_real_gateway.py
```

### 3. Re-export the transcript fixtures

```bash
env/bin/python -c "from jarvis.tests.harness import transcripts as t; t.dump_fixtures()"
```

## Fidelity model (read before trusting a number)

- **Transport / relay / batching = REAL bench code** over a real socket. First-token,
  flush-gap (C3), dwell (C4), ack, terminal outcomes are all real.
- **The 2-worker pool is a SIMULATION** (a bounded thread executor running the real
  relay path), not real RQ/supervisor. It reproduces worker-per-turn starvation
  exactly, which is the point; but it does NOT model RQ scheduler latency or
  fork-per-job connect cost. See the baseline report's caveats.
- **Phase-0 admission (flag ON) is driven through the REAL `accept_or_queue`** — the
  admit/queue/reject decisions, cap-4, and promotion-on-terminal are genuine.
- **Connect cost is localhost-cheap** (~4 ms) vs a real 50–200 ms WAN connect; the
  harness measures the streaming/batching layer faithfully, not real connect.
- Nothing durable is mutated: admission flag + cap are process-only overrides,
  `ensure_paired` is stubbed process-wide, the gateway URL is passed straight to the
  transport (Jarvis Settings is never written). Teardown restores everything and
  clears the harness's own rows (+ the site-wide shard baseline on patterntest).
