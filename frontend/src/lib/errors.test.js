// Unit tests for the shared error-message extractor (#696, plus round-4 review
// findings F1/F2). Pure, node --test - no DOM here, so errMessage() takes its
// typeof document === "undefined" branch and returns the raw string, which is
// exactly what these assert on.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { errMessage, turnErrorInfo, TURN_ERROR_CODES, TURN_ERROR_MATCHERS } from "./errors.js";

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

// #823 splits the old catch-all `provider` in two on the axis that decides what
// the customer should DO: an exhausted quota has an hours-scale reset clock and
// no amount of retrying moves it, so it is terminal and points at the plan; a
// rate limit clears in seconds and is worth retrying. Collapsing them is what
// put a Retry button on a spent quota.
test("an exhausted quota is terminal and points at the plan, not at Retry", () => {
	// An UNAMBIGUOUS exhaustion slug. Retrying it does nothing until the balance
	// is topped up, so the card sends the customer to the plan instead.
	const info = turnErrorInfo("OpenAI error: insufficient_quota, check your billing");
	assert.equal(info.code, "quota-exhausted");
	assert.equal(info.retryable, false);
	assert.equal(info.action, "billing");
	assert.equal(
		turnErrorInfo("Your credit balance is too low to access the API").code,
		"quota-exhausted"
	);
});

// Review finding, and the reason the marker list carries no bare English words.
// Gemini writes "You exceeded your current quota" for an ordinary per-minute
// throttle exactly as it does for a spent balance. Matching the bare word
// "quota" would call that terminal and strip the Retry button off a failure
// that clears in seconds, stranding the customer. An ambiguous 429 must stay
// retryable: terminal is the expensive verdict and has to be earned.
test("an ambiguous 429 that merely mentions a quota stays retryable", () => {
	const info = turnErrorInfo(
		"Google Generative AI API error (429): You exceeded your current quota."
	);
	assert.equal(info.code, "throttled");
	assert.equal(info.retryable, true);
});

// The Python mirror (error_taxonomy) matches on both "rate limit" and the
// hyphenated "rate-limit". Must agree here too, or the same text classifies one
// way live and another on a reload.
test("a hyphenated rate-limit reads as throttled and stays retryable", () => {
	const info = turnErrorInfo("upstream rate-limit exceeded");
	assert.equal(info.code, "throttled");
	assert.equal(info.retryable, true);
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
test("unreachable, quota and gateway are three distinct headline+hint pairs", () => {
	const unreachable = turnErrorInfo("ws open failed");
	const quota = turnErrorInfo("insufficient credit");
	const gateway = turnErrorInfo("LLM request failed: network connection error.");
	const pairs = [unreachable, quota, gateway].map((i) => `${i.headline}|${i.hint}`);
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
	assert.equal(info.headline, "Something went wrong.");
	assert.equal(info.hint, "");
	// A code we cannot name is not evidence that a retry is pointless.
	assert.equal(info.retryable, true);
});

// -----------------------------------------------------------------------
// jarvis#823: the retryable flag, the envelope, and the parity contract.
// -----------------------------------------------------------------------

// The heart of #823. Before it, Retry rendered on every code but `cancelled`,
// so a revoked key, a model that does not exist and a spent quota each got a
// button that could not possibly work. This pins which failures may offer one.
test("every terminal code refuses Retry and every retryable code allows it", () => {
	const terminal = [
		"agent-unpaired",
		"auth-invalid",
		"cancelled",
		"context-overflow",
		"model-not-found",
		"quota-exhausted",
	];
	const retryable = [
		"gateway",
		"internal",
		"provider",
		"recovery-expired",
		"throttled",
		"timeout",
		"unreachable",
	];
	for (const code of terminal)
		assert.equal(turnErrorInfo("", code).retryable, false, `${code} must be terminal`);
	for (const code of retryable)
		assert.equal(turnErrorInfo("", code).retryable, true, `${code} must be retryable`);
	// The two lists together are the WHOLE taxonomy - a code added to the table
	// without a verdict here fails this.
	assert.deepEqual(
		[...terminal, ...retryable].sort(),
		Object.keys(TURN_ERROR_CODES).sort(),
		"every code in the table needs a retryable verdict in this test"
	);
});

// The server's word beats the table. It saw the raised exception and the
// gateway's own rejection code; this file only ever sees prose.
test("the server's retryable flag overrides the table", () => {
	assert.equal(turnErrorInfo("", { code: "gateway", retryable: false }).retryable, false);
	assert.equal(turnErrorInfo("", { code: "auth-invalid", retryable: true }).retryable, true);
});

// A terminal failure has to say what to do instead of offering a retry.
test("terminal codes that a customer can act on carry a corrective action", () => {
	assert.equal(turnErrorInfo("", "auth-invalid").action, "settings");
	assert.equal(turnErrorInfo("", "model-not-found").action, "settings");
	assert.equal(turnErrorInfo("", "quota-exhausted").action, "billing");
	// …and one only a human can resolve routes to that human, rather than
	// leaving the words "contact support" inert in the prose.
	const unpaired = turnErrorInfo("", "agent-unpaired");
	assert.equal(unpaired.action, "support");
	// Every terminal code either offers an action or is one the customer chose
	// (cancelled) or can resolve themselves without leaving the chat.
	for (const code of ["context-overflow", "cancelled"])
		assert.equal(turnErrorInfo("", code).action, null);
});

// Both Settings and the plan page are admin-only server-side, so pointing a
// member at either is a dead end. They get told who to ask instead. This is the
// same rule the plan page's own Renew button already follows.
test("a member gets the who-to-ask wording and no admin-only button", () => {
	for (const code of ["auth-invalid", "quota-exhausted"]) {
		const admin = turnErrorInfo("", code, { canConfigure: true });
		const member = turnErrorInfo("", code, { canConfigure: false });
		assert.ok(admin.action, `${code} should offer an action to an admin`);
		assert.equal(member.action, null, `${code} must not offer it to a member`);
		assert.match(member.hint, /administrator/i);
		assert.notEqual(member.hint, admin.hint);
	}
});

// A build with no support desk must not offer a Contact support button that
// goes nowhere. The hint still says a human is on it, so the card is not a
// silent dead end either.
test("the support action drops on a build with no support desk", () => {
	const withSupport = turnErrorInfo("", "agent-unpaired", { canContactSupport: true });
	const without = turnErrorInfo("", "agent-unpaired", { canContactSupport: false });
	assert.equal(withSupport.action, "support");
	assert.equal(without.action, null);
	assert.ok(without.hint.length > 0);
});

// "Try again in a moment" is true but useless when the real answer is forty
// minutes, so a reset clock the provider named is spoken out loud.
test("a named reset clock is spoken in the hint", () => {
	const throttled = turnErrorInfo("", {
		code: "throttled",
		retryable: true,
		resets_in_seconds: 2400,
	});
	assert.match(throttled.hint, /try again in about 40 minutes/i);
	const exhausted = turnErrorInfo("", {
		code: "quota-exhausted",
		retryable: false,
		resets_in_seconds: 7200,
	});
	assert.match(exhausted.hint, /resets in about 2 hours/i);
	// A monthly reset reads in days, never "about 720 hours".
	assert.match(
		turnErrorInfo("", { code: "quota-exhausted", resets_in_seconds: 30 * 86400 }).hint,
		/resets in about 30 days/i
	);
	// A missing or nonsense clock leaves the standard hint alone.
	assert.equal(turnErrorInfo("", { code: "throttled" }).hint, "Try again in a moment.");
	assert.equal(
		turnErrorInfo("", { code: "throttled", resets_in_seconds: -5 }).hint,
		"Try again in a moment."
	);
});

// The reload path. A row written since #823 carries the envelope, so the card
// after a refresh is the card before it - the divergence #823 exists to close.
test("a persisted envelope reads identically to the live event that wrote it", () => {
	const live = turnErrorInfo("Google error (429): quota exceeded", {
		code: "quota-exhausted",
		retryable: false,
	});
	const reloaded = turnErrorInfo("Google error (429): quota exceeded", {
		code: "quota-exhausted",
		retryable: false,
	});
	assert.deepEqual(live, reloaded);
});

// A row written BEFORE #823 has an error string and no code. The text ladder is
// the last resort, and it has to land on the same code the server's own ladder
// would, or a legacy row still reads differently before and after a refresh.
test("a legacy row with no code still classifies, on the same ladder as the server", () => {
	const cases = {
		"ws open failed: connect ECONNREFUSED": "unreachable",
		"connection timed out": "unreachable",
		"Run did not finish within the recovery window.": "recovery-expired",
		"request timed out after 30s": "timeout",
		"unexpected worker error: TypeError": "internal",
		"You cancelled this message.": "cancelled",
		"LLM request failed: network connection error.": "gateway",
		"This model's maximum context length is 8192 tokens": "context-overflow",
		"OpenAI error (401): Incorrect API key provided": "auth-invalid",
		"model claude-x does not exist or you do not have access": "model-not-found",
		insufficient_balance: "quota-exhausted",
		"Google Generative AI API error (429): You exceeded your current quota.": "throttled",
		"rate_limit_exceeded: slow down": "throttled",
	};
	for (const [text, code] of Object.entries(cases))
		assert.equal(turnErrorInfo(text).code, code, `"${text}" should classify as ${code}`);
});

// The parity ratchet (#823). jarvis#757 and #760 both shipped out of three
// hand-synced copies of this taxonomy. One contract file, asserted from both
// suites, is what stops a fourth: the Python table is checked against the same
// JSON in jarvis/tests/test_turn_handler.py.
test("the JS taxonomy matches the contract in jarvis/chat/turn_error_codes.json", () => {
	const contractPath = path.resolve(
		path.dirname(fileURLToPath(import.meta.url)),
		"../../../jarvis/chat/turn_error_codes.json"
	);
	const contract = JSON.parse(readFileSync(contractPath, "utf8"));
	const want = Object.fromEntries(
		Object.entries(contract.codes).map(([code, v]) => [code, v.retryable])
	);
	const got = Object.fromEntries(
		Object.entries(TURN_ERROR_CODES).map(([code, v]) => [code, !!v.retryable])
	);
	assert.deepEqual(got, want, "code -> retryable must match the contract exactly");
});

// The text ladder is the other half of the drift surface: two ladders that
// disagree make one failure read differently before and after a refresh, which
// is the same defect by another route. Pinned marker for marker.
test("the JS text ladder matches the contract's markers and status map", () => {
	const contractPath = path.resolve(
		path.dirname(fileURLToPath(import.meta.url)),
		"../../../jarvis/chat/turn_error_codes.json"
	);
	const contract = JSON.parse(readFileSync(contractPath, "utf8"));
	assert.deepEqual(TURN_ERROR_MATCHERS.markers, contract.markers);
	const status = Object.fromEntries(
		Object.entries(TURN_ERROR_MATCHERS.http_status).map(([k, v]) => [String(k), v])
	);
	assert.deepEqual(status, contract.http_status);
});
