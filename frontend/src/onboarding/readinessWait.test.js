import test from "node:test";
import assert from "node:assert/strict";

import { readinessWaitExhaustedMessage } from "./readinessWait.js";

test("no verdict ever landed: says so, never claims progress", () => {
	const msg = readinessWaitExhaustedMessage({ sawVerdict: false, detail: "" });
	assert.match(msg, /couldn't reach your workspace/i);
	assert.doesNotMatch(msg, /still finishing on its own/i);
});

test("a verdict landed and carried a detail: quotes it, still no self-healing claim", () => {
	const msg = readinessWaitExhaustedMessage({
		sawVerdict: true,
		detail: "applying your LLM configuration",
	});
	assert.match(msg, /applying your LLM configuration/);
	assert.doesNotMatch(msg, /still finishing on its own/i);
});

test("a verdict landed with no detail: honest fallback, still no self-healing claim", () => {
	const msg = readinessWaitExhaustedMessage({ sawVerdict: true, detail: "" });
	assert.match(msg, /haven't been able to confirm/i);
	assert.doesNotMatch(msg, /still finishing on its own/i);
});

test("missing input defaults to the never-heard-from-admin case", () => {
	const msg = readinessWaitExhaustedMessage();
	assert.match(msg, /couldn't reach your workspace/i);
});

test("a whitespace-only detail is treated as no detail", () => {
	const msg = readinessWaitExhaustedMessage({ sawVerdict: true, detail: "   " });
	assert.match(msg, /haven't been able to confirm/i);
});

test("an unpunctuated detail is closed so it does not run into the next sentence", () => {
	const msg = readinessWaitExhaustedMessage({
		sawVerdict: true,
		detail: "applying your LLM configuration",
	});
	assert.match(msg, /applying your LLM configuration\. We haven't/);
});

test("a detail that already ends in punctuation is not double-punctuated", () => {
	for (const d of ["it is applying.", "are you there?", "stop!"]) {
		const msg = readinessWaitExhaustedMessage({ sawVerdict: true, detail: d });
		assert.match(msg, new RegExp(`${d.slice(-1).replace(/[.?!]/, "\\$&")} We haven't`));
		assert.doesNotMatch(msg, /[.!?]\. We haven't/);
	}
});

test("never asserts anything is still finishing on its own, in any branch", () => {
	const cases = [
		{ sawVerdict: false, detail: "" },
		{ sawVerdict: false, detail: "applying your LLM configuration" },
		{ sawVerdict: true, detail: "" },
		{ sawVerdict: true, detail: "applying your LLM configuration" },
	];
	for (const c of cases) {
		assert.doesNotMatch(readinessWaitExhaustedMessage(c), /finishing on its own/i);
	}
});
