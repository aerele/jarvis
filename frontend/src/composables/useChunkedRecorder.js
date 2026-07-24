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
// Each finished clip is handed to opts.onClip({ blob, durationS, mimeType }); the
// composer feeds it straight into voiceChunkQueue, which STAMPS the monotonic `seq`
// (single seq authority — the recorder deliberately does NOT number clips, so it can
// never collide with recovery on a seq). This composable owns ONLY the browser
// recorder plumbing (unit-tested logic lives in voiceChunkQueue.js; the recorder
// itself is validated by the real-mic QA script).
//
// There is deliberately NO 300 s hard cap here (unlike useAudioRecorder) — chunking
// makes long sessions safe, so the mic stays live until the user stops.
import { reactive, ref } from "vue";

const MIME_PREFS = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
const DEFAULT_CHUNK_MS = 15000;

export function useChunkedRecorder(opts = {}) {
	const chunkMs = Math.max(2000, opts.chunkMs || DEFAULT_CHUNK_MS);
	const onClip = typeof opts.onClip === "function" ? opts.onClip : () => {};

	const state = ref("idle"); // 'idle' | 'recording' | 'error'
	const error = ref("");
	const durationS = ref(0); // total elapsed seconds across all cycles (for the clock)
	const supported = !!(
		typeof navigator !== "undefined" &&
		navigator.mediaDevices &&
		navigator.mediaDevices.getUserMedia &&
		typeof window !== "undefined" &&
		window.MediaRecorder
	);

	let stream = null;
	let recorder = null;
	let chunks = [];
	let mime = "";
	// Synchronous re-entry guard (VAR-3): set the instant start() is entered, BEFORE the
	// async getUserMedia, so a double-click — or an impatient second click while the mic
	// permission prompt is open — cannot launch a second recorder + interval (a leaked hot
	// mic + duplicate clips). The reactive `state` flips to 'recording' only after the
	// grant, too late to guard the window on its own.
	let starting = false;
	let cycleTimer = null; // fires _rotate() at each ~15 s boundary
	let tick = null; // 250 ms clock updater
	let startedAt = 0; // session start (for durationS)
	let cycleStartedAt = 0; // current clip start (for its durationS)
	let action = null; // what the in-flight recorder.stop() means: 'rotate'|'final'|'cancel'
	let settle = null; // resolver for a stop()/cancel() awaiting the final onstop

	function _releaseStream() {
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
		starting = false;
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
			onClip({ blob: built, durationS: dur, mimeType });
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
		if (state.value === "recording" || starting) return;
		starting = true;
		error.value = "";
		durationS.value = 0;
		chunks = [];
		action = null;
		try {
			stream = await navigator.mediaDevices.getUserMedia({ audio: true });
		} catch (e) {
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
		mime = _pickMime();
		try {
			_newRecorder();
		} catch (e) {
			_fail("Couldn't start the recorder in this browser.");
			return;
		}
		startedAt = Date.now();
		state.value = "recording";
		starting = false;
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

	return reactive({ state, error, durationS, supported, start, stop, cancel });
}
