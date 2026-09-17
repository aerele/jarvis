import { test } from "node:test";
import assert from "node:assert/strict";

import {
	pillFor,
	bannerToneFor,
	bannerShouldShow,
	readSnooze,
	writeSnooze,
	SNOOZE_KEY,
} from "./releaseNudge.js";

const DAY = 86400000;

// ---- pillFor: tone/label derivation --------------------------------------

test("pillFor: no notice or no target version -> hidden (never a false 'on the latest')", () => {
	assert.deepEqual(pillFor(null), { show: false });
	assert.deepEqual(pillFor(undefined), { show: false });
	assert.deepEqual(pillFor({}), { show: false });
	assert.deepEqual(pillFor({ version: "" }), { show: false });
});

test("pillFor: current (tier none) -> hidden (no positive 'on the latest' badge)", () => {
	// Being up to date is the quiet default, not a pill: tier "none" renders
	// nothing, same as no notice at all.
	assert.deepEqual(pillFor({ version: "16.4.0", tier: "none", behind: 0 }), { show: false });
	assert.deepEqual(pillFor({ version: "16.4.0", tier: "none" }), { show: false });
});

test("pillFor: soft -> amber; behind>=1 shows the count, behind<1 falls back", () => {
	assert.deepEqual(pillFor({ version: "16.4.0", tier: "soft", behind: 2 }), {
		show: true,
		tone: "amber",
		label: "2 versions behind",
	});
	// behind==1 is singular: "1 version behind", never "1 versions behind".
	assert.equal(
		pillFor({ version: "16.4.0", tier: "soft", behind: 1 }).label,
		"1 version behind"
	);
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
	// behind==1 is singular: "1 version behind", never "1 versions behind".
	assert.equal(
		pillFor({ version: "16.4.0", tier: "hard", behind: 1 }).label,
		"1 version behind"
	);
	assert.equal(pillFor({ version: "16.4.0", tier: "hard", behind: 0 }).label, "Update required");
});

test("pillFor: severe -> red (non-blocking); behind>=1 shows the count, behind<1 falls back", () => {
	assert.deepEqual(pillFor({ version: "16.4.0", tier: "severe", behind: 4 }), {
		show: true,
		tone: "red",
		label: "4 versions behind",
	});
	// behind==1 is singular: "1 version behind", never "1 versions behind".
	assert.equal(
		pillFor({ version: "16.4.0", tier: "severe", behind: 1 }).label,
		"1 version behind"
	);
	// severe's fallback is "Update available" (NOT "Update required" - that stays
	// hard-only, since severe never blocks chat).
	assert.equal(
		pillFor({ version: "16.4.0", tier: "severe", behind: 0 }).label,
		"Update available"
	);
	assert.equal(pillFor({ version: "16.4.0", tier: "severe" }).label, "Update available");
});

test("pillFor: an unknown-but-versioned future tier -> amber, NEVER a false green", () => {
	// Forward-compat: a tier this build doesn't recognise still carries a version,
	// so it's a real nudge - amber, not the green "on the latest" all-clear.
	assert.deepEqual(pillFor({ version: "16.4.0", tier: "whoa", behind: 3 }), {
		show: true,
		tone: "amber",
		label: "3 versions behind",
	});
	// ...and with no behind it still falls back to amber "Update available", never green.
	const p = pillFor({ version: "16.4.0", tier: "whoa" });
	assert.equal(p.tone, "amber");
	assert.equal(p.label, "Update available");
	assert.notEqual(p.tone, "green");
});

test("pillFor: the pill only nudges - none is hidden, every other versioned tier is amber/red", () => {
	// Regression pin: "none" (up to date) shows no pill at all; green is never
	// produced. Every other versioned tier is a nudge (amber/red), never green.
	assert.equal(pillFor({ version: "16.4.0", tier: "none" }).show, false);
	assert.equal(pillFor({ version: "16.4.0", tier: "soft" }).tone, "amber");
	assert.equal(pillFor({ version: "16.4.0", tier: "severe" }).tone, "red");
	assert.equal(pillFor({ version: "16.4.0", tier: "hard" }).tone, "red");
	assert.equal(pillFor({ version: "16.4.0", tier: "future-thing" }).tone, "amber");
});

// ---- bannerToneFor: single-sourced from pillFor --------------------------

test("bannerToneFor: returns pillFor's tone (amber/red), single-sourced", () => {
	// "none" has no pill now, so no tone (undefined) - but bannerShouldShow keeps
	// it from ever reaching here.
	assert.equal(bannerToneFor({ version: "16.4.0", tier: "none" }), undefined);
	assert.equal(bannerToneFor({ version: "16.4.0", tier: "soft" }), "amber");
	assert.equal(bannerToneFor({ version: "16.4.0", tier: "severe" }), "red");
	assert.equal(bannerToneFor({ version: "16.4.0", tier: "hard" }), "red");
	// An unknown-but-versioned tier -> amber, matching the pill.
	assert.equal(bannerToneFor({ version: "16.4.0", tier: "whoa" }), "amber");
});

// ---- bannerShouldShow ----------------------------------------------------

test("bannerShouldShow: soft + no snooze -> true", () => {
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "soft" }, 1000, null), true);
});

test("bannerShouldShow: severe + no snooze -> true (non-blocking nudge, same as soft)", () => {
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "severe" }, 1000, null), true);
});

test("bannerShouldShow: any versioned tier except none/hard shows - even an unknown one", () => {
	// Forward-compat mirror of pillFor: none (all-clear) and hard (full-page gate)
	// never banner; soft, severe AND any unknown-but-versioned future tier do.
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "none" }, 1000, null), false);
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "hard" }, 1000, null), false);
	// An unrecognised future tier with a version -> banner (amber), never dropped.
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "whoa" }, 1000, null), true);
	// A tier without a version can never banner, whatever it is.
	assert.equal(bannerShouldShow({ version: "", tier: "soft" }, 1000, null), false);
	assert.equal(bannerShouldShow({ version: "", tier: "severe" }, 1000, null), false);
	assert.equal(bannerShouldShow({ version: "", tier: "whoa" }, 1000, null), false);
	assert.equal(bannerShouldShow(null, 1000, null), false);
});

test("bannerShouldShow: an unknown-versioned tier still respects an unexpired snooze", () => {
	// The forward-compat "unknown shows" must not bypass snooze - same rule as soft/severe.
	const snooze = { version: "16.4.0", until: 5000 };
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "whoa" }, 1000, snooze), false);
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "whoa" }, 9000, snooze), true);
});

test("bannerShouldShow: same version, unexpired snooze -> false", () => {
	const snooze = { version: "16.4.0", until: 5000 };
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "soft" }, 1000, snooze), false);
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "severe" }, 1000, snooze), false);
});

test("bannerShouldShow: same version, expired snooze -> true", () => {
	const snooze = { version: "16.4.0", until: 5000 };
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "soft" }, 9000, snooze), true);
	assert.equal(bannerShouldShow({ version: "16.4.0", tier: "severe" }, 9000, snooze), true);
});

test("bannerShouldShow: a newer target version supersedes an unexpired snooze -> true", () => {
	const snooze = { version: "16.4.0", until: 999999 };
	assert.equal(bannerShouldShow({ version: "16.5.0", tier: "soft" }, 1000, snooze), true);
	assert.equal(bannerShouldShow({ version: "16.5.0", tier: "severe" }, 1000, snooze), true);
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
		writeSnooze({ version: "16.4.0", banner_interval_days: 7 }, 1_000_000, "user@a");
		const snooze = readSnooze("user@a");
		assert.equal(snooze.version, "16.4.0");
		assert.equal(snooze.until, 1_000_000 + 7 * DAY);
	} finally {
		delete globalThis.localStorage;
	}
});

test("writeSnooze: missing/zero interval defaults to 7 days", () => {
	installLocalStorage();
	try {
		writeSnooze({ version: "16.4.0" }, 0, "user@a");
		assert.equal(readSnooze("user@a").until, 7 * DAY);
	} finally {
		delete globalThis.localStorage;
	}
});

test("snooze is per-user: user A's write doesn't suppress user B's banner", () => {
	const store = installLocalStorage();
	try {
		writeSnooze({ version: "16.4.0", banner_interval_days: 7 }, 1_000_000, "user@a");
		// A is snoozed...
		assert.ok(readSnooze("user@a"));
		// ...but B, who never dismissed, reads no snooze -> the banner still shows.
		assert.equal(readSnooze("user@b"), null);
		// The two keys are genuinely distinct in storage.
		assert.notEqual(`${SNOOZE_KEY}:user@a`, `${SNOOZE_KEY}:user@b`);
		assert.ok(store.has(`${SNOOZE_KEY}:user@a`));
		assert.ok(!store.has(`${SNOOZE_KEY}:user@b`));
	} finally {
		delete globalThis.localStorage;
	}
});

test("missing userId falls back to a stable 'anon' key (round-trips + matches undefined)", () => {
	const store = installLocalStorage();
	try {
		writeSnooze({ version: "16.4.0", banner_interval_days: 7 }, 1_000_000, undefined);
		// The write lands under the literal ":anon" bucket...
		assert.ok(store.has(`${SNOOZE_KEY}:anon`));
		// ...and a later read with no userId finds the very same snooze (stable key).
		assert.equal(readSnooze().version, "16.4.0");
		assert.equal(readSnooze(undefined).version, "16.4.0");
	} finally {
		delete globalThis.localStorage;
	}
});

test("readSnooze: malformed JSON -> null, never throws", () => {
	installLocalStorage();
	try {
		globalThis.localStorage.setItem(`${SNOOZE_KEY}:user@a`, "{not json");
		assert.equal(readSnooze("user@a"), null);
	} finally {
		delete globalThis.localStorage;
	}
});

test("readSnooze/writeSnooze: localStorage absent -> null / no-op, no throw", () => {
	assert.equal(typeof globalThis.localStorage, "undefined");
	assert.equal(readSnooze("user@a"), null);
	assert.doesNotThrow(() => writeSnooze({ version: "16.4.0" }, 1000, "user@a"));
});
