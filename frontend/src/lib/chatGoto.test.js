// Real executable tests for the ```jarvis-goto contract (chatGoto.js), plus
// source assertions fencing how ChatView wires it up. Plain node built-ins
// (node:test + node:assert), the chatAsk.test.js / dashboardBuildCard.test.js
// convention: a .vue SFC cannot be imported into this runner, so its wiring is
// checked by reading the source text instead.
//
// What is being defended: main chat no longer builds a dashboard inline
// (jarvis#884) - it restates the request and points here with a fenced
// ```jarvis-goto block, ChatView stashes the prompt and redirects to the
// Dashboards builder. The parser is an ALLOWLIST (only "dashboards" is a
// valid destination today), never generic navigation, and the redirect must
// fire at most once per message even though the block is real UI on every
// later visit to the transcript.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseGoto, GOTO_RE } from "./chatGoto.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const chatViewSrc = fs.readFileSync(path.join(HERE, "..", "views", "ChatView.vue"), "utf8");

const fence = (body) => "Some lead-in.\n\n```jarvis-goto\n" + body + "\n```";

const fnBody = (src, decl) => {
	const start = src.indexOf(decl);
	assert.notEqual(start, -1, `source must still define ${decl}`);
	const end = src.indexOf("\n}", start + decl.length);
	assert.notEqual(end, -1, `${decl} must close at the top level`);
	return src.slice(start, end);
};

// ---- parsing ---------------------------------------------------------------

test("parses a valid dashboards redirect", () => {
	const goto = parseGoto(fence('{"page":"dashboards","prompt":"Monthly sales by territory"}'));
	assert.deepEqual(goto, { page: "dashboards", prompt: "Monthly sales by territory" });
});

test("only dashboards is allowlisted - any other page parses to null", () => {
	assert.equal(
		parseGoto(fence('{"page":"triggers","prompt":"Warn me on overdue invoices"}')),
		null
	);
	assert.equal(parseGoto(fence('{"page":"skills","prompt":"x"}')), null);
	assert.equal(parseGoto(fence('{"prompt":"no page at all"}')), null);
	assert.equal(parseGoto(fence('{"page":"","prompt":"blank page"}')), null);
});

test("prompt must be present and non-blank", () => {
	assert.equal(parseGoto(fence('{"page":"dashboards"}')), null);
	assert.equal(parseGoto(fence('{"page":"dashboards","prompt":""}')), null);
	assert.equal(parseGoto(fence('{"page":"dashboards","prompt":"   "}')), null);
});

test("no block, malformed JSON, and a non-object payload all parse to null", () => {
	assert.equal(parseGoto("just prose"), null);
	assert.equal(parseGoto(""), null);
	assert.equal(parseGoto(null), null);
	assert.equal(parseGoto(fence("{not json")), null);
	assert.equal(parseGoto(fence('"a string"')), null);
	assert.equal(parseGoto(fence("[1,2,3]")), null);
});

test("an unterminated block does not parse (a half-streamed reply shows no card)", () => {
	assert.equal(parseGoto('```jarvis-goto\n{"page":"dashboards","prompt":"x"'), null);
});

test("GOTO_RE matches exactly the fenced block chatBlocks.js strips", () => {
	assert.match(fence('{"page":"dashboards","prompt":"x"}'), GOTO_RE);
});

// ---- source fences: BLOCK_TAGS, the card, and the one-shot auto-redirect --

test("chatBlocks strips jarvis-goto from the visible prose", () => {
	const blocksSrc = fs.readFileSync(path.join(HERE, "chatBlocks.js"), "utf8");
	assert.match(blocksSrc, /"jarvis-goto",/);
});

test("ChatView parses goto through the shared module, not a private copy", () => {
	assert.match(chatViewSrc, /import \{ parseGoto \} from "@\/lib\/chatGoto";/);
	const gotoOf = fnBody(chatViewSrc, "function gotoOf(m)");
	assert.match(gotoOf, /return parseGoto\(\(m && m\.content\) \|\| ""\);/);
});

test("the card renders the restated prompt and one button that hands off to the builder", () => {
	assert.match(chatViewSrc, /v-if="gotoOf\(m\)" class="jv-macrocard"/);
	assert.match(chatViewSrc, /Continue in Dashboards/);
	// prettier may wrap the interpolation onto its own line, so match loosely
	assert.match(chatViewSrc, /\{\{\s*gotoOf\(m\)\.prompt\s*\}\}/);
	assert.match(chatViewSrc, /@click="gotoDashboards\(gotoOf\(m\)\.prompt\)"/);
});

test("gotoDashboards stashes the prefill and navigates to the builder", () => {
	assert.match(
		chatViewSrc,
		/import \{ setDashboardPrefill \} from "@\/composables\/dashboardPrefill";/
	);
	const fn = fnBody(chatViewSrc, "function gotoDashboards(prompt)");
	assert.match(fn, /setDashboardPrefill\(\{ text: prompt, autoSend: true \}\);/);
	assert.match(fn, /router\.push\("\/dashboards"\);/);
});

test("run:end auto-fires the redirect at most once, and never for an errored or stopped turn", () => {
	const runEnd = chatViewSrc.slice(
		chatViewSrc.indexOf('case "run:end": {'),
		chatViewSrc.indexOf('case "message:enriched": {')
	);
	assert.match(runEnd, /if \(m && !m\.error && !m\.stopped\) \{/);
	assert.match(runEnd, /const goto = gotoOf\(m\);/);
	// durable stamp, keyed per message, checked before it is set
	assert.match(runEnd, /const firedKey = "jarvis:goto-fired:" \+ m\.name;/);
	const stampIdx = runEnd.indexOf("localStorage.getItem(firedKey)");
	const setIdx = runEnd.indexOf("localStorage.setItem(firedKey,");
	const gotoIdx = runEnd.indexOf("gotoDashboards(goto.prompt);");
	assert.notEqual(stampIdx, -1);
	assert.ok(stampIdx < setIdx, "the stamp must be checked before it is written");
	assert.ok(setIdx < gotoIdx, "the stamp must be written before the redirect fires");
});
