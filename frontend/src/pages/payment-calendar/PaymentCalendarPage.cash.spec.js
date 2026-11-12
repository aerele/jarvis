import { vi, afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { scenario } from "../../../tests/payment-calendar/templateFixtures";

const GOLDEN = vi.hoisted(() => ({ scope: null, name: "default", upcoming: [], hold: null }));
// An API double serving one golden scenario of the template's Nov 2026 dataset.
vi.mock("@/api/paymentCalendar", async () => {
	const { vi: mocker } = await import("vitest");
	const ok = (data) => ({ ok: true, data });
	const page = { rows: [], total: 0, total_remaining: "0.00", page: 1, has_more: false };
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
		getSnapshotStatus: mocker.fn(async () => {
			await GOLDEN.hold;
			return ok(GOLDEN.status());
		}),
		getDayDetail: mocker.fn(async () => ok(page)),
		getBucketDetail: mocker.fn(async () => ok(page)),
		getUpcoming: mocker.fn(async () => ok({ rows: GOLDEN.upcoming, total: 0 })),
		configureFloor: mocker.fn(async () => ok(GOLDEN.floor)),
		requestAccountBalances: mocker.fn(async () => ok({ job_id: "job", state: "queued" })),
		getAccountBalancesStatus: mocker.fn(async () => ({ ok: false, code: "not_found" })),
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
import CalendarFilterBar from "./CalendarFilterBar.vue";
import * as api from "@/api/paymentCalendar";
import { modernStatus } from "../../../tests/payment-calendar/headlineFixtures";
import { settleReads } from "../../../tests/payment-calendar/fixtures";

const $ = (selector) => document.body.querySelector(selector);
const UPCOMING = [
	{ date: "2026-11-11", remaining: "42048.00", payees: 3, rows: 3 },
	{ date: "2026-11-12", remaining: "6994.00", payees: 1, rows: 1 },
	{ date: "2026-11-14", remaining: "15000.00", payees: 2, rows: 2 },
];

describe("Payment Calendar page: cash details, next payments, method, toast", () => {
	let wrapper, host;
	beforeEach(() => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date("2026-11-11T05:12:30Z"));
		vi.clearAllMocks();
		useScenario("default");
		GOLDEN.upcoming = UPCOMING;
		Element.prototype.scrollIntoView = vi.fn();
		host = document.createElement("div");
		document.body.appendChild(host);
	});
	afterEach(() => {
		wrapper?.unmount();
		host.remove();
		vi.useRealTimers();
		delete Element.prototype.scrollIntoView;
	});
	function useScenario(name) {
		const base = scenario(name);
		GOLDEN.scope = base.scope;
		GOLDEN.floor = base.floor;
		GOLDEN.status = () => {
			const status = modernStatus(name);
			status.scope.company = "Northstar";
			status.expires_at = "2026-11-11T05:17:00+00:00";
			return status;
		};
	}
	async function render() {
		wrapper = mount(PaymentCalendarPage, { attachTo: host });
		await settleReads();
	}

	describe("layout", () => {
		it("puts the calendar and Next payments in the left column, the ledger and method in the aside", async () => {
			await render();
			const column = wrapper.get(".pcal-calendar-column");
			expect(column.find(".pcal-cal").exists()).toBe(true);
			expect(column.find("#upcoming-month").exists()).toBe(true);
			const aside = wrapper.get(".pcal-aside");
			expect(aside.find("#day-panel").exists()).toBe(true);
			expect(aside.find("details.method").exists()).toBe(true);
			expect(aside.find("#upcoming-month").exists()).toBe(false);
		});

		it("renders the details closed, after the book cash row", async () => {
			await render();
			const details = wrapper.get("details#financial-details");
			expect(details.attributes("open")).toBeUndefined();
			expect(wrapper.get("#stat-closing").text()).toBe("₹2,63,552.00");
			expect(wrapper.get("#closing-note").text()).toBe(
				"Assumes ₹86,250.00 is collected today."
			);
		});
	});

	describe("cash details actions", () => {
		it("opens the account balances dialog from Book cash now", async () => {
			await render();
			await wrapper.get("#book-cash").trigger("click");
			await settleReads();
			expect($(".pcal-modal h2").textContent).toBe("Bank & cash account balances");
		});

		it("opens balance details and the calculation example", async () => {
			await render();
			await wrapper.get("#balances-button").trigger("click");
			await settleReads();
			expect($(".pcal-modal h2").textContent).toBe("Outstanding balance details");
			$(".pcal-modal .pcal-icon-btn").click();
			await settleReads();
			await wrapper.get("#snapshot-note").trigger("click");
			await settleReads();
			expect($(".pcal-modal h2").textContent).toBe("Forecast calculation · 11 Nov");
			expect($(".pcal-modal").textContent).toContain("₹2,63,552.00");
			expect($(".pcal-modal").textContent).toContain("₹2,13,552.00");
		});

		it("writes the bottom note", async () => {
			await render();
			expect(wrapper.get(".bottom-note").text()).toBe(
				"Amounts are remaining balances as of the snapshot. View calculation example"
			);
		});

		it("shows an obligation scope in the side panel", async () => {
			await render();
			await wrapper.get('#financial-details [data-obligation="overdue"]').trigger("click");
			await settleReads();
			expect(api.getBucketDetail.mock.calls.at(-1)[1]).toBe("overdue");
		});

		it("shows the selected date's invoices in day scope and the month modal in month scope", async () => {
			await render();
			const reads = api.getDayDetail.mock.calls.length;
			await wrapper.get("#stat-receivable").trigger("click");
			await settleReads();
			expect(
				wrapper.get('#day-panel [data-panel-side="Receivable"]').attributes("aria-pressed")
			).toBe("true");
			expect(api.getDayDetail.mock.calls.length).toBeGreaterThan(reads);
			await wrapper.get("#scope-month").trigger("click");
			await wrapper.get("#selected-invoices").trigger("click");
			await settleReads();
			expect(api.getBucketDetail.mock.calls.at(-1)[1]).toBe("window");
			expect($(".pcal-modal")).not.toBeNull();
		});
	});

	describe("Today", () => {
		it("returns the cash details to the day view and selects the anchor date", async () => {
			await render();
			await wrapper.get("#scope-month").trigger("click");
			expect(wrapper.get("#scope-month").attributes("aria-pressed")).toBe("true");
			await wrapper.get('[data-date="2026-11-18"]').trigger("click");
			await wrapper.get("#today-button").trigger("click");
			await settleReads();
			expect(wrapper.get("#scope-day").attributes("aria-pressed")).toBe("true");
			expect(wrapper.get("#selected-date-heading").text()).toContain("11 Nov");
		});
	});

	describe("details stay open", () => {
		const details = () => wrapper.get("#financial-details").element;
		async function openDetails() {
			details().open = true;
			details().dispatchEvent(new Event("toggle"));
			await settleReads();
		}
		it("keeps the user's open state across a floor save and its snapshot refresh", async () => {
			await render();
			await openDetails();
			await wrapper.get("#floor-input").setValue("2,00,000");
			await wrapper.get("#floor-save").trigger("click");
			await settleReads();
			await vi.advanceTimersByTimeAsync(5000);
			expect(wrapper.find("#financial-details").exists()).toBe(true);
			expect(details().open).toBe(true);
		});
		it("stays closed across a floor save once the user closed it", async () => {
			await render();
			await openDetails();
			details().open = false;
			details().dispatchEvent(new Event("toggle"));
			await wrapper.get("#floor-input").setValue("2,00,000");
			await wrapper.get("#floor-save").trigger("click");
			await settleReads();
			await vi.advanceTimersByTimeAsync(5000);
			expect(details().open).toBe(false);
		});
	});

	describe("scrolling the side panel into view", () => {
		const lastBlock = () => Element.prototype.scrollIntoView.mock.calls.at(-1)[0].block;

		it("moves the date's invoices the least (nearest)", async () => {
			await render();
			await wrapper.get("#selected-invoices").trigger("click");
			await settleReads();
			expect(lastBlock()).toBe("nearest");
		});

		it("brings an obligation to the top (start), from the cash details and the KPI card", async () => {
			await render();
			await wrapper.get('#financial-details [data-obligation="overdue"]').trigger("click");
			await settleReads();
			expect(lastBlock()).toBe("start");
			await wrapper.get("#stat-payable").trigger("click");
			await settleReads();
			expect(lastBlock()).toBe("nearest");
			await wrapper.get('#immediate-card [data-obligation="immediate"]').trigger("click");
			await settleReads();
			expect(lastBlock()).toBe("start");
		});
	});

	describe("browsing to another month", () => {
		it("shows loading, not the old month's figures, until the new snapshot arrives", async () => {
			await render();
			expect(wrapper.get("#stat-closing").text()).toBe("₹2,63,552.00");
			let release;
			GOLDEN.hold = new Promise((resolve) => (release = resolve));
			GOLDEN.status = () => {
				const status = modernStatus("month_dec");
				status.scope.company = "Northstar";
				status.expires_at = "2026-11-11T05:17:00+00:00";
				return status;
			};
			await wrapper.get("#next-month").trigger("click");
			await settleReads();
			expect(wrapper.get("#month-flow-scope").text()).toBe("December 2026");
			expect(wrapper.get("#month-flow-card").text()).toContain("Loading values…");
			expect(wrapper.get("#closing-note").text()).toBe("Loading values…");
			expect(wrapper.get("#stat-closing").text()).toBe("");
			expect(wrapper.get(".pcal-next .schedule-empty").text()).toBe(
				"Loading upcoming payments…"
			);
			GOLDEN.hold = null;
			release();
			await settleReads();
			expect(wrapper.get("#month-flow").text()).toBe("−₹5,000.00");
			expect(wrapper.get("#closing-note").text()).toMatch(/^Assumes ₹/);
			GOLDEN.hold = null;
		});
	});

	describe("risk alert", () => {
		it("jumps to the first date below the minimum, in day scope, and focuses its cell", async () => {
			useScenario("floor_200000");
			await render();
			await wrapper.get("#scope-month").trigger("click");
			expect(wrapper.get("#risk-alert").text()).toContain(
				"Cash balance falls below the minimum on 19 Nov."
			);
			await wrapper.get("#risk-jump").trigger("click");
			await settleReads();
			expect(wrapper.get("#scope-day").attributes("aria-pressed")).toBe("true");
			expect(wrapper.get("#selected-date-heading").text()).toBe("Thursday, 19 Nov");
			expect(document.activeElement.getAttribute("data-date")).toBe("2026-11-19");
		});

		it("is not shown at the default minimum", async () => {
			await render();
			expect(wrapper.find("#risk-alert").exists()).toBe(false);
		});
	});

	describe("Next payments", () => {
		it("lists the dates after the selected one and selects a row's date", async () => {
			await render();
			const rows = wrapper
				.findAll("[data-upcoming]")
				.map((n) => n.attributes("data-upcoming"));
			expect(rows).toEqual(["2026-11-12", "2026-11-14"]);
			await wrapper.get('[data-upcoming="2026-11-14"]').trigger("click");
			await settleReads();
			expect(wrapper.get("#selected-date-heading").text()).toBe("Saturday, 14 Nov");
			expect(api.getDayDetail.mock.calls.at(-1).slice(1, 3)).toEqual([
				"2026-11-14",
				"Payable",
			]);
		});

		it("switches the calendar to List with View remaining dates", async () => {
			await render();
			expect(wrapper.find(".list-view").exists()).toBe(false);
			await wrapper.get("#all-payments").trigger("click");
			await settleReads();
			expect(wrapper.find(".list-view").exists()).toBe(true);
			// a second click after the person chose Month again lists again
			await wrapper.get("#all-payments").trigger("click");
			expect(wrapper.find(".list-view").exists()).toBe(true);
		});
	});

	describe("toast", () => {
		it("confirms a saved minimum, then hides after 3.5 seconds", async () => {
			await render();
			const toast = () => $(".pcal-toast");
			expect(toast().hidden).toBe(true);
			await wrapper.get("#floor-input").setValue("300000");
			await wrapper.get("#floor-save").trigger("click");
			await settleReads();
			expect(api.configureFloor).toHaveBeenCalled();
			expect(toast().hidden).toBe(false);
			expect(toast().textContent).toBe("Minimum cash balance saved.");
			vi.advanceTimersByTime(3500);
			await settleReads();
			expect(toast().hidden).toBe(true);
		});

		it("confirms a company change", async () => {
			await render();
			wrapper.findComponent(CalendarFilterBar).vm.$emit("choose-company", "Other Co");
			await settleReads();
			expect($(".pcal-toast").textContent).toBe("Company changed.");
		});

		it("does not announce choosing the company already shown", async () => {
			await render();
			wrapper.findComponent(CalendarFilterBar).vm.$emit("choose-company", "Northstar");
			await settleReads();
			expect($(".pcal-toast").hidden).toBe(true);
		});
	});
});
