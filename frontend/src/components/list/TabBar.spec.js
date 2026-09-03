import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

// frappe-ui's ESM entry does not resolve under vitest (see LlmPoolEditor.spec.js
// and every other spec in this app that imports from "frappe-ui").
vi.mock("frappe-ui", () => ({
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant", "size"],
		template: `<span class="badge">{{ label }}</span>`,
	},
}));

import TabBar from "./TabBar.vue";

// jarvis#1062 P1-5 (production-readiness audit): a raw <button>, not
// frappe-ui's <Button>, so it needs its own focus-visible ring - keyboard
// tabbing through these tabs was otherwise invisible.
describe("TabBar keyboard focus (jarvis#1062 P1-5)", () => {
	it("every tab carries a focus-visible ring", () => {
		const w = mount(TabBar, {
			props: {
				tabs: [
					{ label: "Featured", value: "featured" },
					{ label: "Available", value: "available" },
				],
				modelValue: "featured",
			},
		});
		const tabs = w.findAll('[role="tab"]');
		expect(tabs).toHaveLength(2);
		for (const tab of tabs) {
			expect(tab.classes()).toContain("focus-visible:ring-2");
			expect(tab.classes()).toContain("focus-visible:ring-outline-gray-3");
		}
	});
});
