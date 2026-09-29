import { test } from "node:test";
import assert from "node:assert/strict";
import { denyOutcome } from "./denyOutcome.js";

// P0c: DecisionSheet's Deny now calls dismiss_tool instead of dismissing
// client-only. These pin the outcome mapping without needing to mount Vue
// (the PWA has no component-mount test tooling).

test("a clean ok:true response resolves as denied", () => {
	const out = denyOutcome({ ok: true, data: { status: "discarded" } });
	assert.deepEqual(out, { state: "denied", error: "", resolved: "denied" });
});

test("the benign already_handled no-op still resolves as denied", () => {
	// dismiss_tool returns ok:true with status already_handled for a token
	// consumed elsewhere (another tab, or Confirm won the race) - the sheet
	// still just closes as denied, no error.
	const out = denyOutcome({ ok: true, data: { status: "already_handled" } });
	assert.deepEqual(out, { state: "denied", error: "", resolved: "denied" });
});

test("an ok:false response goes back to review with the server's error message", () => {
	const out = denyOutcome({ ok: false, error: { message: "storage unavailable" } });
	assert.deepEqual(out, { state: "review", error: "storage unavailable", resolved: null });
});

test("an ok:false response with no error.message falls back to reason, then a generic string", () => {
	assert.equal(denyOutcome({ ok: false, reason: "busy" }).error, "busy");
	assert.equal(denyOutcome({ ok: false }).error, "Couldn't discard this action.");
});

test("a missing/undefined response is treated as success (never blocks on a malformed envelope)", () => {
	assert.equal(denyOutcome(undefined).resolved, "denied");
	assert.equal(denyOutcome(null).resolved, "denied");
});
