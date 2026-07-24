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
//   * SINGLE seq authority — THE QUEUE stamps every clip's `seq` from one monotonic
//     counter (enqueue AND recover), so the recorder and recovery can never collide
//     on a seq and silently drop a live clip (the VAR-1 P0). Callers pass clips
//     WITHOUT a seq; the queue assigns and returns it.
//   * STRICT in-order commit — text is appended to the composer in `seq` order via
//     a `nextToFlush` cursor, never a later seq before its predecessor, even when
//     transcriptions resolve out of order.
//   * BOUNDED concurrency — at most `concurrency` (default 2) requests in flight.
//   * per-chunk budget + ONE auto-retry — the injected `transcribe` owns the 25 s
//     abort; this module retries a rejected chunk exactly once, then the chunk
//     enters the terminal `failed` state (the session is NEVER halted — the cursor
//     advances PAST a failed chunk so later chunks keep committing).
//   * a NEVER-LOST mirror — every clip is written to the injected `mirror` on
//     enqueue and deleted only once its text is durably captured: on commit by default,
//     or (under `retainUntilSent`, which the composer sets because a draft is volatile
//     until sent) on markSent()/discard. A `failed` clip is RETAINED either way (for the
//     Retry/Download chip and reload recovery).
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
//                     │                   └── retry → pending         │ _drain(): onCommit(seq,text,clip)
//                     │                                               │ in cursor order, mirror.delete
//                     └───────────────────────────────────────────► (terminal)
//
// `done` and `failed` are BOTH drainable: _drain() advances `nextToFlush` while the
// chunk at the cursor is done (commit its text) or failed. A failed chunk is NOT
// dropped from the composed text — as the cursor crosses it, onGap(seq) fires so the
// glue drops an IN-PLACE placeholder token at that ordered position (Fable ruling:
// the chip anchors the gap's spot; retried text replaces the token where it belongs,
// not bolted onto the end where it reads as broken prose). Later chunks then commit
// AFTER the placeholder, so the composed text stays in strict spoken order across the
// gap. A pending/inflight chunk at the cursor blocks the cursor (its predecessors'
// words must not jump ahead of it) but does NOT halt recording — the recorder keeps
// cycling; committed text merely lags that gap by the retry window, then flushes
// contiguously the instant the gap goes terminal.
//
// SECURITY NOTE: transcription text flows OUT of this module (onCommit → composer)
// only. It is NEVER fed back into any transcribe call or prompt — there is no
// rolling-context path here by design (Fable rejected that variant on
// prompt-injection grounds). The module never inspects or forwards transcript text.

const noop = () => {};

// Invoke a caller callback WITHOUT letting a throw unwind the drain/pump chain — a
// bug in onCommit (reactive writes, nextTick) must not wedge the cursor and freeze
// every later commit for the rest of the session (VAR-7).
function _safe(fn, ...args) {
	try {
		fn(...args);
	} catch (e) {
		/* a caller callback threw; swallow so the state machine keeps draining */
	}
}

// clip: an opaque, self-contained recorded unit — { blob, durationS, ... }. The
// caller does NOT set `seq`; the queue assigns it (single authority). Extra fields
// (e.g. conversationId) ride through untouched to `mirror` and to onCommit's 3rd arg.
//
// deps:
//   transcribe(clip, signal) -> Promise<string>   REQUIRED. Rejects on timeout/error;
//                                           the 25 s per-chunk abort lives INSIDE it.
//                                           `signal` (may be undefined) aborts on
//                                           dispose() so in-flight uploads cancel.
//   mirror { put(clip)->Promise?, delete(seq)->Promise?, all()->Promise<clip[]> }
//                                           OPTIONAL crash-safety store (IndexedDB
//                                           in prod, an in-memory Map in tests). All
//                                           calls are best-effort (errors swallowed)
//                                           so a mirror hiccup never drops audio from
//                                           the in-memory source of truth.
//   concurrency       max in-flight requests (default 2)
//   maxAttempts       total tries per chunk before `failed` (default 2 = 1 + 1 retry)
//   onCommit(seq,text,clip,replace) called once per chunk, in strict cursor order, when
//                      its text is ready. `clip` carries the routing key (conversationId)
//                      so the glue writes to the right conversation. `replace` is true
//                      ONLY for a RESURRECTED failed chunk (retried after the cursor
//                      passed it): the glue REPLACES that chunk's in-place placeholder
//                      token with the text rather than appending at the end.
//   onFail(seq)        called when a chunk reaches the terminal `failed` state.
//   onGap(seq,clip)    called ONCE, as the drain cursor crosses a `failed` chunk, so the
//                      glue can drop an in-place placeholder token at the gap's ordered
//                      position (later chunks commit after it — order preserved).
//   onChange()         called after every state transition (drive reactive UI).
export function createVoiceChunkQueue(deps = {}) {
	const transcribe = deps.transcribe;
	if (typeof transcribe !== "function") {
		throw new Error("createVoiceChunkQueue: `transcribe` dependency is required");
	}
	const mirror = deps.mirror || null;
	const concurrency = Math.max(1, deps.concurrency || 2);
	const maxAttempts = Math.max(1, deps.maxAttempts || 2);
	// When set, a committed chunk's audio mirror is NOT dropped on commit — it is retained
	// until markSent()/discard(). The composer opts in because its draft text is volatile
	// (component-local, wiped on reload) until the message is actually SENT, so the audio
	// must stay recoverable behind an un-sent draft (finding 6). Consumers that commit to a
	// durable sink leave it off (default): the mirror is cleared the moment the text lands.
	const retainUntilSent = !!deps.retainUntilSent;
	const onCommit = deps.onCommit || noop;
	const onFail = deps.onFail || noop;
	const onGap = deps.onGap || noop;
	const onChange = deps.onChange || noop;

	// seq -> { seq, clip, state, attempts, text }
	//   state: 'pending' | 'inflight' | 'done' | 'failed'
	const records = new Map();
	let inflight = 0;
	let nextToFlush = null; // the cursor; initialised to the first assigned seq (0)
	let _nextSeq = 0; // THE single seq authority (enqueue + recover both draw from it)
	let disposed = false;
	// Aborts every in-flight upload on dispose() so navigate-away doesn't leave
	// fetches running to their 25 s timeout (VAR-8 / VUX-12).
	const abort = typeof AbortController !== "undefined" ? new AbortController() : null;

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

	// Assign a record onto the single seq space and mirror it. Shared by enqueue and
	// recover so no two producers can ever pick the same seq.
	function _admit(clip) {
		const seq = _nextSeq++;
		const stored = { ...clip, seq };
		records.set(seq, { seq, clip: stored, state: "pending", attempts: 0, text: "" });
		if (nextToFlush === null) nextToFlush = seq; // seqs only ever increase from here
		_mirrorPut(stored);
		return seq;
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
				.then(() => transcribe(pick.clip, abort ? abort.signal : undefined))
				.then(
					(text) => _onResolve(seq, text),
					(err) => _onReject(seq, err)
				);
		}
	}

	// Emit a chunk's transcript (in cursor order, or in-place for a resurrected gap) and
	// mark it committed. Its audio mirror is dropped NOW only when the consumer commits to a
	// durable sink; under retainUntilSent the mirror survives until markSent()/discard so a
	// reload can still recover the audio behind an un-sent, volatile draft (finding 6).
	function _commit(rec, replace) {
		_safe(onCommit, rec.seq, rec.text, rec.clip, replace);
		rec.committed = true;
		if (!retainUntilSent) _mirrorDelete(rec.seq);
	}

	function _onResolve(seq, text) {
		if (disposed) return;
		const rec = records.get(seq);
		// A clip the user discarded mid-flight is a terminal tombstone — a late resolution
		// must not resurrect it (its inflight slot was already freed by discard()).
		if (rec && rec.state === "discarded") return;
		// CONTRACT BELT (defense in depth): the `transcribe` adapter MUST resolve a STRING. A
		// non-string (e.g. the raw {ok,text,...} response object) must NEVER be String()-
		// coerced and committed as "[object Object]" behind deleted audio — route it through
		// the failure path so the clip is retried, then RETAINED (never-lose-audio).
		if (typeof text !== "string") {
			_onReject(seq, new Error("transcribe resolved a non-string value"));
			return; // _onReject owns the inflight decrement + retry/fail transition
		}
		inflight -= 1;
		if (!rec) return; // dropped (shouldn't happen); keep the count honest
		rec.text = text;
		rec.state = "done";
		if (nextToFlush !== null && seq < nextToFlush) {
			// A RESURRECTED failed chunk (its slot was already skipped by the cursor, so
			// its in-place placeholder token is already sitting in the composer). It can't
			// rejoin the contiguous drain — commit it with `replace=true` so the glue swaps
			// the placeholder for the text IN PLACE, not appended at the end.
			_commit(rec, true);
		} else {
			_drain();
		}
		_pump();
		_safe(onChange);
	}

	function _onReject(seq, err) {
		if (disposed) return;
		const rec = records.get(seq);
		if (rec && rec.state === "discarded") return;
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
			_safe(onFail, seq);
		}
		_pump();
		_safe(onChange);
	}

	// Advance the cursor over every contiguous terminal chunk: a `done` chunk
	// commits its text; a `failed` chunk drops an IN-PLACE placeholder (onGap) at its
	// ordered position and is then skipped (fail-through) so it never halts later
	// chunks. Stops at the first pending/inflight chunk (words must not overtake an
	// as-yet-unresolved predecessor) or the first not-yet-seen seq.
	function _drain() {
		if (nextToFlush === null) return;
		while (records.has(nextToFlush)) {
			const rec = records.get(nextToFlush);
			if (rec.state === "done") {
				_commit(rec, false);
				nextToFlush += 1;
			} else if (rec.state === "failed") {
				// Anchor the gap where it belongs (once — the cursor never revisits it).
				_safe(onGap, rec.seq, rec.clip);
				nextToFlush += 1; // fail-through: later chunks commit after the placeholder
			} else if (rec.state === "discarded") {
				// A user-discarded clip is a terminal tombstone: advance ACROSS it (no commit,
				// no gap — its audio + placeholder are already gone) so later done chunks still
				// flush. Deleting the seq instead would wedge the cursor at the hole forever
				// and silently strand every later transcript (finding 4).
				nextToFlush += 1;
			} else {
				break; // pending / inflight — the cursor waits (recording continues)
			}
		}
	}

	// ---- public surface ----

	// Enqueue a freshly recorded clip; the queue STAMPS its seq (returned). Callers
	// must NOT pre-set `seq` — the single-authority counter is what makes a
	// recorder/recovery collision structurally impossible.
	function enqueue(clip) {
		if (disposed || !clip) return null;
		const seq = _admit(clip);
		_pump();
		_safe(onChange);
		return seq;
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
		_safe(onChange);
	}

	// Re-enqueue clips read back from the mirror after a reload. They are stamped
	// with FRESH seqs off the same authority (in ascending original-seq order, so
	// their spoken order is preserved) — their absolute original position is moot
	// post-reload; we only need them transcribed and appended in that order.
	function recover(clips) {
		if (disposed || !Array.isArray(clips) || !clips.length) return [];
		// Preserve the session grouping/order the caller supplied (listOrphanClips already
		// ordered sessions newest-first) and sort each session's clips by ORIGINAL seq so
		// spoken order is restored WITHIN a session. A single global seq sort would
		// interleave sessions — each session's seq restarts at 0 — rebuilding the dictation
		// out of order (finding 3).
		const bySession = new Map();
		const order = [];
		for (const c of clips) {
			const sid = c.sessionId == null ? "" : String(c.sessionId);
			if (!bySession.has(sid)) {
				bySession.set(sid, []);
				order.push(sid);
			}
			bySession.get(sid).push(c);
		}
		const seqs = [];
		for (const sid of order) {
			const group = bySession
				.get(sid)
				.slice()
				.sort((a, b) => (a.seq || 0) - (b.seq || 0));
			for (const c of group) {
				// Drop the caller's stale seq + sessionId before re-admitting under a fresh seq
				// off the single authority. `_adoptKey` (if present) rides through to the mirror
				// so the prior-session copy is deleted in the SAME put transaction (finding 5).
				const { seq: _staleSeq, sessionId: _sid, ...rest } = c;
				seqs.push(_admit({ ...rest, recovered: true }));
			}
		}
		_pump();
		_safe(onChange);
		return seqs;
	}

	// Drop a clip the user chose NOT to recover/retry (e.g. downloaded instead): stop it
	// blocking hasUnfinished()/the unload guard. It becomes a terminal TOMBSTONE rather than
	// a deleted map entry — deleting an un-flushed seq would punch a hole the flush cursor
	// stops at forever, silently stranding every later done transcript (finding 4).
	function discard(seq) {
		if (disposed) return;
		const rec = records.get(seq);
		if (!rec || rec.state === "discarded") return;
		if (rec.state === "inflight") inflight = Math.max(0, inflight - 1);
		rec.state = "discarded";
		rec.text = "";
		rec.clip = null;
		_mirrorDelete(seq);
		if (nextToFlush !== null) _drain();
		_pump();
		_safe(onChange);
	}

	// The composer for `scope` was SENT — its voice-derived text is now durably in the
	// conversation, so drop those committed clips and their retained audio mirror (finding
	// 6): the leave guard/​recovery must no longer treat them as un-sent. Only committed
	// clips for THIS scope are cleared; pending/inflight/failed clips (still un-transcribed)
	// and other scopes' held drafts are untouched.
	function markSent(scope) {
		if (disposed) return;
		let changed = false;
		for (const rec of Array.from(records.values())) {
			if (!rec.committed || rec.state === "discarded") continue;
			const clipScope =
				rec.clip && rec.clip.conversationId != null ? rec.clip.conversationId : null;
			if (clipScope === (scope == null ? null : scope)) {
				_mirrorDelete(rec.seq);
				records.delete(rec.seq);
				changed = true;
			}
		}
		if (changed) _safe(onChange);
	}

	function snapshot() {
		let pending = 0;
		let running = 0;
		let done = 0;
		let total = 0;
		const failed = [];
		for (const rec of records.values()) {
			if (rec.state === "discarded") continue; // a user-removed tombstone — invisible to the UI
			total += 1;
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
			total,
			hasUnfinished: pending + running + failed.length > 0,
		};
	}

	// True while ANY clip is still un-safe — the composer uses this to arm the beforeunload
	// + route-leave guard so audio is never silently lost on navigation. Un-safe means:
	// still pending / in flight / failed (transcription incomplete); OR, under
	// retainUntilSent, committed but still mirrored — its transcript lives only in an
	// un-sent, volatile draft (finding 6). A discarded tombstone is safe.
	function hasUnfinished() {
		for (const rec of records.values()) {
			if (rec.state === "discarded") continue;
			if (rec.state !== "done") return true;
			if (retainUntilSent && rec.committed) return true;
		}
		return false;
	}

	function getClip(seq) {
		const rec = records.get(seq);
		return rec ? rec.clip : null;
	}

	// Stop accepting callbacks (component unmount) — late transcribe resolutions
	// must not poke a dead composer — and ABORT in-flight uploads. The mirror is
	// left intact so a reload can still recover anything un-transcribed.
	function dispose() {
		disposed = true;
		try {
			abort && abort.abort();
		} catch (e) {
			/* ignore */
		}
	}

	return {
		enqueue,
		retry,
		recover,
		discard,
		markSent,
		snapshot,
		hasUnfinished,
		getClip,
		dispose,
	};
}
