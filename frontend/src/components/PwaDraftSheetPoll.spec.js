// The phone's chat view with a draft card, run with the REAL pending-list merge: with an edit sheet
// open, a card that was decided somewhere else is found by a list-only poll, then ONE quiet thread
// read, and the poll never writes the banner, scrolls or shows the loading state; and the view's own
// wiring of the card (the waiting card of a continuing draft, the error line that leaves with its
// card, the settling count). (The sheet host's own wiring is in PwaDraftSheet.spec.js with fake I/O;
// the merge that makes a stale thread row keep a card on the list is the view's loadPending +
// pendingResync.js, so it needs the view.)
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { ref } from "vue";
vi.mock("vue-router", () => ({
	useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
	onBeforeRouteLeave: vi.fn(),
	onBeforeRouteUpdate: vi.fn(),
}));
vi.mock("@/branding", () => ({ agentName: "Jarvis", brandLogoUrl: "" }));
vi.mock("../../../pwa/src/maintenanceGate", () => ({
	holdActive: ref(false),
	raiseHold: vi.fn(),
	clearHold: vi.fn(),
	recheck: vi.fn(),
}));
vi.mock("../../../pwa/src/api", () => ({
	getConversation: vi.fn(),
	activeQueuedTurn: vi.fn(),
	queuePosition: vi.fn(),
	cancelQueuedTurn: vi.fn(),
	stopRun: vi.fn(),
	getChatUiSettings: vi.fn(),
	listPendingConfirmations: vi.fn(),
	sendRecoverableMessage: vi.fn(),
	checkDelivery: vi.fn(),
	uploadFile: vi.fn(),
	listConversations: vi.fn(),
	confirmTool: vi.fn(),
	dismissTool: vi.fn(),
	getDoctypeFormMeta: vi.fn(),
	loadDocForEdit: vi.fn(),
	ruleValues: vi.fn(),
	saveDraftCard: vi.fn(),
	searchLink: vi.fn(),
	proposeNextAction: vi.fn(),
}));
vi.mock("../../../pwa/src/components/BrandMark.vue", () => ({ default: { template: "<div />" } }));
vi.mock("../../../pwa/src/components/ActionCard.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/ChartCard.vue", () => ({ default: { template: "<div />" } }));
vi.mock("../../../pwa/src/components/DecisionCard.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/DecisionSheet.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/FilePreviewSheet.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/MessageMedia.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/RecordCards.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/SkillChips.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/ThinkingIndicator.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/VersionPill.vue", () => ({
	default: { template: "<div />" },
}));
import ChatView from "../../../pwa/src/views/ChatView.vue";
import * as api from "../../../pwa/src/api";
import { recoveryState } from "../../../pwa/src/sendRecoveryStore";
import { store } from "../../../pwa/src/store";
import { TIMESHEET_DETAIL, timesheetCard } from "../lib/timesheetDraftFixture.js";

const F = (fieldname, label, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});
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

// The thread as the server sends it while the draft card is pending, and after it was decided
// somewhere else (the same row, now a receipt).
const pendingRow = () => ({
	name: "m3",
	role: "tool",
	seq: 3,
	created_at: 1,
	tool_call_id: "t1",
	tool_name: "create_doc",
	tool_status: "pending",
	pending_card: timesheetCard(),
	pending_card_safe: true,
	draft_card: true,
	recent: true,
});
const decidedRow = () => ({
	name: "m3",
	role: "tool",
	seq: 3,
	created_at: 1,
	tool_call_id: "t1",
	tool_name: "create_doc",
	tool_status: "",
	pending_card: null,
	action_outcome: "discarded",
	draft_receipt: true,
});
const thread = (row) => ({
	conversation: { name: "A", title: "Chat A" },
	messages: [
		{ name: "m1", role: "user", content: "log my hours", seq: 1, created_at: 1 },
		{ name: "m2", role: "assistant", content: "Here is the draft.", seq: 2, created_at: 1 },
		row,
	],
});
const listed = (tokens) => ({
	ok: true,
	data: {
		pending: tokens.map((token) => ({
			token,
			tool: "create_doc",
			summary: "",
			conversation: "A",
			preview: { card: timesheetCard() },
			pending_card_safe: true,
			created_at: 1,
			recent: true,
		})),
	},
});

let wrapper;
const open = async () => {
	wrapper = mount(ChatView, {
		props: { id: "A" },
		attachTo: document.body,
		global: { provide: { $socket: { on: vi.fn(), off: vi.fn() } } },
	});
	await flushPromises();
};
const button = (label) => wrapper.findAll("button").find((b) => b.text() === label);

beforeEach(() => {
	vi.resetAllMocks();
	for (const key of ["requests", "parkedDrafts"]) recoveryState[key] = [];
	for (const key of ["drafts", "queued", "starting", "observedRuns", "newChatPicks"])
		recoveryState[key] = {};
	recoveryState.heroDraft = null;
	recoveryState.editingNewRequest = null;
	store.loaded = true;
	store.conversations = [];
	api.activeQueuedTurn.mockResolvedValue({ ok: true, active: null });
	api.getChatUiSettings.mockResolvedValue({});
	api.listConversations.mockResolvedValue([]);
	api.getDoctypeFormMeta.mockResolvedValue(structuredClone(META));
	api.loadDocForEdit.mockResolvedValue({ values: {}, tables: {} });
	api.ruleValues.mockResolvedValue({ ok: false });
	api.getConversation.mockResolvedValue(thread(pendingRow()));
	api.listPendingConfirmations.mockResolvedValue(listed(["t1"]));
	document.elementFromPoint = () => null;
	globalThis.ResizeObserver = class {
		observe() {}
		disconnect() {}
	};
	HTMLDialogElement.prototype.showModal = function () {
		this.setAttribute("open", "");
	};
	HTMLDialogElement.prototype.close = function () {
		this.removeAttribute("open");
	};
});
afterEach(() => {
	wrapper?.unmount();
	vi.useRealTimers();
	document.body.innerHTML = "";
});

describe("the edit sheet's poll on the phone, with the real merge", () => {
	// Break: a poll that reads only the list never sees a card decided elsewhere (its thread row
	// still says pending, so the merge keeps the card); a poll that reads the whole thread on every
	// tick costs a transcript read every five seconds (and rewrites the banner while offline).
	it("a list-only tick keeps a card whose thread row is stale; a quiet thread read then drops it", async () => {
		vi.useFakeTimers();
		await open();
		await button("Edit").trigger("click");
		await flushPromises();
		expect(wrapper.find(".jv-dsheet-title").exists()).toBe(true);
		const threadReads = () => api.getConversation.mock.calls.length;
		const before = threadReads();
		// a loud read follows the thread to the bottom (it writes scrollTop); the poll's must not
		const scrolled = vi.fn();
		Object.defineProperty(HTMLElement.prototype, "scrollTop", {
			configurable: true,
			get: () => 0,
			set: scrolled,
		});

		// the card is still listed: the tick reads the list and nothing else
		await vi.advanceTimersByTimeAsync(5000);
		expect(threadReads()).toBe(before);
		expect(wrapper.text()).not.toContain("This draft changed while you were editing");

		// the server's list loses it (cancelled on another device); the thread row still says pending
		api.listPendingConfirmations.mockResolvedValue(listed([]));
		const quiet = thread(decidedRow());
		let release;
		api.getConversation.mockImplementation(
			() => new Promise((r) => (release = () => r(quiet)))
		);
		await vi.advanceTimersByTimeAsync(5000);
		// ONE thread read was asked for, and until it lands the stale row keeps the card (not dead yet)
		expect(threadReads()).toBe(before + 1);
		expect(wrapper.text()).not.toContain("This draft changed while you were editing");
		// the quiet read does not show the loading state, follow, or touch the banner
		expect(wrapper.find(".jv-banner").exists()).toBe(false);
		release();
		await flushPromises();
		expect(wrapper.text()).toContain("This draft changed while you were editing");
		expect(wrapper.find(".jv-banner").exists()).toBe(false);
		expect(scrolled).not.toHaveBeenCalled();
		delete HTMLElement.prototype.scrollTop;
		// the sheet stays (edits visible) and no further thread reads follow
		const after = threadReads();
		await vi.advanceTimersByTimeAsync(20000);
		expect(threadReads()).toBe(after);
	});

	// Break: a tick that cannot reach the server writes the chat's banner (it did, every five
	// seconds), reads the thread, or takes the card for dead.
	it("a tick while offline is silent: no thread read, no banner, the card stays", async () => {
		vi.useFakeTimers();
		await open();
		await button("Edit").trigger("click");
		await flushPromises();
		const before = api.getConversation.mock.calls.length;
		api.listPendingConfirmations.mockRejectedValue(new TypeError("Failed to fetch"));
		api.getConversation.mockRejectedValue(new TypeError("Failed to fetch"));
		await vi.advanceTimersByTimeAsync(20000);
		expect(api.getConversation.mock.calls.length).toBe(before);
		expect(wrapper.find(".jv-banner").exists()).toBe(false);
		expect(wrapper.text()).not.toContain("This draft changed while you were editing");
	});

	// Break: the quiet read writes the banner when it fails (a card gone from the list while the
	// phone has no thread), or marks the card dead on the list's word alone.
	it("a quiet read that fails is silent and the card is not dead", async () => {
		vi.useFakeTimers();
		await open();
		await button("Edit").trigger("click");
		await flushPromises();
		api.listPendingConfirmations.mockResolvedValue(listed([]));
		api.getConversation.mockRejectedValue(new TypeError("Failed to fetch"));
		await vi.advanceTimersByTimeAsync(5000);
		await flushPromises();
		expect(wrapper.find(".jv-banner").exists()).toBe(false);
		expect(wrapper.text()).not.toContain("This draft changed while you were editing");
	});

	// Break: a card that is missing from the list but whose quiet thread read did not drop it (the row
	// still says pending) is read again every tick for ever; or the latch never lets the NEXT episode
	// (the card listed again, then missing again) read.
	it("reads the thread once per episode of the card missing from the list, and again after it was listed", async () => {
		vi.useFakeTimers();
		await open();
		await button("Edit").trigger("click");
		await flushPromises();
		const reads = () => api.getConversation.mock.calls.length;
		const before = reads();
		// missing from the list, and the thread still says pending: the quiet read cannot drop it
		api.listPendingConfirmations.mockResolvedValue(listed([]));
		await vi.advanceTimersByTimeAsync(5000);
		expect(reads()).toBe(before + 1);
		await vi.advanceTimersByTimeAsync(20000);
		expect(reads()).toBe(before + 1);
		// listed again: a new episode when it goes missing next
		api.listPendingConfirmations.mockResolvedValue(listed(["t1"]));
		await vi.advanceTimersByTimeAsync(5000);
		api.listPendingConfirmations.mockResolvedValue(listed([]));
		await vi.advanceTimersByTimeAsync(5000);
		expect(reads()).toBe(before + 2);
	});

	// Break: a quiet read that FAILS counts as the episode's one read (the card is then never found
	// dead while the phone is offline and comes back).
	it("a quiet read that failed is asked for again on the next tick", async () => {
		vi.useFakeTimers();
		await open();
		await button("Edit").trigger("click");
		await flushPromises();
		api.listPendingConfirmations.mockResolvedValue(listed([]));
		const before = api.getConversation.mock.calls.length;
		api.getConversation.mockRejectedValue(new TypeError("Failed to fetch"));
		await vi.advanceTimersByTimeAsync(10000);
		expect(api.getConversation.mock.calls.length).toBe(before + 2);
		api.getConversation.mockResolvedValue(thread(decidedRow()));
		await vi.advanceTimersByTimeAsync(5000);
		await flushPromises();
		expect(wrapper.text()).toContain("This draft changed while you were editing");
	});

	// Break: a quiet read that was already in flight when a loud load started writes its (older) page
	// over the loud one: the thread jumps back, or the card is taken for dead on stale data.
	it("a quiet read overtaken by a loud load does not write its page", async () => {
		vi.useFakeTimers();
		await open();
		await button("Edit").trigger("click");
		await flushPromises();
		api.listPendingConfirmations.mockResolvedValue(listed([]));
		let releaseQuiet;
		api.getConversation.mockImplementationOnce(
			() => new Promise((r) => (releaseQuiet = () => r(thread(decidedRow()))))
		);
		await vi.advanceTimersByTimeAsync(5000);
		expect(releaseQuiet).toBeTypeOf("function");
		// a loud load starts (the app's resync event) and reads the still-pending thread
		api.getConversation.mockResolvedValue(thread(pendingRow()));
		window.dispatchEvent(new Event("jv:resync"));
		await flushPromises();
		releaseQuiet();
		await flushPromises();
		// the older (decided) page did not land: the card is not dead
		expect(wrapper.text()).not.toContain("This draft changed while you were editing");
	});

	// Break: a quiet read that lands after the person switched chats writes the old chat's page onto
	// the new one.
	it("a quiet read that lands after a chat switch writes nothing", async () => {
		vi.useFakeTimers();
		await open();
		await button("Edit").trigger("click");
		await flushPromises();
		api.listPendingConfirmations.mockImplementation(async (id) =>
			id === "A" ? listed([]) : { ok: true, data: { pending: [] } }
		);
		let releaseQuiet;
		api.getConversation.mockImplementationOnce(
			() => new Promise((r) => (releaseQuiet = () => r(thread(decidedRow()))))
		);
		await vi.advanceTimersByTimeAsync(5000);
		expect(releaseQuiet).toBeTypeOf("function");
		api.getConversation.mockResolvedValue({
			conversation: { name: "B", title: "Chat B" },
			messages: [
				{
					name: "b1",
					role: "assistant",
					content: "Only in chat B",
					seq: 1,
					created_at: 1,
				},
			],
		});
		await wrapper.setProps({ id: "B" });
		await flushPromises();
		expect(wrapper.text()).toContain("Only in chat B");
		releaseQuiet();
		await flushPromises();
		expect(wrapper.text()).toContain("Only in chat B");
		expect(wrapper.text()).not.toContain("Here is the draft.");
	});
});

describe("the phone view's wiring of the draft card's decisions", () => {
	const create = () => button("Create").trigger("click");

	// Break: a draft that continues the chat gets no waiting card after Create (the person sees the
	// card vanish and nothing says Jarvis will answer), or a run the view already saw starting is
	// queued again, or a confirm that queued nothing makes one.
	it("a confirmed draft that continues shows the waiting card; one that does not, shows none", async () => {
		await open();
		api.confirmTool.mockResolvedValue({
			ok: true,
			queued: true,
			run_id: "r9",
			queued_position: 2,
		});
		await create();
		await flushPromises();
		expect(recoveryState.queued.A).toEqual({ run_id: "r9", state: "queued", position: 2 });
		expect(wrapper.find(".jv-waiting-card").text()).toContain("Message queued");

		// a run the view has already seen start is not shown as waiting
		wrapper.unmount();
		recoveryState.queued = {};
		recoveryState.observedRuns = { r10: true };
		await open();
		api.confirmTool.mockResolvedValue({ ok: true, queued: true, run_id: "r10" });
		await create();
		await flushPromises();
		expect(recoveryState.queued.A).toBeUndefined();

		// a confirm that queued nothing (a final draft) shows no waiting card
		wrapper.unmount();
		await open();
		api.confirmTool.mockResolvedValue({ ok: true });
		await create();
		await flushPromises();
		expect(recoveryState.queued.A).toBeUndefined();
	});

	// Break: a decision that throws leaves the settling count up, so the typed-draft card stays hidden
	// for the rest of the chat (the one-card rule reads it); or the count is not raised while the call runs.
	it("the settling count is up while a decision runs and back to 0 after a thrown one", async () => {
		await open();
		let fail;
		api.confirmTool.mockImplementation(() => new Promise((_, reject) => (fail = reject)));
		const pressed = create();
		await flushPromises();
		expect(wrapper.vm.decisionsSettling).toBe(1);
		fail(new Error("Failed to fetch"));
		await pressed;
		await flushPromises();
		expect(wrapper.vm.decisionsSettling).toBe(0);
		// the card is still there, with the sanitised line
		expect(wrapper.text()).toContain("Jarvis could not reach the server");
	});

	// Break: an error line stays in the store when its card leaves the list and comes back with the
	// same token (a refused Create's words reappear under a fresh card for the same draft).
	it("a card's error line leaves with the card", async () => {
		await open();
		api.confirmTool.mockResolvedValue({ ok: false, reason_code: "busy" });
		await create();
		await flushPromises();
		const busy = "This action is being handled right now.";
		expect(wrapper.text()).toContain(busy);
		// the card is decided elsewhere: gone from the list and the thread
		api.listPendingConfirmations.mockResolvedValue(listed([]));
		api.getConversation.mockResolvedValue(thread(decidedRow()));
		// (the resync reads the list from the rows it held when it started: it takes two to see the thread)
		window.dispatchEvent(new Event("jv:resync"));
		await flushPromises();
		window.dispatchEvent(new Event("jv:resync"));
		await flushPromises();
		expect(button("Create")).toBeUndefined();
		// the same token is pending again
		api.listPendingConfirmations.mockResolvedValue(listed(["t1"]));
		api.getConversation.mockResolvedValue(thread(pendingRow()));
		window.dispatchEvent(new Event("jv:resync"));
		await flushPromises();
		window.dispatchEvent(new Event("jv:resync"));
		await flushPromises();
		expect(button("Create")).toBeDefined();
		expect(wrapper.text()).not.toContain(busy);
	});
});
