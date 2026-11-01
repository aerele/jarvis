import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { advanceViewGeneration, isViewCurrent } from "./viewGeneration.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const chatView = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

function deferred() {
	let resolve;
	const promise = new Promise((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

function applyListIfCurrent(request, current, cards, next) {
	if (
		!isViewCurrent(
			request.generation,
			request.conversation,
			current.generation,
			current.conversation,
		)
	)
		return cards;
	return next;
}

function applySheetSettlementIfCurrent(action, current, pending, decision) {
	if (
		!isViewCurrent(
			action.generation,
			action.conversation,
			current.generation,
			current.conversation,
		)
	)
		return { pending, decision };
	return {
		pending: pending.filter((card) => card.token !== action.token),
		decision: decision?.token === action.token ? null : decision,
	};
}

test("a deferred A list response cannot mutate B", async () => {
	let generation = 4;
	let conversation = "A";
	let cards = [{ token: "safe-b", conversation: "B" }];
	const oldRequest = { generation, conversation };
	const response = deferred();
	const apply = response.promise.then((next) => {
		cards = applyListIfCurrent(oldRequest, { generation, conversation }, cards, next);
	});

	generation = advanceViewGeneration(generation);
	conversation = "B";
	response.resolve([{ token: "old-a" }]);
	await apply;

	assert.deepEqual(cards, [{ token: "safe-b", conversation: "B" }]);
});

test("a deferred A list response cannot mutate a later A view", async () => {
	let generation = 4;
	let conversation = "A";
	let cards = [{ token: "fresh-a", conversation: "A" }];
	const oldRequest = { generation, conversation };
	const response = deferred();
	const apply = response.promise.then((next) => {
		cards = applyListIfCurrent(oldRequest, { generation, conversation }, cards, next);
	});

	generation = advanceViewGeneration(generation);
	conversation = "B";
	generation = advanceViewGeneration(generation);
	conversation = "A";
	response.resolve([{ token: "old-a" }]);
	await apply;

	assert.deepEqual(cards, [{ token: "fresh-a", conversation: "A" }]);
});

test("an old A sheet cannot close or settle B's safe sibling", () => {
	const oldAction = { generation: 7, conversation: "A", token: "spent-a" };
	const current = { generation: 8, conversation: "B" };
	const pending = [{ token: "safe-b", conversation: "B" }];
	const decision = { token: "safe-b", conversation: "B" };
	const afterOldSettlement = applySheetSettlementIfCurrent(
		oldAction,
		current,
		pending,
		decision,
	);
	assert.deepEqual(afterOldSettlement.pending, pending);
	assert.deepEqual(afterOldSettlement.decision, decision);

	const mismatch = { ...oldAction, generation: current.generation, conversation: "A" };
	assert.equal(
		isViewCurrent(
			mismatch.generation,
			mismatch.conversation,
			current.generation,
			current.conversation,
		),
		false,
	);
});

test("realtime A events keep their scroll continuation in the A view", () => {
	const start = chatView.indexOf("function onEvent(p) {");
	assert.notEqual(start, -1, "ChatView must keep its realtime event handler");
	const end = chatView.indexOf("\nfunction ", start + 1);
	assert.notEqual(end, -1, "could not find the end of the realtime event handler");
	const body = chatView.slice(start, end);
	assert.match(body, /const generation = viewGeneration;/);
	assert.match(body, /const current = \(\) => isCurrent\(conv, generation\);/);
	assert.equal(
		(body.match(/scrollToBottom\(false, current\);/g) || []).length,
		3,
		"delta, tool, and pending-card scrolls must retain the event's view identity"
	);
});

test("a leaving A decision sheet cannot inherit B props", () => {
	const start = chatView.indexOf("<DecisionSheet");
	assert.notEqual(start, -1, "ChatView must render the decision sheet");
	const sheet = chatView.slice(start, chatView.indexOf("/>", start));
	assert.match(
		sheet,
		/:key="`\$\{decisionContext\?\.generation \?\? viewGeneration\}:\$\{decisionContext\?\.conversation \?\? ''\}:\$\{decisionContext\?\.token \?\? ''\}`"/,
		"a conversation change must remount the sheet before a leaving transition can emit"
	);
});

test("menu writes keep their optimistic result in the chat that started them", () => {
	const body = (head) => {
		const start = chatView.indexOf(head);
		assert.notEqual(start, -1, `${head} must exist`);
		return chatView.slice(start, chatView.indexOf("\n}\n", start));
	};
	for (const head of ["async function toggleStar()", "async function saveRename()"]) {
		const fn = body(head);
		assert.match(fn, /const generation = viewGeneration;/);
		assert.match(fn, /const cid = convId\.value;/);
		assert.match(fn, /if \(!isCurrent\(cid, generation\)\) return;/);
	}
});

test("other deferred chat controls keep the view that started them", () => {
	const body = (head) => {
		const start = chatView.indexOf(head);
		assert.notEqual(start, -1, `${head} must exist`);
		return chatView.slice(start, chatView.indexOf("\n}\n", start));
	};
	const cancel = body("async function cancelWaiting()");
	assert.match(cancel, /const generation = viewGeneration;/);
	assert.match(cancel, /if \(!isCurrent\(cid, generation\) \|\| recoveryState\.queued\[cid\] !== q\) return;/);

	const stop = body("async function stop()");
	assert.match(stop, /const generation = viewGeneration;/);
	assert.match(stop, /await api\.stopRun\(cid, runId\);/);
	assert.match(stop, /if \(isCurrent\(cid, generation\)\) load\(\);/);

	const attach = body("async function attach(files)");
	assert.match(attach, /const target = attachments\.value;/);
	assert.match(attach, /const row = target\.find\(\(x\) => x\.key === a\.key\);/);
	assert.match(attach, /if \(isCurrent\(cid, generation\)\)\s*errorBanner\.value/);
});
