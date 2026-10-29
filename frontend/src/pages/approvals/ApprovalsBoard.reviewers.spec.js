import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// admin-v2 #756: a Chat question is visible to its requester and to the reviewers the
// requester explicitly tagged, nobody else. The backend marks such rows `review_scoped`
// (source Chat AND a parent that is not a File Box). These tests pin the board's side:
// who sees which controls, that the chat is never reachable by a reviewer, that tagging
// goes through the dedicated endpoints, that realtime revokes drop the row quietly, and
// that every other source (incl. the Chat questions a real File Box asks inline) keeps
// the original behaviour.
const probe = vi.hoisted(() => ({ docmeta: null }));

vi.mock("vue-router", async () => {
	const { reactive } = await import("vue");
	const route = reactive({ name: "ApprovalsList", params: {}, query: {} });
	const router = {
		replace: vi.fn((to) => {
			if (to.name) {
				route.name = to.name;
				route.params = to.params || {};
			}
			route.query = { ...(to.query || {}) };
		}),
		push: vi.fn(),
	};
	return { useRoute: () => route, useRouter: () => router };
});
vi.mock("@vueuse/core", async (importOriginal) => {
	const actual = await importOriginal();
	const { ref } = await import("vue");
	return { ...actual, useStorage: (_key, initial) => ref(initial) };
});
vi.mock("frappe-ui", () => ({
	Autocomplete: {
		props: ["options", "placeholder"],
		emits: ["update:modelValue"],
		template:
			'<div class="picker"><button v-for="o in options" :key="o.value" class="pick" @click="$emit(\'update:modelValue\', o)">{{ o.label }}</button></div>',
	},
	Badge: { props: ["label", "theme"], template: '<span class="badge">{{ label }}</span>' },
	Breadcrumbs: { props: ["items"], template: "<nav/>" },
	// mirrors frappe-ui: `label` is bound after $attrs, so it is the only accessible
	// name an icon button can get, and an icon button shows no label text
	Button: {
		inheritAttrs: false,
		props: ["label", "icon", "disabled", "loading", "tooltip"],
		emits: ["click"],
		template:
			'<button v-bind="$attrs" :disabled="disabled" :data-tip="tooltip" :aria-label="label" @click="$emit(\'click\')">{{ icon ? "" : label }}</button>',
	},
	FeatherIcon: { template: "<i/>" },
	FormControl: {
		props: ["modelValue", "type", "options", "placeholder"],
		emits: ["update:modelValue"],
		template: `<select v-if="type === 'select'" :value="modelValue" @change="$emit('update:modelValue', $event.target.value)"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select><textarea v-else-if="type === 'textarea'" :placeholder="placeholder" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" /><input v-else :placeholder="placeholder" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	Popover: {
		template:
			'<div><slot name="target" :togglePopover="() => {}" /><div class="popover-body"><slot name="body" /></div></div>',
	},
	Tooltip: { template: "<span><slot/></span>" },
	toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
	call: vi.fn(),
}));
vi.mock("@/api", () => ({
	listApprovalsPage: vi.fn(),
	listWikiWriteProposals: vi.fn(),
	decideApproval: vi.fn(),
	continueApprovalChat: vi.fn(),
	dismissApproval: vi.fn(),
	restoreApproval: vi.fn(),
	tagApprovalReviewer: vi.fn(),
	untagApprovalReviewer: vi.fn(),
	listShareableUsers: vi.fn(async () => []),
	listApprovalReviewers: vi.fn(),
}));
vi.mock("@/api/approvals", () => ({ getApproval: vi.fn(), listPendingActionsLane: vi.fn() }));
const shell = vi.hoisted(() => ({ store: null, refresh: vi.fn() }));
vi.mock("@/stores/shell", async () => {
	const { reactive } = await import("vue");
	shell.store = reactive({ approvalsCount: 0, refreshApprovalsCount: shell.refresh });
	return { useShellStore: () => shell.store };
});
vi.mock("@/data/session", () => ({ session: { user: "me@example.com" } }));
vi.mock("@/utils/datetime", () => ({ timeAgo: () => "5 minutes ago", exactDate: () => "" }));
vi.mock("@/composables/useDocmeta", () => ({
	useDocmeta: () => probe.docmeta,
}));
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: { template: '<header><slot name="left-header" /></header>' },
}));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { template: '<span class="spinner"/>' },
}));
vi.mock("@/components/doc/DocSection.vue", () => ({
	default: { props: ["label"], template: "<section><slot/></section>" },
}));
vi.mock("@/components/doc/DocMetaPanel.vue", () => ({
	default: {
		props: ["docmeta", "canWrite", "allowShares", "allowAssignees"],
		template:
			'<div class="panel" :data-can-write="String(canWrite)" :data-allow-shares="String(allowShares)" :data-allow-assignees="String(allowAssignees)" />',
	},
}));
vi.mock("@/components/doc/CommentsSection.vue", () => ({
	default: {
		props: ["docmeta", "canComment"],
		template: '<div class="comments" :data-can-comment="String(canComment)" />',
	},
}));
vi.mock("./PendingActionDetail.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("./PendingChatDetail.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("./SheetDetail.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("./WikiProposalDetail.vue", () => ({ default: { template: "<div/>" } }));

import { useRoute, useRouter } from "vue-router";
import { toast } from "frappe-ui";
import * as api from "@/api";
import * as approvals from "@/api/approvals";
import ApprovalsBoard from "./ApprovalsBoard.vue";

const route = useRoute();
const router = useRouter();

const ME = "me@example.com";
const ROSTER = [
	{ name: "ja@example.com", full_name: "Jai Admin" },
	{ name: "sm@example.com", full_name: "Sam Manager" },
];

const ar = (name, over = {}) => ({
	name,
	title: "Question " + name,
	status: "Pending",
	source: "Chat",
	document_type: "Sales Order",
	creation: "2026-09-01 12:00:00",
	...over,
});
// get_approval for a review-scoped Chat question, as its requester sees it
const requesterView = (over = {}) => ({
	...ar("AR-1"),
	review_scoped: 1,
	question: "Which warehouse?",
	conversation: "conv-1",
	origin_page: "",
	options: [],
	can_act: 1,
	can_manage: 1,
	is_requester: 1,
	requester: ME,
	requester_name: "Maya Requester",
	reviewers: [{ user: "ja@example.com", full_name: "Jai Admin" }],
	...over,
});
// ... and as a tagged reviewer sees it: no conversation, no origin, no manage
const reviewerView = (over = {}) =>
	requesterView({
		conversation: "",
		origin_page: "",
		can_manage: 0,
		is_requester: 0,
		requester: "maya@example.com",
		...over,
	});
// a real File Box question (source File Box) and the Chat question a File Box asks inline
const fileBoxView = (over = {}) => ({
	...ar("AR-1", { source: "File Box" }),
	review_scoped: 0,
	question: "Pick the supplier",
	conversation: "fb-conv-1",
	origin_page: "",
	options: ["Acme", "Beta", "Gamma"],
	can_act: 1,
	can_manage: 1,
	is_requester: 1,
	requester: ME,
	requester_name: "",
	reviewers: [],
	...over,
});

let state;
let socket;
function fakeSocket() {
	const handlers = new Set();
	return {
		on: (event, fn) => event === "jarvis:event" && handlers.add(fn),
		off: (event, fn) => event === "jarvis:event" && handlers.delete(fn),
		emit: (payload) => [...handlers].forEach((fn) => fn(payload)),
		count: () => handlers.size,
	};
}
function fakeDocmeta(shares = []) {
	return {
		meta: { shares, created: { owner: ME } },
		reload: vi.fn(),
		toggleShare: vi.fn(async () => true),
	};
}
const denied = () =>
	Object.assign(new Error("Not permitted"), { status: 403, exc_type: "PermissionError" });

async function mountBoard(id = "AR-1") {
	route.name = "ApprovalDetail";
	route.params = { id };
	route.query = {};
	const w = mount(ApprovalsBoard, {
		attachTo: document.body,
		global: {
			stubs: {
				"router-link": {
					props: ["to"],
					template: '<a class="conv-link" :data-to="JSON.stringify(to)"><slot/></a>',
				},
			},
			provide: { $socket: socket },
		},
	});
	await flushPromises();
	await flushPromises();
	return w;
}

const pane = (w) => w.find(".flex-1.overflow-y-auto");
const buttons = (w) => w.findAll("button").map((b) => b.text());
const button = (w, label) => w.findAll("button").find((b) => b.text() === label);
const rail = (w) => w.find(".w-\\[360px\\]");
const panel = (w) => w.find(".panel");

describe("ApprovalsBoard reviewer scoping (#756)", () => {
	let mounted = [];
	beforeEach(() => {
		vi.clearAllMocks();
		state = { rows: [ar("AR-1", { review_scoped: 1 })], view: requesterView() };
		socket = fakeSocket();
		probe.docmeta = fakeDocmeta();
		route.query = {};
		api.listApprovalsPage.mockImplementation(async () => ({
			rows: state.rows,
			total: state.rows.length,
			has_more: false,
			facets: {},
			awaiting_reply: [],
			open_drafts: [],
			ready_reports: [],
		}));
		approvals.listPendingActionsLane.mockResolvedValue({ rows: [] });
		api.listWikiWriteProposals.mockResolvedValue({ rows: [], total: 0 });
		api.listApprovalReviewers.mockResolvedValue(ROSTER);
		approvals.getApproval.mockImplementation(async () => state.view);
		api.decideApproval.mockResolvedValue({ ok: true });
		api.dismissApproval.mockResolvedValue({ ok: true });
		api.restoreApproval.mockResolvedValue({ ok: true });
	});
	afterEach(() => {
		mounted.forEach((w) => w.unmount());
		mounted = [];
	});
	// one board at a time: a test that views the same question as another user remounts
	const board = async (...args) => {
		mounted.forEach((w) => w.unmount());
		mounted = [];
		const w = await mountBoard(...args);
		mounted.push(w);
		return w;
	};

	describe("review-scoped Chat question, as its requester", () => {
		it("has no header Open Chat, but keeps the conversation chip and names the people", async () => {
			const w = await board();
			expect(buttons(w)).not.toContain("Open Chat");
			expect(buttons(w)).not.toContain("Open Builder");
			const chip = w.find(".conv-link");
			expect(chip.exists()).toBe(true);
			expect(JSON.parse(chip.attributes("data-to"))).toBe("/c/conv-1");
			expect(pane(w).text()).toContain("Requested by");
			expect(pane(w).text()).toContain("Maya Requester");
			expect(pane(w).text()).toContain("Reviewers");
			expect(pane(w).text()).toContain("Jai Admin");
		});

		it("answers an option-less question in the chat, with no verdict buttons", async () => {
			const w = await board();
			expect(buttons(w)).toContain("Answer in chat");
			expect(buttons(w)).not.toContain("Approve");
			expect(buttons(w)).not.toContain("Reject");
			await button(w, "Answer in chat").trigger("click");
			expect(router.push).toHaveBeenCalledWith("/c/conv-1");
		});

		it("answers a dashboard question in the builder", async () => {
			state.view = requesterView({ origin_page: "dashboards" });
			const w = await board();
			await button(w, "Answer in builder").trigger("click");
			expect(router.push).toHaveBeenCalledWith({
				name: "DashboardsPage",
				query: { conversation: "conv-1" },
			});
			expect(buttons(w)).not.toContain("Open Builder");
		});

		it("approves an options question with the picked chip", async () => {
			state.view = requesterView({ options: ["Main", "Spare", "Transit"] });
			const w = await board();
			expect(buttons(w)).toEqual(expect.arrayContaining(["Main", "Spare", "Transit"]));
			expect(buttons(w)).not.toContain("Answer in chat");
			await button(w, "Spare").trigger("click");
			await button(w, "Approve").trigger("click");
			await flushPromises();
			expect(api.decideApproval).toHaveBeenCalledWith("AR-1", "Spare", 1);
		});

		it("keeps Ignore, and Restore on a dismissed question", async () => {
			const w = await board();
			await button(w, "Ignore").trigger("click");
			await flushPromises();
			expect(api.dismissApproval).toHaveBeenCalledWith("AR-1");

			state.view = requesterView({ status: "Dismissed", decision: "(dismissed)" });
			state.rows = [ar("AR-1", { review_scoped: 1, status: "Dismissed" })];
			const w2 = await board();
			await button(w2, "Restore to board").trigger("click");
			await flushPromises();
			expect(api.restoreApproval).toHaveBeenCalledWith("AR-1");
		});

		it("tags a reviewer through the dedicated endpoint, never the generic share", async () => {
			api.tagApprovalReviewer.mockResolvedValue([
				{ user: "ja@example.com", full_name: "Jai Admin" },
				{ user: "sm@example.com", full_name: "Sam Manager" },
			]);
			const w = await board();
			await button(w, "Tag for review").trigger("click");
			await flushPromises();
			expect(api.listApprovalReviewers).toHaveBeenCalledOnce();
			// the one already tagged is not offered again
			const offered = w.findAll(".pick").map((b) => b.text());
			expect(offered).toEqual(["Sam Manager"]);
			await w.find(".pick").trigger("click");
			await flushPromises();
			expect(api.tagApprovalReviewer).toHaveBeenCalledWith("AR-1", "sm@example.com");
			expect(probe.docmeta.toggleShare).not.toHaveBeenCalled();
			expect(pane(w).text()).toContain("Sam Manager");
			expect(toast.success).toHaveBeenCalled();
		});

		it("untags through the dedicated endpoint and shows the returned list", async () => {
			api.untagApprovalReviewer.mockResolvedValue([]);
			const w = await board();
			await w.find('button[aria-label="Remove Jai Admin"]').trigger("click");
			await flushPromises();
			expect(api.untagApprovalReviewer).toHaveBeenCalledWith("AR-1", "ja@example.com");
			expect(probe.docmeta.toggleShare).not.toHaveBeenCalled();
			expect(pane(w).text()).not.toContain("Jai Admin");
		});

		it("names each remove control and gives it a 24px target", async () => {
			const w = await board();
			const remove = w.find('button[aria-label="Remove Jai Admin"]');
			expect(remove.exists()).toBe(true);
			expect(remove.classes()).toEqual(expect.arrayContaining(["!h-6", "!w-6"]));
			expect(remove.attributes("data-tip")).toContain("access");
		});

		it("can still take a reviewer's access away after the question is settled", async () => {
			state.view = requesterView({
				status: "Approved",
				decision: "Main",
				decided_by: "ja@example.com",
				decided_by_name: "Jai Admin",
				can_act: 0,
			});
			api.untagApprovalReviewer.mockResolvedValue([]);
			const w = await board();
			await w.find('button[aria-label="Remove Jai Admin"]').trigger("click");
			await flushPromises();
			expect(api.untagApprovalReviewer).toHaveBeenCalledWith("AR-1", "ja@example.com");
			expect(pane(w).text()).toContain("No reviewer tagged");
		});

		it("says so and keeps the reviewers when tagging is refused", async () => {
			api.tagApprovalReviewer.mockRejectedValue(denied());
			const w = await board();
			await button(w, "Tag for review").trigger("click");
			await flushPromises();
			await w.find(".pick").trigger("click");
			await flushPromises();
			expect(toast.error).toHaveBeenCalled();
			// the chips are the reviewers; the picker's own options are not
			const chips = w
				.findAll('button[aria-label^="Remove"]')
				.map((b) => b.attributes("aria-label"));
			expect(chips).toEqual(["Remove Jai Admin"]);
		});

		it("offers no generic sharing or assignment, but still lets the owner attach", async () => {
			const w = await board();
			expect(panel(w).attributes("data-can-write")).toBe("true");
			expect(panel(w).attributes("data-allow-shares")).toBe("false");
			expect(panel(w).attributes("data-allow-assignees")).toBe("false");
			expect(w.find(".comments").attributes("data-can-comment")).toBe("true");
		});
	});

	describe("review-scoped Chat question, as a tagged reviewer", () => {
		beforeEach(() => {
			state.view = reviewerView({ options: ["Main", "Spare", "Transit"] });
			state.rows = [
				ar("AR-1", {
					review_scoped: 1,
					shared: 1,
					conversation: "",
					requester: "maya@example.com",
					requester_name: "Maya Requester",
					is_requester: 0,
				}),
			];
		});

		it("approves with the picked option and rejects with the note", async () => {
			const w = await board();
			await button(w, "Spare").trigger("click");
			await button(w, "Approve").trigger("click");
			await flushPromises();
			expect(api.decideApproval).toHaveBeenCalledWith("AR-1", "Spare", 1);

			state.rows = [ar("AR-1", { review_scoped: 1 })];
			const w2 = await board();
			await w2.find("textarea").setValue("Wrong site");
			await button(w2, "Reject").trigger("click");
			await flushPromises();
			expect(api.decideApproval).toHaveBeenLastCalledWith("AR-1", "Wrong site", 0);
		});

		it("answers an option-less question in a text box: Approve needs text", async () => {
			state.view = reviewerView({ options: [] });
			const w = await board();
			const box = w.find("textarea");
			expect(box.attributes("placeholder")).toBe("Your answer");
			expect(buttons(w)).not.toContain("Answer in chat");
			expect(button(w, "Approve").attributes("disabled")).toBeDefined();
			await box.setValue("   ");
			expect(button(w, "Approve").attributes("disabled")).toBeDefined();
			await button(w, "Approve").trigger("click");
			expect(api.decideApproval).not.toHaveBeenCalled();
			await box.setValue("  Use the main warehouse ");
			expect(button(w, "Approve").attributes("disabled")).toBeUndefined();
			await button(w, "Approve").trigger("click");
			await flushPromises();
			expect(api.decideApproval).toHaveBeenCalledWith("AR-1", "Use the main warehouse", 1);
		});

		it("rejects an option-less question with or without text", async () => {
			state.view = reviewerView({ options: [] });
			const w = await board();
			expect(button(w, "Reject").attributes("disabled")).toBeUndefined();
			await button(w, "Reject").trigger("click");
			await flushPromises();
			expect(api.decideApproval).toHaveBeenCalledWith("AR-1", "Rejected", 0);

			const w2 = await board();
			await w2.find("textarea").setValue("Not this one");
			await button(w2, "Reject").trigger("click");
			await flushPromises();
			expect(api.decideApproval).toHaveBeenLastCalledWith("AR-1", "Not this one", 0);
		});

		it("reaches no chat, and manages nothing", async () => {
			const w = await board();
			expect(buttons(w)).not.toContain("Open Chat");
			expect(buttons(w)).not.toContain("Open Builder");
			expect(buttons(w)).not.toContain("Answer in chat");
			expect(buttons(w)).not.toContain("Ignore");
			expect(buttons(w)).not.toContain("Tag for review");
			expect(w.find(".conv-link").exists()).toBe(false);
			expect(w.find('button[aria-label^="Remove"]').exists()).toBe(false);
			expect(pane(w).text()).toContain("Maya Requester");
			expect(pane(w).text()).toContain("Jai Admin");
			// no tag roster is even fetched for someone who cannot tag
			expect(api.listApprovalReviewers).not.toHaveBeenCalled();
		});

		it("cannot restore a dismissed question", async () => {
			state.view = reviewerView({ status: "Dismissed", decision: "(dismissed)" });
			const w = await board();
			expect(buttons(w)).not.toContain("Restore to board");
			expect(pane(w).text()).toContain("(dismissed)");
		});

		it("is read-only in the record details, with comments open", async () => {
			const w = await board();
			expect(panel(w).attributes("data-can-write")).toBe("false");
			expect(panel(w).attributes("data-allow-shares")).toBe("false");
			expect(panel(w).attributes("data-allow-assignees")).toBe("false");
			expect(w.find(".comments").attributes("data-can-comment")).toBe("true");
		});

		it("names the requester on the rail row and shows it as shared", async () => {
			const w = await board();
			expect(rail(w).text()).toContain("Maya Requester");
			expect(rail(w).text()).toContain("Shared");
		});

		it("after losing a decision race, the open question's rail row shows what is true now", async () => {
			const w = await board();
			const reads = approvals.getApproval.mock.calls.length;
			router.replace.mockClear();
			// the requester rejected it first: the stale Approve is refused, and the
			// Pending list no longer holds the row, so the open question stays pinned
			api.decideApproval.mockRejectedValue(
				Object.assign(new Error("This question is already Rejected"), {
					status: 417,
					exc_type: "ValidationError",
				})
			);
			state.view = reviewerView({
				options: ["Main", "Spare", "Transit"],
				status: "Rejected",
				decision: "No",
				decided_by: "maya@example.com",
				decided_by_name: "Maya Requester",
				can_act: 0,
			});
			state.rows = [];
			await button(w, "Main").trigger("click");
			await button(w, "Approve").trigger("click");
			await flushPromises();
			await flushPromises();
			expect(api.decideApproval).toHaveBeenCalledTimes(1);
			expect(toast.error).toHaveBeenCalledTimes(1);
			expect(approvals.getApproval.mock.calls.length).toBe(reads + 1);
			expect(router.replace).not.toHaveBeenCalled();
			const badges = rail(w)
				.findAll(".badge")
				.map((b) => b.text());
			expect(badges).toContain("Rejected");
			expect(badges).not.toContain("Pending");
			expect(badges).toContain("Shared");
			expect(rail(w).text()).toContain("From Maya Requester");
			expect(pane(w).text()).toContain("Rejected");
		});
	});

	describe("everything that is not review-scoped keeps the original behaviour", () => {
		it("a real File Box question: Open Chat, chip, verdict, Ignore and the share-based tag", async () => {
			state.view = fileBoxView();
			state.rows = [ar("AR-1", { source: "File Box" })];
			const w = await board();
			expect(buttons(w)).toContain("Open Chat");
			await button(w, "Open Chat").trigger("click");
			expect(router.push).toHaveBeenCalledWith("/c/fb-conv-1");
			expect(JSON.parse(w.find(".conv-link").attributes("data-to"))).toBe("/c/fb-conv-1");
			expect(pane(w).text()).not.toContain("Requested by");
			expect(buttons(w)).toEqual(expect.arrayContaining(["Acme", "Beta", "Gamma"]));
			await button(w, "Acme").trigger("click");
			await button(w, "Approve").trigger("click");
			await flushPromises();
			expect(api.decideApproval).toHaveBeenCalledWith("AR-1", "Acme", 1);

			const w2 = await board();
			await button(w2, "Tag for review").trigger("click");
			await flushPromises();
			await w2.find(".pick").trigger("click");
			await flushPromises();
			expect(probe.docmeta.toggleShare).toHaveBeenCalledWith("ja@example.com", "add");
			expect(api.tagApprovalReviewer).not.toHaveBeenCalled();
			expect(panel(w2).attributes("data-can-write")).toBe("true");
			expect(panel(w2).attributes("data-allow-shares")).not.toBe("false");
			expect(panel(w2).attributes("data-allow-assignees")).not.toBe("false");
		});

		it("a real File Box question: Reject, Ignore and Restore still work", async () => {
			state.view = fileBoxView();
			state.rows = [ar("AR-1", { source: "File Box" })];
			const w = await board();
			await button(w, "Reject").trigger("click");
			await flushPromises();
			expect(api.decideApproval).toHaveBeenCalledWith("AR-1", "Rejected", 0);
			const w2 = await board();
			await button(w2, "Ignore").trigger("click");
			await flushPromises();
			expect(api.dismissApproval).toHaveBeenCalledWith("AR-1");
			state.view = fileBoxView({ status: "Dismissed", decision: "(dismissed)" });
			const w3 = await board();
			await button(w3, "Restore to board").trigger("click");
			await flushPromises();
			expect(api.restoreApproval).toHaveBeenCalledWith("AR-1");
		});

		it("a Chat question asked inside a real File Box: Answer in chat, Open Chat, share-based tag", async () => {
			state.view = fileBoxView({ source: "Chat", options: [] });
			state.rows = [ar("AR-1", { source: "Chat" })];
			const w = await board();
			expect(buttons(w)).toContain("Open Chat");
			expect(buttons(w)).toContain("Answer in chat");
			expect(buttons(w)).not.toContain("Approve");
			expect(pane(w).text()).not.toContain("Requested by");
			expect(w.find("textarea").exists()).toBe(false);
			await button(w, "Answer in chat").trigger("click");
			expect(router.push).toHaveBeenCalledWith("/c/fb-conv-1");
			await button(w, "Tag for review").trigger("click");
			await flushPromises();
			await w.find(".pick").trigger("click");
			await flushPromises();
			expect(probe.docmeta.toggleShare).toHaveBeenCalledWith("ja@example.com", "add");
			expect(api.tagApprovalReviewer).not.toHaveBeenCalled();
			expect(panel(w).attributes("data-allow-shares")).not.toBe("false");
		});

		it("an Agent Review keeps its acknowledgement wording", async () => {
			state.view = fileBoxView({ source: "Agent Review", options: [] });
			state.rows = [ar("AR-1", { source: "Agent Review" })];
			const w = await board();
			expect(buttons(w)).toContain("Acknowledge review");
			await button(w, "Acknowledge review").trigger("click");
			await flushPromises();
			expect(api.decideApproval).toHaveBeenCalledWith(
				"AR-1",
				"Review acknowledged: no posting authorised",
				1
			);
		});

		it("a payload from before the reviewer fields keeps acting on can_act alone", async () => {
			state.view = {
				...ar("AR-1", { source: "File Box" }),
				question: "Ship it?",
				conversation: "conv-1",
				can_act: 1,
			};
			const w = await board();
			expect(buttons(w)).toEqual(
				expect.arrayContaining([
					"Open Chat",
					"Approve",
					"Reject",
					"Ignore",
					"Tag for review",
				])
			);
			expect(panel(w).attributes("data-can-write")).toBe("true");
		});

		it("someone who cannot act sees the waiting note, no controls", async () => {
			state.view = fileBoxView({ can_act: 0, can_manage: 0 });
			const w = await board();
			expect(pane(w).text()).toContain("Waiting for a decision");
			expect(buttons(w)).not.toContain("Approve");
			expect(buttons(w)).not.toContain("Tag for review");
			expect(panel(w).attributes("data-can-write")).toBe("false");
		});
	});

	describe("realtime access changes", () => {
		it("a revoke drops the open question quietly: no error toast, no 'not found'", async () => {
			state.view = reviewerView();
			const w = await board();
			expect(pane(w).text()).toContain("Which warehouse?");
			approvals.getApproval.mockRejectedValue(denied());
			state.rows = [];
			const calls = api.listApprovalsPage.mock.calls.length;
			socket.emit({ kind: "approval:untagged", name: "AR-1" });
			await flushPromises();
			await flushPromises();
			expect(api.listApprovalsPage.mock.calls.length).toBeGreaterThan(calls);
			expect(toast.error).not.toHaveBeenCalled();
			expect(pane(w).text()).not.toContain("Which warehouse?");
			expect(pane(w).text()).not.toContain("Approval not found");
			expect(pane(w).text()).toContain("Select an approval");
			expect(route.params.id).toBeUndefined();
			expect(rail(w).text()).not.toContain("Question AR-1");
		});

		it("a revoke of another question leaves the open one alone", async () => {
			state.view = reviewerView();
			const w = await board();
			const reads = approvals.getApproval.mock.calls.length;
			socket.emit({ kind: "approval:untagged", name: "AR-9" });
			await flushPromises();
			expect(pane(w).text()).toContain("Which warehouse?");
			expect(approvals.getApproval.mock.calls.length).toBe(reads);
		});

		it("a new tag brings the question onto the rail", async () => {
			state.rows = [];
			state.view = reviewerView();
			const w = await board("");
			expect(rail(w).text()).not.toContain("Question AR-2");
			state.rows = [ar("AR-2", { review_scoped: 1 })];
			socket.emit({ kind: "approval:tagged", name: "AR-2" });
			await flushPromises();
			await flushPromises();
			expect(rail(w).text()).toContain("Question AR-2");
		});

		it("a settle re-reads the rail and the open question", async () => {
			state.view = reviewerView();
			const w = await board();
			expect(buttons(w)).toContain("Approve");
			state.view = reviewerView({
				status: "Approved",
				decision: "Main",
				decided_by: "ja@example.com",
			});
			state.rows = [ar("AR-1", { review_scoped: 1, status: "Approved" })];
			socket.emit({ kind: "action:settled", name: "AR-1" });
			await flushPromises();
			await flushPromises();
			expect(buttons(w)).not.toContain("Approve");
			expect(pane(w).text()).toContain("Main");
		});

		it("a burst of events costs one re-read, not one each", async () => {
			state.view = reviewerView();
			await board();
			const lists = api.listApprovalsPage.mock.calls.length;
			for (let i = 0; i < 5; i++) socket.emit({ kind: "approval:untagged", name: "AR-9" });
			await flushPromises();
			await flushPromises();
			expect(api.listApprovalsPage.mock.calls.length - lists).toBeLessThanOrEqual(2);
		});

		it("ignores frames that mean nothing for approvals, and stops listening when gone", async () => {
			const w = await board();
			const lists = api.listApprovalsPage.mock.calls.length;
			socket.emit({ kind: "run:end", conversation_id: "conv-1" });
			await flushPromises();
			expect(api.listApprovalsPage.mock.calls.length).toBe(lists);
			w.unmount();
			expect(socket.count()).toBe(0);
		});

		it("a decision refused because access just went drops the question", async () => {
			state.view = reviewerView({ options: ["Main", "Spare", "Transit"] });
			const w = await board();
			api.decideApproval.mockRejectedValue(denied());
			approvals.getApproval.mockRejectedValue(denied());
			state.rows = [];
			await button(w, "Main").trigger("click");
			await button(w, "Approve").trigger("click");
			await flushPromises();
			await flushPromises();
			// the reviewer who just clicked is told, once
			expect(toast.error).toHaveBeenCalledTimes(1);
			expect(pane(w).text()).not.toContain("Which warehouse?");
			expect(rail(w).text()).not.toContain("Question AR-1");
		});
	});
	// Sowmya re-review: a decision on a review-scoped question reports whether it
	// reached the requester's chat. Only "done" (accepted) or "" (not tracked) leaves
	// the record; failed / not confirmed keep it open with a persistent notice, and
	// only the requester may send the stored decision, one request at a time.
	describe("decision delivery to the requester's chat", () => {
		const REVIEWER_NOT_SENT =
			"Decision saved, but it was not sent to the requester's chat. Only the requester can send it, from Approvals.";
		const PENDING = "Decision saved. Sending it to the chat is not confirmed yet.";
		const opts = { options: ["Main", "Spare"] };
		const decideAs = async (w, delivery, decided) => {
			api.decideApproval.mockImplementation(async () => {
				state.view = decided;
				return delivery;
			});
			await button(w, "Main").trigger("click");
			await button(w, "Approve").trigger("click");
			await flushPromises();
			await flushPromises();
		};
		const failedView = (view, over = {}) =>
			view({
				...opts,
				status: "Approved",
				decision: "Main",
				continuation: "failed",
				...over,
			});

		it("reviewer, not sent: a persistent notice, no send button, the record stays", async () => {
			state.view = reviewerView(opts);
			const w = await board();
			await decideAs(
				w,
				{ ok: true, status: "Approved", resumed: false, continuation: "failed" },
				failedView(reviewerView, { can_continue: 0 })
			);
			expect(toast.warning).toHaveBeenCalledWith(REVIEWER_NOT_SENT);
			expect(toast.success).not.toHaveBeenCalled();
			expect(JSON.stringify(toast.warning.mock.calls)).not.toContain("notified");
			expect(rail(w).text()).toContain("Question AR-1");
			expect(rail(w).text()).toContain("Not sent to chat");
			expect(pane(w).text()).toContain(REVIEWER_NOT_SENT);
			expect(buttons(w)).not.toContain("Send decision to chat");
			expect(w.find(".conv-link").exists()).toBe(false);
		});

		it("requester, not sent: the notice offers Send decision to chat", async () => {
			state.view = requesterView(opts);
			const w = await board();
			await decideAs(
				w,
				{ ok: true, status: "Approved", resumed: false, continuation: "failed" },
				failedView(requesterView, { can_continue: 1 })
			);
			expect(toast.warning).toHaveBeenCalledWith(
				"Decision saved, but it was not sent to your chat. Use Send decision to chat to try again."
			);
			expect(rail(w).text()).toContain("Question AR-1");
			expect(pane(w).text()).toContain("Decision saved, but it was not sent to your chat.");
			expect(buttons(w)).toContain("Send decision to chat");
		});

		it("sent: names the chat it reached and moves on", async () => {
			state.view = reviewerView(opts);
			const w = await board();
			await decideAs(
				w,
				{ ok: true, status: "Approved", resumed: true, continuation: "done" },
				reviewerView({ ...opts, status: "Approved", continuation: "done" })
			);
			expect(toast.success).toHaveBeenCalledWith("Approved - sent to the requester's chat");
			expect(rail(w).text()).not.toContain("Question AR-1");
		});

		it("not confirmed (pending, unknown or no answer): stays open, never claims it was sent", async () => {
			for (const delivery of [
				{ ok: true, status: "Approved", resumed: false, continuation: "pending" },
				null,
			]) {
				vi.clearAllMocks();
				state.view = requesterView(opts);
				state.rows = [ar("AR-1", { review_scoped: 1 })];
				const w = await board();
				await decideAs(
					w,
					delivery,
					requesterView({ ...opts, status: "Approved", continuation: "pending" })
				);
				expect(toast.warning).toHaveBeenCalledWith(PENDING);
				expect(toast.success).not.toHaveBeenCalled();
				expect(rail(w).text()).toContain("Question AR-1");
				expect(pane(w).text()).toContain(PENDING);
				expect(buttons(w)).not.toContain("Send decision to chat");
			}
		});

		it("an untracked question keeps today's copy and flow", async () => {
			state.view = requesterView(opts);
			const w = await board();
			await decideAs(
				w,
				{ ok: true, status: "Approved", resumed: true, continuation: "" },
				requesterView({ ...opts, status: "Approved" })
			);
			expect(toast.success).toHaveBeenCalledWith("Approved - conversation resumed");
			expect(rail(w).text()).not.toContain("Question AR-1");
		});

		it("a non-Chat question is unchanged", async () => {
			state.rows = [ar("AR-1", { source: "File Box" })];
			state.view = fileBoxView();
			const w = await board();
			api.decideApproval.mockResolvedValue({ ok: true, status: "Approved", resumed: true });
			await button(w, "Acme").trigger("click");
			await button(w, "Approve").trigger("click");
			await flushPromises();
			expect(toast.success).toHaveBeenCalledWith("Approved - conversation resumed");
			expect(toast.warning).not.toHaveBeenCalled();
		});

		describe("Send decision to chat", () => {
			beforeEach(() => {
				state.rows = [
					ar("AR-1", { review_scoped: 1, status: "Approved", continuation: "failed" }),
				];
				state.view = failedView(requesterView, { can_continue: 1 });
			});

			it("sends once while in flight, then shows what the server reports", async () => {
				let finish;
				api.continueApprovalChat.mockImplementation(
					() => new Promise((resolve) => (finish = resolve))
				);
				const w = await board();
				expect(rail(w).text()).toContain("Not sent to chat");
				await button(w, "Send decision to chat").trigger("click");
				await flushPromises();
				expect(button(w, "Send decision to chat").attributes("disabled")).toBeDefined();
				await button(w, "Send decision to chat").trigger("click");
				state.view = requesterView({ ...opts, status: "Approved", continuation: "done" });
				state.rows = [
					ar("AR-1", { review_scoped: 1, status: "Approved", continuation: "done" }),
				];
				finish({ ok: true, continuation: "done" });
				await flushPromises();
				await flushPromises();
				expect(api.continueApprovalChat).toHaveBeenCalledTimes(1);
				expect(api.continueApprovalChat).toHaveBeenCalledWith("AR-1");
				expect(toast.success).toHaveBeenCalledWith("Decision sent to your chat");
				expect(buttons(w)).not.toContain("Send decision to chat");
				expect(rail(w).text()).not.toContain("Not sent to chat");
			});

			it("a refusal shows its reason and the button stays", async () => {
				api.continueApprovalChat.mockResolvedValue({
					ok: false,
					continuation: "failed",
					reason: "a reply is already in progress - hang on a moment",
				});
				const w = await board();
				await button(w, "Send decision to chat").trigger("click");
				await flushPromises();
				await flushPromises();
				expect(toast.error).toHaveBeenCalledWith(
					"a reply is already in progress - hang on a moment"
				);
				expect(buttons(w)).toContain("Send decision to chat");
			});

			it("a lost response re-reads the record and never resends", async () => {
				api.continueApprovalChat.mockRejectedValue(new Error("network down"));
				const w = await board();
				const reads = approvals.getApproval.mock.calls.length;
				state.view = requesterView({ ...opts, status: "Approved", continuation: "done" });
				await button(w, "Send decision to chat").trigger("click");
				await flushPromises();
				await flushPromises();
				expect(api.continueApprovalChat).toHaveBeenCalledTimes(1);
				expect(approvals.getApproval.mock.calls.length).toBeGreaterThan(reads);
				expect(buttons(w)).not.toContain("Send decision to chat");
			});

			it("a reviewer sees the notice only", async () => {
				state.view = failedView(reviewerView, { can_continue: 0 });
				const w = await board();
				expect(pane(w).text()).toContain(REVIEWER_NOT_SENT);
				expect(buttons(w)).not.toContain("Send decision to chat");
			});
		});

		it("re-reads the open question when its decision was not sent", async () => {
			const w = await board();
			const reads = approvals.getApproval.mock.calls.length;
			socket.emit({ kind: "approval:continuation_failed", name: "AR-1" });
			await flushPromises();
			await flushPromises();
			expect(approvals.getApproval.mock.calls.length).toBeGreaterThan(reads);
			expect(w.exists()).toBe(true);
		});
	});
});

// The board's stub above copies this: frappe-ui names an icon button only from `label`.
describe("frappe-ui Button accessible name (#756)", () => {
	it("comes from label; a passed aria-label is overwritten", async () => {
		const { default: RealButton } = await import(
			"../../../node_modules/frappe-ui/src/components/Button/Button.vue"
		);
		const named = mount(RealButton, { props: { icon: "x", label: "Remove Jai Admin" } });
		expect(named.find("button").attributes("aria-label")).toBe("Remove Jai Admin");
		expect(named.find("button").text()).toBe("");
		const unnamed = mount(RealButton, {
			props: { icon: "x" },
			attrs: { "aria-label": "Remove Jai Admin" },
		});
		expect(unnamed.find("button").attributes("aria-label")).toBeUndefined();
		named.unmount();
		unnamed.unmount();
	});
});
