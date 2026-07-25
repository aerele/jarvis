// Real executable client-fence test (CDX-12 / CDX-3). Plain node built-ins (node:test +
// node:assert) — no external framework. Run directly (`node --test eventFence.test.js`) or
// via the python suite: jarvis/tests/test_event_fence_client.py subprocess-runs this so
// codex's 5-step client walk lives in the suite forever. `node --test` exits non-zero on
// any failed assertion, which the python runner asserts.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fenceKey, fenceReject, fenceAccept } from "./eventFence.js";

// One "run" through the fence: reject-check then accept (mirrors ChatView's handlers,
// which call pumpFenceReject(p, isTerminal) then, if not rejected, pumpFenceAccept(...)).
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

test("fenceKey: run_id, then turn_id, else null", () => {
	assert.equal(fenceKey({ run_id: "R" }), "R");
	assert.equal(fenceKey({ turn_id: "T" }), "T");
	assert.equal(fenceKey({}), null);
});

// ── Codex's required 5-step client walk (CDX-12 P0) ─────────────────────────────
test("CDX-12: delta -> FIRST terminal accepted -> repeat terminal rejected -> stale E-1 rejected -> E+1 recovery allowed", () => {
	const fence = {};
	// (1) accept delta (E,N)
	assert.equal(feed(fence, ev(5, 10), false), false, "1. delta (E=5,N=10) accepted");
	// (2) accept the FIRST terminal (E,N) — shares the delta watermark seq; the P0 fix
	assert.equal(feed(fence, ev(5, 10), true), false, "2. FIRST terminal (E=5,N=10) accepted");
	assert.equal(fence.R.terminated, 5, "terminal marked the run terminated at epoch 5");
	// (3) reject the identical SECOND terminal (finalize backstop re-publish) — one-shot
	assert.equal(feed(fence, ev(5, 10), true), true, "3. identical second terminal rejected");
	// (4) reject a stale tool/delta from E-1
	assert.equal(feed(fence, ev(4, 99), false), true, "4. stale E-1 tool/delta rejected");
	assert.equal(feed(fence, ev(4, 99), true), true, "4b. stale E-1 terminal rejected too");
	// (5) allow a legitimate E+1 recovered stream + terminal
	assert.equal(feed(fence, ev(6, 1), false), false, "5a. E+1 recovered delta accepted");
	assert.equal(feed(fence, ev(6, 1), true), false, "5b. E+1 recovered terminal accepted");
	assert.equal(fence.R.terminated, 6, "recovery terminal advanced terminated to epoch 6");
});

// ── The recovered-delta-then-first-terminal walk codex named (was_recovered) ─────
test("CDX-12: after a terminal, a recovered delta then its FIRST equal-seq terminal are both accepted", () => {
	const fence = {};
	feed(fence, ev(3, 7), false); // delta
	feed(fence, ev(3, 7), true); // first terminal at E=3
	// Recovery re-streams at E+1; the recovered terminal shares the recovered delta's seq.
	assert.equal(feed(fence, ev(4, 7), false), false, "recovered delta (E+1, same seq) accepted");
	assert.equal(
		feed(fence, ev(4, 7), true),
		false,
		"recovered FIRST terminal (E+1, equal seq) accepted — NOT bounced as a duplicate"
	);
	// A repeat of the recovered terminal is one-shot rejected.
	assert.equal(feed(fence, ev(4, 7), true), true, "repeat recovered terminal rejected");
});

// ── Guard rails: the fix must not weaken the duplicate/stale rejections ──────────
test("CDX-12: a terminal STRICTLY BELOW the watermark is still stale (rejected)", () => {
	const fence = {};
	feed(fence, ev(2, 10), false); // delta watermark N=10
	feed(fence, ev(2, 12), false); // watermark advances to 12
	assert.equal(feed(fence, ev(2, 8), true), true, "terminal at seq 8 < watermark 12 rejected");
	assert.equal(
		feed(fence, ev(2, 12), true),
		false,
		"terminal at the watermark accepted (first)"
	);
});

test("CDX-3: a duplicate/older NON-terminal at the same epoch is rejected; a higher seq is accepted", () => {
	const fence = {};
	feed(fence, ev(1, 5), false);
	assert.equal(feed(fence, ev(1, 5), false), true, "equal-seq duplicate delta rejected");
	assert.equal(feed(fence, ev(1, 4), false), true, "lower-seq delta rejected");
	assert.equal(feed(fence, ev(1, 6), false), false, "higher-seq delta accepted");
});

test("CDX-3: legacy events (no pump_epoch) bypass the fence entirely", () => {
	const fence = {};
	assert.equal(feed(fence, { run_id: "R" }, false), false, "epoch-less delta bypasses");
	assert.equal(feed(fence, { run_id: "R" }, true), false, "epoch-less terminal bypasses");
	assert.deepEqual(fence, {}, "no fence entry created for legacy events");
});

test("CDX-12: a strictly HIGHER-epoch terminal supersedes an earlier terminal", () => {
	const fence = {};
	feed(fence, ev(1, 3), true); // terminal at E=1
	assert.equal(feed(fence, ev(2, 1), true), false, "higher-epoch terminal (E=2) supersedes");
	assert.equal(fence.R.terminated, 2);
});

// ── F3: terminal acceptance is the signal ChatView uses to tear down the activity
//    block. Prove the FIRST terminal is ACCEPTED (so the caller runs its cleanup) and
//    that the fence exposes `terminated` for the defensive resync guard. ────────────
test("F3: the first terminal is accepted AND leaves a durable `terminated` marker for teardown", () => {
	const fence = {};
	feed(fence, ev(9, 42), false); // delta streamed
	const bounced = feed(fence, ev(9, 42), true); // the authoritative terminal
	assert.equal(
		bounced,
		false,
		"terminal accepted -> run:end handler runs -> activity block torn down"
	);
	assert.notEqual(
		fence.R.terminated,
		null,
		"terminated set -> tearDownActivityIfSettled() can collapse a missed run:end"
	);
});
