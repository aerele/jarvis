import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
	Badge: { template: "<span/>" },
	Button: { template: "<button/>" },
	Tooltip: { template: "<div><slot/></div>" },
}));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));

const storeDouble = {
	tickets: [],
	ticketsLoading: false,
	ticketsError: "",
	isAwaiting: (s) => s === "Replied" || s === "Resolved",
	isClosed: (s) => s === "Closed",
	loadTickets: vi.fn(),
	refreshAwaiting: vi.fn(),
};
vi.mock("@/stores/support", () => ({
	useSupportStore: () => storeDouble,
	badgeFor: () => ({ label: "Open", tone: "open", theme: "blue" }),
}));

import SupportListPage from "@/pages/support/SupportListPage.vue";

const ListPageStub = { props: ["rows", "total", "filters", "sort"], template: "<div/>" };
const opts = {
	global: {
		stubs: {
			ListPage: ListPageStub,
			SupportShell: { template: "<div><slot name='actions'/><slot/></div>" },
		},
	},
};

const ROWS = [
	{ name: "T-1", subject: "Invoice wrong", status: "Open", modified: "2026-07-20 10:00:00" },
	{ name: "T-2", subject: "Login fails", status: "Replied", modified: "2026-07-24 10:00:00" },
	{ name: "T-3", subject: "Refund", status: "Resolved", modified: "2026-07-22 10:00:00" },
	{ name: "T-4", subject: "Old thing", status: "Closed", modified: "2026-07-01 10:00:00" },
	{ name: "T-5", subject: "Paused thing", status: "Paused", modified: "2026-07-23 10:00:00" },
];

function mountList() {
	storeDouble.tickets = ROWS;
	return mount(SupportListPage, opts);
}
async function applyFilters(w, next) {
	w.findComponent(ListPageStub).vm.$emit("update:filters", next);
	await w.vm.$nextTick();
	return w
		.findComponent(ListPageStub)
		.props("rows")
		.map((r) => r.name);
}

describe("SupportListPage", () => {
	beforeEach(() => vi.clearAllMocks());

	it("defaults to most-recently-updated first, the house default sort", async () => {
		const w = mountList();
		expect(
			w
				.findComponent(ListPageStub)
				.props("rows")
				.map((r) => r.name)
		).toEqual(["T-2", "T-5", "T-3", "T-1", "T-4"]);
	});

	it("filters to the awaiting set — Replied AND Resolved", async () => {
		expect(await applyFilters(mountList(), { status: "awaiting" })).toEqual(["T-2", "T-3"]);
	});

	it("Open means neither closed nor awaiting, so an unknown status still appears", async () => {
		// Paused must land somewhere. If the catch-all breaks it vanishes from
		// every filter and the user simply cannot find their ticket.
		expect(await applyFilters(mountList(), { status: "open" })).toEqual(["T-5", "T-1"]);
	});

	it("searches subject and ticket id, case-insensitively", async () => {
		expect(await applyFilters(mountList(), { search: "REFUND" })).toEqual(["T-3"]);
		expect(await applyFilters(mountList(), { search: "t-4" })).toEqual(["T-4"]);
	});

	it("replaces the whole filters object rather than merging", async () => {
		// ListPage emits a complete object; treating it as a patch leaves a
		// cleared filter silently applied.
		const w = mountList();
		await applyFilters(w, { status: "closed" });
		expect(await applyFilters(w, {})).toHaveLength(5);
	});

	it("reports the FILTERED count as total, so the footer counter is honest", async () => {
		const w = mountList();
		await applyFilters(w, { status: "awaiting" });
		expect(w.findComponent(ListPageStub).props("total")).toBe(2);
	});
});
