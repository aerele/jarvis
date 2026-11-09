import { test } from "node:test";
import assert from "node:assert/strict";
import {
	countDiagrams,
	gotoChipPage,
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

test("stripAgentBlocks: a goto naming an unknown page is stripped too", () => {
	assert.equal(stripAgentBlocks(`Hi\n${goto("settings")}`), "Hi");
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

test("gotoChipPage: an invalid goto gets no chip", () => {
	assert.equal(gotoChipPage(goto("settings")), null, "unknown page");
	assert.equal(gotoChipPage(goto("dashboards", "   ")), null, "blank prompt");
	assert.equal(gotoChipPage(fence("jarvis-goto", '{"page":"dashboards"}')), null);
	assert.equal(gotoChipPage(fence("jarvis-goto", "{not json")), null, "bad JSON");
	assert.equal(gotoChipPage(fence("jarvis-goto", "[1]")), null, "not an object");
	assert.equal(gotoChipPage(fence("jarvis-goto", "null")), null, "null");
	assert.equal(
		gotoChipPage('```jarvis-goto\n{"page":"dashboards","prompt":"x"}'),
		null,
		"unclosed fence"
	);
	assert.equal(gotoChipPage("no block here"), null);
	assert.equal(gotoChipPage(""), null);
	assert.equal(gotoChipPage(null), null);
});
