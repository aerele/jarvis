// The phone's edit sheet of a server draft card: the host (pwa/src/lib/draftSheetHost.js) run with
// fake I/O, and the sheet component (pwa/src/components/DraftSheet.vue) mounted. The decisions are
// the shared ones (toolDraftHost.js and friends, tested on their own); what is held here is the
// WIRING: that the phone's host feeds them what they need and shows what they answer. The phone's
// own runner (node --test) cannot import the shared host chain, hence vitest.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { defineComponent, h, nextTick, reactive, ref } from "vue";
import DraftCard from "./chat/DraftCard.vue";
import DraftSheet from "../../../pwa/src/components/DraftSheet.vue";
import DraftEditForm from "./chat/DraftEditForm.vue";
import { makeDraftUi } from "../../../pwa/src/lib/draftCardUi.js";
import {
	keepBusyItems,
	phoneDraftModel,
	runDraftDecision,
} from "../../../pwa/src/lib/draftCardHost.js";
import { makeDraftSheet } from "../../../pwa/src/lib/draftSheetHost.js";
import { TIMESHEET_DETAIL, timesheetCard } from "../lib/timesheetDraftFixture.js";
import { CANT_EDIT_TEXT } from "../lib/toolDraftPanel.js";
import { DRAFT_STALE_TEXT } from "../lib/toolDraftHost.js";

beforeEach(() => {
	// DraftLinkMenu places itself with these; jsdom has neither.
	document.elementFromPoint = () => null;
	globalThis.ResizeObserver = class {
		observe() {}
		disconnect() {}
	};
});
afterEach(() => {
	vi.useRealTimers();
	document.body.innerHTML = "";
	document.body.style.overflow = "";
});

const F = (fieldname, label, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});
// The server's get_doctype_form_meta for the Timesheet card (fixtures/timesheetDraftFixture.js).
const META = {
	ok: true,
	title_field: "",
	is_submittable: 1,
	fields: [
		F("naming_series", "Series"),
		F("employee", "Employee", "Link", { options: "Employee" }),
		F("parent_project", "Project", "Link", { options: "Project" }),
		F("start_date", "Start Date", "Date"),
	],
	tables: { time_logs: TIMESHEET_DETAIL },
};
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

// The host run inside a mounted component (it starts a link search that belongs to one), with
// every outside thing a fake that records its calls.
function harness({ meta = META, streaming = false } = {}) {
	const calls = {
		meta: [],
		save: [],
		search: [],
		confirm: [],
		dismiss: [],
		chat: [],
		list: [],
		say: [],
		removed: [],
		refreshChats: [],
	};
	const replies = { save: { ok: true, verb: "create", name: "TS-0001" }, confirm: { ok: true } };
	const items = ref([item("t1", timesheetCard())]);
	const ui = makeDraftUi(reactive);
	const flags = reactive({ streaming, generation: 1, receipt: false });
	const order = [];
	const state = {
		throwSave: null,
		onList: null,
		onMeta: null,
		onChat: null,
		onRemove: null,
		saveGate: null,
		beforeReply: null,
		onDecideReload: null,
	};
	const api = {
		getDoctypeFormMeta: async (doctype) => {
			calls.meta.push(doctype);
			if (state.onMeta) state.onMeta();
			return structuredClone(meta);
		},
		loadDocForEdit: async () => ({ values: {}, tables: {} }),
		ruleValues: async () => ({ ok: false }),
		searchLink: async (...a) => {
			calls.search.push(a);
			return [];
		},
		saveDraftCard: async (...a) => {
			calls.save.push(a);
			if (state.saveGate) await state.saveGate;
			if (state.beforeReply) state.beforeReply();
			if (state.throwSave) throw state.throwSave;
			return replies.save;
		},
	};
	const removePending = (token) => {
		calls.removed.push(token);
		order.push("remove");
		if (state.onRemove) state.onRemove();
		items.value = items.value.filter((p) => p.token !== token);
	};
	// Create and Cancel on the card are the phone's real runDraftDecision over fake calls.
	const decide = (kind, pa) =>
		runDraftDecision(kind, pa, {
			ui,
			agent: "Jarvis",
			confirm: async (...a) => {
				calls.confirm.push(a);
				return replies.confirm;
			},
			dismiss: async (...a) => {
				calls.dismiss.push(a);
				return replies.dismiss || { ok: true };
			},
			streaming: () => flags.streaming,
			current: () => true,
			removePending,
			settle: () => {},
			say: (t) => calls.say.push(t),
			reload: async () => {
				if (state.onDecideReload) state.onDecideReload();
			},
			reloadPending: async () => {},
			receiptShown: () => flags.receipt,
		});
	const io = {
		ui,
		api,
		items: () => items.value,
		conversation: () => "C1",
		generation: () => flags.generation,
		isCurrent: (cid, generation) => cid === "C1" && generation === flags.generation,
		streaming: () => flags.streaming,
		agent: () => "Jarvis",
		removePending,
		loadChat: async () => {
			calls.chat.push("chat");
			order.push("chat");
			if (state.onChat) state.onChat();
		},
		loadPending: async (source) => {
			calls.list.push(source || "");
			order.push("list");
			if (state.onList) state.onList();
		},
		refreshChats: () => calls.refreshChats.push(1),
		say: (t) => calls.say.push(t),
		receiptShown: () => flags.receipt,
		create: (pa) => decide("create", pa),
		discard: (pa) => decide("cancel", pa),
	};
	let host;
	const wrapper = mount(
		defineComponent({
			setup() {
				host = makeDraftSheet(io);
				return () => h("div");
			},
		})
	);
	return { host, wrapper, calls, replies, items, ui, flags, state, io, order };
}
const opened = async (over) => {
	const t = harness(over);
	await t.host.openEdit(t.items.value[0]);
	return t;
};
const valueOf = (host, fieldname) =>
	host.panel.value.fields.find((f) => f.fieldname === fieldname).value;
const hours = (host) => host.panel.value.tables[0].rows.map((r) => r.hours);
const edit = (host, fieldname, value) => {
	host.panel.value.fields.find((f) => f.fieldname === fieldname).value = value;
};

describe("open: build, snapshot, guard, then show", () => {
	// Break: the phone's builder reads a field by label instead of its fieldname, forgets the
	// calculated cells the card shows (Hrs), or shows a form before it holds the card's values.
	it("opens the form with the draft's values and the card's calculated cells", async () => {
		const t = harness();
		expect(await t.host.openEdit(t.items.value[0])).toBe("opened");
		expect(t.host.open.value).toBe(true);
		expect(t.host.panel.value.token).toBe("t1");
		expect(valueOf(t.host, "employee")).toBe("HR-EMP-00001");
		expect(valueOf(t.host, "parent_project")).toBe("PROJ-0001");
		expect(valueOf(t.host, "start_date")).toBe("2026-12-08");
		expect(hours(t.host)).toEqual(["3", "1.50", "2"]);
		expect(t.calls.meta).toEqual(["Timesheet"]);
		expect(t.host.footer.value.primaryDisabled).toBe(false);
		expect(t.host.notices.value).toEqual([]);
	});

	// Break: the card does not say "Editing" while the sheet is open, or stays pressable while the
	// form is being built (a second tap would start a second build).
	it("tells the card it is being edited and, while it builds, that it is locked", async () => {
		const t = harness();
		const ctx = () =>
			phoneDraftModel({
				items: t.items.value,
				streaming: false,
				stackHint: "",
				ui: t.ui,
				editingToken: t.host.editingToken.value,
				buildingToken: t.host.building.value,
			}).ctxMap.t1;
		const building = t.host.openEdit(t.items.value[0]);
		await Promise.resolve();
		expect(t.host.building.value).toBe("t1");
		expect(ctx().busy).toBe("busy");
		await building;
		expect(t.host.building.value).toBe("");
		expect(ctx().editing).toBe(true);
		const card = mount(DraftCard, { props: { card: timesheetCard(), ctx: ctx() } });
		const buttons = Object.fromEntries(
			card.findAll("button").map((b) => [b.text(), b.attributes("disabled") !== undefined])
		);
		expect(buttons.Edit).toBe(true);
		expect(buttons.Cancel).toBe(false);
	});

	// Break: a form that does not match its card opens with a live Save (a wrong baseline would
	// send a wrong payload). It must open with Save off and the shared line. The card's value
	// changes while the form is being built, so the snapshot no longer equals the card.
	it("a form the guard refuses opens with Save off and the shared line", async () => {
		const t = harness();
		t.state.onMeta = () => {
			t.items.value[0].preview.card.draft.values.employee = "HR-EMP-00002";
		};
		expect(await t.host.openEdit(t.items.value[0])).toBe("opened");
		expect(t.host.panel.value.saveBlocked).toBe(CANT_EDIT_TEXT);
		expect(t.host.footer.value.primaryDisabled).toBe(true);
		expect(t.host.notices.value).toEqual([{ type: "error", text: CANT_EDIT_TEXT }]);
	});

	// Break: a failed build leaves the person with nothing said, or says it in a toast the phone
	// does not have. The words go under the card.
	it("a form that cannot be built says so under the card and opens nothing", async () => {
		const t = harness({ meta: { ok: false } });
		expect(await t.host.openEdit(t.items.value[0])).toBe("failed");
		expect(t.host.open.value).toBe(false);
		expect(t.ui.error("t1")).toBe("Could not load this draft. Try again.");
	});
});

const FIXABLE = {
	ok: false,
	error: {
		code: "ValidationError",
		message: "Project is wrong",
		fields: [
			{ fieldname: "parent_project", label: "Project", message: "Not found", invalid: true },
		],
	},
};
const CLOSED = (extra = {}) => ({
	ok: false,
	closed: true,
	assistant_told: true,
	error: { message: "The record changed" },
	...extra,
});

describe("Save sends only the change; nothing changed is a plain Create", () => {
	// Break: the phone sends the whole form (a drafted value the person never touched is written
	// again) or the wrong conversation / token.
	it("one edited field is the whole payload", async () => {
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		await t.host.save();
		expect(t.calls.save).toEqual([["t1", "C1", { parent_project: "PROJ-0002" }]]);
	});

	// Break: Save with nothing changed calls save_draft_card (the server then records "saved with
	// edited values"), or does not create at all.
	it("nothing changed: no save call, the card's Create runs once, the sheet closes", async () => {
		const t = await opened();
		expect(await t.host.save()).toBe("created");
		expect(t.calls.save).toEqual([]);
		expect(t.calls.confirm).toEqual([["t1", "C1"]]);
		expect(t.host.open.value).toBe(false);
		expect(t.items.value).toEqual([]);
	});

	// Break: a refused plain Create leaves the form open with no words (the error stays on the card
	// behind the sheet), or closes it.
	it("a refused plain Create keeps the form and shows the card's words in it", async () => {
		const t = await opened();
		t.replies.confirm = { ok: false, reason_code: "busy" };
		expect(await t.host.save()).toBe("kept");
		expect(t.host.open.value).toBe(true);
		expect(t.host.panel.value.error).toBe(
			"This action is being handled right now. Try again in a moment."
		);
		expect(t.items.value).toHaveLength(1);
	});
});

describe("what each reply of Save does on screen", () => {
	// Each row: [name, reply, edits the form still holds when the reply lands?, expected]
	// expected = { result, open (the sheet), card (still listed), cardError, say, panelError }
	const SAVED = { ok: true, verb: "create", name: "TS-0001" };
	const ROWS = [
		["saved", SAVED, {}, { result: "saved", open: false, card: false, say: [] }],
		[
			"fixable",
			FIXABLE,
			{},
			{ result: "keep", open: true, card: true, panelError: "Project is wrong", say: [] },
		],
		[
			"closed, the card is still waiting",
			CLOSED({ card_open: true }),
			{},
			{
				result: "closed-open",
				open: false,
				card: true,
				cardError: "Couldn't save this: The record changed.",
				say: [],
			},
		],
		[
			"closed, the edits were undone while it saved",
			CLOSED(),
			{ undo: true },
			{
				result: "closed",
				open: false,
				card: false,
				say: [
					"Couldn't save this: The record changed. The reply in the chat explains what to do.",
				],
			},
		],
		[
			"closed, the form holds edits",
			CLOSED(),
			{},
			{
				result: "closed-dead",
				open: true,
				card: false,
				panelError:
					"Couldn't save this: The record changed. The reply in the chat explains what to do.",
				say: [],
			},
		],
		[
			"partial is never kept",
			CLOSED({ outcome: "partial" }),
			{},
			{
				result: "closed",
				open: false,
				card: false,
				say: [
					"Part of this may already be saved: The record changed. Check the record before trying again.",
				],
			},
		],
	];
	// Break: any reply puts the person on the wrong screen: a sheet left open on a gone card with no
	// banner, a closed sheet that loses the edits of a dead draft, the card taken away while it is
	// still pending, a refusal that is not shown. Each row is one reply kind of the contract.
	it.each(ROWS)("%s", async (_, reply, how, want) => {
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		t.replies.save = reply;
		if (how.undo) t.state.beforeReply = () => edit(t.host, "parent_project", "PROJ-0001");
		const result = await t.host.save();
		expect(result).toBe(want.result);
		expect(t.host.open.value).toBe(want.open);
		expect(t.items.value.length === 1).toBe(want.card);
		expect(t.calls.say).toEqual(want.say);
		if ("cardError" in want) expect(t.ui.error("t1")).toBe(want.cardError);
		if ("panelError" in want) expect(t.host.panel.value.error.message).toBe(want.panelError);
		expect(t.ui.busyKind("t1")).toBe("");
		// the card is spent in these: both lists were read again, the chat first (P4)
		if (!want.card) expect(t.order.filter((x) => x !== "remove")).toEqual(["chat", "list"]);
		else expect(t.calls.chat).toEqual([]);
	});

	// Break: the "reply in the chat" sentence is promised when the server said no reply comes, or
	// "could not be told" is said when nothing says so.
	it.each([
		[
			"a reply follows",
			{ assistant_replies: true },
			"The reply in the chat explains what to do.",
		],
		["no reply follows", { assistant_replies: false }, ""],
		[
			"the assistant was not told",
			{ assistant_told: false, assistant_replies: false },
			"Jarvis could not be told about it, so ask in the chat.",
		],
	])("the note when %s", async (_, extra, tail) => {
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		t.replies.save = CLOSED(extra);
		await t.host.save();
		const text = t.host.panel.value.error.message;
		expect(text).toBe(`Couldn't save this: The record changed.${tail ? " " + tail : ""}`);
	});

	// Break: an offline or failed call closes the form and loses the edits, or leaves Save off.
	it("a thrown call keeps the sheet and the edits and Save usable", async () => {
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		t.state.throwSave = new Error("Failed to fetch");
		expect(await t.host.save()).toBe("error");
		expect(t.host.open.value).toBe(true);
		expect(valueOf(t.host, "parent_project")).toBe("PROJ-0002");
		expect(typeof t.host.panel.value.error.message).toBe("string");
		expect(t.host.footer.value.primaryDisabled).toBe(false);
		expect(t.ui.busyKind("t1")).toBe("");
		expect(t.items.value).toHaveLength(1);
	});

	// Break: the person closes the sheet while Save runs and a refusal is lost (the card is still
	// there, nothing says why).
	it("a fixable refusal that lands after the sheet was closed goes under the card", async () => {
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		t.replies.save = FIXABLE;
		let release;
		t.state.saveGate = new Promise((r) => (release = r));
		const saving = t.host.save();
		await Promise.resolve();
		t.host.requestClose();
		expect(t.host.open.value).toBe(false);
		release();
		expect(await saving).toBe("keep-closed");
		expect(t.ui.error("t1")).toBe("Project is wrong");
	});

	// Break: a closed reply whose receipt the reload already brought says it twice (banner and
	// receipt line).
	it("the chat banner is not used when the reloaded thread already holds the receipt", async () => {
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		t.replies.save = CLOSED();
		t.state.beforeReply = () => edit(t.host, "parent_project", "PROJ-0001");
		t.flags.receipt = true;
		await t.host.save();
		expect(t.calls.say).toEqual([]);
	});
});

describe("the card keeps its spinner while a read replaces the list", () => {
	// Break: the busy kind of a Save lives on the list item (a read replaces it: the spinner and the
	// guard against a second tap vanish), or it is not set at all.
	it("is held by token and survives a list of new objects", async () => {
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		let release;
		t.state.saveGate = new Promise((r) => (release = r));
		const saving = t.host.save();
		await Promise.resolve();
		expect(t.ui.busyKind("t1")).toBe("save");
		expect(t.ui.busyTokens()).toEqual(["t1"]);
		t.items.value = t.items.value.map((x) => ({ ...x })); // a loadPending replaced every object
		const ctx = phoneDraftModel({
			items: t.items.value,
			streaming: false,
			stackHint: "",
			ui: t.ui,
			editingToken: "t1",
		}).ctxMap.t1;
		expect(ctx.busy).toBe("create");
		// a second Save while one is in flight is ignored
		expect(await t.host.save()).toBe("ignored");
		expect(t.calls.save).toHaveLength(1);
		release();
		await saving;
		expect(t.ui.busyKind("t1")).toBe("");
	});
});

describe("a card that leaves the list while its form is open", () => {
	const dead = (t) => t.host.dead.value;
	// Break: the poll is not started, runs at another rate, reads only the list (the thread's own row
	// still says pending, so the card never leaves it), or goes on after the sheet closed.
	it("is found by the poll: banner, Save off, edits kept; the poll stops with the sheet", async () => {
		vi.useFakeTimers();
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		await vi.advanceTimersByTimeAsync(4999);
		expect(t.order).toEqual([]);
		t.state.onList = () => {
			t.items.value = []; // cancelled somewhere else
		};
		await vi.advanceTimersByTimeAsync(1);
		expect(t.order).toEqual(["chat", "list"]);
		expect(t.calls.list).toEqual(["auto"]);
		expect(dead(t)).toBe(true);
		expect(t.host.notices.value).toEqual([{ type: "error", text: DRAFT_STALE_TEXT }]);
		expect(t.host.footer.value.primaryDisabled).toBe(true);
		expect(t.host.footer.value.discardDisabled).toBe(true);
		expect(valueOf(t.host, "parent_project")).toBe("PROJ-0002");
		t.host.requestClose();
		t.order.length = 0;
		await vi.advanceTimersByTimeAsync(30000);
		expect(t.order).toEqual([]);
	});

	// Break: coming back to the app does not look again (a card handled while the phone slept stays
	// editable), or it looks while hidden, or after the sheet closed.
	it("is found when the app returns to the foreground", async () => {
		const t = await opened();
		const set = (state) => {
			Object.defineProperty(document, "visibilityState", {
				value: state,
				configurable: true,
			});
			document.dispatchEvent(new Event("visibilitychange"));
		};
		t.state.onList = () => {
			t.items.value = [];
		};
		set("hidden");
		await flushPromises();
		expect(t.order).toEqual([]);
		set("visible");
		await flushPromises();
		expect(t.order).toEqual(["chat", "list"]);
		expect(dead(t)).toBe(true);
		t.host.requestClose();
		t.order.length = 0;
		set("visible");
		await flushPromises();
		expect(t.order).toEqual([]);
	});

	// Break: the person's own Save shows the "changed while you were editing" banner at some moment
	// between the call and the receipt: while it is in flight (a read left the card out), when the
	// card is taken off the list, or after the reload.
	it("never shows the banner for the person's own Save", async () => {
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		const seen = [];
		let release;
		t.state.saveGate = new Promise((r) => (release = r));
		t.state.onRemove = () => seen.push(["remove", dead(t)]);
		t.state.onChat = () => seen.push(["chat", dead(t)]);
		const saving = t.host.save();
		await Promise.resolve();
		// a read raced the save and left the card out; the chat view keeps an in-flight card
		t.items.value = keepBusyItems([], t.items.value, t.ui.busyTokens());
		seen.push(["in flight", dead(t)]);
		release();
		await saving;
		seen.push(["after", dead(t)]);
		expect(seen).toEqual([
			["in flight", false],
			["remove", false],
			["chat", false],
			["after", false],
		]);
	});
});

describe("the person's own Discard draft", () => {
	// Break: the form is seen as "changed while you were editing" between the card leaving the list
	// and the sheet closing (the card's decision reloads the chat in between).
	it("never shows the banner while the card is taken off the list", async () => {
		const t = await opened();
		const seen = [];
		t.state.onDecideReload = () => seen.push([t.items.value.length, t.host.dead.value]);
		await t.host.discardDraft();
		expect(seen).toEqual([[0, false]]);
	});
});

describe("closing the sheet", () => {
	// Break: a clean sheet asks (an extra tap for nothing), a dirty one closes without asking (the
	// edits are lost), "Keep editing" closes, "Discard changes" discards the DRAFT (it only closes
	// the sheet), or the question is a window.confirm.
	it("a clean sheet closes at once", async () => {
		const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
		const t = await opened();
		expect(t.host.requestClose()).toBe("closed");
		expect(t.host.open.value).toBe(false);
		expect(t.host.confirming.value).toBe(false);
		expect(confirm).not.toHaveBeenCalled();
		confirm.mockRestore();
	});

	it("a dirty sheet asks inline; Keep editing stays; Discard changes closes and keeps the card", async () => {
		const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		expect(t.host.requestClose()).toBe("asking");
		expect(t.host.confirming.value).toBe(true);
		expect(t.host.open.value).toBe(true);
		t.host.keepEditing();
		expect(t.host.confirming.value).toBe(false);
		expect(valueOf(t.host, "parent_project")).toBe("PROJ-0002");
		t.host.requestClose(); // asks again
		expect(t.host.requestClose()).toBe("kept"); // Escape or back on the question: keep editing
		expect(t.host.confirming.value).toBe(false);
		t.host.requestClose();
		t.host.discardChanges();
		expect(t.host.open.value).toBe(false);
		expect(t.calls.save).toEqual([]);
		expect(t.calls.dismiss).toEqual([]);
		expect(t.calls.confirm).toEqual([]);
		expect(t.items.value).toHaveLength(1);
		expect(confirm).not.toHaveBeenCalled();
		confirm.mockRestore();
	});

	// Break: Escape (or back) with a link menu open closes the sheet instead of the menu, as
	// the sheet's own Escape takes the key before the field sees it.
	it("closes an open link menu first, even over unsaved edits", async () => {
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		t.host.link.draftLink.value = {
			key: "f:parent_project",
			items: [{ value: "PROJ-0003", label: "" }],
			open: true,
			anchor: null,
		};
		expect(t.host.requestClose()).toBe("menu");
		expect(t.host.link.draftLink.value.open).toBe(false);
		expect(t.host.open.value).toBe(true);
		expect(t.host.confirming.value).toBe(false);
	});

	// Break: a dead draft with edits asks "Discard your changes?" (the banner already said they were
	// not saved), or a saving sheet asks about edits that are being sent.
	it("a dead or saving sheet closes without asking", async () => {
		const dead = await opened();
		edit(dead.host, "parent_project", "PROJ-0002");
		dead.items.value = [];
		expect(dead.host.requestClose()).toBe("closed");
		const saving = await opened();
		edit(saving.host, "parent_project", "PROJ-0002");
		saving.state.saveGate = new Promise(() => {});
		saving.host.save();
		await Promise.resolve();
		expect(saving.host.requestClose()).toBe("closed");
	});

	// Break: the phone's back button leaves the chat (and its edits) with the sheet open, or
	// blocks leaving when no sheet is open.
	it("back closes (or asks about) the sheet and the chat stays; with no sheet it leaves", async () => {
		const t = await opened();
		expect(t.host.guardLeave()).toBe(false);
		expect(t.host.open.value).toBe(false);
		expect(t.host.guardLeave()).toBe(true);
		await t.host.openEdit(t.items.value[0]);
		edit(t.host, "parent_project", "PROJ-0002");
		expect(t.host.guardLeave()).toBe(false);
		expect(t.host.confirming.value).toBe(true);
		expect(t.host.open.value).toBe(true);
	});

	// Break: a conversation switch leaves the previous chat's form on screen over the new chat, or
	// its poll running.
	it("a conversation switch closes the sheet and stops the poll", async () => {
		vi.useFakeTimers();
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		t.host.reset();
		expect(t.host.open.value).toBe(false);
		expect(t.host.confirming.value).toBe(false);
		await vi.advanceTimersByTimeAsync(30000);
		expect(t.order).toEqual([]);
	});
});

describe("the footer's Discard draft is the card's Cancel", () => {
	// Break: it only closes the sheet (the draft stays), or it asks "Discard your changes?" first,
	// or it confirms the card.
	it("cancels the card through dismiss and closes the sheet", async () => {
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		expect(await t.host.discardDraft()).toBe("cancelled");
		expect(t.calls.dismiss).toEqual([["t1", "C1"]]);
		expect(t.calls.confirm).toEqual([]);
		expect(t.host.open.value).toBe(false);
		expect(t.items.value).toEqual([]);
	});

	// Break: a refused Cancel closes the sheet and loses the edits with no words.
	it("a refused Cancel keeps the sheet, the edits and shows the words", async () => {
		const t = await opened();
		edit(t.host, "parent_project", "PROJ-0002");
		// the real refusal of a card that is held right now (_lifecycle.py discard)
		t.replies.dismiss = {
			ok: false,
			reason_code: "busy",
			error: {
				type: "InvalidConfirmation",
				message: "This action is being handled right now.",
			},
		};
		expect(await t.host.discardDraft()).toBe("kept");
		expect(t.host.open.value).toBe(true);
		expect(t.host.panel.value.error).toBe(
			"This action is being handled right now. Try again in a moment."
		);
		expect(valueOf(t.host, "parent_project")).toBe("PROJ-0002");
	});
});

describe("calculated cells follow the person's edits on the phone too", () => {
	// Break: the phone host does not run the settle watcher: a row's Hrs keeps the card's number
	// beside an edit that makes it stale, or a text edit blanks it.
	it("a feeding edit blanks that row's Hrs, undoing it brings it back; Save sends no Hrs", async () => {
		const t = await opened();
		const rows = t.host.panel.value.tables[0].rows;
		expect(hours(t.host)).toEqual(["3", "1.50", "2"]);
		rows[1].project = "PROJ-0002";
		await nextTick();
		expect(hours(t.host)).toEqual(["3", "", "2"]);
		rows[1].description = "Only text";
		rows[1].project = "PROJ-0001";
		await nextTick();
		expect(hours(t.host)).toEqual(["3", "1.50", "2"]);
		rows[1].project = "PROJ-0002";
		await t.host.save();
		const sent = t.calls.save[0][2].time_logs;
		expect(sent.map((r) => "hours" in r)).toEqual([false, false, false]);
	});
});

describe("link search in the sheet", () => {
	// Break: the phone's link search never reaches the phone api (the picker stays empty), or asks
	// with the desktop's arguments out of order.
	it("a field's search goes through the phone api with the shared arguments", async () => {
		const t = await opened();
		const input = document.createElement("input");
		await t.host.link.onDraftLink("f:parent_project", () => "PROJ", "Project", {
			target: input,
		});
		expect(t.calls.search).toHaveLength(1);
		expect(t.calls.search[0].slice(0, 3)).toEqual(["Project", "PROJ", 8]);
	});
});

// ---- the sheet component --------------------------------------------------------------------
const mountSheet = async (t) => {
	const w = mount(DraftSheet, { props: { host: t.host }, attachTo: document.body });
	await flushPromises();
	return w;
};
const buttonsOf = (w) => w.findAll("footer button").map((b) => b.text());
const byText = (w, text) => w.findAll("button").find((b) => b.text() === text);

describe("DraftSheet: what the person sees", () => {
	// Break: the sheet is not full screen, the shared components do not get the phone's colours
	// (no jv-draft-tokens wrapper), the form is drawn on the desktop surface, or the footer
	// offers Create & Submit or the unclear "Discard".
	it("is the phone's full-height Sheet with the tokens, the phone form and an honest footer", async () => {
		const t = await opened();
		const w = await mountSheet(t);
		expect(w.find(".jv-sheet").classes()).toContain("is-full");
		expect(w.find(".jv-sheet .jv-draft-tokens").exists()).toBe(true);
		expect(w.find(".jv-draft-body").classes()).toContain("jv-surface-phone");
		expect(w.find(".jv-dsheet-title").text()).toBe("New Timesheet");
		expect(w.find(".jv-dsheet-badge").text()).toBe("Draft - not saved");
		expect(buttonsOf(w)).toEqual(["Create", "Discard draft"]);
		// a grid cell carries its column's name for the stacked row card
		const labels = w
			.findAll(".jv-grid tbody tr:first-child td[data-label]")
			.map((c) => c.attributes("data-label"));
		expect(labels).toEqual([
			"Activity Type",
			"From Time",
			"Hrs",
			"Project",
			"Is Billable",
			"Description",
			"To Time",
		]);
		w.unmount();
	});

	// Break: nothing is drawn while the host has no form, or the sheet stays open without one.
	it("draws nothing with no form", async () => {
		const t = harness();
		const w = await mountSheet(t);
		expect(w.find(".jv-sheet").exists()).toBe(false);
		w.unmount();
	});

	// Break: Save from the button sends the wrong thing or nothing; the button is live when the
	// host says off; the dead banner is missing from the body or Save stays on.
	it("Save sends the change; a dead card shows the banner and turns Save off", async () => {
		const t = await opened();
		const w = await mountSheet(t);
		edit(t.host, "parent_project", "PROJ-0002");
		await w.find("footer .is-primary").trigger("click");
		await flushPromises();
		expect(t.calls.save).toEqual([["t1", "C1", { parent_project: "PROJ-0002" }]]);
		w.unmount();

		const d = await opened();
		const w2 = await mountSheet(d);
		d.items.value = [];
		await flushPromises();
		expect(w2.find(".jv-draft-body [role=alert]").text()).toBe(DRAFT_STALE_TEXT);
		expect(w2.find("footer .is-primary").attributes("disabled")).toBeDefined();
		expect(byText(w2, "Discard draft").attributes("disabled")).toBeDefined();
		w2.unmount();
	});

	// Break: a refused Save is not shown at the top of the form body, or the first field the SERVER
	// marked is not scrolled to and focused (the person has to hunt for it).
	it("a fixable refusal shows its banner in the body and focuses the first marked field", async () => {
		const t = await opened();
		const w = await mountSheet(t);
		edit(t.host, "parent_project", "PROJ-0002");
		t.replies.save = FIXABLE;
		await w.find("footer .is-primary").trigger("click");
		await flushPromises();
		await nextTick();
		const banner = w.find(".jv-draft-body [role=alert]");
		expect(banner.text()).toBe("Project is wrong");
		const fields = w.findAll(".jv-draft-fld");
		expect(fields[2].find("input").element).toBe(document.activeElement);
		expect(fields[2].classes()).toContain("invalid");
		// the banner is the first thing in the scrolling body
		expect(
			w.find(".jv-draft-body").element.firstElementChild.querySelector("[role=alert]")
		).not.toBeNull();
		w.unmount();
	});

	// Break: a string error (copied from the card) is not drawn, only an envelope is.
	it("draws both a string error and an envelope", async () => {
		const t = await opened();
		const w = await mountSheet(t);
		t.host.panel.value.error = "Plain words";
		await nextTick();
		expect(w.find(".jv-dsheet-note").text()).toBe("Plain words");
		t.host.panel.value.error = { message: "Rich words", hint: "Try again in a moment." };
		await nextTick();
		expect(w.find(".jv-dsheet-note").text()).toContain("Rich words");
		expect(w.find(".jv-dsheet-hint").text()).toBe("Try again in a moment.");
		w.unmount();
	});
});

describe("DraftSheet: closing", () => {
	// Break: the close control or the Sheet's own Escape closes a dirty sheet outright, the question
	// is not inside the sheet, "Keep editing" does not take focus, or a clean sheet asks.
	it("clean: the close control closes; dirty: the question shows in the footer, Escape answers Keep editing", async () => {
		const clean = await opened();
		const w = await mountSheet(clean);
		await w.find(".jv-dsheet-close").trigger("click");
		await flushPromises();
		expect(clean.host.open.value).toBe(false);
		w.unmount();

		const t = await opened();
		const w2 = await mountSheet(t);
		edit(t.host, "parent_project", "PROJ-0002");
		await w2.find(".jv-dsheet-close").trigger("click");
		await flushPromises();
		expect(t.host.open.value).toBe(true);
		expect(w2.find(".jv-dsheet-ask-text").text()).toBe("Discard your changes?");
		expect(buttonsOf(w2)).toEqual(["Keep editing", "Discard changes"]);
		expect(document.activeElement).toBe(byText(w2, "Keep editing").element);
		// the Sheet takes Escape in the capture phase and asks the host to close: it answers the question
		document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
		await flushPromises();
		expect(w2.find(".jv-dsheet-ask-text").exists()).toBe(false);
		expect(buttonsOf(w2)).toEqual(["Create", "Discard draft"]);
		await byText(w2, "Discard draft").trigger("click");
		await flushPromises();
		expect(t.calls.dismiss).toEqual([["t1", "C1"]]);
		w2.unmount();
	});

	// Break: "Discard changes" discards the draft too (the card is gone and the record never made).
	it("Discard changes closes the sheet and the card stays", async () => {
		const t = await opened();
		const w = await mountSheet(t);
		edit(t.host, "parent_project", "PROJ-0002");
		await w.find(".jv-dsheet-close").trigger("click");
		await byText(w, "Discard changes").trigger("click");
		await flushPromises();
		expect(t.host.open.value).toBe(false);
		expect(t.calls.dismiss).toEqual([]);
		expect(t.items.value).toHaveLength(1);
		w.unmount();
	});
});

describe("the phone surface of the link menu", () => {
	// A menu for an anchor whose bottom edge is at `bottom` px, on a window `innerHeight` tall,
	// with a visual viewport (the part the keyboard leaves) `vvHeight` tall.
	const menuStyle = async ({ surface, bottom, innerHeight, vvHeight }) => {
		Object.defineProperty(window, "innerHeight", { value: innerHeight, configurable: true });
		window.visualViewport = vvHeight
			? { offsetTop: 0, height: vvHeight, addEventListener() {}, removeEventListener() {} }
			: undefined;
		const anchor = document.createElement("input");
		anchor.getBoundingClientRect = () => ({
			left: 10,
			top: bottom - 40,
			width: 300,
			bottom,
			height: 40,
		});
		const t = await opened();
		const form = mount(DraftEditForm, {
			props: {
				surface,
				model: t.host.panel.value,
				draftLink: {
					key: "f:employee",
					items: [{ value: "HR-1", label: "A" }],
					open: true,
					anchor,
				},
			},
			attachTo: document.body,
		});
		await flushPromises();
		const menu = document.body.querySelector(".jv-action-linkmenu");
		const seen = {
			top: menu.style.top,
			bottom: menu.style.bottom,
			classes: [...menu.classList],
		};
		form.unmount();
		return seen;
	};

	// Break: the phone's menu is the desktop's (12.5px, 30px rows), or (on iOS, where the keyboard
	// does not shrink the layout viewport) it opens downward into the keyboard.
	it("opens upward above the keyboard on the phone; the desktop's own rule is unchanged", async () => {
		const phone = await menuStyle({
			surface: "phone",
			bottom: 290,
			innerHeight: 800,
			vvHeight: 400,
		});
		expect(phone.classes).toContain("jv-linkmenu-phone");
		expect(phone.top).toBe("auto");
		expect(phone.bottom).not.toBe("auto");
		const desktop = await menuStyle({
			surface: "desktop",
			bottom: 290,
			innerHeight: 800,
			vvHeight: 400,
		});
		expect(desktop.classes).not.toContain("jv-linkmenu-phone");
		expect(desktop.bottom).toBe("auto");
		expect(desktop.top).toBe("294px");
	});
});

// ---- Preview ----------------------------------------------------------------------------------
const opensPreview = async (over) => {
	const t = harness(over);
	await t.host.openPreview(t.items.value[0]);
	return t;
};
// The text of every grid cell the Preview draws, by column name, row by row.
const previewGrid = (w) => {
	const heads = w.findAll(".dp-grid th").map((th) => th.text());
	return w
		.findAll(".dp-grid tbody tr")
		.map((tr) => Object.fromEntries(tr.findAll("td").map((td, i) => [heads[i], td.text()])));
};

describe("Preview of a draft card on the phone", () => {
	// Break: the Preview shows its own recomputation (Hrs blank or another number), dates as the
	// control's ISO text, or cells of another row, so it can disagree with the card it came from.
	it("shows the card's own numbers and dates, cell for cell", async () => {
		const t = await opensPreview();
		const w = await mountSheet(t);
		const card = timesheetCard().tables[0];
		const rows = previewGrid(w);
		expect(rows).toHaveLength(card.rows.length);
		card.rows.forEach((row, i) =>
			card.columns.forEach((name, j) =>
				expect(rows[i][name], `${name} of row ${i + 1}`).toBe(row.cells[j])
			)
		);
		expect(rows.map((r) => r.Hrs)).toEqual(["3", "1.50", "2"]);
		expect(rows[0]["From Time"]).toBe("08-12-2026 09:30:00");
		// the main fields too, in the card's date format
		expect(w.findAll(".dp-fields dd").map((d) => d.text())).toContain("08-12-2026");
		w.unmount();
	});

	// Break: the Preview is drawn as the desktop's resizable side panel (a drag handle, a width in
	// storage that can throw), or in the sheet with the phone's chrome missing.
	it("is the panel's content with no resize handle, no side-panel chrome and no storage", async () => {
		const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
			throw new Error("storage blocked");
		});
		const t = await opensPreview();
		const w = await mountSheet(t);
		expect(w.find(".dp-resizer").exists()).toBe(false);
		expect(w.find(".dp-overlay").classes()).toContain("dp-surface-phone");
		expect(w.find(".jv-sheet").classes()).toContain("is-full");
		expect(w.find(".jv-sheet .jv-draft-tokens .dp-panel").exists()).toBe(true);
		expect(w.find(".dp-panel").attributes("style")).toContain("width");
		expect(get).not.toHaveBeenCalled();
		expect(w.findAll(".dp-foot button").map((b) => b.text())).toEqual([
			"Create",
			"Edit",
			"Close",
		]);
		get.mockRestore();
		w.unmount();
	});

	// Break: the primary does not create, creates twice on a double tap, or leaves the sheet open
	// over the card's own spinner.
	it("Create is the card's Create: once on a double tap, and the sheet closes", async () => {
		const t = await opensPreview();
		const w = await mountSheet(t);
		const primary = w.find(".dp-foot .dp-btn-primary");
		await primary.trigger("click");
		await primary.trigger("click");
		await flushPromises();
		expect(t.calls.confirm).toEqual([["t1", "C1"]]);
		expect(t.host.open.value).toBe(false);
		expect(t.items.value).toEqual([]);
		w.unmount();
	});

	// Break: Edit leaves the Preview open over the form, builds the form twice, or loses the card.
	it("Edit closes the Preview and opens the edit sheet in the same sheet", async () => {
		const t = await opensPreview();
		const w = await mountSheet(t);
		await w.find(".dp-foot .dp-btn:not(.dp-btn-primary)").trigger("click");
		await flushPromises();
		expect(t.host.preview.value).toBeNull();
		expect(t.host.panel.value.token).toBe("t1");
		expect(w.find(".dp-panel").exists()).toBe(false);
		expect(w.find(".jv-dsheet-title").text()).toBe("New Timesheet");
		expect(t.calls.confirm).toEqual([]);
		w.unmount();
	});

	// Break: Create stays live while a turn runs (the server refuses it silently) or while the card
	// is busy with another press.
	it("Create is off while a turn runs or the card is busy", async () => {
		const t = await opensPreview({ streaming: true });
		const w = await mountSheet(t);
		expect(w.find(".dp-btn-primary").attributes("disabled")).toBeDefined();
		t.flags.streaming = false;
		await nextTick();
		expect(w.find(".dp-btn-primary").attributes("disabled")).toBeUndefined();
		t.ui.setKind("t1", "cancel");
		await nextTick();
		expect(w.find(".dp-btn-primary").attributes("disabled")).toBeDefined();
		w.unmount();
	});

	// Break: a Preview of a card that was cancelled or replaced elsewhere stays on screen with a
	// Create that cannot work (the desktop closes it: a preview holds no edits).
	it("closes when its card leaves the list", async () => {
		const t = await opensPreview();
		const w = await mountSheet(t);
		t.items.value = [];
		await flushPromises();
		expect(t.host.preview.value).toBeNull();
		expect(w.find(".jv-sheet").exists()).toBe(false);
		w.unmount();
	});

	// Break: the close control, Escape or back ask "Discard your changes?" for a Preview (it holds no
	// edits), or do not close it.
	it("closes with the close control, Escape and back, and never asks", async () => {
		const t = await opensPreview();
		const w = await mountSheet(t);
		await w.find(".dp-close").trigger("click");
		expect(t.host.open.value).toBe(false);
		await t.host.openPreview(t.items.value[0]);
		expect(t.host.requestClose()).toBe("closed");
		await t.host.openPreview(t.items.value[0]);
		expect(t.host.guardLeave()).toBe(false);
		expect(t.host.open.value).toBe(false);
		expect(t.host.confirming.value).toBe(false);
		w.unmount();
	});

	// Break: a Preview that cannot be built says nothing, or opens an empty sheet.
	it("a Preview that cannot be built says so under the card", async () => {
		const t = harness({ meta: { ok: false } });
		expect(await t.host.openPreview(t.items.value[0])).toBe("failed");
		expect(t.host.open.value).toBe(false);
		expect(t.ui.error("t1")).toBe("Could not load this draft. Try again.");
	});
});
