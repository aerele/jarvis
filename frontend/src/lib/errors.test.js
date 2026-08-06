// Unit tests for the shared error-message extractor (#696). Pure, node --test -
// no DOM here, so errMessage() takes its typeof document === "undefined" branch
// and returns the raw string, which is exactly what these assert on.

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
// out of this function.
test("never echoes a raw TypeError's message (the exact #696 crash)", () => {
	const e = new TypeError("Cannot read properties of undefined (reading 'exc_type')");
	const msg = errMessage(e);
	assert.notEqual(msg, e.message);
	assert.doesNotMatch(msg, /exc_type/);
	assert.equal(msg, "Something went wrong. Please try again.");
});

test("never echoes a raw ReferenceError or SyntaxError either", () => {
	assert.equal(
		errMessage(new ReferenceError("x is not defined")),
		"Something went wrong. Please try again."
	);
	assert.equal(
		errMessage(new SyntaxError("Unexpected token < in JSON")),
		"Something went wrong. Please try again."
	);
});

test("a well-formed Error (not a TypeError) still surfaces its own message", () => {
	// A plain Error carrying a real, useful message (e.g. a validation failure)
	// must not be swept into the generic fallback just because it's an Error.
	const e = new Error("models must be a non-empty list");
	assert.equal(errMessage(e), "models must be a non-empty list");
});

// #696 suggested direction: special-case 401/403 into a session-expired sentence
// with somewhere to go, since that's the most likely cause of an otherwise
// unreadable error on an authenticated call.
test("a 403 becomes a session-expired sentence, not the server's generic text", () => {
	assert.equal(
		errMessage({ status: 403, messages: ["Internal Server Error"] }),
		"Your session has expired. Please sign in again."
	);
});

test("a 401 gets the same session-expired sentence", () => {
	assert.equal(
		errMessage({ status: 401, message: "Unauthorized" }),
		"Your session has expired. Please sign in again."
	);
});

test("a non-auth status keeps its own message", () => {
	assert.equal(
		errMessage({ status: 500, message: "Internal Server Error" }),
		"Internal Server Error"
	);
});
