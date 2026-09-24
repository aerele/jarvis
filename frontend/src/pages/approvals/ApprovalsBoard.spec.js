import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// One inbox: held File Box writes, chat cards and wiki notes are rows in the rail's
// "Needs your decision" group and open their detail in the right pane.
const probe = vi.hoisted(() => ({
	mounts: [],
	refreshes: [],
	updates: 0,
	docmetaIds: [],
	later: null,
}));

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
	Autocomplete: { template: "<div/>" },
	Badge: { props: ["label", "theme"], template: '<span class="badge">{{ label }}</span>' },
	Breadcrumbs: { props: ["items"], template: "<nav/>" },
	Button: {
		props: ["label", "disabled", "loading", "tooltip"],
		emits: ["click"],
		template:
			'<button :disabled="disabled" :data-tip="tooltip" @click="$emit(\'click\')">{{ label }}</button>',
	},
	FeatherIcon: { template: "<i/>" },
	FormControl: {
		props: ["modelValue", "type", "options", "placeholder"],
		emits: ["update:modelValue"],
		template: `<select v-if="type === 'select'" :value="modelValue" @change="$emit('update:modelValue', $event.target.value)"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select><input v-else :placeholder="placeholder" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	Popover: { template: '<div><slot name="target" :togglePopover="() => {}" /></div>' },
	Tooltip: { template: "<span><slot/></span>" },
	toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
	call: vi.fn(),
}));
vi.mock("@/api", () => ({
	listApprovalsPage: vi.fn(),
	listWikiWriteProposals: vi.fn(),
	decideApproval: vi.fn(),
	dismissApproval: vi.fn(),
	restoreApproval: vi.fn(),
	approveWikiWrite: vi.fn(),
	rejectWikiWrite: vi.fn(),
	retryWikiWrite: vi.fn(),
	listShareableUsers: vi.fn(async () => []),
	getListFilterSchema: vi.fn(),
	getListFilterCapabilities: vi.fn(),
}));
vi.mock("@/api/approvals", () => ({ getApproval: vi.fn(), listPendingActionsLane: vi.fn() }));
const refreshApprovalsCount = vi.fn();
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ refreshApprovalsCount }) }));
vi.mock("@/data/session", () => ({ session: { user: "me@example.com" } }));
vi.mock("@/utils/datetime", () => ({ timeAgo: () => "5 minutes ago", exactDate: () => "" }));
vi.mock("@/composables/useDocmeta", () => ({
	useDocmeta: (_doctype, id) => {
		probe.docmetaIds.push(id);
		return { meta: null, reload: vi.fn(), toggleShare: vi.fn() };
	},
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
vi.mock("@/components/doc/DocMetaPanel.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/components/doc/CommentsSection.vue", () => ({ default: { template: "<div/>" } }));

// The held/chat detail stubs record every mount (a new selection must mount a fresh
// one) and every refresh the board asks for. "decide later" keeps the board's
// callback to answer after a switch unmounted the stub. The wiki detail is real.
function detailStub(kind, cls) {
	return async () => {
		const { onMounted, onUpdated } = await import("vue");
		return {
			default: {
				props: ["name", "onDecided"],
				setup(props, { expose }) {
					onMounted(() => probe.mounts.push(kind + ":" + props.name));
					onUpdated(() => probe.updates++);
					expose({ refresh: () => probe.refreshes.push(kind + ":" + props.name) });
					// read at call time, as the details do: after the unmount
					return { later: () => (probe.later = (res) => props.onDecided(res)) };
				},
				template: `<div class="${cls}"><span class="who">${kind} {{ name }}</span><button class="decide" @click="onDecided({ ok: true })">decide</button><button class="decide-later" @click="later()">later</button></div>`,
			},
		};
	};
}
vi.mock("./PendingActionDetail.vue", detailStub("held", "held-detail"));
vi.mock("./PendingChatDetail.vue", detailStub("chat", "chat-detail"));

import { useRoute, useRouter } from "vue-router";
import * as api from "@/api";
import * as approvals from "@/api/approvals";
import ApprovalsBoard from "./ApprovalsBoard.vue";

const route = useRoute();
const router = useRouter();
const HOSTILE = "<script>window.__pwned=1</script>";

const ar = (name, over = {}) => ({
	name,
	title: "Question " + name,
	status: "Pending",
	source: "Chat",
	document_type: "Sales Order",
	creation: "2026-09-01 12:00:00",
	...over,
});
const heldRow = (name, over = {}) => ({
	name,
	kind: "file_box_held",
	status: "Pending",
	summary: "New supplier: Acme",
	document_type: "Supplier",
	waiters_count: 1,
	for_user: "",
	created_at: "2026-09-01 10:00:00",
	...over,
});
const chatRow = (over = {}) => ({
	name: "PA-9",
	kind: "chat",
	status: "Pending",
	summary: "Create a ToDo",
	document_type: "ToDo",
	created_at: "2026-09-01 10:10:00",
	conversation: "conv-1",
	conversation_title: "Quarter close",
	origin_page: "",
	...over,
});
const wikiRow = (over = {}) => ({
	name: "AR-W1",
	title: "Fake Co terms",
	status: "Pending",
	can_approve: 1,
	needs_retry: 0,
	wiki_digest: "d1",
	dropper: "asha@example.com",
	creation: "2026-09-01 10:15:00",
	preview: { slug: "party-fake-co", append_md: "note" },
	...over,
});

let state;
function reset() {
	state = {
		ar: [ar("AR-1"), ar("AR-2")],
		lane: [
			heldRow("PA-1"),
			heldRow("PA-2", {
				status: "Executing",
				summary: HOSTILE,
				waiters_count: 3,
				for_user: "Asha",
				document_type: "",
				created_at: "2026-09-01 10:05:00",
			}),
			chatRow(),
		],
		wiki: [wikiRow()],
		wikiTotal: 1,
	};
}

async function mountBoard(query = {}, params = {}) {
	route.name = params.id ? "ApprovalDetail" : "ApprovalsList";
	route.params = { ...params };
	route.query = { ...query };
	const w = mount(ApprovalsBoard, {
		attachTo: document.body,
		global: { stubs: { "router-link": true } },
	});
	await flushPromises();
	await flushPromises();
	return w;
}

const rail = (w) => w.find(".w-\\[360px\\]");
const group = (w) => w.find('[aria-labelledby="actions-title"]');
const actionRow = (w, text) =>
	group(w)
		.findAll("button")
		.find((b) => b.text().includes(text));
const arRow = (w, name) =>
	rail(w)
		.findAll("button")
		.find((b) => b.text().includes("Question " + name));
const pane = (w) => w.find(".flex-1.overflow-y-auto");
const button = (w, label) => w.findAll("button").find((b) => b.text() === label);

describe("ApprovalsBoard one inbox", () => {
	let mounted = [];
	beforeEach(() => {
		vi.clearAllMocks();
		reset();
		probe.mounts.length = 0;
		probe.refreshes.length = 0;
		probe.updates = 0;
		probe.docmetaIds.length = 0;
		api.listApprovalsPage.mockImplementation(async () => ({
			rows: state.ar,
			total: state.ar.length,
			has_more: false,
			facets: { document_type: [{ value: "Sales Order", count: state.ar.length }] },
			awaiting_reply: [],
		}));
		approvals.listPendingActionsLane.mockImplementation(async () => ({ rows: state.lane }));
		api.listWikiWriteProposals.mockImplementation(async () => ({
			rows: state.wiki,
			total: state.wikiTotal,
		}));
		approvals.getApproval.mockImplementation(async (name) => ({
			...ar(name),
			question: "Ship it?",
			can_act: 1,
		}));
	});
	afterEach(() => {
		mounted.forEach((w) => w.unmount());
		mounted = [];
	});
	const board = async (...args) => {
		const w = await mountBoard(...args);
		mounted.push(w);
		return w;
	};

	it("stacks no panels: the root is the header, the toolbar and the split view", async () => {
		const w = await board();
		expect(w.element.children).toHaveLength(3);
		expect(w.element.children[2].contains(group(w).element)).toBe(true);
	});

	it("lists every decision above the questions, as text, with kind and type chips", async () => {
		state.wikiTotal = 34;
		const w = await board();
		const g = group(w);
		expect(g.text()).toContain("Needs your decision (4)");
		expect(g.text()).toContain("1 file waiting");
		expect(g.text()).toContain("3 files waiting · dropped by Asha");
		expect(g.text()).toContain("Creating…");
		expect(g.text()).toContain("Quarter close");
		expect(g.text()).toContain("party-fake-co · dropped by asha@example.com");
		for (const chip of ["New record", "Chat action", "Wiki note", "Supplier", "ToDo"])
			expect(g.text()).toContain(chip);
		expect(g.text()).toContain("Unclassified");
		expect(g.text()).toContain("1 of 34 wiki notes");
		expect(g.text()).toContain(HOSTILE);
		expect(w.html()).not.toContain("<script>");
		const text = rail(w).text();
		expect(text.indexOf("Needs your decision")).toBeLessThan(text.indexOf("Questions"));
		expect(text.indexOf("Questions")).toBeLessThan(text.indexOf("Question AR-1"));
		expect(text).toContain("2 of 2 questions");
	});

	it("collapses the group", async () => {
		const w = await board();
		const header = group(w).find("[aria-expanded]");
		await header.trigger("click");
		expect(header.attributes("aria-expanded")).toBe("false");
		expect(group(w).text()).not.toContain("1 file waiting");
	});

	it("opens each kind in its own detail in the right pane, never through get_approval", async () => {
		const w = await board();
		approvals.getApproval.mockClear();
		await actionRow(w, "3 files waiting").trigger("click");
		expect(pane(w).find(".held-detail .who").text()).toBe("held PA-2");
		expect(pane(w).find("h1").text()).toBe(HOSTILE);
		expect(pane(w).text()).toContain("New record");
		expect(pane(w).text()).toContain("Creating…");
		expect(route.name).toBe("ApprovalsList");
		expect(route.query).toEqual({ held: "PA-2" });

		await actionRow(w, "Quarter close").trigger("click");
		expect(pane(w).find(".chat-detail .who").text()).toBe("chat PA-9");
		expect(route.query).toEqual({ held: "PA-9" });

		await actionRow(w, "party-fake-co").trigger("click");
		expect(pane(w).find("h1").text()).toBe("Fake Co terms");
		expect(pane(w).text()).toContain("party-fake-co · dropped by asha@example.com");
		expect(route.query).toEqual({ wiki: "AR-W1" });
		expect(approvals.getApproval).not.toHaveBeenCalled();
		expect(probe.docmetaIds[0].value).toBe("");

		await arRow(w, "AR-2").trigger("click");
		await flushPromises();
		expect(approvals.getApproval).toHaveBeenCalledWith("AR-2");
		expect(route.name).toBe("ApprovalDetail");
		expect(route.params).toEqual({ id: "AR-2" });
		expect(route.query).toEqual({});
		expect(pane(w).text()).not.toContain("dropped by");
		expect(pane(w).text()).toContain("Ship it?");
	});

	it("a new selection mounts a fresh detail, so the row shown is the row decided", async () => {
		const w = await board();
		await actionRow(w, "1 file waiting").trigger("click");
		await actionRow(w, "3 files waiting").trigger("click");
		expect(probe.mounts).toEqual(["held:PA-1", "held:PA-2"]);
		state.lane = state.lane.filter((r) => r.name !== "PA-2");
		await pane(w).find(".held-detail .decide").trigger("click");
		await flushPromises();
		expect(group(w).text()).not.toContain("3 files waiting");
		expect(group(w).text()).toContain("1 file waiting");
	});

	it("deciding drops the row, refreshes the badge and selects the next row down", async () => {
		const w = await board();
		await actionRow(w, "1 file waiting").trigger("click");
		state.lane = state.lane.filter((r) => r.name !== "PA-1");
		const lanes = approvals.listPendingActionsLane.mock.calls.length;
		await pane(w).find(".held-detail .decide").trigger("click");
		await flushPromises();
		expect(refreshApprovalsCount).toHaveBeenCalled();
		expect(approvals.listPendingActionsLane.mock.calls.length).toBe(lanes + 1);
		expect(group(w).text()).not.toContain("1 file waiting");
		expect(pane(w).find(".held-detail .who").text()).toBe("held PA-2");
		expect(route.query).toEqual({ held: "PA-2" });
	});

	it("deciding a chat card drops it, refreshes the badge and selects the next row", async () => {
		const w = await board();
		await actionRow(w, "Quarter close").trigger("click");
		state.lane = state.lane.filter((r) => r.name !== "PA-9");
		await pane(w).find(".chat-detail .decide").trigger("click");
		await flushPromises();
		expect(refreshApprovalsCount).toHaveBeenCalled();
		expect(group(w).text()).not.toContain("Quarter close");
		expect(pane(w).find("h1").text()).toBe("Fake Co terms");
		expect(route.query).toEqual({ wiki: "AR-W1" });
	});

	it("an answer that lands after a switch settles its own row, not the open one", async () => {
		const w = await board();
		await actionRow(w, "1 file waiting").trigger("click");
		await pane(w).find(".held-detail .decide-later").trigger("click");
		await actionRow(w, "3 files waiting").trigger("click");
		expect(probe.mounts).toEqual(["held:PA-1", "held:PA-2"]);
		state.lane = state.lane.filter((r) => r.name !== "PA-1");
		probe.later({ ok: true });
		await flushPromises();
		expect(refreshApprovalsCount).toHaveBeenCalled();
		expect(group(w).text()).not.toContain("1 file waiting");
		expect(group(w).text()).toContain("3 files waiting");
		expect(pane(w).find(".held-detail .who").text()).toBe("held PA-2");
		expect(route.query).toEqual({ held: "PA-2" });
	});

	it("an answer that lands after the board is gone never navigates back to it", async () => {
		const w = await mountBoard();
		await actionRow(w, "Quarter close").trigger("click");
		await pane(w).find(".chat-detail .decide-later").trigger("click");
		w.unmount();
		router.replace.mockClear();
		router.push.mockClear();
		refreshApprovalsCount.mockClear();
		probe.later({ ok: true });
		await flushPromises();
		expect(router.replace).not.toHaveBeenCalled();
		expect(router.push).not.toHaveBeenCalled();
		expect(refreshApprovalsCount).toHaveBeenCalled();
	});

	it("a question decided after the board is gone never navigates back to it", async () => {
		approvals.getApproval.mockImplementation(async (name) => ({
			...ar(name, { source: "File Box" }),
			can_act: 1,
		}));
		let resolve;
		api.decideApproval.mockReturnValue(new Promise((r) => (resolve = r)));
		const w = await mountBoard();
		await button(pane(w), "Approve").trigger("click");
		w.unmount();
		router.replace.mockClear();
		router.push.mockClear();
		resolve({});
		await flushPromises();
		expect(router.replace).not.toHaveBeenCalled();
		expect(router.push).not.toHaveBeenCalled();
		expect(refreshApprovalsCount).toHaveBeenCalled();
	});

	it("an open detail is not re-rendered by unrelated board updates", async () => {
		const w = await board();
		await actionRow(w, "1 file waiting").trigger("click");
		await flushPromises();
		probe.updates = 0;
		await w.find('input[placeholder="Search approvals"]').setValue("acme");
		await w.find('button[data-tip="Refresh"]').trigger("click");
		await flushPromises();
		expect(probe.updates).toBe(0);
	});

	it("deciding the last decision moves on to the first question", async () => {
		const w = await board();
		await actionRow(w, "party-fake-co").trigger("click");
		state.wiki = [];
		approvals.getApproval.mockClear();
		api.approveWikiWrite.mockResolvedValue({ ok: true, applied: true });
		await button(pane(w), "Approve").trigger("click");
		await flushPromises();
		expect(api.approveWikiWrite).toHaveBeenCalledWith("AR-W1", "d1");
		expect(approvals.getApproval).toHaveBeenCalledWith("AR-1");
		expect(pane(w).text()).not.toContain("dropped by");
		expect(route.params).toEqual({ id: "AR-1" });
		expect(route.query).toEqual({});
	});

	it("a refused wiki approve reloads the list and keeps the note open", async () => {
		const w = await board();
		await actionRow(w, "party-fake-co").trigger("click");
		state.wiki = [wikiRow({ wiki_digest: "d2" })];
		api.approveWikiWrite.mockRejectedValue(
			Object.assign(new Error("This proposal changed since you opened it"), {
				exc_type: "PermissionError",
			})
		);
		const calls = api.listWikiWriteProposals.mock.calls.length;
		await button(pane(w), "Approve").trigger("click");
		await flushPromises();
		expect(api.listWikiWriteProposals.mock.calls.length).toBe(calls + 1);
		expect(pane(w).text()).toContain("This note changed since you opened it");
		expect(route.query).toEqual({ wiki: "AR-W1" });
	});

	it("?held= opens the File Box link's row once the rows load, instead of a question", async () => {
		const w = await board({ held: "PA-2" });
		expect(pane(w).find(".held-detail .who").text()).toBe("held PA-2");
		expect(approvals.getApproval).not.toHaveBeenCalled();
		expect(actionRow(w, "3 files waiting").classes()).toContain("bg-surface-selected");
	});

	it("?held= naming a chat card opens the chat detail", async () => {
		const w = await board({ held: "PA-9" });
		expect(pane(w).find(".chat-detail .who").text()).toBe("chat PA-9");
	});

	it("?wiki= opens the wiki note; with both, held wins", async () => {
		const w = await board({ wiki: "AR-W1" });
		expect(pane(w).text()).toContain("party-fake-co · dropped by asha@example.com");
		const both = await board({ held: "PA-1", wiki: "AR-W1" });
		expect(pane(both).find(".held-detail .who").text()).toBe("held PA-1");
		expect(pane(both).text()).not.toContain("dropped by");
	});

	it("a deep-linked name beyond the loaded rows still opens its detail", async () => {
		const w = await board({ held: "PA-404" });
		expect(pane(w).find(".held-detail .who").text()).toBe("held PA-404");
		// no row to name it: one plain heading, no chip repeating it
		expect(pane(w).find("h1").text()).toBe("New record");
		expect(
			pane(w)
				.findAll(".badge")
				.map((b) => b.text())
		).not.toContain("New record");
	});

	it("?wiki= for a note not in the list says it isn't waiting for this reviewer", async () => {
		const w = await board({ wiki: "AR-404" });
		expect(pane(w).text()).toContain("This wiki note isn't waiting for your review.");
		expect(button(pane(w), "Approve")).toBeFalsy();
	});

	it("a ?held= chat card the rows place late still opens as a chat card", async () => {
		state.lane = [heldRow("PA-1")];
		const w = await board({ held: "PA-9" });
		expect(pane(w).find(".held-detail .who").text()).toBe("held PA-9");
		state.lane = [heldRow("PA-1"), chatRow()];
		await w.find('button[data-tip="Refresh"]').trigger("click");
		await flushPromises();
		expect(pane(w).find(".chat-detail .who").text()).toBe("chat PA-9");
		expect(pane(w).find(".held-detail").exists()).toBe(false);
		expect(route.query).toEqual({ held: "PA-9" });
	});

	it("follows the link as it changes, and ignores a link to what is already open", async () => {
		const w = await board();
		route.query = { held: "PA-9" };
		await flushPromises();
		expect(pane(w).find(".chat-detail .who").text()).toBe("chat PA-9");
		route.query = { held: "PA-9", status: "Pending" };
		await flushPromises();
		expect(probe.mounts).toEqual(["chat:PA-9"]);
	});

	it("waits for the decision rows before saying the board is empty", async () => {
		state.ar = [];
		let resolve;
		approvals.listPendingActionsLane.mockReturnValueOnce(new Promise((r) => (resolve = r)));
		const w = await board();
		expect(rail(w).find(".spinner").exists()).toBe(true);
		expect(rail(w).text()).not.toContain("No pending approvals");
		resolve({ rows: state.lane });
		await flushPromises();
		expect(group(w).text()).toContain("1 file waiting");
		expect(rail(w).text()).not.toContain("No pending approvals");
		expect(rail(w).find(".spinner").exists()).toBe(false);
	});

	it("is empty only when nothing at all waits", async () => {
		state.ar = [];
		state.lane = [];
		state.wiki = [];
		const w = await board();
		expect(group(w).exists()).toBe(false);
		expect(rail(w).text()).toContain("No pending approvals");
	});

	it("search and the type filter apply to the group; the type list has its doctypes", async () => {
		const w = await board();
		const [, type] = w.findAll("select");
		const labels = type.findAll("option").map((o) => o.text());
		expect(labels).toEqual([
			"All types",
			"Sales Order (2)",
			"Supplier (1)",
			"Unclassified (1)",
			"ToDo (1)",
		]);
		await type.setValue("Supplier");
		await flushPromises();
		expect(group(w).text()).toContain("Needs your decision (1)");
		expect(group(w).text()).toContain("1 file waiting");
		expect(group(w).text()).not.toContain("party-fake-co");
		await type.setValue("");
		await flushPromises();
		await w.find('input[placeholder="Search approvals"]').setValue("quarter");
		expect(group(w).text()).toContain("Needs your decision (1)");
		expect(group(w).text()).toContain("Quarter close");
	});

	it("leaving the Pending view hides the group and lets go of its selection", async () => {
		const w = await board();
		await actionRow(w, "1 file waiting").trigger("click");
		const [status] = w.findAll("select");
		await status.setValue("Decided");
		await flushPromises();
		expect(group(w).exists()).toBe(false);
		expect(pane(w).find(".held-detail").exists()).toBe(false);
		expect(route.query).toEqual({ status: "Decided" });
	});

	it("a selected row that leaves the rail keeps its pane and re-reads how it settled", async () => {
		const w = await board();
		await actionRow(w, "1 file waiting").trigger("click");
		state.lane = state.lane.filter((r) => r.name !== "PA-1");
		const lanes = approvals.listPendingActionsLane.mock.calls.length;
		await w.find('button[data-tip="Refresh"]').trigger("click");
		await flushPromises();
		expect(approvals.listPendingActionsLane.mock.calls.length).toBe(lanes + 1);
		expect(group(w).text()).not.toContain("1 file waiting");
		expect(pane(w).find(".held-detail .who").text()).toBe("held PA-1");
		expect(probe.refreshes).toEqual(["held:PA-1"]);
	});

	it("a failed load says so, with a retry", async () => {
		approvals.listPendingActionsLane.mockRejectedValueOnce(new Error("boom"));
		const w = await board();
		expect(group(w).find('[role="alert"]').text()).toContain("boom");
		await group(w)
			.findAll("button")
			.find((b) => b.text() === "Try again")
			.trigger("click");
		await flushPromises();
		expect(group(w).find('[role="alert"]').exists()).toBe(false);
		expect(group(w).text()).toContain("1 file waiting");
	});
});
