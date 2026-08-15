import { test } from "node:test";
import assert from "node:assert/strict";
import { formatDuration, parseTs, relativeTime, spanBetween } from "./time.js";

// Regression guard for the bug the module was written to fix: Frappe returns
// "YYYY-MM-DD HH:MM:SS" and Safari refuses that form, so every timestamp in the
// PWA read "Invalid Date" on iOS until parseTs normalised the space to a T.
test("parseTs: accepts Frappe's space-separated datetime", () => {
	const spaced = parseTs("2026-07-13 14:02:11");
	const iso = parseTs("2026-07-13T14:02:11");
	assert.ok(!Number.isNaN(spaced), "space form must parse");
	assert.equal(spaced, iso, "space form and T form must be the same instant");
});

test("parseTs: empty and unparseable values give NaN, never a wrong date", () => {
	assert.ok(Number.isNaN(parseTs("")));
	assert.ok(Number.isNaN(parseTs(null)));
	assert.ok(Number.isNaN(parseTs(undefined)));
	assert.ok(Number.isNaN(parseTs("not a date")));
});

test("relativeTime: unparseable input renders nothing rather than 'Invalid Date'", () => {
	assert.equal(relativeTime(""), "");
	assert.equal(relativeTime("not a date"), "");
});

test("relativeTime: buckets by seconds, minutes, hours, days", () => {
	const ago = (secs) => new Date(Date.now() - secs * 1000).toISOString();
	assert.equal(relativeTime(ago(5)), "just now");
	assert.equal(relativeTime(ago(59)), "just now");
	assert.equal(relativeTime(ago(60)), "1m ago");
	assert.equal(relativeTime(ago(59 * 60)), "59m ago");
	assert.equal(relativeTime(ago(60 * 60)), "1h ago");
	assert.equal(relativeTime(ago(23 * 3600)), "23h ago");
	assert.equal(relativeTime(ago(24 * 3600)), "1d ago");
	assert.equal(relativeTime(ago(6 * 86400)), "6d ago");
});

test("relativeTime: a future timestamp clamps to 'just now', never negative", () => {
	const ahead = new Date(Date.now() + 60_000).toISOString();
	assert.equal(relativeTime(ahead), "just now");
});

test("relativeTime: beyond a week falls back to a calendar date", () => {
	const old = new Date(Date.now() - 30 * 86400 * 1000).toISOString();
	const out = relativeTime(old);
	assert.ok(!out.endsWith("ago"), `expected a calendar date, got ${out}`);
	assert.notEqual(out, "");
});

test("formatDuration: seconds under a minute, m+s above", () => {
	assert.equal(formatDuration(0), "0s");
	assert.equal(formatDuration(8), "8s");
	assert.equal(formatDuration(59), "59s");
	assert.equal(formatDuration(60), "1m 0s");
	assert.equal(formatDuration(72), "1m 12s");
	assert.equal(formatDuration(-5), "0s", "negative input must not print a negative duration");
});

test("spanBetween: too short to be interesting returns nothing", () => {
	assert.equal(spanBetween("2026-07-13 14:02:11", "2026-07-13 14:02:12"), "");
});

test("spanBetween: a real span formats, and a backwards span is refused", () => {
	assert.equal(spanBetween("2026-07-13 14:02:11", "2026-07-13 14:02:19"), "8s");
	assert.equal(spanBetween("2026-07-13 14:02:19", "2026-07-13 14:02:11"), "");
	assert.equal(spanBetween("", "2026-07-13 14:02:19"), "");
});
