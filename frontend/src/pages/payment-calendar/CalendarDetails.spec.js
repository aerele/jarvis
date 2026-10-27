import { mount } from "@vue/test-utils";
import CalendarDetails from "./CalendarDetails.vue";
import { complete, detail } from "../../../tests/payment-calendar/fixtures";

describe("Payment Calendar authorized detail", () => {
	it("paginates complete party totals independently and names documents accurately", async () => {
		const wrapper = mount(CalendarDetails, {
			props: {
				snapshot: complete(),
				data: detail(),
				view: "payees",
				selected: "2026-10-24",
			},
		});
		expect(wrapper.text()).toContain("Documents");
		expect(wrapper.text()).toContain("Rows");
		await wrapper.get('[aria-label="Next payees page"]').trigger("click");
		expect(wrapper.emitted("page")).toEqual([["parties", 2]]);
		wrapper.unmount();
	});
	it("shows allocations, safe source links and escaped party names", () => {
		const data = detail();
		data.rows[0].party_name = "<img src=x onerror=alert(1)>";
		data.rows[0].source_link = "javascript:alert(1)";
		const wrapper = mount(CalendarDetails, {
			props: { snapshot: complete(), data, view: "terms", selected: "2026-10-24" },
		});
		expect(wrapper.find("img").exists()).toBe(false);
		expect(wrapper.find("a").exists()).toBe(false);
		for (const label of ["Original", "Paid", "Credit", "Discount", "Remaining"])
			expect(wrapper.text()).toContain(label);
		wrapper.unmount();
	});
	it("does not show an unavailable receivables scope as no collections", () => {
		const snapshot = complete();
		snapshot.unavailable.Receivable = "forbidden";
		const wrapper = mount(CalendarDetails, {
			props: { snapshot, view: "customers", selected: "2026-10-24" },
		});
		expect(wrapper.text()).toContain("unavailable");
		expect(wrapper.text()).not.toContain("No customer");
		wrapper.unmount();
	});
});
