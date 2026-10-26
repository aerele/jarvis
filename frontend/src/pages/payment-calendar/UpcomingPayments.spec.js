import { mount } from "@vue/test-utils";
import UpcomingPayments from "./UpcomingPayments.vue";
import { complete } from "../../../tests/payment-calendar/fixtures";

describe("Upcoming payment date selection", () => {
	it("allows selectable dates and keeps carry-window dates informational", async () => {
		const snapshot = complete();
		const wrapper = mount(UpcomingPayments, {
			props: {
				snapshot,
				selected: "2027-09-30",
				rows: [
					{ date: "2027-09-30", remaining: "100.00", payees: 2, rows: 3 },
					{ date: "2027-10-10", remaining: "200.00", payees: 1, rows: 1 },
				],
			},
		});
		await wrapper.get('[data-upcoming-date="2027-09-30"] button').trigger("click");
		expect(wrapper.emitted("select")).toEqual([["2027-09-30"]]);
		const lookahead = wrapper.get('[data-upcoming-date="2027-10-10"]');
		expect(lookahead.find("button").exists()).toBe(false);
		expect(lookahead.text()).toContain("Beyond calendar range");
		wrapper.unmount();
	});
	it("shows unavailable separately from an empty list", () => {
		const snapshot = complete();
		snapshot.unavailable.Payable = "reconciliation_mismatch";
		const wrapper = mount(UpcomingPayments, {
			props: { snapshot, selected: "2026-10-24", rows: [] },
		});
		expect(wrapper.text()).toContain("unavailable");
		expect(wrapper.text()).not.toContain("No upcoming payments");
		wrapper.unmount();
	});
});
