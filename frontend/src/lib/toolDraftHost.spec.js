// The desktop chat's pure decisions about a server draft card (lib/toolDraftHost.js). The
// wiring in ChatView.vue is pinned in views/ChatView.toolDraft.spec.js.
import { describe, expect, it } from "vitest";
import fs from "fs";
import path from "path";
import { draftCardView } from "./draftCardView.js";
import {
	hasParkedDraft,
	nextPanelStale,
	toolDraftHostCtx,
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

describe("toolDraftHostCtx through the card view (the buttons the person gets)", () => {
	const buttons = (pa, streaming = false) =>
		draftCardView(card("create-simple"), toolDraftHostCtx({ pa, streaming })).buttons.map(
			(b) => `${b.id}${b.disabled ? ":off" : ""}${b.loading ? ":spin" : ""}`
		);
	const fresh = { token: "a", recent: true };
	// Break: Preview or Edit is clickable before the panel handles them, Create or Cancel is
	// disabled on a ready card, busy shows the wrong spinner, or the turn flag is dropped.
	const table = [
		["ready (S1)", fresh, false, "S1", ["create", "preview:off", "edit:off", "cancel"]],
		[
			"earlier (S2)",
			{ ...fresh, recent: false },
			false,
			"S2",
			["create", "preview:off", "edit:off", "cancel"],
		],
		[
			"create in flight (S4)",
			{ ...fresh, busy: true, busyKind: "create" },
			false,
			"S4",
			["create:off:spin", "preview:off", "edit:off", "cancel:off"],
		],
		[
			"cancel in flight (S4)",
			{ ...fresh, busy: true, busyKind: "cancel" },
			false,
			"S4",
			["create:off", "preview:off", "edit:off", "cancel:off:spin"],
		],
		[
			"a turn is running (S13)",
			fresh,
			true,
			"S13",
			["create:off", "preview:off", "edit:off", "cancel:off"],
		],
	];
	for (const [name, pa, streaming, state, want] of table) {
		it(`${name}`, () => {
			expect(
				draftCardView(card("create-simple"), toolDraftHostCtx({ pa, streaming })).state
			).toBe(state);
			expect(buttons(pa, streaming)).toEqual(want);
		});
	}

	it("always the desktop surface, never editing", () => {
		const ctx = toolDraftHostCtx({ pa: { token: "a", busyKind: "save" }, streaming: false });
		expect([ctx.surface, ctx.editing, ctx.readOnly]).toEqual(["desktop", false, true]);
	});
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
