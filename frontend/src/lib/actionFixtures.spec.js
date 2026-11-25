import { describe, it, expect } from "vitest";

import { ACTION_FIXTURES } from "./actionFixtures";
import { actionBlockState, draftBlockError, normaliseAction } from "./chatAction";

const parse = (f) => {
	try {
		return JSON.parse(f.block);
	} catch {
		return undefined;
	}
};

const fence = (f) => "```jarvis-action\n" + f.block + "\n```";

describe("ACTION_FIXTURES through normaliseAction", () => {
	const valid = ACTION_FIXTURES.filter((f) => parse(f) !== undefined);

	it("never throws on any fixture", () => {
		for (const f of valid) {
			expect(() => normaliseAction(parse(f)), f.id).not.toThrow();
		}
	});

	it("json-array is not an action", () => {
		expect(
			normaliseAction(parse(ACTION_FIXTURES.find((f) => f.id === "json-array")))
		).toBeNull();
	});

	it("canonicalises every table shape to an array with all rows", () => {
		for (const f of ACTION_FIXTURES.filter((x) => x.tableRows)) {
			const a = normaliseAction(parse(f));
			expect(Array.isArray(a.tables), f.id).toBe(true);
			expect(a.tables[0].rows.length, f.id).toBe(f.tableRows);
		}
	});

	it("no card fixture is unreadable", () => {
		for (const f of valid.filter((x) => x.expect === "card")) {
			expect(normaliseAction(parse(f)).unreadable, f.id).toBeUndefined();
		}
	});

	it("every none fixture is a readable, valid action", () => {
		const none = valid.filter((x) => x.expect === "none");
		expect(none.length).toBeGreaterThan(0);
		for (const f of none) {
			const a = normaliseAction(parse(f));
			expect(a, f.id).not.toBeNull();
			expect(a.unreadable, f.id).toBeUndefined();
		}
	});

	it("every fixture expects card, error or none", () => {
		for (const f of ACTION_FIXTURES)
			expect(["card", "error", "none"], f.id).toContain(f.expect);
	});

	it("every error fixture is rejected for a real, asserted reason", () => {
		for (const f of ACTION_FIXTURES.filter((x) => x.expect === "error")) {
			if (f.id === "json-broken" || f.id === "json-array") {
				expect(actionBlockState(fence(f)), f.id).toBe("bad");
				continue;
			}
			const a = normaliseAction(parse(f));
			if (f.id === "verb-unknown") {
				expect(a.unreadable, f.id).toBeUndefined();
				expect(draftBlockError(fence(f), a, "Jarvis"), f.id).not.toBe("");
			} else if (f.id === "docs-batch") {
				// Rejected by buildDraftModel (ChatView.vue) on desktop and by actionCardView
				// `invalid` on the phone (asserted in pwa/src/lib/actionCardView.test.js).
				expect(Array.isArray(a.docs), f.id).toBe(true);
				expect(a.unreadable, f.id).toBeUndefined();
			} else if (f.id === "create-empty") {
				// Same user-visible source as docs-batch: an empty create has nothing to draw.
				expect(a.verb, f.id).toBe("create");
				expect((a.fields || []).length + (a.tables || []).length, f.id).toBe(0);
				expect(a.unreadable, f.id).toBeUndefined();
			} else {
				expect(a.unreadable, f.id).toBe(true);
				expect(draftBlockError(fence(f), a, "Jarvis"), f.id).not.toBe("");
			}
		}
	});

	it("leaves tables and fields as arrays so no consumer can crash iterating them", () => {
		for (const f of valid.filter((x) => x.id !== "json-array")) {
			const a = normaliseAction(parse(f));
			if (a.tables !== undefined) expect(Array.isArray(a.tables), f.id).toBe(true);
			if (a.fields !== undefined) expect(Array.isArray(a.fields), f.id).toBe(true);
		}
	});

	it("lower-cases a capitalised verb and leaves a missing one missing", () => {
		const cap = normaliseAction(
			parse(ACTION_FIXTURES.find((f) => f.id === "verb-capitalised"))
		);
		expect(cap.verb).toBe("create");
		const none = normaliseAction(parse(ACTION_FIXTURES.find((f) => f.id === "verb-missing")));
		expect(none.verb).toBeUndefined();
	});
});
