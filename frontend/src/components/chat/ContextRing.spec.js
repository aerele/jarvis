import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
	Popover: {
		props: ["placement"],
		template:
			"<div><slot name='target' :togglePopover='() => {}'/><slot name='body' :close='() => {}'/></div>",
	},
	Button: {
		props: ["label", "variant", "size", "disabled"],
		emits: ["click"],
		template: "<button :disabled='disabled' @click=\"$emit('click')\">{{ label }}</button>",
	},
}));

import ContextRing from "./ContextRing.vue";

const ctx = (over = {}) => ({
	used: 42000,
	capacity: 200000,
	pct: 21,
	warn_pct: 80,
	auto_compact_pct: 90,
	route: "fits",
	compaction_count: 0,
	last_compacted_at: null,
	last_in: 559,
	last_out: 5,
	model: "gpt-5.5",
	compacting: false,
	fresh: true,
	...over,
});

describe("ContextRing", () => {
	it("is hidden until measured", () => {
		const w = mount(ContextRing, { props: { context: ctx({ fresh: false }) } });
		expect(w.find("[data-testid=context-ring]").exists()).toBe(false);
	});

	it("accessible name leads with used of capacity", () => {
		const w = mount(ContextRing, { props: { context: ctx() } });
		expect(w.find("[data-testid=context-ring]").attributes("aria-label")).toMatch(
			/^42k of 200k/
		);
	});

	it("turns warn at 84 percent", () => {
		const w = mount(ContextRing, { props: { context: ctx({ used: 168000, pct: 84 }) } });
		expect(w.find("[data-testid=context-ring]").classes()).toContain("jv-ctx-warn");
	});

	it("shows the busy class while compacting", () => {
		const w = mount(ContextRing, { props: { context: ctx(), compacting: true } });
		expect(w.find("[data-testid=context-ring]").classes()).toContain("jv-ctx-busy");
	});

	it("shows the done class once compacted", () => {
		const w = mount(ContextRing, { props: { context: ctx(), compacted: true } });
		expect(w.find("[data-testid=context-ring]").classes()).toContain("jv-ctx-done");
	});

	it("shows auto-compact and last-reply rows in the popover", () => {
		const w = mount(ContextRing, { props: { context: ctx() } });
		expect(w.text()).toContain("Auto-compacts at");
		expect(w.text()).toContain("180k (90%)");
		expect(w.text()).toContain("Last reply");
		expect(w.text()).toContain("559 in · 5 out");
	});

	it("disables the popover's Compact button while compacting", () => {
		const w = mount(ContextRing, { props: { context: ctx(), compacting: true } });
		expect(
			w.find("button:not([data-testid=context-ring])").attributes("disabled")
		).toBeDefined();
	});

	it("emits compact when the Compact button is clicked", async () => {
		const w = mount(ContextRing, { props: { context: ctx() } });
		await w.find("button:not([data-testid=context-ring])").trigger("click");
		expect(w.emitted("compact")).toHaveLength(1);
	});
});
