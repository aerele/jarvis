// Real executable test for the crash-safety mirror's PURE decisions (voiceAudioMirror.js): which
// persisted fragments a logged-in user may recover, how loose fragments are rebuilt back into
// recordings, and what an adoption writes. Plain node built-ins, no browser — IndexedDB itself is
// exercised by the browser QA kit; these are the rules that decide whether audio is offered back,
// rebuilt correctly, or silently lost.
//
// Run directly (`node --test voiceAudioMirror.test.js`) or via the python suite:
// jarvis/tests/test_voice_audio_mirror_client.py subprocess-runs it every CI run.
import { test } from "node:test";
import assert from "node:assert/strict";
import { adoptionOps, filterOrphanFragments, groupOrphanRecordings } from "./voiceAudioMirror.js";

const frag = (o) => ({
	key: `${o.recordingId}:${o.index}`,
	sessionId: o.sessionId || "sessA",
	recordingId: o.recordingId,
	index: o.index,
	userId: o.userId === undefined ? "alice@x.com" : o.userId,
	conversationId: o.conversationId === undefined ? "convA" : o.conversationId,
	blob: o.blob || `b${o.index}`,
	durationS: o.durationS || 0,
	mimeType: o.mimeType || "audio/webm;codecs=opus",
	createdAt: o.createdAt || 1000 + o.index,
});

// ── cross-user + live-session gate ───────────────────────────────────────────────────────────
test("filterOrphanFragments hides another user's audio (a shared browser profile must not leak it)", () => {
	const rows = [
		frag({ recordingId: "s1#0", index: 0, userId: "alice@x.com" }),
		frag({ recordingId: "s2#0", index: 0, userId: "bob@x.com" }),
		frag({ recordingId: "s3#0", index: 0, userId: null }),
	];
	const mine = filterOrphanFragments(rows, { userId: "alice@x.com" });
	assert.deepEqual(
		mine.map((r) => r.recordingId),
		["s1#0"],
		"IndexedDB is per-ORIGIN, not per-login — only the SAME user's audio may be offered back"
	);
	// A legacy record with no userId is hidden from a logged-in user, and vice versa.
	assert.deepEqual(
		filterOrphanFragments(rows, { userId: null }).map((r) => r.recordingId),
		["s3#0"]
	);
});

test("filterOrphanFragments excludes the LIVE session — its fragments are not orphans", () => {
	const rows = [
		frag({ recordingId: "live#0", index: 0, sessionId: "live" }),
		frag({ recordingId: "old#0", index: 0, sessionId: "old" }),
	];
	assert.deepEqual(
		filterOrphanFragments(rows, { userId: "alice@x.com", excludeSessionId: "live" }).map(
			(r) => r.recordingId
		),
		["old#0"]
	);
	assert.deepEqual(filterOrphanFragments(null, {}), [], "tolerates missing input");
});

// ── rebuilding recordings out of loose fragments ─────────────────────────────────────────────
test("groupOrphanRecordings rebuilds a full take in INDEX order, with the audio that actually exists", () => {
	// Deliberately shuffled, with realistic increasing timestamps: a createdAt sort would
	// reverse or interleave them, rebuilding the dictation wrong.
	const rows = [
		frag({ recordingId: "s1#0", index: 2, blob: "CCC", durationS: 41, createdAt: 3000 }),
		frag({ recordingId: "s1#0", index: 0, blob: "AAA", durationS: 15, createdAt: 1000 }),
		frag({ recordingId: "s1#0", index: 1, blob: "BBB", durationS: 30, createdAt: 2000 }),
	];
	const [take] = groupOrphanRecordings(rows);
	assert.equal(take.complete, true, "fragment 0 is present, so the take is decodable");
	assert.deepEqual(
		take.fragments.map((f) => f.blob),
		["AAA", "BBB", "CCC"],
		"concatenating THESE in THIS order is the recording — order is read from `index`, never time"
	);
	assert.deepEqual(
		take.keys,
		["s1#0:0", "s1#0:1", "s1#0:2"],
		"…and the keys follow the same order"
	);
	assert.equal(take.durationS, 41, "duration is the largest cumulative value that survived");
	assert.equal(take.conversationId, "convA", "the routing scope comes off the FIRST fragment");
	assert.equal(take.mimeType, "audio/webm;codecs=opus");
});

test("groupOrphanRecordings keeps a TRUNCATED take (a crash mid-recording) recoverable", () => {
	// The tab died before the third fragment landed: what exists is still a valid, shorter webm.
	const rows = [
		frag({ recordingId: "s1#0", index: 0, blob: "AAA", durationS: 15 }),
		frag({ recordingId: "s1#0", index: 1, blob: "BBB", durationS: 30 }),
	];
	const [take] = groupOrphanRecordings(rows);
	assert.equal(
		take.complete,
		true,
		"media containers truncate gracefully — recover what survived"
	);
	assert.equal(
		take.durationS,
		30,
		"and report the audio that actually exists, not an optimistic total"
	);
});

test("groupOrphanRecordings marks a take whose FIRST fragment is missing as un-rebuildable", () => {
	// Fragment 0 carries the container's initialisation segment; without it the rest is a bare
	// cluster stream that cannot be decoded. It must still be offered (Download + Discard) —
	// never silently dropped.
	const rows = [
		frag({ recordingId: "s1#0", index: 1, blob: "BBB", durationS: 30 }),
		frag({ recordingId: "s1#0", index: 2, blob: "CCC", durationS: 41 }),
	];
	const [take] = groupOrphanRecordings(rows);
	assert.equal(
		take.complete,
		false,
		"no fragment 0 → the take cannot be rebuilt into playable audio"
	);
	assert.equal(take.fragments.length, 2, "…but the raw bytes are still there to download");
	assert.deepEqual(take.keys, ["s1#0:1", "s1#0:2"], "…and still deletable as one unit");
});

test("groupOrphanRecordings separates recordings and orders them newest-first", () => {
	const rows = [
		frag({ recordingId: "s1#0", index: 0, createdAt: 1000 }),
		frag({ recordingId: "s1#1", index: 0, createdAt: 5000 }),
		frag({ recordingId: "s1#1", index: 1, createdAt: 6000 }),
	];
	const takes = groupOrphanRecordings(rows);
	assert.deepEqual(
		takes.map((t) => t.recordingId),
		["s1#1", "s1#0"],
		"the most recent take is offered first"
	);
	assert.equal(takes[0].fragments.length, 2, "each take keeps only ITS fragments");
	assert.equal(takes[1].fragments.length, 1);
});

test("groupOrphanRecordings still offers LEGACY per-clip records left by the previous release", () => {
	// The per-clip design wrote standalone webm clips with no recordingId/index. An upgrade must
	// not silently delete audio a user had not yet recovered.
	const legacy = {
		key: "oldsess:2",
		sessionId: "oldsess",
		userId: "alice@x.com",
		conversationId: "convZ",
		seq: 2,
		blob: "LEGACY",
		durationS: 15,
		mimeType: "audio/webm",
		createdAt: 900,
	};
	const [take] = groupOrphanRecordings([legacy]);
	assert.equal(
		take.complete,
		true,
		"a legacy clip is a complete, standalone recording on its own"
	);
	assert.deepEqual(
		take.fragments.map((f) => f.blob),
		["LEGACY"]
	);
	assert.deepEqual(take.keys, ["oldsess:2"]);
	assert.equal(take.conversationId, "convZ", "…and still routes to the chat it was spoken in");
	assert.equal(take.durationS, 15);
});

// ── adoption is ATOMIC: the new copy lands and the old keys go, or neither ────────────────────
test("adoptionOps pairs the adopted PUT with the DELETE of every superseded fragment", () => {
	const ops = adoptionOps(
		{
			recordingId: "new#0",
			blob: "AAABBB",
			durationS: 27,
			mimeType: "audio/webm",
			conversationId: "convA",
			_adoptKeys: ["old#3:0", "old#3:1"],
		},
		"sessNew",
		"alice@x.com"
	);
	assert.equal(ops[0].type, "put");
	assert.equal(ops[0].rec.key, "new#0:0", "the reassembled take is stored as ONE fragment");
	assert.equal(ops[0].rec.blob, "AAABBB");
	assert.equal(ops[0].rec.sessionId, "sessNew");
	assert.equal(ops[0].rec.userId, "alice@x.com", "re-stamped with the CURRENT user");
	assert.equal(ops[0].rec.conversationId, "convA");
	assert.deepEqual(
		ops.slice(1).map((o) => [o.type, o.key]),
		[
			["delete", "old#3:0"],
			["delete", "old#3:1"],
		],
		"the prior-session fragments are deleted in the SAME transaction as the new put — split " +
			"across two, a failure between them would delete the only durable copy"
	);
});

test("adoptionOps never self-deletes, and emits only a put when there is nothing to supersede", () => {
	const ops = adoptionOps(
		{ recordingId: "new#0", blob: "x", durationS: 3, _adoptKeys: ["new#0:0", null] },
		"sessNew",
		null
	);
	assert.deepEqual(
		ops.map((o) => o.type),
		["put"],
		"deleting its own key would erase the copy it just wrote"
	);
	assert.equal(
		ops[0].rec.userId,
		null,
		"a logged-out mirror stamps a null user, not the string 'null'"
	);
});
