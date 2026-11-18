import { test } from "node:test";
import assert from "node:assert/strict";
import { ACTION_FIXTURES } from "../../../frontend/src/lib/actionFixtures.js";
import {
	actionBlockState,
	countDiagrams,
	gotoChipPage,
	messageChips,
	parseAction,
	parseCards,
	stripAgentBlocks,
	toolStatus,
} from "./blocks.js";

const fence = (kind, body) => "```" + kind + "\n" + body + "\n```";

// The whole point of the module: control blocks are part of the reply's raw
// text, so anything that renders it verbatim shows the user a wall of JSON.
test("stripAgentBlocks: removes every control fence from the prose", () => {
	for (const kind of [
		"jarvis-action",
		"confirm",
		"jarvis-ask",
		"jarvis-cards",
		"jarvis-skill",
		"jarvis-macro",
		"jarvis-chart",
		"jarvis-goto",
	]) {
		const out = stripAgentBlocks(`Before\n${fence(kind, '{"a":1}')}\nAfter`);
		assert.equal(out, "Before\n\nAfter", `${kind} leaked into the prose`);
	}
	assert.equal(
		stripAgentBlocks(
			"Hi\n" + fence("jarvis-goto", '{"page":"settings","prompt":"Show cash by month"}')
		),
		"Hi"
	);
});

test("stripAgentBlocks: strips both xychart charts and ordinary mermaid diagrams", () => {
	// xychart-beta is a data chart (rendered by ChartCard); it must not leak.
	const xy = stripAgentBlocks(`Text\n${fence("mermaid", "xychart-beta\n  title x")}\nEnd`);
	assert.ok(!xy.includes("xychart-beta"));
	assert.equal(xy, "Text\n\nEnd");

	// An ordinary diagram (flowchart) can't be drawn here, so it is stripped from
	// the prose too — a chip (countDiagrams) links it to the web chat instead of
	// leaving raw ``` source on screen.
	const flow = `Text\n${fence("mermaid", "graph TD;\n  A-->B;")}\nEnd`;
	const out = stripAgentBlocks(flow);
	assert.ok(!out.includes("graph TD"), "a diagram must not leak as raw source");
	assert.equal(out, "Text\n\nEnd");
});

test("countDiagrams: counts flowchart-style mermaid, ignores xychart data charts", () => {
	assert.equal(countDiagrams(`a\n${fence("mermaid", "flowchart TD\n A-->B")}\nb`), 1);
	assert.equal(countDiagrams(fence("mermaid", "xychart-beta\n bar [1,2]")), 0);
	assert.equal(countDiagrams("no diagrams here"), 0);
	assert.equal(countDiagrams(""), 0);
	assert.equal(countDiagrams(null), 0);
	const two = `${fence("mermaid", "graph TD\n A-->B")}\n\n${fence(
		"mermaid",
		"sequenceDiagram\n A->>B: hi"
	)}`;
	assert.equal(countDiagrams(two), 2);
});

test("stripAgentBlocks: leaves a plain code block alone", () => {
	const code = `Here:\n${fence("python", "print(1)")}\nDone`;
	assert.ok(stripAgentBlocks(code).includes("print(1)"));
});

test("stripAgentBlocks: collapses the gap left behind and trims", () => {
	const out = stripAgentBlocks(`A\n\n\n${fence("jarvis-action", "{}")}\n\n\nB\n\n`);
	assert.equal(out, "A\n\nB");
});

test("stripAgentBlocks: empty input is safe", () => {
	assert.equal(stripAgentBlocks(""), "");
	assert.equal(stripAgentBlocks(null), "");
	assert.equal(stripAgentBlocks(undefined), "");
});

test("parseCards: returns null rather than throwing on malformed JSON", () => {
	assert.equal(parseCards(fence("jarvis-cards", "{not json")), null);
	assert.equal(parseCards("no blocks here"), null);
	assert.equal(parseCards(""), null);
	assert.equal(parseCards(null), null);
});

// The backend writes free-text statuses, so this matches loosely on purpose.
test("toolStatus: failure words map to error", () => {
	for (const s of ["error", "Failed", "FAILURE", "err"]) {
		assert.equal(toolStatus(s), "error", `${s} should be an error`);
	}
});

test("toolStatus: in-flight words map to running", () => {
	for (const s of ["running", "started", "in progress", "pending"]) {
		assert.equal(toolStatus(s), "running", `${s} should be running`);
	}
});

test("toolStatus: anything else settles as done", () => {
	assert.equal(toolStatus("success"), "done");
	assert.equal(toolStatus("ok"), "done");
	assert.equal(toolStatus(""), "done");
	assert.equal(toolStatus(null), "done");
	assert.equal(toolStatus(undefined), "done");
});

const goto = (page, prompt = "Show cash by month") =>
	fence("jarvis-goto", JSON.stringify({ page, prompt }));

// jarvis-goto hands a request to a page of the web app. The phone cannot follow
// it, so the block must never reach the prose as raw JSON.
test("stripAgentBlocks: a goto block of either page leaves the prose and no JSON", () => {
	for (const page of ["dashboards", "payment-calendar"]) {
		const out = stripAgentBlocks(`Before\n${goto(page)}\nAfter`);
		assert.equal(out, "Before\n\nAfter", `${page} goto leaked into the prose`);
		assert.ok(!out.includes("jarvis-goto") && !out.includes("{"), `${page} JSON leaked`);
	}
});

test("gotoChipPage: the page a valid goto names, trimmed, extra keys ignored", () => {
	assert.equal(gotoChipPage(`Opening it.\n${goto("dashboards")}`), "dashboards");
	assert.equal(
		gotoChipPage(fence("jarvis-goto", '{"page":" dashboards ","prompt":" x ","y":1}')),
		"dashboards"
	);
});

// A0's rule was "no chip for payment-calendar (the phone sends no capability)". It is
// intentionally REPLACED: the lead-in the agent writes before the block would otherwise
// be a dead end on the phone, so a valid calendar goto gets the web-chat chip too.
test("gotoChipPage: a valid payment-calendar goto gets the chip as well", () => {
	assert.equal(gotoChipPage(goto("payment-calendar")), "payment-calendar");
	assert.equal(
		gotoChipPage(`Opening the Payment Calendar.\n${goto("payment-calendar", " per day ")}`),
		"payment-calendar"
	);
	assert.equal(gotoChipPage(goto("payment-calendar", "   ")), null, "blank prompt");
});

const diagram = () => fence("mermaid", "flowchart TD\nA-->B");

// One chip per kind: the diagram and the page hand-off are different things and
// used to share a single chip titled "Diagram" when both were present.
test("messageChips: a diagram and a Dashboards goto give two chips", () => {
	assert.deepEqual(messageChips(`${diagram()}\n${goto("dashboards")}`), [
		{ kind: "diagram", title: "Diagram", icon: "diagram" },
		{ kind: "goto", title: "Dashboards", icon: "chart" },
	]);
});

test("messageChips: a Payment Calendar goto has its own title and icon", () => {
	assert.deepEqual(messageChips(goto("payment-calendar")), [
		{ kind: "goto", title: "Payment Calendar", icon: "calendar" },
	]);
});

test("messageChips: a diagram alone is one chip; several diagrams are counted", () => {
	assert.deepEqual(messageChips(diagram()), [
		{ kind: "diagram", title: "Diagram", icon: "diagram" },
	]);
	assert.deepEqual(messageChips(`${diagram()}\n${diagram()}`), [
		{ kind: "diagram", title: "2 diagrams", icon: "diagram" },
	]);
});

test("messageChips: no chip for plain text, an invalid goto, or an unclosed goto", () => {
	assert.deepEqual(messageChips("just prose"), []);
	assert.deepEqual(messageChips(""), []);
	assert.deepEqual(messageChips(null), []);
	assert.deepEqual(messageChips(goto("settings")), []);
	assert.deepEqual(messageChips(goto("dashboards", "  ")), []);
	assert.deepEqual(messageChips('```jarvis-goto\n{"page":"dashboards","prompt":"x"}'), []);
	assert.deepEqual(messageChips(fence("jarvis-goto", '{"page":"dashboards"}')), []);
	assert.deepEqual(messageChips(fence("jarvis-goto", "{not json")), []);
	assert.deepEqual(messageChips(fence("jarvis-goto", "[1]")), []);
	assert.deepEqual(messageChips(fence("jarvis-goto", "null")), []);
});

test("messageChips: a chip carries no prompt or JSON from the goto block", () => {
	const chips = messageChips(goto("dashboards", "SECRET-PROMPT"));
	assert.ok(!JSON.stringify(chips).includes("SECRET-PROMPT"));
});

const wrap = (b) => "Drafting.\n\n```jarvis-action\n" + b + "\n```";

test("parseAction normalises object-shaped tables", () => {
	for (const id of ["tables-object-list", "tables-object-rows"]) {
		const a = parseAction(wrap(ACTION_FIXTURES.find((f) => f.id === id).block));
		assert.ok(Array.isArray(a.tables));
		assert.equal(a.tables[0].fieldname, "time_logs");
		assert.equal(a.tables[0].rows.length, 3);
	}
});

test("parseAction marks an unreadable table instead of dropping it", () => {
	const a = parseAction(wrap(ACTION_FIXTURES.find((f) => f.id === "tables-string").block));
	assert.equal(a.unreadable, true);
	assert.ok(Array.isArray(a.tables));
});

test("parseAction keeps stringified fields and lower-cases the verb", () => {
	const a = parseAction(wrap(ACTION_FIXTURES.find((f) => f.id === "verb-capitalised").block));
	assert.equal(a.verb, "create");
	assert.deepEqual(a.fields, [{ label: "employee", value: "HR-EMP-1" }]);
});

test("actionBlockState tells a broken closed block from no block", () => {
	assert.equal(actionBlockState("plain prose"), "none");
	assert.equal(actionBlockState(wrap('{"kind":"doc",')), "bad");
	assert.equal(actionBlockState(wrap("[1,2]")), "bad");
	assert.equal(actionBlockState('x\n```jarvis-action\n{"kind"'), "none"); // unclosed
	assert.equal(actionBlockState(wrap('{"kind":"doc","verb":"create","doctype":"Task"}')), "ok");
});

test("a half-written block is hidden while streaming", () => {
	assert.equal(
		stripAgentBlocks('Drafting.\n\n```jarvis-action\n{"kind":"do', true),
		"Drafting."
	);
	assert.equal(stripAgentBlocks("Drafting.\n\n```jarv", true), "Drafting.");
});
test("a settled reply keeps today's behaviour", () => {
	assert.equal(stripAgentBlocks("A\n\n```js\nx\n```"), "A\n\n```js\nx\n```");
});
test("streaming still strips non-xychart mermaid and closed blocks", () => {
	assert.equal(stripAgentBlocks("A\n```mermaid\nflowchart TD\nA-->B\n```\nB", true), "A\n\nB");
});
