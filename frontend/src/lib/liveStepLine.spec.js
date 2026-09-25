import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

/**
 * The live step line: while a turn runs, the model's own "what I'm doing"
 * sentence (the bench's run:step event) shows above the tool rows, and it goes
 * away once the answer lands. It is never stored, so nothing else can clean it
 * up later; these pin that it cannot outlive its run.
 *
 * Source-fenced, the same way typedConfirm.spec.js pins ChatView's wiring.
 */

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

function caseBody(name) {
	const at = src.indexOf(`case "${name}":`);
	expect(at).toBeGreaterThan(-1);
	const next = src.indexOf("\n\t\tcase ", at + 1);
	return src.slice(at, next);
}

describe("live step line", () => {
	it("is tied to the run that sent it, so every run teardown hides it", () => {
		// Each teardown resets currentRunId (run:end, run:error, stop, switch);
		// binding the line to it means none of them has to clear it by hand.
		expect(src).toContain("liveStepState.value.runId === currentRunId.value");
	});

	it("run:step is fenced like tool events and replaces the previous line", () => {
		const body = caseBody("run:step");
		expect(body).toContain("pumpFenceReject(p)");
		expect(body).toContain("toolEventIsStale(p)");
		expect(body).toContain(
			"liveStepState.value = { runId: p.run_id || currentRunId.value, text: p.text"
		);
	});

	it("real answer text clears it, the relay's empty mirror does not", () => {
		const body = caseBody("assistant:delta");
		expect(body).toContain('if (p.text) liveStepState.value = { runId: null, text: "" };');
	});

	it("keeps the activity block open while only a step is showing", () => {
		expect(src).toContain("(activeTools.length || waiting || liveStep) &&");
		expect(src).toMatch(/v-if="liveStep"\s+class="jv-livestep"/);
	});
});
