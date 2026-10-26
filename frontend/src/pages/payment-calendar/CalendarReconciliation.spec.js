import { mount } from "@vue/test-utils";
import CalendarReconciliation from "./CalendarReconciliation.vue";
import CalendarDetails from "./CalendarDetails.vue";
import { complete, detail } from "../../../tests/payment-calendar/fixtures";

describe("Disclosed ERPNext reconciliation adjustments", () => {
	it("names invoice-level fallbacks and excluded FX without presenting them as incoming cash", () => {
		const snapshot = complete();
		snapshot.projection.source_notes = {
			Payable: { invoice_level: { count: 2 } },
			Receivable: { currency_residue_excluded: { count: 6, amount: "0.04" } },
		};
		const wrapper = mount(CalendarReconciliation, { props: { snapshot } });
		expect(wrapper.text()).toContain("2 supplier invoices");
		expect(wrapper.text()).toContain("invoice-level");
		expect(wrapper.text()).toContain("6 settled foreign-currency invoices");
		expect(wrapper.text()).toContain("excluded");
		expect(wrapper.text()).toContain("cash colours");
		wrapper.unmount();
	});
	it("makes the missing payment-term breakdown clear without inventing paid cash", () => {
		const data = detail();
		Object.assign(data.rows[0], {
			allocation_basis: "invoice",
			allocation_reason: "withholding",
			paid: null,
			credit: null,
			discount: null,
			settlement: "Partially settled",
		});
		const wrapper = mount(CalendarDetails, {
			props: { snapshot: complete(), data, view: "terms", selected: "2026-10-24" },
		});
		expect(wrapper.text()).toContain("Invoice-level balance");
		expect(wrapper.text()).toContain("payment-term breakdown unavailable");
		expect(wrapper.text()).not.toContain("NaN");
		wrapper.unmount();
	});
});
