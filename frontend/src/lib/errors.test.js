// Unit tests for the shared error-message extractor (#696, plus round-4 review
// findings F1/F2). Pure, node --test - no DOM here, so errMessage() takes its
// typeof document === "undefined" branch and returns the raw string, which is
// exactly what these assert on.

import test from "node:test";
import assert from "node:assert/strict";

import { errMessage, turnErrorInfo } from "./errors.js";

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

// -----------------------------------------------------------------------
// turnErrorInfo (#702): the counterpart to errMessage() above, for a chat
// TURN's raw `error` string instead of a Frappe API exception.
// -----------------------------------------------------------------------

// The exact wire text from #702: the agent's own generic wording for a
// mid-run failure that was actually a device-pairing file caught mid-
// rewrite, nothing to do with the network. Must land on "gateway" (retry),
// not "unreachable" (we never lost the connection) and not "internal" (the
// old behavior - a dead-end headline with no next step).
test("#702: the observed string classifies as gateway, not unreachable or internal", () => {
	const info = turnErrorInfo("LLM request failed: network connection error.");
	assert.equal(info.code, "gateway");
	assert.notEqual(info.code, "unreachable");
	assert.notEqual(info.code, "internal");
	assert.match(info.hint, /try/i);
});

test("a genuine transport failure stays unreachable", () => {
	assert.equal(turnErrorInfo("ws open failed: connect ECONNREFUSED").code, "unreachable");
	assert.equal(turnErrorInfo("agent unreachable after 3 attempts").code, "unreachable");
});

test("a provider rejection (quota/billing) stays provider", () => {
	const info = turnErrorInfo(
		"Google Generative AI API error (429): You exceeded your current quota."
	);
	assert.equal(info.code, "provider");
});

// The Python mirror (turn_handler._classify_error) matches on both "rate
// limit" and the hyphenated "rate-limit". Must agree here too, or the same
// text classifies as provider live and gateway on a reload.
test("a hyphenated rate-limit reads as provider, matching the Python mirror", () => {
	assert.equal(turnErrorInfo("upstream rate-limit exceeded").code, "provider");
});

// classifyTurnErrorCode's "connection timed out" belongs to the unreachable
// bucket (a transport failure), not the generic timeout bucket - the Python
// mirror agrees (see test_connection_timed_out_is_unreachable_not_timeout).
test("connection timed out is unreachable, not a generic timeout", () => {
	assert.equal(turnErrorInfo("connection timed out").code, "unreachable");
});

test("a recovery-window expiry and a timeout keep their own codes", () => {
	assert.equal(
		turnErrorInfo("Run did not finish within the recovery window.").code,
		"recovery-expired"
	);
	assert.equal(turnErrorInfo("request timed out after 30s").code, "timeout");
});

// The worker's own explicit code="internal" backstop text - a page refresh
// only has this persisted string and must reclassify it the same way the
// live event did, not fall into the new "gateway" default.
test("the worker backstop's own text stays internal on a reload", () => {
	assert.equal(turnErrorInfo("unexpected worker error: TypeError").code, "internal");
});

// The live run:error event's own `code` always wins over reclassifying the
// text - this is what lets the backend's richer classification (it has the
// raised exception, not just its stringified message) override the text
// heuristic.
test("an explicit live code wins over reclassifying the text", () => {
	const info = turnErrorInfo("some unrelated message", "provider");
	assert.equal(info.code, "provider");
});

// #702 requirement: three failures that need different customer action must
// not collapse into the same headline+hint.
test("unreachable, provider and gateway are three distinct headline+hint pairs", () => {
	const unreachable = turnErrorInfo("ws open failed");
	const provider = turnErrorInfo("insufficient credit");
	const gateway = turnErrorInfo("LLM request failed: network connection error.");
	const pairs = [unreachable, provider, gateway].map((i) => `${i.headline}|${i.hint}`);
	assert.equal(new Set(pairs).size, 3);
	// The transient/gateway case is the one #702 asks to specifically tell the
	// customer to retry.
	assert.match(gateway.hint, /try/i);
});

test("cancelled has no hint - it renders as a muted note, never the error card", () => {
	const info = turnErrorInfo("You cancelled this message.");
	assert.equal(info.code, "cancelled");
	assert.equal(info.hint, "");
});

// MUST be total (same contract as errMessage above): any shape of input
// returns a usable envelope and never throws, including a truthy non-string
// that would otherwise crash a naive `.toLowerCase()` call.
test("turnErrorInfo is total: null, undefined, a number and an object never throw", () => {
	for (const raw of [null, undefined, 42, { message: "boom" }, []]) {
		const info = turnErrorInfo(raw);
		assert.equal(typeof info.code, "string");
		assert.equal(typeof info.headline, "string");
		assert.ok(info.headline.length > 0);
	}
});

// An unrecognized wire code (e.g. a future server-side taxonomy value this
// build doesn't know about yet) must degrade to the generic headline with no
// hint, never crash and never render a blank headline.
test("an unrecognized explicit code falls back to the generic headline", () => {
	const info = turnErrorInfo("does not matter", "some-future-code-v2");
	assert.equal(info.code, "some-future-code-v2");
	assert.equal(info.headline, "Something went wrong");
	assert.equal(info.hint, "");
});
