// frontend/src/lib/macroSchedule.test.js
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	nextRunState,
	nextRunCell,
	cannotScheduleReason,
	OVERDUE_AFTER_MS,
} from "./macroSchedule.js";

const NOW = 1_800_000_000_000;
const MIN = 60 * 1000;
const scheduled = { scheduleEnabled: true, enabled: true, nowMs: NOW };

// --- nextRunState: which state the slot is in -----------------------------------
test("nextRunState: a future slot is upcoming", () => {
	assert.equal(nextRunState({ ...scheduled, nextRunMs: NOW + 5 * MIN }).kind, "upcoming");
});

test("nextRunState: a slot that just passed is due, never 'N minutes ago'", () => {
	// The cell used to print the relative time as-is, so a slot waiting for the next
	// sweep read "Next run: 5 minutes ago".
	assert.equal(nextRunState({ ...scheduled, nextRunMs: NOW - 5 * MIN }).kind, "due");
	assert.equal(nextRunState({ ...scheduled, nextRunMs: NOW }).kind, "due");
});

test("nextRunState: a healthy slot on an HOURLY sweep is still only due", () => {
	// Before the five-minute sweep is live on a site (it needs a migrate), a 10:15
	// slot legitimately waits until 11:00. Calling that overdue would be a false alarm.
	assert.equal(nextRunState({ ...scheduled, nextRunMs: NOW - 59 * MIN }).kind, "due");
});

test("nextRunState: a slot still waiting past any sweep's reach is overdue", () => {
	assert.equal(
		nextRunState({ ...scheduled, nextRunMs: NOW - OVERDUE_AFTER_MS }).kind,
		"overdue"
	);
	assert.equal(nextRunState({ ...scheduled, nextRunMs: NOW - 3 * 60 * MIN }).kind, "overdue");
	assert.ok(OVERDUE_AFTER_MS > 60 * MIN, "must outlast one hourly sweep");
});

test("nextRunState: a macro that is turned off will not run, whatever its slot says", () => {
	// The scheduler skips a disabled macro's slot without recording anything.
	const s = { ...scheduled, enabled: false };
	assert.equal(nextRunState({ ...s, nextRunMs: NOW + 5 * MIN }).kind, "off");
	assert.equal(nextRunState({ ...s, nextRunMs: NOW - 5 * MIN }).kind, "off");
});

test("nextRunState: no schedule, no slot, or an unreadable slot shows nothing", () => {
	assert.equal(
		nextRunState({ ...scheduled, scheduleEnabled: false, nextRunMs: NOW }).kind,
		"none"
	);
	assert.equal(nextRunState({ ...scheduled, nextRunMs: null }).kind, "none");
	assert.equal(nextRunState({ ...scheduled, nextRunMs: undefined }).kind, "none");
	assert.equal(nextRunState({ ...scheduled, nextRunMs: NaN }).kind, "none");
});

// --- nextRunCell: what the list's "Next run" cell shows --------------------------
const labels = { when: "Thu, Oct 1, 2026 10:15 AM", relative: "in 5 minutes" };

test("nextRunCell: an upcoming slot keeps the relative time and the exact date", () => {
	const cell = nextRunCell({ ...scheduled, ...labels, nextRunMs: NOW + 5 * MIN });
	assert.deepEqual(cell, { text: "in 5 minutes", hint: labels.when, tone: "" });
});

test("nextRunCell: a due slot says so and names when it was due", () => {
	const cell = nextRunCell({ ...scheduled, ...labels, nextRunMs: NOW - 5 * MIN });
	assert.equal(cell.text, "Due now");
	assert.match(cell.hint, /Thu, Oct 1, 2026 10:15 AM/);
	assert.equal(cell.tone, "");
});

test("nextRunCell: an overdue slot is a warning that says where to look", () => {
	const cell = nextRunCell({ ...scheduled, ...labels, nextRunMs: NOW - 2 * 60 * MIN });
	assert.equal(cell.text, "Overdue");
	assert.equal(cell.tone, "warn");
	assert.match(cell.hint, /Runs tab/);
});

test("nextRunCell: a turned-off macro is not confused with 'no schedule'", () => {
	const cell = nextRunCell({ ...scheduled, ...labels, enabled: false, nextRunMs: NOW });
	assert.equal(cell.text, "Turned off");
	assert.equal(cell.tone, "muted");
});

test("nextRunCell: nothing to show is null (the cell renders its placeholder)", () => {
	assert.equal(nextRunCell({ ...scheduled, ...labels, scheduleEnabled: false }), null);
	assert.equal(nextRunCell({ ...scheduled, ...labels, nextRunMs: null }), null);
});

// --- cannotScheduleReason: a NEW macro, before the server has an owner to judge ---
test("cannotScheduleReason: Administrator is told why, a named user is not blocked", () => {
	assert.match(cannotScheduleReason("Administrator"), /Administrator/);
	assert.equal(cannotScheduleReason("priya@example.com"), "");
	assert.equal(cannotScheduleReason(null), "");
});
