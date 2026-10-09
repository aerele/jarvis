import { test } from "node:test";
import assert from "node:assert/strict";
import { createSendRecovery, recoveryCopy } from "./sendRecovery.js";

function setup() {
	let n = 0;
	const state = { requests: [] };
	return { state, recovery: createSendRecovery(state, () => String(++n)) };
}
const files = [{ name: "invoice.pdf", file_url: "/private/files/invoice.pdf" }];
const accepted = {
	delivery: "settled",
	result: { ok: true, conversation_id: "A", message_id: "M", run_id: "R" },
};

test("request snapshot retains files and ordered approval tokens independently of composer", () => {
	const { recovery } = setup();
	const attachments = files.map((f) => ({ ...f }));
	const tokens = ["first", "second"];
	const r = recovery.stage("A", "Review invoice", attachments, tokens);
	attachments[0].name = "changed";
	tokens.reverse();
	recovery.settle(r, { delivery: "settled", result: { ok: false, reason: "maintenance" } });
	assert.deepEqual(r.attachments, files);
	assert.deepEqual(r.approvalTokens, ["first", "second"]);
	assert.equal(r.conversation, "A");
	assert.equal(r.text, "Review invoice");
	assert.match(recoveryCopy(r).title, /Maintenance/);
});
test("unknown, malformed and empty responses never authorize a retry", () => {
	for (const response of [
		null,
		{},
		{ delivery: "unknown" },
		{ delivery: "settled", result: { ok: true } },
	]) {
		const { recovery } = setup();
		const r = recovery.stage("A", "", files);
		recovery.settle(r, response);
		assert.equal(r.state, "uncertain");
		assert.equal(recovery.retry(r), false);
		assert.deepEqual(r.attachments, files);
	}
});
test("attachment-only rejected request retries once with a fresh identifier and same files", () => {
	const { recovery } = setup();
	const r = recovery.stage("A", "", files);
	const first = r.id;
	recovery.settle(r, { delivery: "settled", result: { ok: false } });
	assert.equal(recovery.retry(r), true);
	assert.notEqual(r.id, first);
	assert.equal(recovery.retry(r), false);
	assert.deepEqual(r.attachments, files);
});
test("typed confirmation with partial failure is processed, not retryable rejection", () => {
	const { recovery } = setup();
	const r = recovery.stage("A", "confirm 1", []);
	recovery.settle(r, {
		delivery: "settled",
		result: { ok: false, confirmed: true, conversation_id: "A" },
	});
	assert.equal(r.state, "confirmed");
	assert.equal(recovery.retry(r), false);
});
test("accepted request remains until its exact server message is visible in the originating chat", () => {
	const { state, recovery } = setup();
	const r = recovery.stage("A", "hello", []);
	recovery.settle(r, accepted);
	recovery.reconcile("B", [{ name: "M" }]);
	assert.equal(state.requests.length, 1);
	recovery.reconcile("A", [{ name: "unrelated", content: "hello" }]);
	assert.equal(state.requests.length, 1);
	recovery.reconcile("A", [{ name: "M" }]);
	assert.equal(state.requests.length, 0);
});
test("a matching transcript cannot settle uncertain delivery", () => {
	const { state, recovery } = setup();
	const r = recovery.stage("A", "hello", []);
	recovery.settle(r, null);
	recovery.reconcile("A", [{ name: "M", content: "hello" }]);
	assert.equal(state.requests.length, 1);
	assert.equal(r.state, "uncertain");
});

test("a late transport failure cannot erase a receipt already checked", () => {
	const { recovery } = setup();
	const r = recovery.stage("A", "hello", []);
	recovery.settle(r, accepted);
	recovery.settle(r, null);
	assert.equal(r.state, "accepted");
	assert.equal(r.result.message_id, "M");
});

test("rejection copy uses readable gate messages and preserves the usage window", () => {
	const { recovery } = setup();
	const r = recovery.stage("A", "hello", []);
	recovery.settle(r, {
		delivery: "settled",
		result: { ok: false, reason: "usage_limit", limit_period: "Daily" },
	});
	assert.match(recoveryCopy(r).detail, /midnight/);
	assert.doesNotMatch(recoveryCopy(r).detail, /usage_limit/);
	assert.match(recoveryCopy(r).detail, /preserved/);
});

test("interrupted diagnostic stays uncertain with preserved payload and safe same-ID retry", () => {
	const state = { requests: [] },
		recovery = createSendRecovery(state, () => "id");
	const request = recovery.stage("A", "private draft", []);
	recovery.settle(request, { delivery: "unknown", receipt_status: "interrupted" });
	assert.equal(request.state, "uncertain");
	assert.match(recoveryCopy(request).detail, /Some actions may have completed/);
	assert.equal(recovery.retry(request, true), true);
	assert.equal(request.id, "id");
	assert.equal(request.text, "private draft");
});

for (const operation of ["check", "retry"])
	test(`old POST failure cannot supersede a newer ${operation}`, () => {
		const state = { requests: [] };
		const recovery = createSendRecovery(state, () => "x");
		const request = recovery.stage("A", "original", []);
		const old = recovery.begin(request);
		recovery.settle(request, null, old);
		if (operation === "retry") recovery.retry(request, true);
		const current = recovery.begin(request);
		request.checking = operation === "check";
		recovery.settle(request, null, old);
		assert.equal(request.state, operation === "retry" ? "sending" : "uncertain");
		assert.equal(request.operation, current);
		assert.equal(request.checking, operation === "check");
		recovery.settle(
			request,
			{
				delivery: "settled",
				result: { ok: true, conversation_id: "A", message_id: "M", run_id: "R" },
			},
			old
		);
		assert.equal(request.state, "accepted");
	});
test("off-route acceptance adopts the request and clears only empty first-send picks", () => {
	for (const newer of [false, true]) {
		let refreshed = 0;
		const state = {
			requests: [],
			drafts: { "": { text: newer ? "new task" : "", attachments: [] } },
			newChatPicks: { autoMode: true, model: "old" },
		};
		const recovery = createSendRecovery(
			state,
			() => "x",
			() => refreshed++
		);
		const request = recovery.stage("", "original", []);
		recovery.settle(request, {
			delivery: "settled",
			result: { ok: true, conversation_id: "created", message_id: "M", run_id: "R" },
		});
		assert.equal(request.conversation, "created");
		assert.equal(request.needsAdoption, true);
		assert.equal(refreshed, 1);
		assert.equal(!!state.newChatPicks.autoMode, newer);
		assert.equal(state.drafts[""]?.text, newer ? "new task" : undefined);
	}
});
