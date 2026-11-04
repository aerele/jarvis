// A macro run in the phone app: its chat is not in the chat list, so each step must
// not buzz or dot like a reply, and the run's end is the one signal (macro:done).
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { isMacroConversation, macroDoneEntry, noteMacroConversation } from "./macroFeed.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const appSrc = fs.readFileSync(path.join(HERE, "..", "App.vue"), "utf8");
const notificationsSrc = fs.readFileSync(path.join(HERE, "notifications.js"), "utf8");

test("a conversation is a macro's once a progress or done event names it", () => {
	assert.equal(isMacroConversation("conv-m1"), false);
	noteMacroConversation("conv-m1");
	assert.equal(isMacroConversation("conv-m1"), true);
	assert.equal(isMacroConversation(""), false);
	assert.equal(isMacroConversation(undefined), false);
});

test("the remembered set is bounded", () => {
	noteMacroConversation("conv-first");
	for (let i = 0; i < 250; i++) noteMacroConversation(`conv-fill-${i}`);
	assert.equal(isMacroConversation("conv-first"), false);
	assert.equal(isMacroConversation("conv-fill-249"), true);
});

test("a finished run and a failed run are told, with the macro's name", () => {
	assert.deepEqual(macroDoneEntry({ status: "completed", macro_name: "Close", error: "" }), {
		title: "Macro finished: Close",
		body: "Open the chat to see what it found or did.",
	});
	assert.deepEqual(
		macroDoneEntry({ status: "failed", macro_name: "Close", error: "Step 2 failed." }),
		{
			title: "Macro failed: Close",
			body: "Step 2 failed.",
		}
	);
	assert.equal(macroDoneEntry({ status: "completed" }).title, "Macro finished");
});

test("a stopped run is not told: the owner's own Stop, or a card that already asked", () => {
	assert.equal(macroDoneEntry({ status: "stopped", error: "" }), null);
	assert.equal(macroDoneEntry({ status: "stopped", error: "Step 1 is waiting." }), null);
});

test("the feed records the run's end and skips its steps", () => {
	assert.match(
		notificationsSrc,
		/e\.kind === "macro:progress"[\s\S]*?noteMacroConversation\(conv\)/
	);
	assert.match(
		notificationsSrc,
		/e\.kind === "macro:done"[\s\S]*?id: `macro:\$\{e\.macro_run\}`/
	);
	const runEnd = notificationsSrc.slice(notificationsSrc.indexOf('e.kind === "run:end"'));
	assert.match(runEnd.slice(0, 120), /if \(macroStep\) return;/);
});

test("the shell neither dots nor buzzes for a macro step, and buzzes for its end", () => {
	assert.match(appSrc, /p\.kind === "run:end" && !p\.stopped && !isMacroConversation\(conv\)/);
	assert.match(
		appSrc,
		/p\.kind === "macro:done"[\s\S]*?macroDoneEntry\(p\)[\s\S]*?prefs\.notifyDone/
	);
	// recordEvent runs first, so the progress event is noted before the step's end.
	assert.ok(appSrc.indexOf("recordEvent(p);") < appSrc.indexOf('p.kind === "macro:done"'));
});
