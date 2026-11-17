// The phone's chat view with an edit sheet open, run with the REAL pending-list merge: a card that
// was decided somewhere else is found by a list-only poll, then ONE quiet thread read, and the poll
// never writes the banner, scrolls or shows the loading state. (The sheet host's own wiring is in
// PwaDraftSheet.spec.js with fake I/O; the merge that makes a stale thread row keep a card on the
// list is the view's loadPending + pendingResync.js, so it needs the view.)
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
});
