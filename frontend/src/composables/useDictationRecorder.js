// useDictationRecorder — ONE MediaRecorder session per dictation, in TIMESLICE mode.
//
// `recorder.start(FRAGMENT_MS)` makes the browser hand us a ~15 s fragment while the take is
// still running, and each fragment is persisted the instant it arrives, so a tab crash costs at
// most the last fragment instead of the whole recording. At stop, `new Blob(fragments)` IS the
// recording: timeslice fragments are continuations of ONE stream, so concatenating them in
// arrival order reproduces exactly the bytes the recorder would have produced — no re-encode, no
// remux. That property is what lets us keep crash-safety AND still transcribe the take in ONE
// call with its full context.
//
// It replaces the stop/restart chunked recorder, which cut a take into independently decodable
// ~15 s clips so each could be POSTed on its own. That bought standalone clips at the price of
// CONTEXT: a speech model handed a fragment with no beginning and no end invents words to bridge
// the void, and a pause-only clip came back as a confident phrase nobody said. One recording,
// one transcription, whole context — the fragments exist only for crash-safety now, never as
// transcription units.
//
// The take is capped at `maxDurationS` (300 s, the server's limit): the cap AUTO-STOPS the
// recorder and hands over what was captured — it never discards it.
//
// Callbacks:
//   onFragment({index, blob, durationS, mimeType})  a timeslice fragment arrived mid-take;
//                                    `durationS` is the recording's length up to and including it
//                                    (so a crash-recovered take reports what actually survived).
//   onDone({blob, durationS, mimeType, peakRms, fragments})  the finished, assembled recording.
//   onCancel()                       the user abandoned the take; drop everything for it.
//   onAutoStop(durationS)            the cap fired — onDone follows with the captured audio.
//
// `peakRms` is the loudest RMS an AnalyserNode saw on the SAME MediaStream across the WHOLE take
// (per-session now, not per-chunk): the composer's silence gate reads it to skip uploading a take
// nobody spoke into. It is OMITTED whenever the meter couldn't measure (no WebAudio, a suspended
// context), and the gate treats a missing measurement as "transcribe" — the meter never costs audio.
import { reactive, ref } from "vue";
import { createRmsMeter } from "@/utils/voiceSilenceGate";

const MIME_PREFS = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
const DEFAULT_FRAGMENT_MS = 15000;
// The server rejects longer recordings (jarvis/chat/voice.py `_MAX_DURATION_S`), so the cap is
// enforced HERE, at capture time, where the audio can still be saved rather than refused.
const DEFAULT_MAX_SECONDS = 300;

export function useDictationRecorder(opts = {}) {
	const fragmentMs = Math.max(2000, opts.fragmentMs || DEFAULT_FRAGMENT_MS);
	const maxDurationS = Math.max(10, opts.maxDurationS || DEFAULT_MAX_SECONDS);
	const onFragment = typeof opts.onFragment === "function" ? opts.onFragment : () => {};
	const onDone = typeof opts.onDone === "function" ? opts.onDone : () => {};
	const onCancel = typeof opts.onCancel === "function" ? opts.onCancel : () => {};
	const onAutoStop = typeof opts.onAutoStop === "function" ? opts.onAutoStop : () => {};

	const state = ref("idle"); // 'idle' | 'recording' | 'error'
	const error = ref("");
	const durationS = ref(0);
	// Exposed so the route/leave guard can see the permission-prompt window too: `state` stays
	// 'idle' until getUserMedia is granted, so navigation DURING the prompt would slip past a
	// guard that only watches `state === 'recording'`.
	const starting = ref(false);
	const supported = !!(
		typeof navigator !== "undefined" &&
		navigator.mediaDevices &&
		navigator.mediaDevices.getUserMedia &&
		typeof window !== "undefined" &&
		window.MediaRecorder
	);

	let stream = null;
	let recorder = null;
	let meter = null; // whole-take peak-RMS meter on the same stream; never required
	let fragments = [];
	let mime = "";
	// Cancellation generation covering the pending getUserMedia. start() captures it before
	// awaiting the permission prompt; cancel()/dispose() bump it. A grant that resolves AFTER a
	// bump is recognised as STALE — its tracks are stopped instead of being wired into a recorder
	// on a torn-down / navigated-away component (a hot-mic leak). `starting` is the synchronous
	// re-entry guard too: set the instant start() is entered, BEFORE the async getUserMedia, so a
	// double-click — or an impatient second click while the prompt is open — cannot launch a
	// second recorder.
	let startGen = 0;
	let tick = null; // 250 ms clock + cap watchdog
	let startedAt = 0;
	let action = null; // what the in-flight recorder.stop() means: 'final' | 'cancel'
	let settle = null; // resolver for a stop()/cancel() awaiting onstop
	// This take has already been handed over (onDone/onCancel) — exactly once. Starts TRUE so a
	// cancel() before any take has begun hands nothing over.
	let emitted = true;

	function _releaseStream() {
		try {
			meter?.stop();
		} catch (e) {
			/* already torn down */
		}
		meter = null;
		try {
			stream?.getTracks().forEach((t) => t.stop());
		} catch (e) {
			/* already stopped */
		}
		stream = null;
	}
	function _clearTimers() {
		if (tick) {
			clearInterval(tick);
			tick = null;
		}
	}
	function _resolveSettle() {
		if (settle) {
			const r = settle;
			settle = null;
			r();
		}
	}
	function _fail(msg) {
		starting.value = false;
		_clearTimers();
		_releaseStream();
		state.value = "error";
		error.value = msg;
		_resolveSettle();
	}

	function _pickMime() {
		return (
			MIME_PREFS.find((m) => {
				try {
					return window.MediaRecorder.isTypeSupported(m);
				} catch (e) {
					return false;
				}
			}) || ""
		);
	}

	// Whole seconds captured so far, clamped to the cap so the value handed to the server can
	// never exceed the limit the auto-stop exists to respect.
	function _elapsed() {
		return Math.min(maxDurationS, Math.max(0, Math.round((Date.now() - startedAt) / 1000)));
	}

	// Hand the take over exactly ONCE, whatever ended it (a stop, the cap, or a recorder error
	// mid-take). `keep` is false only for an explicit cancel — everything else salvages whatever
	// fragments exist, because a recorder that died 4 minutes in must not take those minutes with it.
	function _emitTake(keep) {
		if (emitted) return;
		emitted = true;
		const mimeType = (recorder && recorder.mimeType) || mime || "audio/webm";
		const dur = _elapsed();
		const parts = fragments.slice();
		fragments = [];
		let peakRms;
		try {
			peakRms = meter ? meter.peak() : undefined;
		} catch (e) {
			peakRms = undefined;
		}
		if (!keep || !parts.length) {
			// Abandoned, or nothing was ever captured: there is no recording to keep, and the
			// caller drops the fragments it has already mirrored.
			onCancel();
			return;
		}
		// Concatenating timeslice fragments in arrival order IS the recording — a bare
		// `new Blob(parts)` with the recorder's own mime, no re-encode and no remux.
		const take = { blob: new Blob(parts, { type: mimeType }), durationS: dur, mimeType };
		// Only carry a REAL measurement — an absent key is what tells the gate "unmeasured".
		if (typeof peakRms === "number") take.peakRms = peakRms;
		take.fragments = parts.length;
		onDone(take);
	}

	function _onStop() {
		const localAction = action;
		action = null;
		_clearTimers();
		const keep = localAction !== "cancel";
		_emitTake(keep);
		_releaseStream();
		if (!keep) durationS.value = 0;
		state.value = "idle";
		_resolveSettle();
	}

	async function start() {
		if (!supported) {
			_fail("Voice recording isn't supported in this browser.");
			return;
		}
		// Reject a re-entry synchronously — while already recording OR while a previous start()
		// is mid-getUserMedia (the permission-prompt window).
		if (state.value === "recording" || starting.value) return;
		starting.value = true;
		const gen = ++startGen;
		error.value = "";
		durationS.value = 0;
		fragments = [];
		action = null;
		emitted = false;
		let acquired;
		try {
			acquired = await navigator.mediaDevices.getUserMedia({ audio: true });
		} catch (e) {
			if (gen !== startGen) return; // cancelled/torn down during the prompt — nothing acquired
			const name = (e && e.name) || "";
			_fail(
				name === "NotAllowedError" || name === "SecurityError"
					? "Microphone access is blocked. Allow the microphone for this site and try again."
					: name === "NotFoundError"
					? "No microphone was found on this device."
					: "Couldn't start the microphone."
			);
			return;
		}
		// Navigated away / cancelled while the prompt was open: this grant is STALE. STOP the
		// tracks so the mic never stays hot on a dead component, and bail without a recorder.
		if (gen !== startGen) {
			try {
				acquired.getTracks().forEach((t) => t.stop());
			} catch (e) {
				/* already stopped */
			}
			return;
		}
		stream = acquired;
		mime = _pickMime();
		// The silence meter rides the SAME stream. createRmsMeter NEVER throws — an unavailable or
		// failing WebAudio returns a meter that measures nothing, and an unmeasured take is always
		// transcribed — so recording is never gated on it. It is NOT reset mid-take: the peak now
		// describes the WHOLE recording, and one spoken word anywhere in it clears the gate.
		meter = createRmsMeter(stream);
		try {
			recorder = mime
				? new MediaRecorder(stream, { mimeType: mime })
				: new MediaRecorder(stream);
			recorder.ondataavailable = (e) => {
				if (!e.data || !e.data.size) return;
				const index = fragments.length;
				fragments.push(e.data);
				// Persist THIS fragment now (the caller writes it to the durable mirror). The
				// final flush that stop() triggers comes through here too, so the last seconds of
				// a take are durable before its transcription even starts.
				onFragment({
					index,
					blob: e.data,
					durationS: _elapsed(),
					mimeType: (recorder && recorder.mimeType) || mime || "audio/webm",
				});
			};
			// A recorder that dies mid-take must not take the take with it: salvage the fragments
			// captured so far (they are a valid, truncated webm) and hand them over BEFORE
			// reporting the error, so the user gets a transcript of what was said, not nothing.
			recorder.onerror = () => {
				_emitTake(true);
				_fail("Recording failed. Try again.");
			};
			recorder.onstop = _onStop;
			startedAt = Date.now();
			// TIMESLICE: the whole take is one recorder session; the browser flushes a fragment
			// every `fragmentMs` so crash exposure is bounded.
			recorder.start(fragmentMs);
		} catch (e) {
			_fail("Couldn't start the recorder in this browser.");
			return;
		}
		state.value = "recording";
		starting.value = false;
		tick = setInterval(() => {
			durationS.value = Math.min(maxDurationS, Math.floor((Date.now() - startedAt) / 1000));
			if (durationS.value >= maxDurationS && state.value === "recording" && !action) {
				// The cap is a stop, never a discard: onDone still receives everything captured.
				onAutoStop(durationS.value);
				void stop();
			}
		}, 250);
	}

	// Finalise: the recorder flushes its tail as one last fragment, then onstop assembles the
	// take. Resolves once that has happened, so the caller knows the recording is complete.
	function stop() {
		if (state.value !== "recording" || !recorder) return Promise.resolve();
		if (recorder.state !== "recording") {
			_clearTimers();
			_releaseStream();
			state.value = "idle";
			return Promise.resolve();
		}
		_clearTimers(); // the clock stops at the moment the user pressed Stop, not at onstop
		return new Promise((resolve) => {
			settle = resolve;
			action = "final";
			try {
				recorder.stop();
			} catch (e) {
				_fail("Recording failed. Try again.");
			}
		});
	}

	// Abandon the take entirely — unlike the old chunked recorder there is nothing partial to
	// keep, because a dictation is one all-or-nothing recording. onCancel() tells the caller to
	// drop the fragments it has already mirrored.
	function cancel() {
		// Disown any getUserMedia still awaiting the permission grant so a late resolve stops its
		// tracks instead of starting a recorder on a torn-down component.
		startGen += 1;
		starting.value = false;
		if (state.value !== "recording" || !recorder || recorder.state !== "recording") {
			_clearTimers();
			_emitTake(false);
			_releaseStream();
			durationS.value = 0;
			if (state.value !== "error") state.value = "idle";
			return Promise.resolve();
		}
		_clearTimers();
		return new Promise((resolve) => {
			settle = resolve;
			action = "cancel";
			try {
				recorder.stop();
			} catch (e) {
				_clearTimers();
				_emitTake(false);
				_releaseStream();
				durationS.value = 0;
				state.value = "idle";
				resolve();
			}
		});
	}

	// Synchronous teardown for component unmount: disown any pending getUserMedia and release the
	// mic immediately (no awaiting), so navigating away never leaves a track hot — even
	// mid-permission-prompt. Fragments already mirrored stay durable, so the take is offered back
	// by reload recovery rather than silently dropped.
	function dispose() {
		startGen += 1;
		starting.value = false;
		action = null;
		_clearTimers();
		_releaseStream();
		recorder = null;
		if (state.value !== "error") state.value = "idle";
		_resolveSettle();
	}

	return reactive({
		state,
		error,
		durationS,
		starting,
		supported,
		maxDurationS,
		start,
		stop,
		cancel,
		dispose,
	});
}
