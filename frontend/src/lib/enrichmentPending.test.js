// Real executable tests for the bounded "Finishing…" affordance (enrichmentPending.js),
// plus source assertions fencing the ONE view that drives it. Plain node built-ins
// (node:test + node:assert), like eventFence.test.js / chatAsk.test.js. Run directly
// (`node --test enrichmentPending.test.js`), via `npm run test:node`, or via the python
// suite (jarvis/tests/test_enrichment_pending_client.py subprocess-runs it every CI run).
//
// What is being defended (jarvis#681, e2e finding F26). A chat turn rendered its answer,
// its tool count and its duration, and then showed "Finishing…" forever. The reply was
// complete; only the post-turn enrichment had stalled, because a background lane was
// still holding credentials the operator had just replaced. The affordance had exactly
// one thing that could ever clear it, the `message:enriched` realtime push, so a stalled
// or lost enrichment left a permanent claim that the answer was unfinished.
//
// The contract these tests pin down: the affordance is BOUNDED. It clears on
// `message:enriched` as before, and it ALSO clears on its own deadline, whatever the
// server is doing, so no server-side failure can produce a permanent "Finishing…".
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createEnrichmentPending, ENRICHMENT_PENDING_MAX_MS } from "./enrichmentPending.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const chatViewSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

// A controllable clock, so the deadline is proved rather than waited out.
function fakeClock() {
	let nextId = 1;
	let now = 0;
	const timers = new Map(); // id -> { at, fn }
	return {
		setTimer(fn, ms) {
			const id = nextId++;
			timers.set(id, { at: now + ms, fn });
			return id;
		},
		clearTimer(id) {
			timers.delete(id);
		},
		advance(ms) {
			now += ms;
			const due = [...timers.entries()]
				.filter(([, t]) => t.at <= now)
				.sort((a, b) => a[1].at - b[1].at);
			for (const [id, t] of due) {
				timers.delete(id);
				t.fn();
			}
		},
		get liveTimers() {
			return timers.size;
		},
	};
}

// Builds a tracker on the fake clock and records everything it emits.
function harness(maxMs = ENRICHMENT_PENDING_MAX_MS) {
	const clock = fakeClock();
	const changes = [];
	const expired = [];
	const tracker = createEnrichmentPending({
		maxMs,
		setTimer: clock.setTimer,
		clearTimer: clock.clearTimer,
		onChange: (set) => changes.push(set),
		onExpire: (id) => expired.push(id),
	});
	return { clock, changes, expired, tracker };
}

const latest = (changes) => changes[changes.length - 1];

// ---- the ordinary path is unchanged --------------------------------------

test("run:end marks the reply pending and message:enriched clears it", () => {
	const { tracker, changes, expired, clock } = harness();
	tracker.mark("msg-1");
	assert.equal(tracker.has("msg-1"), true);
	assert.deepEqual([...latest(changes)], ["msg-1"]);

	tracker.clear("msg-1");
	assert.equal(tracker.has("msg-1"), false, "the affordance goes away when enrichment lands");
	assert.equal(latest(changes).size, 0);
	assert.equal(clock.liveTimers, 0, "clearing cancels the deadline, no orphan timer");

	clock.advance(ENRICHMENT_PENDING_MAX_MS * 2);
	assert.deepEqual(expired, [], "a reply that enriched normally never expires afterwards");
});

test("onChange is handed a fresh Set every time, never the tracker's own", () => {
	// ChatView assigns this straight onto a ref; a mutated-in-place Set would not
	// re-render, which is how the affordance would go stale in the other direction.
	const { tracker, changes } = harness();
	tracker.mark("msg-1");
	tracker.mark("msg-2");
	assert.equal(changes.length, 2);
	assert.notEqual(changes[0], changes[1]);
	assert.equal(changes[0].size, 1, "the earlier snapshot is not mutated by the later mark");
	assert.equal(changes[1].size, 2);
});

// ---- the reported failure shape ------------------------------------------

test("a completed turn whose enrichment never lands stops claiming to be unfinished", () => {
	// jarvis#681 exactly: the turn SUCCEEDED (run:end with enrichment_pending, the answer
	// is on screen), and then the follow-up work died, so message:enriched never comes.
	const { tracker, changes, expired, clock } = harness();
	tracker.mark("msg-stuck");

	clock.advance(ENRICHMENT_PENDING_MAX_MS - 1);
	assert.equal(tracker.has("msg-stuck"), true, "still within the grace window");
	assert.deepEqual(expired, [], "nothing given up on yet");

	clock.advance(1);
	assert.equal(tracker.has("msg-stuck"), false, "the deadline clears it with no server help");
	assert.equal(latest(changes).size, 0);
	assert.deepEqual(expired, ["msg-stuck"], "the owner is told once so it can resync");
	assert.equal(clock.liveTimers, 0);
});

test("expiry fires once, even if the owner keeps the clock running", () => {
	const { tracker, expired, clock } = harness();
	tracker.mark("msg-stuck");
	clock.advance(ENRICHMENT_PENDING_MAX_MS * 5);
	assert.deepEqual(
		expired,
		["msg-stuck"],
		"no repeated resync storm on a permanently dead turn"
	);
});

test("a re-delivered terminal does not push the deadline out", () => {
	// The CDX-12 finalize backstop re-publishes run:end for a turn it believes is still
	// owed enrichment. If mark() restarted the clock, a server stuck in that loop would
	// hold the affordance up forever, which is the bug this module exists to prevent.
	const { tracker, expired, clock } = harness();
	assert.equal(tracker.mark("msg-1"), true);
	clock.advance(ENRICHMENT_PENDING_MAX_MS - 10);
	assert.equal(tracker.mark("msg-1"), false, "re-marking an already-pending reply is a no-op");
	clock.advance(10);
	assert.deepEqual(expired, ["msg-1"], "the clock still ran from the FIRST terminal");
});

test("deadlines are per reply and independent", () => {
	const { tracker, expired, clock } = harness(1000);
	tracker.mark("msg-early");
	clock.advance(400);
	tracker.mark("msg-late");

	clock.advance(600); // msg-early is now 1000ms old, msg-late only 600ms
	assert.deepEqual(expired, ["msg-early"]);
	assert.equal(tracker.has("msg-late"), true, "a later reply keeps its own full window");

	clock.advance(400);
	assert.deepEqual(expired, ["msg-early", "msg-late"]);
});

test("enrichment landing for one reply leaves another reply's affordance alone", () => {
	const { tracker, clock, expired } = harness(1000);
	tracker.mark("msg-1");
	tracker.mark("msg-2");
	tracker.clear("msg-1");
	assert.equal(tracker.size, 1);
	clock.advance(1000);
	assert.deepEqual(expired, ["msg-2"]);
});

// ---- teardown ------------------------------------------------------------

test("reset cancels every deadline so a torn-down view never resyncs", () => {
	const { tracker, changes, expired, clock } = harness();
	tracker.mark("msg-1");
	tracker.mark("msg-2");
	tracker.reset();
	assert.equal(tracker.size, 0);
	assert.equal(latest(changes).size, 0);
	assert.equal(clock.liveTimers, 0, "no timer survives to fire a reload into a dead component");
	clock.advance(ENRICHMENT_PENDING_MAX_MS * 2);
	assert.deepEqual(expired, []);
});

test("clear and mark ignore a missing message id", () => {
	const { tracker, changes } = harness();
	assert.equal(tracker.mark(null), false);
	assert.equal(tracker.mark(undefined), false);
	assert.equal(tracker.clear(null), false);
	assert.equal(tracker.clear("never-marked"), false);
	assert.equal(changes.length, 0, "a no-op never churns the render");
});

test("the bound is generous enough for a healthy slow finalize and short enough to be a bound", () => {
	// The slowest healthy enrichment is the usage effect's own bounded gateway poll
	// (three reads, 1.5s apart) plus short-queue latency, comfortably inside a minute.
	assert.ok(ENRICHMENT_PENDING_MAX_MS >= 60000, "must not clip a slow but healthy finalize");
	assert.ok(ENRICHMENT_PENDING_MAX_MS <= 300000, "must not read as permanent to a human");
});

// ---- source fences: ChatView must go through this module ------------------

test("ChatView drives the affordance through this module, not a raw Set", () => {
	assert.match(
		chatViewSrc,
		/import \{ createEnrichmentPending \} from "@\/lib\/enrichmentPending"/,
		"ChatView must import the bounded tracker"
	);
	assert.match(
		chatViewSrc,
		/enrichmentTracker\.mark\(p\.message_id\)/,
		"run:end must mark through the tracker so the deadline is armed"
	);
	assert.match(
		chatViewSrc,
		/enrichmentTracker\.clear\(p\.message_id\)/,
		"message:enriched must clear through the tracker so the deadline is disarmed"
	);
	// The old unbounded bookkeeping wrote the ref directly. If either line comes back,
	// an entry gets added or removed with no deadline attached and the bug returns.
	assert.ok(
		!/enrichmentPending\.value = new Set\(enrichmentPending\.value\)/.test(chatViewSrc),
		"no direct Set surgery on the ref: every mutation must carry a deadline"
	);
});
