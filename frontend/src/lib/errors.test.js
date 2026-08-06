// Unit tests for the shared error-message extractor (#696, plus round-4 review
// findings F1/F2). Pure, node --test - no DOM here, so errMessage() takes its
// typeof document === "undefined" branch and returns the raw string, which is
// exactly what these assert on.

import test from "node:test";
import assert from "node:assert/strict";

import { errMessage } from "./errors.js";

test("extracts the first server message when present", () => {
	assert.equal(errMessage({ messages: ["Settings -> Developer"] }), "Settings -> Developer");
});

test("falls back to e.message when there are no server messages", () => {
	assert.equal(errMessage({ message: "boom" }), "boom");
});

test("falls back to a generic sentence for a falsy error", () => {
	assert.equal(errMessage(null), "Something went wrong. Please try again.");
	assert.equal(errMessage(undefined), "Something went wrong. Please try again.");
});

// #696: frappe-ui's call() can itself crash mid-parse (error.exc_type read with
// no guard after a failed JSON.parse) and throw a raw TypeError instead of the
// Frappe-shaped Error it normally builds. That TypeError's .message is an
// internal property name, never a sentence for a customer - it must never come
// out of this function. Both current and pre-2021 V8 wording are covered.
test("never echoes a raw TypeError's message (the exact #696 crash, current V8 wording)", () => {
	const e = new TypeError("Cannot read properties of undefined (reading 'exc_type')");
	const msg = errMessage(e);
	assert.notEqual(msg, e.message);
	assert.doesNotMatch(msg, /exc_type/);
	assert.equal(msg, "Something went wrong. Please try again.");
});

test("never echoes a raw TypeError's message (pre-2021 V8 wording)", () => {
	const e = new TypeError("Cannot read property 'exc_type' of undefined");
	assert.equal(errMessage(e), "Something went wrong. Please try again.");
});

test("also catches the null-target form of the same crash", () => {
	const e = new TypeError("Cannot read properties of null (reading 'exc_type')");
	assert.equal(errMessage(e), "Something went wrong. Please try again.");
});

// Round-4 review F2: a blanket `instanceof TypeError` (or Reference/SyntaxError)
// check swallowed OTHER real, actionable errors that happen to share a class
// with the #696 crash - most notably the browser's own network failure, which
// support engineers rely on seeing verbatim. Only the SPECIFIC "property read
// on undefined/null" shape may be suppressed; every other TypeError (and any
// ReferenceError/SyntaxError) must surface its own message like any other Error.
test("a TypeError that is NOT the property-read crash still surfaces its own message", () => {
	assert.equal(errMessage(new TypeError("Failed to fetch")), "Failed to fetch");
	assert.equal(
		errMessage(new TypeError("NetworkError when attempting to fetch resource.")),
		"NetworkError when attempting to fetch resource."
	);
});

test("a ReferenceError or SyntaxError is shown like any other Error (F2: no longer blanket-suppressed)", () => {
	assert.equal(errMessage(new ReferenceError("x is not defined")), "x is not defined");
	assert.equal(
		errMessage(new SyntaxError("Unexpected token < in JSON")),
		"Unexpected token < in JSON"
	);
});

test("a well-formed Error (not a TypeError) still surfaces its own message", () => {
	// A plain Error carrying a real, useful message (e.g. a validation failure)
	// must not be swept into the generic fallback just because it's an Error.
	const e = new Error("models must be a non-empty list");
	assert.equal(errMessage(e), "models must be a non-empty list");
});

// Round-4 review F1: a blanket 401/403 override buried a real, actionable
// permission message under a generic "session expired" sentence, sending the
// customer through a re-auth into the SAME 403 for a reason expiry never
// caused. The server's own explicit message must win whenever there is one.
test("a specific 403 message (a real permission refusal) wins over the session-expired override", () => {
	assert.equal(
		errMessage({
			status: 403,
			messages: ["You do not have permission to disconnect this model"],
		}),
		"You do not have permission to disconnect this model"
	);
});

test("a specific 401 message also wins over the session-expired override", () => {
	assert.equal(
		errMessage({ status: 401, message: "Two-factor code required" }),
		"Two-factor code required"
	);
});

// Only once there is genuinely nothing specific to show does a 401/403 fall
// back to the session-expired sentence.
test("a 403 with no usable message becomes a session-expired sentence", () => {
	assert.equal(errMessage({ status: 403 }), "Your session has expired. Please sign in again.");
	assert.equal(
		errMessage({ status: 403, messages: [] }),
		"Your session has expired. Please sign in again."
	);
});

test("a 401 with no usable message gets the same session-expired sentence", () => {
	assert.equal(errMessage({ status: 401 }), "Your session has expired. Please sign in again.");
});

// The #696 crash itself carries no .status at all (call.js throws before
// e.status is ever assigned) - it must fall through to the generic sentence,
// not somehow trip the 401/403 branch.
test("the #696 crash shape (no status) falls through to the generic sentence, not session-expired", () => {
	const e = new TypeError("Cannot read properties of undefined (reading 'exc_type')");
	assert.equal(errMessage(e), "Something went wrong. Please try again.");
});

test("a non-auth status keeps its own message", () => {
	assert.equal(
		errMessage({ status: 500, message: "Internal Server Error" }),
		"Internal Server Error"
	);
});
