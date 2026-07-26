import { test } from "node:test";
import assert from "node:assert/strict";
import { emptyStream, applyEvent, visibleMessages } from "./chat_stream.mjs";

// `tool` rows are the MOST common role in a real conversation (508 of 1201 on
// the dev site) and their content is null or "get_doc -> completed" — machine
// chatter that must never reach a text-only panel.
test("visibleMessages: drops tool rows", () => {
  const out = visibleMessages([
    { name: "1", role: "user", content: "hi" },
    { name: "2", role: "tool", content: "get_doc → completed" },
    { name: "3", role: "tool", content: null },
    { name: "4", role: "assistant", content: "hello" },
  ]);
  assert.deepEqual(
    out.map((m) => m.name),
    ["1", "4"]
  );
});

test("visibleMessages: drops empty and whitespace-only content", () => {
  const out = visibleMessages([
    { name: "1", role: "assistant", content: "" },
    { name: "2", role: "assistant", content: "   " },
    { name: "3", role: "assistant", content: null },
    { name: "4", role: "user", content: "real" },
  ]);
  assert.deepEqual(
    out.map((m) => m.name),
    ["4"]
  );
});

test("visibleMessages: preserves order and is safe on junk input", () => {
  const rows = [
    { name: "a", role: "user", content: "one" },
    { name: "b", role: "assistant", content: "two" },
  ];
  assert.deepEqual(
    visibleMessages(rows).map((m) => m.name),
    ["a", "b"]
  );
  assert.deepEqual(visibleMessages(null), []);
  assert.deepEqual(visibleMessages(undefined), []);
  assert.deepEqual(visibleMessages([null, undefined]), []);
});

test("emptyStream: starts idle", () => {
  const s = emptyStream();
  assert.equal(s.live, null);
  assert.equal(s.busy, false);
  assert.equal(s.error, "");
  assert.deepEqual(s.pending, []);
  assert.equal(s.reload, false);
});

test("run:start opens a live turn and clears busy", () => {
  const s = applyEvent(
    { ...emptyStream(), busy: true },
    { kind: "run:start", run_id: "r1", message_id: "m1" }
  );
  assert.equal(s.busy, false);
  assert.deepEqual(s.live, { runId: "r1", messageId: "m1", text: "" });
});

test("assistant:delta ASSIGNS cumulative text, never appends", () => {
  let s = applyEvent(emptyStream(), {
    kind: "run:start",
    run_id: "r1",
    message_id: "m1",
  });
  s = applyEvent(s, { kind: "assistant:delta", run_id: "r1", text: "Hello" });
  s = applyEvent(s, {
    kind: "assistant:delta",
    run_id: "r1",
    text: "Hello there",
  });
  assert.equal(s.live.text, "Hello there");
});

test("assistant:delta with no prior run:start still opens a live turn", () => {
  const s = applyEvent(emptyStream(), {
    kind: "assistant:delta",
    run_id: "r1",
    text: "Hi",
  });
  assert.equal(s.live.text, "Hi");
  assert.equal(s.live.runId, "r1");
});

test("assistant:delta keeps a known message id when a later frame omits it", () => {
  let s = applyEvent(emptyStream(), {
    kind: "run:start",
    run_id: "r1",
    message_id: "m1",
  });
  s = applyEvent(s, { kind: "assistant:delta", run_id: "r1", text: "a" });
  assert.equal(s.live.messageId, "m1");
});

test("run:end clears live and asks for a reload", () => {
  let s = applyEvent(emptyStream(), { kind: "run:start", run_id: "r1" });
  s = applyEvent(s, { kind: "run:end", run_id: "r1" });
  assert.equal(s.live, null);
  assert.equal(s.busy, false);
  assert.equal(s.reload, true);
});

test("run:error surfaces the message and reloads", () => {
  const s = applyEvent(emptyStream(), {
    kind: "run:error",
    error: "The agent is unreachable.",
  });
  assert.equal(s.error, "The agent is unreachable.");
  assert.equal(s.live, null);
  assert.equal(s.busy, false);
  assert.equal(s.reload, true);
});

test("run:error with no message falls back to a plain sentence", () => {
  const s = applyEvent(emptyStream(), { kind: "run:error" });
  assert.equal(s.error, "That turn failed.");
});

test("action:pending queues a confirmation, ignoring duplicate tokens", () => {
  let s = applyEvent(emptyStream(), {
    kind: "action:pending",
    token: "t1",
    tool: "create_doc",
    summary: "Create ToDo",
  });
  s = applyEvent(s, {
    kind: "action:pending",
    token: "t1",
    tool: "create_doc",
    summary: "Create ToDo",
  });
  assert.equal(s.pending.length, 1);
  assert.deepEqual(s.pending[0], {
    token: "t1",
    tool: "create_doc",
    summary: "Create ToDo",
  });
});

test("action:pending without a token is ignored", () => {
  const s = applyEvent(emptyStream(), {
    kind: "action:pending",
    summary: "no token",
  });
  assert.deepEqual(s.pending, []);
});

test("action:pending queues distinct tokens in arrival order", () => {
  let s = applyEvent(emptyStream(), {
    kind: "action:pending",
    token: "t1",
    summary: "first",
  });
  s = applyEvent(s, { kind: "action:pending", token: "t2", summary: "second" });
  assert.deepEqual(
    s.pending.map((p) => p.token),
    ["t1", "t2"]
  );
});

test("action:resolved drops the token", () => {
  let s = applyEvent(emptyStream(), {
    kind: "action:pending",
    token: "t1",
    summary: "x",
  });
  s = applyEvent(s, { kind: "action:resolved", token: "t1" });
  assert.deepEqual(s.pending, []);
});

test("conversation:renamed asks for a reload", () => {
  const s = applyEvent(emptyStream(), { kind: "conversation:renamed" });
  assert.equal(s.reload, true);
});

test("unknown kinds leave state untouched", () => {
  const before = emptyStream();
  const after = applyEvent(before, { kind: "something:new" });
  assert.deepEqual(after, before);
});

test("a missing or malformed payload is inert", () => {
  const before = emptyStream();
  assert.deepEqual(applyEvent(before, null), before);
  assert.deepEqual(applyEvent(before, undefined), before);
  assert.deepEqual(applyEvent(before, {}), before);
});

test("applyEvent does not mutate the input state", () => {
  const before = emptyStream();
  applyEvent(before, { kind: "run:start", run_id: "r1" });
  assert.equal(before.live, null);

  const withPending = applyEvent(before, {
    kind: "action:pending",
    token: "t1",
    summary: "x",
  });
  applyEvent(withPending, { kind: "action:resolved", token: "t1" });
  assert.equal(withPending.pending.length, 1);
});

test("live text survives an unrelated event", () => {
  let s = applyEvent(emptyStream(), {
    kind: "assistant:delta",
    run_id: "r1",
    text: "partial",
  });
  s = applyEvent(s, { kind: "action:pending", token: "t1", summary: "x" });
  assert.equal(s.live.text, "partial");
});

// ── JF-018: Relay-Pump epoch/seq fence ────────────────────────────────────────
// The panel consumes the same pump-fenced frames as the desktop SPA. The shared
// comparison ladder has its own exhaustive suite (../../shared/pump_fence.test.mjs
// — including a parity walk against desktop's copy); these prove the REDUCER wires
// it in: a straggler must not rewind the live text or re-close a settled turn.
const pumped = (kind, epoch, seq, extra = {}) => ({
  kind,
  run_id: "r1",
  pump_epoch: epoch,
  event_seq: seq,
  ...extra,
});

test("fence: a superseded pump's delta cannot rewind the live text", () => {
  let s = applyEvent(
    emptyStream(),
    pumped("run:start", 2, 1, { message_id: "m1" })
  );
  s = applyEvent(s, pumped("assistant:delta", 2, 2, { text: "Hello there" }));
  // Same run, older epoch, higher seq — the classic post-handoff straggler.
  s = applyEvent(s, pumped("assistant:delta", 1, 99, { text: "Hel" }));
  assert.equal(s.live.text, "Hello there");
});

test("fence: a replayed same-epoch delta is dropped", () => {
  let s = applyEvent(
    emptyStream(),
    pumped("assistant:delta", 3, 5, { text: "full answer" })
  );
  s = applyEvent(s, pumped("assistant:delta", 3, 4, { text: "full" }));
  s = applyEvent(s, pumped("assistant:delta", 3, 5, { text: "full" }));
  assert.equal(s.live.text, "full answer");
});

test("fence: the FIRST terminal settles the turn, the backstop repeat does not re-fire", () => {
  let s = applyEvent(
    emptyStream(),
    pumped("assistant:delta", 4, 7, { text: "done" })
  );
  // finalize reproduces the delta watermark's seq — this must still settle.
  s = applyEvent(s, pumped("run:end", 4, 7));
  assert.equal(s.live, null);
  assert.equal(s.reload, true);
  // Panel.vue clears `reload` the moment it acts on it; a re-published terminal
  // must not set it again and cause a second fetch.
  s = { ...s, reload: false };
  s = applyEvent(s, pumped("run:end", 4, 7));
  assert.equal(s.reload, false);
});

test("fence: a stale terminal cannot re-close a turn that has moved on", () => {
  let s = applyEvent(
    emptyStream(),
    pumped("assistant:delta", 5, 1, { text: "a" })
  );
  s = applyEvent(s, pumped("run:end", 5, 1));
  s = { ...s, reload: false };
  // Recovery re-streams at E+1; seq restarts low, which is legitimate and must flow
  // (the fence resets its watermark on an epoch bump).
  s = applyEvent(s, pumped("run:start", 6, 1, { message_id: "m2" }));
  s = applyEvent(
    s,
    pumped("assistant:delta", 6, 2, { text: "recovered answer" })
  );
  assert.equal(s.live.text, "recovered answer");
  // Now the losing pump's late terminal arrives. It must NOT wipe the live turn.
  s = applyEvent(s, pumped("run:end", 5, 9));
  assert.equal(s.live.text, "recovered answer", "stale run:end dropped");
  assert.equal(s.reload, false, "…and triggered no reload");
  s = applyEvent(s, pumped("run:error", 5, 9, { error: "boom" }));
  assert.equal(s.error, "", "stale run:error dropped");
  assert.equal(s.live.text, "recovered answer");
});

test("fence: a stale run:start cannot re-open a settled turn", () => {
  let s = applyEvent(
    emptyStream(),
    pumped("assistant:delta", 7, 3, { text: "final" })
  );
  s = applyEvent(s, pumped("run:end", 7, 3));
  s = { ...s, reload: false };
  s = applyEvent(s, pumped("run:start", 6, 1, { message_id: "ghost" }));
  assert.equal(s.live, null);
});

test("fence: approvals and renames bypass it (they are not pump-sequenced)", () => {
  let s = applyEvent(
    emptyStream(),
    pumped("assistant:delta", 2, 9, { text: "x" })
  );
  s = applyEvent(s, {
    kind: "action:pending",
    token: "t1",
    pump_epoch: 1,
    event_seq: 1,
  });
  assert.equal(s.pending.length, 1, "a parked write is never fenced away");
  s = applyEvent(s, {
    kind: "conversation:renamed",
    pump_epoch: 1,
    event_seq: 1,
  });
  assert.equal(s.reload, true);
});

test("fence: legacy frames with no pump_epoch behave exactly as before", () => {
  let s = applyEvent(emptyStream(), {
    kind: "assistant:delta",
    run_id: "r1",
    text: "one",
  });
  s = applyEvent(s, { kind: "assistant:delta", run_id: "r1", text: "two" });
  assert.equal(s.live.text, "two");
  assert.deepEqual(s.fence, {});
});

test("fence: state is copied, never mutated in place", () => {
  const before = applyEvent(
    emptyStream(),
    pumped("assistant:delta", 1, 1, { text: "a" })
  );
  const snapshot = JSON.parse(JSON.stringify(before.fence));
  applyEvent(before, pumped("assistant:delta", 1, 2, { text: "ab" }));
  assert.deepEqual(before.fence, snapshot);
});

test("fence: a fresh stream (new conversation) starts unfenced", () => {
  let s = applyEvent(emptyStream(), pumped("run:end", 9, 9));
  assert.notDeepEqual(s.fence, {});
  s = emptyStream();
  assert.deepEqual(s.fence, {});
  s = applyEvent(s, pumped("assistant:delta", 1, 1, { text: "new chat" }));
  assert.equal(s.live.text, "new chat");
});
