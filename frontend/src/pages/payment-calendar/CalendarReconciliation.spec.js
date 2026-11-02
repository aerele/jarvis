import { mount } from "@vue/test-utils";
import CalendarReconciliation from "./CalendarReconciliation.vue";
import CalendarDetails from "./CalendarDetails.vue";
import { complete, detail } from "../../../tests/payment-calendar/fixtures";

describe("Compact balance details", () => {
	it("keeps balance figures and drilldowns without implementation paragraphs", async () => {
		const snapshot = complete();
		snapshot.projection.source_notes = {
			Payable: { invoice_level: { count: 2 } },
			Receivable: { currency_residue_excluded: { count: 6, amount: "0.04" } },
		};
		const wrapper = mount(CalendarReconciliation, { props: { snapshot } });
		expect(wrapper.text()).not.toContain("What the forecast includes");
		expect(wrapper.text()).not.toContain("Every bucket reconciles");
		expect(wrapper.text()).not.toContain("invoice-level balance");
		expect(wrapper.text()).not.toContain("settled foreign-currency invoices");
		expect(wrapper.text()).toContain("Balance details");
		expect(wrapper.text()).toContain("Total outstanding");
		expect(wrapper.text()).toContain("Exchange differences");
		expect(wrapper.text()).toContain("0.04");
		await wrapper.get('[aria-label="Supplier Overdue records"]').trigger("click");
		expect(wrapper.emitted("bucket")).toEqual([["Payable", "overdue"]]);
		wrapper.unmount();
	});
	it("omits exchange differences when none are supplied", () => {
		const wrapper = mount(CalendarReconciliation, { props: { snapshot: complete() } });
		expect(wrapper.text()).not.toContain("Exchange differences");
		expect(wrapper.attributes("aria-label")).toBe("Outstanding balances");
		wrapper.unmount();
	});
	it.each([
		["withholding", "Tax withholding"],
		["foreign_advance", "Advance adjustment"],
		["unrecognized", "Invoice balance"],
	])("uses a short adjustment label for %s without inventing paid cash", (reason, label) => {
		const data = detail();
		Object.assign(data.rows[0], {
			allocation_basis: "invoice",
			allocation_reason: reason,
			paid: null,
			credit: null,
			discount: null,
			settlement: "Partially settled",
		});
		const wrapper = mount(CalendarDetails, {
			props: { snapshot: complete(), data, view: "terms", selected: "2026-10-24" },
		});
		expect(wrapper.text()).toContain(label);
		expect(wrapper.text()).not.toContain("payment-term breakdown unavailable");
		expect(wrapper.text()).not.toContain("NaN");
		wrapper.unmount();
	});
});
