// The phone's chat view with a draft sheet open, run behind a REAL vue-router (memory history): the
// back button, the sheet's own history entry, and a navigation to another chat.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { ref } from "vue";
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
import { createMemoryHistory, createRouter, RouterView } from "vue-router";
import { defineComponent, h } from "vue";
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
const threadOf = (id) => ({
	conversation: { name: id, title: `Chat ${id}` },
	messages: [
		{ name: "m1", role: "user", content: "log my hours", seq: 1, created_at: 1 },
		{ name: "m2", role: "assistant", content: "Here is the draft.", seq: 2, created_at: 1 },
		{
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
		},
	],
});
const pendingList = (conversation) => ({
	ok: true,
	data: {
		pending: [
			{
				token: "t1",
				tool: "create_doc",
				summary: "",
				conversation,
				preview: { card: timesheetCard() },
				pending_card_safe: true,
				created_at: 1,
				recent: true,
			},
		],
	},
});

let wrapper;
let router;
const Home = defineComponent({ render: () => h("div", { class: "home" }, "home") });
const settle = async () => {
	await flushPromises();
	await new Promise((r) => setTimeout(r, 5));
	await flushPromises();
};
async function openChat(id = "A") {
	router = createRouter({
		history: createMemoryHistory(),
		routes: [
			{ path: "/", component: Home },
			{ path: "/c/:id", component: ChatView, props: true },
		],
	});
	router.push("/");
	await router.isReady();
	await router.push(`/c/${id}`);
	wrapper = mount(defineComponent({ render: () => h(RouterView) }), {
		attachTo: document.body,
		global: { plugins: [router], provide: { $socket: { on: vi.fn(), off: vi.fn() } } },
	});
	await settle();
}
const where = () => `${router.currentRoute.value.path}${router.currentRoute.value.hash}`;
const button = (label) => wrapper.findAll("button").find((b) => b.text() === label);
const openSheet = async () => {
	await button("Edit").trigger("click");
	await settle();
	expect(wrapper.find(".jv-dsheet-title").exists()).toBe(true);
};
const dirty = async () => {
	const field = wrapper
		.findAll(".jv-draft-fld")
		.find((f) => f.find("label").text().startsWith("Series"));
	await field.find("input").setValue("TS-CHANGED");
};
const asking = () => wrapper.find(".jv-dsheet-ask-text").exists();

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
	api.getConversation.mockImplementation(async (id) => threadOf(id));
	api.listPendingConfirmations.mockImplementation(async (id) => pendingList(id));
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
	document.body.innerHTML = "";
});

describe("the phone's Back with a draft sheet open, through a real router", () => {
	// Break: the guard blocks Back when no sheet is open (the person cannot leave the chat), or the
	// sheet's history entry is still there after it closed (one Back lands on the same chat again).
	it("no sheet: Back leaves the chat; a clean sheet closes with its own entry, and one Back then leaves", async () => {
		await openChat();
		router.back();
		await settle();
		expect(where()).toBe("/");
		wrapper.unmount();

		await openChat();
		await openSheet();
		// the sheet holds one entry, so the first Back always reaches the guard
		expect(where()).toBe("/c/A#editing");
		router.back();
		await settle();
		expect(where()).toBe("/c/A");
		expect(wrapper.find(".jv-dsheet-title").exists()).toBe(false);
		router.back();
		await settle();
		expect(where()).toBe("/");
	});

	// Break: the close control leaves its history entry behind (history grows by one per open sheet,
	// and the next Back only closes nothing), or the entry is popped twice (Back leaves the chat).
	it("closing the sheet by its own control pops the entry: one Back leaves the chat", async () => {
		await openChat();
		await openSheet();
		await wrapper.find(".jv-dsheet-close").trigger("click");
		await settle();
		expect(wrapper.find(".jv-dsheet-title").exists()).toBe(false);
		expect(where()).toBe("/c/A");
		router.back();
		await settle();
		expect(where()).toBe("/");
	});

	// Break: Back on a dirty sheet leaves the chat and loses the edits (the first history entry of a
	// chat opened from a notification), or asks only once, or "Discard changes" leaves the entry.
	it("a dirty sheet asks on every Back, keeps its edits on Keep editing, and discards on the answer", async () => {
		await openChat();
		await openSheet();
		await dirty();
		router.back();
		await settle();
		expect(asking()).toBe(true);
		expect(where()).toBe("/c/A#editing");
		await button("Keep editing").trigger("click");
		await settle();
		expect(asking()).toBe(false);
		router.back();
		await settle();
		expect(asking()).toBe(true);
		expect(where()).toBe("/c/A#editing");
		await button("Discard changes").trigger("click");
		await settle();
		expect(wrapper.find(".jv-dsheet-title").exists()).toBe(false);
		expect(where()).toBe("/c/A");
		router.back();
		await settle();
		expect(where()).toBe("/");
	});
});

describe("a navigation to another chat while the sheet is open (a notification tap)", () => {
	// Break: a tap on a notification for another chat is dropped (the guard refuses a clean sheet),
	// or it goes on with a dirty sheet and loses the edits without asking.
	it("a clean sheet closes and the navigation goes on", async () => {
		await openChat();
		await openSheet();
		await router.push("/c/B");
		await settle();
		expect(router.currentRoute.value.path).toBe("/c/B");
		expect(wrapper.find(".jv-dsheet-title").exists()).toBe(false);
		// the sheet's marker entry went away with the navigation: ONE Back reaches the chat it left
		// (with the marker left behind it would land on "/c/A#editing", and need a second Back)
		router.back();
		await settle();
		expect(where()).toBe("/c/A");
		router.back();
		await settle();
		expect(where()).toBe("/");
	});

	// Break: a dirty sheet is navigated away from without asking, the question never continues the
	// navigation ("Discard changes" drops the tap), or "Keep editing" still navigates.
	it("a dirty sheet asks; Keep editing stays; Discard changes continues to the other chat", async () => {
		await openChat();
		await openSheet();
		await dirty();
		await router.push("/c/B").catch(() => {});
		await settle();
		expect(asking()).toBe(true);
		expect(where()).toBe("/c/A#editing");
		await button("Keep editing").trigger("click");
		await settle();
		expect(where()).toBe("/c/A#editing");
		await router.push("/c/B").catch(() => {});
		await settle();
		await button("Discard changes").trigger("click");
		await settle();
		expect(router.currentRoute.value.path).toBe("/c/B");
		expect(wrapper.find(".jv-dsheet-title").exists()).toBe(false);
		// the same history as a clean sheet: one Back lands on the chat it left, with no marker
		router.back();
		await settle();
		expect(where()).toBe("/c/A");
	});

	// Break: a notification tap for the SAME chat or a route to the chat list leaves the marker behind
	// too (any navigation that leaves the sheet's chat while the sheet is open).
	it("leaving to the chat list replaces the marker: one Back returns to the chat", async () => {
		await openChat();
		await openSheet();
		await router.push("/");
		await settle();
		expect(where()).toBe("/");
		router.back();
		await settle();
		expect(where()).toBe("/c/A");
	});
});

describe("a page reloaded with the sheet's marker in its URL", () => {
	// Break: the dead marker stays (the URL keeps "#editing" and one Back lands on the same chat), or
	// cleaning it adds a history entry (the Back would then land on the chat again).
	it("is cleaned in place: no hash, and one Back leaves the chat", async () => {
		router = createRouter({
			history: createMemoryHistory(),
			routes: [
				{ path: "/", component: Home },
				{ path: "/c/:id", component: ChatView, props: true },
			],
		});
		await router.push("/");
		await router.push("/c/A#editing");
		wrapper = mount(defineComponent({ render: () => h(RouterView) }), {
			attachTo: document.body,
			global: { plugins: [router], provide: { $socket: { on: vi.fn(), off: vi.fn() } } },
		});
		await settle();
		expect(where()).toBe("/c/A");
		expect(wrapper.find(".jv-dsheet-title").exists()).toBe(false);
		router.back();
		await settle();
		expect(where()).toBe("/");
	});
});
