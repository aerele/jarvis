// Real executable tests for the per-reply model-attribution rule (jarvis#560),
// plus source assertions fencing the ONE surface that renders it. Plain node
// built-ins (node:test + node:assert), like eventFence.test.js. Run directly
// (`node --test modelBadge.test.js`) or via the python suite
// (jarvis/tests/test_model_badge_client.py subprocess-runs it every CI run).
//
// What is being defended: the server can stamp the model that actually answered
// on every assistant row and the feature still fails if the transcript never
// shows it. The rule below is deliberately quiet, hiding the badge whenever a
// reply matches what the chat is set to now, so a bug that hides one case too
// many is INVISIBLE. These tests pin the cases that must speak.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { currentThreadModel, modelBadgeFor, modelBadgeTitleFor } from "./modelBadge.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const read = (...p) => fs.readFileSync(path.join(HERE, ...p), "utf8");
const chatViewSrc = read("..", "views", "ChatView.vue");

const reply = (model, extra = {}) => ({ role: "assistant", model, ...extra });
const ask = (text = "hi") => ({ role: "user", content: text });

// ---- what "right now" means ---------------------------------------------

test("a pin is what the chat is set to, whatever the transcript says", () => {
	const msgs = [ask(), reply("glm-4.7"), ask(), reply("gemini-3.6-flash")];
	assert.equal(currentThreadModel(msgs, "claude-opus-4-6"), "claude-opus-4-6");
});

test("on Auto, the newest attributed reply is what the thread resolves to", () => {
	const msgs = [ask(), reply("gemini-3.6-flash"), ask(), reply("glm-4.7")];
	assert.equal(currentThreadModel(msgs, ""), "glm-4.7");
});

test("unattributed rows are skipped when resolving Auto", () => {
	// Legacy rows carry no model, and tool/user rows never do; neither may be
	// mistaken for "the thread is running on nothing".
	const msgs = [reply("glm-4.7"), { role: "tool", tool_name: "get_list" }, reply("")];
	assert.equal(currentThreadModel(msgs, ""), "glm-4.7");
});

test("an empty or missing transcript resolves to nothing rather than throwing", () => {
	assert.equal(currentThreadModel([], ""), "");
	assert.equal(currentThreadModel(undefined, ""), "");
});

// ---- when the badge shows ------------------------------------------------

test("a steady thread is completely silent", () => {
	const msgs = [ask(), reply("glm-4.7"), ask(), reply("glm-4.7")];
	const now = currentThreadModel(msgs, "glm-4.7");
	assert.deepEqual(
		msgs.map((m) => modelBadgeFor(m, now)),
		["", "", "", ""]
	);
});

test("a mid-thread switch makes the older replies name their model", () => {
	// The user answered on gemini, then pinned glm. The gemini reply is no longer
	// what the chat is set to, and the transcript must say so.
	const msgs = [ask(), reply("gemini-3.6-flash"), ask(), reply("glm-4.7")];
	const now = currentThreadModel(msgs, "glm-4.7");
	assert.equal(modelBadgeFor(msgs[1], now), "gemini-3.6-flash");
	assert.equal(modelBadgeFor(msgs[3], now), "");
});

test("an Auto thread that failed over names the substitute", () => {
	// The pool answered turn 2 with glm after gemini failed. Nothing else in the
	// product records the substitution, so this badge is the only trace.
	const msgs = [
		ask(),
		reply("gemini-3.6-flash"),
		ask(),
		reply("glm-4.7"),
		ask(),
		reply("gemini-3.6-flash"),
	];
	const now = currentThreadModel(msgs, "");
	assert.equal(now, "gemini-3.6-flash");
	assert.equal(modelBadgeFor(msgs[3], now), "glm-4.7", "the failed-over turn speaks");
	assert.equal(modelBadgeFor(msgs[1], now), "");
	assert.equal(modelBadgeFor(msgs[5], now), "");
});

test("only assistant rows are ever badged", () => {
	// The server refuses to stamp a non-assistant row, so a badge on one would
	// mean the transcript disagrees with the database.
	const now = "glm-4.7";
	assert.equal(modelBadgeFor({ role: "user", model: "gemini-3.6-flash" }, now), "");
	assert.equal(modelBadgeFor({ role: "tool", model: "gemini-3.6-flash" }, now), "");
	assert.equal(modelBadgeFor(null, now), "");
});

test("an unattributed reply shows nothing rather than guessing", () => {
	// Legacy rows and turns whose session row never named a model.
	assert.equal(modelBadgeFor(reply(""), "glm-4.7"), "");
	assert.equal(modelBadgeFor({ role: "assistant" }, "glm-4.7"), "");
});

// ---- tooltip -------------------------------------------------------------

test("the tooltip names the provider when there is one", () => {
	const t = modelBadgeTitleFor(reply("glm-4.7", { provider: "openai_compat" }));
	assert.match(t, /glm-4\.7/);
	assert.match(t, /via openai_compat/);
});

test("the tooltip omits the provider clause when it is unknown", () => {
	const t = modelBadgeTitleFor(reply("glm-4.7"));
	assert.match(t, /glm-4\.7/);
	assert.doesNotMatch(t, /via/);
});

// ---- source fences on the ONE renderer -----------------------------------

test("ChatView renders the badge through this module, not a forked copy", () => {
	assert.match(
		chatViewSrc,
		/import \{[^}]*modelBadgeFor[^}]*\} from "@\/utils\/modelBadge"/,
		"ChatView must import the rule rather than re-implement it"
	);
	assert.match(chatViewSrc, /v-if="modelBadgeOf\(m\)"/, "the badge is gated on the rule");
});

test("the badge sits in the reply's existing meta row", () => {
	// jv-* custom properties are not on :root and have no fallbacks, so new
	// markup has to live inside an element the palette binding already reaches.
	// The meta row does; a floating sibling would render unstyled.
	assert.match(
		chatViewSrc,
		/toolCountOf\(m\) \|\| elapsedOf\(m\) \|\| modelBadgeOf\(m\)/,
		"the meta row must open when the badge is the only thing to show"
	);
	assert.match(chatViewSrc, /class="jv-modelchip"/);
	assert.match(chatViewSrc, /\.jv-modelchip \{/, "the chip carries its own palette-bound style");
});

test("the transcript endpoint is asked for the attribution fields", () => {
	// The chip cannot render off fields get_conversation does not return.
	const apiSrc = read("..", "..", "..", "jarvis", "chat", "api.py");
	const fields = apiSrc.slice(apiSrc.indexOf("def get_conversation"));
	assert.match(fields.slice(0, 2000), /"model",/);
	assert.match(fields.slice(0, 2000), /"provider",/);
});
