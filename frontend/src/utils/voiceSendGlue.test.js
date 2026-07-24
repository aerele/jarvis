// ChatView-level INTEGRATION tests for the composer↔queue↔send glue (voiceSendGlue.js), the seam
// Codex round 3 flagged: the round-2 tests were queue-unit-level and never exercised send-rejection
// or conversation-id promotion, which is exactly where the three HIGH never-lose-audio bugs lived.
//
// The ChatView single-file component can't be mounted under `node --test` (no DOM, no Vue harness,
// hundreds of deps), so — per the round-3 disposition's explicit fallback — the send/ack/promote/
// resend logic was FACTORED into pure helpers (voiceSendGlue.js: promoteNewChatScope, planRejectedSend)
// plus a payload-bound queue method (voiceChunkQueue.js: captureSentInPayload). ChatView imports and
// calls exactly these, so the tests below drive the REAL code, not a reimplementation. Each test
// stitches the helpers to the REAL queue (the source of truth for the audio lifecycle) + a mock send
// outcome, reproducing the ChatView send flow end to end:
//   * R3-1 — composer-edit-before-send: a clip edited/deleted out of the payload is NOT acknowledged.
//   * R3-2 — id-less send success with a mid-flight (retry) commit: the late clip is migrated to the
//            real id and released there, never stranded under the new-chat sentinel.
//   * R3-3 — failed-bubble resend rejection across ok:false / usage_limit / subscription_suspended /
//            single-flight: a bubble carrying the SAME voiceAck survives, audio retained, resendable.
//
// Run: `node --test voiceSendGlue.test.js`, or via the python suite
// (jarvis/tests/test_voice_send_glue_client.py subprocess-runs it so the contract holds every CI run).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createVoiceChunkQueue } from "./voiceChunkQueue.js";
import { promoteNewChatScope, planRejectedSend } from "./voiceSendGlue.js";

const SENTINEL = "__jarvis_new_chat__";
const flush = () => new Promise((r) => setTimeout(r, 0));

// A controllable transcribe: parks each call until the test settles it by seq (same shape as the
// queue-unit suite), so out-of-order / retry timing is fully deterministic.
function makeTranscriber() {
	const calls = [];
	const fn = (clip) =>
		new Promise((resolve, reject) => {
			calls.push({ seq: clip.seq, settled: false, resolve, reject });
		});
	const _first = (seq) => calls.find((c) => c.seq === seq && !c.settled);
	return {
		fn,
		resolve(seq, text) {
			const c = _first(seq);
			assert.ok(c, `no in-flight transcribe for seq ${seq}`);
			c.settled = true;
			c.resolve(text);
		},
		reject(seq, err) {
			const c = _first(seq);
			assert.ok(c, `no in-flight transcribe for seq ${seq}`);
			c.settled = true;
			c.reject(err || new Error("boom"));
		},
	};
}

function makeMirror() {
	const store = new Map();
	return {
		store,
		put(clip) {
			store.set(clip.seq, clip);
		},
		delete(seq) {
			store.delete(seq);
		},
		all() {
			return Array.from(store.values());
		},
	};
}

// A faithful mini-model of ChatView's voice send glue, assembled from the SAME extracted pieces the
// component imports. It owns the composer scope + drafts + the recording take, wires the queue's
// onCommit to route a transcript to the live composer (when the clip's scope is on screen) or to that
// scope's stashed draft, and reproduces the send / rejection / id-adoption branches this round fixed.
function makeChat() {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const chat = {
		tx,
		mirror,
		currentId: null, // null == the id-less new-chat composer
		micConvId: null,
		drafts: {}, // scope -> stashed draft text
		input: "", // the live composer
		messages: [], // optimistic bubbles
		queue: null,
	};
	const scopeOf = () => chat.currentId || SENTINEL;
	const clipScope = (clip) =>
		clip && clip.conversationId != null ? clip.conversationId : SENTINEL;
	// Append a committed transcript to the composer target for the clip's scope (the live input when
	// that scope is on screen, else its stashed draft) — the _appendTranscript / _mutateComposerFor path.
	const append = (prev, t) => (prev.trim() ? prev.replace(/\s+$/, "") + " " + t : t);
	chat.queue = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 3,
		onCommit: (seq, text, clip) => {
			const t = (text || "").trim();
			if (!t) return;
			const scope = clipScope(clip);
			if (scope === scopeOf()) chat.input = append(chat.input, t);
			else chat.drafts[scope] = append(chat.drafts[scope] || "", t);
		},
	});
	chat.micConvId = scopeOf();

	// Switch the on-screen conversation — the loadConversation() path: stash the leaving scope's
	// live input into its draft, restore the target's, and REPLACE the rendered messages (the array
	// loadConversation swaps, which is exactly what drops an optimistic bubble kept only there).
	chat.navigateTo = (id) => {
		chat.drafts[scopeOf()] = chat.input;
		chat.currentId = id; // a real id, or null for the id-less new-chat composer
		chat.input = chat.drafts[scopeOf()] || "";
		chat.messages = []; // (no server store here — the switch tests don't need persisted rows)
	};

	// The ChatView send() path this round touched, driven by an injected `outcome` (the mock POST).
	//   textArg: undefined == a main-composer send (fromMain); a string == a programmatic send / resend.
	//   resendAck: the failed bubble's voiceAck (a resend); undefined otherwise.
	//   outcome: { ok:true, conversation_id? } | { ok:false, reason? } | { throw:true }
	//   midFlight: optional fn run AFTER the optimistic bubble is pushed but BEFORE the outcome is
	//     processed — models the user navigating / acting while the POST is in flight (VR4-2/3).
	chat.send = (textArg, resendAck, outcome, midFlight) => {
		const sentScope = scopeOf();
		const fromMain = typeof textArg !== "string";
		const text = fromMain ? chat.input : textArg;
		const voiceAck =
			resendAck || (fromMain ? chat.queue.captureSentInPayload(sentScope, text) : null);
		if (fromMain) chat.input = "";
		// VR4-1: a fromMain send clears the composer, so any committed clip in scope NOT in the
		// payload token (edited out) becomes an ACTIONABLE retained clip instead of a silent count.
		if (fromMain) chat.queue.markUnsentOrphans(sentScope, voiceAck);
		const tmp = {
			name: `tmp-${chat.messages.length}`,
			content: text,
			voiceAck: voiceAck && voiceAck.length ? voiceAck : undefined,
		};
		chat.messages.push(tmp);

		// The POST is now "in flight": let the test navigate away / act before the server replies.
		if (typeof midFlight === "function") midFlight();

		if (outcome && outcome.throw) {
			tmp.failed = true; // the catch path keeps the bubble (unchanged behaviour)
			return tmp;
		}
		if (outcome && outcome.ok === false) {
			const plan = planRejectedSend({ fromMain, bubbleVoiceAck: tmp.voiceAck });
			if (plan.keepBubble) {
				tmp.failed = true;
			} else {
				chat.messages = chat.messages.filter((m) => m.name !== tmp.name);
				if (plan.restoreText && !chat.input) chat.input = text;
			}
			return tmp;
		}
		// Accepted: release exactly the captured clips, then adopt a returned id (promoting the scope).
		if (voiceAck) chat.queue.acknowledge(voiceAck);
		if (outcome && outcome.conversation_id) {
			// Whether the user is STILL on the chat we sent from — captured now, before any mutation.
			const stillOnSentChat = scopeOf() === sentScope;
			// VR4-3: the sentinel→real-id scope/record/mirror/draft/take migration is
			// VISIBILITY-INDEPENDENT — run it whenever we sent from the sentinel and got a real id,
			// REGARDLESS of the on-screen chat, else a mid-send switch strands the sentinel clips.
			if (sentScope === SENTINEL && outcome.conversation_id !== SENTINEL)
				chat.micConvId = promoteNewChatScope({
					queue: chat.queue,
					drafts: chat.drafts,
					fromScope: SENTINEL,
					toId: outcome.conversation_id,
					takeScope: chat.micConvId,
				});
			// Only currentId is gated on visibility — never yank a user who switched away.
			if (stillOnSentChat && outcome.conversation_id !== chat.currentId)
				chat.currentId = outcome.conversation_id;
		}
		return tmp;
	};

	// resendFailed(bubble): drop the failed bubble, resend its text carrying the SAME voiceAck.
	chat.resendFailed = (bubble, outcome, midFlight) => {
		chat.messages = chat.messages.filter((m) => m.name !== bubble.name);
		return chat.send(bubble.content, bubble.voiceAck || null, outcome, midFlight);
	};
	return chat;
}

// ── planRejectedSend: the decision is reason-independent — a resend carrying a voiceAck is KEPT ──
test("planRejectedSend keeps a voice resend's bubble across EVERY rejection reason; main-send drops+restores", () => {
	const token = [0, 1];
	for (const reason of ["ok_false", "usage_limit", "subscription_suspended", "single_flight"]) {
		const plan = planRejectedSend({ fromMain: false, bubbleVoiceAck: token });
		assert.deepEqual(
			plan,
			{ keepBubble: true, restoreText: false },
			`a failed-bubble voice resend is preserved on rejection reason=${reason} (R3-3)`
		);
	}
	// A MAIN-composer send is unchanged from rounds 1/2: drop the bubble, restore its text.
	assert.deepEqual(
		planRejectedSend({ fromMain: true, bubbleVoiceAck: undefined }),
		{ keepBubble: false, restoreText: true },
		"a main-composer send drops the bubble and restores its text"
	);
	// A programmatic non-voice send (approval yes/no, form answers) drops the bubble, no restore.
	assert.deepEqual(
		planRejectedSend({ fromMain: false, bubbleVoiceAck: undefined }),
		{ keepBubble: false, restoreText: false },
		"a programmatic non-voice send drops its bubble as before — no spurious resendable bubble"
	);
	// An empty token is not "voice to protect".
	assert.deepEqual(planRejectedSend({ fromMain: false, bubbleVoiceAck: [] }), {
		keepBubble: false,
		restoreText: false,
	});
});

// ── promoteNewChatScope: migrates draft + take + queue records/mirror; idempotent + non-clobbering ─
test("promoteNewChatScope migrates draft ownership, the take scope, and queue records/mirror off the sentinel", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceChunkQueue({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 2,
	});
	q.enqueue({ blob: "a", durationS: 15, conversationId: SENTINEL });
	await flush();
	tx.resolve(0, "recovered words");
	await flush();

	const drafts = { [SENTINEL]: "typed new-chat draft" };
	const nextTake = promoteNewChatScope({
		queue: q,
		drafts,
		fromScope: SENTINEL,
		toId: "conv-real",
		takeScope: SENTINEL,
	});
	assert.equal(
		nextTake,
		"conv-real",
		"the active take is re-pointed so later clips enqueue under the real id"
	);
	assert.equal(
		drafts["conv-real"],
		"typed new-chat draft",
		"the sentinel draft becomes the real conversation's"
	);
	assert.ok(!(SENTINEL in drafts), "the stale sentinel draft key is dropped");
	assert.deepEqual(q.captureSent(SENTINEL), [], "no queue records remain under the sentinel");
	assert.deepEqual(
		q.captureSent("conv-real"),
		[0],
		"the record now belongs to the real conversation"
	);
	assert.ok(mirror.store.has(0), "migration never drops audio — the mirror record survives");

	// Non-clobbering + no-op guards.
	const d2 = { [SENTINEL]: "from sentinel", "conv-real": "already here" };
	promoteNewChatScope({
		queue: null,
		drafts: d2,
		fromScope: SENTINEL,
		toId: "conv-real",
		takeScope: "x",
	});
	assert.equal(d2["conv-real"], "already here", "an existing target draft is never clobbered");
	assert.ok(!(SENTINEL in d2), "the sentinel key is still cleared");
	const keepTake = promoteNewChatScope({
		queue: q,
		drafts: {},
		fromScope: SENTINEL,
		toId: "",
		takeScope: "keep",
	});
	assert.equal(keepTake, "keep", "a falsy toId is a no-op (take unchanged)");
});

// ── (R3-1) INTEGRATION: composer-edit-before-send — a clip edited out is never acknowledged ─────
test("R3-1 integration: a clip whose transcript the user edits/deletes out of the composer before sending keeps its audio", async () => {
	const chat = makeChat();
	// Two clips dictated into the (id-less) new-chat composer; both commit into the live input.
	chat.queue.enqueue({ blob: "a", durationS: 15, conversationId: SENTINEL });
	chat.queue.enqueue({ blob: "b", durationS: 15, conversationId: SENTINEL });
	await flush();
	chat.tx.resolve(0, "alpha words");
	chat.tx.resolve(1, "bravo words");
	await flush();
	assert.equal(chat.input, "alpha words bravo words", "both transcripts landed in the composer");

	// The user DELETES clip B's words before sending, leaving only clip A's text.
	chat.input = "alpha words";
	const bubble = chat.send(undefined, undefined, { ok: true, conversation_id: "conv-1" });

	assert.deepEqual(
		bubble.voiceAck,
		[0],
		"only clip A (present in the payload) is in the release token"
	);
	assert.ok(!chat.mirror.store.has(0), "clip A — its text was sent — is released");
	assert.ok(chat.mirror.store.has(1), "clip B — edited out — is RETAINED (audio never lost)");
	assert.equal(
		chat.queue.hasUnfinished(),
		true,
		"the retained clip keeps the leave guard armed + actionable"
	);
});

// ── (R3-2) INTEGRATION: an id-less send, then a retry commits AFTER id-adoption — not stranded ──
test("R3-2 integration: a failed clip retried AFTER an id-less send's id-adoption commits under the REAL id and is releasable", async () => {
	const chat = makeChat();
	// A clip dictated in the new-chat composer FAILS (both attempts reject) → a retained failed clip.
	const q = createVoiceChunkQueue({
		transcribe: chat.tx.fn,
		mirror: chat.mirror,
		retainUntilSent: true,
		concurrency: 2,
		maxAttempts: 1,
		onCommit: (seq, text, clip) => {
			// route like ChatView: live input when the clip's (possibly migrated) scope is on screen
			const scope = clip && clip.conversationId != null ? clip.conversationId : SENTINEL;
			const cur = chat.currentId || SENTINEL;
			const t = (text || "").trim();
			if (!t) return;
			if (scope === cur) chat.input = chat.input.trim() ? chat.input + " " + t : t;
			else chat.drafts[scope] = (chat.drafts[scope] ? chat.drafts[scope] + " " : "") + t;
		},
	});
	chat.queue = q;
	q.enqueue({ blob: "later", durationS: 15, conversationId: SENTINEL });
	await flush();
	chat.tx.reject(0, new Error("stt down")); // maxAttempts 1 → straight to failed
	await flush();
	assert.equal(q.snapshot().failed.length, 1, "the clip is a retained FAILED clip (its chip)");

	// The user also typed text and SENDS the id-less new chat (the failed clip does not block send).
	chat.input = "typed message";
	chat.send(undefined, undefined, { ok: true, conversation_id: "conv-real" });
	assert.equal(chat.currentId, "conv-real", "the new chat adopted its real id");
	assert.deepEqual(
		q.captureSent(SENTINEL),
		[],
		"the failed clip was migrated OFF the sentinel by id-adoption"
	);

	// Now the user hits Retry on the failed clip; it succeeds and commits AFTER the id switch.
	// (The send cleared the composer, so the late words land in the real conversation's now-empty
	// live composer — the point is they route to the REAL id, never a stranded sentinel draft.)
	q.retry(0);
	await flush();
	chat.tx.resolve(0, "the late words");
	await flush();
	assert.equal(
		chat.input,
		"the late words",
		"the late retry routes to the LIVE composer of the real conversation"
	);
	assert.ok(!chat.drafts[SENTINEL], "nothing was stranded in a sentinel draft (the R3-2 leak)");
	// A follow-up send from the real conversation releases the migrated clip — it was never stranded.
	assert.deepEqual(
		q.captureSent("conv-real"),
		[0],
		"the migrated clip is releasable under the real id"
	);
	chat.send(undefined, undefined, { ok: true });
	assert.ok(
		!chat.mirror.store.has(0),
		"the real-scope send releases it — no forever-armed guard"
	);
	assert.equal(
		q.hasUnfinished(),
		false,
		"guard clears end to end — the clip was never stranded (R3-2)"
	);
});

// ── (R3-3) INTEGRATION: failed-bubble resend rejected across all four reasons keeps token + audio ─
test("R3-3 integration: a failed-bubble resend rejected (ok:false / usage_limit / subscription_suspended / single-flight) keeps a bubble with the SAME token; audio retained; resendable", async () => {
	for (const reason of [undefined, "usage_limit", "subscription_suspended", "single_flight"]) {
		const chat = makeChat();
		// A clip is dictated + committed into a real conversation, then the FIRST send fails outright.
		chat.currentId = "convR";
		chat.micConvId = "convR";
		chat.queue.enqueue({ blob: "a", durationS: 15, conversationId: "convR" });
		await flush();
		chat.tx.resolve(0, "spoken words");
		await flush();
		chat.input = "spoken words";
		// The first send THROWS (a 500) → the catch path keeps the bubble as failed carrying its token.
		const failedBubble = chat.send(undefined, undefined, { throw: true });
		assert.ok(
			failedBubble.failed,
			"the first send failed — a failed bubble carries the token"
		);
		assert.deepEqual(
			failedBubble.voiceAck,
			[0],
			"the failed bubble carries the payload-bound token"
		);
		assert.ok(
			chat.mirror.store.has(0),
			"audio still retained after the failed send (nothing acknowledged)"
		);
		assert.equal(
			chat.queue.hasUnfinished(),
			true,
			"guard armed — the words are not durably sent"
		);

		// The user hits Resend and it is REJECTED with this reason.
		const resendBubble = chat.resendFailed(failedBubble, { ok: false, reason });
		assert.ok(
			chat.messages.some((m) => m.name === resendBubble.name && m.failed),
			`the rejected resend KEEPS a failed bubble (reason=${reason}) — not dropped (R3-3)`
		);
		assert.deepEqual(
			resendBubble.voiceAck,
			[0],
			"the preserved bubble carries the SAME voiceAck so the user can resend again"
		);
		assert.ok(
			chat.mirror.store.has(0),
			"audio STILL retained across the rejected resend — never lost"
		);
		assert.equal(
			chat.queue.hasUnfinished(),
			true,
			"guard still armed, but with an ACTION (resend the bubble)"
		);

		// Resend once more; this time it succeeds → the clip is finally released, guard clears.
		chat.resendFailed(resendBubble, { ok: true });
		assert.ok(
			!chat.mirror.store.has(0),
			"a successful resend finally releases the delivered clip"
		);
		assert.equal(
			chat.queue.hasUnfinished(),
			false,
			"guard clears — no forever-armed `done` record (R3-3)"
		);
	}
});

// ── (VR4-1) INTEGRATION: a clip edited out of a MAIN send becomes an ACTIONABLE retained clip;
//        Download exposes the audio and Discard clears the guard (no forever-armed done record). ─
test("VR4-1 integration: an edited-out clip surfaces as a retained (actionable) clip after a send; Discard clears the guard", async () => {
	const chat = makeChat();
	chat.queue.enqueue({ blob: "a", durationS: 15, conversationId: SENTINEL });
	chat.queue.enqueue({ blob: "b", durationS: 15, conversationId: SENTINEL });
	await flush();
	chat.tx.resolve(0, "alpha words");
	chat.tx.resolve(1, "bravo words");
	await flush();
	assert.deepEqual(
		chat.queue.snapshot().retained,
		[],
		"nothing retained while both are in the draft"
	);

	// The user DELETES clip B's words, then sends → B can no longer be released by a payload match.
	chat.input = "alpha words";
	chat.send(undefined, undefined, { ok: true, conversation_id: "conv-1" });

	const snap = chat.queue.snapshot();
	assert.equal(
		snap.retained.length,
		1,
		"the edited-out clip B is surfaced as an ACTIONABLE retained clip"
	);
	assert.equal(snap.retained[0].seq, 1, "it is clip B");
	assert.equal(
		snap.retained[0].text,
		"bravo words",
		"the retained entry carries B's transcript for Restore"
	);
	assert.ok(chat.mirror.store.has(1), "B's audio is still retained (never lost)");
	assert.ok(chat.queue.getClip(1), "Download exposes the audio (getClip returns B's blob)");
	assert.equal(
		chat.queue.hasUnfinished(),
		true,
		"the guard is armed — but now with a visible resolution"
	);
	// clip A (sent) was released, and B rode the sentinel→real-id promotion still flagged retained.
	assert.ok(!chat.mirror.store.has(0), "clip A (in the payload) was released");
	assert.equal(
		chat.currentId,
		"conv-1",
		"the new chat adopted its real id (B migrated with it)"
	);

	// Discard the retained clip: guard clears, audio dropped — the VR4-1 forever-armed gap is closed.
	chat.queue.discard(1);
	assert.deepEqual(
		chat.queue.snapshot().retained,
		[],
		"Discard removes it from the retained list"
	);
	assert.equal(
		chat.queue.hasUnfinished(),
		false,
		"Discard clears the guard — no forever-armed clip"
	);
	assert.ok(!chat.mirror.store.has(1), "and drops the audio mirror");
});

// ── (VR4-3) INTEGRATION: switching chats DURING an id-less send still promotes the sentinel scope,
//        so a later retry routes to the REAL conversation and is releasable — never stranded. ─────
test("VR4-3 integration: a mid-send chat switch still promotes the sentinel — a retried clip routes to the real id and releases", async () => {
	const chat = makeChat();
	// A clip dictated in the new-chat composer FAILS (both attempts reject) → a retained failed clip.
	chat.queue.enqueue({ blob: "later", durationS: 15, conversationId: SENTINEL });
	await flush();
	chat.tx.reject(0, new Error("stt down 1"));
	await flush();
	chat.tx.reject(0, new Error("stt down 2"));
	await flush();
	assert.equal(chat.queue.snapshot().failed.length, 1, "the clip is a retained FAILED clip");

	// The user types + sends the id-less new chat, but SWITCHES conversations while the POST is
	// in flight. The server still created a real conversation and returned its id.
	chat.input = "typed message";
	chat.send(undefined, undefined, { ok: true, conversation_id: "conv-real" }, () => {
		chat.navigateTo("other-conv"); // mid-POST conversation switch
	});
	assert.equal(chat.currentId, "other-conv", "we were NOT yanked back to the chat we sent from");
	assert.deepEqual(
		chat.queue.captureSent(SENTINEL),
		[],
		"the failed clip was migrated OFF the sentinel despite the switch (VR4-3)"
	);
	assert.ok(!chat.drafts[SENTINEL], "no stale sentinel draft left behind");

	// The user hits Retry on the failed clip; it now commits under the REAL id. Since conv-real is
	// off-screen, the late words land in ITS draft — routed to the real conversation, not stranded.
	chat.queue.retry(0);
	await flush();
	chat.tx.resolve(0, "the late words");
	await flush();
	assert.equal(
		chat.drafts["conv-real"],
		"the late words",
		"the retry routed to the REAL conversation's draft, not a stranded sentinel (VR4-3)"
	);
	assert.deepEqual(
		chat.queue.captureSent("conv-real"),
		[0],
		"the migrated clip is releasable under the real id"
	);

	// Return to conv-real and send: the real-scope send releases the clip — the guard clears.
	chat.navigateTo("conv-real");
	assert.equal(
		chat.input,
		"the late words",
		"returning restores conv-real's draft into the composer"
	);
	chat.send(undefined, undefined, { ok: true });
	assert.ok(
		!chat.mirror.store.has(0),
		"the real-scope send releases it — never stranded (VR4-3)"
	);
	assert.equal(chat.queue.hasUnfinished(), false, "guard clears end to end");
});
