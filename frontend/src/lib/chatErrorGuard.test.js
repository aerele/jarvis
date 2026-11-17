// Every error a chat user sees says what went wrong in plain words (owner directive, 2026-11-15).
// This guard fails on the two idioms that put raw text in front of them: "the exception's own
// message, or else a fallback" (a stack head, "Failed to fetch", "<method> <exc_type>") and a
// machine reason code shown as-is. The server's error envelope (`r.error.message`) is written for
// customers and is allowed. A deliberate exception carries a `raw-ok:` comment on the line or
// the line above it, saying why.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../../", import.meta.url);
const CHAT_SURFACES = [
	"frontend/src/views/ChatView.vue",
	"frontend/src/pages/dashboards/DashboardChatPane.vue",
	"frontend/src/components/ActionError.vue",
	"frontend/src/components/personalise/ChatComposer.vue",
	"frontend/src/components/VoiceRecorder.vue",
	"pwa/src/views/ChatView.vue",
	"jarvis/public/js/jarvis_chat/widget/Panel.vue",
];
const RAW_IDIOMS = [
	[/\b(?:e|err|ex|exc)\??\.message\s*\|\|/, "an exception's own message as the text"],
	[/\(\s*(e|err)\s*&&\s*\1\.message\s*\)\s*\|\|/, "an exception's own message as the text"],
	[/\b(?:r|p|res)\??\.reason\s*\|\|/, "a machine reason code as the text"],
];

test("no chat surface shows raw exception text or a raw reason code", () => {
	const found = [];
	for (const file of CHAT_SURFACES) {
		const lines = readFileSync(new URL(file, ROOT), "utf8").split("\n");
		lines.forEach((line, i) => {
			if (/console\.|reportError\(|raw-ok:/.test(line) || /raw-ok:/.test(lines[i - 1] || ""))
				return;
			for (const [pattern, what] of RAW_IDIOMS)
				if (pattern.test(line)) found.push(`${file}:${i + 1} ${what}: ${line.trim()}`);
		});
	}
	assert.deepEqual(found, [], `use the plain copy helpers instead:\n${found.join("\n")}`);
});

test("the guard catches the raw idioms and lets the plain ones through", () => {
	const bad = [
		'notify(e.message || "Could not save.");',
		'toast(e?.message || "Couldn\'t upload");',
		'msg = (e && e.message) || "Couldn\'t attach that file.";',
		'notify(r.reason || "Couldn\'t retry that.");',
	];
	const good = [
		'notify(r.error.message, { type: "error" });',
		'notify(errMessage(e, "Could not save."));',
		"draftLinkError.value = e instanceof DraftLinkFilterError ? e.message : COPY;",
	];
	const hits = (line) => RAW_IDIOMS.some(([pattern]) => pattern.test(line));
	for (const line of bad) assert.ok(hits(line), `missed: ${line}`);
	for (const line of good) assert.ok(!hits(line), `false alarm: ${line}`);
});
