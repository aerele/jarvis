import { test } from "node:test";
import assert from "node:assert/strict";

import { pillFor, bannerShouldShow, readSnooze, writeSnooze, SNOOZE_KEY } from "./releaseNudge.js";

const DAY = 86400000;

// ---- pillFor: tone/label derivation --------------------------------------

test("pillFor: no notice or no target version -> hidden (never a false 'on the latest')", () => {
	assert.deepEqual(pillFor(null), { show: false });
	assert.deepEqual(pillFor(undefined), { show: false });
	assert.deepEqual(pillFor({}), { show: false });
	assert.deepEqual(pillFor({ version: "" }), { show: false });
});

test("pillFor: current (tier none, known version) -> green, branded label", () => {
	const p = pillFor({ version: "16.4.0", tier: "none", behind: 0 });
	assert.deepEqual(p, { show: true, tone: "green", label: "On the latest Jarvis" });
});

test("pillFor: agentName param brands the green label (not an import)", () => {
	assert.equal(pillFor({ version: "16.4.0", tier: "none" }, "Aida").label, "On the latest Aida");
});

test("pillFor: soft -> amber; behind>=1 shows the count, behind<1 falls back", () => {
	assert.deepEqual(pillFor({ version: "16.4.0", tier: "soft", behind: 2 }), {
		show: true,
		tone: "amber",
		label: "2 versions behind",
	});
	assert.equal(
		pillFor({ version: "16.4.0", tier: "soft", behind: 0 }).label,
		"Update available"
	);
	// Old CP: soft without a behind -> 0 -> the fallback label, still amber.
	assert.equal(pillFor({ version: "16.4.0", tier: "soft" }).label, "Update available");
});

test("pillFor: hard -> red; behind>=1 shows the count, behind<1 falls back", () => {
	assert.deepEqual(pillFor({ version: "16.4.0", tier: "hard", behind: 5 }), {
		show: true,
		tone: "red",
		label: "5 versions behind",
	});
	assert.equal(pillFor({ version: "16.4.0", tier: "hard", behind: 0 }).label, "Update required");
});

// ---- bannerShouldShow ----------------------------------------------------

test("bannerShouldShow: soft + no snooze -> true", () => {
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "soft" }, 1000, null), true);
});

test("bannerShouldShow: only the soft tier with a version can show", () => {
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "none" }, 1000, null), false);
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "hard" }, 1000, null), false);
	assert.equal(bannerShouldShow({ version: "", tier: "soft" }, 1000, null), false);
	assert.equal(bannerShouldShow(null, 1000, null), false);
});

test("bannerShouldShow: same version, unexpired snooze -> false", () => {
	const snooze = { version: "16.4.0", until: 5000 };
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "soft" }, 1000, snooze), false);
});

test("bannerShouldShow: same version, expired snooze -> true", () => {
	const snooze = { version: "16.4.0", until: 5000 };
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "soft" }, 9000, snooze), true);
});

test("bannerShouldShow: a newer target version supersedes an unexpired snooze -> true", () => {
	const snooze = { version: "16.4.0", until: 999999 };
	assert.equal(bannerShouldShow({ version: "16.5.0", tier: "soft" }, 1000, snooze), true);
});

// ---- snooze read/write ---------------------------------------------------

function installLocalStorage() {
	const store = new Map();
	globalThis.localStorage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, String(v)),
		removeItem: (k) => store.delete(k),
	};
	return store;
}

test("writeSnooze/readSnooze: round-trip with a present localStorage", () => {
	installLocalStorage();
	try {
		writeSnooze({ version: "16.4.0", banner_interval_days: 7 }, 1_000_000);
		const snooze = readSnooze();
		assert.equal(snooze.version, "16.4.0");
		assert.equal(snooze.until, 1_000_000 + 7 * DAY);
	} finally {
		delete globalThis.localStorage;
	}
});

test("writeSnooze: missing/zero interval defaults to 7 days", () => {
	installLocalStorage();
	try {
		writeSnooze({ version: "16.4.0" }, 0);
		assert.equal(readSnooze().until, 7 * DAY);
	} finally {
		delete globalThis.localStorage;
	}
});

test("readSnooze: malformed JSON -> null, never throws", () => {
	installLocalStorage();
	try {
		globalThis.localStorage.setItem(SNOOZE_KEY, "{not json");
		assert.equal(readSnooze(), null);
	} finally {
		delete globalThis.localStorage;
	}
});

test("readSnooze/writeSnooze: localStorage absent -> null / no-op, no throw", () => {
	assert.equal(typeof globalThis.localStorage, "undefined");
	assert.equal(readSnooze(), null);
	assert.doesNotThrow(() => writeSnooze({ version: "16.4.0" }, 1000));
});
