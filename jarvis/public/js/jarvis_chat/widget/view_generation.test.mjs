import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { advanceViewGeneration, isViewCurrent } from "./view_generation.mjs";

function deferred() {
	let resolve;
	const promise = new Promise((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

test("a delayed old response cannot repopulate a new Desk chat", async () => {
	let generation = 0;
	let conversation = "conversation-a";
	let pendingCards = [];
	const oldRequest = { generation, conversation };
	const response = deferred();

	const applyOldResponse = response.promise.then((cards) => {
		if (!isViewCurrent(oldRequest.generation, oldRequest.conversation, generation, conversation))
			return;
		pendingCards = cards;
	});

	// This is the state transition in Panel.vue's startNewChat().
	generation = advanceViewGeneration(generation);
	conversation = "";
	response.resolve([{ token: "old-a" }]);
	await applyOldResponse;

	assert.deepEqual(pendingCards, []);
});

test("a fresh view can use the same conversation id after the old response is fenced", () => {
	const oldGeneration = 4;
	const freshGeneration = advanceViewGeneration(oldGeneration);
	assert.equal(isViewCurrent(oldGeneration, "conversation-a", freshGeneration, "conversation-a"), false);
	assert.equal(isViewCurrent(freshGeneration, "conversation-a", freshGeneration, "conversation-a"), true);
});

test("Panel fences each old read, send, confirmation, and poll continuation", () => {
	const here = path.dirname(fileURLToPath(import.meta.url));
	const panel = fs.readFileSync(path.join(here, "Panel.vue"), "utf8");
	const section = (from, to) => panel.slice(panel.indexOf(from), panel.indexOf(to, panel.indexOf(from)));

	const newChat = section("function startNewChat()", "function pickFile()");
	assert.match(newChat, /viewGeneration = advanceViewGeneration\(viewGeneration\)/);

	const resync = section("async function resyncPending(source)", "// Auto-heal");
	assert.match(resync, /const generation = viewGeneration/);
	assert.match(resync, /await listPendingConfirmations\(cid, source\);\s*\/\/[^]*?!isCurrentView\(generation, cid\)/);

	const load = section("async function load()", "function startNewChat()");
	assert.match(load, /await listConversations\(\);\s*if \(viewGeneration !== generation \|\| convId\.value\) return/);
	assert.match(load, /await getConversation\(requestedConversation\);\s*if \(!isCurrentView\(generation, requestedConversation\)\) return/);
	assert.match(load, /await resyncPending\(\);\s*if \(!isCurrentView\(generation, requestedConversation\)\) return/);

	const send = section("async function send()", "function retryLast()");
	assert.match(send, /await sendMessage\(sentConversation,[^]*?\);\s*if \(!isCurrentView\(generation, sentConversation\)\) return/);

	const resolve = section("async function resolvePending(token)", "function dismissSettledNotice()");
	assert.match(resolve, /await confirmTool\(token, cid\);\s*if \(!isCurrentView\(generation, cid\)\) return/);

	const poll = section("function startPolling()", "onMounted(()");
	assert.match(poll, /await getConversation\(cid\);\s*if \(!isCurrentView\(generation, cid\)\) return/);
});
