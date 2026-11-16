// The desktop chat's pure decisions about a server draft card (lib/toolDraftHost.js). The
// wiring in ChatView.vue is pinned in views/ChatView.toolDraft.spec.js.
import { describe, expect, it } from "vitest";
import fs from "fs";
import path from "path";
import { draftCardView } from "./draftCardView.js";
import {
	hasParkedDraft,
	nextPanelStale,
	receiptReloadNeeded,
	toolDraftCtxMap,
	withBusyKind,
} from "./toolDraftHost.js";

const FIXTURES = JSON.parse(
	fs.readFileSync(path.resolve(__dirname, "draftCardFixtures.json"), "utf8")
);
const card = (id) => structuredClone(FIXTURES.find((f) => f.id === id).card);
const item = (id, extra = {}) => ({ token: `t-${id}`, preview: { card: card(id) }, ...extra });
const GATED = { token: "t-gated", preview: { card: { kind: "verb", verb: "submit" } } };

describe("hasParkedDraft: one card always", () => {
	// Break: a sensitive or ordinary card retires the typed draft (two cards would be wrong the
	// other way round: the typed draft left alive beside a server draft).
	const table = [
		["no cards", [], false],
		["only an ordinary card", [GATED], false],
		["only a sensitive card", [item("sensitive")], false],
		["a draft card", [item("create-simple")], true],
		["an unreadable draft card", [item("unreadable")], true],
		["a draft behind other cards", [GATED, item("sensitive"), item("update-diff")], true],
		["not a list", null, false],
	];
	for (const [name, list, want] of table)
		it(`${name}: ${want}`, () => expect(hasParkedDraft(list)).toBe(want));
});

describe("a stack of pending cards: the hint and the number each draft card shows", () => {
	// Items of the visible stack, in order: [kind, recent]. kind "draft" is a server draft card,
	// "legacy" an ordinary gated card, "sensitive" a warning card; the last two draw their own
	// markup (the stack hint is theirs when they are last) but take a number like any card.
	const GO = 'or type "go ahead"';
	const ALL3 = 'or type "confirm all", or "confirm 1 and 3"';
	const ALL2 = 'or type "confirm all", or "confirm 1 and 2"';
	const MIX2 = 'or type "confirm 1 and 2"';
	const make = ([kind, recent = true], i) => {
		const token = `t${i + 1}`;
		if (kind === "draft") return item("create-simple", { token, recent });
		if (kind === "sensitive") return item("sensitive", { token, recent });
		return { token, recent, preview: { card: { kind: "verb", verb: "submit" } } };
	};
	// Break: a card in a stack tells the person to type something that confirms other cards, names
	// the wrong number, repeats the stack hint on every card, or loses its place. Each row:
	// [name, stack, stack hint, then per DRAFT card [token, hint, number]].
	const table = [
		["one card alone", [["draft"]], GO, [["t1", GO, ""]]],
		[
			"one Earlier card alone",
			[["draft", false]],
			'or type "confirm 1"',
			[["t1", 'or type "confirm 1"', ""]],
		],
		[
			"draft then legacy: the legacy card shows the stack hint",
			[["draft"], ["legacy"]],
			ALL2,
			[["t1", 'or type "confirm 1"', "1 of 2"]],
		],
		[
			"legacy then draft: the draft is last and shows the stack hint",
			[["legacy"], ["draft"]],
			ALL2,
			[["t2", ALL2, "2 of 2"]],
		],
		[
			"Earlier draft then recent draft",
			[["draft", false], ["draft"]],
			MIX2,
			[
				["t1", 'or type "confirm 1"', "1 of 2"],
				["t2", MIX2, "2 of 2"],
			],
		],
		[
			"recent draft then Earlier draft: the Earlier one keeps its own number",
			[["draft"], ["draft", false]],
			MIX2,
			[
				["t1", 'or type "confirm 1"', "1 of 2"],
				["t2", 'or type "confirm 2"', "2 of 2"],
			],
		],
		[
			"draft, legacy, draft",
			[["draft"], ["legacy"], ["draft"]],
			ALL3,
			[
				["t1", 'or type "confirm 1"', "1 of 3"],
				["t3", ALL3, "3 of 3"],
			],
		],
		[
			"a sensitive card takes a place too",
			[["sensitive"], ["draft"]],
			ALL2,
			[["t2", ALL2, "2 of 2"]],
		],
		[
			"a card being created shows its place and no hint",
			[["draft", true, "busy"], ["draft"]],
			ALL2,
			[
				["t1", "", "1 of 2"],
				["t2", ALL2, "2 of 2"],
			],
		],
	];
	for (const [name, stack, stackHint, want] of table) {
		it(name, () => {
			const items = stack.map(make);
			if (stack[0][2]) Object.assign(items[0], { busy: true, busyKind: "create" });
			const map = toolDraftCtxMap({ items, streaming: false, stackHint });
			const got = Object.keys(map).map((token) => {
				const v = draftCardView(card("create-simple"), map[token]);
				return [token, v.hint, v.number];
			});
			expect(got).toEqual(want);
		});
	}

	// Break: a legacy or sensitive card gets a draft ctx; Preview or Edit is live before the panel
	// handles them (this commit; the next one enables them and rewrites this test).
	it("only server draft cards get a ctx, with Preview and Edit still off", () => {
		const map = toolDraftCtxMap({
			items: [make(["legacy"], 0), make(["draft"], 1), make(["sensitive"], 2)],
			streaming: false,
			stackHint: ALL3,
		});
		expect(Object.keys(map)).toEqual(["t2"]);
		const buttons = draftCardView(card("create-simple"), map.t2).buttons;
		expect(buttons.filter((b) => b.disabled).map((b) => b.id)).toEqual(["preview", "edit"]);
	});

	it("not a list gives no ctx", () => {
		expect(toolDraftCtxMap({ items: null, streaming: false, stackHint: "" })).toEqual({});
	});
});

describe("receiptReloadNeeded: after Create, does the chat need a reload to show the receipt?", () => {
	const base = {
		token: "t1",
		conversation: "c1",
		currentConversation: "c1",
		items: [],
		rows: [],
	};
	const decided = { role: "tool", tool_call_id: "t1", action_outcome: "confirmed" };
	// Break: S10 (the card is spent, no receipt loaded) shows nothing, or a normal success reloads twice.
	const table = [
		["the card left the list and no receipt row is loaded (S10)", {}, true],
		[
			"the card is still pending (a refused Create that keeps it)",
			{ items: [{ token: "t1" }] },
			false,
		],
		[
			"a decided row for this token is already loaded (a normal success)",
			{ rows: [decided] },
			false,
		],
		[
			"the pending row of the token has no outcome yet",
			{ rows: [{ role: "tool", tool_call_id: "t1", action_outcome: "" }] },
			true,
		],
		[
			"another token's receipt does not count",
			{ rows: [{ ...decided, tool_call_id: "t2" }] },
			true,
		],
		["another chat is on screen now", { currentConversation: "c2" }, false],
		["no token", { token: "" }, false],
	];
	for (const [name, over, want] of table)
		it(`${name}: ${want}`, () => expect(receiptReloadNeeded({ ...base, ...over })).toBe(want));
});

describe("withBusyKind: busy transitions around Create and Cancel", () => {
	const gate = () => {
		let open;
		const promise = new Promise((r) => (open = r));
		return { promise, open };
	};

	it("holds the kind while the action runs and clears it when it settles", async () => {
		const pa = { token: "a", busy: false, busyKind: "" };
		const g = gate();
		const seen = [];
		const run = withBusyKind(pa, "create", async () => {
			seen.push(pa.busyKind);
			await g.promise;
		});
		expect(pa.busyKind).toBe("create");
		g.open();
		await expect(run).resolves.toBe(true);
		expect([seen, pa.busyKind]).toEqual([["create"], ""]);
	});

	// Break: a failed or thrown action leaves the card locked for good.
	it("clears the kind when the action throws, and rethrows", async () => {
		const pa = { token: "a", busy: false, busyKind: "" };
		await expect(
			withBusyKind(pa, "cancel", async () => {
				throw new Error("boom");
			})
		).rejects.toThrow("boom");
		expect(pa.busyKind).toBe("");
	});

	// Break: a second press on the same card fires a second request.
	it("ignores a second press while one is in flight, whichever button", async () => {
		const pa = { token: "a", busy: false, busyKind: "" };
		const g = gate();
		let calls = 0;
		const first = withBusyKind(pa, "create", async () => {
			calls++;
			await g.promise;
		});
		const again = await withBusyKind(pa, "create", async () => calls++);
		const cancel = await withBusyKind(pa, "cancel", async () => calls++);
		expect([again, cancel, calls, pa.busyKind]).toEqual([false, false, 1, "create"]);
		g.open();
		await first;
		expect(pa.busyKind).toBe("");
	});

	// Break: Cancel starts while a confirm that set `busy` itself is running (the handlers'
	// own guard is the last line; this is the first).
	it("does nothing for an item that is already busy, or no item", async () => {
		let calls = 0;
		const run = async () => calls++;
		expect(await withBusyKind({ token: "a", busy: true }, "cancel", run)).toBe(false);
		expect(await withBusyKind(null, "create", run)).toBe(false);
		expect(calls).toBe(0);
	});

	it("hands the item to the action", async () => {
		const pa = { token: "a" };
		let got = null;
		await withBusyKind(pa, "create", async (x) => (got = x));
		expect(got).toBe(pa);
	});
});

describe("nextPanelStale: the typed edit panel when a server draft arrives", () => {
	const closed = { panelOpen: false, panelChanged: false, parked: false };
	// Break: the banner never shows, shows on a closed panel, or lingers into the next panel.
	// Each row: [name, state before, the change, state after].
	const table = [
		[
			"a panel is open, nothing parked",
			false,
			{ panelOpen: true, panelChanged: false, parked: false },
			false,
		],
		[
			"a server draft arrives while the panel is open",
			false,
			{ panelOpen: true, panelChanged: false, parked: true },
			true,
		],
		["still parked", true, { panelOpen: true, panelChanged: false, parked: true }, true],
		[
			"the server draft is decided; the panel stays stale",
			true,
			{ panelOpen: true, panelChanged: false, parked: false },
			true,
		],
		["the panel closes", true, { ...closed }, false],
		["closed and nothing parked", false, { ...closed }, false],
		["a server draft arrives with no panel open", false, { ...closed, parked: true }, false],
		[
			"a different panel opens after a stale one",
			true,
			{ panelOpen: true, panelChanged: true, parked: false },
			false,
		],
		[
			"a different panel opens while a draft is parked",
			false,
			{ panelOpen: true, panelChanged: true, parked: true },
			true,
		],
	];
	for (const [name, before, change, after] of table)
		it(`${name}: ${before} -> ${after}`, () =>
			expect(nextPanelStale(before, change)).toBe(after));
});
