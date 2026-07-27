// voiceDictationStore — the per-RECORDING lifecycle + retention state machine behind voice
// dictation, extracted as a PLAIN, importable, unit-tested module (the composer imports it;
// useDictationRecorder feeds it) exactly like the Relay-Pump eventFence precedent. It owns NO
// browser APIs: MediaRecorder, `fetch` and IndexedDB are all injected, so its whole contract is
// testable with `node --test` (see voiceDictationStore.test.js, run in the python suite forever
// via jarvis/tests/test_voice_dictation_store_client.py).
//
// WHY ONE UNIT PER RECORDING. The previous design cut a long take into independent ~15 s clips
// and transcribed each one on its own. That destroyed the context a speech model needs: each
// request saw a fragment with no beginning and no end, so the model filled the void — inventing
// words at clip boundaries and, on a pause-only clip, confident little phrases nobody said. The
// fix is not a better filter, it is not asking the question: ONE recording, ONE transcription
// call, full context. Measured on the #463 endpoint, whisper-turbo transcribes 300 s in 1.2 s
// and 591 s in 2.2 s, so a single call per take is comfortably inside every budget.
//
// The invariant that outranks everything: THE AUDIO IS NEVER LOST. Either it transcribes, or it
// stays locally available to retry or download — all-or-nothing per recording, never a partial
// take with silent holes in it. Concretely:
//
//   * FRAGMENTS ARE MIRRORED AS THEY ARRIVE. The recorder runs in timeslice mode, so a ~15 s
//     fragment lands during the take and is written to the durable mirror immediately: a crash
//     costs at most the last fragment, never the recording. Those fragments are continuations
//     of ONE stream — concatenating them in index order IS the recording's bytes, with no
//     re-encode and no remux, and a prefix of them is still decodable.
//   * WRITE DURABILITY IS OBSERVED, not assumed. A mirror signals a failed write by rejecting
//     or by resolving `false`; after retries the recording is marked persist:"failed" and
//     onPersistFail fires so the composer can stop recording rather than keep capturing audio
//     it cannot crash-protect.
//   * RELEASE ONLY ON AN ACCEPTED SEND. Under `retainUntilSent` a transcribed recording's audio
//     is kept until its words are part of a payload the server accepted (captureSentInPayload →
//     acknowledge), because a draft is volatile until it is sent.
//   * A FAILED RECORDING IS RETAINED, always — that is what its Retry / Download chip acts on,
//     and what reload recovery re-offers.
//   * A MEASURED-SILENT TAKE IS NOT DELETED EITHER (finishSilent). It is not uploaded — silence
//     is what makes the model fabricate — but the measurement can be wrong, and no measurement is
//     allowed to cost a recording. It takes the same terminal `failed` route, audio retained.
//   * NOTHING HERE EVER DELETES AUDIO EXCEPT discard() (the user chose to) and acknowledge() (its
//     words are durable in a sent message).
//
// Recording state machine (per id):
//
//        begin()                    finish()                        transcribe resolves
//     ───────────► recording ──────────────────────► transcribing ─────────────────────► done
//                      │  │                               │  ▲                            │
//         cancel/abort │  │ finishSilent()      rejects / │  │ retry(id) / auto-retry     │ onCommit
//                      │  │ (measured silent)   cancelTranscription()                     │
//                      ▼  └──────────────────────────►    ▼  │                            ▼
//                  discarded  ◄──── discard(id) ───────  failed  (audio RETAINED)     (text out)
//
// Text is emitted in RECORDING order through a `nextToFlush` cursor: a second take started while
// the first is still transcribing can resolve first, and its words must not jump ahead of the
// earlier take's. A failed recording does not halt the cursor (it advances past it), and a later
// successful retry commits with `resurrected` set so the composer can say where the text landed
// instead of dropping unexplained words into the draft.
//
// SECURITY NOTE: transcription text flows OUT of this module (onCommit → composer) only. It is
// NEVER fed back into any transcribe call or prompt — there is no rolling-context path here by
// design (Fable rejected that variant on prompt-injection grounds).

const noop = () => {};

// The one outcome that means "we reached the audio and there was nothing in it": either the
// client's own meter measured the WHOLE take as inaudible (finishSilent), or the provider came
// back with an empty transcript. Both are epistemically identical — genuine silence, OR a miss of
// real speech — so both land in the SAME terminal `failed` state with the audio RETAINED behind an
// actionable chip. Neither ever deletes: a positive-but-wrong measurement would otherwise destroy
// up to five minutes of speech, and deletion is only ever the user's explicit choice.
const NO_SPEECH = "no speech detected";

// Invoke a caller callback WITHOUT letting a throw unwind the drain chain — a bug in onCommit
// (reactive writes, nextTick) must not wedge the cursor and freeze every later commit.
function _safe(fn, ...args) {
	try {
		fn(...args);
	} catch (e) {
		/* a caller callback threw; swallow so the state machine keeps draining */
	}
}

// Boundary-aware first-occurrence index of `needle` in the already-normalized `hay`: the match
// must be flanked by WORD BOUNDARIES (string start/end or a single space) so an earlier take's
// "send it" can never match INSIDE a later take's "resend it" and acknowledge — then DELETE — the
// wrong recording's audio. `hay` is whitespace-collapsed + trimmed, so a boundary is exactly a
// space or an edge. Returns -1 when no boundary-aligned occurrence exists → the caller RETAINS
// (never-lose bias: an ambiguous / mid-word match is treated as "not present", never over-released).
function _boundaryIndexOf(hay, needle) {
	if (!needle) return -1;
	const isBoundary = (ch) => ch === undefined || ch === " ";
	let from = 0;
	while (from <= hay.length) {
		const at = hay.indexOf(needle, from);
		if (at === -1) return -1;
		if (isBoundary(hay[at - 1]) && isBoundary(hay[at + needle.length])) return at;
		from = at + 1; // this occurrence sits mid-word — keep scanning for a bounded one
	}
	return -1;
}

// EVERY boundary-aligned occurrence of `needle` in `hay`, as half-open [start, end) ranges. Used
// by the retain-on-ambiguity scan to detect when two recordings' contributions OVERLAP in the same
// payload span (e.g. "send it" + "now" both sitting inside "send it now").
function _boundaryRanges(hay, needle) {
	const out = [];
	if (!needle) return out;
	const isBoundary = (ch) => ch === undefined || ch === " ";
	let from = 0;
	while (from <= hay.length) {
		const at = hay.indexOf(needle, from);
		if (at === -1) break;
		if (isBoundary(hay[at - 1]) && isBoundary(hay[at + needle.length])) {
			out.push([at, at + needle.length]);
		}
		from = at + 1;
	}
	return out;
}

// Do any of `a`'s ranges overlap any of `b`'s ranges (half-open interval overlap)?
function _rangesOverlap(a, b) {
	for (const [s1, e1] of a) {
		for (const [s2, e2] of b) {
			if (s1 < e2 && s2 < e1) return true;
		}
	}
	return false;
}

// deps:
//   transcribe(recording, signal) -> Promise<string>   REQUIRED. Rejects on timeout/error; the
//                              per-call abort budget lives INSIDE it. `signal` (may be undefined)
//                              aborts on dispose() so an in-flight upload cancels.
//   mirror {
//     recordingKey(id) -> string          namespaces this session's recordings
//     putFragment(frag) -> Promise<bool>  durability is OBSERVED: a failed write rejects OR
//                                         resolves `false`; anything else is a confirmed write
//                                         (so an in-memory test mirror needs no change).
//     adopt(recording) -> Promise<bool>   atomic recovery adoption (put new + delete old keys)
//     deleteRecording(id) -> Promise?     best-effort (a stale record is safe; a dropped one is not)
//     reassignConversation(id, scope) -> Promise?   best-effort routing update
//   }                                     OPTIONAL crash-safety store (IndexedDB in prod).
//   maxAttempts       total transcription tries per recording before `failed` (default 2)
//   retryBackoffMs    pause before an AUTO-retry re-dispatches (default 0). An immediate retry
//                     against a 429 / 5xx is two back-to-back multi-megabyte uploads, and the
//                     server has already retried inside the first one. Default 0 keeps the
//                     primitive synchronous for tests; the composer injects a real pause.
//   concurrency       max transcriptions in flight at once (default 2)
//   retainUntilSent   keep a transcribed recording's audio until acknowledge()/discard(), because
//                     the composer's draft is volatile until the message is actually sent
//   onCommit(id, text, rec, resurrected)  the recording's transcript, in recording order.
//                     `resurrected` is true when the cursor had already passed this recording (a
//                     Retry that landed after later text), so the composer can say where it went.
//   onFail(id)        the recording reached the terminal `failed` state
//   onPersistFail(id) this recording's audio could NOT be confirmed durably mirrored
//   onChange()        after every state transition (drive reactive UI)
export function createVoiceDictationStore(deps = {}) {
	const transcribe = deps.transcribe;
	if (typeof transcribe !== "function") {
		throw new Error("createVoiceDictationStore: `transcribe` dependency is required");
	}
	const mirror = deps.mirror || null;
	const maxAttempts = Math.max(1, deps.maxAttempts || 2);
	const retryBackoffMs = Math.max(0, deps.retryBackoffMs || 0);
	const concurrency = Math.max(1, deps.concurrency || 2);
	const retainUntilSent = !!deps.retainUntilSent;
	const onCommit = deps.onCommit || noop;
	const onFail = deps.onFail || noop;
	const onPersistFail = deps.onPersistFail || noop;
	const onChange = deps.onChange || noop;
	// Total mirror write attempts for one fragment before its durability is declared
	// UN-confirmable. >1 so a transient IndexedDB hiccup is retried before we give up.
	const MIRROR_PUT_ATTEMPTS = Math.max(1, deps.mirrorPutAttempts || 3);

	// id -> record. `id` is THE single authority (live takes AND recovery both draw from it), so a
	// recovered recording can never collide with a live one.
	const records = new Map();
	let _nextId = 0;
	let nextToFlush = null; // commit cursor; initialised to the first admitted id
	let running = 0; // transcriptions in flight
	let disposed = false;
	// Aborts every in-flight upload on dispose() so navigate-away doesn't leave a fetch running
	// to its full client budget.
	const abort = typeof AbortController !== "undefined" ? new AbortController() : null;

	function _admit(init) {
		const id = _nextId++;
		const rec = {
			id,
			recordingId: mirror ? mirror.recordingKey(id) : `local#${id}`,
			conversationId: init.conversationId != null ? init.conversationId : null,
			mimeType: init.mimeType || "audio/webm",
			state: init.state || "recording",
			fragments: [],
			blob: init.blob || null,
			durationS: init.durationS || 0,
			peakRms: undefined,
			attempts: 0,
			text: "",
			recovered: !!init.recovered,
			// Dispatch generation. Bumped on every transcription dispatch AND by every transition
			// that frees the record's `running` slot early (cancelTranscription), so a late
			// resolution of a call nobody is waiting for any more is recognised as STALE and
			// cannot decrement `running` a second time or resurrect a terminal recording.
			gen: 0,
			// Earliest wall-clock time an auto-retry may re-dispatch (see `retryBackoffMs`).
			retryAt: 0,
			// When this recording last entered `transcribing` — the only honest elapsed number the
			// pill can show while waiting.
			transcribingSince: 0,
			// Durable-write bookkeeping (only meaningful when a mirror is injected). A recording
			// is crash-safe ONLY once every fragment write has been CONFIRMED.
			persist: mirror ? "persisting" : undefined,
			_writesInFlight: 0,
			_unwritten: [],
		};
		records.set(id, rec);
		if (nextToFlush === null) nextToFlush = id;
		return rec;
	}

	// ---- durable mirror (observed) ----

	function _recomputePersist(rec) {
		if (!mirror) return;
		const was = rec.persist;
		if (rec._unwritten.length) rec.persist = "failed";
		else if (rec._writesInFlight > 0) rec.persist = "persisting";
		else rec.persist = "persisted";
		if (rec.persist === "failed" && was !== "failed") {
			_safe(onPersistFail, rec.id);
			_safe(onChange);
		} else if (was === "failed" && rec.persist !== "failed") {
			_safe(onChange); // a Retry that finally landed clears the chip
		}
	}

	// OBSERVABLE durable write for one fragment. A swallowed failure would let the composer keep
	// recording as if the audio were protected while a crash or reload lost it permanently, so the
	// write is retried and, if durability still can't be established, the RECORDING is marked
	// un-persisted and onPersistFail fires.
	function _persistFragment(rec, frag) {
		if (!mirror) return;
		rec._writesInFlight += 1;
		let attempt = 0;
		const done = (ok) => {
			if (disposed) return;
			if (records.get(rec.id) !== rec || rec.state === "discarded") return; // gone/tombstoned
			rec._writesInFlight -= 1;
			if (ok) frag.persisted = true;
			else if (!rec._unwritten.includes(frag)) rec._unwritten.push(frag);
			_recomputePersist(rec);
		};
		const attemptPut = () => {
			attempt += 1;
			let p;
			try {
				p = Promise.resolve(
					mirror.putFragment({
						recordingId: rec.recordingId,
						index: frag.index,
						blob: frag.blob,
						durationS: frag.durationS,
						mimeType: rec.mimeType,
						conversationId: rec.conversationId,
					})
				);
			} catch (e) {
				p = Promise.reject(e); // a SYNC throw is a failed write
			}
			p.then(
				(res) => (res === false ? _retryOrGiveUp() : done(true)),
				() => _retryOrGiveUp()
			);
		};
		const _retryOrGiveUp = () => {
			if (disposed) return;
			if (records.get(rec.id) !== rec || rec.state === "discarded") return;
			if (attempt < MIRROR_PUT_ATTEMPTS) {
				attemptPut(); // try hard to make the audio durable before we give up
				return;
			}
			done(false);
		};
		attemptPut();
	}

	// The recovery equivalent: ONE atomic write that lands the adopted copy and deletes the
	// prior-session fragments it supersedes.
	function _persistAdoption(rec, adoptKeys) {
		if (!mirror) return;
		const frag = { index: 0, blob: rec.blob, durationS: rec.durationS };
		rec.fragments = [frag];
		rec._writesInFlight += 1;
		let attempt = 0;
		const done = (ok) => {
			if (disposed) return;
			if (records.get(rec.id) !== rec || rec.state === "discarded") return;
			rec._writesInFlight -= 1;
			if (ok) frag.persisted = true;
			else if (!rec._unwritten.includes(frag)) rec._unwritten.push(frag);
			_recomputePersist(rec);
		};
		const attemptPut = () => {
			attempt += 1;
			let p;
			try {
				p = Promise.resolve(
					mirror.adopt({
						recordingId: rec.recordingId,
						blob: rec.blob,
						durationS: rec.durationS,
						mimeType: rec.mimeType,
						conversationId: rec.conversationId,
						_adoptKeys: adoptKeys || [],
					})
				);
			} catch (e) {
				p = Promise.reject(e);
			}
			p.then(
				(res) => (res === false ? _retryOrGiveUp() : done(true)),
				() => _retryOrGiveUp()
			);
		};
		const _retryOrGiveUp = () => {
			if (disposed) return;
			if (records.get(rec.id) !== rec || rec.state === "discarded") return;
			if (attempt < MIRROR_PUT_ATTEMPTS) {
				attemptPut();
				return;
			}
			done(false);
		};
		attemptPut();
	}

	function _mirrorDrop(rec) {
		if (!mirror) return;
		try {
			Promise.resolve(mirror.deleteRecording(rec.recordingId)).catch(noop);
		} catch (e) {
			/* best-effort */
		}
	}

	// ---- transcription ----

	// One AbortController per DISPATCH, chained to the store-wide one. Cancelling a single stuck
	// transcription must abort THAT upload (a multi-megabyte POST the user has given up on)
	// without touching any other recording's, while dispose() still aborts all of them.
	function _dispatchSignal(rec) {
		if (typeof AbortController === "undefined") return abort ? abort.signal : undefined;
		const ac = new AbortController();
		rec._abort = ac;
		if (abort) {
			if (abort.signal.aborted) ac.abort();
			else abort.signal.addEventListener("abort", () => ac.abort(), { once: true });
		}
		return ac.signal;
	}

	let _retryTimer = null;
	let _retryTimerAt = 0;
	// Re-pump when the earliest backed-off retry comes due. One timer for the soonest deadline;
	// a sooner one supersedes it.
	function _schedulePump(at) {
		if (disposed) return;
		if (_retryTimer && _retryTimerAt <= at) return;
		if (_retryTimer) clearTimeout(_retryTimer);
		_retryTimerAt = at;
		_retryTimer = setTimeout(() => {
			_retryTimer = null;
			_retryTimerAt = 0;
			if (disposed) return;
			_pump();
			_safe(onChange);
		}, Math.max(0, at - Date.now()));
	}

	function _pump() {
		if (disposed) return;
		let soonest = Infinity;
		while (running < concurrency) {
			let pick = null;
			const now = Date.now();
			for (const rec of records.values()) {
				if (rec.state !== "transcribing" || rec.inflight) continue;
				if (rec.retryAt > now) {
					if (rec.retryAt < soonest) soonest = rec.retryAt;
					continue; // still backing off — it is not dispatchable yet
				}
				if (pick === null || rec.id < pick.id) pick = rec;
			}
			if (!pick) break;
			pick.retryAt = 0;
			pick.inflight = true;
			pick.attempts += 1;
			pick.gen += 1;
			running += 1;
			const id = pick.id;
			const gen = pick.gen;
			const signal = _dispatchSignal(pick);
			// Promise.resolve() tolerates a sync-throwing or non-promise transcribe.
			Promise.resolve()
				.then(() => transcribe(pick, signal))
				.then(
					(text) => _onResolve(id, text, gen),
					(err) => _onReject(id, err, gen)
				);
		}
		if (soonest !== Infinity) _schedulePump(soonest);
	}

	// A resolution/rejection nobody is waiting for any more: the recording was discarded, or its
	// transcription was cancelled and the slot already freed. Ignoring it is what keeps `running`
	// balanced — the transition that abandoned the call did the decrement.
	function _isStale(rec, gen) {
		if (!rec) return false;
		if (rec.state === "discarded") return true;
		return gen !== undefined && rec.gen !== gen;
	}

	function _onResolve(id, text, gen) {
		if (disposed) return;
		const rec = records.get(id);
		// A recording the user discarded mid-flight is a terminal tombstone — a late resolution
		// must not resurrect it (its slot was already freed by discard()).
		if (_isStale(rec, gen)) return;
		// CONTRACT BELT (defense in depth): the `transcribe` adapter MUST resolve a STRING. A
		// non-string (e.g. the raw {ok,text,...} response object) must NEVER be String()-coerced
		// and committed as "[object Object]" behind deleted audio — route it through the failure
		// path so the recording is retried, then RETAINED (never-lose-audio).
		if (typeof text !== "string") {
			_onReject(id, new Error("transcribe resolved a non-string value"), gen);
			return; // _onReject owns the running decrement + retry/fail transition
		}
		running -= 1;
		if (!rec) return; // dropped (shouldn't happen); keep the count honest
		rec.inflight = false;
		rec._abort = null;
		rec.text = text;
		// A trimmed-empty successful transcription is UNCERTAIN — genuine silence OR a miss of
		// real speech. Committing it would retain the audio behind an INVISIBLE done record (no
		// chip, and an empty composer can't be sent to release it) → a leave guard armed forever
		// with no way to clear it. Treat it like a terminal failure instead: an ACTIONABLE
		// recording (the Retry/Download/Discard chip), never auto-released (that could silently
		// drop mis-heard real speech) and never looped (re-sending silence just returns empty
		// again). Discard is the user's explicit "it was silence".
		if (rec.text.trim() === "") {
			rec.state = "failed";
			rec.error = NO_SPEECH;
			if (nextToFlush !== null && id >= nextToFlush) _drain();
			_safe(onFail, id);
			_pump();
			_safe(onChange);
			return;
		}
		rec.state = "done";
		if (nextToFlush !== null && id < nextToFlush) {
			// A RESURRECTED recording: the cursor already passed it (it had failed, and later
			// takes committed after it), so its words land at the END of the draft rather than in
			// spoken position. Tell the composer, which tells the user.
			_commit(rec, true);
		} else {
			_drain();
		}
		_pump();
		_safe(onChange);
	}

	function _onReject(id, err, gen) {
		if (disposed) return;
		const rec = records.get(id);
		if (_isStale(rec, gen)) return;
		running -= 1;
		if (!rec) return;
		rec.inflight = false;
		rec._abort = null;
		if (rec.attempts < maxAttempts) {
			rec.state = "transcribing"; // ONE auto-retry (or more if maxAttempts raised)
			// …after a pause. Re-uploading the same megabytes into a 429 the instant it lands
			// just spends the user's budget twice as fast for the same answer.
			if (retryBackoffMs > 0) rec.retryAt = Date.now() + retryBackoffMs;
		} else {
			rec.state = "failed";
			rec.error = err ? String((err && err.message) || err) : "transcription failed";
			// The mirror is RETAINED (never deleted here) — the audio survives for the
			// Retry/Download chip and for reload recovery.
			if (nextToFlush !== null && id >= nextToFlush) _drain();
			_safe(onFail, id);
		}
		_pump();
		_safe(onChange);
	}

	// Emit a recording's transcript and mark it committed. Its audio mirror is dropped NOW only
	// when the consumer commits to a durable sink; under retainUntilSent it survives until
	// acknowledge()/discard so a reload can still recover the audio behind an un-sent draft.
	function _commit(rec, resurrected) {
		_safe(onCommit, rec.id, rec.text, rec, !!resurrected);
		rec.committed = true;
		if (!retainUntilSent) _mirrorDrop(rec);
	}

	// Advance the cursor over every contiguous terminal recording: a `done` one commits its text;
	// a `failed` or `discarded` one is skipped so it never halts a later take's words. Stops at the
	// first recording still being spoken or transcribed (later words must not overtake it).
	function _drain() {
		if (nextToFlush === null) return;
		while (records.has(nextToFlush)) {
			const rec = records.get(nextToFlush);
			if (rec.state === "done") {
				_commit(rec, false);
				nextToFlush += 1;
			} else if (rec.state === "failed" || rec.state === "discarded") {
				nextToFlush += 1;
			} else {
				break; // recording / transcribing — the cursor waits
			}
		}
	}

	// ---- public surface ----

	// A take has started. Returns its id; the caller feeds fragments in with addFragment() and
	// closes it with finish() (or discard() to abandon it).
	function begin(init = {}) {
		if (disposed) return null;
		const rec = _admit({
			conversationId: init.conversationId,
			mimeType: init.mimeType,
			state: "recording",
		});
		_safe(onChange);
		return rec.id;
	}

	// A ~15 s timeslice fragment arrived mid-take: hold it and mirror it NOW. `durationS` is the
	// recording's length up to and including this fragment, so a crash-recovered take reports the
	// audio that actually survived.
	function addFragment(id, frag) {
		if (disposed || !frag) return;
		const rec = records.get(id);
		if (!rec || (rec.state !== "recording" && rec.state !== "transcribing")) return;
		const entry = {
			index: Number(frag.index) || 0,
			blob: frag.blob,
			durationS: frag.durationS || 0,
			persisted: false,
		};
		rec.fragments.push(entry);
		if (entry.durationS > rec.durationS) rec.durationS = entry.durationS;
		_persistFragment(rec, entry);
	}

	// Adopt the assembled bytes + measurements the recorder handed over, and make sure SOMETHING
	// is mirrored for the take. Shared by finish() and finishSilent() so an inaudible recording is
	// exactly as durable and as downloadable as a transcribed one.
	function _closeTake(rec, take) {
		rec.blob = take.blob || rec.blob;
		if (take.durationS) rec.durationS = take.durationS;
		if (take.mimeType) rec.mimeType = take.mimeType;
		if (typeof take.peakRms === "number") rec.peakRms = take.peakRms;
		// Safety net for a take that closed without ever emitting a timeslice fragment (a stop
		// inside the first slice on a recorder that doesn't flush early): mirror the assembled
		// bytes as fragment 0, so the audio is durable and `persist` can ever reach "persisted".
		if (!rec.fragments.length && rec.blob) {
			const only = { index: 0, blob: rec.blob, durationS: rec.durationS, persisted: false };
			rec.fragments.push(only);
			_persistFragment(rec, only);
		}
	}

	// The take is closed: the caller hands over the ASSEMBLED blob (the fragments concatenated —
	// one valid webm, no re-encode) and the take moves to transcription, ONE call for the lot.
	function finish(id, take = {}) {
		if (disposed) return;
		const rec = records.get(id);
		if (!rec || rec.state !== "recording") return;
		_closeTake(rec, take);
		rec.state = "transcribing";
		rec.transcribingSince = Date.now();
		_pump();
		_safe(onChange);
	}

	// The take is closed, but the CLIENT measured it as inaudible end to end, so it is never
	// uploaded (whisper answers silence with a confident phrase nobody said, and the provider
	// exposes no no_speech_prob to filter on). It is NOT deleted: the measurement can be wrong —
	// a muted mic that still captures, a headset gain quirk, an analyser the OS starved — and
	// deleting on it would cost up to five minutes of real speech with no undo, breaking the one
	// invariant that outranks everything here. It lands in the SAME terminal `failed` state an
	// empty server transcript produces, so the existing chip offers Transcribe-anyway (retry),
	// Download and Discard, and the user decides.
	function finishSilent(id, take = {}) {
		if (disposed) return;
		const rec = records.get(id);
		if (!rec || rec.state !== "recording") return;
		_closeTake(rec, take);
		rec.state = "failed";
		rec.error = NO_SPEECH;
		if (nextToFlush !== null && id >= nextToFlush) _drain();
		_safe(onFail, id);
		_pump();
		_safe(onChange);
	}

	// The user gave up waiting on a transcription (the pill's Cancel). The in-flight upload is
	// ABORTED and the recording moves to the same terminal `failed` state a transcription error
	// produces — audio RETAINED, chip visible, Retry available. Never a discard: the point is to
	// unblock the composer, not to throw the recording away.
	function cancelTranscription(id) {
		if (disposed) return;
		const rec = records.get(id);
		if (!rec || rec.state !== "transcribing") return;
		// Bump the generation FIRST: the in-flight call's settlement is stale from here on and
		// must not decrement `running` again — this transition frees the slot itself.
		rec.gen += 1;
		if (rec.inflight) {
			running = Math.max(0, running - 1);
			rec.inflight = false;
		}
		rec.retryAt = 0;
		try {
			rec._abort && rec._abort.abort();
		} catch (e) {
			/* already aborted */
		}
		rec._abort = null;
		rec.state = "failed";
		rec.error = "you cancelled it";
		if (nextToFlush !== null && id >= nextToFlush) _drain();
		_safe(onFail, id);
		_pump();
		_safe(onChange);
	}

	// User pressed Retry on a failed recording's chip: re-send the SAME assembled bytes with a
	// fresh attempt budget.
	function retry(id) {
		if (disposed) return;
		const rec = records.get(id);
		if (!rec || rec.state !== "failed") return;
		rec.attempts = 0;
		rec.error = undefined;
		rec.retryAt = 0; // an explicit Retry is the user waiting — it never sits out a backoff
		rec.state = "transcribing";
		rec.transcribingSince = Date.now();
		_pump();
		_safe(onChange);
	}

	// User pressed Retry on an "audio couldn't be saved" chip: re-attempt every write that gave up.
	function retryPersist(id) {
		if (disposed) return;
		const rec = records.get(id);
		if (!rec || rec.state === "discarded" || rec.persist !== "failed") return;
		const again = rec._unwritten.slice();
		rec._unwritten = [];
		// A recovered take is ONE adopted copy, not a fragment sequence — re-run the adoption
		// (its prior-session keys are already gone, so there is nothing left to delete).
		if (rec.recovered) _persistAdoption(rec, []);
		else for (const frag of again) _persistFragment(rec, frag);
		_recomputePersist(rec);
		_safe(onChange);
	}

	// Re-admit recordings read back from the mirror after a reload. Each arrives ALREADY
	// assembled (fragments concatenated in index order by the caller) and is stamped with a FRESH
	// id off the same authority, then adopted into this session's mirror atomically.
	function recover(recordings) {
		if (disposed || !Array.isArray(recordings) || !recordings.length) return [];
		const ids = [];
		for (const r of recordings) {
			if (!r || !r.blob) continue;
			const rec = _admit({
				conversationId: r.conversationId,
				mimeType: r.mimeType,
				blob: r.blob,
				durationS: r.durationS,
				state: "transcribing",
				recovered: true,
			});
			rec.transcribingSince = Date.now();
			_persistAdoption(rec, r._adoptKeys || r.keys || []);
			ids.push(rec.id);
		}
		_pump();
		_safe(onChange);
		return ids;
	}

	// Drop a recording the user chose not to keep (Discard, or a cancelled take). It becomes a
	// terminal TOMBSTONE rather than a deleted map entry — deleting an un-flushed id would punch a
	// hole the cursor stops at forever, silently stranding every later transcript.
	function discard(id) {
		if (disposed) return;
		const rec = records.get(id);
		if (!rec || rec.state === "discarded") return;
		rec.gen += 1; // any in-flight settlement for this record is stale from here on
		if (rec.inflight) {
			running = Math.max(0, running - 1);
			rec.inflight = false;
			try {
				rec._abort && rec._abort.abort(); // stop uploading audio nobody wants any more
			} catch (e) {
				/* already aborted */
			}
		}
		rec._abort = null;
		rec.retryAt = 0;
		rec.state = "discarded";
		rec.text = "";
		rec.blob = null;
		rec.fragments = [];
		rec._unwritten = [];
		rec._writesInFlight = 0;
		_mirrorDrop(rec);
		if (nextToFlush !== null) _drain();
		_pump();
		_safe(onChange);
	}

	// Capture the EXACT set of committed recording ids for `scope` at THIS instant — the voice
	// records a payload about to be sent is carrying. This is the release TOKEN: a recording that
	// commits AFTER this capture is not in the returned set, so acknowledge()ing this token can
	// never release that later, still-un-sent recording.
	function captureSent(scope) {
		const target = scope == null ? null : scope;
		const token = [];
		for (const rec of records.values()) {
			if (!rec.committed || rec.state === "discarded") continue;
			if ((rec.conversationId != null ? rec.conversationId : null) === target)
				token.push(rec.id);
		}
		return token;
	}

	// PAYLOAD-BOUND capture: captureSent binds the release to every committed recording in
	// `scope`, but the user may have EDITED or DELETED a recording's transcript out of the composer
	// before sending — acknowledging it would delete audio whose text never left in the payload.
	// This variant admits a committed recording's id ONLY when its contributed text is actually
	// PRESENT in `payloadText` (normalized whitespace-collapse match). A recording whose text can't
	// be matched (edited/deleted/empty) is NOT acknowledged, so its audio is RETAINED and stays
	// actionable/recoverable — err toward never-lose. Occurrences are CONSUMED lowest-id first so,
	// when two takes contributed identical text and the user deleted ONE, only one id matches the
	// single surviving occurrence and the other's audio is kept. `normalize` is injectable for
	// tests; it defaults to the composer's collapse-whitespace-and-trim.
	function captureSentInPayload(scope, payloadText, normalize) {
		const target = scope == null ? null : scope;
		const norm =
			typeof normalize === "function"
				? normalize
				: (s) => (s == null ? "" : String(s)).replace(/\s+/g, " ").trim();
		const hay0 = norm(payloadText);
		const committed = [];
		for (const rec of records.values()) {
			if (!rec.committed || rec.state === "discarded") continue;
			if ((rec.conversationId != null ? rec.conversationId : null) === target)
				committed.push(rec);
		}
		committed.sort((a, b) => a.id - b.id);
		// Precompute every boundary-aligned occurrence RANGE of each transcript in the ORIGINAL
		// payload (before any consumption), so cross-recording overlaps are detectable.
		const cand = committed.map((rec) => {
			const needle = norm(rec.text);
			return { rec, needle, ranges: needle ? _boundaryRanges(hay0, needle) : [] };
		});
		// RETAIN-ON-AMBIGUITY: boundary-alignment still lets MULTI-WORD prefix/suffix overlaps
		// release the WRONG recording. Takes "send it", "now", "send it now" with a payload of
		// "send it now": greedy lowest-id consumption would acknowledge (delete) the first two
		// while keeping the one actually carrying "send it now" — the opposite of what was sent.
		// The payload span is genuinely ambiguous, so we acknowledge NEITHER; every affected
		// recording stays RETAINED + actionable. This can only retain MORE audio, never delete the
		// wrong one's. Identical transcripts are NOT flagged — that is the duplicate case resolved
		// by consuming one occurrence each.
		const ambiguous = new Set();
		for (let i = 0; i < cand.length; i++) {
			for (let j = i + 1; j < cand.length; j++) {
				if (cand[i].needle === cand[j].needle) continue; // duplicates → consume, not ambiguous
				if (!cand[i].ranges.length || !cand[j].ranges.length) continue;
				if (_rangesOverlap(cand[i].ranges, cand[j].ranges)) {
					ambiguous.add(cand[i].rec.id);
					ambiguous.add(cand[j].rec.id);
				}
			}
		}
		let hay = hay0;
		const token = [];
		for (const c of cand) {
			if (!c.needle) continue; // an empty contribution can't be matched → RETAIN
			if (ambiguous.has(c.rec.id)) continue; // ambiguous attribution → RETAIN (never-lose)
			const at = _boundaryIndexOf(hay, c.needle);
			if (at === -1) continue; // edited/deleted out of the payload → RETAIN (never-lose)
			// Consume this occurrence so an identical later take must find its OWN occurrence.
			hay = hay.slice(0, at) + " " + hay.slice(at + c.needle.length);
			token.push(c.rec.id);
		}
		return token;
	}

	// The payload carrying EXACTLY these captured ids was SENT — its voice-derived text is now
	// durable in the conversation, so drop just those committed recordings and their retained
	// audio: the leave guard/recovery must no longer treat them as un-sent. Only the
	// still-committed, non-discarded records named in the token are cleared.
	function acknowledge(token) {
		if (disposed || !token || !token.length) return;
		let changed = false;
		for (const id of token) {
			const rec = records.get(id);
			if (!rec || !rec.committed || rec.state === "discarded") continue;
			_mirrorDrop(rec);
			records.delete(id);
			changed = true;
		}
		if (changed) _safe(onChange);
	}

	// Migrate every non-discarded recording from one routing scope to another. Used when an
	// unsaved new-chat composer (the caller's sentinel) is promoted to a real conversation id: its
	// still-retained recordings — and their mirror — follow the conversation so a later send can
	// release them by the real scope instead of stranding them under the sentinel, where no send's
	// captureSent would ever match them.
	function reassignScope(fromScope, toScope) {
		if (disposed) return;
		const from = fromScope == null ? null : fromScope;
		let changed = false;
		for (const rec of records.values()) {
			if (rec.state === "discarded") continue;
			if ((rec.conversationId != null ? rec.conversationId : null) !== from) continue;
			rec.conversationId = toScope;
			if (mirror) {
				try {
					Promise.resolve(mirror.reassignConversation(rec.recordingId, toScope)).catch(
						noop
					);
				} catch (e) {
					/* best-effort */
				}
			}
			changed = true;
		}
		if (changed) _safe(onChange);
	}

	// Mark every committed recording in `scope` that was NOT sent (its id is absent from
	// `keepToken` — the payload-matched set) as ORPHANED. Its transcript was edited/deleted out of
	// the composer before the send, so a later payload match can never release it: without this it
	// lingers as a silent committed record that arms the leave guard forever with no chip. An
	// orphaned recording surfaces in snapshot().retained as ACTIONABLE (Restore/Download/Discard).
	function markUnsentOrphans(scope, keepToken) {
		if (disposed) return;
		const target = scope == null ? null : scope;
		const keep = keepToken && keepToken.length ? new Set(keepToken) : null;
		let changed = false;
		for (const rec of records.values()) {
			if (!rec.committed || rec.state === "discarded") continue;
			if (keep && keep.has(rec.id)) continue;
			if ((rec.conversationId != null ? rec.conversationId : null) !== target) continue;
			if (!rec.orphaned) {
				rec.orphaned = true;
				changed = true;
			}
		}
		if (changed) _safe(onChange);
	}

	// The committed ids for `scope` that are still terminally FAILED — the recordings an outgoing
	// message is about to leave behind. The composer captures these before a send so it can flag
	// them once the send is accepted (see markSentWithout).
	function failedIdsForScope(scope) {
		const target = scope == null ? null : scope;
		const out = [];
		for (const rec of records.values()) {
			if (rec.state !== "failed") continue;
			if ((rec.conversationId != null ? rec.conversationId : null) === target)
				out.push(rec.id);
		}
		return out;
	}

	// A message left this scope while this recording was still un-transcribed, so the sent,
	// immutable message does not contain what was said into it and nothing else records that. Flag
	// it so its chip can say so instead of reading like leftover clutter next to an already-
	// answered message, and so its Retry can promise a draft rather than an edit of a message that
	// has already gone. Only a terminal `failed` recording can be sent-WITHOUT: a `done` one's text
	// WAS in the payload, and releasing it is acknowledge()'s job. The audio stays retained either way.
	function markSentWithout(id) {
		if (disposed) return;
		const rec = records.get(id);
		if (!rec || rec.state !== "failed" || rec.sentWithout) return;
		rec.sentWithout = true;
		_safe(onChange);
	}

	// The user chose Restore on a retained recording — its transcript is going back into the
	// composer, so it is a normal un-sent draft recording again (the next send's payload match
	// releases it). Clears the orphaned flag so its chip disappears; the audio is untouched.
	function unorphan(id) {
		if (disposed) return;
		const rec = records.get(id);
		if (!rec || !rec.orphaned) return;
		rec.orphaned = false;
		_safe(onChange);
	}

	function snapshot() {
		let total = 0;
		let capturing = 0;
		// [{id, durationS, since}] — drives the inline "Transcribing M:SS…" pill. `since` is when
		// the wait actually started, so the pill can report elapsed time rather than invent
		// progress it has no way of knowing.
		const transcribing = [];
		// [{id, durationS, sentWithout, error, noSpeech}] — drives the failed chip
		// (Retry/Download/Discard). `sentWithout` marks a recording a send already left behind:
		// the chip's copy branches on it, because "not sent yet" and "sent without this" are
		// otherwise indistinguishable. `error` is WHY it failed — a permanent config fault
		// ("Speech-to-text is not enabled on this site.") and a transient blip otherwise read
		// identically, so users Retry-loop megabytes forever and nobody files a useful ticket.
		// `noSpeech` separates "we heard nothing" from "it didn't transcribe", which need
		// different copy and a different verb on the retry button.
		const failed = [];
		// [{id, durationS, text}] — a transcribed recording whose words were edited OUT before the
		// send: Restore/Download/Discard, so the audio is resolvable instead of arming the leave
		// guard forever with no affordance.
		const retained = [];
		// [{id, durationS}] — recordings whose audio could NOT be confirmed durably saved:
		// Download/Retry chips. Orthogonal to the transcription state, so scanned independently.
		const unpersisted = [];
		for (const rec of records.values()) {
			if (rec.state === "discarded") continue; // a user-removed tombstone — invisible to the UI
			total += 1;
			if (rec.state === "recording") capturing += 1;
			else if (rec.state === "transcribing")
				transcribing.push({
					id: rec.id,
					durationS: rec.durationS,
					since: rec.transcribingSince || 0,
				});
			else if (rec.state === "failed")
				failed.push({
					id: rec.id,
					durationS: rec.durationS,
					sentWithout: !!rec.sentWithout,
					error: rec.error ? String(rec.error) : "",
					noSpeech: rec.error === NO_SPEECH,
				});
			else if (rec.state === "done" && rec.orphaned)
				retained.push({ id: rec.id, durationS: rec.durationS, text: rec.text });
			if (rec.persist === "failed")
				unpersisted.push({ id: rec.id, durationS: rec.durationS });
		}
		return {
			capturing,
			transcribing,
			failed,
			retained,
			unpersisted,
			total,
			hasUnfinished:
				capturing +
					transcribing.length +
					failed.length +
					retained.length +
					unpersisted.length >
				0,
		};
	}

	// WHY anything is still outstanding — the distinction the composer's leave guard needs so it
	// stops crying wolf. Returns:
	//   "live"       — leaving now can genuinely LOSE something: a take still being spoken or
	//                  transcribed, a recording whose audio could NOT be confirmed durably
	//                  mirrored (nothing to recover it from), or, under retainUntilSent, a
	//                  transcribed recording whose words exist ONLY in this volatile, un-sent draft.
	//   "unresolved" — only terminal recordings the user hasn't dealt with remain: a `failed` one,
	//                  or one a send already left behind. Every one of them is durably mirrored and
	//                  re-offered by the recovery banner next visit, so leaving loses NOTHING —
	//                  loss-framed copy here is factually wrong and trains users to click through
	//                  the warning that does matter.
	//   null         — nothing outstanding at all.
	function hasUnfinishedReason() {
		let unresolved = false;
		for (const rec of records.values()) {
			if (rec.state === "discarded") continue;
			// A recording whose audio isn't confirmed durable is UN-safe regardless of its
			// transcription state — the guard must stay armed until it persists or is discarded.
			if (rec.persist === "failed") return "live";
			if (rec.state === "recording" || rec.state === "transcribing") return "live";
			if (rec.state === "failed") {
				// "Durably mirrored" is the ENTIRE justification for not warning about a failed
				// recording — so it has to be CONFIRMED, not merely in flight (a write that has
				// not settled yet may still fail, and then there is nothing to recover from).
				if (rec.persist === "persisting") return "live";
				unresolved = true; // terminal + confirmed durable: actionable, nothing at risk
				continue;
			}
			if (retainUntilSent && rec.committed) {
				// A sent-without recording's words are back in the draft, and an ORPHANED one's
				// were edited out of the message before it went — in both cases the audio is
				// still mirrored and re-offered by the recovery banner, and its chip is right
				// there. Terminal-unresolved, not at-risk: loss-framed copy for either is the
				// cry-wolf design.md §4.5 forbids. (Durability has to be CONFIRMED to say that,
				// exactly as for a failed recording.)
				if (rec.sentWithout || rec.orphaned) {
					if (rec.persist === "persisting") return "live";
					unresolved = true;
				} else return "live";
			}
		}
		return unresolved ? "unresolved" : null;
	}

	// True while ANY recording is un-safe OR merely unresolved — the coarse predicate for callers
	// that only need "is anything outstanding". The composer arms its guard off
	// hasUnfinishedReason() instead, so a terminally-failed recording raises no loss warning.
	function hasUnfinished() {
		return hasUnfinishedReason() !== null;
	}

	// The record for `id` — the composer reads `blob`/`durationS` off it for Download. While a take
	// is still recording the assembled bytes don't exist yet, so the fragments are joined on demand.
	function get(id) {
		const rec = records.get(id);
		if (!rec || rec.state === "discarded") return null;
		return rec;
	}

	// Stop accepting callbacks (component unmount) — a late transcription must not poke a dead
	// composer — and ABORT in-flight uploads. The mirror is left intact so a reload can still
	// recover anything un-transcribed.
	function dispose() {
		disposed = true;
		if (_retryTimer) {
			clearTimeout(_retryTimer);
			_retryTimer = null;
			_retryTimerAt = 0;
		}
		try {
			abort && abort.abort();
		} catch (e) {
			/* ignore */
		}
		// No store-wide controller (an environment without AbortController) — abort each
		// dispatch's own, so an unmount never leaves an upload running to its full budget.
		for (const rec of records.values()) {
			try {
				rec._abort && rec._abort.abort();
			} catch (e) {
				/* ignore */
			}
		}
	}

	return {
		begin,
		addFragment,
		finish,
		finishSilent,
		cancelTranscription,
		retry,
		retryPersist,
		recover,
		discard,
		captureSent,
		captureSentInPayload,
		acknowledge,
		reassignScope,
		markUnsentOrphans,
		markSentWithout,
		failedIdsForScope,
		unorphan,
		snapshot,
		hasUnfinished,
		hasUnfinishedReason,
		get,
		dispose,
	};
}
