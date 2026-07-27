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

test("start() answers whether IT started the recorder — a re-entry is FALSE, never undefined", () => {
	// The lockout bug's root defect: the re-entry guard returned `undefined`, which is
	// indistinguishable from success, so the caller sailed on and opened a SECOND recording unit
	// for a take that was still finishing. The first unit then never left `recording`, and the
	// composer refused every send — typed ones included — until the tab was reloaded.
	assert.match(
		recSrc,
		/if \(state\.value === "recording" \|\| starting\.value\) return false;/,
		"the re-entry guard must report FALSE to its caller"
	);
	const startBody = recSrc.slice(
		recSrc.indexOf("async function start() {"),
		recSrc.indexOf("\t// Finalise:")
	);
	assert.equal(
		/\n\t\t\treturn;\n/.test(startBody) || /\n\t\treturn;\n/.test(startBody),
		false,
		"every exit from start() must answer true or false — a bare return is the old ambiguity"
	);
	assert.match(startBody, /\n\t\treturn true;\n/, "the success path must answer TRUE");
});

test("startMic refuses to open a second recording while one is still live or finishing", () => {
	const body = src.slice(
		src.indexOf("async function startMic() {"),
		src.indexOf("async function stopMic()")
	);
	assert.match(
		body,
		/if \(\n\t\tmicState\.value === "recording" \|\|\n\t\tmicRec\.starting \|\|\n\t\tmicRec\.state === "recording" \|\|\n\t\t_activeRecordingId != null\n\t\)\n\t\treturn;/,
		"all four windows must be closed: the UI state, the permission prompt, the recorder's own " +
			"stop→onstop gap, and a recording unit that is still open"
	);
	assert.match(
		body,
		/const started = await micRec\.start\(\);/,
		"the composer must gate on start()'s ANSWER…"
	);
	assert.match(
		body,
		/if \(!started \|\| micRec\.state !== "recording"\) return;/,
		"…because micRec.state still reads 'recording' from the PREVIOUS take during its stop"
	);
	const begin = body.indexOf("voiceStore.begin({ conversationId: _micConvId })");
	assert.ok(
		begin > body.indexOf("if (!started"),
		"the recording unit may only be opened after the gate"
	);
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
		/\? `Recording \$\{len\} didn't transcribe — your last message went without it\$\{why\}`\n\t\t: `Recording \$\{len\} didn't transcribe\$\{why\}`;/,
		"identical copy for both states is exactly what makes a correctly-retained chip read as stuck"
	);
	assert.match(
		src,
		/const why = f\.error \? ` · \$\{f\.error\}` : "";/,
		"a permanent fault ('Speech-to-text is not enabled on this site.') and a transient blip " +
			"read identically without the reason — users Retry-loop the upload and report nothing useful"
	);
	assert.match(src, /:title="failedChipTitle\(f\)"/, "…and the full reason is on the chip");
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
	// What it MAY add is the wait it actually measured, once a wait is long enough to worry about.
	assert.match(pill, /transcribingWait\(t\)/);
	assert.match(
		src,
		/return s >= VOICE_WAIT_VISIBLE_AFTER_S \? ` · waiting \$\{s\}s` : "";/,
		"elapsed seconds are a number the composer knows; a percentage is not"
	);
});

test("the transcribing pill can be CANCELLED — a stuck STT may not hold the composer hostage", () => {
	const pill = src.slice(
		src.indexOf('v-for="t in voiceQ.transcribing"'),
		src.indexOf("recovery banner")
	);
	assert.match(
		pill,
		/@click="cancelTranscribing\(t\.id\)"/,
		"without this the only exit from a slow transcription is a tab reload — which the leave " +
			"guard fights — while EVERY send, typed ones included, is refused"
	);
	assert.match(
		src,
		/function cancelTranscribing\(id\) \{\n\tif \(voiceStore\) voiceStore\.cancelTranscription\(id\);\n\}/,
		"cancel moves the recording to its failed chip with the audio intact — never a discard"
	);
});

test("a measured-silent take is RETAINED and actionable — it is never deleted on a measurement", () => {
	const start = src.indexOf("const micRec = useDictationRecorder({");
	const body = src.slice(start, src.indexOf("\nasync function startMic()", start));
	assert.match(
		body,
		/if \(isNearSilent\(take\.peakRms\)\) \{\n\t\t\tvoiceStore\.finishSilent\(id, take\);/,
		"discard() here destroyed up to five minutes of speech on one RMS reading, with no undo — " +
			"against this feature's own first invariant. finishSilent keeps the audio."
	);
	assert.equal(
		body.includes("voiceStore.discard(id);\n\t\t\tnotify("),
		false,
		"the silent path must not delete the take"
	);
	assert.match(
		src,
		/notify\("Nothing was heard — try closer to the microphone\.", \{ type: "info" \}\);/,
		"…and the user is still told, in the moment, what happened"
	);
	assert.match(
		src,
		/if \(f\.noSpeech\) return `Recording \$\{len\} — nothing was heard`;/,
		"the chip must say what happened rather than the riddle 'didn't transcribe'"
	);
	assert.match(
		src,
		/const failedChipRetryLabel = \(f\) => \(f\.noSpeech \? "Transcribe anyway" : "Retry"\);/,
		"…and offer the honest verb: the measurement can be wrong, so transcribing anyway is a real choice"
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
	assert.ok(
		banner.includes("laterTake(t)"),
		"a non-destructive dismiss must exist: otherwise the only ways out are to act now or see " +
			"the banner on every reload, which is pressure toward the one button that deletes"
	);
});

test("recovery is scoped by a session id that EXISTS by the time the banner is built", () => {
	const mount = src.slice(
		src.indexOf("if (ui.value && ui.value.stt_enabled && micRec.supported)")
	);
	assert.match(
		mount.slice(0, 200),
		/\{\n\t\t_ensureVoiceSession\(\);\n\t\t_loadRecovery\(\);\n\t\}/,
		"_voiceSessionId is minted by _ensureVoiceSession; loading recovery first left it null, so " +
			"excludeSessionId never applied and the banner could offer THIS tab's own live take"
	);
	assert.match(
		src,
		/const takes = await listOrphanRecordings\(_voiceSessionId, session\.user\);/,
		"the exclusion + the cross-user gate both ride on that call"
	);
	assert.match(
		src,
		/_recoveryRecheck = setTimeout\(/,
		"a take held back as 'still warm' must be re-read once the grace window passes, or a reload " +
			"seconds after a crash would show nothing at all"
	);
});

test("the un-saveable-audio chip has a way out, and its confirm names the real risk", () => {
	const chip = src.slice(
		src.indexOf("voiceQ.unpersisted &&"),
		src.indexOf("#left-toolbar", src.indexOf("voiceQ.unpersisted &&"))
	);
	assert.ok(
		chip.includes("discardUnpersistedRecording(u.id)"),
		"Retry + Download only meant this chip could never be resolved — and it arms the leave guard"
	);
	assert.match(
		src,
		/"This audio could not be saved to disk — discarding loses it\./,
		"the copy must not borrow the durable case's reassurance: there is no second copy here"
	);
});

test("the approaching-cap warning fires once, before the cut, not after it", () => {
	assert.match(
		recSrc,
		/const NEAR_CAP_WARN_S = 30;/,
		"the cap is otherwise only ever explained after it has already cut a sentence in half"
	);
	assert.match(
		recSrc,
		/nearCapWarned = true;\n\t\t\t\tonNearCap\(maxDurationS - durationS\.value\);/
	);
	assert.match(
		src,
		/onNearCap: \(secondsLeft\) => \{/,
		"…and the composer must actually say it"
	);
});

test("Send visibly refuses while a dictation is still landing, with the reason on the button", () => {
	assert.match(
		src,
		/!voiceSendBlockReason\.value\n\);/,
		"canSend must fold in the voice block — a lit button that swallows the click behind a " +
			"3-second toast is the control lying about what it does"
	);
	assert.match(
		src,
		/:sendTitle="voiceSendBlockReason"/,
		"…and the reason has to reach the user, not just disable the button"
	);
	// The Enter path bypasses canSend (onKey calls send() directly), so send()'s own guard stays.
	assert.match(
		sendBody(),
		/if \(micState\.value === "recording" \|\| voiceBusyCount\.value > 0\) \{/,
		"the send-time guard is what actually prevents the words being dropped"
	);
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

// ── the composer lockout, driven against the REAL store ──────────────────────────────────────
// ChatView cannot be imported here (single-file component, no harness) and useDictationRecorder
// imports `vue`, so this drives the REAL store through a replica of startMic/stopMic and a
// recorder stand-in that reproduces the ONE property the bug turned on: the browser fires
// `onstop` on a LATER task, so micState is already 'idle' while the recorder is still recording.
// The source assertions above fence the replica against the real handlers.
function makeMic(store) {
	let micState = "idle";
	let activeId = null;
	let recState = "idle";
	let starting = false;
	let pendingStop = null;
	const mic = {
		get micState() {
			return micState;
		},
		async start() {
			// startMic's guard, verbatim in shape.
			if (
				micState === "recording" ||
				starting ||
				recState === "recording" ||
				activeId != null
			)
				return;
			starting = true;
			const started = recState === "recording" ? false : ((recState = "recording"), true);
			starting = false;
			if (!started || recState !== "recording") return;
			activeId = store.begin({ conversationId: "c1" });
			micState = "recording";
		},
		async stop() {
			if (micState !== "recording") return;
			micState = "idle"; // synchronous, exactly as in ChatView
			// the recorder stays 'recording' until the browser's async onstop lands
			pendingStop = true;
		},
		// the browser's onstop, one task later: flush the take and hand it over
		settle() {
			if (!pendingStop) return;
			pendingStop = null;
			recState = "idle";
			const id = activeId;
			activeId = null;
			if (id != null) store.finish(id, { blob: "take", durationS: 20 });
		},
	};
	return mic;
}

test("a double-click (and auto-stop → immediate click) cannot strand a take or lock out sending", async () => {
	const settle = [];
	const store = createVoiceDictationStore({
		transcribe: (rec) => new Promise((res) => settle.push({ id: rec.id, res })),
		retainUntilSent: true,
	});
	const mic = makeMic(store);
	const busy = () => {
		const s = store.snapshot();
		return s.transcribing.length + (s.capturing || 0);
	};

	await mic.start();
	assert.equal(store.snapshot().total, 1, "one take, one recording unit");
	// DOUBLE-CLICK: stop, then a second click before the browser's onstop lands. The button reads
	// "Dictate" in that window, which is exactly what the user sees after the 5-minute auto-stop
	// toast too — "transcribing what you said" invites the next click immediately.
	await mic.stop();
	await mic.start();
	assert.equal(
		store.snapshot().total,
		1,
		"the second click must NOT mint a second recording — the first one's fragments are all in it"
	);
	mic.settle();
	await flush();
	settle.find((c) => c.id === 0).res("the words I said");
	await flush();
	assert.equal(
		busy(),
		0,
		"…and the composer is free again: send() is blocked while this is > 0"
	);
	assert.equal(
		store.hasUnfinishedReason(),
		"live",
		"only the un-sent draft remains outstanding"
	);
	const snap = store.snapshot();
	assert.equal(snap.total, 1, "exactly ONE recording existed, start to finish");
	assert.equal(snap.capturing, 0, "nothing is stranded in `recording` forever");
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
