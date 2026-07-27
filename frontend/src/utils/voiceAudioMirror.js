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
				// keyPath 'key' = `${recordingId}:${index}`; the `sessionId` index lets a
				// session clear its own leftovers without scanning the whole store.
				const os = db.createObjectStore(STORE, { keyPath: "key" });
				os.createIndex("sessionId", "sessionId", { unique: false });
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
			let store;
			try {
				const tx = db.transaction(STORE, mode);
				store = tx.objectStore(STORE);
				tx.oncomplete = () => {
					try {
						db.close();
					} catch (e) {
						/* noop */
					}
					resolve(true);
				};
				tx.onerror = () => resolve(false);
				tx.onabort = () => resolve(false);
			} catch (e) {
				resolve(false);
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
		// discarded it). Cursor-scanned rather than key-guessed: a take whose fragment count
		// the caller no longer knows must still clear completely.
		deleteRecording(recordingId) {
			const want = String(recordingId);
			return _tx("readwrite", (store) => {
				const cur = store.openCursor();
				cur.onsuccess = () => {
					const c = cur.result;
					if (!c) return;
					const v = c.value;
					if (v && String(v.recordingId) === want) c.delete();
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
			return _tx("readwrite", (store) => {
				const cur = store.openCursor();
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

// PURE, node-testable orphan filter. Keeps only fragments that (a) belong to a PRIOR session
// (not the live one) and (b) belong to the SAME Frappe user — the cross-user safety gate. A
// record whose userId doesn't exactly match (including a legacy record with no userId when a
// user IS logged in, or vice-versa) is HIDDEN, never offered to whoever happens to be logged in.
export function filterOrphanFragments(rows, opts = {}) {
	const norm = (u) => (u == null ? null : String(u));
	const want = norm(opts.userId);
	const excludeSessionId = opts.excludeSessionId;
	return (rows || []).filter((v) => {
		if (!v) return false;
		if (excludeSessionId && v.sessionId === excludeSessionId) return false;
		if (norm(v.userId) !== want) return false; // strict same-user; both-null is OK
		return true;
	});
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
// Recordings are returned newest-first (by the newest write in the group).
export function groupOrphanRecordings(fragments) {
	const groups = new Map();
	for (const v of fragments || []) {
		if (!v) continue;
		const legacy = v.recordingId == null;
		const rid = legacy ? `legacy:${v.key}` : String(v.recordingId);
		let g = groups.get(rid);
		if (!g) {
			g = {
				recordingId: rid,
				sessionId: v.sessionId == null ? null : String(v.sessionId),
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
		.sort((a, b) => b.createdAt - a.createdAt);
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
				tx.oncomplete = () => {
					try {
						db.close();
					} catch (e) {
						/* noop */
					}
					resolve(
						groupOrphanRecordings(
							filterOrphanFragments(out, { userId, excludeSessionId })
						)
					);
				};
				tx.onerror = () => resolve([]);
				tx.onabort = () => resolve([]);
			} catch (e) {
				resolve([]);
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
