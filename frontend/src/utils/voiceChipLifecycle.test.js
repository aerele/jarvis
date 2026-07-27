// UX chip-lifecycle wiring: the ChatView half of the voice-clip retention story.
//
// The queue primitive's half (markSentWithoutClip, snapshot().failed[].sentWithout,
// hasUnfinishedReason) is proven behaviorally in voiceChunkQueue.test.js against the real module.
// What CANNOT be proven there is whether ChatView actually calls it at the right moment and says
// the right thing: the composer is a single-file component with no harness in this app (no vitest,
// no @vue/test-utils; mounting it would need a router, a socket and the whole api surface), so —
// exactly like pwa/src/lib/pumpFence.test.js — the wiring is asserted against the SOURCE.
//
// Crude, but a real regression guard: it fails the moment the send-time gap confirm is removed or
// moved after the strip, the sent-without flagging drifts out of the accepted-send branch, a chip's
// copy stops branching on `sentWithout`, the retry-after-send toast disappears, or the leave guard
// goes back to arming on "anything outstanding" (the cry-wolf warning UX-3 removed).
//
// It also fences the ONE path this change must not regress: the done-clip
// captureSentInPayload → acknowledge release, which has its own extensive suites.
//
// Run: `node --test voiceChipLifecycle.test.js`, or via the python suite
// (jarvis/tests/test_voice_chip_lifecycle_client.py subprocess-runs it every CI run).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createVoiceChunkQueue } from "./voiceChunkQueue.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CHAT_VIEW = path.join(HERE, "..", "views", "ChatView.vue");
const src = fs.readFileSync(CHAT_VIEW, "utf8");

function sendBody() {
	const start = src.indexOf("async function send(textArg, resendAck) {");
	assert.notEqual(start, -1, "ChatView must still define send(textArg, resendAck)");
	const end = src.indexOf("\nfunction openProactive()", start);
	assert.notEqual(end, -1, "could not find the end of send()");
	return src.slice(start, end);
}

// ── the ⟦clip N⟧ token has ONE definition ───────────────────────────────────────
test("the gap token is read and stripped through the SAME regex — tokens found == tokens stripped", () => {
	assert.match(
		src,
		/const _GAP_TOKEN_RE = \/⟦clip \\d\+⟧\/g;/,
		"the token shape must stay a single constant"
	);
	assert.match(
		src,
		/function _stripGapTokens\(s\) \{\n\treturn \(s \|\| ""\)\.replace\(_GAP_TOKEN_RE, ""\)/,
		"the strip must use the shared constant"
	);
	assert.match(
		src,
		/function _gapSeqsIn\(s\) \{[\s\S]*?matchAll\(_GAP_TOKEN_RE\)/,
		"…and so must the seq reader, or a send could strip a token it never counted"
	);
	// seq+1 is the chip's label AND the token's number: the reader must undo that, not guess.
	assert.match(
		src,
		/function _gapSeqsIn\(s\) \{[\s\S]*?out\.push\(n - 1\);/,
		"_gapSeqsIn must convert the token's 1-based clip number back to the 0-based seq"
	);
});

// ── UX-2: the send-time gap confirmation, BEFORE anything is stripped or sent ───
test("send() confirms before sending a payload that is missing a clip's words — ahead of the strip", () => {
	const body = sendBody();
	const read = body.indexOf("let _gapSeqsInText = fromMain ? _gapSeqsIn(input.value) : [];");
	const confirmAt = body.indexOf('title: "Send without part of what you said?"');
	const strip = body.indexOf("const text = _stripGapTokens(fromMain ? input.value : textArg);");
	const post = body.indexOf("await api.sendMessage(");
	assert.notEqual(read, -1, "the gap seqs must be read from the RAW composer text");
	assert.notEqual(confirmAt, -1, "the send-time gap confirmation is missing");
	assert.notEqual(strip, -1, "send() must still strip the placeholders from the payload");
	assert.ok(read < confirmAt, "the confirm needs the seqs it is confirming about");
	assert.ok(
		confirmAt < strip && confirmAt < post,
		"the confirm must run BEFORE the tokens are stripped and before the POST — after either, " +
			"the words are already gone from a message the user never agreed to send incomplete"
	);
	assert.match(
		body,
		/"One voice clip didn't transcribe, so part of your dictation is missing from this message\. Send anyway, or fix it first\?"/,
		"the single-gap copy must name what is actually missing"
	);
	assert.match(
		body,
		/confirmLabel: "Send anyway",\n\t\t\tcancelLabel: "Review first",/,
		"Review first must be the cancel path — cancelling returns to the composer, it does not send"
	);
	assert.match(
		body,
		/if \(!ok\) return; \/\/ focus returns to the composer/,
		"cancel must ABORT the send, leaving the placeholder + chip in place to act on"
	);
	// A resend is re-sending a payload the user already chose to send with the gap in it.
	assert.match(
		body,
		/let _gapSeqsInText = fromMain \?/,
		"the confirm (and the flagging) is fromMain-only — a resendFailed must not re-ask"
	);
});

// ── UX-1: flag the sent-without clips, but ONLY on an accepted send ─────────────
test("send() flags sent-without clips only in the accepted-send branch, after the release", () => {
	const body = sendBody();
	const ack = body.indexOf("if (_voiceAck) voiceQueue?.acknowledge(_voiceAck);");
	const mark = body.indexOf("voiceQueue.markSentWithoutClip(_gapSeq)");
	const rejected = body.indexOf("if (r && r.ok === false) {");
	assert.notEqual(ack, -1, "the done-clip release must still run on acceptance (fenced path)");
	assert.notEqual(mark, -1, "failed clips carried by an accepted send must be flagged");
	assert.ok(
		rejected !== -1 && mark > rejected,
		"the flagging must sit after the rejection branch — a rejected send left the words in the composer"
	);
	assert.ok(
		mark > ack,
		"flag alongside the release, in the same accepted-send branch, so both describe the SAME payload"
	);
	assert.match(
		body,
		/if \(fromMain && voiceQueue\)\n\t\t\tfor \(const _gapSeq of _gapSeqsInText\) voiceQueue\.markSentWithoutClip\(_gapSeq\);/,
		"every gap the payload carried is flagged (the queue ignores any that is no longer failed)"
	);
});

// ── UX-1: the chip's copy branches on sentWithout ───────────────────────────────
test("the failed chip and its Retry tooltip branch on sentWithout", () => {
	assert.match(
		src,
		/{{ failedChipLabel\(f\) }}/,
		"the failed chip's label must come from the branching helper, not a fixed string"
	);
	assert.match(
		src,
		/:title="failedChipRetryTitle\(f\)"/,
		"…and so must its Retry tooltip: post-send it can only build a follow-up"
	);
	assert.match(
		src,
		/f\.sentWithout\n\t\t\? `Clip \$\{f\.seq \+ 1\} was missing from your last message`\n\t\t: `Clip \$\{f\.seq \+ 1\} didn't transcribe`/,
		"the two states must read differently — identical copy is what makes a retained clip look stuck"
	);
	assert.match(
		src,
		/f\.sentWithout\n\t\t\? "Transcribe and add as a follow-up message"/,
		"the post-send tooltip must not promise to edit a message that has already gone"
	);
	assert.match(
		src,
		/: "Transcribe again — the words drop back into the ⟦clip⟧ placeholder in the message, where they belong"/,
		"the PRE-send tooltip is unchanged — it is accurate while the placeholder is still there"
	);
});

// ── UX-4: the retry-after-send append is explained ──────────────────────────────
test("_replaceGapPlaceholder toasts when it appends instead of replacing", () => {
	const start = src.indexOf("function _replaceGapPlaceholder(seq, text, clip) {");
	assert.notEqual(start, -1, "ChatView must still define _replaceGapPlaceholder");
	const body = src.slice(start, src.indexOf("\nfunction _removeGapPlaceholder", start));
	assert.match(
		body,
		/appendedInstead = !!t;\n\t\treturn t \? _joinAppend\(prev, t\) : prev;/,
		"the fallback-append branch (token gone — the message already left) must be detectable"
	);
	assert.match(
		body,
		/if \(appendedInstead\)\n\t\tnotify\(`Recovered text for Clip \$\{seq \+ 1\} added to your draft\.`, \{ type: "info" \}\);/,
		"…and announced, or unexplained text just appears in an otherwise-empty composer"
	);
	assert.ok(
		body.indexOf("appendedInstead = !!t") < body.indexOf("if (appendedInstead)"),
		"the flag is set inside the mutation and read after it"
	);
	// The IN-PLACE replacement is the normal path and must stay silent.
	assert.match(
		body,
		/if \(prev\.includes\(tok\)\) \{\n\t\t\treturn t\n\t\t\t\t\? prev\.split\(tok\)\.join\(t\)/,
		"a token that IS still there is replaced in place, with no toast"
	);
});

// ── UX-3: the leave guard arms on the REASON, not on "anything outstanding" ─────
test("the leave guard blocks only for a live loss risk, and keeps its copy verbatim for that case", () => {
	assert.match(
		src,
		/function _voiceGuardReason\(\) \{/,
		"the guard must read the queue's reason, not a bare boolean"
	);
	assert.match(
		src,
		/return \(voiceQueue && voiceQueue\.hasUnfinishedReason\(\)\) \|\| null;/,
		"the reason comes from the queue — one source of truth for what is at risk"
	);
	assert.doesNotMatch(
		src,
		/voiceQueue\.hasUnfinished\(\)/,
		"the old arm-on-anything predicate must be gone from the guard path (it cried wolf for " +
			"terminally-failed clips whose audio is durably mirrored)"
	);
	assert.match(
		src,
		/function _beforeUnloadVoice\(e\) \{\n\tif \(_voiceGuardReason\(\) === "live"\) \{/,
		"beforeunload arms for a live risk ONLY"
	);
	assert.match(
		src,
		/onBeforeRouteLeave\(async \(\) => \{\n\tif \(_voiceGuardReason\(\) !== "live"\) return true;/,
		"…and so does the route-leave confirm: unresolved must not block navigation at all"
	);
	// The live case is a REAL risk — its copy is accurate and stays exactly as it was.
	assert.match(
		src,
		/title: "Leave with un-transcribed audio\?",\n\t\tmessage:\n\t\t\t"Some dictated voice clips haven't finished transcribing\. Leaving now may lose audio that hasn't been saved yet\. Leave anyway\?",/,
		"the live-risk copy is unchanged — it was always accurate for that case"
	);
	// A recording (or a take starting) is live before any clip exists.
	assert.match(
		src,
		/micState\.value === "recording" \|\|[\s\S]*?micRec\.starting\n\t\)\n\t\treturn "live";/,
		"a live take still arms the guard before the queue has anything in it"
	);
});

// ── the fenced path: the done-clip release is untouched ─────────────────────────
test("the done-clip captureSentInPayload → acknowledge release is untouched", () => {
	const body = sendBody();
	assert.match(
		body,
		/const _voiceAck =\n\t\tresendAck \|\|\n\t\t\(fromMain && voiceQueue \? voiceQueue\.captureSentInPayload\(_sentScope, text\) : null\);/,
		"the payload-bound token is still captured from the EXACT outgoing text, before the POST"
	);
	assert.ok(
		body.indexOf("voiceQueue.captureSentInPayload(_sentScope, text)") <
			body.indexOf("await api.sendMessage("),
		"…and still captured BEFORE the POST (R2-1: a clip committing mid-flight is not released)"
	);
	assert.match(
		body,
		/if \(fromMain && voiceQueue\) voiceQueue\.markUnsentOrphans\(_sentScope, _voiceAck\);/,
		"the edited-out retained-clip path (VR4-1) is unchanged"
	);
});

// ── decision parity: the queue behaves the way the wiring above assumes ─────────
test("parity: a stripped gap + a released done clip leave exactly one flagged chip and no live guard", async () => {
	const flush = () => new Promise((r) => setTimeout(r, 0));
	const settle = [];
	const q = createVoiceChunkQueue({
		transcribe: (clip) => new Promise((res, rej) => settle.push({ seq: clip.seq, res, rej })),
		mirror: {
			store: new Map(),
			put(c) {
				this.store.set(c.seq, c);
			},
			delete(s) {
				this.store.delete(s);
			},
			all() {
				return [...this.store.values()];
			},
		},
		retainUntilSent: true,
		concurrency: 2,
		maxAttempts: 1,
	});
	const a = q.enqueue({ blob: "a", durationS: 15, conversationId: "c1" });
	const b = q.enqueue({ blob: "b", durationS: 15, conversationId: "c1" });
	await flush();
	settle.find((s) => s.seq === a).rej(new Error("timeout"));
	settle.find((s) => s.seq === b).res("the rest of it");
	await flush();

	// What ChatView does on an accepted send: composer held "⟦clip 1⟧ the rest of it".
	const raw = `⟦clip ${a + 1}⟧ the rest of it`;
	const seqs = [...raw.matchAll(/⟦clip \d+⟧/g)].map((m) => Number(m[0].replace(/\D+/g, "")) - 1);
	assert.deepEqual(seqs, [a], "the raw composer text names exactly the failed clip");
	const payload = raw
		.replace(/⟦clip \d+⟧/g, "")
		.replace(/ {2,}/g, " ")
		.trim();
	q.acknowledge(q.captureSentInPayload("c1", payload));
	for (const s of seqs) q.markSentWithoutClip(s);
	await flush();

	const snap = q.snapshot();
	assert.equal(snap.failed.length, 1, "one chip remains — the clip whose words never went out");
	assert.equal(snap.failed[0].sentWithout, true, "…and it says so");
	assert.equal(snap.done, 0, "the delivered clip was released, exactly as before");
	assert.equal(
		q.hasUnfinishedReason(),
		"unresolved",
		"nothing is at risk: the remaining audio is durably mirrored, so navigation is not blocked"
	);
});
