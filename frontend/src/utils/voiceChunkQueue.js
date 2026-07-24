// voiceChunkQueue — the ordering + retry + retention state machine behind chunked
// voice dictation, extracted as a PLAIN, importable, unit-tested module (the
// composer imports it; useChunkedRecorder feeds clips into it) exactly like the
// Relay-Pump eventFence precedent. It owns NO browser APIs: MediaRecorder,
// `fetch`, and IndexedDB are all injected, so its whole contract is testable with
// `node --test` (see voiceChunkQueue.test.js, run in the python suite forever via
// jarvis/tests/test_voice_chunk_queue_client.py).
//
// The problem it solves: long dictation is captured as many self-contained ~15 s
// WebM clips (the header trap forbids timeslice continuation chunks — see the
// investigation). Each clip is transcribed by an INDEPENDENT async request to the
// existing stateless endpoint, so responses arrive OUT OF ORDER, some time out,
// and the audio must NEVER be lost. This module guarantees:
//
//   * STRICT in-order commit — text is appended to the composer in `seq` order via
//     a `nextToFlush` cursor, never a later seq before its predecessor, even when
//     transcriptions resolve out of order.
//   * BOUNDED concurrency — at most `concurrency` (default 2) requests in flight.
//   * per-chunk budget + ONE auto-retry — the injected `transcribe` owns the 25 s
//     abort; this module retries a rejected chunk exactly once, then the chunk
//     enters the terminal `failed` state (the session is NEVER halted — the cursor
//     advances PAST a failed chunk so later chunks keep committing).
//   * a NEVER-LOST mirror — every clip is written to the injected `mirror` on
//     enqueue and deleted only once its text is committed; a `failed` clip is
//     RETAINED (for the Retry/Download chip and reload recovery).
//
// Chunk record state machine (per seq):
//
//        enqueue()                     _pump() picks lowest-seq pending
//     ─────────────►  pending  ───────────────────────────────►  inflight
//                        ▲                                          │
//         retry(seq) or  │  attempts < maxAttempts (auto-retry)     │
//         auto-retry ────┘◄─────────────────────────────  reject ───┤
//                                                                    │ resolve
//                        attempts === maxAttempts (reject)           ▼
//                     ┌──────────────►  failed  ◄──── retry(seq)     done
//                     │                   │  (mirror RETAINED)        │
//                     │                   └── retry → pending         │ _drain(): onCommit(seq,text)
//                     │                                               │ in cursor order, mirror.delete
//                     └───────────────────────────────────────────► (terminal)
//
// `done` and `failed` are BOTH drainable: _drain() advances `nextToFlush` while the
// chunk at the cursor is done (commit its text) or failed (skip — the chip holds
// the user's place; retried text appends at the composer end, the simpler correct
// variant, documented in ChatView). A pending/inflight chunk at the cursor blocks
// the cursor (its predecessors' words must not jump ahead of it) but does NOT halt
// recording — the recorder keeps cycling; committed text merely lags that gap by
// the retry window, then flushes contiguously the instant the gap goes terminal.
//
// SECURITY NOTE: transcription text flows OUT of this module (onCommit → composer)
// only. It is NEVER fed back into any transcribe call or prompt — there is no
// rolling-context path here by design (Fable rejected that variant on
// prompt-injection grounds). The module never inspects or forwards transcript text.

const noop = () => {};

// clip: an opaque, self-contained recorded unit — { seq, blob, durationS }. Only
// `seq` (a monotonic integer assigned by the recorder) is read here; `blob` /
// `durationS` are passed straight through to `transcribe` and `mirror`.
//
// deps:
//   transcribe(clip)  -> Promise<string>   REQUIRED. Rejects on timeout/error; the
//                                           25 s per-chunk abort lives INSIDE it.
//   mirror { put(clip)->Promise?, delete(seq)->Promise?, all()->Promise<clip[]> }
//                                           OPTIONAL crash-safety store (IndexedDB
//                                           in prod, an in-memory Map in tests). All
//                                           calls are best-effort (errors swallowed)
//                                           so a mirror hiccup never drops audio from
//                                           the in-memory source of truth.
//   concurrency       max in-flight requests (default 2)
//   maxAttempts       total tries per chunk before `failed` (default 2 = 1 + 1 retry)
//   onCommit(seq,text) called once per chunk, in strict cursor order, when its text
//                      is ready to append to the composer.
//   onFail(seq)        called when a chunk reaches the terminal `failed` state.
//   onChange()         called after every state transition (drive reactive UI).
export function createVoiceChunkQueue(deps = {}) {
	const transcribe = deps.transcribe;
	if (typeof transcribe !== "function") {
		throw new Error("createVoiceChunkQueue: `transcribe` dependency is required");
	}
	const mirror = deps.mirror || null;
	const concurrency = Math.max(1, deps.concurrency || 2);
	const maxAttempts = Math.max(1, deps.maxAttempts || 2);
	const onCommit = deps.onCommit || noop;
	const onFail = deps.onFail || noop;
	const onChange = deps.onChange || noop;

	// seq -> { seq, clip, state, attempts, text }
	//   state: 'pending' | 'inflight' | 'done' | 'failed'
	const records = new Map();
	let inflight = 0;
	let nextToFlush = null; // the cursor; initialised to the first enqueued seq
	let nextRecoverSeq = 0; // fresh contiguous seq space for recover() batches
	let disposed = false;

	function _mirrorPut(clip) {
		try {
			Promise.resolve(mirror && mirror.put(clip)).catch(noop);
		} catch (e) {
			/* best-effort */
		}
	}
	function _mirrorDelete(seq) {
		try {
			Promise.resolve(mirror && mirror.delete(seq)).catch(noop);
		} catch (e) {
			/* best-effort */
		}
	}

	// Start as many pending chunks as the concurrency budget allows, always the
	// LOWEST seq first so the cursor's blocker is worked on soonest (minimises the
	// gap-window lag). Pending includes both never-tried chunks and auto-retries.
	function _pump() {
		if (disposed) return;
		while (inflight < concurrency) {
			let pick = null;
			for (const rec of records.values()) {
				if (rec.state === "pending" && (pick === null || rec.seq < pick.seq)) pick = rec;
			}
			if (!pick) break;
			pick.state = "inflight";
			pick.attempts += 1;
			inflight += 1;
			const seq = pick.seq;
			// Promise.resolve() tolerates a sync-throwing or non-promise transcribe.
			Promise.resolve()
				.then(() => transcribe(pick.clip))
				.then(
					(text) => _onResolve(seq, text),
					(err) => _onReject(seq, err)
				);
		}
	}

	function _onResolve(seq, text) {
		if (disposed) return;
		const rec = records.get(seq);
		inflight -= 1;
		if (!rec) return; // dropped (shouldn't happen); keep the count honest
		rec.text = typeof text === "string" ? text : text == null ? "" : String(text);
		rec.state = "done";
		if (nextToFlush !== null && seq < nextToFlush) {
			// A RESURRECTED failed chunk (its slot was already skipped by the cursor).
			// It cannot rejoin the contiguous drain, so commit it directly — the glue
			// appends it at the composer end (documented: retried gap text lands at the
			// cursor position at retry time, not reinserted mid-draft).
			onCommit(seq, rec.text);
			_mirrorDelete(seq);
		} else {
			_drain();
		}
		_pump();
		onChange();
	}

	function _onReject(seq, err) {
		if (disposed) return;
		const rec = records.get(seq);
		inflight -= 1;
		if (!rec) return;
		if (rec.attempts < maxAttempts) {
			rec.state = "pending"; // ONE auto-retry (or more if maxAttempts raised)
		} else {
			rec.state = "failed";
			rec.error = err ? String((err && err.message) || err) : "transcription failed";
			// Mirror is RETAINED (never deleted here) — the clip survives for the
			// Retry/Download chip and for reload recovery.
			if (nextToFlush !== null && seq >= nextToFlush) _drain();
			onFail(seq);
		}
		_pump();
		onChange();
	}

	// Advance the cursor over every contiguous terminal chunk: a `done` chunk
	// commits its text; a `failed` chunk is skipped (fail-through) so it never
	// halts later chunks. Stops at the first pending/inflight chunk (words must not
	// overtake an as-yet-unresolved predecessor) or the first not-yet-seen seq.
	function _drain() {
		if (nextToFlush === null) return;
		while (records.has(nextToFlush)) {
			const rec = records.get(nextToFlush);
			if (rec.state === "done") {
				onCommit(rec.seq, rec.text);
				_mirrorDelete(rec.seq);
				nextToFlush += 1;
			} else if (rec.state === "failed") {
				nextToFlush += 1; // fail-through: the chip holds the user's place
			} else {
				break; // pending / inflight — the cursor waits (recording continues)
			}
		}
	}

	// ---- public surface ----

	// Enqueue a freshly recorded clip. `clip.seq` MUST be a monotonic integer
	// (the recorder guarantees contiguity within a live session).
	function enqueue(clip) {
		if (disposed || !clip) return;
		const seq = clip.seq;
		if (records.has(seq)) return; // idempotent guard
		records.set(seq, { seq, clip, state: "pending", attempts: 0, text: "" });
		if (nextToFlush === null || seq < nextToFlush) nextToFlush = seq;
		if (seq >= nextRecoverSeq) nextRecoverSeq = seq + 1;
		_mirrorPut(clip);
		_pump();
		onChange();
	}

	// User pressed Retry on a failed chunk's chip: give it a fresh attempt budget.
	function retry(seq) {
		if (disposed) return;
		const rec = records.get(seq);
		if (!rec || rec.state !== "failed") return;
		rec.attempts = 0;
		rec.error = undefined;
		rec.state = "pending";
		_pump();
		onChange();
	}

	// Re-enqueue clips read back from the mirror after a reload. They may be
	// non-contiguous (only un-transcribed clips survive), so they are reindexed
	// onto a fresh contiguous seq space and enqueued in ascending original order —
	// their absolute original position is moot post-reload; we only need them
	// transcribed and appended in their spoken order.
	function recover(clips) {
		if (disposed || !Array.isArray(clips) || !clips.length) return [];
		const ordered = clips.slice().sort((a, b) => (a.seq || 0) - (b.seq || 0));
		const seqs = [];
		for (const c of ordered) {
			const seq = nextRecoverSeq++;
			const clip = { ...c, seq, recovered: true };
			records.set(seq, { seq, clip, state: "pending", attempts: 0, text: "" });
			if (nextToFlush === null || seq < nextToFlush) nextToFlush = seq;
			_mirrorPut(clip); // rewrite under the new seq so its delete lands
			if (c.seq != null && c.seq !== seq) _mirrorDelete(c.seq); // drop the stale key
			seqs.push(seq);
		}
		_pump();
		onChange();
		return seqs;
	}

	// Drop a clip the user chose NOT to recover/retry (e.g. downloaded instead):
	// remove it from the source of truth and the mirror so it stops blocking
	// hasUnfinished()/the unload guard.
	function discard(seq) {
		if (disposed) return;
		const rec = records.get(seq);
		if (!rec) return;
		records.delete(seq);
		_mirrorDelete(seq);
		if (rec.state === "inflight") inflight = Math.max(0, inflight - 1);
		if (nextToFlush !== null) _drain();
		_pump();
		onChange();
	}

	function snapshot() {
		let pending = 0;
		let running = 0;
		let done = 0;
		const failed = [];
		for (const rec of records.values()) {
			if (rec.state === "pending") pending += 1;
			else if (rec.state === "inflight") running += 1;
			else if (rec.state === "done") done += 1;
			else if (rec.state === "failed") failed.push({ seq: rec.seq, clip: rec.clip });
		}
		return {
			pending,
			inflight: running,
			done,
			failed, // [{seq, clip}] — drives the composer chips (Retry/Download)
			total: records.size,
			hasUnfinished: pending + running + failed.length > 0,
		};
	}

	// True while ANY clip is still pending / in flight / failed — the composer uses
	// this to arm the beforeunload + route-leave guard so audio is never silently
	// lost on navigation.
	function hasUnfinished() {
		for (const rec of records.values()) {
			if (rec.state !== "done") return true;
		}
		return false;
	}

	function getClip(seq) {
		const rec = records.get(seq);
		return rec ? rec.clip : null;
	}

	// Stop accepting callbacks (component unmount) — late transcribe resolutions
	// must not poke a dead composer. The mirror is left intact so a reload can
	// still recover anything un-transcribed.
	function dispose() {
		disposed = true;
	}

	return {
		enqueue,
		retry,
		recover,
		discard,
		snapshot,
		hasUnfinished,
		getClip,
		dispose,
	};
}
