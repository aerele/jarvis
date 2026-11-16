// The phone's host of the shared server draft card (pwa/src/lib/draftCardHost.js). The phone's own
// runner (node --test) cannot import the shared host chain, so the parts that need it run here.
import { describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { reactive } from "vue";
import fs from "fs";
import path from "path";
import DraftCard from "./chat/DraftCard.vue";
import { draftCardView } from "../lib/draftCardView.js";
import { chatSettledReason } from "../lib/chatCardActions.js";
import { makeDraftUi } from "../../../pwa/src/lib/draftCardUi.js";
import { DRAFT_STALE_TEXT } from "../../../pwa/src/lib/draftCardDecision.js";
import {
	keepBusyItems,
	phoneDraftModel,
	runDraftDecision,
} from "../../../pwa/src/lib/draftCardHost.js";

const FIXTURES = JSON.parse(
	fs.readFileSync(path.resolve(__dirname, "../lib/draftCardFixtures.json"), "utf8")
);
const cardOf = (id) => structuredClone(FIXTURES.find((f) => f.id === id).card);
const SUBMIT = { kind: "submit_doc", doctype: "Sales Invoice", name: "SINV-1", rows: [] };
// A pending item as the phone's list holds it (pendingActionsFromRows / realtime).
const item = (token, card, extra = {}) => ({
	token,
	conversation: "C1",
	tool: "create_doc",
	summary: "",
	preview: { card },
	created_at: Number(token.replace(/\D/g, "")) || 1,
	recent: true,
	...extra,
});
const STACK_HINT = 'or type "confirm all", or "confirm 1 and 3"';
const model = (items, o = {}) =>
	phoneDraftModel({
		items,
		streaming: false,
		stackHint: STACK_HINT,
		ui: makeDraftUi(),
		...o,
	});

describe("phoneDraftModel: what the chat draws for its pending list", () => {
	// Break: a sensitive card, an ordinary gated tool or a typed card gets a draft card (the card would
	// then be confirmed with less detail), or a draft card is missed; or the place number of a card
	// drifts from its place in the list the tiles are numbered from.
	it("gives a ctx to draft and unreadable cards only, numbered by place in the whole stack", () => {
		const items = [
			item("t1", cardOf("create-simple")),
			item("t2", cardOf("sensitive")),
			item("t3", SUBMIT, { tool: "submit_doc" }),
			item("t4", cardOf("unreadable")),
			item("t5", cardOf("update-diff"), { recent: false }),
		];
		const { ctxMap, parked } = model(items);
		expect(Object.keys(ctxMap)).toEqual(["t1", "t4", "t5"]);
		expect(Object.values(ctxMap).map((c) => [c.position, c.count])).toEqual([
			[1, 5],
			[4, 5],
			[5, 5],
		]);
		expect(ctxMap.t5.earlier).toBe(true);
		expect(parked).toBe(true);
	});

	// Break: the one-card signal is raised by a sensitive or ordinary card (a typed draft would vanish
	// for nothing), or not raised by a draft.
	it.each([
		["a draft", [item("t1", cardOf("create-simple"))], true],
		["an unreadable draft", [item("t1", cardOf("unreadable"))], true],
		["a sensitive card", [item("t1", cardOf("sensitive"))], false],
		["an ordinary tool", [item("t1", SUBMIT)], false],
		["nothing", [], false],
	])("parked: %s", (_, items, parked) => expect(model(items).parked).toBe(parked));

	// Break: the phone card is drawn on the desktop surface (8 table rows, 28px buttons), or Preview
	// and Edit are live before their sheets exist (they would emit events nothing handles).
	it("draws on the phone surface with Preview and Edit disabled", () => {
		const items = [item("t1", cardOf("create-with-table"))];
		const ctx = model(items).ctxMap.t1;
		const w = mount(DraftCard, { props: { card: cardOf("create-with-table"), ctx } });
		expect(w.classes()).toContain("dc-phone");
		const states = Object.fromEntries(
			w.findAll("button").map((b) => [b.text(), b.attributes("disabled") !== undefined])
		);
		expect(states).toEqual({ Create: false, Preview: true, Edit: true, Cancel: false });
	});

	// Break (P5): the typed hint is wrong for where the card sits. The stack hint is drawn ONCE, on
	// the last card when it can show it, else under the stack; the cards above name their own number;
	// with no draft card the line is the phone's hint exactly as before.
	// Each row: [items, streaming, lines under the stack, hints drawn inside the draft cards by token]
	const D = (n, extra) => item(`t${n}`, cardOf("create-simple"), extra);
	const L = (n) => item(`t${n}`, SUBMIT, { tool: "submit_doc" });
	it.each([
		["no draft card: today's line", [L(1), L(2)], false, STACK_HINT, {}],
		["one legacy card", [L(1)], false, STACK_HINT, {}],
		["a lone draft card draws its own", [D(1)], false, "", { t1: 'or type "go ahead"' }],
		[
			"draft then draft: the last one carries the stack hint",
			[D(1), D(2)],
			false,
			"",
			{ t1: 'or type "confirm 1"', t2: STACK_HINT },
		],
		[
			"draft then legacy: the line under the stack is the hint",
			[D(1), L(2)],
			false,
			STACK_HINT,
			{ t1: 'or type "confirm 1"' },
		],
		["legacy then draft", [L(1), D(2)], false, "", { t2: STACK_HINT }],
		[
			"last draft card is replying: it draws none, the line is under the stack",
			[D(1), D(2)],
			true,
			STACK_HINT,
			{},
		],
		[
			"an Earlier draft card names its number",
			[D(1, { recent: false })],
			false,
			"",
			{ t1: 'or type "confirm 1"' },
		],
	])("%s", (_, items, streaming, line, inside) => {
		const { ctxMap, hintLine } = model(items, { streaming });
		expect(hintLine).toBe(line);
		const drawn = {};
		for (const pa of items)
			if (ctxMap[pa.token]) {
				const hint = draftCardView(cardOf("create-simple"), ctxMap[pa.token]).hint;
				if (hint) drawn[pa.token] = hint;
			}
		expect(drawn).toEqual(inside);
	});
});

describe("state by token: a pending read that replaces the items", () => {
	// Break (P3): busy kind kept on the item, so the next read gives the card back with no spinner
	// (or a second Create press) in the middle of its own Create.
	it("keeps the card's spinner and locks its buttons when the item objects are replaced", async () => {
		const ui = makeDraftUi(reactive);
		const first = item("t1", cardOf("create-simple"));
		let release;
		const run = runDraftDecision("create", first, {
			...deps(ui),
			confirm: () => new Promise((res) => (release = res)),
		});
		const replaced = [item("t1", cardOf("create-simple"))]; // what loadPending hands back
		const ctx = phoneDraftModel({ items: replaced, streaming: false, stackHint: "", ui })
			.ctxMap.t1;
		const view = draftCardView(cardOf("create-simple"), ctx);
		expect(view.state).toBe("S4");
		expect(view.buttons.find((b) => b.id === "create").loading).toBe(true);
		expect(view.buttons.every((b) => b.disabled)).toBe(true);
		release({ ok: true });
		await run;
	});

	// Break: a read that does not list the card in flight takes it off the screen in the middle of
	// its own Create or Cancel; or a card that is not in flight is kept past a clean read.
	it("keeps an in-flight card a read left out, and only that one", () => {
		const a = item("t1", cardOf("create-simple"));
		const b = item("t2", cardOf("create-simple"));
		const fresh = [item("t3", cardOf("create-simple"))];
		expect(keepBusyItems(fresh, [a, b], ["t1"]).map((x) => x.token)).toEqual(["t3", "t1"]);
		expect(keepBusyItems(fresh, [a, b], [])).toBe(fresh);
		const same = [item("t1", cardOf("create-simple"))];
		expect(keepBusyItems(same, [a], ["t1"])).toBe(same);
	});
});

// A fake view with every effect recorded; `calls` is the order they happened in.
function deps(ui, over = {}) {
	const calls = [];
	const d = {
		ui,
		agent: "Jarvis",
		calls,
		confirm: vi.fn(async () => ({ ok: true })),
		dismiss: vi.fn(async () => ({ ok: true })),
		streaming: () => false,
		current: () => true,
		removePending: (t) => calls.push(["remove", t]),
		settle: (t) => calls.push(["settle", t]),
		say: (t) => calls.push(["say", t]),
		reload: vi.fn(async () => calls.push(["reload"])),
		reloadPending: vi.fn(async () => calls.push(["reloadPending"])),
		...over,
	};
	return d;
}
const INVALID = { type: "InvalidConfirmation" };

describe("runDraftDecision: what each reply does on screen", () => {
	// [name, kind, how the call answers, result, card gone, error line, banner line]
	// Break: a spent card stays (pressable twice), a pending card is taken away, a failure is silent,
	// or the receipt is never reloaded (a final draft starts no agent turn, so nothing else will).
	const stale = "This record changed after the draft was made. Nothing was saved.";
	const ROWS = [
		["Create ok", "create", { ok: true }, "gone", true, "", ""],
		[
			"Create busy",
			"create",
			{ ok: false, reason_code: "busy" },
			"kept",
			false,
			"This action is being handled right now. Try again in a moment.",
			"",
		],
		[
			"Create record changed",
			"create",
			{ ok: false, reason_code: "stale", pa_status: "Failed", error: INVALID },
			"gone",
			true,
			"",
			stale,
		],
		[
			"Create token already used",
			"create",
			{ ok: false, error: INVALID },
			"gone",
			true,
			"",
			"Couldn't confirm. It may have been handled elsewhere. Refresh, or ask Jarvis to try again.",
		],
		[
			"Create tool failed (receipt)",
			"create",
			{ ok: false, reason_code: "failed", pa_status: "Failed" },
			"gone",
			true,
			"",
			"",
		],
		[
			"Create thrown",
			"create",
			new Error("Failed to fetch"),
			"kept",
			false,
			"Failed to fetch",
			"",
		],
		["Cancel ok", "cancel", { ok: true }, "gone", true, "", ""],
		[
			"Cancel already handled",
			"cancel",
			{ ok: true, status: "already_handled" },
			"gone",
			true,
			"",
			"",
		],
		[
			"Cancel refused",
			"cancel",
			{ ok: false, error: { message: "Storage is down" } },
			"kept",
			false,
			"Storage is down",
			"",
		],
		["Cancel thrown", "cancel", new Error("offline"), "kept", false, "offline", ""],
	];
	it.each(ROWS)("%s", async (_, kind, answer, result, gone, error, say) => {
		const ui = makeDraftUi(reactive);
		const pa = item("t1", cardOf("create-simple"));
		const reply = vi.fn(async () => {
			if (answer instanceof Error) throw answer;
			return answer;
		});
		const d = deps(ui, kind === "create" ? { confirm: reply } : { dismiss: reply });
		expect(await runDraftDecision(kind, pa, d)).toBe(result);
		expect(reply.mock.calls).toEqual([["t1", "C1"]]);
		expect(kind === "create" ? d.dismiss : d.confirm).not.toHaveBeenCalled();
		// the card is gone with both reloads (in that order, after it left the list), or it stays
		// with an error line and the buttons usable again
		expect(d.calls.map((c) => c[0])).toEqual(
			gone ? ["remove", "settle", ...(say ? ["say"] : []), "reload", "reloadPending"] : []
		);
		if (say) expect(d.calls.find((c) => c[0] === "say")[1]).toBe(say);
		expect(ui.error("t1")).toBe(error);
		expect(ui.busyKind("t1")).toBe("");
		expect(ui.busyTokens()).toEqual([]);
	});

	// Break: the "changed after the draft was made" line drifts from the desktop's.
	it("says what the desktop says when the record changed", () => {
		expect(DRAFT_STALE_TEXT).toBe(
			chatSettledReason({ ok: false, reason_code: "stale" }, { draft: true })
		);
	});

	// Break: a second tap starts a second confirm (a double write), or Cancel runs under a Create.
	it("ignores a second tap, of either button, while one is in flight", async () => {
		const ui = makeDraftUi(reactive);
		const pa = item("t1", cardOf("create-simple"));
		let release;
		const d = deps(ui, { confirm: vi.fn(() => new Promise((res) => (release = res))) });
		const first = runDraftDecision("create", pa, d);
		expect(await runDraftDecision("create", pa, d)).toBe("ignored");
		expect(await runDraftDecision("cancel", pa, d)).toBe("ignored");
		expect(d.confirm).toHaveBeenCalledTimes(1);
		expect(d.dismiss).not.toHaveBeenCalled();
		expect(ui.busyKind("t1")).toBe("create");
		release({ ok: true });
		expect(await first).toBe("gone");
		// and once it is over the card could be pressed again (it is gone here, but the lock is released)
		expect(ui.busyKind("t1")).toBe("");
	});

	// Break: Create runs while the agent is still replying (a confirm must not start a turn beside
	// the one that parked the card).
	it("does nothing while the turn is running", async () => {
		const d = deps(makeDraftUi(), { streaming: () => true });
		expect(await runDraftDecision("create", item("t1", cardOf("create-simple")), d)).toBe(
			"ignored"
		);
		expect(d.confirm).not.toHaveBeenCalled();
	});

	// Break: a reply that lands after the person left the chat removes or reloads the NEW chat's lists.
	it("touches nothing when the chat was left during the call", async () => {
		let here = true;
		const ui = makeDraftUi(reactive);
		const d = deps(ui, {
			current: () => here,
			confirm: vi.fn(async () => {
				here = false;
				return { ok: true };
			}),
		});
		expect(await runDraftDecision("create", item("t1", cardOf("create-simple")), d)).toBe(
			"stale"
		);
		expect(d.calls).toEqual([]);
		expect(ui.error("t1")).toBe("");
	});

	// Break: a failing reload after a successful Create shows an error line on a card that is gone
	// (or throws into the click handler).
	it("a failed reload after a done decision is not an error", async () => {
		const ui = makeDraftUi(reactive);
		const d = deps(ui, {
			reload: vi.fn(async () => {
				throw new Error("boom");
			}),
		});
		expect(await runDraftDecision("create", item("t1", cardOf("create-simple")), d)).toBe(
			"gone"
		);
		expect(ui.error("t1")).toBe("");
		expect(d.calls.map((c) => c[0])).toEqual(["remove", "settle"]);
	});
});
