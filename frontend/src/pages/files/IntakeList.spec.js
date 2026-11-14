import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("vue-router", async () => {
	const { reactive } = await import("vue");
	const route = reactive({ name: "FilesList", params: {}, query: {} });
	return { useRoute: () => route, useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) };
});
vi.mock("@vueuse/core", async (importOriginal) => {
	const actual = await importOriginal();
	const { ref } = await import("vue");
	return { ...actual, useStorage: (_key, initial) => ref(initial) };
});
vi.mock("frappe-ui", () => ({
	Badge: {
		props: ["label", "theme"],
		template: '<span class="badge" :data-theme="theme">{{ label }}</span>',
	},
	Button: {
		props: ["label"],
		emits: ["click"],
		template: '<button class="btn" @click="$emit(\'click\')">{{ label }}</button>',
	},
	FeatherIcon: { template: "<i/>" },
	Select: {
		props: ["options", "modelValue"],
		emits: ["update:modelValue"],
		template:
			'<select class="scope" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select>',
	},
	TabButtons: { template: '<div class="tabs"/>' },
	toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/components/list/ListPage.vue", () => ({
	default: {
		props: ["rows", "emptyState"],
		template: `<div><slot name="right-header" /><div v-if="!rows.length" class="empty">{{ emptyState.description }}</div><div v-for="row in rows" :key="row.name" class="row"><slot name="cell-file" :row="row" /><slot name="cell-source" :row="row" /><slot name="cell-status" :row="row" /><slot name="cell-draft" :row="row" /><slot name="cell-summary" :row="row" /><slot name="cell-_retry" :row="row" /></div></div>`,
	},
}));
vi.mock("@/api", () => ({
	getListFilterSchema: vi.fn(),
	getListFilterCapabilities: vi.fn(),
}));
vi.mock("@/api/intake", () => ({ intakeListPage: vi.fn(), intakeRetry: vi.fn() }));
vi.mock("@/api/triggers", () => ({ getTriggersCaps: vi.fn() }));

import { intakeListPage, intakeRetry } from "@/api/intake";
import { getTriggersCaps } from "@/api/triggers";
import IntakeList from "./IntakeList.vue";

const row = (over = {}) => ({
	name: "A1",
	creation: "2026-11-13 10:00:00",
	file_name: "bill.pdf",
	file_url: "/private/files/bill.pdf",
	source_label: "Folder: Bills",
	status: "Success",
	summary: "Draft ready",
	draft_doctype: "Purchase Invoice",
	draft_name: "PINV-0001",
	approval_request: "",
	can_retry: false,
	...over,
});

async function mountList(rows, can_manage = false) {
	getTriggersCaps.mockResolvedValue({ can_manage });
	intakeListPage.mockResolvedValue({ rows, total: rows.length, has_more: false });
	const w = mount(IntakeList, {
		global: {
			stubs: { "router-link": { props: ["to"], template: '<a :href="to"><slot /></a>' } },
		},
	});
	await flushPromises();
	return w;
}

describe("IntakeList", () => {
	let w;
	beforeEach(() => {
		vi.clearAllMocks();
		vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
	});
	afterEach(() => {
		w && w.unmount();
		vi.useRealTimers();
	});

	it("shows intake rows with source, status and the draft link", async () => {
		w = await mountList([
			row(),
			row({ name: "A2", status: "Failed", draft_doctype: "", draft_name: "" }),
		]);
		const first = w.findAll(".row")[0];
		expect(first.text()).toContain("bill.pdf");
		expect(first.text()).toContain("Folder: Bills");
		expect(first.find(".badge").text()).toBe("Draft created");
		expect(first.find(".badge").attributes("data-theme")).toBe("green");
		expect(first.find("a").attributes("href")).toBe("/app/purchase-invoice/PINV-0001");
		expect(w.findAll(".row")[1].find(".badge").attributes("data-theme")).toBe("red");
	});

	it("shows a deleted draft as Draft deleted, neutral, with no link", async () => {
		w = await mountList([row({ draft_name: "", draft_missing: true })]);
		const first = w.findAll(".row")[0];
		expect(first.find(".badge").text()).toBe("Draft deleted");
		expect(first.find(".badge").attributes("data-theme")).toBe("gray");
		expect(first.find("a").exists()).toBe(false);
	});

	it("links a review row without a draft to its approval", async () => {
		w = await mountList([
			row({
				status: "Needs Review",
				draft_doctype: "",
				draft_name: "",
				approval_request: "AR-1",
			}),
		]);
		expect(w.find(".row a").attributes("href")).toBe("/approvals/AR-1");
	});

	it("shows the empty copy", async () => {
		w = await mountList([]);
		expect(w.find(".empty").text()).toBe("Files dropped into a watched folder show up here.");
	});

	it("shows the retry button only when can_retry, and retries once", async () => {
		w = await mountList([
			row({ name: "F1", status: "Failed", can_retry: true }),
			row({ name: "F2", status: "Failed", can_retry: false }),
		]);
		const rows = w.findAll(".row");
		expect(rows[0].findAll(".btn").map((b) => b.text())).toEqual(["Retry"]);
		expect(rows[1].findAll(".btn")).toHaveLength(0);
		intakeRetry.mockResolvedValue({ ok: true, status: "Queued" });
		await rows[0].find(".btn").trigger("click");
		await flushPromises();
		expect(intakeRetry).toHaveBeenCalledWith("F1");
	});

	it("offers Mine / All only to managers and sends the scope", async () => {
		w = await mountList([row()], false);
		expect(w.find(".scope").exists()).toBe(false);
		w.unmount();
		w = await mountList([row()], true);
		expect(w.find(".scope").exists()).toBe(true);
		const sel = w.find(".scope");
		sel.element.value = "all";
		await sel.trigger("change");
		await flushPromises();
		expect(intakeListPage.mock.calls.at(-1)[0].scope).toBe("all");
	});

	it("polls every 10 s while a row is Queued or Running, then stops", async () => {
		w = await mountList([row({ status: "Running", draft_doctype: "", draft_name: "" })]);
		const calls = intakeListPage.mock.calls.length;
		await vi.advanceTimersByTimeAsync(10000);
		expect(intakeListPage.mock.calls.length).toBe(calls + 1);
		intakeListPage.mockResolvedValue({ rows: [row()], total: 1, has_more: false });
		await vi.advanceTimersByTimeAsync(10000);
		const settled = intakeListPage.mock.calls.length;
		await vi.advanceTimersByTimeAsync(30000);
		expect(intakeListPage.mock.calls.length).toBe(settled);
	});

	it("stops polling on unmount", async () => {
		w = await mountList([row({ status: "Queued", draft_doctype: "", draft_name: "" })]);
		w.unmount();
		const calls = intakeListPage.mock.calls.length;
		await vi.advanceTimersByTimeAsync(30000);
		expect(intakeListPage.mock.calls.length).toBe(calls);
		w = null;
	});
});
