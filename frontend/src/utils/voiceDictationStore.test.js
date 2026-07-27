// Real executable test for the per-RECORDING dictation lifecycle (voiceDictationStore.js).
// Plain node built-ins (node:test + node:assert) — no external framework — exactly like
// eventFence.test.js. Run directly (`node --test voiceDictationStore.test.js`) or via the python
// suite: jarvis/tests/test_voice_dictation_store_client.py subprocess-runs this so the client
// contract is enforced by every CI run. `node --test` exits non-zero on any failed assertion,
// which the python runner asserts.
//
// The two properties every test here defends:
//   1. ONE recording is transcribed in ONE call, with its whole context — never per fragment.
//      Fragments exist for crash-safety only.
//   2. The audio is NEVER lost: mirrored before transcription, retained through failure, released
//      only by an ACCEPTED send, and re-offered after a crash.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createVoiceDictationStore } from "./voiceDictationStore.js";

// Drain pending microtasks (the store chains a few .then()s per transition).
const flush = () => new Promise((r) => setTimeout(r, 0));

// A controllable transcribe: every call parks until the test settles it by recording id, while
// tracking how many requests are in flight at once and WHAT was uploaded.
function makeTranscriber() {
	const calls = [];
	let inflight = 0;
	let maxInflight = 0;
	const fn = (rec) => {
		inflight += 1;
		if (inflight > maxInflight) maxInflight = inflight;
		return new Promise((resolve, reject) => {
			calls.push({
				id: rec.id,
				blob: rec.blob,
				durationS: rec.durationS,
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
	const _first = (id) => calls.find((c) => c.id === id && !c.settled);
	return {
		fn,
		calls,
		get maxInflight() {
			return maxInflight;
		},
		uploads() {
			return calls.map((c) => c.blob);
		},
		resolve(id, text) {
			const c = _first(id);
			assert.ok(c, `no in-flight transcribe call for recording ${id}`);
			c.settle(true, text);
		},
		reject(id, err) {
			const c = _first(id);
			assert.ok(c, `no in-flight transcribe call for recording ${id}`);
			c.settle(false, err || new Error("boom"));
		},
		callCount(id) {
			return calls.filter((c) => c.id === id).length;
		},
	};
}

// In-memory stand-in for the IndexedDB fragment mirror; logs every write/delete so the
// crash-safety lifecycle (mirror on arrival, RETAIN on failure, drop on release) is testable.
function makeMirror(sessionId = "s1") {
	const store = new Map(); // `${recordingId}:${index}` -> record
	const log = [];
	let failWrite = null; // (frag) => truthy to make the write fail
	return {
		store,
		log,
		recordingKey: (id) => `${sessionId}#${id}`,
		putFragment(frag) {
			log.push(["put", frag.recordingId, frag.index]);
			if (failWrite && failWrite(frag)) return Promise.resolve(false);
			store.set(`${frag.recordingId}:${frag.index}`, frag);
			return Promise.resolve(true);
		},
		adopt(r) {
			log.push(["adopt", r.recordingId]);
			if (failWrite && failWrite(r)) return Promise.resolve(false);
			for (const k of r._adoptKeys || []) store.delete(k);
			store.set(`${r.recordingId}:0`, r);
			return Promise.resolve(true);
		},
		deleteRecording(rid) {
			log.push(["delete", rid]);
			for (const k of Array.from(store.keys())) if (k.startsWith(rid + ":")) store.delete(k);
			return Promise.resolve(true);
		},
		reassignConversation(rid, scope) {
			log.push(["reassign", rid, scope]);
			for (const [k, v] of store)
				if (String(v.recordingId) === rid) store.set(k, { ...v, conversationId: scope });
			return Promise.resolve(true);
		},
		setFailWrite(fn) {
			failWrite = fn;
		},
		has(rid) {
			return Array.from(store.keys()).some((k) => k.startsWith(rid + ":"));
		},
		fragmentsOf(rid) {
			return Array.from(store.values())
				.filter((v) => String(v.recordingId) === rid)
				.map((v) => v.index)
				.sort((a, b) => a - b);
		},
	};
}

// Drive a whole take exactly as useDictationRecorder does: begin, N timeslice fragments as they
// arrive, then finish with the CONCATENATION of those fragments (the assembled recording).
function dictate(store, { conversationId = null, fragments = ["a"], durationS = 15 } = {}) {
	const id = store.begin({ conversationId, mimeType: "audio/webm;codecs=opus" });
	fragments.forEach((f, i) => {
		store.addFragment(id, {
			index: i,
			blob: f,
			durationS: Math.round((durationS * (i + 1)) / fragments.length),
		});
	});
	store.finish(id, { blob: fragments.join(""), durationS, mimeType: "audio/webm;codecs=opus" });
	return id;
}

// ── (1) ONE call per recording, carrying the ASSEMBLED bytes ────────────────────────────────
test("a take's fragments are mirrored as they arrive, then transcribed as ONE assembled recording", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const commits = [];
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror,
		onCommit: (id, text) => commits.push([id, text]),
	});

	const id = q.begin({ conversationId: "convA" });
	// Three ~15 s timeslice fragments arrive DURING the take.
	q.addFragment(id, { index: 0, blob: "AAA", durationS: 15 });
	q.addFragment(id, { index: 1, blob: "BBB", durationS: 30 });
	await flush();
	assert.equal(
		tx.calls.length,
		0,
		"a fragment is crash-safety only — it must NEVER be uploaded"
	);
	assert.deepEqual(
		mirror.log.filter((l) => l[0] === "put"),
		[
			["put", "s1#0", 0],
			["put", "s1#0", 1],
		],
		"each fragment is written the instant it arrives, in index order"
	);

	q.addFragment(id, { index: 2, blob: "CCC", durationS: 41 });
	q.finish(id, { blob: "AAABBBCCC", durationS: 41 });
	await flush();
	assert.equal(tx.calls.length, 1, "ONE transcription for the whole recording");
	assert.equal(tx.calls[0].blob, "AAABBBCCC", "…and it carries the ASSEMBLED bytes, in order");
	assert.equal(tx.calls[0].durationS, 41);
	assert.deepEqual(mirror.fragmentsOf("s1#0"), [0, 1, 2], "all three fragments are durable");

	tx.resolve(id, "the whole sentence, with context");
	await flush();
	assert.deepEqual(commits, [[0, "the whole sentence, with context"]]);
});

// ── (2) two overlapping takes still land in SPOKEN order ────────────────────────────────────
test("a second take started while the first is transcribing never jumps ahead of it", async () => {
	const tx = makeTranscriber();
	const commits = [];
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror: makeMirror(),
		onCommit: (id, text) => commits.push(text),
	});
	const first = dictate(q, { fragments: ["one"] });
	const second = dictate(q, { fragments: ["two"] });
	await flush();
	// The SECOND recording comes back first.
	tx.resolve(second, "second words");
	await flush();
	assert.deepEqual(commits, [], "nothing commits while an EARLIER recording is still in flight");
	tx.resolve(first, "first words");
	await flush();
	assert.deepEqual(commits, ["first words", "second words"], "spoken order is preserved");
});

test("never runs more than `concurrency` transcriptions at once", async () => {
	const tx = makeTranscriber();
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror: makeMirror(),
		concurrency: 2,
	});
	const ids = [0, 1, 2, 3].map(() => dictate(q, { fragments: ["x"] }));
	await flush();
	assert.equal(tx.maxInflight, 2, "the concurrency bound holds");
	tx.resolve(ids[0], "a");
	await flush();
	assert.ok(tx.maxInflight <= 2, "…and stays held as slots free");
});

// ── (3) failure RETAINS the audio and surfaces ONE actionable chip ───────────────────────────
test("a recording that rejects twice enters `failed`, fires onFail, and RETAINS its audio", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const failed = [];
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		onFail: (id) => failed.push(id),
	});
	const id = dictate(q, { fragments: ["a", "b"], durationS: 30 });
	await flush();
	tx.reject(id, new Error("timeout"));
	await flush();
	assert.equal(tx.callCount(id), 2, "exactly ONE auto-retry");
	tx.reject(id, new Error("timeout"));
	await flush();
	assert.deepEqual(failed, [id]);
	const snap = q.snapshot();
	assert.equal(
		snap.failed.length,
		1,
		"ONE chip for the whole recording — never one per fragment"
	);
	assert.deepEqual(snap.failed[0], { id, durationS: 30, sentWithout: false });
	assert.ok(mirror.has("s1#0"), "the audio is RETAINED — that is what Retry/Download act on");
	assert.equal(q.hasUnfinishedReason(), "unresolved", "terminal + durable is not a loss risk");
});

test("Retry re-sends the SAME assembled bytes; a late success commits as `resurrected`", async () => {
	const tx = makeTranscriber();
	const commits = [];
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror: makeMirror(),
		retainUntilSent: true,
		onCommit: (id, text, rec, resurrected) => commits.push([text, resurrected]),
	});
	const first = dictate(q, { fragments: ["AAA", "BBB"] });
	await flush();
	tx.reject(first);
	await flush();
	tx.reject(first);
	await flush();
	// A later take transcribes fine and its words land while the first is still failed.
	const second = dictate(q, { fragments: ["ZZZ"] });
	await flush();
	tx.resolve(second, "later words");
	await flush();
	assert.deepEqual(
		commits,
		[["later words", false]],
		"a failed recording never halts a later one"
	);

	q.retry(first);
	await flush();
	const retryCall = tx.calls.filter((c) => c.id === first).pop();
	assert.equal(
		retryCall.blob,
		"AAABBB",
		"Retry uploads the SAME assembled recording, not a fragment"
	);
	tx.resolve(first, "recovered words");
	await flush();
	assert.deepEqual(
		commits[1],
		["recovered words", true],
		"the cursor had moved on, so the commit is flagged resurrected (it lands at the END)"
	);
});

test("an empty transcription is a FAILURE, not a silent success", async () => {
	const tx = makeTranscriber();
	const commits = [];
	const mirror = makeMirror();
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		onCommit: (id, text) => commits.push(text),
	});
	const id = dictate(q, { fragments: ["a"] });
	await flush();
	tx.resolve(id, "   ");
	await flush();
	assert.deepEqual(commits, [], "nothing is committed — empty is UNCERTAIN, not proven silence");
	assert.equal(
		tx.callCount(id),
		1,
		"and it is NOT looped: re-sending silence just returns empty"
	);
	assert.equal(q.snapshot().failed.length, 1, "it becomes an ACTIONABLE chip");
	assert.ok(mirror.has("s1#0"), "…with its audio retained behind it");
});

test("belt: a transcribe that resolves a non-string is treated as a failure, never String()-coerced", async () => {
	const tx = makeTranscriber();
	const commits = [];
	const mirror = makeMirror();
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		onCommit: (id, text) => commits.push(text),
	});
	const id = dictate(q, { fragments: ["a"] });
	await flush();
	tx.resolve(id, { ok: true, text: "hello" }); // the raw response object — the old bug
	await flush();
	tx.resolve(id, { ok: true, text: "hello" });
	await flush();
	assert.deepEqual(commits, [], "no '[object Object]' ever reaches the composer");
	assert.equal(q.snapshot().failed.length, 1);
	assert.ok(mirror.has("s1#0"), "and the audio is retained rather than deleted behind garbage");
});

// ── (4) release ONLY on an accepted send ────────────────────────────────────────────────────
test("retainUntilSent: a transcribed recording keeps its audio + the guard until the draft is SENT", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
	});
	const id = dictate(q, { conversationId: "convA", fragments: ["a"] });
	await flush();
	tx.resolve(id, "hello there");
	await flush();
	assert.ok(
		mirror.has("s1#0"),
		"committed is NOT sent — the draft is volatile, so the audio stays"
	);
	assert.equal(q.hasUnfinishedReason(), "live", "…and the guard says so");
	const token = q.captureSentInPayload("convA", "hello there");
	assert.deepEqual(token, [id]);
	q.acknowledge(token);
	assert.ok(!mirror.has("s1#0"), "an ACCEPTED send releases the audio");
	assert.equal(q.hasUnfinishedReason(), null, "…and clears the guard");
});

test("captureSentInPayload acknowledges ONLY recordings whose text is actually in the payload", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceDictationStore({ transcribe: tx.fn, mirror, retainUntilSent: true });
	const a = dictate(q, { conversationId: "c1", fragments: ["a"] });
	const b = dictate(q, { conversationId: "c1", fragments: ["b"] });
	await flush();
	tx.resolve(a, "keep this");
	await flush();
	tx.resolve(b, "delete this");
	await flush();
	// The user edited B's words out of the composer before sending.
	const token = q.captureSentInPayload("c1", "keep this and something I typed");
	assert.deepEqual(token, [a], "only the recording actually present in the payload is released");
	q.acknowledge(token);
	assert.ok(!mirror.has("s1#0"), "A — its words were sent — is released");
	assert.ok(mirror.has("s1#1"), "B — edited out — is RETAINED (never lose audio)");
});

test("captureSentInPayload matches on WORD BOUNDARIES and retains on ambiguity", async () => {
	const tx = makeTranscriber();
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror: makeMirror(),
		retainUntilSent: true,
	});
	// "send it" must NOT match inside "resend it".
	const a = dictate(q, { conversationId: "c", fragments: ["a"] });
	const b = dictate(q, { conversationId: "c", fragments: ["b"] });
	await flush();
	tx.resolve(a, "send it");
	await flush();
	tx.resolve(b, "resend it");
	await flush();
	assert.deepEqual(
		q.captureSentInPayload("c", "resend it"),
		[b],
		"a mid-word substring is never a match — the wrong recording's audio is never deleted"
	);

	// Overlapping multi-word contributions are genuinely ambiguous → acknowledge NEITHER.
	const tx2 = makeTranscriber();
	const q2 = createVoiceDictationStore({
		transcribe: tx2.fn,
		mirror: makeMirror("s2"),
		retainUntilSent: true,
	});
	const x = dictate(q2, { conversationId: "c", fragments: ["x"] });
	const y = dictate(q2, { conversationId: "c", fragments: ["y"] });
	const z = dictate(q2, { conversationId: "c", fragments: ["z"] });
	await flush();
	tx2.resolve(x, "send it");
	await flush();
	tx2.resolve(y, "now");
	await flush();
	tx2.resolve(z, "send it now");
	await flush();
	assert.deepEqual(
		q2.captureSentInPayload("c", "send it now"),
		[],
		"an ambiguous payload span retains EVERY affected recording — it can only over-retain"
	);
});

test("markUnsentOrphans surfaces an edited-out recording as an actionable retained chip", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceDictationStore({ transcribe: tx.fn, mirror, retainUntilSent: true });
	const a = dictate(q, { conversationId: "c1", fragments: ["a"], durationS: 42 });
	await flush();
	tx.resolve(a, "words the user deleted");
	await flush();
	const token = q.captureSentInPayload("c1", "something else entirely");
	assert.deepEqual(token, [], "its words are not in the payload");
	q.markUnsentOrphans("c1", token);
	const snap = q.snapshot();
	assert.deepEqual(snap.retained, [{ id: a, durationS: 42, text: "words the user deleted" }]);
	// Restore puts it back into the draft: a normal un-sent recording again.
	q.unorphan(a);
	assert.equal(q.snapshot().retained.length, 0);
	// Discard resolves it instead: audio dropped, guard cleared.
	q.markUnsentOrphans("c1", []);
	q.discard(a);
	assert.ok(!mirror.has("s1#0"), "Discard drops the audio the user chose to lose");
	assert.equal(q.hasUnfinishedReason(), null, "…and never leaves a forever-armed guard");
});

// ── (5) the honest "your message went without this" state ────────────────────────────────────
test("markSentWithout flags a failed recording a send left behind, and downgrades the guard", async () => {
	const tx = makeTranscriber();
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror: makeMirror(),
		retainUntilSent: true,
	});
	const id = dictate(q, { conversationId: "c1", fragments: ["a"], durationS: 12 });
	await flush();
	tx.reject(id);
	await flush();
	tx.reject(id);
	await flush();
	assert.deepEqual(q.failedIdsForScope("c1"), [id], "the composer reads these BEFORE the POST");
	assert.deepEqual(q.failedIdsForScope("other"), [], "…scoped to the conversation being sent");
	assert.equal(q.snapshot().failed[0].sentWithout, false, "pre-send: still fixable in place");
	q.markSentWithout(id);
	assert.equal(
		q.snapshot().failed[0].sentWithout,
		true,
		"post-send: the chip must be able to say the message went without it"
	);
	// A `done` recording can never be sent-WITHOUT — its words WERE in the payload.
	const other = dictate(q, { conversationId: "c1", fragments: ["b"] });
	await flush();
	tx.resolve(other, "spoken");
	await flush();
	q.markSentWithout(other);
	assert.equal(
		q.snapshot().failed.length,
		1,
		"…so the flag never lands on a transcribed recording"
	);
});

test("the leave guard separates a LIVE loss risk from a merely unresolved one", async () => {
	const tx = makeTranscriber();
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror: makeMirror(),
		retainUntilSent: true,
	});
	assert.equal(q.hasUnfinishedReason(), null, "nothing outstanding");
	const id = q.begin({ conversationId: "c1" });
	assert.equal(q.hasUnfinishedReason(), "live", "a take being spoken is a real risk");
	q.addFragment(id, { index: 0, blob: "a", durationS: 15 });
	q.finish(id, { blob: "a", durationS: 15 });
	await flush();
	assert.equal(q.hasUnfinishedReason(), "live", "…and so is one mid-transcription");
	tx.reject(id);
	await flush();
	tx.reject(id);
	await flush();
	assert.equal(
		q.hasUnfinishedReason(),
		"unresolved",
		"a terminal, durably-mirrored failure loses NOTHING on navigation — warning about it cries wolf"
	);
	assert.equal(
		q.hasUnfinished(),
		true,
		"…but it IS still outstanding for callers that need that"
	);
});

// ── (6) durability is observed, never assumed ────────────────────────────────────────────────
test("an unconfirmable fragment write fires onPersistFail ONCE, arms the guard, and Retry clears it", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const persistFails = [];
	mirror.setFailWrite(() => true); // the store is full / private mode / IndexedDB blocked
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		mirrorPutAttempts: 2,
		onPersistFail: (id) => persistFails.push(id),
	});
	const id = q.begin({ conversationId: "c1" });
	q.addFragment(id, { index: 0, blob: "a", durationS: 15 });
	q.addFragment(id, { index: 1, blob: "b", durationS: 30 });
	await flush();
	await flush();
	assert.deepEqual(persistFails, [id], "the composer is told ONCE, not once per fragment");
	assert.deepEqual(
		q.snapshot().unpersisted,
		[{ id, durationS: 30 }],
		"…and the chip is actionable"
	);
	assert.equal(q.hasUnfinishedReason(), "live", "un-persistable audio is a genuine loss risk");

	mirror.setFailWrite(null); // the user freed space
	q.retryPersist(id);
	await flush();
	await flush();
	assert.deepEqual(q.snapshot().unpersisted, [], "a landed Retry clears the chip");
	assert.deepEqual(
		mirror.fragmentsOf("s1#0"),
		[0, 1],
		"…and both fragments are finally durable"
	);
});

test("with no mirror injected there is no persistence gating at all", async () => {
	const tx = makeTranscriber();
	const q = createVoiceDictationStore({ transcribe: tx.fn });
	const id = dictate(q, { fragments: ["a"] });
	await flush();
	assert.deepEqual(q.snapshot().unpersisted, []);
	tx.resolve(id, "fine");
	await flush();
	assert.equal(q.hasUnfinishedReason(), null);
});

// ── (7) recovery, scope migration, tombstones, dispose ───────────────────────────────────────
test("recover() adopts a prior-session recording atomically and transcribes it whole", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	// Two fragments a crashed session left behind, under ITS recording key.
	mirror.store.set("old#3:0", { recordingId: "old#3", index: 0, blob: "AAA" });
	mirror.store.set("old#3:1", { recordingId: "old#3", index: 1, blob: "BBB" });
	const commits = [];
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		onCommit: (id, text, rec) => commits.push([text, rec.conversationId]),
	});
	const [id] = q.recover([
		{
			blob: "AAABBB", // the caller reassembled the fragments in index order
			durationS: 27,
			mimeType: "audio/webm",
			conversationId: "convSpoken",
			_adoptKeys: ["old#3:0", "old#3:1"],
		},
	]);
	await flush();
	assert.equal(tx.calls[0].blob, "AAABBB", "the recovered take is transcribed as ONE recording");
	assert.ok(!mirror.store.has("old#3:0"), "the superseded prior-session fragments are gone…");
	assert.ok(mirror.has("s1#0"), "…and the adopted copy landed — in ONE transaction");
	tx.resolve(id, "what I said last time");
	await flush();
	assert.deepEqual(
		commits,
		[["what I said last time", "convSpoken"]],
		"a recovered transcript routes to the chat it was SPOKEN in, never the one on screen"
	);
});

test("reassignScope moves a retained recording + its mirror off the new-chat sentinel", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceDictationStore({ transcribe: tx.fn, mirror, retainUntilSent: true });
	const id = dictate(q, { conversationId: "__sentinel__", fragments: ["a"] });
	await flush();
	tx.resolve(id, "hello");
	await flush();
	assert.deepEqual(
		q.captureSentInPayload("real-1", "hello"),
		[],
		"not releasable under the real id yet"
	);
	q.reassignScope("__sentinel__", "real-1");
	assert.deepEqual(
		q.captureSentInPayload("real-1", "hello"),
		[id],
		"after promotion the real conversation's send releases it"
	);
	assert.ok(
		mirror.log.some((l) => l[0] === "reassign" && l[2] === "real-1"),
		"the mirror follows, so a reload recovers it into the right conversation"
	);
});

test("discarding a recording mid-flight is a tombstone: the cursor never wedges", async () => {
	const tx = makeTranscriber();
	const commits = [];
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror: makeMirror(),
		onCommit: (id, text) => commits.push(text),
	});
	const a = dictate(q, { fragments: ["a"] });
	const b = dictate(q, { fragments: ["b"] });
	await flush();
	q.discard(a); // the user gave up on the first take while it was still in flight
	tx.resolve(b, "second");
	await flush();
	assert.deepEqual(commits, ["second"], "the later transcript still flushes across the hole");
	tx.resolve(a, "late resurrection attempt");
	await flush();
	assert.deepEqual(
		commits,
		["second"],
		"a late resolution never resurrects a discarded recording"
	);
	assert.equal(q.snapshot().total, 1, "…and the tombstone is invisible to the UI");
});

test("after dispose(), a late transcription fires no onCommit", async () => {
	const tx = makeTranscriber();
	const commits = [];
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror: makeMirror(),
		onCommit: (id, text) => commits.push(text),
	});
	const id = dictate(q, { fragments: ["a"] });
	await flush();
	q.dispose();
	tx.resolve(id, "too late");
	await flush();
	assert.deepEqual(commits, [], "a dead composer is never poked");
});

test("get() joins the fragments of a take still being spoken, so Download is never empty", () => {
	const q = createVoiceDictationStore({
		transcribe: () => Promise.resolve(""),
		mirror: makeMirror(),
	});
	const id = q.begin({ conversationId: "c" });
	q.addFragment(id, { index: 0, blob: "AAA", durationS: 15 });
	const rec = q.get(id);
	assert.equal(rec.blob, null, "the assembled blob only exists after finish()");
	assert.deepEqual(
		rec.fragments.map((f) => f.blob),
		["AAA"],
		"…but the captured fragments are readable, which is what the composer downloads"
	);
	q.discard(id);
	assert.equal(q.get(id), null, "a discarded recording exposes nothing");
});
