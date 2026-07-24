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
import { filterOrphans } from "./clipMirror.js";

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
		rec({ sessionId: "old", userId: "u", seq: 1, createdAt: 100 }),
		rec({ sessionId: "old", userId: "u", seq: 0, createdAt: 100 }),
		rec({ sessionId: "new", userId: "u", seq: 0, createdAt: 500 }),
	];
	const out = filterOrphans(clips, { userId: "u" });
	assert.deepEqual(
		out.map((c) => `${c.sessionId}:${c.seq}`),
		["new:0", "old:0", "old:1"],
		"newest session first, ascending seq within a session"
	);
});

test("filterOrphans tolerates empty / missing input", () => {
	assert.deepEqual(filterOrphans(undefined, { userId: "u" }), []);
	assert.deepEqual(filterOrphans([], { userId: "u" }), []);
});
