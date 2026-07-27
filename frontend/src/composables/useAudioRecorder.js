// useAudioRecorder - shared MediaRecorder wrapper for the composer mic, the
// wiki-nudge card and the Business-tab recorder. One instance per surface.
//
// Returned as reactive() (house style, like useDocmeta) so consumers read
// plain properties: `rec.state`, `rec.durationS`.
//
//   state:     'idle' | 'recording' | 'stopped' | 'error'
//   error:     friendly message when state === 'error' (mic denied, no mic, …)
//   durationS: elapsed whole seconds while recording
//   start():   Promise<void> - requests the mic; on denial → state 'error'
//   stop():    Promise<{blob, mimeType, durationS} | null>
//   cancel():  discard the take (no blob), release the mic
//   supported: false on browsers without getUserMedia/MediaRecorder
//
// Recordings hard-stop at 300 s (the server rejects longer clips). When the
// cap fires without a user stop() in flight, the result is stashed - a later
// stop() resolves with it - and opts.onAutoStop(result) is invoked so the UI
// can transcribe immediately and tell the user why recording ended.
import { reactive, ref } from "vue";

const MIME_PREFS = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
const MAX_SECONDS = 300;
// Speech, not music — the same reasoning as useDictationRecorder's constant. A browser's default
// (~129 kbps measured on Chrome) makes a 300 s note ~4.6 MB to upload and to cap-check, with no
// benefit at all for a transcription model.
const AUDIO_BITS_PER_SECOND = 32000;

export function useAudioRecorder(opts = {}) {
	const state = ref("idle");
	const error = ref("");
	const durationS = ref(0);
	const supported = !!(
		typeof navigator !== "undefined" &&
		navigator.mediaDevices &&
		navigator.mediaDevices.getUserMedia &&
		typeof window !== "undefined" &&
		window.MediaRecorder
	);

	let recorder = null;
	let stream = null;
	let chunks = [];
	let tick = null;
	let startedAt = 0;
	let settle = null; // resolver of the in-flight stop() promise
	let cancelled = false;
	let autoStopped = false;
	let pendingResult = null; // auto-stop result awaiting a stop() call

	function _releaseStream() {
		try {
			stream?.getTracks().forEach((t) => t.stop());
		} catch (e) {
			/* already stopped */
		}
		stream = null;
	}
	function _clearTick() {
		if (tick) {
			clearInterval(tick);
			tick = null;
		}
	}
	function _fail(msg) {
		_clearTick();
		_releaseStream();
		state.value = "error";
		error.value = msg;
		if (settle) {
			const r = settle;
			settle = null;
			r(null);
		}
	}

	async function start() {
		if (!supported) {
			_fail("Voice recording isn't supported in this browser.");
			return;
		}
		if (state.value === "recording") return;
		error.value = "";
		durationS.value = 0;
		chunks = [];
		cancelled = false;
		autoStopped = false;
		pendingResult = null;
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
		const mime = MIME_PREFS.find((m) => {
			try {
				return window.MediaRecorder.isTypeSupported(m);
			} catch (e) {
				return false;
			}
		});
		try {
			recorder = mime
				? new MediaRecorder(stream, {
						mimeType: mime,
						audioBitsPerSecond: AUDIO_BITS_PER_SECOND,
				  })
				: new MediaRecorder(stream, { audioBitsPerSecond: AUDIO_BITS_PER_SECOND });
		} catch (e) {
			_fail("Couldn't start the recorder in this browser.");
			return;
		}
		recorder.ondataavailable = (e) => {
			if (e.data && e.data.size) chunks.push(e.data);
		};
		recorder.onerror = () => _fail("Recording failed. Try again.");
		recorder.onstop = () => {
			_clearTick();
			_releaseStream();
			if (cancelled) {
				chunks = [];
				durationS.value = 0;
				state.value = "idle";
				if (settle) {
					const r = settle;
					settle = null;
					r(null);
				}
				return;
			}
			const mimeType = (recorder && recorder.mimeType) || mime || "audio/webm";
			const result = {
				blob: new Blob(chunks, { type: mimeType }),
				mimeType,
				durationS: durationS.value,
			};
			chunks = [];
			state.value = "stopped";
			if (settle) {
				const r = settle;
				settle = null;
				r(result);
			} else if (autoStopped && typeof opts.onAutoStop === "function") {
				// hit the 300 s cap with no stop() waiting - the callback owns
				// the take (stashing it too would risk a double transcribe).
				opts.onAutoStop(result);
			} else {
				// capped with no callback: stash so a later stop() resolves with it.
				pendingResult = result;
			}
		};
		startedAt = Date.now();
		try {
			recorder.start();
		} catch (e) {
			_fail("Couldn't start the recorder in this browser.");
			return;
		}
		state.value = "recording";
		tick = setInterval(() => {
			durationS.value = Math.floor((Date.now() - startedAt) / 1000);
			if (durationS.value >= MAX_SECONDS && state.value === "recording") {
				autoStopped = true;
				try {
					recorder.stop();
				} catch (e) {
					_fail("Recording failed. Try again.");
				}
			}
		}, 250);
	}

	function stop() {
		if (pendingResult) {
			const r = pendingResult;
			pendingResult = null;
			return Promise.resolve(r);
		}
		if (!recorder || state.value !== "recording") return Promise.resolve(null);
		return new Promise((resolve) => {
			settle = resolve;
			try {
				recorder.stop();
			} catch (e) {
				settle = null;
				_fail("Recording failed. Try again.");
				resolve(null);
			}
		});
	}

	function cancel() {
		pendingResult = null;
		if (recorder && state.value === "recording") {
			cancelled = true;
			try {
				recorder.stop();
			} catch (e) {
				_clearTick();
				_releaseStream();
				durationS.value = 0;
				state.value = "idle";
			}
		} else {
			_clearTick();
			_releaseStream();
			chunks = [];
			durationS.value = 0;
			if (state.value !== "error") state.value = "idle";
		}
	}

	return reactive({ state, error, durationS, start, stop, cancel, supported });
}
