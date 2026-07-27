// Real executable tests for the shared ```jarvis-ask contract (chatAsk.js), plus
// source assertions fencing the ONE renderer it feeds. Plain node built-ins
// (node:test + node:assert), like eventFence.test.js / voiceSendGlue.test.js.
// Run directly (`node --test chatAsk.test.js`) or via the python suite
// (jarvis/tests/test_chat_ask_client.py subprocess-runs it every CI run).
//
// What is being defended: the clarifying-question cards used to exist only
// inside ChatView, and the Dashboards builder pane DELETED the block instead of
// rendering it - so an agent that asked before building produced an empty
// bubble the user could not answer. The parsing/readiness/answer rules now live
// here and the cards live in <AskCard>, so both surfaces behave identically. A
// fork of either would silently drift, which is what the source fences catch.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseAsk, isAskReady, askAnswerText, ASK_FIELD_TYPES } from "./chatAsk.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const read = (...p) => fs.readFileSync(path.join(HERE, ...p), "utf8");
const askCardSrc = read("..", "components", "chat", "AskCard.vue");
const chatViewSrc = read("..", "views", "ChatView.vue");
const dashPaneSrc = read("..", "pages", "dashboards", "DashboardChatPane.vue");

const fence = (body) => "Some lead-in.\n\n```jarvis-ask\n" + body + "\n```";

// ---- parsing -------------------------------------------------------------

test("parses a bare array of questions and defaults the shape", () => {
	const spec = parseAsk(
		fence('[{"q":"Which records?","type":"multi","options":["Draft","Submitted"]}]')
	);
	assert.deepEqual(spec, {
		questions: [
			{ q: "Which records?", type: "multi", options: ["Draft", "Submitted"], doctype: "" },
		],
	});
});

test("accepts the {questions: [...]} envelope as well as a bare array", () => {
	const spec = parseAsk(fence('{"questions":[{"q":"Go?","type":"yesno"}]}'));
	assert.equal(spec.questions.length, 1);
	assert.equal(spec.questions[0].type, "yesno");
});

test("boolean is normalised to yesno and an unknown type falls back to single", () => {
	const spec = parseAsk(
		fence(
			'[{"q":"A","type":"boolean"},' +
				'{"q":"B","type":"wat","options":["x"]},' +
				'{"q":"C","type":"link","link":"Customer"}]'
		)
	);
	assert.deepEqual(
		spec.questions.map((q) => q.type),
		["yesno", "single", "link"]
	);
	// `link` is accepted under either key; only link questions keep a doctype.
	assert.equal(spec.questions[2].doctype, "Customer");
	assert.equal(spec.questions[0].doctype, "");
});

test("choice questions with no options are dropped; field types survive without them", () => {
	const spec = parseAsk(fence('[{"q":"No opts","type":"single"},{"q":"When?","type":"date"}]'));
	assert.deepEqual(
		spec.questions.map((q) => q.q),
		["When?"]
	);
});

test("caps: 6 questions, 8 options; empty prompts dropped; all-dropped yields null", () => {
	const many = Array.from({ length: 9 }, (_, i) => ({
		q: `Q${i}`,
		type: "single",
		options: Array.from({ length: 12 }, (_, o) => `o${o}`),
	}));
	const spec = parseAsk(fence(JSON.stringify(many)));
	assert.equal(spec.questions.length, 6);
	assert.equal(spec.questions[0].options.length, 8);
	assert.equal(parseAsk(fence('[{"q":"  ","type":"yesno"}]')), null);
});

test("no block, malformed JSON, and a non-list payload all parse to null", () => {
	assert.equal(parseAsk("just prose"), null);
	assert.equal(parseAsk(""), null);
	assert.equal(parseAsk(null), null);
	assert.equal(parseAsk(fence("{not json")), null);
	assert.equal(parseAsk(fence('"a string"')), null);
});

test("an unterminated block does not parse (a half-streamed reply shows no card)", () => {
	assert.equal(parseAsk('```jarvis-ask\n[{"q":"Which records?","type":"yesno"}]'), null);
});

// ---- readiness -----------------------------------------------------------

const twoQ = parseAsk(
	fence('[{"q":"Scope?","type":"single","options":["All","Mine"]},{"q":"When?","type":"date"}]')
);

test("readiness needs every question answered", () => {
	assert.equal(isAskReady(twoQ, {}, {}), false);
	assert.equal(isAskReady(twoQ, { 0: "All" }, {}), false);
	assert.equal(isAskReady(twoQ, { 0: "All", 1: "2026-07-27" }, {}), true);
	assert.equal(isAskReady(null, {}, {}), false);
});

test('"Other…" alone answers a choice question but never a field question', () => {
	assert.equal(isAskReady(twoQ, { 1: "2026-07-27" }, { 0: "Something else" }), true);
	// whitespace is not an answer
	assert.equal(isAskReady(twoQ, { 1: "2026-07-27" }, { 0: "   " }), false);
	// a field question cannot be satisfied by the Other box (it has none)
	assert.equal(isAskReady(twoQ, { 0: "All" }, { 1: "next friday" }), false);
});

test("multi needs at least one pick (an empty array is not an answer)", () => {
	const spec = parseAsk(fence('[{"q":"Which?","type":"multi","options":["a","b"]}]'));
	assert.equal(isAskReady(spec, { 0: [] }, {}), false);
	assert.equal(isAskReady(spec, { 0: ["a"] }, {}), true);
});

// ---- answer formatting ---------------------------------------------------

test("answers render as a numbered list the agent can read back", () => {
	const text = askAnswerText(twoQ, { 0: "All", 1: "2026-07-27" }, {});
	assert.equal(text, "Here are my answers:\n1. Scope? → All\n2. When? → 2026-07-27");
});

test("multi picks join with commas; a single-answer Other REPLACES a stale pick", () => {
	const spec = parseAsk(
		fence(
			'[{"q":"Which?","type":"multi","options":["a","b"]},' +
				'{"q":"Scope?","type":"single","options":["All"]}]'
		)
	);
	const text = askAnswerText(spec, { 0: ["a", "b"], 1: "All" }, { 1: "just EU" });
	// The UI keeps pick and Other exclusive; if stale state ever ships both, the
	// typed text wins and the pick is dropped (sending both was a reported bug).
	assert.equal(text, "Here are my answers:\n1. Which? → a, b\n2. Scope? → just EU");
});

test("multi keeps BOTH its picks and its Other text", () => {
	const spec = parseAsk(fence('[{"q":"Which?","type":"multi","options":["a","b"]}]'));
	assert.equal(
		askAnswerText(spec, { 0: ["a"] }, { 0: "and c" }),
		"Here are my answers:\n1. Which? → a, and c"
	);
});

test("an unanswered question is spelled out, never sent as an empty arrow", () => {
	assert.match(askAnswerText(twoQ, {}, {}), /1\. Scope\? → \(no answer\)/);
});

test("ASK_FIELD_TYPES is the value-typed set (no option buttons)", () => {
	assert.deepEqual([...ASK_FIELD_TYPES].sort(), ["date", "datetime", "link", "text"]);
});

// ---- source fences: one parser, one renderer, two surfaces ---------------

test("AskCard is the renderer and it reads its rules from chatAsk", () => {
	assert.match(askCardSrc, /from "@\/lib\/chatAsk"/);
	assert.match(askCardSrc, /isAskReady/);
	assert.match(askCardSrc, /askAnswerText/);
	assert.match(askCardSrc, /class="jv-ask-submit"/);
	assert.match(askCardSrc, /Submit answers/);
	// It must not re-implement the parser it is handed a spec from.
	assert.doesNotMatch(askCardSrc, /jarvis-ask\[ \\t\]/);
});

test("ChatView renders AskCard instead of its own copy of the cards", () => {
	assert.match(chatViewSrc, /import AskCard from "@\/components\/chat\/AskCard\.vue"/);
	assert.match(chatViewSrc, /import \{ parseAsk \} from "@\/lib\/chatAsk"/);
	assert.match(chatViewSrc, /<AskCard[\s\S]{0,200}:spec="activeAsk"/);
	// the extracted duplicates are gone
	for (const dead of ["askSel", "askOther", "submitAsk", "askReady", "_ASK_RE"]) {
		assert.doesNotMatch(chatViewSrc, new RegExp(dead), `ChatView still defines ${dead}`);
	}
});

test("the Dashboards builder pane RENDERS the ask instead of swallowing it", () => {
	assert.match(dashPaneSrc, /import AskCard from "@\/components\/chat\/AskCard\.vue"/);
	assert.match(dashPaneSrc, /<AskCard[\s\S]{0,240}:spec="activeAsk"/);
	// Answers go back into the SAME conversation as an ordinary user message.
	assert.match(dashPaneSrc, /@submit="sendText"/);
	// AskCard styles with the jv-* tokens, which this frappe-ui page must bind.
	assert.match(dashPaneSrc, /useJarvisTheme/);
	assert.match(dashPaneSrc, /<AskCard[\s\S]{0,240}:style="paletteVars"/);
	// Both surfaces key the card by message so a new ask starts a clean draft.
	assert.match(dashPaneSrc, /<AskCard[\s\S]{0,240}:key="m\.name"/);
	assert.match(chatViewSrc, /<AskCard[\s\S]{0,200}:key="m\.name"/);
});
