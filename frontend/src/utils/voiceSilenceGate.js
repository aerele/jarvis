// voiceSilenceGate — the CLIENT-SIDE near-silence gate for voice dictation.
//
// whisper-large-v3-turbo deterministically HALLUCINATES on near-silent audio: a 15 s
// pure-silence webm transcribes as "Thank you." (3/3 on the probe). A dictation nobody
// actually spoke into — a muted mic, a hot key pressed by accident — would otherwise come
// back as a confident little phrase the user never said.
//
// It CANNOT be filtered server-side: OpenRouter's transcription endpoint does not pass
// through verbose_json / no_speech_prob (probed — it 400s), and a phrase blocklist was
// vetoed because it would swallow the same words when a user genuinely dictates them. So the
// gate lives at the microphone instead: measured near-silence never leaves the browser.
//
// Two plain pieces, both dependency-injected so `node --test` can drive them without a
// browser (the eventFence.js precedent):
//   createRmsMeter(stream, deps) — an AnalyserNode riding the SAME MediaStream as the
//                                  MediaRecorder, tracking peak RMS across a take.
//   isNearSilent(peakRms)        — the decision, biased hard toward TRANSCRIBING.
//
// A positive gate result means "do not UPLOAD this", never "delete this": the composer routes a
// measured-silent take to the same retained, actionable failed chip as any other outcome
// (voiceDictationStore.finishSilent), because a wrong measurement must not cost a recording.
//
// The invariant that outranks the feature: ABSENCE OF MEASUREMENT MUST NEVER DROP AUDIO.
// Every failure mode — no AudioContext, a constructor or graph call that throws, a context
// the browser suspended (its analyser reads all-zero, which would look exactly like silence),
// a clip that ended before the first sample — reports the peak as `undefined`, and
// isNearSilent(undefined) is false. Only a real, positive measurement below the threshold
// skips a clip; everything else is uploaded exactly as it is today.

// Peak RMS below this is treated as true near-silence. AnalyserNode time-domain samples are
// nominally [-1, 1], so this is ~0.5% of full scale (≈ -46 dBFS) — far under any speech that
// could plausibly transcribe (even a close whisper sits well above it) and comfortably over
// the noise floor getUserMedia leaves after Chrome's default noise suppression. It is the PEAK
// across the WHOLE take, so a single word anywhere in it clears the gate; skipping needs the
// ENTIRE recording to be inaudible. Deliberately conservative: a too-low threshold merely
// restores today's behaviour (transcribe, risk a hallucination), a too-high one deletes speech.
export const SILENCE_PEAK_RMS = 0.005;

// AnalyserNode window size. 4096 samples ≈ 85 ms at 48 kHz, so at SAMPLE_MS = 100 the meter
// observes ~85% of the clip's timeline — a short word can't slip between two windows.
const FFT_SIZE = 4096;
const SAMPLE_MS = 100; // 10 Hz; one 4096-float RMS per tick is negligible work

const noop = () => {};

// A meter that measured nothing and therefore never skips anything. Returned for every
// unavailable / failed WebAudio path so callers need no capability branching of their own.
const DEAD_METER = Object.freeze({
	available: false,
	peak: () => undefined,
	reset: noop,
	stop: noop,
});

// Attach a peak-RMS meter to `stream` (the SAME MediaStream the MediaRecorder is recording,
// so the measurement describes exactly the audio in the clip).
//
// deps (all optional — real browsers need none):
//   AudioContext   constructor override (tests inject a fake)
//   setInterval / clearInterval   timer overrides
//   sampleMs       sampling period, default 100 ms
//
// Returns { available, peak(), reset(), stop() }. `peak()` is the highest RMS seen since the
// last reset(), or `undefined` when the measurement can't be trusted. The dictation recorder
// NEVER calls reset() — the peak is per TAKE, which is why one audible word anywhere in a long,
// pause-heavy recording clears the gate; `stop()` tears the graph down with the stream.
export function createRmsMeter(stream, deps = {}) {
	const Ctx =
		deps.AudioContext ||
		(typeof window !== "undefined" && (window.AudioContext || window.webkitAudioContext));
	const setIntervalFn =
		deps.setInterval || (typeof setInterval === "function" ? setInterval : null);
	const clearIntervalFn =
		deps.clearInterval || (typeof clearInterval === "function" ? clearInterval : null);
	const sampleMs = Math.max(20, deps.sampleMs || SAMPLE_MS);
	if (!stream || !Ctx || !setIntervalFn || !clearIntervalFn) return DEAD_METER;

	let ctx = null;
	let source = null;
	let analyser = null;
	let sink = null;
	let buf = null;
	try {
		ctx = new Ctx();
		source = ctx.createMediaStreamSource(stream);
		analyser = ctx.createAnalyser();
		analyser.fftSize = FFT_SIZE;
		source.connect(analyser);
		buf = new Float32Array(analyser.fftSize);
	} catch (e) {
		try {
			ctx && ctx.close && ctx.close();
		} catch (e2) {
			/* nothing to close */
		}
		return DEAD_METER; // no measurement → isNearSilent(undefined) → nothing is ever skipped
	}
	// A WebAudio graph is rendered because something PULLS it from the destination. The
	// mic-meter recipe of leaving the analyser dangling works in the browsers we ship to, but a
	// graph that is silently never pulled reads all-zero — which is indistinguishable from
	// silence and would drop every clip. Terminate the chain in a MUTED gain node so the pull is
	// guaranteed; gain 0 means nothing is ever audible (and no mic→speaker feedback loop).
	try {
		sink = ctx.createGain();
		sink.gain.value = 0;
		analyser.connect(sink);
		sink.connect(ctx.destination);
	} catch (e) {
		sink = null; // best-effort; the analyser still works everywhere we've measured
	}
	// An AudioContext can start suspended (autoplay policy). start() runs off a user gesture so
	// this should be a no-op, but resume anyway — and peak() re-checks state, so a context that
	// stays suspended reports "unmeasured" rather than a bogus zero.
	try {
		Promise.resolve(ctx.resume && ctx.resume()).catch(noop);
	} catch (e) {
		/* best-effort */
	}

	let peak = 0;
	let samples = 0;
	function _sample() {
		try {
			analyser.getFloatTimeDomainData(buf);
		} catch (e) {
			return; // a read that throws contributes no sample; peak() stays honest about that
		}
		let sum = 0;
		for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
		const rms = Math.sqrt(sum / buf.length);
		if (Number.isFinite(rms)) {
			if (rms > peak) peak = rms;
			samples += 1;
		}
	}
	let timer = setIntervalFn(_sample, sampleMs);

	return {
		available: true,
		peak() {
			// Nothing sampled yet (a clip shorter than one tick, or every read threw): UNMEASURED.
			if (!samples) return undefined;
			// A suspended/closed context feeds the analyser nothing but zeros. Reporting that as
			// silence would drop real speech, so an un-running context is UNMEASURED too.
			if (ctx.state && ctx.state !== "running") return undefined;
			return peak;
		},
		reset() {
			peak = 0;
			samples = 0;
		},
		stop() {
			if (timer != null) {
				try {
					clearIntervalFn(timer);
				} catch (e) {
					/* already cleared */
				}
				timer = null;
			}
			for (const node of [source, analyser, sink]) {
				try {
					node && node.disconnect && node.disconnect();
				} catch (e) {
					/* already disconnected */
				}
			}
			try {
				Promise.resolve(ctx.close && ctx.close()).catch(noop);
			} catch (e) {
				/* already closed */
			}
		},
	};
}

// Is this clip's measured peak RMS true near-silence — i.e. safe to drop WITHOUT transcribing?
//
// False for anything that isn't a finite number, so every unmeasured clip (no meter, suspended
// context, undefined/null/NaN/a string) is transcribed. Strictly `<` the threshold, so a clip
// sitting exactly ON the boundary is transcribed too: every tie goes to keeping the audio.
export function isNearSilent(peakRms, threshold = SILENCE_PEAK_RMS) {
	if (typeof peakRms !== "number" || !Number.isFinite(peakRms)) return false;
	return peakRms < threshold;
}
