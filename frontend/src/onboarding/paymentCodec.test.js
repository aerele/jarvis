// Wire-shape decoding for the onboarding payment surfaces. Pure module, so
// node --test runs it with no bundler and no DOM (see vitest.config.js for why
// *.test.js and *.spec.js are split).
//
// Three shapes reach this codec and they are NOT the same shape - which is the
// whole reason it exists:
//   1. the facade envelope on success  {ok:true, contract_version, data, context}
//   2. the facade envelope on failure  {ok:false, error, context} under a 4xx/5xx
//   3. finish_payment's OLD shape      a raised frappe.throw: a generic status,
//      Frappe's exc_type, and the machine-readable error STAMPED on the response
//      next to it (onboarding_contract.stamp_error) rather than inside a body
//      the caller returned.

import test from "node:test";
import assert from "node:assert/strict";

import { decode, effectiveCode, CLIENT_OFFLINE, CLIENT_UNREADABLE } from "./paymentCodec.js";
import { CODES } from "./paymentCodes.js";

// ---------------------------------------------------------------------------
// 1. the facade success envelope
// ---------------------------------------------------------------------------
test("decode: a facade success carries data, context and the contract version", () => {
	const out = decode({
		status: 200,
		body: {
			message: {
				ok: true,
				contract_version: 2,
				data: {
					code: CODES.PAYMENT_CONFIRMATION_PENDING,
					attempt_id: "att_1",
					generation: 2,
				},
				context: { email: "a@b.com", plan: "pro" },
			},
		},
	});
	assert.equal(out.ok, true);
	assert.equal(out.code, CODES.PAYMENT_CONFIRMATION_PENDING);
	assert.equal(out.contractVersion, 2);
	assert.equal(out.data.attempt_id, "att_1");
	assert.equal(out.context.email, "a@b.com");
	assert.equal(out.httpStatus, 200);
});

// ---------------------------------------------------------------------------
// 2. the facade failure envelope, under its DELIBERATE 4xx
// ---------------------------------------------------------------------------
test("decode: a facade failure reads error.code, never the HTTP status", () => {
	const out = decode({
		status: 409,
		body: {
			message: {
				ok: false,
				error: {
					code: CODES.PAYMENT_ALREADY_ACTIVE,
					message: "This signup is already paid. Continue setup.",
					recovery: "continue_setup",
					subscription_status: "Active",
				},
				context: { plan: "pro" },
			},
		},
	});
	assert.equal(out.ok, false);
	assert.equal(out.code, CODES.PAYMENT_ALREADY_ACTIVE);
	assert.equal(out.recovery, "continue_setup");
	assert.equal(out.httpStatus, 409);
	// The error object's extras ride along - a reader that needs
	// subscription_status must not have to re-derive it from the status line.
	assert.equal(out.error.subscription_status, "Active");
	assert.equal(out.context.plan, "pro");
});

test("decode: a 429 carries retry_after_seconds and is NOT a decline", () => {
	const out = decode({
		status: 429,
		body: {
			message: {
				ok: false,
				error: {
					code: CODES.PAYMENT_CHECK_RATE_LIMITED,
					message: "We're still checking with your payment provider.",
					recovery: "retry",
					retry_after_seconds: 45,
				},
			},
		},
	});
	assert.equal(out.code, CODES.PAYMENT_CHECK_RATE_LIMITED);
	assert.equal(out.retryAfterSeconds, 45);
	assert.equal(out.ok, false);
});

test("decode: a bench-local refusal keeps its BENCH_ code", () => {
	const out = decode({
		status: 409,
		body: {
			message: {
				ok: false,
				error: {
					code: CODES.BENCH_AWAITING_RECONCILIATION,
					message: "we're still confirming a payment on this signup",
					recovery: "check_status",
				},
				context: {},
			},
		},
	});
	assert.equal(out.code, CODES.BENCH_AWAITING_RECONCILIATION);
	assert.equal(out.recovery, "check_status");
});

// ---------------------------------------------------------------------------
// 3. finish_payment's OLD shape: a THROW with the error stamped beside exc_type
// ---------------------------------------------------------------------------
test("decode: a stamped throw is read from the response body's own error key", () => {
	const out = decode({
		status: 417,
		body: {
			exc_type: "ValidationError",
			exception:
				"frappe.exceptions.ValidationError: This Cashfree mandate is not authorized.",
			_server_messages:
				'["{\\"message\\": \\"This Cashfree mandate is not authorized.\\"}"]',
			error: {
				code: CODES.PAYMENT_DECLINED,
				message: "This Cashfree mandate is not authorized.",
				recovery: "retry",
			},
		},
	});
	assert.equal(out.ok, false);
	assert.equal(out.code, CODES.PAYMENT_DECLINED);
	// 417 is frappe.throw's generic status. Nothing may be read from it.
	assert.equal(out.httpStatus, 417);
	assert.equal(out.excType, "ValidationError");
});

test("decode: a stamped throw's message beats the server-message prose", () => {
	const out = decode({
		status: 417,
		body: {
			exc_type: "ValidationError",
			_server_messages: '["{\\"message\\": \\"admin is unreachable right now\\"}"]',
			error: {
				code: CODES.BENCH_ADMIN_UNREACHABLE,
				message: "connect timeout",
				recovery: "retry",
			},
		},
	});
	assert.equal(out.code, CODES.BENCH_ADMIN_UNREACHABLE);
	assert.equal(out.message, "connect timeout");
});

test("decode: an UNSTAMPED throw falls back to (status, exc_type), never to prose", () => {
	// A control plane older than the stamping build. The duplicate is the one
	// rejection whose class is stable all the way back, so it is the one that
	// may be keyed structurally.
	const out = decode({
		status: 409,
		body: {
			exc_type: "DuplicateEntryError",
			_server_messages:
				'["{\\"message\\": \\"An account for this email and company already exists.\\"}"]',
		},
	});
	assert.equal(out.code, CODES.ACCOUNT_ALREADY_EXISTS);
	assert.equal(out.message, "An account for this email and company already exists.");
});

test("decode: an unstamped, unrecognised throw is honestly unknown", () => {
	const out = decode({
		status: 500,
		body: {
			exc_type: "SomethingElseError",
			_server_messages: '["{\\"message\\": \\"boom\\"}"]',
		},
	});
	assert.equal(out.code, CLIENT_UNREADABLE);
	assert.equal(out.message, "boom");
});

test("decode: a 403 is a permission problem, not a payment verdict", () => {
	const out = decode({
		status: 403,
		body: {
			exc_type: "PermissionError",
			_server_messages: '["{\\"message\\": \\"Not permitted\\"}"]',
		},
	});
	assert.equal(out.code, CLIENT_UNREADABLE);
	assert.equal(out.ok, false);
	// Specifically NOT any payment code: a permission refusal must never render
	// as a decline or a pending payment.
	assert.notEqual(out.code, CODES.PAYMENT_DECLINED);
});

// ---------------------------------------------------------------------------
// transport
// ---------------------------------------------------------------------------
test("decode: a dead network is its own code, not a payment state", () => {
	const out = decode({ status: 0, networkError: true });
	assert.equal(out.ok, false);
	assert.equal(out.code, CLIENT_OFFLINE);
});

test("decode: an unparseable body never throws", () => {
	const out = decode({ status: 502, body: null });
	assert.equal(out.ok, false);
	assert.equal(out.code, CLIENT_UNREADABLE);
});

// ---------------------------------------------------------------------------
// the pre-contract bridge: start_signup and finish_payment answer FLAT
// ---------------------------------------------------------------------------
test("decode: a flat legacy success is data with no code", () => {
	const out = decode({
		status: 200,
		body: { message: { pending_verification: true, email: "a@b.com" } },
	});
	assert.equal(out.ok, true);
	assert.equal(out.code, "");
	assert.equal(out.data.pending_verification, true);
});

test("effectiveCode: a coded envelope is taken at its word", () => {
	assert.equal(
		effectiveCode({ ok: true, code: CODES.PAYMENT_DECLINED, data: {} }),
		CODES.PAYMENT_DECLINED
	);
});

test("effectiveCode: a flat pending_verification maps onto the contract vocabulary", () => {
	assert.equal(
		effectiveCode({ ok: true, code: "", data: { pending_verification: true } }),
		CODES.SIGNUP_VERIFICATION_REQUIRED
	);
});

test("effectiveCode: a flat response carrying handles is a live intent", () => {
	assert.equal(
		effectiveCode({
			ok: true,
			code: "",
			data: { razorpay_order_id: "order_1", razorpay_key_id: "k" },
		}),
		CODES.PAYMENT_CONFIRMATION_PENDING
	);
	assert.equal(
		effectiveCode({ ok: true, code: "", data: { subscription_session_id: "s_1" } }),
		CODES.PAYMENT_CONFIRMATION_PENDING
	);
});

test("effectiveCode: a flat already-Active response is paid", () => {
	assert.equal(
		effectiveCode({ ok: true, code: "", data: { subscription_status: "Active" } }),
		CODES.PAYMENT_ALREADY_ACTIVE
	);
});

test("effectiveCode: a flat response with nothing to go on stays unknown", () => {
	assert.equal(effectiveCode({ ok: true, code: "", data: {} }), CLIENT_UNREADABLE);
});

// A 200 must not mean paid. confirm_payment answers FLAT, and only the
// connection-payload shape means admin actually confirmed the money.
test("effectiveCode: a confirm that returns the connection payload is paid", () => {
	assert.equal(
		effectiveCode({
			ok: true,
			code: "",
			data: { tenant_status: "running", agent_url: "https://pod", chat_readiness: "Ready" },
		}),
		CODES.PAYMENT_ALREADY_ACTIVE
	);
});

test("effectiveCode: admin's allocation-FAILURE confirm is still paid, and keeps its reason", () => {
	// api/tenant.py returns {"ok": True, data: _pending_payload("Something went
	// wrong finishing setup - our team has been alerted.")} when the payment was
	// recorded but allocation blew up. The money DID move, so it is paid; what
	// must not happen is the customer getting a 90-second spinner instead of that
	// sentence. _pending_payload carries tenant_status:"pending" and a BLANK
	// agent_url, so the discriminator cannot be agent_url truthiness.
	const decoded = {
		ok: true,
		code: "",
		data: {
			tenant_status: "pending",
			agent_url: "",
			chat_readiness: "Provisioning",
			chat_readiness_reason:
				"Something went wrong finishing setup — our team has been alerted.",
		},
	};
	assert.equal(effectiveCode(decoded), CODES.PAYMENT_ALREADY_ACTIVE);
});

test("effectiveCode: an empty or junk 200 body is NOT paid", () => {
	// The fuzz cases that used to advance straight to PAID because the old
	// confirm path fired on decoded.ok alone.
	assert.equal(effectiveCode({ ok: true, code: "", data: {} }), CLIENT_UNREADABLE);
	assert.equal(effectiveCode({ ok: true, code: "", data: [] }), CLIENT_UNREADABLE);
	assert.equal(
		effectiveCode({ ok: true, code: "", data: { some: "unrelated", fields: 1 } }),
		CLIENT_UNREADABLE
	);
});
