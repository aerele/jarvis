// Real executable test for the chunked-voice ordering/retry/retention state
// machine (voiceChunkQueue.js). Plain node built-ins (node:test + node:assert) —
// no external framework — exactly like eventFence.test.js. Run directly
// (`node --test voiceChunkQueue.test.js`) or via the python suite:
// jarvis/tests/test_voice_chunk_queue_client.py subprocess-runs this so the client
// contract is enforced by every CI run. `node --test` exits non-zero on any failed
// assertion, which the python runner asserts.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createVoiceChunkQueue } from "./voiceChunkQueue.js";

// Drain all pending microtasks (the queue chains a few .then()s per transition).
const flush = () => new Promise((r) => setTimeout(r, 0));

// A controllable transcribe: every call parks until the test settles it by seq,
// while tracking how many requests are in flight at once (the concurrency proof).
function makeTranscriber() {
	const calls = []; // { seq, settled, settle(ok, val) }
	let inflight = 0;
	let maxInflight = 0;
	const fn = (clip) => {
		inflight += 1;
		if (inflight > maxInflight) maxInflight = inflight;
		return new Promise((resolve, reject) => {
			calls.push({
				seq: clip.seq,
				settled: false,
				settle(ok, val) {
					if (this.settled) return;
					this.settled = true;
					inflight -= 1;
					ok ? resolve(val) : reject(val);
				},
			});
		});
	};
	const _first = (seq) => calls.find((c) => c.seq === seq && !c.settled);
	return {
		fn,
		calls,
		get maxInflight() {
			return maxInflight;
		},
		get currentInflight() {
			return inflight;
		},
		resolve(seq, text) {
			const c = _first(seq);
			assert.ok(c, `no in-flight transcribe call for seq ${seq}`);
			c.settle(true, text);
		},
		reject(seq, err) {
			const c = _first(seq);
			assert.ok(c, `no in-flight transcribe call for seq ${seq}`);
			c.settle(false, err || new Error("boom"));
		},
		callCount(seq) {
			return calls.filter((c) => c.seq === seq).length;
		},
	};
}

// In-memory stand-in for the IndexedDB clip mirror; logs every put/delete so the
// mirror lifecycle (mirror on enqueue, clear on commit, RETAIN on fail) is testable.
function makeMirror() {
	const store = new Map();
	const log = [];
	return {
		store,
		log,
		put(clip) {
			store.set(clip.seq, clip);
			log.push(["put", clip.seq]);
		},
		delete(seq) {
			store.delete(seq);
			log.push(["delete", seq]);
		},
		all() {
			return Array.from(store.values());
		},
	};
}

const clip = (seq) => ({ seq, blob: `blob-${seq}`, durationS: 15 });

// ── (1) in-order append with OUT-OF-ORDER completions ───────────────────────────
test("commits strictly in seq order even when transcriptions resolve out of order", async () => {
	const tx = makeTranscriber();
	const commits = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		concurrency: 3,
		onCommit: (seq, text) => commits.push([seq, text]),
	});
	q.enqueue(clip(0));
	q.enqueue(clip(1));
	q.enqueue(clip(2));
	await flush();

	// seq 2 finishes FIRST — must NOT commit ahead of 0 and 1.
	tx.resolve(2, "two");
	await flush();
	assert.deepEqual(commits, [], "seq 2 held: its predecessors haven't resolved");

	tx.resolve(0, "zero");
	await flush();
	assert.deepEqual(
		commits,
		[[0, "zero"]],
		"seq 0 commits; seq 1 still pending blocks the cursor"
	);

	tx.resolve(1, "one");
	await flush();
	assert.deepEqual(
		commits,
		[
			[0, "zero"],
			[1, "one"],
			[2, "two"],
		],
		"seq 1 resolving drains 1 THEN the already-done 2 — contiguous, in order"
	);
});

// ── (2) BOUNDED concurrency (<= 2) ──────────────────────────────────────────────
test("never runs more than `concurrency` transcriptions at once", async () => {
	const tx = makeTranscriber();
	const commits = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		concurrency: 2,
		onCommit: (seq, text) => commits.push([seq, text]),
	});
	for (let i = 0; i < 5; i++) q.enqueue(clip(i));
	await flush();
	assert.equal(tx.currentInflight, 2, "only 2 of 5 start immediately");

	// Resolve the whole session one at a time; a new chunk starts only as a slot frees.
	tx.resolve(0, "c0");
	await flush();
	assert.ok(tx.currentInflight <= 2, "still <= 2 after a slot frees and the next starts");
	tx.resolve(1, "c1");
	await flush();
	tx.resolve(2, "c2");
	await flush();
	tx.resolve(3, "c3");
	await flush();
	tx.resolve(4, "c4");
	await flush();

	assert.equal(tx.maxInflight, 2, "the high-water mark of in-flight requests never exceeded 2");
	assert.deepEqual(
		commits.map((c) => c[0]),
		[0, 1, 2, 3, 4],
		"all five committed in order"
	);
});

// ── (3) per-chunk budget + ONE auto-retry, THEN failed ──────────────────────────
test("a chunk that rejects twice enters `failed`, fires onFail, and RETAINS its clip", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const failed = [];
	const commits = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		concurrency: 1,
		maxAttempts: 2,
		onCommit: (seq, text) => commits.push([seq, text]),
		onFail: (seq) => failed.push(seq),
	});
	q.enqueue(clip(0));
	await flush();
	assert.equal(tx.callCount(0), 1, "attempt 1 dispatched");

	tx.reject(0, new Error("timeout"));
	await flush();
	assert.equal(tx.callCount(0), 2, "attempt 2 (the ONE auto-retry) dispatched");
	assert.deepEqual(failed, [], "not failed yet — one retry remains");

	tx.reject(0, new Error("timeout again"));
	await flush();
	assert.deepEqual(failed, [0], "second rejection is terminal → failed + onFail");
	assert.equal(tx.callCount(0), 2, "no third attempt (budget is 1 + 1 retry)");
	assert.deepEqual(commits, [], "a failed chunk commits no text");

	const snap = q.snapshot();
	assert.equal(snap.failed.length, 1, "snapshot exposes the failed chunk for its chip");
	assert.equal(snap.failed[0].seq, 0);
	assert.ok(mirror.store.has(0), "the failed clip is RETAINED in the mirror (never lost)");
	assert.ok(
		!mirror.log.some((e) => e[0] === "delete" && e[1] === 0),
		"the failed clip's mirror record was never deleted"
	);
});

// ── (4) auto-retry that then SUCCEEDS commits normally ──────────────────────────
test("a chunk that rejects once then succeeds commits its text and clears its mirror", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const commits = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		concurrency: 1,
		onCommit: (seq, text) => commits.push([seq, text]),
	});
	q.enqueue(clip(0));
	await flush();
	tx.reject(0, new Error("transient"));
	await flush();
	tx.resolve(0, "recovered text");
	await flush();
	assert.deepEqual(commits, [[0, "recovered text"]], "the retry succeeded and committed");
	assert.ok(!mirror.store.has(0), "mirror cleared once the text landed");
});

// ── (5) IndexedDB mirror lifecycle: put on enqueue, delete on commit ────────────
test("mirror is written on enqueue and cleared exactly when the text is committed", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceChunkQueue({ transcribe: tx.fn, mirror, concurrency: 2 });
	q.enqueue(clip(0));
	q.enqueue(clip(1));
	await flush();
	assert.deepEqual(
		mirror.log,
		[
			["put", 0],
			["put", 1],
		],
		"both clips mirrored on enqueue, before any transcription resolves"
	);
	assert.ok(mirror.store.has(0) && mirror.store.has(1), "both un-transcribed clips are held");

	tx.resolve(0, "a");
	await flush();
	assert.ok(!mirror.store.has(0), "seq 0 cleared on commit");
	assert.ok(mirror.store.has(1), "seq 1 still mirrored (not yet committed)");
	tx.resolve(1, "b");
	await flush();
	assert.ok(
		!mirror.store.has(1),
		"seq 1 cleared on commit — mirror empty, nothing left to lose"
	);
	assert.equal(mirror.store.size, 0);
});

// ── (6) recovery drain: reload → re-enqueue mirrored clips → transcribe in order ─
test("recover() re-enqueues mirrored clips onto a fresh seq space and drains them in order", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const commits = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		concurrency: 2,
		onCommit: (seq, text) => commits.push([seq, text]),
	});
	// Simulate a reload: two un-transcribed clips survived at non-contiguous seqs.
	const seqs = q.recover([
		{ seq: 5, blob: "b5", durationS: 15 },
		{ seq: 3, blob: "b3", durationS: 15 },
	]);
	assert.deepEqual(
		seqs,
		[0, 1],
		"reindexed onto a fresh contiguous seq space, ascending by original seq"
	);
	await flush();
	assert.equal(
		tx.currentInflight,
		2,
		"both recovered clips dispatched under the concurrency budget"
	);

	// Resolve out of order — recovery still commits in spoken (original) order.
	tx.resolve(1, "spoken second");
	await flush();
	assert.deepEqual(commits, [], "seq 1 held behind seq 0");
	tx.resolve(0, "spoken first");
	await flush();
	assert.deepEqual(
		commits,
		[
			[0, "spoken first"],
			[1, "spoken second"],
		],
		"recovered clips committed in their original spoken order"
	);
	assert.equal(mirror.store.size, 0, "recovered clips cleared from the mirror once transcribed");
});

// ── (7) fail-through: a failed chunk never halts the session; its retry commits ──
test("a failed chunk does not block later chunks, and a manual retry re-commits it (never lost)", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const commits = [];
	const failed = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		concurrency: 3,
		maxAttempts: 2,
		onCommit: (seq, text) => commits.push([seq, text]),
		onFail: (seq) => failed.push(seq),
	});
	q.enqueue(clip(0));
	q.enqueue(clip(1));
	q.enqueue(clip(2));
	await flush();

	// seq 0 rejects (auto-retry), while 1 and 2 succeed and WAIT behind the gap.
	tx.reject(0, new Error("t1"));
	await flush();
	tx.resolve(1, "one");
	await flush();
	tx.resolve(2, "two");
	await flush();
	assert.deepEqual(
		commits,
		[],
		"1 and 2 are done but held: the cursor waits on the retrying gap"
	);

	// seq 0 rejects a second time → failed → fail-through releases 1 and 2.
	tx.reject(0, new Error("t2"));
	await flush();
	assert.deepEqual(failed, [0], "seq 0 failed");
	assert.deepEqual(
		commits,
		[
			[1, "one"],
			[2, "two"],
		],
		"fail-through: later chunks commit the instant the gap goes terminal — session not halted"
	);
	assert.ok(mirror.store.has(0), "failed clip retained for Retry/Download");

	// User hits Retry on the chip; it succeeds and is re-committed (at the end).
	q.retry(0);
	await flush();
	tx.resolve(0, "zero");
	await flush();
	assert.deepEqual(
		commits,
		[
			[1, "one"],
			[2, "two"],
			[0, "zero"],
		],
		"the resurrected failed chunk re-commits (appended at the end — documented) — audio never lost"
	);
	assert.ok(!mirror.store.has(0), "mirror cleared after the successful retry");
	assert.equal(q.hasUnfinished(), false, "nothing left pending/failed after the retry");
});

// ── (8) hasUnfinished / discard drive the unload guard ──────────────────────────
test("hasUnfinished tracks the unload guard; discard removes a clip from the source of truth", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceChunkQueue({ transcribe: tx.fn, mirror, concurrency: 1, maxAttempts: 1 });
	assert.equal(q.hasUnfinished(), false, "empty queue: nothing to lose");
	q.enqueue(clip(0));
	await flush();
	assert.equal(q.hasUnfinished(), true, "an in-flight clip arms the guard");

	tx.reject(0, new Error("dead")); // maxAttempts 1 → straight to failed
	await flush();
	assert.equal(
		q.hasUnfinished(),
		true,
		"a failed clip still arms the guard (audio not yet safe)"
	);

	// User downloads the clip then dismisses it: discard clears the guard.
	assert.ok(q.getClip(0), "getClip returns the retained blob for Download");
	q.discard(0);
	await flush();
	assert.equal(
		q.hasUnfinished(),
		false,
		"guard clears once the user disposes of the failed clip"
	);
	assert.ok(!mirror.store.has(0), "discard drops the mirror record too");
});

// ── (9) dispose(): late resolutions must not poke a dead composer ───────────────
test("after dispose(), a late transcribe resolution fires no onCommit", async () => {
	const tx = makeTranscriber();
	const commits = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		concurrency: 1,
		onCommit: (seq, text) => commits.push([seq, text]),
	});
	q.enqueue(clip(0));
	await flush();
	q.dispose(); // component unmounted mid-flight
	tx.resolve(0, "too late");
	await flush();
	assert.deepEqual(commits, [], "no commit after dispose — the dead composer is never touched");
});
