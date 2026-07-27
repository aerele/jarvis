// UX lifecycle wiring: the ChatView half of the dictation retention story.
//
// The store primitive's half (markSentWithout, snapshot().failed[].sentWithout, hasUnfinishedReason,
// the payload-bound release) is proven behaviourally in voiceDictationStore.test.js against the real
// module. What CANNOT be proven there is whether ChatView actually calls it at the right moment and
// says the right thing: the composer is a single-file component with no harness in this app (no
// vitest, no @vue/test-utils; mounting it would need a router, a socket and the whole api surface),
// so — exactly like pwa/src/lib/pumpFence.test.js — the wiring is asserted against the SOURCE.
//
// Crude, but a real regression guard. It fails the moment the per-clip machinery grows back (gap
// placeholder tokens, per-clip chips, the send-without-gap confirm), the recorder stops running in
// timeslice mode, fragments stop being mirrored as they arrive, a chip's copy stops branching on
// `sentWithout`, the leave guard goes back to arming on "anything outstanding", or an
// un-rebuildable recovery take is offered a Transcribe button that cannot work.
//
// It also fences the ONE path this rewrite must not regress: the payload-bound
// captureSentInPayload → acknowledge release, which has its own extensive suites.
//
// Run: `node --test voiceDictationLifecycle.test.js`, or via the python suite
// (jarvis/tests/test_voice_dictation_lifecycle_client.py subprocess-runs it every CI run).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createVoiceDictationStore } from "./voiceDictationStore.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CHAT_VIEW = path.join(HERE, "..", "views", "ChatView.vue");
const RECORDER = path.join(HERE, "..", "composables", "useDictationRecorder.js");
const src = fs.readFileSync(CHAT_VIEW, "utf8");
const recSrc = fs.readFileSync(RECORDER, "utf8");

const flush = () => new Promise((r) => setTimeout(r, 0));

function sendBody() {
	const start = src.indexOf("async function send(textArg, resendAck) {");
	assert.notEqual(start, -1, "ChatView must still define send(textArg, resendAck)");
	const end = src.indexOf("\nfunction openProactive()", start);
	assert.notEqual(end, -1, "could not find the end of send()");
	return src.slice(start, end);
}

// ── the per-clip machinery is GONE, not merely unused ───────────────────────────────────────
test("no gap placeholders, no per-clip chips, no send-without-gap confirm survive anywhere", () => {
	// Each of these existed only because a take was cut into independently-transcribed clips. With
	// one recording per dictation there is no gap to anchor, so re-introducing any of them would be
	// re-introducing the fabrication bug's whole scaffolding.
	for (const dead of [
		"_GAP_TOKEN_RE",
		"_gapToken",
		"_gapSeqsIn",
		"_stripGapTokens",
		"_insertGapPlaceholder",
		"_replaceGapPlaceholder",
		"_removeGapPlaceholder",
		"⟦clip",
		"Send without part of what you said?",
	]) {
		assert.equal(src.includes(dead), false, `${dead} must not survive the at-once rewrite`);
	}
	assert.equal(
		src.includes("useChunkedRecorder") || src.includes("voiceChunkQueue"),
		false,
		"the per-clip recorder/queue modules are deleted — nothing may still import them"
	);
	assert.match(
		src,
		/import \{ createVoiceDictationStore \} from "@\/utils\/voiceDictationStore";/,
		"the composer must drive the per-recording store"
	);
	assert.match(
		src,
		/import \{ useDictationRecorder \} from "@\/composables\/useDictationRecorder";/
	);
});

// ── the recorder: ONE session, timeslice fragments, capped ──────────────────────────────────
test("the recorder runs ONE MediaRecorder in TIMESLICE mode and hands over the concatenation", () => {
	assert.match(
		recSrc,
		/recorder\.start\(fragmentMs\);/,
		"timeslice mode is what bounds crash exposure without cutting the take into transcription units"
	);
	assert.equal(
		/recorder\.start\(\);/.test(recSrc),
		false,
		"a bare start() would buffer the whole take with nothing durable until it ends"
	);
	assert.match(
		recSrc,
		/new Blob\(parts, \{ type: mimeType \}\)/,
		"the take is the fragments CONCATENATED — no re-encode, no remux"
	);
	// Each fragment must be handed out as it arrives, from inside ondataavailable.
	const onData = recSrc.slice(
		recSrc.indexOf("recorder.ondataavailable = (e) => {"),
		recSrc.indexOf("recorder.onerror =")
	);
	assert.match(onData, /onFragment\(\{/, "a fragment must be surfaced the instant it arrives");
	assert.match(onData, /index,/, "…with its ordinal, which is what recovery reassembles by");
});

test("the 5-minute cap AUTO-STOPS and keeps the audio — it never refuses the take", () => {
	assert.match(
		recSrc,
		/const DEFAULT_MAX_SECONDS = 300;/,
		"the client cap must match the server's _MAX_DURATION_S, enforced where the audio can still be saved"
	);
	const tickBlock = recSrc.slice(
		recSrc.indexOf("tick = setInterval("),
		recSrc.indexOf("\t// Finalise:")
	);
	assert.match(
		tickBlock,
		/onAutoStop\(durationS\.value\);/,
		"the user must be told why it ended"
	);
	assert.match(tickBlock, /void stop\(\);/, "…and it must STOP, which hands the take to onDone");
	assert.equal(
		tickBlock.includes("cancel()"),
		false,
		"cancelling at the cap would throw away five minutes of speech"
	);
	assert.match(
		recSrc,
		/Math\.min\(maxDurationS,/,
		"the reported duration is clamped to the cap, so the server never rejects it on a rounding second"
	);
});

test("ChatView mirrors every fragment and transcribes the assembled take exactly once", () => {
	const start = src.indexOf("const micRec = useDictationRecorder({");
	const end = src.indexOf("\nasync function startMic()", start);
	assert.notEqual(start, -1);
	const body = src.slice(start, end);
	assert.match(
		body,
		/onFragment: \(frag\) => \{[\s\S]*?voiceStore\.addFragment\(_activeRecordingId, frag\);/,
		"a fragment must go straight into the durable mirror — that is the whole crash-safety story"
	);
	assert.match(body, /onDone: \(take\) => \{/, "the assembled take is the transcription unit");
	assert.match(
		body,
		/onCancel: \(\) => \{[\s\S]*?voiceStore\.discard\(id\);/,
		"an abandoned take must drop its already-mirrored fragments, not leave them in recovery"
	);
	assert.match(
		body,
		/Recording stopped at the 5-minute limit — transcribing what you said\./,
		"the cap's toast must say the audio is being used, not lost"
	);
});

// ── send: no strip, no gap confirm, and the release path untouched ──────────────────────────
test("send() carries the composer text verbatim — there is nothing left to strip out of it", () => {
	const body = sendBody();
	assert.match(
		body,
		/const text = \(fromMain \? input\.value : textArg\)\.trim\(\);/,
		"a placeholder-stripping payload was only ever needed because clips could leave holes"
	);
	const post = body.indexOf("await api.sendMessage(");
	const capture = body.indexOf("voiceStore.captureSentInPayload(_sentScope, text)");
	assert.notEqual(capture, -1, "the payload-bound release must still be captured…");
	assert.ok(capture < post, "…BEFORE the POST, so it binds to exactly what is going out");
});

test("send() flags sent-without recordings only in the ACCEPTED branch, after the release", () => {
	const body = sendBody();
	const read = body.indexOf("voiceStore.failedIdsForScope(_sentScope)");
	const post = body.indexOf("await api.sendMessage(");
	const ack = body.indexOf("if (_voiceAck) voiceStore?.acknowledge(_voiceAck);");
	const flag = body.indexOf("voiceStore.markSentWithout(_fid)");
	assert.notEqual(read, -1, "the recordings a message is leaving behind must be read up front");
	assert.ok(read < post, "…before the POST, while they are still knowably failed");
	assert.notEqual(ack, -1, "the done-recording release must stay exactly where it is");
	assert.notEqual(flag, -1, "the sent-without flagging is missing");
	assert.ok(
		flag > ack,
		"flagging must sit in the accepted-send branch, after the release — a rejected send left " +
			"nothing behind, so claiming it did would be a lie"
	);
	const rejected = body.indexOf("if (r && r.ok === false) {");
	assert.ok(rejected < ack && flag > rejected, "…and it must not run on the rejection path");
});

test("the done-recording captureSentInPayload → acknowledge release is untouched", () => {
	const body = sendBody();
	assert.match(
		body,
		/const _voiceAck =\n\t\tresendAck \|\|\n\t\t\(fromMain && voiceStore \? voiceStore\.captureSentInPayload\(_sentScope, text\) : null\);/,
		"payload-bound capture, resend token reuse, and the fromMain gate all stay as they were"
	);
	assert.match(
		body,
		/if \(fromMain && voiceStore\) voiceStore\.markUnsentOrphans\(_sentScope, _voiceAck\);/,
		"the edited-out → retained-chip path is unchanged"
	);
});

// ── chips + pill: honest copy for each distinct state ───────────────────────────────────────
test("the failed chip and its Retry tooltip branch on sentWithout", () => {
	assert.match(
		src,
		/return f\.sentWithout\n\t\t\? `Recording \$\{len\} didn't transcribe — your last message went without it`\n\t\t: `Recording \$\{len\} didn't transcribe`;/,
		"identical copy for both states is exactly what makes a correctly-retained chip read as stuck"
	);
	assert.match(
		src,
		/const failedChipRetryTitle = \(f\) =>\n\tf\.sentWithout/,
		"Retry cannot edit a message that has already been sent — its tooltip must not promise that"
	);
	assert.match(
		src,
		/\{\{ failedChipLabel\(f\) \}\}/,
		"the template must USE the branching label"
	);
	assert.match(src, /:title="failedChipRetryTitle\(f\)"/);
	// The three actions the owner specified, on the one chip.
	const chip = src.slice(
		src.indexOf("{{ failedChipLabel(f) }}"),
		src.indexOf("retained-recording chip")
	);
	for (const act of [
		"retryRecording(f.id)",
		"downloadRecording(f.id)",
		"discardRecording(f.id)",
	]) {
		assert.ok(chip.includes(act), `the failed chip must offer ${act}`);
	}
});

test("the transcribing pill states the RECORDING's length and invents no progress", () => {
	assert.match(
		src,
		/Transcribing \{\{ recLength\(t\.durationS\) \}\}…/,
		"M:SS is the length of what is being transcribed — the only honest number available"
	);
	const pill = src.slice(
		src.indexOf('v-for="t in voiceQ.transcribing"'),
		src.indexOf("recovery banner")
	);
	for (const fake of ["%", "progress", "elapsed"]) {
		assert.equal(pill.includes(fake), false, `the pill must not fake ${fake}`);
	}
});

test("the silent-take message says what happened and offers no phantom recovery", () => {
	assert.match(
		src,
		/notify\("Nothing was heard — try closer to the microphone\.", \{ type: "info" \}\);/,
		"a measured-silent take is not a failure and must not leave a chip behind"
	);
});

// ── recovery banner: per recording, and honest about what cannot be rebuilt ──────────────────
test("the recovery banner names the recording's length and hides Transcribe when it can't work", () => {
	assert.match(
		src,
		/A recording from your last session wasn't transcribed \(\$\{len\}\)/,
		"the owner-approved copy names the length so the user knows what is on offer"
	);
	assert.match(
		src,
		/can't be rebuilt \(\$\{len\}\) — its first fragment is missing/,
		"a take with no fragment 0 has no initialisation segment — say so rather than fail on click"
	);
	const banner = src.slice(
		src.indexOf('v-for="t in recoveryTakes"'),
		src.indexOf("failed-recording chip")
	);
	assert.match(
		banner,
		/v-if="t\.complete"\n\t*class="jv-voicechip-act"\n\t*@click="recoverTake\(t\)"/,
		"Transcribe must be gated on `complete` — offering it for un-rebuildable bytes is a dead button"
	);
	for (const act of ["downloadTake(t)", "discardTake(t)"]) {
		assert.ok(
			banner.includes(act),
			`an un-rebuildable take must still offer ${act} — never silent loss`
		);
	}
});

// ── leave guard: loss-framed copy only for a genuine loss ────────────────────────────────────
test("the leave guard blocks only for a LIVE loss risk", () => {
	assert.match(
		src,
		/function _voiceGuardReason\(\) \{\n\tif \(micState\.value === "recording" \|\| micRec\.starting\) return "live";\n\treturn \(voiceStore && voiceStore\.hasUnfinishedReason\(\)\) \|\| null;\n\}/,
		"the guard must arm on the store's REASON, not on 'anything outstanding'"
	);
	assert.match(
		src,
		/function _beforeUnloadVoice\(e\) \{\n\tif \(_voiceGuardReason\(\) === "live"\)/,
		"a terminally-failed, durably-mirrored recording must not raise the browser's close warning"
	);
	assert.match(
		src,
		/onBeforeRouteLeave\(async \(\) => \{\n\tif \(_voiceGuardReason\(\) !== "live"\) return true;/
	);
	const dialog = src.slice(src.indexOf("onBeforeRouteLeave(async () => {"));
	assert.match(
		dialog.slice(0, 700),
		/is still being captured or transcribed, or its words are only in this unsent draft/,
		"the copy must describe the risk that is actually present, or users learn to click through it"
	);
});

// ── parity walk against the REAL store ───────────────────────────────────────────────────────
test("parity: a send with one transcribed and one failed recording releases one and flags the other", async () => {
	const settle = [];
	const mirror = new Map();
	const store = createVoiceDictationStore({
		transcribe: (rec) => new Promise((res, rej) => settle.push({ id: rec.id, res, rej })),
		mirror: {
			recordingKey: (id) => `r${id}`,
			putFragment: (f) => {
				mirror.set(`${f.recordingId}:${f.index}`, f);
				return Promise.resolve(true);
			},
			adopt: () => Promise.resolve(true),
			deleteRecording: (rid) => {
				for (const k of Array.from(mirror.keys()))
					if (k.startsWith(rid + ":")) mirror.delete(k);
				return Promise.resolve(true);
			},
			reassignConversation: () => Promise.resolve(true),
		},
		retainUntilSent: true,
		maxAttempts: 1,
	});
	const take = (blob) => {
		const id = store.begin({ conversationId: "convA" });
		store.addFragment(id, { index: 0, blob, durationS: 20 });
		store.finish(id, { blob, durationS: 20 });
		return id;
	};
	const good = take("g");
	const bad = take("b");
	await flush();
	settle.find((c) => c.id === good).res("the words I said");
	await flush();
	settle.find((c) => c.id === bad).rej(new Error("stt down"));
	await flush();

	// The composer's send path, in the same order ChatView runs it.
	const payload = "the words I said";
	const ack = store.captureSentInPayload("convA", payload);
	const leftBehind = store.failedIdsForScope("convA");
	assert.deepEqual(ack, [good]);
	assert.deepEqual(leftBehind, [bad]);
	store.acknowledge(ack);
	for (const id of leftBehind) store.markSentWithout(id);

	const snap = store.snapshot();
	assert.equal(snap.failed.length, 1, "exactly ONE chip — one recording, one unit");
	assert.equal(snap.failed[0].sentWithout, true, "…and it knows the message went without it");
	assert.equal(snap.retained.length, 0, "nothing was edited out, so no retained chip");
	assert.ok(!mirror.has("r0:0"), "the sent recording's audio is released");
	assert.ok(mirror.has("r1:0"), "the failed recording's audio is kept — Retry/Download need it");
	assert.equal(
		store.hasUnfinishedReason(),
		"unresolved",
		"nothing is at risk any more, so navigating away must not claim otherwise"
	);
});
