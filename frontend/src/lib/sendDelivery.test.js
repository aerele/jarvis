import test from "node:test";
import assert from "node:assert/strict";
import { boundedDelivery, newSendRequestId, settledSendResult } from "./sendDelivery.js";

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
