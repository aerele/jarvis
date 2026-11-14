import { test } from "node:test";
import assert from "node:assert/strict";
import { ACTION_FIXTURES } from "../../../frontend/src/lib/actionFixtures.js";
import { parseAction } from "./blocks.js";
import { actionCardView } from "./actionCardView.js";

const parse = (id) => {
	const f = ACTION_FIXTURES.find((x) => x.id === id);
	return parseAction("x\n```jarvis-action\n" + f.block + "\n```");
};

for (const f of ACTION_FIXTURES) {
	test(`fixture ${f.id} is ${f.expect}, never a throw`, () => {
		const a = parseAction("x\n```jarvis-action\n" + f.block + "\n```");
		const v = actionCardView(a, "Jarvis");
		if (f.expect === "card") {
			assert.equal(v.kind, "write");
			assert.equal(v.invalid, "");
			if (f.tableRows) assert.equal(v.tableLines[0].value, `${f.tableRows} rows`);
		} else if (f.expect === "none") {
			assert.deepEqual(v, { kind: "none" });
		} else {
			assert.equal(f.expect, "error");
			assert.ok(v.kind === "error" || (v.kind === "write" && v.invalid));
			if (
				[
					"tables-string",
					"tables-object-bad-rows",
					"tables-bad-items",
					"fields-bad-items",
					"verb-unknown",
				].includes(f.id)
			)
				assert.equal(v.kind, "error"); // never a partial card
		}
	});
}

test("error copy is exact", () => {
	assert.equal(
		actionCardView(null, "Jarvis").message,
		"Couldn't show this draft. Ask Jarvis to send it again."
	);
});

test("non-object actions are the error view", () => {
	for (const a of [undefined, "x", 3, []])
		assert.equal(actionCardView(a, "Jarvis").kind, "error");
});

test("email wins over verb", () => {
	assert.equal(
		actionCardView({ kind: "email", to: "a@b.c", subject: "s", body: "b" }, "Jarvis").kind,
		"email"
	);
});

test("an unreadable action is the error view, not a card from what is left", () => {
	const v = actionCardView({ doctype: "X", verb: "create", fields: [], tables: [], unreadable: true }, "Jarvis");
	assert.equal(v.kind, "error");
});

test("verb is normalised, and missing means create", () => {
	assert.equal(actionCardView(parse("verb-capitalised"), "Jarvis").verb, "create");
	assert.equal(actionCardView(parse("verb-missing"), "Jarvis").verb, "create");
});

test("a getter that throws becomes the error view", () => {
	const a = { doctype: "X", verb: "create", fields: [], tables: [] };
	Object.defineProperty(a, "title", {
		get() {
			throw new Error("boom");
		},
	});
	assert.equal(actionCardView(a, "Jarvis").kind, "error");
});

test("docs batch and empty create keep their specific messages", () => {
	const docs = actionCardView({ doctype: "X", verb: "create", fields: [], tables: [], docs: [{}] }, "Jarvis");
	assert.equal(docs.kind, "write");
	assert.match(docs.invalid, /`docs` batch/);
	assert.match(docs.invalid, /Ask Jarvis to apply them as a batch/);
	const empty = actionCardView({ doctype: "X", verb: "create", fields: [], tables: [] }, "Jarvis");
	assert.equal(empty.invalid, "This draft has no fields to show.");
});

test("heading defaults from verb and doctype", () => {
	assert.equal(actionCardView(parse("canonical"), "Jarvis").heading, "Create Timesheet");
});
