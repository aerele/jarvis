import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
	Badge: { name: "Badge", props: ["label", "theme", "variant"], template: `<span />` },
	Button: {
		name: "Button",
		props: ["label", "variant", "disabled", "loading"],
		emits: ["click"],
		template: `<button @click="$emit('click')">{{ label }}</button>`,
	},
	FeatherIcon: { name: "FeatherIcon", template: `<span />` },
}));

import PlanCard from "./PlanCard.vue";

// #10-e review (2026-08): "excl. GST" used to render unconditionally, which
// wrongly claimed an exemption for a 0-GST plan and for EVERY plan before
// get_plans starts sending gst_percent at all. It must track the plan's own
// gst_percent, exactly like the Review & pay card's Subtotal/GST/Total rows.
describe("PlanCard: excl. GST label tracks the plan's own gst_percent", () => {
	function cycleLine(w) {
		// The cycle text and the optional "· excl. GST" suffix share one line.
		return w.text();
	}

	it("is absent when gst_percent is undefined (pre-companion-PR get_plans row)", () => {
		const w = mount(PlanCard, {
			props: { plan: { plan_name: "Pro", price_inr: 3999, billing_cycle: "Monthly" } },
		});
		expect(cycleLine(w)).not.toContain("excl. GST");
	});

	it("is absent when gst_percent is 0", () => {
		const w = mount(PlanCard, {
			props: {
				plan: {
					plan_name: "Pro",
					price_inr: 3999,
					billing_cycle: "Monthly",
					gst_percent: 0,
				},
			},
		});
		expect(cycleLine(w)).not.toContain("excl. GST");
	});

	it("is present when gst_percent is a positive number", () => {
		const w = mount(PlanCard, {
			props: {
				plan: {
					plan_name: "Pro",
					price_inr: 3999,
					billing_cycle: "Monthly",
					gst_percent: 18,
				},
			},
		});
		expect(cycleLine(w)).toContain("excl. GST");
	});
});
