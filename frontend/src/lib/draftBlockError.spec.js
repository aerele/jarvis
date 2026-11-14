import { describe, it, expect } from "vitest";

import { ACTION_FIXTURES } from "./actionFixtures";
import { draftBlockError, GATED_VERBS, normaliseAction } from "./chatAction";

const COPY = "Couldn't show this draft. Ask Jarvis to send it again.";
const wrap = (block) => "Here is the draft.\n```jarvis-action\n" + block + "\n```\n";

// Derive the action the way the view does: parse, normalise, null on failure.
function actionOf(block) {
	try {
		const a = JSON.parse(block.trim());
		return a && typeof a === "object" ? normaliseAction(a) : null;
	} catch {
		return null;
	}
}

describe("draftBlockError", () => {
	for (const f of ACTION_FIXTURES) {
		const wantsError = f.expect === "error" && !["docs-batch", "create-empty"].includes(f.id);
		it(`${f.id}: ${wantsError ? "error copy" : "empty"}`, () => {
			const out = draftBlockError(wrap(f.block), actionOf(f.block), "Jarvis");
			expect(out).toBe(wantsError ? COPY : "");
		});
	}

	it("verb-gated (submit) is not an error: the waiting note shows instead", () => {
		const f = ACTION_FIXTURES.find((x) => x.id === "verb-gated");
		expect(draftBlockError(wrap(f.block), actionOf(f.block), "Jarvis")).toBe("");
		for (const verb of GATED_VERBS)
			expect(draftBlockError("", { kind: "doc", verb }, "Jarvis")).toBe("");
	});

	it("verb-unknown (draft) still shows the copy", () => {
		const f = ACTION_FIXTURES.find((x) => x.id === "verb-unknown");
		expect(draftBlockError(wrap(f.block), actionOf(f.block), "Jarvis")).toBe(COPY);
	});

	it("uses the agent name it is given", () => {
		expect(draftBlockError(wrap("{bad"), null, "Terra")).toBe(
			"Couldn't show this draft. Ask Terra to send it again."
		);
	});

	it("does not flag an email block with any verb", () => {
		const a = normaliseAction({ kind: "email", verb: "send", to: "a@b.c" });
		expect(draftBlockError(wrap(JSON.stringify(a)), a, "Jarvis")).toBe("");
	});

	it("does not flag content with no fence", () => {
		expect(draftBlockError("plain reply", null, "Jarvis")).toBe("");
	});
});
