import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// A document whose approval sheet is being applied reads "Applying", links to the
// sheet on the board, and keeps the list polling until it moves on.
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
	Autocomplete: { template: "<div/>" },
	Badge: { props: ["label", "theme"], template: '<span class="badge">{{ label }}</span>' },
	Button: { props: ["label"], template: "<button>{{ label }}</button>" },
	Dropdown: { template: "<div/>" },
	FeatherIcon: { template: "<i/>" },
	Tooltip: { template: "<span><slot/></span>" },
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
	confirmDialog: vi.fn(),
}));
vi.mock("@/components/list/ListPage.vue", () => ({
	default: {
		props: ["rows"],
		template: `<div><div v-for="row in rows" :key="row.name" class="row"><slot name="cell-status" :row="row" /><slot name="cell-result" :row="row" /></div></div>`,
	},
}));
vi.mock("@/components/FilePreview.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ refreshApprovalsCount: vi.fn() }) }));
vi.mock("@/utils/datetime", () => ({ timeAgo: () => "now", exactDate: () => "" }));
vi.mock("@/branding", () => ({ agentName: "Jarvis" }));
vi.mock("@/api", () => ({
	fileboxListPage: vi.fn(),
	listCustomSkills: vi.fn(async () => []),
	getListFilterSchema: vi.fn(),
	getListFilterCapabilities: vi.fn(),
}));

import * as api from "@/api";
import FilesList from "./FilesList.vue";

const applying = {
	name: "conv-1",
	title: "loreal.pdf",
	status: "applying",
	result: "Applying the approval sheet… 40/250",
	result_link: "/approvals?held=PA-S1",
};

async function mountList(rows) {
	api.fileboxListPage.mockResolvedValue({ rows, total: rows.length, has_more: false });
	const w = mount(FilesList, {
		global: {
			stubs: { "router-link": { props: ["to"], template: '<a :href="to"><slot /></a>' } },
		},
	});
	await flushPromises();
	return w;
}

describe("FilesList: a sheet being applied", () => {
	let w;
	beforeEach(() => {
		vi.clearAllMocks();
		vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
	});
	afterEach(() => {
		w && w.unmount();
		vi.useRealTimers();
	});

	it("reads Applying and links its progress line to the sheet", async () => {
		w = await mountList([applying]);
		expect(w.find(".badge").text()).toBe("Applying");
		const link = w.find("a");
		expect(link.attributes("href")).toBe("/approvals?held=PA-S1");
		expect(link.text()).toBe("Applying the approval sheet… 40/250");
	});

	it("keeps polling while it applies, and stops once nothing moves", async () => {
		w = await mountList([applying]);
		const calls = api.fileboxListPage.mock.calls.length;
		await vi.advanceTimersByTimeAsync(5000);
		expect(api.fileboxListPage.mock.calls.length).toBe(calls + 1);
		w.unmount();
		w = await mountList([{ ...applying, status: "draft_created", result_link: "/app/x/1" }]);
		const settled = api.fileboxListPage.mock.calls.length;
		await vi.advanceTimersByTimeAsync(15000);
		expect(api.fileboxListPage.mock.calls.length).toBe(settled);
	});
});
