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
