// Real executable test for the "Approve & run" arming toggle's gating + copy
// (skill approve-and-run, P2). Plain node built-ins (node:test + node:assert) —
// no browser, the promotionBudget.test.js precedent. Run directly
// (`node --test approveRunToggle.test.js`), via `npm run test:node`, or through
// the python suite (jarvis/tests/test_approve_run_toggle_client.py subprocess-runs
// it so the kill-switch matrix + risk-disclosure contract lives in the suite
// forever). `node --test` exits non-zero on any failed assertion.
//
// Why this exists: `armToggleLocked` is a 3-boolean truth table where one wrong
// sign silently (a) LOCKS the kill switch for an armed skill's owner, or (b)
// makes the toggle look flippable to a non-admin. The server guard still blocks
// the write either way, but the UI would misrepresent a security control's state
// — exactly the class of bug a happy-path flow review won't probe.
import { test } from "node:test";
import assert from "node:assert/strict";
import { armToggleLocked, armToggleDescription } from "./approveRunToggle.js";

test("readonly locks regardless of arm/admin state", () => {
	assert.equal(armToggleLocked(true, false, false), true);
	assert.equal(armToggleLocked(true, true, true), true);
	assert.equal(armToggleLocked(true, true, false), true); // a shared/read-only skill never editable
});

test("unarmed + non-admin -> locked (only an admin may arm)", () => {
	assert.equal(armToggleLocked(false, false, false), true);
});

test("unarmed + admin -> unlocked (an admin may arm)", () => {
	assert.equal(armToggleLocked(false, false, true), false);
});

test("armed + non-admin owner -> unlocked (KILL SWITCH: disabling is always free)", () => {
	assert.equal(armToggleLocked(false, true, false), false);
});

test("armed + admin -> unlocked", () => {
	assert.equal(armToggleLocked(false, true, true), false);
});

test("description appends the admin-only hint ONLY for an unarmed non-admin viewer", () => {
	assert.match(armToggleDescription(false, false), /Only a Jarvis Admin can arm a skill\./);
	assert.doesNotMatch(armToggleDescription(false, true), /Only a Jarvis Admin/); // admin
	assert.doesNotMatch(armToggleDescription(true, false), /Only a Jarvis Admin/); // already armed
});

test("description discloses the true covered set and the corrected kill-switch copy", () => {
	const d = armToggleDescription(true, true);
	assert.match(d, /Approve & run/);
	// run_method disclosure: the description must NOT undersell the blast radius to
	// only doc-shaped verbs — "any other whitelisted action" covers run_method.
	assert.match(d, /any other whitelisted action/);
	assert.match(d, /each still leaves a receipt/);
	// the kill-switch copy must not claim the toggle alone takes effect (explicit-Save form)
	assert.match(d, /takes effect immediately once saved/);
	assert.match(d, /delete, cancel and amend still ask/);
});
