import { test } from "node:test";
import assert from "node:assert/strict";
import { parseCards, stripAgentBlocks, toolStatus } from "./blocks.js";

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

test("stripAgentBlocks: strips an xychart mermaid block but keeps ordinary mermaid", () => {
	const xy = stripAgentBlocks(`Text\n${fence("mermaid", "xychart-beta\n  title x")}\nEnd`);
	assert.ok(!xy.includes("xychart-beta"));

	const flow = `Text\n${fence("mermaid", "graph TD;\n  A-->B;")}\nEnd`;
	assert.ok(stripAgentBlocks(flow).includes("graph TD"), "a normal diagram must survive");
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
