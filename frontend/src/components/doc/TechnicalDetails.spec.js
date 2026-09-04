import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import TechnicalDetails from "./TechnicalDetails.vue";

// jarvis#1062 P0-2/P1-3 (production-readiness audit): TechnicalDetails is the
// shared "collapsed, labelled, monospace" renderer for extractTechnicalDetails()
// output, used by both a finding's expanded detail and the run coverage/warning
// banners. It wraps DocSection directly (no frappe-ui import), so it can be
// mounted for real here rather than stubbed.
describe("TechnicalDetails", () => {
	it("renders nothing at all - not even a header - when details is empty", () => {
		const w = mount(TechnicalDetails, { props: { details: [] } });
		expect(w.text()).toBe("");
		expect(w.find("button").exists()).toBe(false);
	});

	it("renders a 'Technical details' header and one labelled dt/dd pair per entry", () => {
		const w = mount(TechnicalDetails, {
			props: {
				details: [
					{ label: "Rule", value: "nsv-tieout-7d92" },
					{ label: "Field reference", value: "Warehouse.account" },
				],
			},
		});
		expect(w.text()).toContain("Technical details");
		const rows = w.findAll("dl > div");
		expect(rows).toHaveLength(2);
		expect(rows[0].find("dt").text()).toBe("Rule");
		expect(rows[0].find("dd").text()).toBe("nsv-tieout-7d92");
		expect(rows[1].find("dt").text()).toBe("Field reference");
		expect(rows[1].find("dd").text()).toBe("Warehouse.account");
	});

	it("is collapsed by default - the dl is present but not visible until the header is opened", () => {
		const w = mount(TechnicalDetails, {
			attachTo: document.body,
			props: { details: [{ label: "Rule", value: "nsv-grad-7d92" }] },
		});
		expect(w.find("dl").isVisible()).toBe(false);
		w.unmount();
	});
});
