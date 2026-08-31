// ChatView-level INTEGRATION tests for the composer↔store↔send glue (voiceSendGlue.js), the seam
// Codex round 3 flagged: the earlier tests were unit-level and never exercised send-rejection or
// conversation-id promotion, which is exactly where the three HIGH never-lose-audio bugs lived.
//
// The ChatView single-file component can't be mounted under `node --test` (no DOM, no Vue harness,
// hundreds of deps), so — per the round-3 disposition's explicit fallback — the send/ack/promote/
// resend logic was FACTORED into pure helpers (voiceSendGlue.js: promoteNewChatScope,
// planRejectedSend) plus a payload-bound store method (voiceDictationStore.js:
// captureSentInPayload). ChatView imports and calls exactly these, so the tests below drive the REAL
// code, not a reimplementation. Each test stitches the helpers to the REAL dictation store (the
// source of truth for the audio lifecycle) + a mock send outcome, reproducing the ChatView send flow
// end to end:
//   * R3-1 — composer-edit-before-send: a recording edited/deleted out of the payload is NOT acknowledged.
//   * R3-2 — id-less send success with a mid-flight (retry) commit: the late recording is migrated to
//            the real id and released there, never stranded under the new-chat sentinel.
//   * R3-3 — failed-bubble resend rejection across ok:false / usage_limit / subscription_suspended /
//            single-flight: a bubble carrying the SAME voiceAck survives, audio retained, resendable.
//   * jarvis#496 — the discuss-in-chat / ground-in-wiki one-shot SEND CONTEXT is the SAME bug class:
//            it must survive a rejected or thrown send so a retry still carries it, consumed only
//            once the server actually accepts. The mini-model below is extended (groundWiki,
//            prefillContext, sendCtx) to reproduce that slice of ChatView's send(), and a source-pin
//            test ties the fix's exact placement to the real ChatView.vue file.
//
// A dictation is now ONE recording transcribed in ONE call, so each take below is begin → one
// timeslice fragment → finish, exactly as useDictationRecorder drives it.
//
// Run: `node --test voiceSendGlue.test.js`, or via the python suite
// (jarvis/tests/test_voice_send_glue_client.py subprocess-runs it so the contract holds every CI run).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createVoiceDictationStore } from "./voiceDictationStore.js";
import {
	promoteNewChatScope,
	planRejectedSend,
	createPendingSends,
	injectPendingBubbles,
} from "./voiceSendGlue.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const chatViewSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

const SENTINEL = "__jarvis_new_chat__";
const flush = () => new Promise((r) => setTimeout(r, 0));

// A controllable transcribe: parks each call until the test settles it by recording id (same shape
// as the store-unit suite), so retry timing is fully deterministic.
function makeTranscriber() {
	const calls = [];
	const fn = (rec) =>
		new Promise((resolve, reject) => {
			calls.push({ id: rec.id, settled: false, resolve, reject });
		});
	const _first = (id) => calls.find((c) => c.id === id && !c.settled);
	return {
		fn,
		resolve(id, text) {
			const c = _first(id);
			assert.ok(c, `no in-flight transcribe for recording ${id}`);
			c.settled = true;
			c.resolve(text);
		},
		reject(id, err) {
			const c = _first(id);
			assert.ok(c, `no in-flight transcribe for recording ${id}`);
			c.settled = true;
			c.reject(err || new Error("boom"));
		},
	};
}

// In-memory stand-in for the IndexedDB fragment mirror. `has(id)` asks the question every test
// below really asks: is this recording's audio still saved?
function makeMirror() {
	const store = new Map(); // `${recordingId}:${index}` -> fragment
	return {
		store,
		recordingKey: (id) => `r${id}`,
		putFragment(f) {
			store.set(`${f.recordingId}:${f.index}`, f);
			return Promise.resolve(true);
		},
		adopt(r) {
			for (const k of r._adoptKeys || []) store.delete(k);
			store.set(`${r.recordingId}:0`, r);
			return Promise.resolve(true);
		},
		deleteRecording(rid) {
			for (const k of Array.from(store.keys())) if (k.startsWith(rid + ":")) store.delete(k);
			return Promise.resolve(true);
		},
		reassignConversation(rid, scope) {
			for (const [k, v] of store)
				if (String(v.recordingId) === rid) store.set(k, { ...v, conversationId: scope });
			return Promise.resolve(true);
		},
		has(id) {
			return Array.from(store.keys()).some((k) => k.startsWith(`r${id}:`));
		},
	};
}

// Drive one whole take exactly as useDictationRecorder does: begin, a timeslice fragment, finish
// with the assembled bytes. Returns the recording id the store stamped.
function dictate(store, { blob = "a", durationS = 15, conversationId = null } = {}) {
	const id = store.begin({ conversationId });
	store.addFragment(id, { index: 0, blob, durationS });
	store.finish(id, { blob, durationS });
	return id;
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
		pending: createPendingSends(), // VR4-2: origin-scoped failed bubbles (survive nav)
		// jarvis#496: the one-shot SEND CONTEXT (groundNextTurn's ground_wiki flag, or a
		// discuss-in-chat prefill) — test-settable, mirroring groundNextTurn.value / the module-scope
		// _prefillSendContext ChatView.vue carries across renders.
		groundWiki: false,
		prefillContext: null,
	};
	let _bubbleSeq = 0; // stable bubble names (a switch clears messages, so length would collide)
	const scopeOf = () => chat.currentId || SENTINEL;
	const recScope = (rec) => (rec && rec.conversationId != null ? rec.conversationId : SENTINEL);
	// Append a committed transcript to the composer target for the recording's scope (the live input
	// when that scope is on screen, else its stashed draft) — the _appendTranscript path.
	const append = (prev, t) => (prev.trim() ? prev.replace(/\s+$/, "") + " " + t : t);
	chat.queue = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 3,
		onCommit: (id, text, rec) => {
			const t = (text || "").trim();
			if (!t) return;
			const scope = recScope(rec);
			if (scope === scopeOf()) chat.input = append(chat.input, t);
			else chat.drafts[scope] = append(chat.drafts[scope] || "", t);
		},
	});
	chat.micConvId = scopeOf();
	// Record a whole take into whatever store `chat.queue` currently is (one test swaps it).
	chat.dictate = (opts) => dictate(chat.queue, opts);

	// Switch the on-screen conversation — the loadConversation() path: stash the leaving scope's
	// live input into its draft, restore the target's, and REPLACE the rendered messages (the array
	// loadConversation swaps, which is exactly what drops an optimistic bubble kept only there).
	chat.navigateTo = (id) => {
		chat.drafts[scopeOf()] = chat.input;
		chat.currentId = id; // a real id, or null for the id-less new-chat composer
		chat.input = chat.drafts[scopeOf()] || "";
		chat.messages = []; // (no server store here — the switch tests don't need persisted rows)
		// VR4-2: loadConversation re-injects this scope's pending failed bubbles (dedupe by name),
		// so a rejected send that happened while off-screen reappears with its token on return.
		const have = new Set(chat.messages.map((m) => m.name));
		for (const b of chat.pending.peek(scopeOf())) if (!have.has(b.name)) chat.messages.push(b);
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
		// jarvis#496: sendCtx is COMPUTED here (same as ChatView.vue) but chat.prefillContext is NOT
		// cleared yet — only the accepted tail below may consume it, so a rejected/thrown send
		// leaves it armed for a retry.
		const groundWiki = chat.groundWiki;
		const sendCtx = groundWiki ? { ground_wiki: 1 } : chat.prefillContext || undefined;
		const voiceAck =
			resendAck || (fromMain ? chat.queue.captureSentInPayload(sentScope, text) : null);
		if (fromMain) chat.input = "";
		// A fromMain send clears the composer, so any transcribed recording in scope NOT in the
		// payload token (edited out) becomes an ACTIONABLE retained chip instead of a silent count.
		if (fromMain) chat.queue.markUnsentOrphans(sentScope, voiceAck);
		const tmp = {
			name: `tmp-${_bubbleSeq++}`,
			content: text,
			voiceAck: voiceAck && voiceAck.length ? voiceAck : undefined,
			sendCtx, // jarvis#496: the context this particular attempt carried
		};
		chat.messages.push(tmp);

		// The POST is now "in flight": let the test navigate away / act before the server replies.
		if (typeof midFlight === "function") midFlight();

		// VR4-2: keep a failed bubble ORIGIN-scoped so it (and its token) survive a mid-send switch;
		// show it now only if the origin is still on screen. loadConversation re-injects on return.
		const _keepFailed = () => {
			tmp.failed = true;
			chat.pending.add(sentScope, tmp);
			if (scopeOf() === sentScope && !chat.messages.some((m) => m.name === tmp.name))
				chat.messages.push(tmp);
		};

		if (outcome && outcome.throw) {
			_keepFailed(); // the catch path keeps the bubble (now origin-scoped — VR4-2)
			return tmp;
		}
		if (outcome && outcome.ok === false) {
			const originOnScreen = scopeOf() === sentScope;
			const plan = planRejectedSend({ fromMain, bubbleVoiceAck: tmp.voiceAck });
			if (plan.keepBubble) {
				_keepFailed();
			} else {
				// Drop the bubble; restore its text to the ORIGIN composer (on screen) or draft (off).
				if (originOnScreen) {
					chat.messages = chat.messages.filter((m) => m.name !== tmp.name);
					if (plan.restoreText && !chat.input) chat.input = text;
				} else if (plan.restoreText && !chat.drafts[sentScope]) {
					chat.drafts[sentScope] = text;
				}
			}
			return tmp;
		}
		// Accepted — jarvis#496: the one-shot send context is consumed ONLY here, mirroring
		// ChatView.vue's `if (groundWiki) groundNextTurn.value = false;` placement exactly.
		chat.prefillContext = null;
		// Accepted: release exactly the captured recordings, then adopt a returned id (promoting the scope).
		if (voiceAck) chat.queue.acknowledge(voiceAck);
		if (outcome && outcome.conversation_id) {
			// Whether the user is STILL on the chat we sent from — captured now, before any mutation.
			const stillOnSentChat = scopeOf() === sentScope;
			// VR4-3: the sentinel→real-id scope/record/mirror/draft/take migration is
			// VISIBILITY-INDEPENDENT — run it whenever we sent from the sentinel and got a real id,
			// REGARDLESS of the on-screen chat, else a mid-send switch strands the sentinel recordings.
			if (sentScope === SENTINEL && outcome.conversation_id !== SENTINEL) {
				chat.micConvId = promoteNewChatScope({
					queue: chat.queue,
					drafts: chat.drafts,
					fromScope: SENTINEL,
					toId: outcome.conversation_id,
					takeScope: chat.micConvId,
				});
				// VR4-2: a failed bubble queued under the sentinel follows the promoted conversation.
				chat.pending.reassign(SENTINEL, outcome.conversation_id);
			}
			// Only currentId is gated on visibility — never yank a user who switched away.
			if (stillOnSentChat && outcome.conversation_id !== chat.currentId)
				chat.currentId = outcome.conversation_id;
		}
		return tmp;
	};

	// resendFailed(bubble): drop the failed bubble (from messages AND the pending store — it is being
	// REPLACED by a fresh send), then resend its text carrying the SAME voiceAck.
	chat.resendFailed = (bubble, outcome, midFlight) => {
		chat.messages = chat.messages.filter((m) => m.name !== bubble.name);
		chat.pending.remove(scopeOf(), bubble.name);
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

// ── promoteNewChatScope: migrates draft + take + store records/mirror; idempotent + non-clobbering ─
test("promoteNewChatScope migrates draft ownership, the take scope, and store records/mirror off the sentinel", async () => {
	const tx = makeTranscriber();
	const mirror = makeMirror();
	const q = createVoiceDictationStore({
		transcribe: tx.fn,
		mirror,
		retainUntilSent: true,
		concurrency: 2,
	});
	dictate(q, { blob: "a", conversationId: SENTINEL });
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
		"the active take is re-pointed so a later recording is admitted under the real id"
	);
	assert.equal(
		drafts["conv-real"],
		"typed new-chat draft",
		"the sentinel draft becomes the real conversation's"
	);
	assert.ok(!(SENTINEL in drafts), "the stale sentinel draft key is dropped");
	assert.deepEqual(q.captureSent(SENTINEL), [], "no store records remain under the sentinel");
	assert.deepEqual(
		q.captureSent("conv-real"),
		[0],
		"the record now belongs to the real conversation"
	);
	assert.ok(mirror.has(0), "migration never drops audio — the mirrored fragments survive");

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

// ── (R3-1) INTEGRATION: composer-edit-before-send — an edited-out recording is never acknowledged ─
test("R3-1 integration: a recording whose transcript the user edits/deletes out of the composer before sending keeps its audio", async () => {
	const chat = makeChat();
	// Two takes dictated into the (id-less) new-chat composer; both commit into the live input.
	chat.dictate({ blob: "a", conversationId: SENTINEL });
	chat.dictate({ blob: "b", conversationId: SENTINEL });
	await flush();
	chat.tx.resolve(0, "alpha words");
	chat.tx.resolve(1, "bravo words");
	await flush();
	assert.equal(chat.input, "alpha words bravo words", "both transcripts landed in the composer");

	// The user DELETES recording B's words before sending, leaving only recording A's text.
	chat.input = "alpha words";
	const bubble = chat.send(undefined, undefined, { ok: true, conversation_id: "conv-1" });

	assert.deepEqual(
		bubble.voiceAck,
		[0],
		"only recording A (present in the payload) is in the release token"
	);
	assert.ok(!chat.mirror.has(0), "recording A — its text was sent — is released");
	assert.ok(chat.mirror.has(1), "recording B — edited out — is RETAINED (audio never lost)");
	assert.equal(
		chat.queue.hasUnfinished(),
		true,
		"the retained recording keeps the leave guard armed + actionable"
	);
});

// ── (R3-2) INTEGRATION: an id-less send, then a retry commits AFTER id-adoption — not stranded ──
test("R3-2 integration: a failed recording retried AFTER an id-less send's id-adoption commits under the REAL id and is releasable", async () => {
	const chat = makeChat();
	// A take dictated in the new-chat composer FAILS → a retained failed recording.
	const q = createVoiceDictationStore({
		transcribe: chat.tx.fn,
		mirror: chat.mirror,
		retainUntilSent: true,
		concurrency: 2,
		maxAttempts: 1,
		onCommit: (id, text, rec) => {
			// route like ChatView: live input when the recording's (possibly migrated) scope is on screen
			const scope = rec && rec.conversationId != null ? rec.conversationId : SENTINEL;
			const cur = chat.currentId || SENTINEL;
			const t = (text || "").trim();
			if (!t) return;
			if (scope === cur) chat.input = chat.input.trim() ? chat.input + " " + t : t;
			else chat.drafts[scope] = (chat.drafts[scope] ? chat.drafts[scope] + " " : "") + t;
		},
	});
	chat.queue = q;
	chat.dictate({ blob: "later", conversationId: SENTINEL });
	await flush();
	chat.tx.reject(0, new Error("stt down")); // maxAttempts 1 → straight to failed
	await flush();
	assert.equal(
		q.snapshot().failed.length,
		1,
		"the take is a retained FAILED recording (its chip)"
	);

	// The user also typed text and SENDS the id-less new chat (a failed recording does not block send).
	chat.input = "typed message";
	chat.send(undefined, undefined, { ok: true, conversation_id: "conv-real" });
	assert.equal(chat.currentId, "conv-real", "the new chat adopted its real id");
	assert.deepEqual(
		q.captureSent(SENTINEL),
		[],
		"the failed recording was migrated OFF the sentinel by id-adoption"
	);

	// Now the user hits Retry on the failed recording; it succeeds and commits AFTER the id switch.
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
		"the migrated recording is releasable under the real id"
	);
	chat.send(undefined, undefined, { ok: true });
	assert.ok(!chat.mirror.has(0), "the real-scope send releases it — no forever-armed guard");
	assert.equal(
		q.hasUnfinished(),
		false,
		"guard clears end to end — the recording was never stranded (R3-2)"
	);
});

// ── (R3-3) INTEGRATION: failed-bubble resend rejected across all four reasons keeps token + audio ─
test("R3-3 integration: a failed-bubble resend rejected (ok:false / usage_limit / subscription_suspended / single-flight) keeps a bubble with the SAME token; audio retained; resendable", async () => {
	for (const reason of [undefined, "usage_limit", "subscription_suspended", "single_flight"]) {
		const chat = makeChat();
		// A take is dictated + committed into a real conversation, then the FIRST send fails outright.
		chat.currentId = "convR";
		chat.micConvId = "convR";
		chat.dictate({ blob: "a", conversationId: "convR" });
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
			chat.mirror.has(0),
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
			chat.mirror.has(0),
			"audio STILL retained across the rejected resend — never lost"
		);
		assert.equal(
			chat.queue.hasUnfinished(),
			true,
			"guard still armed, but with an ACTION (resend the bubble)"
		);

		// Resend once more; this time it succeeds → the recording is finally released, guard clears.
		chat.resendFailed(resendBubble, { ok: true });
		assert.ok(
			!chat.mirror.has(0),
			"a successful resend finally releases the delivered recording"
		);
		assert.equal(
			chat.queue.hasUnfinished(),
			false,
			"guard clears — no forever-armed `done` record (R3-3)"
		);
	}
});

// ── (VR4-1) INTEGRATION: a recording edited out of a MAIN send becomes an ACTIONABLE retained chip;
//        Download exposes the audio and Discard clears the guard (no forever-armed done record). ─
test("VR4-1 integration: an edited-out recording surfaces as a retained (actionable) chip after a send; Discard clears the guard", async () => {
	const chat = makeChat();
	chat.dictate({ blob: "a", conversationId: SENTINEL });
	chat.dictate({ blob: "b", conversationId: SENTINEL });
	await flush();
	chat.tx.resolve(0, "alpha words");
	chat.tx.resolve(1, "bravo words");
	await flush();
	assert.deepEqual(
		chat.queue.snapshot().retained,
		[],
		"nothing retained while both are in the draft"
	);

	// The user DELETES recording B's words, then sends → B can no longer be released by a payload match.
	chat.input = "alpha words";
	chat.send(undefined, undefined, { ok: true, conversation_id: "conv-1" });

	const snap = chat.queue.snapshot();
	assert.equal(
		snap.retained.length,
		1,
		"the edited-out recording B is surfaced as an ACTIONABLE retained chip"
	);
	assert.equal(snap.retained[0].id, 1, "it is recording B");
	assert.equal(
		snap.retained[0].text,
		"bravo words",
		"the retained entry carries B's transcript for Restore"
	);
	assert.ok(chat.mirror.has(1), "B's audio is still retained (never lost)");
	assert.ok(
		chat.queue.get(1).blob,
		"Download exposes the audio (get() returns B's assembled blob)"
	);
	assert.equal(
		chat.queue.hasUnfinished(),
		true,
		"the guard is armed — but now with a visible resolution"
	);
	// A (sent) was released, and B rode the sentinel→real-id promotion still flagged retained.
	assert.ok(!chat.mirror.has(0), "recording A (in the payload) was released");
	assert.equal(
		chat.currentId,
		"conv-1",
		"the new chat adopted its real id (B migrated with it)"
	);

	// Discard the retained recording: guard clears, audio dropped — the forever-armed gap is closed.
	chat.queue.discard(1);
	assert.deepEqual(
		chat.queue.snapshot().retained,
		[],
		"Discard removes it from the retained list"
	);
	assert.equal(
		chat.queue.hasUnfinished(),
		false,
		"Discard clears the guard — no forever-armed recording"
	);
	assert.ok(!chat.mirror.has(1), "and drops the mirrored audio");
});

// ── (VR4-3) INTEGRATION: switching chats DURING an id-less send still promotes the sentinel scope,
//        so a later retry routes to the REAL conversation and is releasable — never stranded. ─────
test("VR4-3 integration: a mid-send chat switch still promotes the sentinel — a retried recording routes to the real id and releases", async () => {
	const chat = makeChat();
	// A take dictated in the new-chat composer FAILS (both attempts reject) → a retained failure.
	chat.dictate({ blob: "later", conversationId: SENTINEL });
	await flush();
	chat.tx.reject(0, new Error("stt down 1"));
	await flush();
	chat.tx.reject(0, new Error("stt down 2"));
	await flush();
	assert.equal(chat.queue.snapshot().failed.length, 1, "it is a retained FAILED recording");

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
		"the failed recording was migrated OFF the sentinel despite the switch (VR4-3)"
	);
	assert.ok(!chat.drafts[SENTINEL], "no stale sentinel draft left behind");

	// The user hits Retry on the failed recording; it now commits under the REAL id. Since conv-real is
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
		"the migrated recording is releasable under the real id"
	);

	// Return to conv-real and send: the real-scope send releases it — the guard clears.
	chat.navigateTo("conv-real");
	assert.equal(
		chat.input,
		"the late words",
		"returning restores conv-real's draft into the composer"
	);
	chat.send(undefined, undefined, { ok: true });
	assert.ok(!chat.mirror.has(0), "the real-scope send releases it — never stranded (VR4-3)");
	assert.equal(chat.queue.hasUnfinished(), false, "guard clears end to end");
});

// ── (VR4-2) INTEGRATION: a resend REJECTED while the user switched conversations keeps the failed
//        bubble + its voice token on the ORIGIN conversation (for EVERY rejection reason). ─────────
test("VR4-2 integration: a resend rejected mid-send-switch keeps the bubble + token on the ORIGIN conversation (all reasons)", async () => {
	for (const reason of [undefined, "usage_limit", "subscription_suspended", "single_flight"]) {
		const chat = makeChat();
		chat.currentId = "convR";
		chat.micConvId = "convR";
		chat.dictate({ blob: "a", conversationId: "convR" });
		await flush();
		chat.tx.resolve(0, "spoken words");
		await flush();
		chat.input = "spoken words";
		// The first send THROWS → a failed bubble on convR carrying the payload-bound token.
		const failed1 = chat.send(undefined, undefined, { throw: true });
		assert.deepEqual(failed1.voiceAck, [0], "the failed bubble carries the token");
		assert.ok(chat.mirror.has(0), "audio retained after the failed send");

		// The user hits Resend but SWITCHES to another conversation while the POST is in flight; the
		// resend is then REJECTED with this reason.
		chat.resendFailed(failed1, { ok: false, reason }, () => chat.navigateTo("other"));
		assert.equal(chat.currentId, "other", "the user is on the OTHER conversation now");
		assert.ok(
			!chat.messages.some((m) => m.failed),
			"the failed bubble is NOT shown on the unrelated conversation"
		);
		assert.ok(
			chat.pending.has("convR"),
			`the rejected resend survives ORIGIN-scoped, not in the rendered messages (reason=${reason})`
		);
		assert.ok(
			chat.mirror.has(0),
			"audio STILL retained across the mid-send switch + rejection — never lost (VR4-2)"
		);
		assert.equal(
			chat.queue.hasUnfinished(),
			true,
			"guard armed — the words are not durably sent"
		);

		// Return to convR: loadConversation re-injects the failed bubble WITH its token.
		chat.navigateTo("convR");
		const shown = chat.messages.find((m) => m.failed);
		assert.ok(shown, `returning to the origin shows the resend affordance (reason=${reason})`);
		assert.deepEqual(shown.voiceAck, [0], "the re-injected bubble carries the SAME token");

		// Resend once more (now on screen) succeeds → the recording is released, guard clears.
		chat.resendFailed(shown, { ok: true });
		assert.ok(!chat.mirror.has(0), "a successful resend releases the delivered recording");
		assert.equal(chat.queue.hasUnfinished(), false, "guard clears — never stranded (VR4-2)");
		assert.ok(
			!chat.pending.has("convR"),
			"the resolved bubble is dropped from the pending store"
		);
	}
});

// ── (VR4-2) a MAIN-composer send rejected off-origin restores its text to the ORIGIN draft, never
//        into the conversation the user switched to (no cross-conversation text bleed). ────────────
test("VR4-2: a main send rejected after a mid-send switch restores text to the ORIGIN draft, not the visible chat", async () => {
	const chat = makeChat();
	chat.currentId = "convR";
	chat.micConvId = "convR";
	chat.input = "typed in convR";
	// A plain (no-voice) main send; the user switches to 'other' mid-POST; the server rejects it.
	chat.send(undefined, undefined, { ok: false, reason: "single_flight" }, () =>
		chat.navigateTo("other")
	);
	assert.equal(chat.currentId, "other", "the user switched conversations");
	assert.equal(chat.input, "", "the VISIBLE (other) composer is not polluted with convR's text");
	assert.equal(
		chat.drafts["convR"],
		"typed in convR",
		"the origin's text is restored to ITS draft — no cross-conversation bleed (VR4-2)"
	);
});

// ── (VR5-3) newChat() promotes the sentinel scope then resets messages; the route watcher no-ops, so
//        the promoted failed bubble + its token must be RE-INJECTED here or it stays hidden. ─────────
test("VR5-3: after newChat promotes the sentinel, the failed bubble is re-injected (not hidden until another nav)", () => {
	const pending = createPendingSends();
	// A send from the id-less new-chat composer failed → a failed bubble carrying a voice token, kept
	// origin-scoped under the sentinel.
	const bubble = { name: "opt-7", content: "hello", failed: true, voiceAck: [3] };
	pending.add(SENTINEL, bubble);

	// newChat(): promote the sentinel to the real id (_promoteNewChatScope's queue/draft migration is a
	// no-op here; the pending-store reassign is the part that matters for the bubble)...
	promoteNewChatScope({
		queue: null,
		drafts: {},
		fromScope: SENTINEL,
		toId: "conv-real",
		takeScope: SENTINEL,
	});
	pending.reassign(SENTINEL, "conv-real");
	assert.deepEqual(
		pending.peek(SENTINEL),
		[],
		"the failed bubble left the sentinel scope on promotion"
	);
	assert.deepEqual(
		pending.peek("conv-real"),
		[bubble],
		"…and now belongs to the real conversation"
	);

	// ...then newChat resets messages to [] and (the VR5-3 fix) re-injects the real scope's pending
	// bubbles exactly as loadConversation does.
	let messages = [];
	messages = injectPendingBubbles(messages, pending.peek("conv-real"));
	assert.deepEqual(
		messages,
		[bubble],
		"the promoted failed bubble (with its voice-release token) is visible right after newChat"
	);
	assert.deepEqual(
		messages[0].voiceAck,
		[3],
		"its recovery token survives the promotion + hydrate"
	);

	// Idempotent: a subsequent hydrate (or a later loadConversation) must not double-inject.
	messages = injectPendingBubbles(messages, pending.peek("conv-real"));
	assert.equal(messages.length, 1, "dedupe by name — no duplicate bubble on re-hydrate");
});

// ── (VR5-3) injectPendingBubbles is a pure, defensive merge (dedupe by name, tolerant inputs). ──────
test("VR5-3: injectPendingBubbles dedupes by name, ignores nameless entries, and tolerates empty input", () => {
	const a = { name: "a", content: "A" };
	const b = { name: "b", content: "B" };
	// nothing to add → same array reference back (no needless reactive churn).
	const base = [a];
	assert.equal(
		injectPendingBubbles(base, []),
		base,
		"empty pending → the input array is returned as-is"
	);
	assert.equal(
		injectPendingBubbles(base, undefined),
		base,
		"missing pending → input returned as-is"
	);
	// dedupe: `a` is already present, only `b` is appended.
	assert.deepEqual(
		injectPendingBubbles([a], [a, b]),
		[a, b],
		"an already-present bubble (by name) is not duplicated; the new one is appended"
	);
	// defensive: a nameless / null entry is skipped, never appended.
	assert.deepEqual(
		injectPendingBubbles([], [null, { content: "no name" }, b]),
		[b],
		"nameless/null entries are ignored"
	);
	// tolerates a non-array messages input.
	assert.deepEqual(
		injectPendingBubbles(undefined, [b]),
		[b],
		"a non-array messages input degrades to []"
	);
});

// ── jarvis#496: the discuss-in-chat / ground-in-wiki one-shot SEND CONTEXT must survive a
//        REJECTED or THROWN send exactly like a voice bubble does (R3-3 above) — only a send the
//        server actually ACCEPTS may consume it, so a retry still carries the same context. ───────
test("jarvis#496: a REJECTED send preserves the prefill send context; the retry still carries it", () => {
	const chat = makeChat();
	const prefill = { doctype: "Sales Invoice", name: "INV-1" };
	chat.prefillContext = prefill;
	chat.input = "discuss this invoice";

	const rejected = chat.send(undefined, undefined, { ok: false, reason: "single_flight" });
	assert.deepEqual(
		rejected.sendCtx,
		prefill,
		"the rejected attempt DID carry the prefill context"
	);
	assert.equal(
		chat.prefillContext,
		prefill,
		"a rejected send must NOT clear it — still armed for the retry (jarvis#496)"
	);
	assert.equal(
		chat.input,
		"discuss this invoice",
		"the composer text was restored for the retry"
	);

	// The user hits Retry (a plain resend of the restored composer text).
	const retried = chat.send(undefined, undefined, { ok: true });
	assert.deepEqual(
		retried.sendCtx,
		prefill,
		"the retry carries the SAME context the first, rejected attempt would have lost"
	);
	assert.equal(
		chat.prefillContext,
		null,
		"an ACCEPTED send finally consumes the one-shot context"
	);
});

test("jarvis#496: a THROWN send (network error / 500) also preserves the prefill context for retry", () => {
	const chat = makeChat();
	const prefill = { ground_wiki_source: "wiki:123" };
	chat.prefillContext = prefill;
	chat.input = "discuss this";

	const failed = chat.send(undefined, undefined, { throw: true });
	assert.deepEqual(failed.sendCtx, prefill, "the failed attempt carried the context");
	assert.equal(
		chat.prefillContext,
		prefill,
		"a thrown send is the SAME bug class as a rejection — must not clear it either"
	);

	// Resend the failed bubble; this time it is accepted.
	const retried = chat.resendFailed(failed, { ok: true });
	assert.deepEqual(retried.sendCtx, prefill, "the resend still carries the preserved context");
	assert.equal(chat.prefillContext, null, "consumed only once a send is actually accepted");
});

test("jarvis#496: an ACCEPTED send is still one-shot — it consumes the context so a later send goes bare", () => {
	const chat = makeChat();
	chat.prefillContext = { doctype: "Purchase Order", name: "PO-9" };
	chat.input = "first message";
	const first = chat.send(undefined, undefined, { ok: true });
	assert.ok(first.sendCtx, "the first send carried the armed context");
	assert.equal(chat.prefillContext, null, "consumed after acceptance, as before this fix");

	chat.input = "second, unrelated message";
	const second = chat.send(undefined, undefined, { ok: true });
	assert.equal(
		second.sendCtx,
		undefined,
		"a later send with nothing armed goes bare, unchanged"
	);
});

test("jarvis#496: groundWiki overrides the prefill context in sendCtx, but a rejection still leaves the prefill armed", () => {
	const chat = makeChat();
	chat.groundWiki = true;
	const prefill = { doctype: "Sales Invoice", name: "INV-2" };
	chat.prefillContext = prefill;
	chat.input = "ground this";

	const rejected = chat.send(undefined, undefined, { ok: false });
	assert.deepEqual(
		rejected.sendCtx,
		{ ground_wiki: 1 },
		"groundWiki wins over the prefill context"
	);
	assert.equal(
		chat.prefillContext,
		prefill,
		"the prefill stays armed across the rejection regardless of which context sendCtx used"
	);
});

// ── jarvis#496 source pin: ties the mini-model above to the REAL file, so a regression that moves
//        the clear back before the await, OR reinserts it as a statement INSIDE the rejection
//        block (still textually "after" a bare `if (...)` check, but on the wrong side of its
//        closing brace — code review caught this gap in an earlier version of this test), fails
//        HERE even if nobody remembers to update the mini-model. ────────────────────────────────
test("jarvis#496 source pin: ChatView clears _prefillSendContext only on the accepted path, never before the send POST", () => {
	const start = chatViewSrc.indexOf("async function send(textArg, resendAck) {");
	const end = chatViewSrc.indexOf("\nfunction openProactive()", start);
	assert.ok(start > -1 && end > start, "located ChatView.vue's send() function body");
	const sendSrc = chatViewSrc.slice(start, end);

	// Anchor on the call itself, not its first arg: prettier wraps a long
	// sendMessage(...) across lines, so "sendMessage(sentFrom" is not contiguous.
	const awaitIdx = sendSrc.indexOf("await api.sendMessage(");
	// Anchors the rejection block's CLOSING brace, not just its opening `if (...)` — a scope-aware
	// check. A regression that clears the context as the FIRST statement inside the rejected branch
	// (unconditionally wiping it on every rejection, reintroducing the jarvis#496 bug) would still
	// be textually after a bare "if (r && r.ok === false)" search, but sits BEFORE this closing
	// sequence, so it fails here. The rejection block's final `return;` and closing brace are
	// immediately followed by the workers-notice self-heal block's `if`, not directly by the
	// "// Send accepted" comment (a workersNotice/workersWarnNotice self-heal block now sits
	// between the two), so anchor on that sibling `if` instead of the comment.
	const rejectBlockCloseIdx = sendSrc.indexOf("return;\n\t\t}\n\t\tif (r && r.ok !== false) {");
	const clearIdx = sendSrc.indexOf("_prefillSendContext = null;");
	assert.ok(
		awaitIdx > -1 && rejectBlockCloseIdx > -1 && clearIdx > -1,
		"all three anchors are present in send()"
	);
	assert.ok(
		clearIdx > awaitIdx,
		"the one-shot context must never be cleared before api.sendMessage runs — that was the jarvis#496 bug"
	);
	assert.ok(
		clearIdx > rejectBlockCloseIdx,
		"the clear must sit strictly OUTSIDE the rejection block (after its closing brace), not merely " +
			"after the `if` condition text — a clear placed inside that block would still wipe the " +
			"context on every rejection"
	);
});
