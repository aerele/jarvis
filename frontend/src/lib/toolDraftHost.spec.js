// The desktop chat's pure decisions about a server draft card (lib/toolDraftHost.js). The
// wiring in ChatView.vue is pinned in views/ChatView.toolDraft.spec.js.
import { describe, expect, it } from "vitest";
import fs from "fs";
import path from "path";
import { draftCardView } from "./draftCardView.js";
import {
	cancelToolItem,
	discardFromForm,
	hasParkedDraft,
	nextPanelStale,
	openToolDraftPanel,
	openToolPreview,
	orphanStackHint,
	receiptReloadNeeded,
	saveToolDraft,
	toolDraftCtxMap,
	toolPanelDead,
	toolPanelNotices,
	withBusyKind,
} from "./toolDraftHost.js";
import { blankRow, buildDraftTable } from "./draftTables.js";
import { panelField } from "./docFields.js";
import { reconcilePending } from "./pendingResync.js";
import { CANT_EDIT_TEXT } from "./toolDraftPanel.js";
import { timesheetCard, timesheetModel } from "./timesheetDraftFixture.js";
import { lineItemSummary } from "./actionSummary.js";

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
		// the stack hint is drawn once, on the last card, so a last card that is Earlier draws it:
		// this is also where [legacy recent, draft Earlier] keeps its hint
		[
			"recent draft then Earlier draft: the last card, Earlier, shows the stack hint",
			[["draft"], ["draft", false]],
			MIX2,
			[
				["t1", 'or type "confirm 1"', "1 of 2"],
				["t2", MIX2, "2 of 2"],
			],
		],
		[
			"legacy then Earlier draft: the stack hint is not lost",
			[["legacy"], ["draft", false]],
			MIX2,
			[["t2", MIX2, "2 of 2"]],
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

	// Break: a legacy or sensitive card gets a draft ctx, Preview or Edit stays disabled, the open
	// form's card does not read "Editing", or a card can be pressed while a form is being built
	// (the press would be ignored: the buttons must look disabled).
	it("only server draft cards get a ctx; Preview and Edit are live; editing shows", () => {
		const items = [make(["legacy"], 0), make(["draft"], 1), make(["draft"], 2)];
		const map = toolDraftCtxMap({
			items,
			streaming: false,
			stackHint: ALL3,
			editingToken: "t2",
		});
		expect(Object.keys(map)).toEqual(["t2", "t3"]);
		const seen = (ctx) => {
			const v = draftCardView(card("create-simple"), ctx);
			return [v.state, v.buttons.map((b) => `${b.id}${b.disabled ? ":off" : ""}`)];
		};
		expect(seen(map.t2)).toEqual(["S3", ["create:off", "preview:off", "edit:off", "cancel"]]);
		expect(seen(map.t3)).toEqual(["S1", ["create", "preview", "edit", "cancel"]]);
		const idle = toolDraftCtxMap({ items, streaming: false, stackHint: ALL3 });
		expect(seen(idle.t2)).toEqual(["S1", ["create", "preview", "edit", "cancel"]]);
	});

	// Break: while the form of card A is being built, card B's Edit and Preview look pressable but
	// do nothing.
	it("while any form is being built every draft card is locked, the one pressed and the others", () => {
		const items = [make(["draft"], 0), make(["draft"], 1)];
		const map = toolDraftCtxMap({
			items,
			streaming: false,
			stackHint: ALL2,
			buildingToken: "t1",
		});
		const state = (t) => {
			const v = draftCardView(card("create-simple"), map[t]);
			return [v.state, v.buttons.map((b) => b.disabled)];
		};
		expect([state("t1"), state("t2")]).toEqual([
			["S4", [true, true, true, true]],
			["S4", [true, true, true, true]],
		]);
	});
});

describe("toolDraftCtxMap surface: the same card draws fewer table rows on the phone", () => {
	// Break: the map or the host ctx drops `surface`, so a phone card keeps the desktop's 8 rows.
	const tall = () => {
		const c = card("create-with-table");
		const row = c.tables[0].rows[0];
		c.tables[0].rows = Array.from({ length: 7 }, () => structuredClone(row));
		c.tables[0].count = 7;
		return c;
	};
	const rowsShown = (over) => {
		const items = [{ token: "t1", recent: true, preview: { card: tall() } }];
		const map = toolDraftCtxMap({ items, streaming: false, stackHint: "", ...over });
		const view = draftCardView(tall(), map.t1);
		return [view.tables[0].rows.length, view.tables[0].more];
	};
	it("the default is the desktop's 7 of 7", () => {
		expect(rowsShown({})).toEqual([7, 0]);
	});
	it('surface "phone" shows 5 and says 2 more', () => {
		expect(rowsShown({ surface: "phone" })).toEqual([5, 2]);
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

describe("withBusyKind with an injected accessor", () => {
	// Break: the accessor is ignored and the flag lands on the item again (the phone's next read
	// gives the card back with no spinner), or the default accessor stops reading `busy`.
	it("reads and writes the accessor, never the item", async () => {
		const pa = { token: "a", busy: false, busyKind: "" };
		const store = { a: "" };
		const where = {
			read: (x) => ({ busy: false, busyKind: store[x.token] }),
			setKind: (x, kind) => (store[x.token] = kind),
		};
		let during;
		await withBusyKind(
			pa,
			"create",
			async () => {
				during = store.a;
				expect(await withBusyKind(pa, "cancel", async () => {}, where)).toBe(false);
			},
			where
		);
		expect([during, store.a, pa.busyKind]).toEqual(["create", "", ""]);
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

describe("toolPanelDead: a form on a card that is no longer pending", () => {
	const panel = (extra = {}) => ({ tool: true, token: "t1", conversation: "c1", ...extra });
	const live = [{ token: "t1", conversation: "c1" }];
	// Break: edits on a replaced, confirmed or discarded draft are lost silently (no banner), or a
	// form that is saving shows the banner during its own save.
	const table = [
		["the card is still pending", panel(), live, false],
		["the card left the list", panel(), [{ token: "t2", conversation: "c1" }], true],
		["the list is empty", panel(), [], true],
		[
			"another chat's card with the same token",
			panel(),
			[{ token: "t1", conversation: "c2" }],
			true,
		],
		["its own save or cancel is running", panel({ applying: true }), [], false],
		["a typed draft's panel is never judged here", { token: "t1" }, [], false],
		["no panel", null, [], false],
		["not a list", panel(), null, true],
	];
	for (const [name, p, items, want] of table)
		it(`${name}: ${want}`, () => expect(toolPanelDead(p, items)).toBe(want));
});

describe("toolPanelNotices: the lines above a server draft's form", () => {
	const p = (extra = {}) => ({ tool: true, ...extra });
	// Break: a dead card, a blocked save or a record that could not be loaded goes unsaid, or the
	// words change.
	it("says each thing once, in order, in its own style", () => {
		expect(toolPanelNotices(p(), false)).toEqual([]);
		expect(
			toolPanelNotices(p({ saveBlocked: CANT_EDIT_TEXT, storedUnavailable: true }), true)
		).toEqual([
			{
				type: "error",
				text: "This draft changed while you were editing. Your edits were not saved.",
			},
			{
				type: "error",
				text: "This draft can't be edited here. Use Create on the card, or ask Jarvis to change it.",
			},
			{
				type: "warning",
				text: "The saved values of this record could not be loaded, so the form shows only what Jarvis drafted.",
			},
		]);
	});
	it("says nothing for a typed draft's panel or no panel", () => {
		expect(toolPanelNotices({ saveBlocked: "x" }, true)).toEqual([]);
		expect(toolPanelNotices(null, true)).toEqual([]);
	});
});

describe("orphanStackHint: the stack's hint when its last draft card has no line for it", () => {
	const ALL2 = 'or type "confirm all", or "confirm 1 and 2"';
	const draft = (token, extra = {}) => item("create-simple", { token, recent: true, ...extra });
	const legacy = (token) => ({
		token,
		recent: true,
		preview: { card: { kind: "verb", verb: "submit" } },
	});
	const hintFor = (items, over = {}) => {
		const stackHint = ALL2;
		const ctxMap = toolDraftCtxMap({ items, streaming: false, stackHint, ...over });
		return orphanStackHint({ items, ctxMap, stackHint });
	};
	// Break: the stack hint is lost (a last card in S3, S4, S12 or S13 draws no hint line), is
	// drawn twice (the last card already carries it), or appears for one card or a legacy last card.
	const table = [
		["last draft card ready (it carries the hint)", [legacy("a"), draft("b")], {}, ""],
		[
			"last draft card Earlier (it carries the hint)",
			[legacy("a"), draft("b", { recent: false })],
			{},
			"",
		],
		[
			"last draft card being edited (S3)",
			[legacy("a"), draft("b")],
			{ editingToken: "b" },
			ALL2,
		],
		[
			"last draft card working (S4)",
			[legacy("a"), draft("b", { busy: true, busyKind: "create" })],
			{},
			ALL2,
		],
		[
			"last draft card while a turn runs (S13)",
			[legacy("a"), draft("b")],
			{ streaming: true },
			ALL2,
		],
		["last draft card being built", [draft("a"), draft("b")], { buildingToken: "a" }, ALL2],
		[
			"last draft card unreadable (S12)",
			[legacy("a"), item("unreadable", { token: "b" })],
			{},
			ALL2,
		],
		[
			"last card is legacy (its own hint)",
			[draft("a"), legacy("b")],
			{ editingToken: "a" },
			"",
		],
		["one card: no stack", [draft("a")], { editingToken: "a" }, ""],
		["no cards", [], {}, ""],
	];
	for (const [name, items, over, want] of table)
		it(`${name}: ${want ? "drawn under the stack" : "nothing extra"}`, () =>
			expect(hintFor(items, over)).toBe(want));
	it("no stack hint, nothing to draw", () =>
		expect(
			orphanStackHint({ items: [draft("a"), draft("b")], ctxMap: {}, stackHint: "" })
		).toBe(""));
});

describe("the dead-draft fence against the pending list the view really keeps (reconcilePending)", () => {
	const panel = { tool: true, token: "t1", conversation: "c1" };
	const serverItem = (token) => ({ token, conversation: "c1", pending_card_safe: true });
	const queue = (token, extra = {}) => ({ token, conversation: "c1", ...extra });
	// what the view does with a resync: keep `kept`, add `toAdd` as new items
	const resync = (existing, serverTokens, inflight = new Set()) => {
		const { kept, toAdd } = reconcilePending(
			existing,
			"c1",
			serverTokens.map(serverItem),
			inflight
		);
		return [...kept, ...toAdd.map((it) => queue(it.token))];
	};
	// Break: the banner never shows when the server stops listing the draft, or does not go away when
	// the same token is listed again, or shows during the form's own save that a resync races.
	it("drop and re-add of the same token: dead while it is gone, live again when listed", () => {
		let list = [queue("t1")];
		list = resync(list, ["t1"]);
		expect(toolPanelDead(panel, list)).toBe(false);
		list = resync(list, ["t2"]); // the server no longer lists t1
		expect(toolPanelDead(panel, list)).toBe(true);
		list = resync(list, ["t1", "t2"]); // and lists it again
		expect(toolPanelDead(panel, list)).toBe(false);
	});

	it("its own save racing a resync: the busy item is kept, so the form is never dead", () => {
		const list = resync([queue("t1", { busy: true, busyKind: "save" })], [], new Set(["t1"]));
		expect(toolPanelDead(panel, list)).toBe(false);
	});

	it("the card removed by its own save: the form is applying, so not dead until the save is over", () => {
		const list = resync([queue("t1")], []);
		expect([
			toolPanelDead({ ...panel, applying: true }, list),
			toolPanelDead({ ...panel, applying: false }, list),
		]).toEqual([false, true]);
	});

	it("a token listed for another chat does not keep this form alive", () => {
		const list = resync([queue("t1")], []);
		expect(toolPanelDead(panel, [...list, { token: "t1", conversation: "c2" }])).toBe(true);
	});
});

// ---- the doors, with fake deps -------------------------------------------------------------

const LOGS = {
	label: "Logs",
	child_doctype: "Log",
	columns: [
		{ fieldname: "activity", label: "activity", fieldtype: "Data", read_only: 0 },
		{ fieldname: "hours", label: "hours", fieldtype: "Float", read_only: 0 },
	],
	extra_columns: [],
	form_fields: [],
};
const DRAFT_CARD = () => ({
	kind: "create",
	draft: {
		verb: "create",
		doctype: "Timesheet",
		values: {
			title: "Hello",
			logs: [
				{ activity: "A1", hours: 1 },
				{ activity: "A2", hours: 2 },
			],
		},
	},
});
// What ChatView's buildDraftModel returns for DRAFT_CARD (the real field and table builders).
function builtModel() {
	const f = { fieldname: "title", label: "title", fieldtype: "Data", reqd: 0, read_only: 0 };
	return {
		verb: "create",
		doctype: "Timesheet",
		fields: [{ ...panelField(f, "Hello"), proposed: true }],
		tables: [
			buildDraftTable({
				fieldname: "logs",
				spec: LOGS,
				required: true,
				proposed: [
					{ activity: "A1", hours: 1 },
					{ activity: "A2", hours: 2 },
				],
				verb: "create",
			}),
		],
		applying: false,
		error: "",
	};
}

// A fake host: the pending list, the open form, and a log of every effect in order.
function host(over = {}) {
	const state = {
		items: [
			{
				token: "t1",
				conversation: "c1",
				busy: false,
				busyKind: "",
				preview: { card: DRAFT_CARD() },
			},
		],
		panel: null,
		preview: null,
		building: "",
		streaming: false,
		chat: "c1",
		log: [],
		deadAt: [],
	};
	const log = (...a) => state.log.push(a.join(":"));
	const d = {
		find: (token, conv) =>
			state.items.find((x) => x.token === token && x.conversation === conv),
		capture: (token) => ({ token, conversation: state.chat }),
		pendingCurrent: (v) =>
			v.conversation === state.chat &&
			(d.inflight.has(v.token) || state.items.some((x) => x.token === v.token)),
		chatCurrent: (v) => v.conversation === state.chat,
		panel: () => state.panel,
		setPanel: (m) => {
			state.panel = m;
			log("setPanel");
		},
		closePanel: () => {
			state.panel = null;
			log("closePanel");
		},
		build: async (action) => {
			log("build");
			state.action = action;
			return builtModel();
		},
		autoOpen: () => log("autoOpen"),
		building: () => state.building,
		setBuilding: (t) => {
			state.building = t;
			log("building", t || "off");
		},
		setPreview: (p) => {
			state.preview = p;
			log("setPreview");
		},
		notify: (text) => log("notify", text),
		warn: (...a) => log("warn", a[0]),
		streaming: () => state.streaming,
		inflight: new Set(),
		removePending: (token) => {
			state.items = state.items.filter((x) => x.token !== token);
			state.deadAt.push(toolPanelDead(state.panel, state.items));
			log("removePending");
		},
		reload: async () => {
			state.deadAt.push(toolPanelDead(state.panel, state.items));
			log("reload");
		},
		refreshChats: () => log("refreshChats"),
		showQueued: (r) => log("showQueued", r && r.queued ? "yes" : "no"),
		save: async (token, conv, values) => {
			log("save", JSON.stringify(values));
			return state.reply;
		},
		agent: () => "Jarvis",
		thrown: (e, fallback) => ({ message: `${fallback} (${e.message})` }),
		metaFields: () => [],
		afterMarks: (mark) => {
			log("afterMarks");
			state.mark = mark;
		},
		create: async (pa) => {
			log("create");
			await state.createEffect?.(pa);
		},
		discard: async (pa) => {
			log("discard");
			await state.discardEffect?.(pa);
			return !state.discardIgnored;
		},
		...over,
	};
	return { d, state };
}
// an open form on the card of "t1"
async function openForm(h) {
	await openToolDraftPanel(h.state.items[0], h.d);
	h.state.log.length = 0;
	return h.state.panel;
}

describe("openToolDraftPanel: build, snapshot, guard, then show", () => {
	// Break: the form is shown before it is snapshotted and guarded, so a late edit or row becomes
	// the baseline; or it is built from the label instead of the card.
	it("builds from the card with the card marker, stamps and snapshots, then shows", async () => {
		const h = host();
		expect(await openToolDraftPanel(h.state.items[0], h.d)).toBe("opened");
		expect(h.state.log).toEqual([
			"building:t1",
			"build",
			"building:off",
			"autoOpen",
			"setPanel",
		]);
		const p = h.state.panel;
		expect([p.tool, p.token, p.snapshot !== undefined, p.saveBlocked]).toEqual([
			true,
			"t1",
			true,
			"",
		]);
		// the action the builder was given: the card's fields by fieldname, and the marker
		expect(h.state.action.fields).toEqual([
			{ fieldname: "title", label: "title", value: "Hello" },
		]);
		expect(Object.getOwnPropertySymbols(h.state.action)).toHaveLength(1);
	});

	// Break: a form that does not match its card offers Save anyway.
	it("a form that does not match its card still opens, with Save blocked and a log line without values", async () => {
		const h = host({
			build: async () => {
				const m = builtModel();
				m.tables[0].rows.push(blankRow(m.tables[0]));
				return m;
			},
		});
		await openToolDraftPanel(h.state.items[0], h.d);
		expect(h.state.panel.saveBlocked).toBe(CANT_EDIT_TEXT);
		expect(h.state.log).toContain("warn:Jarvis card: this draft form does not match its card");
	});

	// Break: a second click starts a second build; a build that fails opens an empty form.
	it("ignores a click while a form is building, and one on an already open form", async () => {
		const h = host();
		h.state.building = "t1";
		expect(await openToolDraftPanel(h.state.items[0], h.d)).toBe("failed");
		expect(h.state.log).toEqual([]);
		h.state.building = "";
		await openToolDraftPanel(h.state.items[0], h.d);
		h.state.log.length = 0;
		expect(await openToolDraftPanel(h.state.items[0], h.d)).toBe("ignored");
		expect(h.state.log).toEqual([]);
	});

	it("tells the person when the draft cannot be built, and clears the building flag", async () => {
		const h = host({
			build: async () => {
				throw Object.assign(new Error("x"), {
					jvUserMessage: "This draft has no fields to show.",
				});
			},
		});
		expect(await openToolDraftPanel(h.state.items[0], h.d)).toBe("failed");
		expect(h.state.panel).toBeNull();
		expect(h.state.log).toContain("notify:This draft has no fields to show.");
		expect(h.state.building).toBe("");
	});

	// Break: a form opens on a card that was decided while it was building.
	it("does not open when the card went while it was building", async () => {
		const h = host({
			build: async () => {
				h.state.items = [];
				return builtModel();
			},
		});
		expect(await openToolDraftPanel({ ...h.state.items[0] }, h.d)).toBe("failed");
		expect(h.state.panel).toBeNull();
	});

	it("an unreadable card is not opened", async () => {
		const h = host();
		h.state.items[0].preview.card = {
			kind: "create",
			draft: { verb: "create", values: "no" },
		};
		expect(await openToolDraftPanel(h.state.items[0], h.d)).toBe("failed");
		expect(h.state.log[0]).toMatch(/^notify:Couldn't open this draft/);
	});
});

describe("openToolPreview: the card's own numbers, no form", () => {
	it("builds the model, fills the calculated cells from the card, and shows only the preview", async () => {
		const h = host();
		h.state.items[0].preview.card.tables = [
			{
				label: "Logs",
				count: 2,
				columns: ["activity", "hours"],
				fieldnames: ["activity", "hours"],
				rows: [{ cells: ["A1", "1.50"] }, { cells: ["A2", "2"] }],
			},
		];
		expect(await openToolPreview(h.state.items[0], h.d)).toBe("opened");
		expect(h.state.panel).toBeNull();
		expect(h.state.preview.token).toBe("t1");
		expect(h.state.preview.model.computed.logs[0]).toEqual({ activity: "A1", hours: "1.50" });
	});
});

describe("the form and the Preview of a Timesheet draft show the card's numbers", () => {
	const tsHost = () => {
		const h = host({ build: async () => timesheetModel() });
		h.state.items[0].preview.card = timesheetCard();
		return h;
	};
	// Break: the form opens with Hrs blank (the card shows 3 / 1.50 / 2), or the host does not ask
	// for the card's cells before the snapshot.
	it("the form opens with the card's Hrs, untouched", async () => {
		const h = tsHost();
		await openToolDraftPanel(h.state.items[0], h.d);
		const p = h.state.panel;
		expect(p.tables[0].rows.map((r) => r.hours)).toEqual(["3", "1.50", "2"]);
		expect(p.saveBlocked).toBe("");
	});

	// Break: the Preview shows the control's ISO dates and a blank Hrs / Is Billable.
	it("the Preview reads as the card: Hrs, Is Billable and dates", async () => {
		const h = tsHost();
		await openToolPreview(h.state.items[0], h.d);
		const model = h.state.preview.model;
		const view = lineItemSummary(model.tables[0], model.computed.time_logs);
		const col = (name) => model.tables[0].columns.findIndex((c) => c.fieldname === name);
		expect([
			view.rows.map((r) => r.cells[col("hours")]),
			view.rows.map((r) => r.cells[col("is_billable")]),
			view.rows.map((r) => r.cells[col("from_time")]),
		]).toEqual([
			["3", "1.50", "2"],
			["No", "No", "No"],
			["08-12-2026 09:30:00", "08-12-2026 13:30:00", "08-12-2026 15:00:00"],
		]);
	});
});
describe("saveToolDraft: the replies of the edit form's Save", () => {
	const fixable = (fields = []) => ({
		ok: false,
		error: { code: "ValidationError", message: "Hours must be set.", fields },
	});
	const edit = (p) => (p.fields[0].value = "Edited");

	// Break: Save sends more than the change, or on the wrong card; or the item is not busy /
	// in flight while the request runs.
	it("sends only what changed, with the item busy (save) and in flight meanwhile", async () => {
		const h = host();
		const p = await openForm(h);
		edit(p);
		h.state.reply = { ok: true, verb: "create", name: "TS-1" };
		const seen = {};
		const save = h.d.save;
		h.d.save = async (...a) => {
			seen.busy = [
				h.state.items[0].busy,
				h.state.items[0].busyKind,
				h.d.inflight.has("t1"),
				p.applying,
			];
			return save(...a);
		};
		expect(await saveToolDraft(p, h.d)).toBe("saved");
		expect(seen.busy).toEqual([true, "save", true, true]);
		expect(h.state.log[0]).toBe('save:{"title":"Edited"}');
	});

	// Break: a saved reply leaves the form open, or closes it after the card went (a dead-token
	// banner flashes on the person's own save), or skips the reload that shows the receipt.
	it("saved: closes the form BEFORE the card leaves, reloads, and never shows a dead form", async () => {
		const h = host();
		const p = await openForm(h);
		edit(p);
		h.state.reply = { ok: true, verb: "create", name: "TS-1", queued: true };
		expect(await saveToolDraft(p, h.d)).toBe("saved");
		expect(h.state.log).toEqual([
			'save:{"title":"Edited"}',
			"closePanel",
			"removePending",
			"reload",
			"refreshChats",
			"showQueued:yes",
		]);
		expect(h.state.deadAt).toEqual([false, false]);
		expect([h.state.items.length, h.d.inflight.size]).toEqual([0, 0]);
	});

	// Break: a fixable reply closes the form (the edits go), shows no marks, or no focus.
	it("fixable: the form stays, shows the error, marks its fields and rows, and asks for the focus", async () => {
		const h = host();
		const p = await openForm(h);
		edit(p);
		p.tables[0].rows[1].hours = "";
		h.state.reply = fixable([
			{ fieldname: "title", label: "title", message: "Bad", invalid: true },
			{ fieldname: "hours", parentfield: "logs", idx: 2, message: "Needed" },
		]);
		expect(await saveToolDraft(p, h.d)).toBe("keep");
		expect(h.state.panel).toBe(p);
		expect(p.error.message).toBe("Hours must be set.");
		expect(p.fields[0].serverInvalid).toBe(true);
		expect(p.tables[0].rows[1].__missing).toEqual(["hours"]);
		expect(h.state.log).toContain("afterMarks");
		// the server's first mark: the main field, not the row
		expect(h.state.mark).toEqual({ kind: "field", index: 0 });
		expect([
			p.applying,
			h.state.items[0].busy,
			h.state.items[0].busyKind,
			h.d.inflight.size,
		]).toEqual([false, false, "", 0]);
	});

	// Break: the server's "keep a saved row" refusal marks the TABLE by name; markMissing would add
	// the table to the form as a text field. The banner carries the message instead.
	it("a mark on a table never adds the table as a field", async () => {
		const h = host({
			metaFields: () => [{ fieldname: "logs", label: "Logs", fieldtype: "Table" }],
		});
		const p = await openForm(h);
		edit(p);
		h.state.reply = fixable([
			{
				fieldname: "logs",
				label: "Logs",
				message:
					"Keep the saved rows you want, or clear the table and save before adding new rows.",
				invalid: 1,
			},
		]);
		const before = p.fields.length;
		expect(await saveToolDraft(p, h.d)).toBe("keep");
		expect(p.fields).toHaveLength(before);
		// nothing of the form is marked: the banner is what gets scrolled to
		expect(h.state.log).toContain("afterMarks");
		expect(h.state.mark).toBeNull();
	});

	// Break: the busy reply (no fields) is treated as closed, or marks nothing and hides the banner.
	it("busy (no fields): the form stays with the banner", async () => {
		const h = host();
		const p = await openForm(h);
		edit(p);
		h.state.reply = {
			ok: false,
			error: {
				code: "DraftBusyError",
				message: "This draft is being handled. Try again in a moment.",
				fields: [],
			},
		};
		expect(await saveToolDraft(p, h.d)).toBe("keep");
		expect(p.error.message).toBe("This draft is being handled. Try again in a moment.");
		expect(h.state.panel).toBe(p);
	});

	// Break: card_open closes the card too (the item is lost), or says "could not be told".
	it("closed with card_open: the form closes, the card STAYS, no 'could not be told'", async () => {
		const h = host();
		const p = await openForm(h);
		edit(p);
		h.state.reply = {
			ok: false,
			closed: true,
			card_open: true,
			assistant_told: false,
			error: { code: "X", message: "This draft cannot be saved right now" },
		};
		expect(await saveToolDraft(p, h.d)).toBe("closed-open");
		expect(h.state.panel).toBeNull();
		expect(h.state.items).toHaveLength(1);
		const note = h.state.log.find((l) => l.startsWith("notify:"));
		expect(note).toContain("Couldn't save this: This draft cannot be saved right now.");
		expect(note).not.toContain("could not be told");
		expect(h.state.log).not.toContain("reload");
	});

	// Break: a closed reply (the draft is no longer open) closes a form that holds edits (they are
	// lost), or leaves the card on screen, or loads no receipt.
	it("closed with edits in the form: the form stays, the card goes, the chat reloads, the form reads dead", async () => {
		const h = host();
		const p = await openForm(h);
		edit(p);
		h.state.reply = {
			ok: false,
			closed: true,
			assistant_told: true,
			error: { code: "X", message: "This record changed after the draft was made" },
		};
		expect(await saveToolDraft(p, h.d)).toBe("closed-dead");
		expect(h.state.panel).toBe(p);
		expect(p.fields[0].value).toBe("Edited");
		expect(h.state.items).toHaveLength(0);
		expect(h.state.log).toEqual([
			'save:{"title":"Edited"}',
			expect.stringMatching(/^notify:Couldn't save this: This record changed/),
			"removePending",
			"reload",
			"refreshChats",
			"showQueued:no",
		]);
		// not dead during its own save, dead once the save is over
		expect(h.state.deadAt).toEqual([false, false]);
		expect(toolPanelDead(p, h.state.items)).toBe(true);
	});

	// Break: a partial reply (part of the edits may already be saved) keeps the form with the banner
	// "Your edits were not saved", which contradicts the toast.
	it("closed with outcome partial and edits in the form: the form closes", async () => {
		const h = host();
		const p = await openForm(h);
		edit(p);
		h.state.reply = {
			ok: false,
			closed: true,
			outcome: "partial",
			assistant_told: true,
			error: { code: "X", message: "Part of this was saved" },
		};
		expect(await saveToolDraft(p, h.d)).toBe("closed");
		expect(h.state.panel).toBeNull();
		expect(h.state.log).toContain("reload");
	});

	// Break: a clean form is kept open for no reason (nothing to lose), or the dirty check is
	// taken before the save instead of after it.
	it("closed with no edits left in the form: the form closes", async () => {
		const h = host();
		const p = await openForm(h);
		edit(p);
		h.state.reply = {
			ok: false,
			closed: true,
			assistant_told: true,
			error: { code: "X", message: "Gone" },
		};
		const inner = h.d.save;
		h.d.save = async (...a) => {
			p.fields[0].value = "Hello"; // the person put it back while the save ran
			return inner(...a);
		};
		expect(await saveToolDraft(p, h.d)).toBe("closed");
		expect(h.state.panel).toBeNull();
	});

	// Break: the note promises a reply in the chat the server did not say was sent, or says
	// the assistant could not be told when the reply says nothing.
	const closedNote = async (reply) => {
		const h = host();
		const p = await openForm(h);
		edit(p);
		h.state.reply = {
			ok: false,
			closed: true,
			error: { code: "X", message: "Gone" },
			...reply,
		};
		await saveToolDraft(p, h.d);
		return h.state.log.find((l) => l.startsWith("notify:"));
	};
	for (const [name, reply, want] of [
		[
			"told: the reply in the chat is promised",
			{ assistant_told: true },
			"notify:Couldn't save this: Gone. The reply in the chat explains what to do.",
		],
		[
			"not told: the person is asked to say it in the chat",
			{ assistant_told: false },
			"notify:Couldn't save this: Gone. Jarvis could not be told about it, so ask in the chat.",
		],
		["no flag: neither is said", {}, "notify:Couldn't save this: Gone."],
		[
			"told by a note only: no reply is promised",
			{ assistant_told: true, assistant_replies: false },
			"notify:Couldn't save this: Gone.",
		],
	])
		it(`closed note, ${name}`, async () => expect(await closedNote(reply)).toBe(want));

	// Break: the person pressed X while the save ran; the refusal marks a form nobody sees and says nothing.
	it("fixable reply after the form was closed with the X: a toast with the server's message", async () => {
		const h = host();
		const p = await openForm(h);
		edit(p);
		h.state.reply = fixable([{ fieldname: "title", label: "title", message: "Bad" }]);
		const inner = h.d.save;
		h.d.save = async (...a) => {
			h.d.closePanel();
			return inner(...a);
		};
		expect(await saveToolDraft(p, h.d)).toBe("keep-closed");
		expect(h.state.log).toContain("notify:Hours must be set.");
		expect(h.state.log).not.toContain("afterMarks");
		expect(p.fields[0].serverMissing).toBeUndefined();
	});

	// Break: a thrown error closes the form or leaves it saving for good.
	it("a thrown error keeps the form, shows an error, and clears the busy state", async () => {
		const h = host({
			save: async () => {
				throw new Error("network");
			},
		});
		const p = await openForm(h);
		edit(p);
		expect(await saveToolDraft(p, h.d)).toBe("error");
		expect(p.error.message).toBe("Could not save your changes. (network)");
		expect([
			h.state.panel === p,
			p.applying,
			h.state.items[0].busy,
			h.d.inflight.size,
		]).toEqual([true, false, false, 0]);
	});

	// Break: a second press while one is in flight sends a second save.
	it("a second Save while one is in flight does nothing", async () => {
		let release;
		const gate = new Promise((r) => (release = r));
		const h = host();
		const p = await openForm(h);
		edit(p);
		h.state.reply = { ok: true };
		const inner = h.d.save;
		h.d.save = async (...a) => {
			await gate;
			return inner(...a);
		};
		const first = saveToolDraft(p, h.d);
		expect(await saveToolDraft(p, h.d)).toBe("ignored");
		release();
		expect(await first).toBe("saved");
		expect(h.state.log.filter((l) => l.startsWith("save:"))).toHaveLength(1);
	});

	// Break: Save is live on a blocked form, a dead card, a busy item or while a turn runs.
	it("does nothing when blocked, dead, busy or while Jarvis is replying", async () => {
		for (const [name, arrange] of [
			["blocked", (h, p) => (p.saveBlocked = CANT_EDIT_TEXT)],
			["no such card", (h) => (h.state.items = [])],
			["the item is busy", (h) => (h.state.items[0].busy = true)],
			["a turn is running", (h) => (h.state.streaming = true)],
		]) {
			const h = host();
			const p = await openForm(h);
			edit(p);
			arrange(h, p);
			expect([name, await saveToolDraft(p, h.d)]).toEqual([name, "ignored"]);
			expect(h.state.log).toEqual([]);
		}
	});

	// Break: nothing changed is sent as an edited save ("saved with edited values" receipt) or
	// does nothing at all.
	it("nothing changed: Save is plain Create, and the form closes once the card is gone", async () => {
		const h = host();
		const p = await openForm(h);
		h.state.createEffect = async () => (h.state.items = []);
		expect(await saveToolDraft(p, h.d)).toBe("created");
		expect(h.state.log).toEqual(["create", "closePanel"]);
		expect(h.state.log).not.toContain("save");
	});

	it("nothing changed and Create refused: the form stays with the card's error", async () => {
		const h = host();
		const p = await openForm(h);
		h.state.createEffect = async (pa) => (pa.error = { message: "Not now" });
		expect(await saveToolDraft(p, h.d)).toBe("kept");
		expect([h.state.panel === p, p.error.message, p.applying]).toEqual([
			true,
			"Not now",
			false,
		]);
	});

	// Break (the phone): Save, plain Create and Cancel read or write the busy flags and the error on
	// the ITEM instead of through the host's accessor, so a pending read that replaces the item
	// objects loses them mid-call. With `d.where` the item is never touched.
	it("with an injected accessor the item's own flags and error are never read or written", async () => {
		const h = host();
		const flags = { busy: false, busyKind: "", error: undefined };
		const calls = [];
		h.d.where = {
			read: () => ({ ...flags }),
			setKind: (_, k) => (calls.push(["kind", k]), (flags.busyKind = k)),
			setBusy: (_, b) => (calls.push(["busy", b]), (flags.busy = b)),
		};
		const p = await openForm(h);
		const item = h.state.items[0];
		edit(p);
		h.state.reply = { ok: true, verb: "create", name: "TS-1" };
		expect(await saveToolDraft(p, h.d)).toBe("saved");
		// (the saved card left the list, so there is nothing to clear afterwards)
		expect(calls).toEqual([
			["busy", true],
			["kind", "save"],
		]);
		expect([item.busy, item.busyKind]).toEqual([false, ""]);
		// a refused plain Create: the error comes from the accessor, not from the item
		const h2 = host();
		h2.d.where = {
			read: () => ({ busy: false, busyKind: "", error: { message: "From the store" } }),
			setKind() {},
			setBusy() {},
		};
		const p2 = await openForm(h2);
		h2.state.items[0].error = { message: "On the item" };
		expect(await saveToolDraft(p2, h2.d)).toBe("kept");
		expect(p2.error.message).toBe("From the store");
	});

	// Break: the late answer of a chat the person left closes or marks the NEW chat's form.
	it("a reply that lands after the chat changed touches nothing", async () => {
		const h = host();
		const p = await openForm(h);
		edit(p);
		h.state.reply = { ok: true };
		const inner = h.d.save;
		h.d.save = async (...a) => {
			h.state.chat = "c2";
			return inner(...a);
		};
		expect(await saveToolDraft(p, h.d)).toBe("stale");
		expect(h.state.log).toEqual(['save:{"title":"Edited"}']);
	});
});

describe("cancelToolItem and discardFromForm: Cancel on the card, Discard in the form", () => {
	// Break: Discard in the form leaves the form open on a gone card (the dead-token banner on the
	// person's own discard), or closes it while the card is still there.
	it("the form closes when the card is gone, and was never shown dead", async () => {
		const h = host();
		const p = await openForm(h);
		h.state.discardEffect = async () => {
			h.d.removePending("t1");
			await h.d.reload();
		};
		expect(await discardFromForm(p, h.d)).toBe("cancelled");
		expect(h.state.panel).toBeNull();
		expect(h.state.deadAt).toEqual([false, false]);
		expect(p.applying).toBe(false);
	});

	it("a refused discard keeps the form and shows the card's error", async () => {
		const h = host();
		const p = await openForm(h);
		h.state.discardEffect = async (pa) => (pa.error = { message: "Busy" });
		expect(await discardFromForm(p, h.d)).toBe("kept");
		expect([h.state.panel === p, p.error.message, p.applying]).toEqual([true, "Busy", false]);
	});

	// Break: Cancel on the card with the form open leaves the form on a gone card.
	it("the card's own Cancel closes the open form of that card, and only that card's", async () => {
		const h = host();
		await openForm(h);
		h.state.discardEffect = async () => h.d.removePending("t1");
		expect(await cancelToolItem(h.state.items[0], h.d)).toBe("cancelled");
		expect(h.state.panel).toBeNull();
	});

	it("without a form it is just the discard; a discard that did not start is ignored", async () => {
		const h = host();
		expect(await cancelToolItem(h.state.items[0], h.d)).toBe("cancelled");
		h.state.discardIgnored = true;
		expect(await cancelToolItem(h.state.items[0], h.d)).toBe("ignored");
	});

	it("a form that is already saving is not discarded", async () => {
		const h = host();
		const p = await openForm(h);
		p.applying = true;
		expect(await discardFromForm(p, h.d)).toBe("ignored");
		expect(h.state.log).toEqual([]);
	});
});
