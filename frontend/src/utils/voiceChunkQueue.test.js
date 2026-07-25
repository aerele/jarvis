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

// ── (7) fail-through with an IN-PLACE gap: a failed chunk anchors a placeholder at its
//        ordered position; later chunks commit AFTER it; a retry REPLACES it in place
//        (VUX-2 — retried text lands where it belongs, not bolted onto the end). ──────
test("a failed chunk anchors an in-place gap; later chunks commit after it; retry replaces it in place", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const events = []; // ordered log of onGap / onCommit(replace) so placement is provable
	const failed = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		concurrency: 3,
		maxAttempts: 2,
		onCommit: (seq, text, clip, replace) =>
			events.push({ k: replace ? "replace" : "commit", seq, text }),
		onGap: (seq) => events.push({ k: "gap", seq }),
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
		events,
		[],
		"1 and 2 are done but held: the cursor waits on the retrying gap"
	);

	// seq 0 rejects a second time → failed → the cursor crosses it: gap FIRST, then 1,2.
	tx.reject(0, new Error("t2"));
	await flush();
	assert.deepEqual(failed, [0], "seq 0 failed");
	assert.deepEqual(
		events,
		[
			{ k: "gap", seq: 0 }, // in-place placeholder dropped at the gap's ordered spot
			{ k: "commit", seq: 1, text: "one" }, // then later chunks commit AFTER the gap
			{ k: "commit", seq: 2, text: "two" },
		],
		"fail-through drops the gap FIRST, then commits later chunks after it — order preserved"
	);
	assert.ok(mirror.store.has(0), "failed clip retained for Retry/Download");

	// User hits Retry; it succeeds and commits with replace=true so the glue swaps the
	// placeholder for the words IN PLACE (never at the end).
	q.retry(0);
	await flush();
	tx.resolve(0, "zero");
	await flush();
	assert.deepEqual(
		events[events.length - 1],
		{ k: "replace", seq: 0, text: "zero" },
		"the resurrected failed chunk commits with replace=true — the glue swaps its placeholder in place"
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

// ── (10) SINGLE seq authority: record → recover → record can't collide (VAR-1 P0) ──
test("the queue stamps every seq from one authority, so recover around live recording never drops a clip", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const commits = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		concurrency: 4,
		onCommit: (seq) => commits.push(seq),
	});
	// The recorder emits live clips WITHOUT a seq — the queue assigns it. Under the old
	// dual-counter design the recorder's next seq and recover()'s cursor were EQUAL at
	// rest, so a recover() grabbed the value the next live clip would use → silent drop.
	const a = q.enqueue({ blob: "live-a", durationS: 15 }); // -> 0
	const b = q.enqueue({ blob: "live-b", durationS: 15 }); // -> 1
	// A prior-session orphan whose STALE seq (1) collides with the recorder's counter.
	const [r] = q.recover([{ blob: "orphan", durationS: 15, seq: 1 }]); // -> 2 (fresh)
	const c = q.enqueue({ blob: "live-c", durationS: 15 }); // -> 3 (NOT 2)
	assert.deepEqual(
		[a, b, r, c],
		[0, 1, 2, 3],
		"four producers, four UNIQUE seqs from one authority"
	);
	assert.equal(
		q.snapshot().total,
		4,
		"all four admitted — none silently dropped by a seq collision"
	);
	await flush();
	tx.resolve(0, "a");
	tx.resolve(1, "b");
	tx.resolve(2, "orphan");
	tx.resolve(3, "c");
	await flush();
	assert.deepEqual(
		commits,
		[0, 1, 2, 3],
		"all four committed in order — the VAR-1 live-clip drop is impossible"
	);
});

// ── (11) onCommit passes the clip so the glue routes by conversationId (VUX-4/VAR-4) ──
test("onCommit carries the clip; live and recovered clips keep the conversation they were spoken in", async () => {
	const tx = makeTranscriber();
	const commits = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		concurrency: 2,
		onCommit: (seq, text, clip) =>
			commits.push({ seq, text, conv: clip && clip.conversationId }),
	});
	q.enqueue({ blob: "a", durationS: 15, conversationId: "chatA" });
	await flush();
	tx.resolve(0, "hi");
	await flush();
	assert.deepEqual(
		commits[0],
		{ seq: 0, text: "hi", conv: "chatA" },
		"a live clip's conversationId flows through onCommit for routing"
	);
	// A recovered clip carries its OWN conversation, not the one currently open.
	q.recover([{ blob: "b", durationS: 15, conversationId: "chatB", seq: 9 }]);
	await flush();
	tx.resolve(1, "yo");
	await flush();
	assert.equal(
		commits[1].conv,
		"chatB",
		"a recovered clip routes to the chat it was spoken in — never dumped into whatever is open"
	);
});

// The EXACT adapter ChatView wraps around voice.transcribeAudio (ChatView.vue
// _ensureVoiceSession): await the RESPONSE OBJECT, validate {ok,text}, hand back ONLY the
// string. This is the seam the earlier unit tests missed — they fed the queue bare STRINGS
// and never the real {ok,text,stt_ms,model} object, so the "[object Object]" commit slipped
// through review (Codex critical, finding 1).
function chatViewAdapter(transcribeAudio) {
	return async (clip, signal) => {
		const res = await transcribeAudio(clip.blob, { durationS: clip.durationS, signal });
		if (res && res.ok === true && typeof res.text === "string") return res.text;
		throw new Error((res && res.error) || "Transcription returned no usable text.");
	};
}

// ── (12) INTEGRATION with the REAL transcribeAudio {ok,text,...} shape (finding 1) ──────
test("integration: the real transcribeAudio response OBJECT commits its .text (not '[object Object]'), retains the mirror until sent, and retains a failed/missing-text clip", async () => {
	// A fake transcribeAudio that returns the SERVER's real object shape, keyed by clip blob.
	const responses = new Map(); // blob -> {ok,text,...}
	const transcribeAudio = async (blob) => responses.get(blob);
	const mirror = makeMirror();
	const commits = [];
	const q = createVoiceChunkQueue({
		transcribe: chatViewAdapter(transcribeAudio),
		mirror,
		retainUntilSent: true,
		concurrency: 2,
		maxAttempts: 1, // one shot per clip so a bad payload lands in `failed` immediately
		onCommit: (seq, text, clip) =>
			commits.push({ seq, text, conv: clip && clip.conversationId }),
	});

	// (a) a GOOD clip: the real {ok:true, text:"hello", stt_ms, model} object.
	responses.set("good", { ok: true, text: "hello", stt_ms: 42, model: "whisper-x" });
	q.enqueue({ blob: "good", durationS: 15, conversationId: "chatA" });
	await flush();
	assert.deepEqual(
		commits,
		[{ seq: 0, text: "hello", conv: "chatA" }],
		"the STRING 'hello' is committed — never the '[object Object]' the raw response would String()-coerce to"
	);
	assert.ok(
		mirror.store.has(0),
		"mirror RETAINED after commit (the draft is un-sent) — the audio is still recoverable"
	);

	// captureSent + acknowledge → the draft is durably sent, so the mirror + leave guard clear.
	q.acknowledge(q.captureSent("chatA"));
	await flush();
	assert.ok(!mirror.store.has(0), "mirror cleared once the containing draft is sent");
	assert.equal(q.hasUnfinished(), false, "nothing un-sent left after acknowledge");

	// (b) a FAILED payload: {ok:false} → the adapter throws → clip RETAINED as failed, no commit.
	responses.set("bad", { ok: false });
	const s1 = q.enqueue({ blob: "bad", durationS: 15, conversationId: "chatA" });
	await flush();
	assert.equal(commits.length, 1, "no commit for the ok:false payload");
	let snap = q.snapshot();
	assert.equal(snap.failed.length, 1, "the ok:false clip is a RETAINED failed clip (its chip)");
	assert.equal(snap.failed[0].seq, s1);
	assert.ok(mirror.store.has(s1), "the failed clip's audio is RETAINED (never lost)");
	assert.equal(q.hasUnfinished(), true, "the failed clip keeps the leave guard armed");

	// (c) a MISSING-TEXT payload ({ok:true} but no text) is ALSO a failure — retained, no commit.
	responses.set("notext", { ok: true, stt_ms: 5 });
	const s2 = q.enqueue({ blob: "notext", durationS: 15, conversationId: "chatA" });
	await flush();
	assert.equal(commits.length, 1, "still no new commit for the missing-text payload");
	assert.ok(
		q.snapshot().failed.some((f) => f.seq === s2),
		"the missing-text clip is retained as failed too — nothing committed, audio kept"
	);
});

// ── (13) belt: a non-string resolution is a FAILURE, never a String()-coerced commit ────
test("belt: a transcribe that resolves the raw object (the old bug) is treated as a failure — no '[object Object]' commit, audio retained", async () => {
	const mirror = makeMirror();
	const commits = [];
	const q = createVoiceChunkQueue({
		// The ORIGINAL bug: an adapter that hands back the whole {ok,text} object, not text.
		transcribe: async () => ({ ok: true, text: "hello" }),
		mirror,
		concurrency: 1,
		maxAttempts: 2,
		onCommit: (seq, text) => commits.push([seq, text]),
	});
	q.enqueue({ blob: "b0", durationS: 15 });
	// Drive the auto-retry to exhaustion (each attempt resolves an object → belt → reject).
	for (let i = 0; i < 6 && q.snapshot().failed.length === 0; i++) await flush();
	assert.deepEqual(commits, [], "a non-string resolution NEVER commits — no '[object Object]'");
	const snap = q.snapshot();
	assert.equal(snap.failed.length, 1, "it lands in `failed`, retained for Retry/Download");
	assert.ok(mirror.store.has(0), "the audio is RETAINED — never deleted behind garbage text");
});

// ── (14) discard leaves a tombstone the cursor drains ACROSS (finding 4) ────────────────
test("discarding a failed later seq does not wedge the cursor: an earlier inflight clip still lets later done transcripts commit", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const commits = [];
	const failed = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		concurrency: 3,
		maxAttempts: 1, // a single reject → straight to failed
		onCommit: (seq, text) => commits.push([seq, text]),
		onFail: (seq) => failed.push(seq),
	});
	q.enqueue(clip(0)); // stays inflight — blocks the cursor at seq 0
	q.enqueue(clip(1)); // will FAIL, then the user discards it
	q.enqueue(clip(2)); // done, waits behind the gap
	await flush();

	tx.reject(1, new Error("dead")); // seq 1 → failed
	await flush();
	tx.resolve(2, "two"); // seq 2 done but held behind inflight seq 0
	await flush();
	assert.deepEqual(commits, [], "nothing commits yet — inflight seq 0 blocks the cursor");
	assert.deepEqual(failed, [1], "seq 1 failed");

	// User discards the failed seq 1. Under the OLD records.delete(seq) this punched a hole
	// at seq 1 that wedged the flush cursor forever — seq 2 would NEVER commit (silent loss).
	q.discard(1);
	await flush();
	assert.deepEqual(commits, [], "discard alone commits nothing — seq 0 is still inflight");

	// seq 0 resolves → cursor drains 0, crosses the seq-1 TOMBSTONE, commits seq 2.
	tx.resolve(0, "zero");
	await flush();
	assert.deepEqual(
		commits,
		[
			[0, "zero"],
			[2, "two"],
		],
		"later transcript committed across the discarded hole — no silent loss"
	);
	assert.ok(!mirror.store.has(1), "the discarded clip's mirror is gone");
	assert.equal(q.hasUnfinished(), false, "everything committed or discarded — the guard clears");
});

// ── (15) retainUntilSent keeps the guard armed + mirror retained until acknowledge (finding 6) ─
test("retainUntilSent: a committed clip keeps the leave guard armed and its audio retained until the draft is SENT", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 1,
	});
	q.enqueue({ blob: "b0", durationS: 15, conversationId: "chatX" });
	await flush();
	tx.resolve(0, "hello");
	await flush();
	assert.equal(
		q.hasUnfinished(),
		true,
		"committed but UN-SENT text keeps the leave guard armed — the draft is volatile"
	);
	assert.ok(mirror.store.has(0), "audio mirror retained past commit so a reload can recover it");
	assert.equal(q.snapshot().done, 1, "the clip shows as done in the snapshot");

	q.acknowledge(q.captureSent("chatX"));
	await flush();
	assert.equal(
		q.hasUnfinished(),
		false,
		"sending the draft makes it durable — the guard clears"
	);
	assert.ok(!mirror.store.has(0), "mirror cleared on send");
	assert.equal(q.snapshot().total, 0, "the sent clip has left the queue");

	// acknowledge for a DIFFERENT scope must never touch another scope's committed clips.
	q.enqueue({ blob: "b1", durationS: 15, conversationId: "chatY" });
	await flush();
	tx.resolve(1, "world");
	await flush();
	q.acknowledge(q.captureSent("chatZ")); // wrong scope → empty token, releases nothing
	await flush();
	assert.equal(
		q.hasUnfinished(),
		true,
		"a clip for chatY is untouched by acknowledge(captureSent('chatZ')) — held drafts don't clear cross-scope"
	);
	assert.ok(mirror.store.has(1), "its audio stays retained");
});

// ── (16) recover() restores spoken order within a session under a shuffled arrival (finding 3) ─
test("recover() restores spoken order WITHIN a session even when the clips arrive shuffled", async () => {
	const tx = makeTranscriber();
	const commits = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		concurrency: 5,
		onCommit: (seq, text) => commits.push(text),
	});
	// listOrphanClips already grouped + ordered these; recover must keep the session together
	// and sort by ORIGINAL seq. Pass them SHUFFLED to prove recover re-sorts within the session
	// (a global seq sort across sessions would interleave — see the note in recover()).
	const seqs = q.recover([
		{ blob: "s0c2", durationS: 15, sessionId: "s0", seq: 2 },
		{ blob: "s0c0", durationS: 15, sessionId: "s0", seq: 0 },
		{ blob: "s0c1", durationS: 15, sessionId: "s0", seq: 1 },
	]);
	assert.deepEqual(
		seqs,
		[0, 1, 2],
		"fresh contiguous seqs assigned in ascending ORIGINAL seq order within the session"
	);
	await flush();
	// Resolve out of order; strict in-order commit still restores spoken order.
	tx.resolve(2, "third");
	tx.resolve(0, "first");
	tx.resolve(1, "second");
	await flush();
	assert.deepEqual(
		commits,
		["first", "second", "third"],
		"committed in spoken order, not arrival/shuffle/resolution order"
	);
});

// ── (17) R2-1: payload-bound release — a clip that commits AFTER a send captured its token is
//        NEVER deleted by that send (the old scope sweep deleted every committed record). ─────
test("R2-1: a clip that commits after captureSent is never released by that send's acknowledge", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 4,
	});
	// Clip A commits — it is the payload the composer is about to send.
	q.enqueue({ blob: "a", durationS: 15, conversationId: "chatA" });
	await flush();
	tx.resolve(0, "alpha");
	await flush();
	// The composer captures this send's payload NOW (A only), before the POST is attempted.
	const token = q.captureSent("chatA");
	assert.deepEqual(token, [0], "the token is exactly the clip committed before the send began");

	// While that POST is in flight the user keeps dictating: clip B commits into the SAME scope.
	q.enqueue({ blob: "b", durationS: 15, conversationId: "chatA" });
	await flush();
	tx.resolve(1, "bravo");
	await flush();
	assert.ok(
		mirror.store.has(0) && mirror.store.has(1),
		"both committed clips are mirror-retained (retainUntilSent)"
	);

	// The in-flight send resolves — it must release ONLY its captured clip (A), never B.
	q.acknowledge(token);
	await flush();
	assert.ok(!mirror.store.has(0), "clip A (in the sent payload) is released");
	assert.ok(
		mirror.store.has(1),
		"clip B (committed AFTER capture) is NOT released — audio kept"
	);
	assert.equal(q.hasUnfinished(), true, "B's still-un-sent text keeps the leave guard armed");
	assert.equal(q.snapshot().total, 1, "B is still in the queue behind its volatile draft");
});

// ── (18) R2-1 inverse: a FAILED send releases nothing; the failed-bubble RESEND (carrying the
//        same token) releases the delivered records — no mirror leak / stuck guard. ───────────
test("R2-1 inverse: a failed send holds its clips; the resend carrying the same token releases them", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 2,
	});
	q.enqueue({ blob: "a", durationS: 15, conversationId: "chatA" });
	await flush();
	tx.resolve(0, "alpha");
	await flush();
	// The first send captures its payload; then the POST FAILS — nothing is acknowledged.
	const token = q.captureSent("chatA");
	assert.deepEqual(token, [0]);
	assert.ok(mirror.store.has(0), "audio still retained after the failed send");
	assert.equal(q.hasUnfinished(), true, "the guard stays armed — nothing was durably sent");

	// The user hits Retry on the failed bubble; the resend carries the ORIGINAL token and succeeds.
	q.acknowledge(token);
	await flush();
	assert.ok(!mirror.store.has(0), "the delivered records ARE released on the resend");
	assert.equal(q.hasUnfinished(), false, "the guard clears — no leak, no forever-armed state");
});

// ── (19) R2-2: a recovered new-chat clip lives under the ONE sentinel scope — a send from that
//        scope releases it; reassignScope migrates it to a real id so a real-scope send releases
//        it too (guard + mirror clear, nothing re-offered on reload). ─────────────────────────
test("R2-2: recovered new-chat clip is releasable under the single sentinel scope; reassignScope moves it to a real id", async () => {
	const SENTINEL = "__jarvis_new_chat__";
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 2,
	});
	// Recovery canonicalises a missing scope to the sentinel BEFORE admission (ChatView does this),
	// so the clip is admitted, routed, and released all under the SAME representation.
	q.recover([{ blob: "orphan", durationS: 15, conversationId: SENTINEL, seq: 4 }]);
	await flush();
	tx.resolve(0, "recovered words");
	await flush();
	assert.ok(
		mirror.store.has(0),
		"the recovered new-chat clip is retained until its draft is sent"
	);
	// A bare-null capture must NOT match a sentinel-scoped clip (proves the single representation).
	assert.deepEqual(q.captureSent(null), [], "null is a DIFFERENT scope — no cross-release");
	assert.deepEqual(q.captureSent(SENTINEL), [0], "the sentinel scope owns the recovered clip");

	// Path A — sent straight from the id-less new-chat composer (scope stays the sentinel).
	q.acknowledge(q.captureSent(SENTINEL));
	await flush();
	assert.ok(!mirror.store.has(0), "a sent new-chat recovery clears its mirror");
	assert.equal(q.hasUnfinished(), false, "and its leave guard — nothing to re-offer on reload");

	// Path B — newChat() promotes the sentinel to a REAL id: reassignScope moves the record so a
	// real-scope send releases it (otherwise it would be stranded under the sentinel forever).
	q.recover([{ blob: "orphan2", durationS: 15, conversationId: SENTINEL, seq: 7 }]);
	await flush();
	tx.resolve(1, "more words");
	await flush();
	q.reassignScope(SENTINEL, "conv-real");
	assert.deepEqual(
		q.captureSent(SENTINEL),
		[],
		"nothing left under the sentinel after migration"
	);
	assert.deepEqual(
		q.captureSent("conv-real"),
		[1],
		"the record now belongs to the real conversation"
	);
	assert.ok(mirror.store.has(1), "still retained — migration must never drop audio");
	q.acknowledge(q.captureSent("conv-real"));
	await flush();
	assert.ok(!mirror.store.has(1), "the real-scope send releases the migrated clip");
	assert.equal(
		q.hasUnfinished(),
		false,
		"guard clears end to end under the single representation"
	);
});

// ── (20) R2-3: a trimmed-empty transcription is an ACTIONABLE failed clip, never an invisible
//        retained done record — Download/Discard clears it, no forever-armed guard, no loop. ──
test("R2-3: an empty/whitespace transcription surfaces an actionable failed chip, not a forever-armed guard", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const failed = [];
	const commits = [];
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 1,
		maxAttempts: 2, // even with a retry budget available, an empty result must NOT loop
		onCommit: (seq, text) => commits.push([seq, text]),
		onFail: (seq) => failed.push(seq),
	});
	q.enqueue({ blob: "silence", durationS: 15, conversationId: "chatA" });
	await flush();
	// The endpoint returns a whitespace-only string (genuine silence OR a miss of real speech).
	tx.resolve(0, "   ");
	await flush();

	assert.deepEqual(commits, [], "an empty transcription commits NO text (nothing to append)");
	assert.deepEqual(failed, [0], "it is surfaced as a failed clip — the actionable chip");
	const snap = q.snapshot();
	assert.equal(snap.failed.length, 1, "the snapshot exposes it for Download/Discard/Retry");
	assert.equal(snap.done, 0, "it is NOT an invisible done record");
	assert.equal(tx.callCount(0), 1, "no auto-retry loop — silence is not re-uploaded");
	assert.ok(mirror.store.has(0), "the audio is RETAINED (never silently dropped)");
	assert.equal(q.hasUnfinished(), true, "the guard is armed — but now with an ACTIONABLE chip");
	assert.ok(q.getClip(0), "getClip returns the blob so the chip's Download works");

	// It must NEVER be auto-released by a send for its scope (it is failed, not committed).
	q.acknowledge(q.captureSent("chatA"));
	await flush();
	assert.ok(
		mirror.store.has(0),
		"a send for the scope does not release an un-transcribed empty clip"
	);
	assert.equal(q.hasUnfinished(), true, "still armed until the user acts");

	// The user's explicit "it was silence": Discard clears the guard (no forever-armed loop).
	q.discard(0);
	await flush();
	assert.equal(q.hasUnfinished(), false, "Discard clears the guard — no forever-armed state");
	assert.ok(!mirror.store.has(0), "and drops the mirror record");
});

// ── (21) R3-1: PAYLOAD-bound release — a clip whose transcript the user EDITED or DELETED out of
//        the composer before sending is NOT acknowledged, so its audio is RETAINED. captureSent
//        (scope-bound) would have released every committed clip regardless of the payload. ────────
test("R3-1: captureSentInPayload acknowledges ONLY clips whose transcript is present in the outgoing payload", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 3,
	});
	q.enqueue({ blob: "a", durationS: 15, conversationId: "chatA" });
	q.enqueue({ blob: "b", durationS: 15, conversationId: "chatA" });
	q.enqueue({ blob: "c", durationS: 15, conversationId: "chatA" });
	await flush();
	tx.resolve(0, "alpha words");
	tx.resolve(1, "bravo words");
	tx.resolve(2, "charlie words");
	await flush();

	// The OLD scope-bound token released ALL THREE — the R3-1 bug: it deletes audio whose text the
	// user removed from the composer.
	assert.deepEqual(
		q.captureSent("chatA"),
		[0, 1, 2],
		"captureSent is scope-bound — every committed clip, payload be damned"
	);

	// The user kept A verbatim, RETYPED over B (its exact text is gone), and DELETED C.
	const payload = "alpha words and something I retyped instead of bravo";
	const token = q.captureSentInPayload("chatA", payload);
	assert.deepEqual(
		token,
		[0],
		"only clip A — its transcript is present verbatim; B (edited) and C (deleted) are absent"
	);

	q.acknowledge(token);
	await flush();
	assert.ok(!mirror.store.has(0), "clip A (in the sent payload) is released");
	assert.ok(
		mirror.store.has(1) && mirror.store.has(2),
		"clips B (edited) and C (deleted) are RETAINED — audio never lost without an explicit discard"
	);
	assert.equal(
		q.hasUnfinished(),
		true,
		"the retained clips keep the leave guard armed + recoverable"
	);
});

// ── (22) R3-1: normalized (whitespace-insensitive) match + duplicate transcripts consume ONE
//        occurrence each — deleting one of two identical clips retains the other (never over-release). ─
test("R3-1: captureSentInPayload normalizes whitespace and consumes duplicate occurrences", async () => {
	const tx = makeTranscriber();
	const q = createVoiceChunkQueue({ transcribe: tx.fn, retainUntilSent: true, concurrency: 4 });
	q.enqueue({ blob: "a", durationS: 15, conversationId: "s" });
	q.enqueue({ blob: "b", durationS: 15, conversationId: "s" });
	await flush();
	// Two clips with IDENTICAL committed text; the server text carries messy whitespace.
	tx.resolve(0, "  hello   there  ");
	tx.resolve(1, "hello there");
	await flush();

	// One occurrence in the payload (the user deleted one of the two identical segments), collapsed
	// whitespace: a NORMALIZED match still finds it, but only ONE clip is acknowledged.
	assert.equal(
		q.captureSentInPayload("s", "prefix hello there suffix").length,
		1,
		"one occurrence → exactly one clip acknowledged; the other is RETAINED"
	);
	// Both occurrences present → both acknowledged.
	assert.deepEqual(
		q.captureSentInPayload("s", "hello there and again hello there"),
		[0, 1],
		"two occurrences → both clips acknowledged"
	);
	// An empty / whitespace-only payload matches nothing → releases nothing (never-lose).
	assert.deepEqual(q.captureSentInPayload("s", "   "), [], "an empty payload releases nothing");
	assert.deepEqual(q.captureSentInPayload("s", ""), [], "no payload releases nothing");
	// Scope isolation: a clip in another scope is never released by this scope's payload match.
	q.enqueue({ blob: "z", durationS: 15, conversationId: "other" });
	await flush();
	tx.resolve(2, "hello there");
	await flush();
	assert.deepEqual(
		q.captureSentInPayload("s", "hello there hello there hello there"),
		[0, 1],
		"even with a third identical occurrence present, the 'other'-scope clip is never matched"
	);
});

// ── (23) VR4-4: BOUNDARY-aware match — an earlier "send it" must NOT match inside a later
//        "resend it". The old raw indexOf acknowledged (then deleted) the WRONG clip's audio. ───
test("VR4-4: captureSentInPayload matches on WORD BOUNDARIES — 'send it' never matches inside 'resend it' (order-independent)", async () => {
	// Forward order: the deleted clip has the LOWER seq (it is processed first — the exact ordering
	// that made the old indexOf consume the wrong occurrence).
	{
		const tx = makeTranscriber();
		const mirror = makeMirror();
		const q = createVoiceChunkQueue({
			transcribe: tx.fn,
			mirror,
			retainUntilSent: true,
			concurrency: 4,
		});
		q.enqueue({ blob: "x", durationS: 15, conversationId: "s" }); // seq 0 → "send it" (DELETED)
		q.enqueue({ blob: "y", durationS: 15, conversationId: "s" }); // seq 1 → "resend it" (KEPT)
		await flush();
		tx.resolve(0, "send it");
		tx.resolve(1, "resend it");
		await flush();
		const token = q.captureSentInPayload("s", "resend it");
		assert.deepEqual(
			token,
			[1],
			"only the KEPT clip ('resend it') is acknowledged; the deleted 'send it' is NOT matched inside it"
		);
		q.acknowledge(token);
		await flush();
		assert.ok(
			mirror.store.has(0),
			"the DELETED clip's audio is RETAINED (never lost — VR4-4)"
		);
		assert.ok(!mirror.store.has(1), "the SENT clip is released");
	}
	// Reverse order: the kept clip has the LOWER seq — must still never over-release.
	{
		const tx = makeTranscriber();
		const mirror = makeMirror();
		const q = createVoiceChunkQueue({
			transcribe: tx.fn,
			mirror,
			retainUntilSent: true,
			concurrency: 4,
		});
		q.enqueue({ blob: "y", durationS: 15, conversationId: "s" }); // seq 0 → "resend it" (KEPT)
		q.enqueue({ blob: "x", durationS: 15, conversationId: "s" }); // seq 1 → "send it" (DELETED)
		await flush();
		tx.resolve(0, "resend it");
		tx.resolve(1, "send it");
		await flush();
		const token = q.captureSentInPayload("s", "resend it");
		assert.deepEqual(
			token,
			[0],
			"the kept 'resend it' clip is acknowledged regardless of seq order"
		);
		q.acknowledge(token);
		await flush();
		assert.ok(
			mirror.store.has(1),
			"the deleted 'send it' clip is RETAINED (order-independent)"
		);
	}
});

// ── (24) VR4-4: a GENUINELY boundary-aligned occurrence still matches (the fix must not
//        over-retain: a clip whose text really is present, whole-word, is released as before). ──
test("VR4-4: a whole-word occurrence still matches — boundary-awareness never blocks a real send", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 4,
	});
	q.enqueue({ blob: "a", durationS: 15, conversationId: "s" }); // "send it"
	q.enqueue({ blob: "b", durationS: 15, conversationId: "s" }); // "now"
	await flush();
	tx.resolve(0, "send it");
	tx.resolve(1, "now");
	await flush();
	// Both clips present at word boundaries in the payload → both acknowledged.
	const token = q.captureSentInPayload("s", "please send it now thanks");
	assert.deepEqual(token, [0, 1], "both whole-word clips are matched and released");
	q.acknowledge(token);
	await flush();
	assert.ok(!mirror.store.has(0) && !mirror.store.has(1), "both released — no false retention");
});

// ── (25) VR4-1: a committed clip edited OUT of a sent payload becomes an ACTIONABLE retained clip
//        (snapshot.retained) rather than a silent `done` count; Discard clears the guard. ────────
test("VR4-1: markUnsentOrphans surfaces an edited-out committed clip as retained; Restore/Discard resolve it", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 3,
	});
	q.enqueue({ blob: "a", durationS: 15, conversationId: "chatA" });
	q.enqueue({ blob: "b", durationS: 15, conversationId: "chatA" });
	await flush();
	tx.resolve(0, "alpha words");
	tx.resolve(1, "bravo words");
	await flush();
	assert.deepEqual(
		q.snapshot().retained,
		[],
		"nothing retained yet — both clips just committed"
	);

	// The user sent only clip A's text (B edited out). Release A, then mark the unsent orphans.
	const token = q.captureSentInPayload("chatA", "alpha words");
	assert.deepEqual(token, [0], "only clip A is in the payload");
	q.acknowledge(token);
	q.markUnsentOrphans("chatA", token);

	const snap = q.snapshot();
	assert.equal(
		snap.retained.length,
		1,
		"the edited-out clip B is now an ACTIONABLE retained clip"
	);
	assert.equal(snap.retained[0].seq, 1);
	assert.equal(
		snap.retained[0].text,
		"bravo words",
		"retained entry carries its transcript for Restore"
	);
	assert.ok(mirror.store.has(1), "its audio is still retained (never lost)");
	assert.equal(q.hasUnfinished(), true, "the leave guard is armed — but now with a resolution");

	// Restore clears the orphaned flag (its text is going back into the composer) — chip disappears,
	// but it stays retained until sent (so a later send can release it).
	q.unorphan(1);
	assert.deepEqual(q.snapshot().retained, [], "Restore clears the retained chip");
	assert.equal(
		q.hasUnfinished(),
		true,
		"still un-sent until the restored text is actually sent"
	);

	// Discard the guard-armed clip: its audio drops and the guard clears (never forever-armed).
	q.markUnsentOrphans("chatA", []); // re-orphan (the user edited it out again and didn't restore)
	assert.equal(q.snapshot().retained.length, 1, "re-orphaned");
	q.discard(1);
	assert.deepEqual(q.snapshot().retained, [], "Discard removes it from the retained list");
	assert.equal(
		q.hasUnfinished(),
		false,
		"Discard clears the guard — the VR4-1 forever-armed gap"
	);
	assert.ok(!mirror.store.has(1), "and drops the mirror record");
});

// ── (26) VR5-1: RETAIN-ON-AMBIGUITY — a multi-word prefix/suffix overlap ("send it" + "now" ⊂
//        "send it now") must delete NO clip; every affected clip is RETAINED (never the wrong one). ──
test("VR5-1: overlapping multi-word clips (send it / now / send it now) are all RETAINED — no wrong-clip deletion, either seq order", async () => {
	// The exact 3-clip trap the boundary matcher (VR4-4) did NOT cover. Prove BOTH enqueue orders:
	// the single-word-first clips having the LOWER seqs (the greedy path that mis-consumed) AND the
	// combined clip having the lower seq.
	const orders = [
		["send it", "now", "send it now"], // seq 0,1 are the prefix/suffix; seq 2 is the whole
		["send it now", "send it", "now"], // seq 0 is the whole; seq 1,2 are the prefix/suffix
	];
	for (const texts of orders) {
		const tx = makeTranscriber();
		const mirror = makeMirror();
		const q = createVoiceChunkQueue({
			transcribe: tx.fn,
			mirror,
			retainUntilSent: true,
			concurrency: 4,
		});
		for (let i = 0; i < texts.length; i++)
			q.enqueue({ blob: `b${i}`, durationS: 15, conversationId: "s" });
		await flush();
		for (let i = 0; i < texts.length; i++) tx.resolve(i, texts[i]);
		await flush();

		// The user removed the two single-word clips and sent only the combined recording (or vice
		// versa) — but "send it now" is exactly "send it" + "now", so the payload span is ambiguous.
		const token = q.captureSentInPayload("s", "send it now");
		assert.deepEqual(
			token,
			[],
			`ambiguous overlap → acknowledge NOTHING (order ${JSON.stringify(texts)})`
		);
		q.acknowledge(token);
		await flush();
		for (let i = 0; i < texts.length; i++)
			assert.ok(
				mirror.store.has(i),
				`clip ${i} ("${texts[i]}") RETAINED — the wrong clip's audio is never deleted`
			);

		// The affected clips surface as ACTIONABLE retained chips (VR4-1), never a silent forever-armed
		// guard: markUnsentOrphans with the (empty) token flags every un-released committed clip.
		q.markUnsentOrphans("s", token);
		const snap = q.snapshot();
		assert.equal(
			snap.retained.length,
			texts.length,
			"all overlapping clips become actionable retained chips (Restore/Download/Discard)"
		);
		assert.equal(q.hasUnfinished(), true, "guard armed WITH an action, not silently lost");
	}
});

// ── (27) VR5-1: a partial multi-word overlap ("play the song" vs "song of the year") still retains
//        BOTH when their spans overlap in the payload; unrelated clips are unaffected. ─────────────
test("VR5-1: partial word-run overlap retains both overlapping clips but releases an unrelated clip", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 4,
	});
	q.enqueue({ blob: "a", durationS: 15, conversationId: "s" }); // seq 0 "the song is"
	q.enqueue({ blob: "b", durationS: 15, conversationId: "s" }); // seq 1 "song is great"
	q.enqueue({ blob: "c", durationS: 15, conversationId: "s" }); // seq 2 "totally unrelated"
	await flush();
	tx.resolve(0, "the song is");
	tx.resolve(1, "song is great");
	tx.resolve(2, "totally unrelated");
	await flush();
	// Payload contains a span "the song is great" (0 and 1 overlap on "song is") plus the unrelated clip.
	const token = q.captureSentInPayload("s", "the song is great and totally unrelated");
	assert.deepEqual(
		token,
		[2],
		"the two overlapping clips are RETAINED (ambiguous); the disjoint unrelated clip is released"
	);
	q.acknowledge(token);
	await flush();
	assert.ok(mirror.store.has(0) && mirror.store.has(1), "overlapping clips 0,1 kept");
	assert.ok(!mirror.store.has(2), "the unrelated clip 2 (unambiguous) is released");
});
