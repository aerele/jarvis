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
	assert.match(resolve, /const actionIsCurrent = \(\) =>/);
	assert.match(resolve, /resolving\.value === token \|\| \(stream\.value\.pending \|\| \[\]\)\.some/);
	assert.match(resolve, /await confirmTool\(token, cid\);[^]*?if \(!actionIsCurrent\(\)\) return/);

	const poll = section("function startPolling()", "onMounted(()");
	assert.match(poll, /await getConversation\(cid\);\s*if \(!isCurrentView\(generation, cid\)\) return/);
});

test("Desk scroll continuations retain the view that scheduled them", () => {
	const here = path.dirname(fileURLToPath(import.meta.url));
	const panel = fs.readFileSync(path.join(here, "Panel.vue"), "utf8");
	const section = (from, to) => panel.slice(panel.indexOf(from), panel.indexOf(to, panel.indexOf(from)));

	const scroll = section("async function scrollToBottom", "// Sticky-bottom guard");
	assert.match(scroll, /await nextTick\(\);\s*if \(stillCurrent\(\) && bodyEl\.value\)/);

	const stick = section("function stickToBottom()", "// Arrow click");
	assert.match(stick, /const generation = viewGeneration;/);
	assert.match(stick, /scrollToBottom\(current\);/);

	const load = section("async function load()", "function startNewChat()");
	assert.match(
		load,
		/await scrollToBottom\(\(\) => isCurrentView\(generation, requestedConversation\)\);\s*if \(!isCurrentView\(generation, requestedConversation\)\) return/
	);

	const send = section("async function send()", "function retryLast()");
	assert.match(send, /await scrollToBottom\(\(\) => isCurrentView\(generation, sentConversation\)\);/);
});

test("Desk composer uploads and dictation cannot finish in a new chat", () => {
	const here = path.dirname(fileURLToPath(import.meta.url));
	const panel = fs.readFileSync(path.join(here, "Panel.vue"), "utf8");
	const section = (from, to) => panel.slice(panel.indexOf(from), panel.indexOf(to, panel.indexOf(from)));

	const fresh = section("function startNewChat()", "function pickFile()");
	assert.match(fresh, /abortRecording\(\);/);
	assert.match(fresh, /uploading\.value = false;/);
	assert.match(fresh, /transcribing\.value = false;/);
	assert.match(fresh, /attachments\.value = \[\];/);

	const upload = section("async function onFilePicked(e)", "function removeAttachment");
	assert.match(upload, /const generation = viewGeneration;/);
	assert.match(upload, /await uploadFile\(f\);\s*if \(!isCurrentView\(generation, conversation\)\) return/);

	const voice = section("async function toggleVoice()", "async function send()");
	assert.match(voice, /await navigator\.mediaDevices\.getUserMedia\(\{ audio: true \}\);\s*if \(!isCurrentView\(generation, conversation\)\)/);
	assert.match(voice, /await transcribeAudio\([^]*?\);\s*if \(!isCurrentView\(generation, conversation\)\) return/);
});
