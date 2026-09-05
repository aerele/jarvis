import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import ContextPill from "./ContextPill.vue";

const ctx = (over = {}) => ({
	used: 42000,
	capacity: 200000,
	pct: 21,
	warn_pct: 80,
	auto_compact_pct: 90,
	route: "fits",
	compaction_count: 0,
	last_compacted_at: null,
	compacting: false,
	fresh: true,
	...over,
});

describe("ContextPill", () => {
	it("is hidden until measured", () => {
		const w = mount(ContextPill, { props: { context: ctx({ fresh: false }) } });
		expect(w.find("[data-testid=context-pill]").exists()).toBe(false);
	});
	it("shows used of capacity and the auto-compact tick", () => {
		const w = mount(ContextPill, { props: { context: ctx() } });
		expect(w.text()).toContain("42k / 200k");
		expect(w.find("[data-testid=auto-tick]").attributes("style")).toContain("90%");
		expect(w.classes()).not.toContain("jv-ctx-warn");
	});
	it("turns warn at 80 percent", () => {
		const w = mount(ContextPill, { props: { context: ctx({ used: 168000, pct: 84 }) } });
		expect(w.classes()).toContain("jv-ctx-warn");
	});
	it("shows compacting and compacted states", () => {
		const a = mount(ContextPill, { props: { context: ctx(), compacting: true } });
		expect(a.text()).toContain("Compacting");
		const b = mount(ContextPill, { props: { context: ctx(), compacted: true } });
		expect(b.text()).toContain("Compacted");
	});
	it("emits compact on click", async () => {
		const w = mount(ContextPill, { props: { context: ctx() } });
		await w.find("[data-testid=context-pill]").trigger("click");
		expect(w.emitted("compact")).toHaveLength(1);
	});
	it("keeps the visible value in the accessible name", () => {
		const w = mount(ContextPill, { props: { context: ctx() } });
		expect(w.find("[data-testid=context-pill]").attributes("aria-label")).toMatch(
			/^42k \/ 200k/
		);
	});
	it("disables the pill while compacting", () => {
		const w = mount(ContextPill, { props: { context: ctx(), compacting: true } });
		expect(w.find("[data-testid=context-pill]").attributes("disabled")).toBeDefined();
	});
	it("empties the bar once compacted", () => {
		const w = mount(ContextPill, { props: { context: ctx(), compacted: true } });
		expect(w.find(".jv-ctx-fill").attributes("style")).toContain("width: 0%");
	});
});
