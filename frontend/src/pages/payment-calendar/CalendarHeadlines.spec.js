import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import CalendarHeadlines from "./CalendarHeadlines.vue";
import { scenario } from "../../../tests/payment-calendar/templateFixtures";
import { modernStatus } from "../../../tests/payment-calendar/headlineFixtures";

const render = (snapshot) => mount(CalendarHeadlines, { props: { snapshot } });
const text = (wrapper, selector) => wrapper.get(selector).text();

describe("CalendarHeadlines: Lowest cushion", () => {
	it("shows the template's default values", () => {
		const wrapper = render(modernStatus());
		const card = wrapper.get("#lowest-card");
		expect(card.get("h2").text()).toBe("Lowest cushion");
		expect(card.attributes("data-state")).toBe("green");
		expect(text(wrapper, "#lowest-status")).toBe("Safe");
		expect(text(wrapper, "#lowest-cushion")).toBe("₹97,614.00");
		expect(text(wrapper, "#lowest-note")).toBe("Lowest dip on 23 Nov");
		expect(card.get(".headline-foot span").text()).toBe("11 Nov–10 Dec · 30 days");
		expect(text(wrapper, "#trough-jump")).toBe("View 23 Nov →");
		expect(card.text()).toContain("Assumes scheduled collections arrive on time");
	});

	it.each([
		["floor_140000", "amber", "Tight", "₹7,614.00", "Lowest dip on 23 Nov"],
		["floor_147614", "amber", "Tight", "₹0.00", "Lowest dip on 23 Nov"],
		["floor_150000", "red", "Below minimum", "−₹2,386.00", "Short of minimum on 23 Nov"],
		["floor_200000", "red", "Below minimum", "−₹52,386.00", "Short of minimum on 23 Nov"],
	])("%s reads %s / %s / %s", (name, state, pill, value, note) => {
		const wrapper = render(modernStatus(name));
		expect(wrapper.get("#lowest-card").attributes("data-state")).toBe(state);
		expect(text(wrapper, "#lowest-status")).toBe(pill);
		expect(text(wrapper, "#lowest-cushion")).toBe(value);
		expect(text(wrapper, "#lowest-note")).toBe(note);
		expect(text(wrapper, "#trough-jump")).toBe("View 23 Nov →");
	});

	it("takes the colour from the server state, not from the cushion", () => {
		const status = modernStatus();
		status.horizon.lowest.state = "tight";
		const wrapper = render(status);
		expect(wrapper.get("#lowest-card").attributes("data-state")).toBe("amber");
		expect(text(wrapper, "#lowest-status")).toBe("Tight");
	});

	it("emits jump with the trough date", async () => {
		const wrapper = render(modernStatus());
		await wrapper.get("#trough-jump").trigger("click");
		expect(wrapper.emitted("jump")).toEqual([["2026-11-23"]]);
	});

	it("is unavailable for an old-shape result, never ₹0", () => {
		const status = scenario("default");
		delete status.horizon;
		const wrapper = render(status);
		expect(wrapper.get("#lowest-card").attributes("data-state")).toBe("unavailable");
		expect(text(wrapper, "#lowest-cushion")).toBe("Unavailable");
		expect(text(wrapper, "#lowest-status")).toBe("Incomplete");
		expect(text(wrapper, "#lowest-note")).toBe("A complete 30-day forecast is required.");
		expect(text(wrapper, "#trough-jump")).toBe("View unavailable");
		expect(wrapper.get("#trough-jump").attributes("disabled")).toBeDefined();
		expect(wrapper.text()).not.toContain("₹0.00");
	});

	it("is unavailable when the server found no complete 30 days", () => {
		const status = modernStatus();
		status.horizon.lowest = null;
		status.horizon.forecast_available = false;
		const wrapper = render(status);
		expect(text(wrapper, "#lowest-cushion")).toBe("Unavailable");
		expect(text(wrapper, "#lowest-note")).toBe("A complete 30-day forecast is required.");
		expect(text(wrapper, ".headline-foot span")).toBe("11 Nov–10 Dec · 30 days");
	});

	it("asks for accounts when none are selected (golden accounts_none)", () => {
		const wrapper = render(modernStatus("accounts_none"));
		expect(wrapper.get("#lowest-card").attributes("data-state")).toBe("unavailable");
		expect(text(wrapper, "#lowest-note")).toBe("Select bank and cash accounts.");
		expect(wrapper.get("#trough-jump").attributes("disabled")).toBeDefined();
	});

	it("asks for accounts when the server sent no horizon at all", () => {
		const status = modernStatus();
		status.horizon = null;
		status.scope.accounts = [];
		const wrapper = render(status);
		expect(text(wrapper, "#lowest-note")).toBe("Select bank and cash accounts.");
		expect(text(wrapper, ".headline-foot span")).toBe("");
	});

	it("keeps the jump disabled and silent when unavailable", async () => {
		const wrapper = render(modernStatus("accounts_none"));
		await wrapper.get("#trough-jump").trigger("click");
		expect(wrapper.emitted("jump")).toBeUndefined();
	});
});

describe("CalendarHeadlines: Month net flow", () => {
	it("shows the template's default values", () => {
		const wrapper = render(modernStatus());
		const card = wrapper.get("#month-flow-card");
		expect(card.get("h2").text()).toBe("Month net flow");
		expect(card.attributes("data-state")).toBe("positive");
		expect(text(wrapper, "#month-flow-scope")).toBe("November 2026");
		expect(text(wrapper, "#month-flow")).toBe("+₹3,29,702.00");
		expect(text(wrapper, "#month-flow-note")).toBe("Surplus this month");
		expect(card.get(".headline-foot span").text()).toBe(
			"Expected inflows − scheduled outflows"
		);
		expect(text(wrapper, "#month-flow-details")).toBe("View totals →");
		expect(card.text()).toContain("Remaining balances · Not cash received");
	});

	it("reads a negative net from the server string", () => {
		const status = modernStatus();
		status.projection.month_totals.net = "-600898.00";
		const wrapper = render(status);
		expect(wrapper.get("#month-flow-card").attributes("data-state")).toBe("negative");
		expect(text(wrapper, "#month-flow")).toBe("−₹6,00,898.00");
		expect(text(wrapper, "#month-flow-note")).toBe("Net outflow this month");
	});

	it("says there are no scheduled payments, not 'match', for a month with no rows", () => {
		const status = modernStatus();
		Object.assign(status.projection.month_totals, {
			net: "0.00",
			payable: "0.00",
			receivable: "0.00",
			payable_rows: 0,
			receivable_rows: 0,
		});
		const wrapper = render(status);
		expect(wrapper.get("#month-flow-card").attributes("data-state")).toBe("neutral");
		expect(text(wrapper, "#month-flow")).toBe("₹0.00");
		expect(text(wrapper, "#month-flow-note")).toBe("No scheduled payments");
	});

	it("shows zero as neutral without a sign", () => {
		const status = modernStatus();
		status.projection.month_totals.net = "0.00";
		const wrapper = render(status);
		expect(wrapper.get("#month-flow-card").attributes("data-state")).toBe("neutral");
		expect(text(wrapper, "#month-flow")).toBe("₹0.00");
		expect(text(wrapper, "#month-flow-note")).toBe("Inflows match outflows");
	});

	it.each([
		["null month_totals (partial month)", (p) => (p.month_totals = null)],
		["missing month_totals (old shape)", (p) => delete p.month_totals],
		["null net", (p) => (p.month_totals.net = null)],
	])("is unavailable for %s", (_, mutate) => {
		const status = modernStatus();
		mutate(status.projection);
		const wrapper = render(status);
		expect(wrapper.get("#month-flow-card").attributes("data-state")).toBe("unavailable");
		expect(text(wrapper, "#month-flow")).toBe("Unavailable");
		expect(text(wrapper, "#month-flow-note")).toBe("Full-month data is not loaded.");
		expect(text(wrapper, "#month-flow-scope")).toBe("November 2026");
		expect(wrapper.get("#month-flow-details").attributes("disabled")).toBeDefined();
	});

	it("emits view-totals", async () => {
		const wrapper = render(modernStatus());
		await wrapper.get("#month-flow-details").trigger("click");
		expect(wrapper.emitted("view-totals")).toHaveLength(1);
	});
});

describe("CalendarHeadlines: Immediate dues", () => {
	it("shows the template's default values", () => {
		const wrapper = render(modernStatus());
		const card = wrapper.get("#immediate-card");
		expect(card.get("h2").text()).toBe("Immediate dues");
		expect(card.attributes("data-state")).toBe("amber");
		expect(text(wrapper, "#immediate-status")).toBe("Needs review");
		expect(text(wrapper, "#immediate-dues")).toBe("₹27,500.00");
		expect(card.text()).toContain("Overdue & undated supplier invoices");
		expect(card.get(".headline-foot span").text()).toBe("Excludes dues scheduled today");
		expect(card.get('[data-obligation="immediate"]').text()).toBe("View invoices →");
	});

	it("is Clear and green at zero", () => {
		const status = modernStatus();
		status.projection.due_now.immediate = "0.00";
		const wrapper = render(status);
		expect(wrapper.get("#immediate-card").attributes("data-state")).toBe("green");
		expect(text(wrapper, "#immediate-status")).toBe("Clear");
		expect(text(wrapper, "#immediate-dues")).toBe("₹0.00");
	});

	it("tells a missing value from an invalid one in data-cause, for support", () => {
		const status = modernStatus();
		status.projection.month_totals.net = null;
		status.projection.due_now.immediate = "-5.00";
		status.horizon.lowest = { ...status.horizon.lowest, cushion: "12abc" };
		const wrapper = render(status);
		expect(wrapper.get("#month-flow-card").attributes("data-cause")).toBe("missing");
		expect(wrapper.get("#immediate-card").attributes("data-cause")).toBe("invalid");
		expect(wrapper.get("#lowest-card").attributes("data-cause")).toBe("invalid");
	});

	it("carries no data-cause on a card that shows a value", () => {
		const wrapper = render(modernStatus());
		expect(wrapper.get("#month-flow-card").attributes("data-cause")).toBeUndefined();
	});

	it("is unavailable, not ₹0, when the field is missing", () => {
		const status = scenario("default");
		delete status.projection.due_now.immediate;
		const wrapper = render(status);
		expect(wrapper.get("#immediate-card").attributes("data-state")).toBe("unavailable");
		expect(text(wrapper, "#immediate-dues")).toBe("Unavailable");
		expect(wrapper.find("#immediate-status").exists()).toBe(false);
		expect(wrapper.get('[data-obligation="immediate"]').attributes("disabled")).toBeDefined();
	});

	it("emits obligation with immediate", async () => {
		const wrapper = render(modernStatus());
		await wrapper.get('[data-obligation="immediate"]').trigger("click");
		expect(wrapper.emitted("obligation")).toEqual([["immediate"]]);
	});
});

describe("CalendarHeadlines: loading frame", () => {
	it("shows Loading values… and no figures while there is no snapshot", () => {
		const frame = {
			snapshot_id: null,
			currency: "INR",
			scope: { accounts: [], month: "2026-11", anchor: "2026-11-11" },
			projection: { precision: 2, due_now: { immediate: null } },
		};
		const wrapper = render(frame);
		for (const id of ["#lowest-card", "#month-flow-card", "#immediate-card"]) {
			expect(wrapper.get(id).attributes("data-state")).toBe("unavailable");
			expect(wrapper.get(id).attributes("aria-busy")).toBe("true");
			expect(wrapper.get(id).text()).toContain("Loading values…");
			expect(wrapper.get(id).text()).not.toContain("₹");
		}
		expect(wrapper.get("#trough-jump").attributes("disabled")).toBeDefined();
		expect(wrapper.get("#month-flow-details").attributes("disabled")).toBeDefined();
		expect(wrapper.get('[data-obligation="immediate"]').attributes("disabled")).toBeDefined();
	});
});

describe("CalendarHeadlines: Month net flow across a month change", () => {
	const renderMonth = (snapshot, month) =>
		mount(CalendarHeadlines, { props: { snapshot, month } });

	it("never pairs one month's label with another month's totals", async () => {
		const wrapper = renderMonth(modernStatus(), "2026-12");
		const card = wrapper.get("#month-flow-card");
		expect(text(wrapper, "#month-flow-scope")).toBe("December 2026");
		expect(card.attributes("aria-busy")).toBe("true");
		expect(card.text()).toContain("Loading values…");
		expect(card.text()).not.toContain("₹3,29,702.00");
		expect(wrapper.get("#month-flow-details").attributes("disabled")).toBeDefined();
		await wrapper.setProps({ snapshot: modernStatus("month_dec") });
		expect(text(wrapper, "#month-flow-scope")).toBe("December 2026");
		expect(text(wrapper, "#month-flow")).toBe("−₹5,000.00");
		expect(wrapper.get("#month-flow-card").attributes("aria-busy")).toBeUndefined();
	});

	it("shows the values when the snapshot is the displayed month's", () => {
		const wrapper = renderMonth(modernStatus(), "2026-11");
		expect(text(wrapper, "#month-flow")).toBe("+₹3,29,702.00");
	});
});
