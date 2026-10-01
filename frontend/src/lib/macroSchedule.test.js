// frontend/src/lib/macroSchedule.test.js
import { test } from "node:test";
import assert from "node:assert/strict";
import { nextRunState, cannotScheduleReason, OVERDUE_AFTER_MS } from "./macroSchedule.js";

const NOW = 1_800_000_000_000;
const MIN = 60 * 1000;
const scheduled = { scheduleEnabled: true, enabled: true, nowMs: NOW };

// --- nextRunState: what the list's "Next run" cell should say -------------------
test("nextRunState: a future slot is upcoming", () => {
	assert.equal(nextRunState({ ...scheduled, nextRunMs: NOW + 5 * MIN }).kind, "upcoming");
});

test("nextRunState: a slot that just passed is due, never 'N minutes ago'", () => {
	// The cell used to print the relative time as-is, so a slot waiting for the next
	// sweep read "Next run: 5 minutes ago".
	assert.equal(nextRunState({ ...scheduled, nextRunMs: NOW - 5 * MIN }).kind, "due");
	assert.equal(nextRunState({ ...scheduled, nextRunMs: NOW }).kind, "due");
});

test("nextRunState: a slot still waiting after the grace period is overdue", () => {
	assert.equal(
		nextRunState({ ...scheduled, nextRunMs: NOW - OVERDUE_AFTER_MS }).kind,
		"overdue"
	);
	assert.equal(nextRunState({ ...scheduled, nextRunMs: NOW - 3 * 60 * MIN }).kind, "overdue");
});

test("nextRunState: a macro that is turned off will not run, whatever its slot says", () => {
	// The scheduler skips a disabled macro's slot without recording anything.
	const s = { ...scheduled, enabled: false };
	assert.equal(nextRunState({ ...s, nextRunMs: NOW + 5 * MIN }).kind, "off");
	assert.equal(nextRunState({ ...s, nextRunMs: NOW - 5 * MIN }).kind, "off");
});

test("nextRunState: no schedule, or no slot yet, shows nothing", () => {
	assert.equal(
		nextRunState({ ...scheduled, scheduleEnabled: false, nextRunMs: NOW }).kind,
		"none"
	);
	assert.equal(nextRunState({ ...scheduled, nextRunMs: null }).kind, "none");
});

// --- cannotScheduleReason: say up front that this account's schedule never runs ---
test("cannotScheduleReason: Administrator is told why, a named user is not blocked", () => {
	assert.match(cannotScheduleReason("Administrator"), /Administrator/);
	assert.equal(cannotScheduleReason("priya@example.com"), "");
	assert.equal(cannotScheduleReason(null), "");
});
