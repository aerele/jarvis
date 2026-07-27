// voiceAudioMirror — the crash-safety store behind the "audio is NEVER lost" invariant.
//
// ONE dictation take = one RECORDING, transcribed in ONE call. While it is being spoken the
// MediaRecorder timeslice hands us a ~15 s FRAGMENT at a time, and each fragment is written
// here the instant it arrives — so a tab crash mid-take costs at most the last ≤15 s, never
// the whole recording. Fragments of one recording are CONTINUATIONS of a single stream:
// concatenating them in `index` order reproduces exactly the bytes MediaRecorder would have
// emitted, and a PREFIX of them is still a decodable webm (media containers truncate
// gracefully). That is what makes recovery possible at all — see groupOrphanRecordings.
//
// The in-memory voiceDictationStore is the source of truth; this is a durable mirror whose
// write result is OBSERVED (an unconfirmed write must never be treated as safe). Every op is
// wrapped so an IndexedDB hiccup (private mode, quota, unsupported) never throws into the
// store — it degrades crash-safety and says so, it never drops live audio.
//
// DB/store names are deliberately UNCHANGED from the per-clip release: a browser that still
// holds un-transcribed clips from the previous version must not have them silently deleted by
// an upgrade. Those legacy records carry no `recordingId`, and groupOrphanRecordings offers
// each of them as its own single-fragment recording, so pre-upgrade audio stays recoverable.

const DB_NAME = "jarvis_voice";
const STORE = "clips";
const DB_VERSION = 1;

// A fragment written moments ago belongs to a recorder that is STILL RUNNING — most often ANOTHER
// TAB of the same app, mid-dictation, whose session id this tab has never seen (IndexedDB is
// shared per origin; the live-session exclusion below only knows about this tab). Offering that
// take as "recovery" is destructive, not helpful: Transcribe adopts it and DELETES its fragment
// keys — including index 0, the initialisation segment whose loss is the one case this design
// calls unrecoverable — while the other tab keeps writing fragments into the hole; Discard deletes
// it outright. So a recording only counts as an orphan once nothing has been written to it for
// 2× the recorder's timeslice: by then a live recorder would have flushed again.
export const LIVE_FRAGMENT_GRACE_MS = 30000;

function _idb() {
	try {
		return typeof indexedDB !== "undefined" ? indexedDB : null;
	} catch (e) {
		return null;
	}
}

function _openDb() {
	const idb = _idb();
	if (!idb) return Promise.resolve(null);
	return new Promise((resolve) => {
		let req;
		try {
			req = idb.open(DB_NAME, DB_VERSION);
		} catch (e) {
			resolve(null);
			return;
		}
		req.onupgradeneeded = () => {
			const db = req.result;
			if (!db.objectStoreNames.contains(STORE)) {
				// keyPath 'key' = `${recordingId}:${index}`. That prefix IS the index: every
				// per-recording op below runs a bound key range over it, so no secondary index
				// is created. (A database from the previous release carries an unused
				// `sessionId` index; leaving it costs nothing, and bumping DB_VERSION to drop
				// it would run an upgrade over stores that still hold un-recovered audio.)
				db.createObjectStore(STORE, { keyPath: "key" });
			}
		};
		req.onsuccess = () => resolve(req.result);
		req.onerror = () => resolve(null);
		req.onblocked = () => resolve(null);
	});
}

// Resolves TRUE only when the transaction actually COMMITTED (tx.oncomplete), FALSE on any
// failure path — error, abort, a throw building the tx, or IndexedDB being unavailable. The
// store observes putFragment()'s result to know whether that slice of audio is durably saved.
function _tx(mode, fn) {
	return _openDb().then((db) => {
		if (!db) return false; // IndexedDB unavailable (private mode / unsupported) — NOT durable
		return new Promise((resolve) => {
			// EVERY terminal path closes the connection. Closing only on success leaked one open
			// connection per failed transaction, and the failing path is exactly the one that
			// repeats: a quota failure retries each fragment MIRROR_PUT_ATTEMPTS times.
			const settle = (ok) => {
				try {
					db.close();
				} catch (e) {
					/* noop */
				}
				resolve(ok);
			};
			let store;
			try {
				const tx = db.transaction(STORE, mode);
				store = tx.objectStore(STORE);
				tx.oncomplete = () => settle(true);
				tx.onerror = () => settle(false);
				tx.onabort = () => settle(false);
			} catch (e) {
				settle(false);
				return;
			}
			try {
				fn(store);
			} catch (e) {
				/* the tx error/abort handler resolves */
			}
		});
	});
}

// The half-open key range covering every fragment of one recording: keys are
// `${recordingId}:${index}` and ";" is the character right after ":", so the bound range selects
// exactly this recording's rows. Returns null where IDBKeyRange isn't available, and the caller
// falls back to a full scan.
function _recordingRange(recordingId) {
	try {
		if (typeof IDBKeyRange === "undefined") return null;
		return IDBKeyRange.bound(`${recordingId}:`, `${recordingId};`, false, true);
	} catch (e) {
		return null;
	}
}

// The persisted record for one fragment. Split out so putFragment() and the pure,
// browser-free adoptionOps() below agree byte-for-byte on the shape and the key derivation.
//
// `durationS` is CUMULATIVE — the recording's length up to and including this fragment — so a
// crash-recovered take reports the audio that actually survived, not an optimistic total, and
// the final fragment carries the true total for a take that finished normally.
function _record(frag, sid, uid) {
	const idx = Number(frag.index) || 0;
	return {
		key: `${frag.recordingId}:${idx}`,
		sessionId: sid,
		recordingId: String(frag.recordingId),
		index: idx,
		userId: uid,
		// The conversation the take was SPOKEN in — recovery routes its transcript back
		// there, never into whatever chat happens to be open.
		conversationId: frag.conversationId != null ? frag.conversationId : null,
		blob: frag.blob,
		durationS: frag.durationS || 0,
		mimeType: frag.mimeType || (frag.blob && frag.blob.type) || "audio/webm",
		createdAt: Date.now(),
	};
}

// PURE, browser-free: the writes an ADOPTION performs when a prior-session recording is pulled
// into the live session. The recovered take is re-mirrored as ONE already-assembled fragment
// (concatenated fragments ARE one valid webm), and the prior-session fragment keys it
// supersedes are deleted — both in ONE transaction, so a crash or quota failure can never
// delete the old audio without the new copy landing. Exposed so the pairing is unit-testable
// without IndexedDB; the transactional atomicity itself is asserted by the browser QA kit.
export function adoptionOps(recording, sessionId, userId) {
	const sid = String(sessionId || "session");
	const uid = userId == null ? null : String(userId);
	const rec = _record(
		{
			recordingId: recording.recordingId,
			index: 0,
			blob: recording.blob,
			durationS: recording.durationS,
			mimeType: recording.mimeType,
			conversationId: recording.conversationId,
		},
		sid,
		uid
	);
	const ops = [{ type: "put", key: rec.key, rec }];
	for (const k of recording._adoptKeys || []) {
		if (k != null && k !== rec.key) ops.push({ type: "delete", key: String(k) });
	}
	return ops;
}

// A per-session mirror handed to the store. Every method resolves (never rejects);
// putFragment() and adopt() resolve a BOOLEAN durability signal (true = committed) that the
// store observes. deleteRecording() stays best-effort (a stale record is harmless; a dropped
// one is not). `userId` (the current Frappe user) is stamped on every record so reload
// recovery can be scoped to the SAME user — IndexedDB is per-origin, not per-login, so a
// shared browser profile must not offer user A's audio to user B.
export function createVoiceAudioMirror(sessionId, userId) {
	const sid = String(sessionId || "session");
	const uid = userId == null ? null : String(userId);
	return {
		sessionId: sid,
		userId: uid,
		// The mirror owns the recording-key namespace, so two sessions can never collide.
		recordingKey: (id) => `${sid}#${id}`,
		putFragment(frag) {
			const rec = _record(frag, sid, uid);
			return _tx("readwrite", (store) => store.put(rec)).catch(() => false);
		},
		// Atomic recovery adoption: the assembled copy lands and the superseded prior-session
		// fragments go, or neither happens.
		adopt(recording) {
			const ops = adoptionOps(recording, sid, uid);
			return _tx("readwrite", (store) => {
				for (const op of ops) {
					if (op.type === "put") store.put(op.rec);
					else if (op.type === "delete") store.delete(op.key);
				}
			}).catch(() => false);
		},
		// Drop every fragment of one recording (its transcript is durable, or the user
		// discarded it). Cursor-scanned rather than key-guessed — a take whose fragment count
		// the caller no longer knows must still clear completely — but over this recording's
		// KEY RANGE only, and on keys rather than values, so a discard never materialises every
		// audio blob in the store.
		deleteRecording(recordingId) {
			const want = String(recordingId);
			const range = _recordingRange(want);
			return _tx("readwrite", (store) => {
				const cur = range ? store.openKeyCursor(range) : store.openCursor();
				cur.onsuccess = () => {
					const c = cur.result;
					if (!c) return;
					if (range) store.delete(c.key);
					else if (c.value && String(c.value.recordingId) === want) c.delete();
					c.continue();
				};
			}).catch(() => undefined);
		},
		// Re-point a recording's stored conversation without rewriting its blobs: the
		// new-chat sentinel is promoted to a real id mid-take, and a reload must recover the
		// audio into the conversation it actually belongs to.
		reassignConversation(recordingId, conversationId) {
			const want = String(recordingId);
			const to = conversationId != null ? conversationId : null;
			const range = _recordingRange(want);
			return _tx("readwrite", (store) => {
				const cur = range ? store.openCursor(range) : store.openCursor();
				cur.onsuccess = () => {
					const c = cur.result;
					if (!c) return;
					const v = c.value;
					if (v && String(v.recordingId) === want && v.conversationId !== to) {
						c.update({ ...v, conversationId: to });
					}
					c.continue();
				};
			}).catch(() => undefined);
		},
	};
}

// The recording a fragment belongs to. A legacy per-clip record carries no recordingId, so it
// stands alone under its own key — exactly as groupOrphanRecordings groups it.
function _fragRecordingId(v) {
	return v.recordingId == null ? `legacy:${v.key}` : String(v.recordingId);
}

// PURE, node-testable orphan filter. Keeps only fragments that (a) belong to a PRIOR session
// (not the live one), (b) belong to the SAME Frappe user — the cross-user safety gate — and
// (c) belong to a recording nothing has written to for `liveGraceMs` (see
// LIVE_FRAGMENT_GRACE_MS: a still-warm recording is another tab's LIVE take, not an orphan). A
// record whose userId doesn't exactly match (including a legacy record with no userId when a
// user IS logged in, or vice-versa) is HIDDEN, never offered to whoever happens to be logged in.
//
// `now` / `liveGraceMs` are injectable for tests; `liveGraceMs: 0` disables the age gate.
export function filterOrphanFragments(rows, opts = {}) {
	const norm = (u) => (u == null ? null : String(u));
	const want = norm(opts.userId);
	const excludeSessionId = opts.excludeSessionId;
	const now = typeof opts.now === "number" ? opts.now : Date.now();
	const graceMs = opts.liveGraceMs == null ? LIVE_FRAGMENT_GRACE_MS : opts.liveGraceMs;
	const mine = (rows || []).filter((v) => {
		if (!v) return false;
		if (excludeSessionId && v.sessionId === excludeSessionId) return false;
		if (norm(v.userId) !== want) return false; // strict same-user; both-null is OK
		return true;
	});
	if (!(graceMs > 0)) return mine;
	// Age is judged per RECORDING, not per fragment: fragment 0 of a 90 s live take is minutes
	// old, and dropping only the young rows would offer a take whose head is missing — the worst
	// of both outcomes.
	const newest = new Map();
	for (const v of mine) {
		const rid = _fragRecordingId(v);
		const at = v.createdAt || 0;
		if (at > (newest.get(rid) || 0)) newest.set(rid, at);
	}
	return mine.filter((v) => now - (newest.get(_fragRecordingId(v)) || 0) >= graceMs);
}

// PURE, node-testable: rebuild RECORDINGS out of loose persisted fragments.
//
//   * fragments are grouped by `recordingId` and ordered by `index` — spoken order restored
//     exactly, never guessed from timestamps (each fragment gets its own Date.now()).
//   * a record with no `recordingId` is a LEGACY per-clip mirror entry from the previous
//     release: it is a complete, standalone webm, so it becomes its own single-fragment
//     recording rather than being dropped on upgrade.
//   * `complete` is false when fragment 0 is missing. Every later fragment is a bare cluster
//     continuation with no initialisation segment, so the take cannot be rebuilt into playable
//     audio — the UI must then offer the raw bytes + Discard and never a silent loss.
//   * `durationS` is the largest cumulative duration any surviving fragment recorded, i.e. how
//     much audio actually exists — an honest number for a crash-truncated take.
//
// ORDER. Sessions come newest-first (by the newest write anywhere in that session). WITHIN a
// session, LEGACY clips are ordered by their `seq` ASCENDING — spoken order. That is not
// cosmetic: each legacy clip was a separate ~15 s slice of one dictation, each clip's row got its
// own Date.now() at write time, so a flat newest-first sort hands a user their sentence
// BACKWARDS, and clicking Transcribe down the banner types it in that order. (They are never
// concatenated: a legacy clip is a standalone webm with its own EBML header, so joining them
// would not decode.) Modern recordings within a session stay newest-first — each is a whole
// dictation, and the most recent one is the one being looked for.
export function groupOrphanRecordings(fragments) {
	const groups = new Map();
	// sessionId -> the newest write seen anywhere in that session
	const sessionRecency = new Map();
	for (const v of fragments || []) {
		if (!v) continue;
		const legacy = v.recordingId == null;
		const rid = legacy ? `legacy:${v.key}` : String(v.recordingId);
		const sid = v.sessionId == null ? null : String(v.sessionId);
		if ((v.createdAt || 0) > (sessionRecency.get(sid) || 0))
			sessionRecency.set(sid, v.createdAt || 0);
		let g = groups.get(rid);
		if (!g) {
			g = {
				recordingId: rid,
				sessionId: sid,
				legacy,
				// Spoken position of a legacy clip within its session (0 for a modern recording,
				// whose fragments carry `index` instead). The previous release always stamped
				// `seq`; falling back to the write time keeps the ordering right for anything
				// that somehow lacks it, since within ONE session both ascend together.
				seq: legacy ? (v.seq != null ? Number(v.seq) || 0 : v.createdAt || 0) : 0,
				conversationId: v.conversationId != null ? v.conversationId : null,
				mimeType: v.mimeType || "audio/webm",
				durationS: 0,
				createdAt: 0,
				keys: [],
				fragments: [],
			};
			groups.set(rid, g);
		}
		const idx = legacy ? 0 : Number(v.index) || 0;
		g.fragments.push({ index: idx, blob: v.blob, key: v.key });
		g.keys.push(v.key);
		if ((v.durationS || 0) > g.durationS) g.durationS = v.durationS || 0;
		if ((v.createdAt || 0) > g.createdAt) g.createdAt = v.createdAt || 0;
		// The routing scope + mime belong to the take, not the fragment: read them off the
		// EARLIEST fragment so a later re-put can't rewrite where the transcript goes.
		if (idx === 0) {
			g.conversationId = v.conversationId != null ? v.conversationId : null;
			g.mimeType = v.mimeType || g.mimeType;
		}
	}
	return Array.from(groups.values())
		.map((g) => {
			g.fragments.sort((a, b) => a.index - b.index);
			g.keys = g.fragments.map((f) => f.key);
			g.complete = g.fragments.length > 0 && g.fragments[0].index === 0;
			return g;
		})
		.sort((a, b) => {
			const sa = sessionRecency.get(a.sessionId) || 0;
			const sb = sessionRecency.get(b.sessionId) || 0;
			if (sa !== sb) return sb - sa; // newest session first
			if (a.legacy && b.legacy) return a.seq - b.seq; // …then SPOKEN order
			return b.createdAt - a.createdAt;
		});
}

// Read back every mirrored recording left by PRIOR sessions belonging to `userId` (anything
// still in the store when the composer mounts is un-transcribed leftover audio). Returns the
// groupOrphanRecordings() shape; `blob` is assembled by the caller from `fragments`.
export function listOrphanRecordings(excludeSessionId, userId) {
	const idb = _idb();
	if (!idb) return Promise.resolve([]);
	return _openDb().then((db) => {
		if (!db) return [];
		return new Promise((resolve) => {
			const out = [];
			// Close on EVERY terminal path, success or not — see _tx.
			const settle = (v) => {
				try {
					db.close();
				} catch (e) {
					/* noop */
				}
				resolve(v);
			};
			try {
				const tx = db.transaction(STORE, "readonly");
				const store = tx.objectStore(STORE);
				const cur = store.openCursor();
				cur.onsuccess = () => {
					const c = cur.result;
					if (c) {
						out.push(c.value);
						c.continue();
					}
				};
				tx.oncomplete = () =>
					settle(
						groupOrphanRecordings(
							filterOrphanFragments(out, { userId, excludeSessionId })
						)
					);
				tx.onerror = () => settle([]);
				tx.onabort = () => settle([]);
			} catch (e) {
				settle([]);
			}
		});
	});
}

// Delete an offered recovery recording outright (the user downloaded it or discarded it).
export function deleteOrphanRecording(keys) {
	const list = (keys || []).filter((k) => k != null);
	if (!list.length) return Promise.resolve(undefined);
	return _tx("readwrite", (store) => {
		for (const k of list) store.delete(String(k));
	}).catch(() => undefined);
}
