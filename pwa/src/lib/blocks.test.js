import { test } from "node:test";
import assert from "node:assert/strict";
import { countDiagrams, parseCards, stripAgentBlocks, toolStatus } from "./blocks.js";

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
