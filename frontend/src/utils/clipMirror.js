// clipMirror — the crash-safety store behind the "audio NEVER lost" invariant. Each
// recorded clip is mirrored to IndexedDB (binary blobs, not the ~5 MB localStorage
// string quota) the instant it is enqueued, and deleted only once its transcript has
// been committed to the composer. A failed clip is RETAINED so it survives even a tab
// crash / accidental reload, from which listOrphanClips() offers recovery.
//
// The in-memory voiceChunkQueue is the source of truth; this is a best-effort mirror.
// Every op is wrapped so an IndexedDB hiccup (private mode, quota, unsupported) NEVER
// throws into the queue — it just degrades crash-safety, never drops live audio.
//
// The queue calls mirror.put(clip) / mirror.delete(seq) with the CURRENT session's
// seqs; createClipMirror(sessionId) closes over the session so the DB key is
// `${sessionId}:${seq}`. listOrphanClips() (a static read) returns clips left by
// PRIOR sessions — the recovery source after a reload.

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
				// keyPath 'key' = `${sessionId}:${seq}`; a `sessionId` index lets a
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

function _tx(mode, fn) {
	return _openDb().then((db) => {
		if (!db) return undefined;
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
					resolve();
				};
				tx.onerror = () => resolve(undefined);
				tx.onabort = () => resolve(undefined);
			} catch (e) {
				resolve(undefined);
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

// A per-session mirror handed to the queue. Methods return promises but the queue
// treats them as fire-and-forget (best-effort), so a rejection is impossible here —
// every path resolves. `userId` (the current Frappe user) is stamped on every record
// so reload recovery can be scoped to the SAME user — IndexedDB is per-origin, not
// per-login, so a shared browser profile must not offer user A's audio to user B.
export function createClipMirror(sessionId, userId) {
	const sid = String(sessionId || "session");
	const uid = userId == null ? null : String(userId);
	const key = (seq) => `${sid}:${seq}`;
	return {
		sessionId: sid,
		userId: uid,
		put(clip) {
			// Store the blob and just enough metadata to recover + route it later.
			const rec = {
				key: key(clip.seq),
				sessionId: sid,
				userId: uid,
				// The conversation the clip was SPOKEN in — recovery routes text back
				// here, never dumping it into whatever chat happens to be open.
				conversationId: clip.conversationId != null ? clip.conversationId : null,
				seq: clip.seq,
				blob: clip.blob,
				durationS: clip.durationS || 0,
				mimeType: clip.mimeType || (clip.blob && clip.blob.type) || "audio/webm",
				createdAt: Date.now(),
			};
			return _tx("readwrite", (store) => store.put(rec)).catch(() => undefined);
		},
		delete(seq) {
			return _tx("readwrite", (store) => store.delete(key(seq))).catch(() => undefined);
		},
		clear() {
			// Drop every clip this session owns (clean teardown once all committed).
			return _tx("readwrite", (store) => {
				try {
					const idx = store.index("sessionId");
					const cur = idx.openCursor(IDBKeyRange.only(sid));
					cur.onsuccess = () => {
						const c = cur.result;
						if (c) {
							c.delete();
							c.continue();
						}
					};
				} catch (e) {
					/* best-effort */
				}
			}).catch(() => undefined);
		},
	};
}

// PURE, node-testable orphan filter (the browser-free heart of listOrphanClips). Keeps
// only clips that (a) belong to a PRIOR session (not the live one) and (b) belong to the
// SAME Frappe user — the cross-user safety gate (VAR-4). A record whose userId doesn't
// exactly match (including a legacy record with no userId when a user IS logged in, or
// vice-versa) is HIDDEN, never offered to whoever happens to be logged in. Sorted newest
// session first, then spoken order within a session.
export function filterOrphans(clips, opts = {}) {
	const norm = (u) => (u == null ? null : String(u));
	const want = norm(opts.userId);
	const excludeSessionId = opts.excludeSessionId;
	return (clips || [])
		.filter((v) => {
			if (excludeSessionId && v.sessionId === excludeSessionId) return false;
			if (norm(v.userId) !== want) return false; // strict same-user; both-null is OK
			return true;
		})
		.sort((a, b) => b.createdAt - a.createdAt || a.seq - b.seq);
}

// Read back every mirrored clip from PRIOR sessions belonging to `userId` (anything
// still in the store when the composer mounts is un-transcribed leftover audio). Returns
// [{ key, sessionId, userId, conversationId, seq, blob, durationS, mimeType, createdAt }].
export function listOrphanClips(excludeSessionId, userId) {
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
					resolve(filterOrphans(out, { userId, excludeSessionId }));
				};
				tx.onerror = () => resolve([]);
				tx.onabort = () => resolve([]);
			} catch (e) {
				resolve([]);
			}
		});
	});
}

// Delete a specific orphan by its full key (after the user recovers or downloads it).
export function deleteOrphanClip(fullKey) {
	return _tx("readwrite", (store) => store.delete(fullKey)).catch(() => undefined);
}
