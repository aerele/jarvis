import test from "node:test";
import assert from "node:assert/strict";
import {
	boundedDelivery,
	newSendRequestId,
	settledSendResult,
	observeDelivery,
} from "./sendDelivery.js";

test("request ids are independently generated opaque 128-bit values", () => {
	const ids = new Set(Array.from({ length: 100 }, newSendRequestId));
	assert.equal(ids.size, 100);
	for (const id of ids) assert.match(id, /^[a-f0-9]{32}$/);
});
test("only settled, well-formed outcomes can authorize recovery", () => {
	for (const envelope of [
		null,
		{},
		{ delivery: "unknown" },
		{ delivery: "settled", result: { ok: true } },
		{ delivery: "settled", result: { confirmed: true } },
	])
		assert.throws(() => settledSendResult(envelope), /Delivery not confirmed/);
	for (const result of [
		{ ok: false, reason: "busy" },
		{ ok: false, confirmed: true, conversation_id: "A" },
		{ ok: true, conversation_id: "A", message_id: "M", run_id: "R" },
	])
		assert.equal(settledSendResult({ delivery: "settled", result }), result);
});
test("a stalled send is bounded without retrying or treating it as rejection", async () => {
	let finish;
	const pending = new Promise((resolve) => {
		finish = resolve;
	});
	await assert.rejects(boundedDelivery(pending, 1), /timed out/);
	finish({ ok: true });
	assert.deepEqual(await boundedDelivery(Promise.resolve({ ok: false })), { ok: false });
});

test("request IDs work without secure-context randomUUID", () => {
	const rng = {
		getRandomValues: (bytes) => {
			bytes.fill(19);
			return bytes;
		},
	};
	assert.equal(newSendRequestId(rng), "13".repeat(16));
});

test("a deadline exposes recovery but preserves late authoritative success", async () => {
	let resolve,
		expired = false;
	const result = { ok: true, conversation_id: "A", message_id: "M", run_id: "R" };
	const completion = observeDelivery(
		new Promise((r) => {
			resolve = r;
		}),
		() => {
			expired = true;
		},
		1
	);
	await new Promise((r) => setTimeout(r, 10));
	assert.equal(expired, true);
	resolve(result);
	assert.equal(await completion, result);
});

test("uncertain diagnostics remain errors and never expose arbitrary server text", () => {
	for (const status of ["missing", "pending", "interrupted", "unavailable"]) {
		assert.throws(
			() =>
				settledSendResult({
					delivery: "unknown",
					receipt_status: status,
					message: "PRIVATE",
				}),
			(error) => error.deliveryUncertain && !error.message.includes("PRIVATE")
		);
	}
});

test("authentication, CSRF and access failures explain recovery without claiming delivery failed", async () => {
	const { deliveryFailureCopy } = await import("./sendDelivery.js");
	assert.match(deliveryFailureCopy({ status: 401 }), /sign in/i);
	assert.match(deliveryFailureCopy({ exc_type: "CSRFTokenError" }), /copy.*reload/i);
	assert.match(deliveryFailureCopy({ status: 403 }), /access|permission/i);
});

test("every transport failure names its cause, the recovery step and a support reference", async () => {
	const { deliveryFailureCopy, deliveryFailureCause, deliveryTimeoutError } = await import(
		"./sendDelivery.js"
	);
	const id = "0123456789abcdef0123456789abcdef";
	const cases = [
		[deliveryTimeoutError(30000), /did not answer within 30 seconds/],
		[new Error("Delivery check timed out"), /delivery check did not get an answer/i],
		[new TypeError("Failed to fetch"), /network connection dropped/],
		[Object.assign(new Error("x"), { status: 502 }), /not reachable right now \(HTTP 502\)/],
		[Object.assign(new Error("x"), { status: 429 }), /too many requests/],
		[Object.assign(new Error("x"), { status: 413 }), /too large/],
		[
			Object.assign(new Error("x"), { status: 500, exc_type: "OperationalError" }),
			/hit an error .*\(HTTP 500\)\./,
		],
		[
			Object.assign(new Error("x"), { status: 417, exc_type: "ValidationError" }),
			/refused this request \(HTTP 417\)/,
		],
		// frappe-ui's call() crashes on a non-JSON body, which is how an nginx 502
		// or a restarting bench reaches this code (#696). That shape is the
		// gateway error, not an app bug.
		[
			new TypeError("Cannot read properties of undefined (reading 'exc_type')"),
			/did not send a readable reply/,
		],
		// Any other TypeError is an app bug, and must not send the customer
		// chasing their network.
		[new TypeError("x is not a function"), /app hit an error/],
		[
			Object.assign(new Error("x"), { exc_type: "TimestampMismatchError" }),
			/failed before the server answered/,
		],
		[new Error("x"), /failed before the server answered/],
	];
	for (const [error, cause] of cases) {
		const copy = deliveryFailureCopy(error, id);
		assert.match(copy, cause, String(error?.message || error?.status));
		assert.doesNotMatch(copy, /OperationalError|ValidationError|TypeError/, "no class names");
		assert.match(copy, /Check delivery before sending it again/);
		assert.match(copy, /Reference 89abcdef\.$/);
		assert.doesNotMatch(
			copy,
			/^Delivery not confirmed/,
			"the title is not repeated as the note's lead"
		);
	}
	// No reference clause without an ID (programmatic sends).
	assert.doesNotMatch(deliveryFailureCopy(new Error("x")), /Reference/);
	assert.equal(deliveryFailureCause(null), "The request failed before the server answered.");
});

test("the server's own sentence is quoted, a generic 500 placeholder and markup are not", async () => {
	const { deliveryFailureCause } = await import("./sendDelivery.js");
	const said = deliveryFailureCause(
		Object.assign(new Error("x"), {
			status: 500,
			exc_type: "ValidationError",
			messages: ["<b>Attachment</b> invoice.pdf was &gt; 25 MB"],
		})
	);
	assert.match(said, /The server said: Attachment invoice\.pdf was (&gt;|>) 25 MB\.$/);
	assert.doesNotMatch(said, /<b>/);
	assert.doesNotMatch(
		deliveryFailureCause(
			Object.assign(new Error("x"), { status: 500, messages: ["Internal Server Error"] })
		),
		/server said/
	);
	const long = "a".repeat(600);
	const clipped = deliveryFailureCause(
		Object.assign(new Error("x"), { status: 500, messages: [long] })
	);
	assert.ok(clipped.length < 400, "a runaway server message is clipped");
});

test("an uncertain receipt keeps its own diagnostic and gains the reference", async () => {
	const { deliveryFailureCopy, settledSendResult } = await import("./sendDelivery.js");
	let thrown;
	try {
		settledSendResult({ delivery: "unknown", reason: "request_mismatch" });
	} catch (e) {
		thrown = e;
	}
	assert.match(thrown.message, /delivery ID already belongs to a different message/);
	// Recovery keeps the same ID, so the copy must not promise a fresh-ID retry.
	assert.doesNotMatch(thrown.message, /fresh ID|Retry/);
	assert.match(thrown.message, /send the message again from the composer/);
	assert.match(
		deliveryFailureCopy(thrown, "abcdefgh12345678"),
		/different message.* Reference 12345678\.$/
	);
	// A reason that is not request_mismatch never collides with a receipt key.
	assert.throws(
		() => settledSendResult({ delivery: "unknown", reason: "unavailable" }),
		/could not read the delivery receipt/
	);
	// Sign-in, CSRF and access notes carry the reference too.
	for (const e of [{ status: 401 }, { exc_type: "CSRFTokenError" }, { status: 403 }])
		assert.match(deliveryFailureCopy(e, "abcdefgh12345678"), /Reference 12345678\.$/);
	// check_delivery's own read failing is told apart from "no receipt".
	assert.throws(
		() => settledSendResult({ delivery: "unknown" }),
		/Delivery not confirmed\. The server could not read the delivery receipt/
	);
});
