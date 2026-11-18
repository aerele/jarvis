// The pane's and the Desk widget's rule for "this pending item is a draft" lives in
// jarvis/public/js/shared/draft_pending.mjs (the widget's bundle cannot import frontend/src).
// It must stay the full chat's rule, toolDraft.js isToolDraftItem.
import { describe, expect, it } from "vitest";
import { cardOfItem, isToolDraftItem } from "./toolDraft.js";
import { draftCardView } from "./draftCardView.js";
import {
	cardOfPendingItem,
	draftTitleOfItem,
	isDraftPendingItem,
} from "../../../jarvis/public/js/shared/draft_pending.mjs";

const draft = { draft: { verb: "create", doctype: "ToDo", values: { a: 1 } } };
const cards = [
	["a draft", draft],
	["an unreadable draft", { draft: { verb: "create", values: "no" } }],
	["a draft key with junk", { draft: "junk" }],
	["a null draft key", { draft: null }],
	["a sensitive card", { kind: "update", risk: "sensitive" }],
	["a draft with a risk", { ...draft, risk: "sensitive" }],
	["an ordinary gated card", { kind: "verb", verb: "submit", doctype: "Sales Order" }],
	["a card with an empty risk", { ...draft, risk: "" }],
	["no card", null],
];
const shapes = (c) => [
	{ token: "t", preview: c === null ? undefined : { card: c } },
	{ token: "t", pending_card: c },
	{ token: "t", preview: { card: "no" }, pending_card: c },
	{ token: "t" },
];

describe("shared draft_pending.mjs agrees with toolDraft.js", () => {
	// Break: either file changes the rule for "is a draft item" alone, so the full chat draws a
	// draft card while the pane or widget offers a plain Confirm (or the reverse).
	for (const [name, card] of cards) {
		it(`${name}: same card, same answer, on every item shape`, () => {
			for (const item of shapes(card)) {
				expect(cardOfPendingItem(item)).toBe(cardOfItem(item));
				expect(isDraftPendingItem(item)).toBe(isToolDraftItem(item));
				// a draft item always has a title to show; anything else has none
				expect(draftTitleOfItem(item) !== "").toBe(isToolDraftItem(item));
			}
		});
	}

	it("a value that is not an item is not a draft in either", () => {
		for (const x of [null, undefined, "x", 3, []]) {
			expect(isDraftPendingItem(x)).toBe(isToolDraftItem(x));
		}
	});
});

describe("the title of a draft is one rule on every surface", () => {
	const named = (title, extra = {}) => ({
		draft: {
			verb: "create",
			doctype: "Timesheet",
			name: null,
			values: { a: 1 },
			title,
			...extra,
		},
	});
	// Break: either rule (the tool-name pattern, the "null" / "undefined" rule, the fallback) changes
	// alone, so the full chat's card, the pane's strip and the widget's line name one draft differently.
	// Real cards carry "New <doctype>" / "<doctype> <name>"; the rest are what a model or an old row
	// could put there. Readable cards only: an unreadable one has no card title at all.
	it.each([
		["a real create title", named("New Timesheet")],
		["a real update title", named("Timesheet TS-1", { verb: "update", name: "TS-1" })],
		["a title with spaces", named("  Timesheet for May ")],
		["a tool name", named("create_doc")],
		["a prefixed tool name", named("jarvis__update_doc")],
		["a tool name inside words", named("please create_doc now")],
		["another snake_case word", named("submit_doc")],
		["the string null", named("null")],
		["the string undefined", named("undefined")],
		["an empty title, create", named("")],
		["an empty title, update", named("", { verb: "update", name: "TS-2" })],
		["no title at all", named(undefined)],
		["a number title", named(42)],
		["an object title", named({ a: 1 })],
		["no doctype", named("", { doctype: undefined })],
	])("%s", (_, card) => {
		const item = { token: "t", preview: { card } };
		expect(draftTitleOfItem(item)).toBe(draftCardView(card, {}).title);
	});
});
