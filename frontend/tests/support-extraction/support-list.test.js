import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { reactive } from "vue";

vi.mock("frappe-ui", () => ({
	Badge: { template: "<span/>" },
	Button: { template: "<button/>" },
	Tooltip: { template: "<div><slot/></div>" },
}));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));

// reactive(), not a plain object: the real store (stores/support.js) wraps its
// state the same way, and the refresh/paging tests below reassign
// storeDouble.tickets AFTER mount to simulate loadTickets() — that only
// triggers SupportListPage's `filtered` computed to recompute if the double
// is actually reactive.
const storeDouble = reactive({
	tickets: [],
	ticketsLoading: false,
	ticketsError: "",
	isAwaiting: (s) => s === "Replied" || s === "Resolved",
	isClosed: (s) => s === "Closed",
	loadTickets: vi.fn(),
	refreshAwaiting: vi.fn(),
});
// Mirrors the real badgeFor's grouping (stores/support.js) rather than
// stubbing a constant label — the sort-by-status test below needs the mock
// to actually distinguish statuses, same as production does.
vi.mock("@/stores/support", () => ({
	useSupportStore: () => storeDouble,
	badgeFor: (status) => {
		if (status === "Replied" || status === "Resolved")
			return { label: "Awaiting you", theme: "orange" };
		if (status === "Closed") return { label: "Closed", theme: "gray" };
		return { label: "Open", theme: "blue" };
	},
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

// A page-length-sized fixture set for the paging tests below — every ROWS-based
// test above has only 5 rows against a page length of 20, which makes the
// `.slice(0, shown.value)` in `rows` a no-op no matter what it slices to.
function makeRows(n) {
	return Array.from({ length: n }, (_, i) => ({
		name: `P-${i}`,
		subject: `Ticket ${i}`,
		status: "Open",
		modified: "2026-07-20 10:00:00",
	}));
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
		expect(await applyFilters(w, { status: "closed" })).toEqual(["T-4"]);
		expect(await applyFilters(w, {})).toHaveLength(5);
	});

	it("reports the FILTERED count as total, so the footer counter is honest", async () => {
		const w = mountList();
		await applyFilters(w, { status: "awaiting" });
		expect(w.findComponent(ListPageStub).props("total")).toBe(2);
	});

	it("sorts Status by the displayed badge label, not the raw Helpdesk value", async () => {
		// Ascending by badge label groups alphabetically as "Awaiting you" (T-2,
		// T-3) < "Closed" (T-4) < "Open" (T-1, T-5 — the Paused catch-all lands
		// here too). A raw-value sort would instead give
		// Closed(T-4) < Open(T-1) < Paused(T-5) < Replied(T-2) < Resolved(T-3),
		// splitting the two "Open"-labelled rows (T-1, T-5) around T-2/T-3.
		const w = mountList();
		w.findComponent(ListPageStub).vm.$emit("update:sort", { field: "status", dir: "asc" });
		await w.vm.$nextTick();
		expect(
			w
				.findComponent(ListPageStub)
				.props("rows")
				.map((r) => r.name)
		).toEqual(["T-2", "T-3", "T-4", "T-1", "T-5"]);
	});

	it("pages: 20 rows on load, load-more to 40, a page-length switch to 50", async () => {
		storeDouble.tickets = makeRows(60);
		const w = mount(SupportListPage, opts);
		await w.vm.$nextTick();
		expect(w.findComponent(ListPageStub).props("rows")).toHaveLength(20);

		w.findComponent(ListPageStub).vm.$emit("load-more");
		await w.vm.$nextTick();
		expect(w.findComponent(ListPageStub).props("rows")).toHaveLength(40);

		w.findComponent(ListPageStub).vm.$emit("update:page-length", 50);
		await w.vm.$nextTick();
		expect(w.findComponent(ListPageStub).props("rows")).toHaveLength(50);
	});

	it("a refresh (store.tickets reassigned) does not collapse pagination back to one page", async () => {
		// loadTickets() always assigns a fresh array to store.tickets, even when
		// the filters/sort and the data are otherwise unchanged. Regression guard
		// for watching `filtered` directly, which recomputes on every such
		// reassignment and snapped `shown` back to pageLength on every Refresh.
		storeDouble.tickets = makeRows(60);
		const w = mount(SupportListPage, opts);
		await w.vm.$nextTick();

		w.findComponent(ListPageStub).vm.$emit("load-more");
		await w.vm.$nextTick();
		expect(w.findComponent(ListPageStub).props("rows")).toHaveLength(40);

		storeDouble.tickets = makeRows(60); // simulates store.loadTickets()'s refresh
		await w.vm.$nextTick();
		expect(w.findComponent(ListPageStub).props("rows")).toHaveLength(40);
	});
});
