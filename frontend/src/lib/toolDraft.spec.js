// Routing and card context of a pending item that holds a server draft card.
import { describe, expect, it } from "vitest";
import { cardOfItem, isToolDraftItem, toolDraftCtx } from "./toolDraft.js";

const DRAFT = { kind: "create", draft: { verb: "create", doctype: "ToDo", values: { a: 1 } } };
const UNREADABLE = { kind: "create", draft: { verb: "create", values: "no" } };
const SENSITIVE = { kind: "update", risk: "sensitive" };
const GATED = { kind: "verb", verb: "submit", doctype: "Sales Order" };
// a draft that also carries a risk is still the warning card
const RISKY_DRAFT = { ...DRAFT, risk: "sensitive" };

const viaList = (card) => ({ token: "t", preview: { card } });
const viaRow = (card) => ({ token: "t", pending_card: card });

describe("isToolDraftItem", () => {
	const table = [
		["a draft card", DRAFT, true],
		["an unreadable draft card (drawn with only Cancel)", UNREADABLE, true],
		["a sensitive card", SENSITIVE, false],
		["a draft that carries a risk", RISKY_DRAFT, false],
		["an ordinary gated card", GATED, false],
		["no card", null, false],
	];
	for (const [name, c, want] of table) {
		it(`${name}: ${want}, by the list path and the row path`, () => {
			expect(isToolDraftItem(c ? viaList(c) : { token: "t" })).toBe(want);
			expect(isToolDraftItem(c ? viaRow(c) : { token: "t" })).toBe(want);
		});
	}

	it("not an item: false", () => {
		for (const x of [null, undefined, "x", 3, []]) expect(isToolDraftItem(x)).toBe(false);
	});
});

describe("cardOfItem", () => {
	it("reads the list path, then the row path, else null", () => {
		expect(cardOfItem(viaList(DRAFT))).toBe(DRAFT);
		expect(cardOfItem(viaRow(DRAFT))).toBe(DRAFT);
		expect(cardOfItem({ preview: { card: "no" }, pending_card: DRAFT })).toBe(DRAFT);
		expect(cardOfItem({ preview: {} })).toBeNull();
		expect(cardOfItem(null)).toBeNull();
	});
});

describe("toolDraftCtx", () => {
	const ctx = (pa, over = {}) =>
		toolDraftCtx({ pa, editingToken: null, streaming: false, ...over });
	const table = [
		["a fresh item", { token: "a", recent: true }, {}, {}],
		["earlier is recent === false only", { token: "a", recent: false }, {}, { earlier: true }],
		["an item with no recent flag is not earlier", { token: "a" }, {}, {}],
		[
			"create in flight",
			{ token: "a", busy: true, busyKind: "create" },
			{},
			{ busy: "create" },
		],
		[
			"cancel in flight",
			{ token: "a", busy: true, busyKind: "cancel" },
			{},
			{ busy: "cancel" },
		],
		[
			"a save from the panel counts as create",
			{ token: "a", busy: true, busyKind: "save" },
			{},
			{ busy: "create" },
		],
		[
			"busy with no kind locks the card without naming a button",
			{ token: "a", busy: true },
			{},
			{ busy: "busy" },
		],
		["editing this token", { token: "a" }, { editingToken: "a" }, { editing: true }],
		["editing another token", { token: "a" }, { editingToken: "b" }, {}],
		["no panel open", { token: "a" }, { editingToken: "" }, {}],
		["the turn is running", { token: "a" }, { streaming: true }, { streaming: true }],
	];
	for (const [name, pa, over, patch] of table) {
		it(name, () => {
			expect(ctx(pa, over)).toEqual({
				earlier: false,
				busy: "",
				editing: false,
				streaming: false,
				surface: "desktop",
				...patch,
			});
		});
	}
});
