import { vi, afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { scenario } from "../../../tests/payment-calendar/templateFixtures";

const GOLDEN = vi.hoisted(() => ({ scope: null }));
// An API double with a request log, serving the template's Nov 2026 dataset.
vi.mock("@/api/paymentCalendar", async () => {
	const { vi: mocker } = await import("vitest");
	const ok = (data) => ({ ok: true, data });
	const records = (tag) =>
		Array.from({ length: 8 }, (_, i) => ({
			id: `${tag}-${i}`,
			party_name: `Party ${tag} ${i}`,
			voucher_no: `INV-${tag}-${i}`,
			due_date: "2026-11-12",
			original: "20000.00",
			remaining: "18500.00",
			settled: "1500.00",
			settlement: "Partially paid",
		}));
	const reply = (tag, page = 1) =>
		ok({
			rows: records(tag),
			total: 8,
			total_remaining: "42048.00",
			page,
			has_more: page < 2,
		});
	return {
		getCapabilities: mocker.fn(async () =>
			ok({
				companies: [{ name: "Northstar", default_currency: "INR" }],
				accounts: GOLDEN.scope.accounts.map((name) => ({
					name,
					account_name: name,
					account_currency: "INR",
				})),
				settings: GOLDEN.floor,
				can_configure: true,
			})
		),
		getPreferences: mocker.fn(async () =>
			ok({ company: "Northstar", accounts: GOLDEN.scope.accounts })
		),
		setPreferences: mocker.fn(async (company, accounts) => ok({ company, accounts })),
		requestSnapshot: mocker.fn(async () =>
			ok({ snapshot_id: "a".repeat(32), state: "queued" })
		),
		getSnapshotStatus: mocker.fn(async () => ok(GOLDEN.status())),
		getDayDetail: mocker.fn(async (id, date, side, page, parties, sorting) =>
			reply(`${side}-${date}-${sorting?.sort}-${sorting?.order}`, page)
		),
		getBucketDetail: mocker.fn(async (id, bucket, side, page, parties, sorting) =>
			reply(`${side}-${bucket}-${sorting?.sort}-${sorting?.order}`, page)
		),
		getUpcoming: mocker.fn(async () => ok({ rows: [], total: 0 })),
		configureFloor: mocker.fn(async () => ok(GOLDEN.floor)),
	};
});
vi.mock("@/utils/datetime", () => ({ siteTimezone: () => "Asia/Kolkata" }));
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ mobile: false }) }));
vi.mock("@/theme", async () => {
	const { ref } = await import("vue");
	return { useJarvisTheme: () => ({ effectiveDark: ref(false), toggleTheme: () => {} }) };
});
vi.mock("vue-router", () => ({
	useRoute: () => ({ query: {} }),
	useRouter: () => ({ back: () => {}, push: () => {}, resolve: () => ({ fullPath: "" }) }),
}));
vi.mock("frappe-ui", () => ({
	Breadcrumbs: { template: "<nav />", props: ["items"] },
	FeatherIcon: { template: "<span />", props: ["name"] },
	Dialog: { props: ["modelValue"], template: '<div v-if="modelValue"><slot /></div>' },
}));
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: { template: '<header><slot name="left-header"/></header>' },
}));
import PaymentCalendarPage from "./PaymentCalendarPage.vue";
import CalendarGrid from "./CalendarGrid.vue";
import * as api from "@/api/paymentCalendar";
import { modernStatus } from "../../../tests/payment-calendar/headlineFixtures";
import { settleReads } from "../../../tests/payment-calendar/fixtures";

const $ = (selector) => document.body.querySelector(selector);

describe("Payment Calendar page: side ledger and full details", () => {
	let wrapper, host;
	beforeEach(() => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date("2026-11-11T05:12:30Z"));
		vi.clearAllMocks();
		const base = scenario("default");
		GOLDEN.scope = base.scope;
		GOLDEN.floor = base.floor;
		GOLDEN.status = () => {
			const status = modernStatus();
			status.scope.company = "Northstar";
			status.expires_at = "2026-11-11T05:17:00+00:00";
			return status;
		};
		Element.prototype.scrollIntoView = vi.fn();
		host = document.createElement("div");
		document.body.appendChild(host);
	});
	afterEach(() => {
		wrapper?.unmount();
		host.remove();
		vi.useRealTimers();
		delete Element.prototype.scrollIntoView;
		delete window.matchMedia;
	});
	async function render() {
		wrapper = mount(PaymentCalendarPage, { attachTo: host });
		await settleReads();
	}
	const dayReads = () => api.getDayDetail.mock.calls.length;

	describe("one detail read per click (AC1)", () => {
		it("reads exactly once for a date click and shows the rows in the side panel", async () => {
			await render();
			const before = dayReads(),
				buckets = api.getBucketDetail.mock.calls.length;
			await wrapper.get('[data-date="2026-11-12"]').trigger("click");
			await settleReads();
			expect(dayReads()).toBe(before + 1);
			expect(api.getBucketDetail.mock.calls.length).toBe(buckets);
			expect(api.getDayDetail.mock.calls.at(-1).slice(1, 3)).toEqual([
				"2026-11-12",
				"Payable",
			]);
			expect(wrapper.get("#ledger-heading").text()).toBe("Thursday, 12 Nov");
			expect(wrapper.findAll("#day-panel .ledger-row")).toHaveLength(5);
		});
		it("reads once, for the clicked side, when an amount line is clicked", async () => {
			await render();
			const before = dayReads();
			await wrapper
				.get('[data-cell-date="2026-11-12"] .amount-line.incoming')
				.trigger("click");
			await settleReads();
			expect(dayReads()).toBe(before + 1);
			expect(api.getDayDetail.mock.calls.at(-1)[2]).toBe("Receivable");
			expect(wrapper.get('[data-panel-side="Receivable"]').attributes("aria-pressed")).toBe(
				"true"
			);
		});
		it("keeps the side when the clicked date is not selectable, and makes no read", async () => {
			await render();
			const before = dayReads();
			wrapper.findComponent(CalendarGrid).vm.$emit("select", "2030-01-01", "Receivable");
			await settleReads();
			expect(dayReads()).toBe(before);
			expect(wrapper.get('[data-panel-side="Payable"]').attributes("aria-pressed")).toBe(
				"true"
			);
			expect(wrapper.get("#ledger-heading").text()).toBe("Wednesday, 11 Nov");
		});
		it("reads once for a side tab click", async () => {
			await render();
			const before = dayReads();
			await wrapper.get('[data-panel-side="Receivable"]').trigger("click");
			await settleReads();
			expect(dayReads()).toBe(before + 1);
		});
	});

	describe("Immediate dues", () => {
		it("sets the obligation scope, scrolls to the panel and focuses its heading", async () => {
			await render();
			await wrapper.get('#immediate-card [data-obligation="immediate"]').trigger("click");
			await settleReads();
			const call = api.getBucketDetail.mock.calls.at(-1);
			expect(call.slice(1, 3)).toEqual(["immediate", "Payable"]);
			expect(wrapper.get("#ledger-heading").text()).toBe(
				"Immediate dues · Overdue & undated"
			);
			expect(wrapper.get("#day-panel").text()).toContain("CURRENT OBLIGATIONS");
			expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({
				block: "start",
				behavior: "smooth",
			});
			expect(document.activeElement).toBe(wrapper.get("#ledger-heading").element);
		});
		it("scrolls without animation when the user prefers reduced motion", async () => {
			window.matchMedia = vi.fn(() => ({ matches: true }));
			await render();
			await wrapper.get('#immediate-card [data-obligation="immediate"]').trigger("click");
			expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({
				block: "start",
				behavior: "auto",
			});
		});
		it("goes back to the selected date, Payable side, with Back to date", async () => {
			await render();
			await wrapper.get('#immediate-card [data-obligation="immediate"]').trigger("click");
			await settleReads();
			await wrapper.get(".ledger-back").trigger("click");
			await settleReads();
			expect(wrapper.get("#ledger-heading").text()).toBe("Wednesday, 11 Nov");
			expect(api.getDayDetail.mock.calls.at(-1).slice(1, 3)).toEqual([
				"2026-11-11",
				"Payable",
			]);
		});
	});

	describe("full details", () => {
		it("opens the month scope from View totals, and Escape closes it", async () => {
			await render();
			wrapper.get("#month-flow-details").element.focus();
			await wrapper.get("#month-flow-details").trigger("click");
			await settleReads();
			expect(api.getBucketDetail.mock.calls.at(-1).slice(1, 3)).toEqual([
				"window",
				"Payable",
			]);
			expect($("[role=dialog] h2").textContent).toBe("November 2026 · Invoices");
			expect($("[role=dialog]").textContent).toContain("8 records · ₹42,048.00");
			document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
			await settleReads();
			expect($("[role=dialog]")).toBeNull();
			expect(document.activeElement).toBe(wrapper.get("#month-flow-details").element);
		});
		it("opens the panel's scope and side from View all and closes with its button", async () => {
			await render();
			await wrapper.get('[data-panel-side="Receivable"]').trigger("click");
			await settleReads();
			wrapper.get("#panel-view-all").element.focus();
			await wrapper.get("#panel-view-all").trigger("click");
			await settleReads();
			expect(api.getDayDetail.mock.calls.at(-1).slice(1, 3)).toEqual([
				"2026-11-11",
				"Receivable",
			]);
			expect($("[role=dialog] h2").textContent).toBe("Wednesday, 11 Nov");
			expect($("[data-modal-side=Receivable]").getAttribute("aria-pressed")).toBe("true");
			$('[aria-label="Close invoice details"]').click();
			await settleReads();
			expect($("[role=dialog]")).toBeNull();
			expect(document.activeElement).toBe(wrapper.get("#panel-view-all").element);
		});
		it("sorts the panel and the dialog together from a header button and toggles", async () => {
			await render();
			await wrapper.get("#panel-view-all").trigger("click");
			await settleReads();
			const reads = dayReads();
			$('[data-sort="party"]').click();
			await settleReads();
			// The panel and the dialog show the same page: one shared read for both.
			expect(dayReads()).toBe(reads + 1);
			expect(api.getDayDetail.mock.calls.at(-1)[5]).toEqual({ sort: "party", order: "asc" });
			expect($('thead th [data-sort="party"]').closest("th").getAttribute("aria-sort")).toBe(
				"ascending"
			);
			$('[data-sort="party"]').click();
			await settleReads();
			expect(api.getDayDetail.mock.calls.at(-1)[5]).toEqual({
				sort: "party",
				order: "desc",
			});
			expect($('[data-sort="party"]').closest("th").getAttribute("aria-sort")).toBe(
				"descending"
			);
			expect($('[data-sort="party"]').textContent.trim()).toBe("Party ↓");
		});
		it("pages through the composable", async () => {
			await render();
			await wrapper.get("#panel-view-all").trigger("click");
			await settleReads();
			$(".page-next").click();
			await settleReads();
			expect(api.getDayDetail.mock.calls.at(-1)[3]).toBe(2);
			expect($(".modal-foot").textContent).toContain("Page 2");
		});
		it("uses the sort select too, with the key's default direction", async () => {
			await render();
			await wrapper.get("#panel-view-all").trigger("click");
			await settleReads();
			const select = $("#record-sort");
			select.value = "date";
			select.dispatchEvent(new Event("change"));
			await settleReads();
			expect(api.getDayDetail.mock.calls.at(-1)[5]).toEqual({
				sort: "due_date",
				order: "asc",
			});
		});
	});

	it("shows no old detail table and no bucket dialog any more", async () => {
		await render();
		expect(wrapper.find(".pcal-tabs").exists()).toBe(false);
		expect(wrapper.find(".terms-table").exists()).toBe(false);
		expect($("[role=dialog]")).toBeNull();
	});
});
