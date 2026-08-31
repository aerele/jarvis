// isExpectedCompanyDefaultsMiss: distinguishing the EXPECTED "no such company"
// rejection from a genuine failure. Pure module, node --test (see
// vitest.config.js for why *.test.js and *.spec.js are split).

import test from "node:test";
import assert from "node:assert/strict";

import { isExpectedCompanyDefaultsMiss } from "./companyDefaultsMiss.js";

test("a 404 status alone is treated as the expected miss", () => {
	const e = new Error("not found");
	e.status = 404;
	assert.equal(isExpectedCompanyDefaultsMiss(e), true);
});

test("COMPANY_DEFAULTS_NOT_FOUND carried in e.messages is treated as the expected miss", () => {
	// frappe-ui's call() concats the response body's `message` key into
	// e.messages; a coded {ok:false, error:{code}} envelope arrives as an
	// OBJECT there because JSON.parse on a non-string leaves it untouched.
	const e = new Error("unknown company");
	e.status = 404;
	e.messages = [
		{ ok: false, error: { code: "COMPANY_DEFAULTS_NOT_FOUND", message: "unknown company" } },
	];
	assert.equal(isExpectedCompanyDefaultsMiss(e), true);
});

test("COMPANY_DEFAULTS_NOT_FOUND with no status set is still caught via e.messages", () => {
	const e = { messages: [{ error: { code: "COMPANY_DEFAULTS_NOT_FOUND" } }] };
	assert.equal(isExpectedCompanyDefaultsMiss(e), true);
});

test("COMPANY_DEFAULTS_FORBIDDEN (403) is NOT the expected miss - it is a real problem", () => {
	const e = new Error("forbidden");
	e.status = 403;
	e.messages = [
		{ ok: false, error: { code: "COMPANY_DEFAULTS_FORBIDDEN", message: "not permitted" } },
	];
	assert.equal(isExpectedCompanyDefaultsMiss(e), false);
});

test("a generic 500 / network failure is NOT the expected miss", () => {
	const e = new Error("Internal Server Error");
	e.status = 500;
	e.messages = ["Internal Server Error"];
	assert.equal(isExpectedCompanyDefaultsMiss(e), false);
});

test("a falsy or malformed error never throws and is never the expected miss", () => {
	assert.equal(isExpectedCompanyDefaultsMiss(null), false);
	assert.equal(isExpectedCompanyDefaultsMiss(undefined), false);
	assert.equal(isExpectedCompanyDefaultsMiss({}), false);
	assert.equal(isExpectedCompanyDefaultsMiss({ messages: "not-an-array" }), false);
	assert.equal(isExpectedCompanyDefaultsMiss({ messages: [null, "string message", 42] }), false);
});
