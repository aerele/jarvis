// Real executable test for the client-side near-silence gate (voiceSilenceGate.js) and for the
// recorder→composer wiring that carries it. Plain node built-ins (node:test + node:assert), like
// voiceDictationStore.test.js / voiceSendGlue.test.js. Run directly
// (`node --test voiceSilenceGate.test.js`) or via the python suite
// (jarvis/tests/test_voice_silence_gate_client.py subprocess-runs it every CI run).
//
// What is being defended: whisper-large-v3-turbo hallucinates a confident phrase onto silent
// audio ("Thank you." 3/3 on a pure-silence 15 s webm), the provider exposes no no_speech_prob to
// filter on server-side, and a phrase blocklist was vetoed — so a take nobody spoke into must be
// dropped in the BROWSER, before any upload. And the counter-invariant that matters more: a take
// the meter could not measure must ALWAYS be transcribed.
//
// The gate is now WHOLE-TAKE: one dictation is one recording, so the peak describes the entire
// recording and a single audible word anywhere in it clears the gate. Only a recording that was
// inaudible from end to end is skipped — a pause mid-sentence is just part of the take.
//
// The meter's WebAudio graph and the composer's take handoff are both driven here: the graph
// against injected fakes (deps-injection, so no browser is needed), the handoff against the REAL
// createVoiceDictationStore, plus source assertions that fence the ChatView/recorder wiring the
// replica stands in for (the lifecycle-test precedent).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRmsMeter, isNearSilent, SILENCE_PEAK_RMS } from "./voiceSilenceGate.js";
import { createVoiceDictationStore } from "./voiceDictationStore.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CHAT_VIEW = path.join(HERE, "..", "views", "ChatView.vue");
const RECORDER = path.join(HERE, "..", "composables", "useDictationRecorder.js");
const chatSrc = fs.readFileSync(CHAT_VIEW, "utf8");
const recSrc = fs.readFileSync(RECORDER, "utf8");

const flush = () => new Promise((r) => setTimeout(r, 0));

// A whole fake WebAudio stack + fake timers, so the meter's real code runs with no browser.
// `level` is the constant sample value the analyser reports, so RMS == |level| exactly and a
// test can name the peak it expects instead of approximating one.
function makeAudioStack(opts = {}) {
	const log = [];
	let level = 0;
	const timers = [];
	class FakeNode {
		constructor(kind) {
			this.kind = kind;
		}
		connect() {
			log.push(`${this.kind}:connect`);
		}
		disconnect() {
			log.push(`${this.kind}:disconnect`);
		}
	}
	class FakeAnalyser extends FakeNode {
		constructor() {
			super("analyser");
			this.fftSize = 2048;
		}
		getFloatTimeDomainData(buf) {
			if (opts.readThrows) throw new Error("analyser is gone");
			for (let i = 0; i < buf.length; i++) buf[i] = level;
		}
	}
	class FakeAudioContext {
		constructor() {
			if (opts.ctorThrows) throw new Error("AudioContext unavailable");
			this.state = opts.state || "running";
			this.destination = new FakeNode("destination");
			log.push("ctx:new");
		}
		createMediaStreamSource() {
			if (opts.sourceThrows) throw new Error("stream refused");
			return new FakeNode("source");
		}
		createAnalyser() {
			return new FakeAnalyser();
		}
		createGain() {
			if (opts.gainThrows) throw new Error("no gain node");
			const g = new FakeNode("gain");
			g.gain = { value: 1 };
			return g;
		}
		resume() {
			log.push("ctx:resume");
			return Promise.resolve();
		}
		close() {
			log.push("ctx:close");
			return Promise.resolve();
		}
		_suspend() {
			this.state = "suspended";
		}
	}
	const ctxs = [];
	class Tracked extends FakeAudioContext {
		constructor() {
			super();
			ctxs.push(this);
		}
	}
	return {
		log,
		ctxs,
		deps: {
			AudioContext: Tracked,
			setInterval: (fn) => {
				timers.push(fn);
				return timers.length;
			},
			clearInterval: (id) => {
				log.push(`clear:${id}`);
				timers[id - 1] = null;
			},
		},
		setLevel(v) {
			level = v;
		},
		tick(n = 1) {
			for (let i = 0; i < n; i++) for (const fn of timers) if (fn) fn();
		},
	};
}

const STREAM = { id: "fake-stream" }; // opaque to the meter — it only hands it to WebAudio

// ── isNearSilent: the decision, biased hard toward transcribing ────────────────────────────
test("isNearSilent skips ONLY a real measurement under the threshold", () => {
	assert.equal(isNearSilent(0), true, "digital silence is silence");
	assert.equal(isNearSilent(SILENCE_PEAK_RMS / 2), true, "well under the threshold is silence");
	assert.equal(isNearSilent(0.2), false, "speech-level audio is transcribed");
	assert.equal(
		isNearSilent(SILENCE_PEAK_RMS * 1.5),
		false,
		"above the threshold is transcribed"
	);
});

test("the threshold boundary keeps the audio: exactly AT the threshold is NOT silent", () => {
	assert.equal(
		isNearSilent(SILENCE_PEAK_RMS),
		false,
		"the comparison must be strictly `<` — a tie has to go to transcribing, never to dropping"
	);
	// One ULP below still skips, so the boundary is a real boundary and not a dead zone.
	const justUnder = SILENCE_PEAK_RMS - Number.EPSILON * SILENCE_PEAK_RMS;
	assert.ok(justUnder < SILENCE_PEAK_RMS);
	assert.equal(isNearSilent(justUnder), true);
});

test("an UNMEASURED take is never silent — absence of measurement must not drop audio", () => {
	for (const v of [undefined, null, NaN, Infinity, "0", "", false, {}, []]) {
		assert.equal(
			isNearSilent(v),
			false,
			`peakRms ${String(v)} is not a measurement — the take must still be transcribed`
		);
	}
});

test("the threshold is conservative: far below any plausible speech level", () => {
	assert.ok(SILENCE_PEAK_RMS > 0, "a zero threshold could never gate anything");
	assert.ok(
		SILENCE_PEAK_RMS <= 0.01,
		"peak RMS 0.01 (~-40 dBFS) is already quiet speech territory — the gate must stay under it"
	);
});

// ── createRmsMeter: the WebAudio half ──────────────────────────────────────────────────────
test("the meter tracks the loudest RMS across the take, not the last one", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, a.deps);
	assert.equal(m.available, true);
	a.setLevel(0.3);
	a.tick();
	a.setLevel(0.0);
	a.tick(5); // a long pause AFTER a word must not erase the word
	assert.ok(Math.abs(m.peak() - 0.3) < 1e-6, `peak should be 0.3, got ${m.peak()}`);
	assert.equal(isNearSilent(m.peak()), false, "a take containing a word is transcribed");
});

test("a take that is silent from end to end measures as silent", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, a.deps);
	a.setLevel(0);
	a.tick(10);
	assert.equal(m.peak(), 0);
	assert.equal(isNearSilent(m.peak()), true);
});

test("reset() re-arms the meter — a loud take does not shield a later one", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, a.deps);
	a.setLevel(0.4);
	a.tick(3);
	assert.equal(isNearSilent(m.peak()), false);
	m.reset(); // each take starts from a clean peak (in prod the meter is built per take)
	assert.equal(
		m.peak(),
		undefined,
		"straight after a reset nothing is sampled yet — UNMEASURED"
	);
	a.setLevel(0);
	a.tick(4);
	assert.equal(m.peak(), 0);
	assert.equal(isNearSilent(m.peak()), true, "the silent take after a loud one is gated");
});

test("a take that ends before the first sample is UNMEASURED, not silent", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, a.deps);
	assert.equal(m.peak(), undefined, "no ticks yet — nothing was measured");
	assert.equal(isNearSilent(m.peak()), false, "…so the take is transcribed");
});

test("a SUSPENDED AudioContext reads all-zero — that must report UNMEASURED, never silence", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, a.deps);
	a.setLevel(0.5);
	a.tick(2);
	assert.equal(isNearSilent(m.peak()), false);
	a.ctxs[0]._suspend(); // autoplay policy / device change parks the context mid-take
	a.setLevel(0);
	a.tick(2);
	assert.equal(
		m.peak(),
		undefined,
		"a parked context's zeros look exactly like silence — reporting them would delete real speech"
	);
	assert.equal(isNearSilent(m.peak()), false);
});

test("an analyser read that throws contributes no sample — UNMEASURED, and never throws out", () => {
	const a = makeAudioStack({ readThrows: true });
	const m = createRmsMeter(STREAM, a.deps);
	assert.doesNotThrow(() => a.tick(3));
	assert.equal(m.peak(), undefined);
	assert.equal(isNearSilent(m.peak()), false);
});

test("no AudioContext at all → a dead meter that measures nothing and skips nothing", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, { ...a.deps, AudioContext: null });
	assert.equal(m.available, false);
	assert.equal(m.peak(), undefined);
	assert.doesNotThrow(() => {
		m.reset();
		m.stop();
	});
	assert.equal(isNearSilent(m.peak()), false);
});

test("a throwing AudioContext constructor degrades to the dead meter", () => {
	const a = makeAudioStack({ ctorThrows: true });
	const m = createRmsMeter(STREAM, a.deps);
	assert.equal(m.available, false);
	assert.equal(m.peak(), undefined);
});

test("a throwing createMediaStreamSource degrades AND closes the context it opened", () => {
	const a = makeAudioStack({ sourceThrows: true });
	const m = createRmsMeter(STREAM, a.deps);
	assert.equal(m.available, false);
	assert.equal(m.peak(), undefined);
	assert.ok(
		a.log.includes("ctx:close"),
		"a half-built graph must not leak an open AudioContext"
	);
});

test("a missing stream degrades to the dead meter (no graph is even attempted)", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(null, a.deps);
	assert.equal(m.available, false);
	assert.equal(a.log.includes("ctx:new"), false);
});

test("the muted pull-sink is best-effort: createGain throwing still leaves a working meter", () => {
	const a = makeAudioStack({ gainThrows: true });
	const m = createRmsMeter(STREAM, a.deps);
	assert.equal(m.available, true, "no gain node is not a reason to stop measuring");
	a.setLevel(0.25);
	a.tick(2);
	assert.ok(Math.abs(m.peak() - 0.25) < 1e-6);
});

test("the sink is MUTED and terminates at the destination (guaranteed pull, no feedback)", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, a.deps);
	// gain 0: the mic is never routed audibly back to the speakers.
	assert.equal(a.log.filter((l) => l === "gain:connect").length, 1);
	m.stop();
});

test("stop() clears the timer, disconnects the graph and closes the context", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, a.deps);
	a.setLevel(0.4);
	a.tick();
	m.stop();
	assert.ok(
		a.log.some((l) => l.startsWith("clear:")),
		"the sampling interval must be cleared"
	);
	assert.ok(a.log.includes("source:disconnect"));
	assert.ok(a.log.includes("analyser:disconnect"));
	assert.ok(a.log.includes("ctx:close"));
	a.tick(3); // a cleared timer must genuinely stop sampling
	assert.doesNotThrow(() => m.stop(), "a second stop() (dispose after stop) must be safe");
});

// ── the take handoff, end to end against the REAL store ────────────────────────────────────
// Mirrors ChatView's micRec.onDone exactly (the source assertions below fence the mirroring).
function makeHandoff() {
	const asked = [];
	const commits = [];
	const fails = [];
	const skipped = [];
	const store = createVoiceDictationStore({
		transcribe: (rec) => {
			asked.push(rec.id);
			return Promise.resolve("hello there");
		},
		retainUntilSent: true,
		maxAttempts: 2,
		onCommit: (id, text) => commits.push([id, text]),
		onFail: (id) => fails.push(id),
	});
	return {
		store,
		asked,
		commits,
		fails,
		skipped,
		// Record a whole take, then hand it over the way the recorder does.
		dictate(take) {
			const id = store.begin({ conversationId: "chatA" });
			store.addFragment(id, { index: 0, blob: take.blob, durationS: take.durationS });
			// ChatView's onDone, verbatim in shape: gate FIRST, discard on silence, else finish.
			if (isNearSilent(take.peakRms)) {
				store.discard(id);
				skipped.push(take);
				return id;
			}
			store.finish(id, take);
			return id;
		},
	};
}

test("END TO END: a take measured silent from end to end is dropped — no upload, no chip, no guard", async () => {
	const h = makeHandoff();
	h.dictate({ blob: "silence", durationS: 42, peakRms: 0 });
	await flush();
	assert.deepEqual(h.asked, [], "a silent take must never reach the transcription endpoint");
	assert.deepEqual(h.commits, [], "it contributes no text");
	assert.deepEqual(h.fails, [], "it is not a failure — nothing failed, nothing happened");
	const snap = h.store.snapshot();
	assert.equal(snap.total, 0, "the discarded take is invisible to the UI");
	assert.equal(snap.failed.length, 0, "so there is no chip to show");
	assert.equal(snap.transcribing.length, 0, "and no pill either");
	assert.equal(snap.unpersisted.length, 0);
	assert.equal(h.store.hasUnfinishedReason(), null, "the leave guard is not armed by silence");
	assert.equal(h.skipped.length, 1);
});

test("END TO END: a take with speech in it passes the gate and transcribes as ONE recording", async () => {
	const h = makeHandoff();
	const id = h.dictate({ blob: "speech", durationS: 42, peakRms: 0.18 });
	await flush();
	assert.deepEqual(h.asked, [id], "the audible take is transcribed — once");
	assert.deepEqual(h.commits, [[id, "hello there"]]);
});

test("END TO END: an UNMEASURED take (no meter) is always transcribed", async () => {
	const h = makeHandoff();
	const a = h.dictate({ blob: "unknown", durationS: 20 }); // no peakRms key at all
	const b = h.dictate({ blob: "unknown2", durationS: 20, peakRms: undefined });
	await flush();
	assert.deepEqual(h.asked, [a, b], "no measurement means no skipping — both takes go up");
	assert.equal(h.skipped.length, 0);
});

test("END TO END: one audible word anywhere in a take clears the gate for the WHOLE take", async () => {
	// The gate is per-RECORDING now: a long take that is mostly pauses still transcribes, because
	// its peak is the loudest moment in it. Only an entirely inaudible take is skipped — which is
	// exactly why the per-clip gate could never have been enough.
	const h = makeHandoff();
	const id = h.dictate({ blob: "mostly pauses, one sentence", durationS: 300, peakRms: 0.2 });
	await flush();
	assert.deepEqual(h.asked, [id]);
	assert.equal(h.skipped.length, 0);
});

// ── the wiring the replica above stands in for ─────────────────────────────────────────────
test("ChatView's onDone runs the gate BEFORE the recording is ever uploaded", () => {
	const start = chatSrc.indexOf("const micRec = useDictationRecorder({");
	assert.notEqual(start, -1, "ChatView must still build the dictation recorder");
	const end = chatSrc.indexOf("\nasync function startMic()", start);
	assert.notEqual(end, -1, "could not find the end of the recorder wiring");
	const body = chatSrc.slice(start, end);
	const gate = body.indexOf("if (isNearSilent(take.peakRms)) {");
	const finish = body.indexOf("voiceStore.finish(id, take);");
	assert.notEqual(gate, -1, "the near-silence gate is missing from the take handoff");
	assert.notEqual(finish, -1, "the handoff must still transcribe an audible take");
	assert.ok(
		gate < finish,
		"gating AFTER finish() would upload the silence anyway — the whole point is that an " +
			"inaudible take never leaves the browser"
	);
	assert.match(
		body,
		/if \(isNearSilent\(take\.peakRms\)\) \{[\s\S]*?return;\n/,
		"a gated take must RETURN — it may not fall through into transcription"
	);
	assert.match(
		body,
		/voiceStore\.discard\(id\);/,
		"…and its mirrored fragments must be dropped, not left arming the recovery banner"
	);
	assert.match(
		chatSrc,
		/import \{ isNearSilent \} from "@\/utils\/voiceSilenceGate";/,
		"the gate must be the shared module, not a re-implemented threshold in the view"
	);
	// The gated path must not fabricate any of the artifacts a real recording produces.
	const gatedBlock = body.slice(gate, finish);
	for (const forbidden of ["voiceStore.finish", "onFail"]) {
		assert.equal(
			gatedBlock.includes(forbidden),
			false,
			`a skipped silent take must not ${forbidden} — it produces no chip and no failure`
		);
	}
});

test("the recorder measures on the SAME stream, for the WHOLE take, and stamps only a REAL measurement", () => {
	assert.match(
		recSrc,
		/import \{ createRmsMeter \} from "@\/utils\/voiceSilenceGate";/,
		"the recorder must use the shared meter"
	);
	const meterAt = recSrc.indexOf("meter = createRmsMeter(stream);");
	assert.notEqual(meterAt, -1, "the meter must ride the SAME MediaStream as the MediaRecorder");
	const recorderAt = recSrc.indexOf("new MediaRecorder(stream", meterAt);
	assert.notEqual(recorderAt, -1);
	assert.ok(
		meterAt < recorderAt,
		"the meter has to exist before the recorder starts, or the take is always unmeasured"
	);
	assert.equal(
		recSrc.includes("meter?.reset()"),
		false,
		"the peak is PER TAKE now — resetting it mid-recording would gate on a fragment again"
	);
	assert.match(
		recSrc,
		/if \(typeof peakRms === "number"\) take\.peakRms = peakRms;/,
		"only a real number may be stamped — an unmeasured take must carry NO peakRms key"
	);
});

test("the take's peak is read BEFORE the stream (and its meter) is released", () => {
	const emit = recSrc.slice(
		recSrc.indexOf("function _emitTake(keep)"),
		recSrc.indexOf("function _onStop()")
	);
	assert.match(emit, /peakRms = meter \? meter\.peak\(\) : undefined;/);
	const onStop = recSrc.slice(
		recSrc.indexOf("function _onStop()"),
		recSrc.indexOf("async function start()")
	);
	const emitAt = onStop.indexOf("_emitTake(keep);");
	const releaseAt = onStop.indexOf("_releaseStream();");
	assert.ok(emitAt !== -1 && releaseAt !== -1);
	assert.ok(
		emitAt < releaseAt,
		"releasing first would tear the meter down before its peak is read"
	);
});

test("releasing the stream tears the meter down with it (no orphan AudioContext)", () => {
	const release = recSrc.slice(
		recSrc.indexOf("function _releaseStream()"),
		recSrc.indexOf("function _clearTimers()")
	);
	assert.match(release, /meter\?\.stop\(\)/, "the meter must die with the stream it measures");
	assert.match(release, /meter = null;/);
});
