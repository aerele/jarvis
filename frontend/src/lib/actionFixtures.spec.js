import { describe, it, expect } from "vitest";

import { ACTION_FIXTURES } from "./actionFixtures";
import { normaliseAction } from "./chatAction";

const parse = (f) => {
	try {
		return JSON.parse(f.block);
	} catch {
		return undefined;
	}
};

// Rejected by the draft-model rules (verb allow-list, empty create, batch docs),
// not by normaliseAction.
const MODEL_REJECTED = new Set(["verb-unknown", "docs-batch", "create-empty"]);

describe("ACTION_FIXTURES through normaliseAction", () => {
	const valid = ACTION_FIXTURES.filter((f) => parse(f) !== undefined);

	it("never throws on any fixture", () => {
		for (const f of valid) {
			expect(() => normaliseAction(parse(f)), f.id).not.toThrow();
		}
	});

	it("json-array is not an action", () => {
		expect(normaliseAction(parse(ACTION_FIXTURES.find((f) => f.id === "json-array")))).toBeNull();
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
		for (const f of ACTION_FIXTURES) expect(["card", "error", "none"], f.id).toContain(f.expect);
	});

	it("every error fixture is unreadable or rejected by the model rules", () => {
		for (const f of valid.filter((x) => x.expect === "error" && x.id !== "json-array")) {
			const a = normaliseAction(parse(f));
			expect(a.unreadable === true || MODEL_REJECTED.has(f.id), f.id).toBe(true);
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
		const cap = normaliseAction(parse(ACTION_FIXTURES.find((f) => f.id === "verb-capitalised")));
		expect(cap.verb).toBe("create");
		const none = normaliseAction(parse(ACTION_FIXTURES.find((f) => f.id === "verb-missing")));
		expect(none.verb).toBeUndefined();
	});
});
