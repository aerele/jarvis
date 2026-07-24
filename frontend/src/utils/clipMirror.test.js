// Real executable test for the clip-mirror's cross-user recovery gate (filterOrphans,
// the browser-free heart of listOrphanClips). Plain node built-ins — no framework —
// like eventFence.test.js / voiceChunkQueue.test.js. Run directly
// (`node --test clipMirror.test.js`) or via the python suite:
// jarvis/tests/test_clip_mirror_client.py subprocess-runs this so the VAR-4 cross-account
// safety gate is enforced by every CI run. `node --test` exits non-zero on any failure.
//
// IndexedDB itself is not exercised here (it needs a browser); the pure filter that
// decides WHICH persisted clips a logged-in user may recover is what carries the
// security-relevant logic, and that is fully testable.
import { test } from "node:test";
import assert from "node:assert/strict";
import { filterOrphans, adoptionOps } from "./clipMirror.js";

const rec = (o) => ({
	key: `${o.sessionId}:${o.seq}`,
	sessionId: o.sessionId,
	userId: o.userId,
	conversationId: o.conversationId ?? null,
	seq: o.seq,
	createdAt: o.createdAt,
});

// ── VAR-4: only the SAME Frappe user may recover their audio on a shared profile ─────
test("filterOrphans hides other users' clips (cross-account audio exposure is blocked)", () => {
	const clips = [
		rec({ sessionId: "sA", userId: "alice@x", seq: 0, createdAt: 100 }),
		rec({ sessionId: "sA", userId: "alice@x", seq: 1, createdAt: 100 }),
		rec({ sessionId: "sB", userId: "bob@x", seq: 0, createdAt: 200 }),
	];
	const forAlice = filterOrphans(clips, { userId: "alice@x" });
	assert.deepEqual(
		forAlice.map((c) => c.userId),
		["alice@x", "alice@x"],
		"Alice sees only Alice's clips — never Bob's audio"
	);
	const forBob = filterOrphans(clips, { userId: "bob@x" });
	assert.deepEqual(
		forBob.map((c) => c.sessionId),
		["sB"],
		"Bob sees only Bob's clips"
	);
});

test("filterOrphans excludes the live session and hides records with a mismatched/absent userId", () => {
	const clips = [
		rec({ sessionId: "live", userId: "alice@x", seq: 0, createdAt: 300 }), // current session
		rec({ sessionId: "old", userId: "alice@x", seq: 0, createdAt: 200 }), // recoverable
		rec({ sessionId: "legacy", userId: undefined, seq: 0, createdAt: 100 }), // no user stamp
	];
	const out = filterOrphans(clips, { userId: "alice@x", excludeSessionId: "live" });
	assert.deepEqual(
		out.map((c) => c.sessionId),
		["old"],
		"the live session is excluded; a legacy record with no userId is NOT offered to a logged-in user"
	);
});

test("filterOrphans sorts newest session first, then spoken order within a session", () => {
	const clips = [
		// INCREASING per-clip timestamps WITHIN the old session: seq 1 was spoken AFTER seq 0,
		// so it carries a LATER createdAt. A naive `createdAt DESC` sort would return seq 1
		// before seq 0 — reversing the dictation. Grouping + within-session seq ASC restores
		// it. (The prior version of this test masked the bug with identical timestamps —
		// finding 3.)
		rec({ sessionId: "old", userId: "u", seq: 1, createdAt: 200 }),
		rec({ sessionId: "old", userId: "u", seq: 0, createdAt: 100 }),
		rec({ sessionId: "new", userId: "u", seq: 0, createdAt: 500 }),
	];
	const out = filterOrphans(clips, { userId: "u" });
	assert.deepEqual(
		out.map((c) => `${c.sessionId}:${c.seq}`),
		["new:0", "old:0", "old:1"],
		"newest session first, ascending seq within a session (NOT reversed by per-clip timestamps)"
	);
});

test("filterOrphans restores spoken order for a long single-session take with realistic increasing timestamps", () => {
	// Five clips recorded ~15 s apart — createdAt strictly increases with seq. The OLD
	// `createdAt DESC` sort would hand them back 4,3,2,1,0 (the dictation, backwards).
	const clips = [
		rec({ sessionId: "s", userId: "u", seq: 0, createdAt: 1000 }),
		rec({ sessionId: "s", userId: "u", seq: 1, createdAt: 1015 }),
		rec({ sessionId: "s", userId: "u", seq: 2, createdAt: 1030 }),
		rec({ sessionId: "s", userId: "u", seq: 3, createdAt: 1045 }),
		rec({ sessionId: "s", userId: "u", seq: 4, createdAt: 1060 }),
	];
	// Shuffle the input to prove the SORT — not input order — decides.
	const shuffled = [clips[3], clips[0], clips[4], clips[1], clips[2]];
	const out = filterOrphans(shuffled, { userId: "u" });
	assert.deepEqual(
		out.map((c) => c.seq),
		[0, 1, 2, 3, 4],
		"ascending seq = spoken order, despite increasing timestamps and shuffled input"
	);
});

// ── VAR-… atomic recovery adoption (finding 5): the pure op-pairing that put() applies in
//    ONE IndexedDB transaction. (The transactional atomicity ITSELF — that a failed put
//    leaves the old key intact — is browser-only and asserted by the real-mic QA kit.) ─────
test("adoptionOps pairs the new-session PUT with the prior-session DELETE (atomic recovery adoption)", () => {
	// A recovered clip carrying `_adoptKey` (its prior-session key) yields BOTH a put of the
	// new-session record AND a delete of the old key — put() applies them in one transaction,
	// so a crash/quota failure can never delete the old audio without the new copy landing.
	const ops = adoptionOps(
		{ seq: 3, blob: "b", durationS: 15, conversationId: "chatA", _adoptKey: "oldsess:7" },
		"newsess",
		"alice@x"
	);
	assert.equal(ops.length, 2, "an adoption is exactly two ops");
	const put = ops.find((o) => o.type === "put");
	const del = ops.find((o) => o.type === "delete");
	assert.ok(put, "there is a put of the new record");
	assert.equal(
		put.rec.key,
		"newsess:3",
		"the new record is keyed under the LIVE session + fresh seq"
	);
	assert.equal(
		put.rec.conversationId,
		"chatA",
		"the spoken-in conversation rides through for routing"
	);
	assert.equal(put.rec.userId, "alice@x", "the user stamp is preserved for the cross-user gate");
	assert.ok(del, "there is a delete of the prior-session key");
	assert.equal(
		del.key,
		"oldsess:7",
		"…deleting exactly the old key, in the SAME op list (one tx)"
	);
});

test("adoptionOps emits ONLY a put for a fresh (non-recovered) clip, and never self-deletes", () => {
	const fresh = adoptionOps({ seq: 0, blob: "b", durationS: 15 }, "sess", null);
	assert.deepEqual(
		fresh.map((o) => o.type),
		["put"],
		"a fresh clip is a single put — nothing to adopt"
	);
	// A degenerate `_adoptKey` equal to the clip's own new key must NOT delete itself.
	const same = adoptionOps({ seq: 5, blob: "b", _adoptKey: "sess:5" }, "sess", null);
	assert.deepEqual(
		same.map((o) => o.type),
		["put"],
		"an _adoptKey equal to the new key is ignored (no self-delete)"
	);
});

test("filterOrphans tolerates empty / missing input", () => {
	assert.deepEqual(filterOrphans(undefined, { userId: "u" }), []);
	assert.deepEqual(filterOrphans([], { userId: "u" }), []);
});
