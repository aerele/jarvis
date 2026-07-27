// Real executable test for the client-side near-silence gate (voiceSilenceGate.js) and for
// the recorder→composer wiring that carries it. Plain node built-ins (node:test +
// node:assert), like voiceChunkQueue.test.js / voiceSendGlue.test.js. Run directly
// (`node --test voiceSilenceGate.test.js`) or via the python suite
// (jarvis/tests/test_voice_silence_gate_client.py subprocess-runs it every CI run).
//
// What is being defended: whisper-large-v3-turbo hallucinates a confident phrase onto silent
// audio ("Thank you." 3/3 on a pure-silence 15 s webm), the provider exposes no
// no_speech_prob to filter on server-side, and a phrase blocklist was vetoed — so a measured
// pause must be dropped in the BROWSER, before any upload. And the counter-invariant that
// matters more: a clip the meter could not measure must ALWAYS be transcribed.
//
// The meter's WebAudio graph and the composer's clip handoff are both driven here: the graph
// against injected fakes (deps-injection, so no browser is needed), the handoff against the
// REAL createVoiceChunkQueue, plus source assertions that fence the ChatView/recorder wiring
// the replica stands in for (the voiceChipLifecycle.test.js precedent).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRmsMeter, isNearSilent, SILENCE_PEAK_RMS } from "./voiceSilenceGate.js";
import { createVoiceChunkQueue } from "./voiceChunkQueue.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CHAT_VIEW = path.join(HERE, "..", "views", "ChatView.vue");
const RECORDER = path.join(HERE, "..", "composables", "useChunkedRecorder.js");
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

test("an UNMEASURED clip is never silent — absence of measurement must not drop audio", () => {
	for (const v of [undefined, null, NaN, Infinity, "0", "", false, {}, []]) {
		assert.equal(
			isNearSilent(v),
			false,
			`peakRms ${String(v)} is not a measurement — the clip must still be transcribed`
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
test("the meter tracks the loudest RMS across the cycle, not the last one", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, a.deps);
	assert.equal(m.available, true);
	a.setLevel(0.3);
	a.tick();
	a.setLevel(0.0);
	a.tick(5); // a long pause AFTER a word must not erase the word
	assert.ok(Math.abs(m.peak() - 0.3) < 1e-6, `peak should be 0.3, got ${m.peak()}`);
	assert.equal(isNearSilent(m.peak()), false, "a clip containing a word is transcribed");
});

test("a whole cycle of true silence measures as silent", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, a.deps);
	a.setLevel(0);
	a.tick(10);
	assert.equal(m.peak(), 0);
	assert.equal(isNearSilent(m.peak()), true);
});

test("reset() makes the peak PER CLIP — a loud clip doesn't shield the next one", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, a.deps);
	a.setLevel(0.4);
	a.tick(3);
	assert.equal(isNearSilent(m.peak()), false);
	m.reset(); // the recorder calls this as each new cycle's MediaRecorder starts
	assert.equal(
		m.peak(),
		undefined,
		"straight after a reset nothing is sampled yet — UNMEASURED"
	);
	a.setLevel(0);
	a.tick(4);
	assert.equal(m.peak(), 0);
	assert.equal(isNearSilent(m.peak()), true, "the silent cycle after a loud one is gated");
});

test("a clip that ends before the first sample is UNMEASURED, not silent", () => {
	const a = makeAudioStack();
	const m = createRmsMeter(STREAM, a.deps);
	assert.equal(m.peak(), undefined, "no ticks yet — nothing was measured");
	assert.equal(isNearSilent(m.peak()), false, "…so the clip is transcribed");
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

// ── the clip handoff, end to end against the REAL queue ────────────────────────────────────
// Mirrors ChatView's micRec.onClip exactly (the source assertions below fence the mirroring).
function makeHandoff() {
	const asked = [];
	const commits = [];
	const gaps = [];
	const fails = [];
	const skipped = [];
	const q = createVoiceChunkQueue({
		transcribe: (clip) => {
			asked.push(clip.seq);
			return Promise.resolve("hello there");
		},
		retainUntilSent: true,
		maxAttempts: 2,
		onCommit: (seq, text) => commits.push([seq, text]),
		onGap: (seq) => gaps.push(seq),
		onFail: (seq) => fails.push(seq),
	});
	return {
		q,
		asked,
		commits,
		gaps,
		fails,
		skipped,
		onClip(clip) {
			if (isNearSilent(clip.peakRms)) {
				skipped.push(clip);
				return;
			}
			q.enqueue({ ...clip, conversationId: "chatA" });
		},
	};
}

test("END TO END: a measured-silent clip is dropped — no transcribe, no chip, no placeholder", async () => {
	const h = makeHandoff();
	h.onClip({ blob: "silence", durationS: 15, peakRms: 0 });
	await flush();
	assert.deepEqual(h.asked, [], "a silent clip must never reach the transcription endpoint");
	assert.deepEqual(h.commits, [], "it contributes no text");
	assert.deepEqual(h.gaps, [], "and leaves NO ⟦clip⟧ placeholder in the composer");
	assert.deepEqual(h.fails, [], "it is not a failure — nothing failed, nothing happened");
	const snap = h.q.snapshot();
	assert.equal(snap.total, 0, "the queue never saw it");
	assert.equal(snap.failed.length, 0, "so there is no chip to show");
	assert.equal(snap.unpersisted.length, 0);
	assert.equal(snap.hasUnfinished, false);
	assert.equal(h.q.hasUnfinishedReason(), null, "and the leave guard is not armed by a pause");
	assert.equal(h.skipped.length, 1);
});

test("END TO END: a clip with speech in it passes the gate and transcribes normally", async () => {
	const h = makeHandoff();
	h.onClip({ blob: "speech", durationS: 15, peakRms: 0.18 });
	await flush();
	assert.deepEqual(h.asked, [0], "the loud clip is transcribed");
	assert.deepEqual(h.commits, [[0, "hello there"]]);
	assert.deepEqual(h.gaps, []);
});

test("END TO END: an UNMEASURED clip (no meter) is always transcribed", async () => {
	const h = makeHandoff();
	h.onClip({ blob: "unknown", durationS: 15 }); // no peakRms key at all
	h.onClip({ blob: "unknown2", durationS: 15, peakRms: undefined });
	await flush();
	assert.deepEqual(h.asked, [0, 1], "no measurement means no skipping — both clips go up");
	assert.equal(h.skipped.length, 0);
});

test("END TO END: skipping a pause does NOT disturb the seq order of the clips around it", async () => {
	const h = makeHandoff();
	h.onClip({ blob: "a", durationS: 15, peakRms: 0.2 });
	h.onClip({ blob: "pause", durationS: 15, peakRms: 0.0004 });
	h.onClip({ blob: "b", durationS: 15, peakRms: 0.2 });
	await flush();
	assert.deepEqual(
		h.asked,
		[0, 1],
		"the two spoken clips take seqs 0 and 1 — the pause takes none"
	);
	assert.deepEqual(
		h.commits.map(([seq]) => seq),
		[0, 1],
		"both commit contiguously; the dropped pause leaves no hole for the cursor to stall on"
	);
	assert.equal(h.q.snapshot().total, 2);
});

// ── the wiring the replica above stands in for ─────────────────────────────────────────────
test("ChatView's onClip runs the gate BEFORE the queue ever sees the clip", () => {
	const start = chatSrc.indexOf("const micRec = useChunkedRecorder({");
	assert.notEqual(start, -1, "ChatView must still build the chunked recorder");
	const end = chatSrc.indexOf("\nasync function startMic()", start);
	assert.notEqual(end, -1, "could not find the end of the recorder wiring");
	const body = chatSrc.slice(start, end);
	const gate = body.indexOf("if (isNearSilent(clip.peakRms)) {");
	const enqueue = body.indexOf("voiceQueue.enqueue(");
	assert.notEqual(gate, -1, "the near-silence gate is missing from the clip handoff");
	assert.notEqual(enqueue, -1, "the handoff must still enqueue non-silent clips");
	assert.ok(
		gate < enqueue,
		"gating AFTER the enqueue would upload the silence anyway — the whole point is that a " +
			"pause never leaves the browser"
	);
	assert.match(
		body,
		/if \(isNearSilent\(clip\.peakRms\)\) \{[\s\S]*?return;\n/,
		"a gated clip must RETURN — it may not fall through into the queue"
	);
	assert.match(
		chatSrc,
		/import \{ isNearSilent \} from "@\/utils\/voiceSilenceGate";/,
		"the gate must be the shared module, not a re-implemented threshold in the view"
	);
	// The gated path must not fabricate any of the artifacts a real clip produces.
	const gatedBlock = body.slice(gate, enqueue);
	for (const forbidden of ["_insertGapPlaceholder", "notify(", "_takeCommitted", "onFail"]) {
		assert.equal(
			gatedBlock.includes(forbidden),
			false,
			`a skipped pause must not ${forbidden} — it produces no chip, no placeholder, no toast`
		);
	}
});

test("the recorder measures on the SAME stream and stamps only a REAL measurement", () => {
	assert.match(
		recSrc,
		/import \{ createRmsMeter \} from "@\/utils\/voiceSilenceGate";/,
		"the recorder must use the shared meter"
	);
	const meterAt = recSrc.indexOf("meter = createRmsMeter(stream);");
	assert.notEqual(meterAt, -1, "the meter must ride the SAME MediaStream as the MediaRecorder");
	const firstRecorder = recSrc.indexOf("_newRecorder();", meterAt);
	assert.notEqual(firstRecorder, -1);
	assert.ok(
		meterAt < firstRecorder,
		"the meter has to exist before the first cycle starts, or clip 1 is always unmeasured"
	);
	assert.match(
		recSrc,
		/if \(typeof peakRms === "number"\) clip\.peakRms = peakRms;/,
		"only a real number may be stamped — an unmeasured clip must carry NO peakRms key"
	);
});

test("the recorder resets the peak per cycle and reads it before the next cycle overwrites it", () => {
	const newRec = recSrc.slice(
		recSrc.indexOf("function _newRecorder()"),
		recSrc.indexOf("function _onStop()")
	);
	assert.match(
		newRec,
		/meter\?\.reset\(\)/,
		"each cycle must start from a clean peak, or one loud clip shields every later pause"
	);
	const onStop = recSrc.slice(
		recSrc.indexOf("function _onStop()"),
		recSrc.indexOf("async function start()")
	);
	const read = onStop.indexOf("meter.peak()");
	const emit = onStop.indexOf("onClip(clip);");
	const restart = onStop.indexOf("_newRecorder();");
	assert.notEqual(read, -1, "the finished cycle's peak must be read in _onStop");
	assert.notEqual(emit, -1);
	assert.notEqual(restart, -1);
	assert.ok(read < emit && read < restart, "read the peak BEFORE the next cycle resets it");
});

test("releasing the stream tears the meter down with it (no orphan AudioContext)", () => {
	const release = recSrc.slice(
		recSrc.indexOf("function _releaseStream()"),
		recSrc.indexOf("function _clearTimers()")
	);
	assert.match(release, /meter\?\.stop\(\)/, "the meter must die with the stream it measures");
	assert.match(release, /meter = null;/);
});
