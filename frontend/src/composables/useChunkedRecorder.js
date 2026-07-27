// useChunkedRecorder — the header-trap-safe long-dictation recorder. It keeps ONE
// MediaStream alive for the whole session and, every ~15 s, STOPS the current
// MediaRecorder (which flushes a COMPLETE, self-contained WebM/opus file with its
// own header) and IMMEDIATELY starts a fresh MediaRecorder on the same stream.
//
// Why stop/restart and NOT `recorder.start(15000)` timeslice: with a timeslice the
// FIRST ondataavailable blob carries the WebM/EBML initialisation segment and every
// later blob is a bare cluster continuation that is NOT independently decodable — you
// cannot POST clip #2 alone to a stateless endpoint (the investigation's header-trap
// verdict). Stop/restart yields clips that each decode and transcribe STANDALONE.
// Cost: a tens-of-ms inter-cycle gap where a boundary word can clip — accepted per
// the owner spec (chunking is what makes long sessions safe).
//
// Each finished clip is handed to opts.onClip({ blob, durationS, mimeType, peakRms }); the
// composer feeds it straight into voiceChunkQueue, which STAMPS the monotonic `seq`
// (single seq authority — the recorder deliberately does NOT number clips, so it can
// never collide with recovery on a seq). This composable owns ONLY the browser
// recorder plumbing (unit-tested logic lives in voiceChunkQueue.js / voiceSilenceGate.js;
// the recorder itself is validated by the real-mic QA script).
//
// `peakRms` is the loudest RMS an AnalyserNode saw on the SAME MediaStream during THIS
// clip's cycle — the composer's near-silence gate (voiceSilenceGate.js) reads it to drop a
// pause-only clip before it can be transcribed into a whisper hallucination. It is OMITTED
// whenever the meter couldn't measure (no WebAudio, a suspended context), and the gate
// treats a missing measurement as "transcribe" — the meter never costs audio.
//
// There is deliberately NO 300 s hard cap here (unlike useAudioRecorder) — chunking
// makes long sessions safe, so the mic stays live until the user stops.
import { reactive, ref } from "vue";
import { createRmsMeter } from "@/utils/voiceSilenceGate";

const MIME_PREFS = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
const DEFAULT_CHUNK_MS = 15000;

export function useChunkedRecorder(opts = {}) {
	const chunkMs = Math.max(2000, opts.chunkMs || DEFAULT_CHUNK_MS);
	const onClip = typeof opts.onClip === "function" ? opts.onClip : () => {};

	const state = ref("idle"); // 'idle' | 'recording' | 'error'
	const error = ref("");
	const durationS = ref(0); // total elapsed seconds across all cycles (for the clock)
	// Exposed so the route/leave guard can see the permission-prompt window too: `state`
	// stays 'idle' until getUserMedia is granted, so navigation DURING the prompt would slip
	// past a guard that only watches `state === 'recording'` (finding 2).
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
	let meter = null; // peak-RMS meter on the same stream; null until start(), never required
	let chunks = [];
	let mime = "";
	// Cancellation generation covering the pending getUserMedia (finding 2). start() captures
	// it before awaiting the permission prompt; cancel()/dispose() bump it. A grant that
	// resolves AFTER a bump is recognised as STALE — its tracks are stopped instead of being
	// wired into a recorder + timers on a torn-down / navigated-away component (a hot-mic
	// leak). `starting` (the ref above) is the synchronous VAR-3 re-entry guard too: set the
	// instant start() is entered, BEFORE the async getUserMedia, so a double-click — or an
	// impatient second click while the prompt is open — cannot launch a second recorder.
	let startGen = 0;
	let cycleTimer = null; // fires _rotate() at each ~15 s boundary
	let tick = null; // 250 ms clock updater
	let startedAt = 0; // session start (for durationS)
	let cycleStartedAt = 0; // current clip start (for its durationS)
	let action = null; // what the in-flight recorder.stop() means: 'rotate'|'final'|'cancel'
	let settle = null; // resolver for a stop()/cancel() awaiting the final onstop

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
		if (cycleTimer) {
			clearTimeout(cycleTimer);
			cycleTimer = null;
		}
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

	function _armCycle() {
		if (cycleTimer) clearTimeout(cycleTimer);
		cycleTimer = setTimeout(_rotate, chunkMs);
	}

	// Boundary reached: stop the current recorder; its onstop emits the finished clip
	// and (because action==='rotate') starts the next cycle.
	function _rotate() {
		if (state.value !== "recording" || !recorder || recorder.state !== "recording") return;
		action = "rotate";
		try {
			recorder.stop();
		} catch (e) {
			_fail("Recording failed. Try again.");
		}
	}

	function _newRecorder() {
		chunks = [];
		recorder = mime
			? new MediaRecorder(stream, { mimeType: mime })
			: new MediaRecorder(stream);
		recorder.ondataavailable = (e) => {
			if (e.data && e.data.size) chunks.push(e.data);
		};
		recorder.onerror = () => _fail("Recording failed. Try again.");
		recorder.onstop = _onStop;
		// Peak RMS is PER CLIP: zero it as the new cycle's recorder starts, so a loud
		// sentence in clip N never keeps clip N+1's silence from being gated.
		try {
			meter?.reset();
		} catch (e) {
			/* a meter that misbehaves just stops measuring; it must not break recording */
		}
		cycleStartedAt = Date.now();
		recorder.start(); // NO timeslice — the whole cycle is one self-contained file
	}

	function _onStop() {
		const localAction = action;
		action = null;
		const mimeType = (recorder && recorder.mimeType) || mime || "audio/webm";
		const dur = Math.max(0, Math.round((Date.now() - cycleStartedAt) / 1000));
		const built = chunks.length ? new Blob(chunks, { type: mimeType }) : null;
		chunks = [];
		// Read the cycle's peak BEFORE the next _newRecorder() resets it. `undefined` means the
		// meter couldn't measure this clip — the gate then transcribes it (never-drop-audio).
		let peakRms;
		try {
			peakRms = meter ? meter.peak() : undefined;
		} catch (e) {
			peakRms = undefined;
		}

		if (localAction === "cancel") {
			// Discard ONLY this in-progress snippet; clips already emitted this session
			// have been enqueued and are never dropped (the never-lost invariant).
			_clearTimers();
			_releaseStream();
			durationS.value = 0;
			state.value = "idle";
			_resolveSettle();
			return;
		}

		if (built && built.size) {
			const clip = { blob: built, durationS: dur, mimeType };
			// Only carry a REAL measurement — an absent key is what tells the gate "unmeasured".
			if (typeof peakRms === "number") clip.peakRms = peakRms;
			onClip(clip);
		}

		if (localAction === "rotate" && state.value === "recording") {
			try {
				_newRecorder();
				_armCycle();
			} catch (e) {
				_fail("Couldn't restart the recorder.");
			}
			return;
		}

		// 'final' (user pressed Stop): the last clip is emitted above; tear down.
		_clearTimers();
		_releaseStream();
		state.value = "idle";
		_resolveSettle();
	}

	async function start() {
		if (!supported) {
			_fail("Voice recording isn't supported in this browser.");
			return;
		}
		// VAR-3: reject a re-entry synchronously — while already recording OR while a
		// previous start() is mid-getUserMedia (the permission-prompt window).
		if (state.value === "recording" || starting.value) return;
		starting.value = true;
		// This attempt's cancellation token; cancel()/dispose() bump startGen to disown a
		// getUserMedia that resolves after teardown/navigation (finding 2).
		const gen = ++startGen;
		error.value = "";
		durationS.value = 0;
		chunks = [];
		action = null;
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
		// The near-silence meter rides the SAME stream. createRmsMeter NEVER throws — an
		// unavailable/failing WebAudio returns a meter that measures nothing, and an unmeasured
		// clip is always transcribed — so recording is never gated on it.
		meter = createRmsMeter(stream);
		try {
			_newRecorder();
		} catch (e) {
			_fail("Couldn't start the recorder in this browser.");
			return;
		}
		startedAt = Date.now();
		state.value = "recording";
		starting.value = false;
		_armCycle();
		tick = setInterval(() => {
			durationS.value = Math.floor((Date.now() - startedAt) / 1000);
		}, 250);
	}

	// Finalise: emit the current (partial) clip too, then release the mic. Resolves
	// once the final onstop has fired so the caller knows the last clip is enqueued.
	function stop() {
		if (state.value !== "recording" || !recorder) return Promise.resolve();
		if (recorder.state !== "recording") {
			_clearTimers();
			_releaseStream();
			state.value = "idle";
			return Promise.resolve();
		}
		_clearTimers();
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

	// Stop recording and DROP the current unsent snippet. Clips already emitted this
	// session stay in the queue (their audio is captured) — Cancel is not "undo".
	function cancel() {
		// Disown any getUserMedia still awaiting the permission grant so a late resolve stops
		// its tracks instead of starting a recorder on a torn-down component (finding 2).
		startGen += 1;
		starting.value = false;
		if (state.value !== "recording" || !recorder || recorder.state !== "recording") {
			_clearTimers();
			_releaseStream();
			chunks = [];
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
				_releaseStream();
				durationS.value = 0;
				state.value = "idle";
				resolve();
			}
		});
	}

	// Synchronous teardown for component unmount: disown any pending getUserMedia and release
	// the mic immediately (no awaiting), so navigating away never leaves a track hot — even
	// mid-permission-prompt (finding 2). The queue's own dispose() handles late transcripts.
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
		start,
		stop,
		cancel,
		dispose,
	});
}
