// Executable contract test for the SHARED pump fence (JF-018). Plain node built-ins
// (node:test + node:assert) — no framework. Run directly
// (`node --test pump_fence.test.mjs`) or via the python suite:
// jarvis/tests/test_pump_fence_shared_client.py subprocess-runs this, so the fence the
// PWA and the Desk widget depend on is enforced by every CI run.
//
// The last test is the anti-drift guard: it replays an EXHAUSTIVE walk matrix through
// both this module and desktop's frontend/src/utils/eventFence.js and asserts the two
// make identical accept/drop decisions and reach identical state. If desktop's fence
// is ever changed without changing this one (or vice versa), that test fails.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  FENCED_KINDS,
  TERMINAL_KINDS,
  admitEvent,
  createFence,
  fenceAccept,
  fenceKey,
  fenceReject,
} from "./pump_fence.mjs";
import * as desktop from "../../../../frontend/src/utils/eventFence.js";

// One trip through the low-level pair: reject-check then accept — exactly what
// admitEvent() and desktop's ChatView handlers do. Returns TRUE when rejected.
function feed(fence, p, isTerminal) {
  const rejected = fenceReject(fence, p, isTerminal);
  if (!rejected) fenceAccept(fence, p, isTerminal);
  return rejected;
}
const ev = (epoch, seq, extra = {}) => ({
  run_id: "R",
  pump_epoch: epoch,
  event_seq: seq,
  ...extra,
});
const delta = (epoch, seq, extra = {}) =>
  ev(epoch, seq, { kind: "assistant:delta", ...extra });
const end = (epoch, seq, extra = {}) =>
  ev(epoch, seq, { kind: "run:end", ...extra });

test("createFence: a fresh fence is empty", () => {
  assert.deepEqual(createFence(), {});
});

test("fenceKey: run_id, then turn_id, else null", () => {
  assert.equal(fenceKey({ run_id: "R" }), "R");
  assert.equal(fenceKey({ turn_id: "T" }), "T");
  assert.equal(fenceKey({}), null);
});

// ── stale epoch ────────────────────────────────────────────────────────────────
test("stale epoch: a superseded writer's frame is dropped, the current epoch is kept", () => {
  const fence = createFence();
  assert.equal(
    admitEvent(fence, delta(5, 3)),
    true,
    "current-epoch delta applied"
  );
  assert.equal(
    admitEvent(fence, delta(4, 99)),
    false,
    "E-1 delta dropped even at a higher seq"
  );
  assert.equal(
    fence.R.epoch,
    5,
    "the lower epoch did not clobber the watermark"
  );
  assert.equal(fence.R.seq, 3, "…nor the seq watermark");
});

// ── stale seq ──────────────────────────────────────────────────────────────────
test("stale seq: a replayed/duplicate non-terminal at the same epoch is dropped", () => {
  const fence = createFence();
  admitEvent(fence, delta(1, 5));
  assert.equal(
    admitEvent(fence, delta(1, 5)),
    false,
    "equal-seq replay dropped"
  );
  assert.equal(
    admitEvent(fence, delta(1, 4)),
    false,
    "lower-seq straggler dropped"
  );
  assert.equal(
    admitEvent(fence, delta(1, 6)),
    true,
    "the next real frame is applied"
  );
});

// ── terminal latch ─────────────────────────────────────────────────────────────
test("terminal latch: the FIRST terminal is applied, the repeat and every later straggler are not", () => {
  const fence = createFence();
  assert.equal(admitEvent(fence, delta(5, 10)), true);
  // The finalize/settlement terminal legitimately reproduces the delta watermark's
  // seq — it must NOT be mistaken for a duplicate, or the surface never settles.
  assert.equal(
    admitEvent(fence, end(5, 10)),
    true,
    "first terminal at the watermark applied"
  );
  assert.equal(fence.R.terminated, 5);
  assert.equal(
    admitEvent(fence, end(5, 10)),
    false,
    "backstop re-publish dropped one-shot"
  );
  assert.equal(
    admitEvent(fence, delta(5, 11)),
    true,
    "same-epoch progress after a terminal still flows"
  );
  assert.equal(
    admitEvent(fence, delta(4, 99)),
    false,
    "post-terminal LOWER-epoch delta dropped"
  );
  assert.equal(
    admitEvent(fence, end(4, 99)),
    false,
    "post-terminal LOWER-epoch terminal dropped"
  );
  assert.equal(
    admitEvent(fence, {
      kind: "run:start",
      run_id: "R",
      pump_epoch: 4,
      event_seq: 1,
    }),
    false,
    "a stale run:start cannot re-open a settled turn"
  );
});

test("terminal latch: a terminal STRICTLY below the watermark is still stale", () => {
  const fence = createFence();
  admitEvent(fence, delta(2, 10));
  admitEvent(fence, delta(2, 12));
  assert.equal(
    admitEvent(fence, end(2, 8)),
    false,
    "seq 8 < watermark 12 dropped"
  );
  assert.equal(
    admitEvent(fence, end(2, 12)),
    true,
    "the terminal at the watermark applied"
  );
});

// ── epoch bump resets ──────────────────────────────────────────────────────────
test("epoch bump: a higher epoch RESETS the seq watermark and supersedes a terminal", () => {
  const fence = createFence();
  admitEvent(fence, delta(3, 90));
  admitEvent(fence, end(3, 90));
  assert.equal(fence.R.terminated, 3);
  // Recovery re-streams from seq 1 at E+1; those low seqs must not read as stale.
  assert.equal(
    admitEvent(fence, delta(4, 1)),
    true,
    "recovered delta at a LOWER seq applied"
  );
  assert.equal(fence.R.seq, 1, "the watermark reset with the epoch");
  assert.equal(
    admitEvent(fence, end(4, 1)),
    true,
    "recovered terminal applied"
  );
  assert.equal(fence.R.terminated, 4, "the higher-epoch terminal superseded");
  assert.equal(admitEvent(fence, end(4, 1)), false, "…and is itself one-shot");
});

// ── run scoping ────────────────────────────────────────────────────────────────
test("the fence is RUN-scoped: a settled run never gates a different run", () => {
  const fence = createFence();
  admitEvent(fence, {
    kind: "assistant:delta",
    run_id: "A",
    pump_epoch: 9,
    event_seq: 5,
  });
  admitEvent(fence, {
    kind: "run:end",
    run_id: "A",
    pump_epoch: 9,
    event_seq: 5,
  });
  assert.equal(
    admitEvent(fence, {
      kind: "assistant:delta",
      run_id: "B",
      pump_epoch: 1,
      event_seq: 1,
    }),
    true,
    "a fresh run at a lower epoch is untouched by run A's terminal"
  );
});

test("tool frames carry no message_id but ARE fenced (the CDX-3 hole)", () => {
  const fence = createFence();
  admitEvent(fence, {
    kind: "tool:start",
    run_id: "R",
    pump_epoch: 2,
    event_seq: 4,
  });
  assert.equal(
    admitEvent(fence, {
      kind: "tool:end",
      run_id: "R",
      pump_epoch: 1,
      event_seq: 99,
    }),
    false,
    "a superseded writer's tool:end is dropped"
  );
});

// ── bypasses ───────────────────────────────────────────────────────────────────
test("legacy frames (no pump_epoch) bypass the fence entirely", () => {
  const fence = createFence();
  assert.equal(
    admitEvent(fence, { kind: "assistant:delta", run_id: "R" }),
    true
  );
  assert.equal(admitEvent(fence, { kind: "run:end", run_id: "R" }), true);
  assert.deepEqual(fence, {}, "no fence entry created for legacy frames");
});

test("unfenced kinds pass through without touching fence state", () => {
  const fence = createFence();
  admitEvent(fence, delta(2, 5));
  const snapshot = { ...fence.R };
  for (const kind of [
    "action:pending",
    "action:resolved",
    "conversation:renamed",
    "canvas",
    "run:status",
    "queue:position",
    "turn:cancelled",
    "something:new",
  ]) {
    assert.equal(
      admitEvent(fence, { kind, run_id: "R", pump_epoch: 1, event_seq: 1 }),
      true,
      `${kind} must bypass the fence`
    );
  }
  assert.deepEqual(
    fence.R,
    snapshot,
    "an unfenced kind left no trace in the fence"
  );
});

test("a missing / kind-less payload is admitted and inert", () => {
  const fence = createFence();
  assert.equal(admitEvent(fence, null), true);
  assert.equal(admitEvent(fence, undefined), true);
  assert.equal(admitEvent(fence, {}), true);
  assert.deepEqual(fence, {});
});

test("the fenced/terminal kind sets are the ones desktop uses", () => {
  assert.deepEqual([...TERMINAL_KINDS].sort(), ["run:end", "run:error"]);
  assert.deepEqual([...FENCED_KINDS].sort(), [
    "assistant:delta",
    "run:end",
    "run:error",
    "run:recovering",
    "run:start",
    "tool:end",
    "tool:start",
  ]);
  for (const k of TERMINAL_KINDS)
    assert.ok(FENCED_KINDS.has(k), `${k} must also be fenced`);
});

// ── anti-drift: identical decisions to desktop's fence ─────────────────────────
test("PARITY: every walk decides identically in this module and desktop's eventFence.js", () => {
  // 12 distinct frames (2 epochs x 3 seqs x terminal?) replayed as every ordered
  // 3-frame walk = 1728 walks, 5184 decisions. Small enough to be exhaustive,
  // wide enough to catch any divergence in the comparison ladder.
  const frames = [];
  for (const epoch of [1, 2]) {
    for (const seq of [1, 2, 3]) {
      for (const isTerminal of [false, true])
        frames.push({ p: ev(epoch, seq), isTerminal });
    }
  }
  let walks = 0;
  let decisions = 0;
  for (const a of frames) {
    for (const b of frames) {
      for (const c of frames) {
        const mine = {};
        const theirs = {};
        for (const f of [a, b, c]) {
          const m = feed(mine, f.p, f.isTerminal);
          const t = desktop.fenceReject(theirs, f.p, f.isTerminal);
          if (!t) desktop.fenceAccept(theirs, f.p, f.isTerminal);
          assert.equal(
            m,
            t,
            `decision drift on epoch=${f.p.pump_epoch} seq=${f.p.event_seq} terminal=${f.isTerminal}`
          );
          decisions += 1;
        }
        assert.deepEqual(mine, theirs, "fence STATE drift after the walk");
        walks += 1;
      }
    }
  }
  assert.equal(walks, 1728);
  assert.equal(decisions, 5184);
});

test("PARITY: legacy (epoch-less) frames behave identically too", () => {
  const mine = {};
  const theirs = {};
  for (const isTerminal of [false, true]) {
    const p = { run_id: "R" };
    assert.equal(
      feed(mine, p, isTerminal),
      desktop.fenceReject(theirs, p, isTerminal)
    );
  }
  assert.deepEqual(mine, theirs);
});
