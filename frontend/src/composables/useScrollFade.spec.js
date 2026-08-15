import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { vScrollFade } from "./useScrollFade";

// Bare host element: the `v-scroll-fade` directive works on any scrollable
// node, so a plain <div> is enough to exercise mounted()/unmounted() without
// pulling in a real popover component. The vXxx -> v-xxx auto-registration
// only kicks in for <script setup> SFCs, so the test wires the directive in
// explicitly via `global.directives`.
const Host = {
	template: '<div class="scroll-region" v-scroll-fade style="overflow:auto"></div>',
};

function mountHost() {
	return mount(Host, { global: { directives: { "scroll-fade": vScrollFade } } });
}

describe("v-scroll-fade directive", () => {
	beforeEach(() => {
		vi.useFakeTimers();
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	it("marks the element as an overlay-scrollbar surface on mount", () => {
		const w = mountHost();
		expect(w.find(".scroll-region").classes()).toContain("jv-scroll-fade");
	});

	it("adds is-scrolling while a scroll event is firing", async () => {
		const w = mountHost();
		await w.find(".scroll-region").trigger("scroll");
		expect(w.find(".scroll-region").classes()).toContain("is-scrolling");
	});

	it("removes is-scrolling ~800ms after scrolling stops", async () => {
		const w = mountHost();
		await w.find(".scroll-region").trigger("scroll");
		expect(w.find(".scroll-region").classes()).toContain("is-scrolling");
		vi.advanceTimersByTime(800);
		expect(w.find(".scroll-region").classes()).not.toContain("is-scrolling");
	});

	it("restarts the hide timer on repeated scrolling instead of hiding early", async () => {
		const w = mountHost();
		const el = w.find(".scroll-region");
		await el.trigger("scroll");
		vi.advanceTimersByTime(500);
		await el.trigger("scroll"); // still scrolling — should push the deadline out
		vi.advanceTimersByTime(500);
		expect(el.classes()).toContain("is-scrolling");
		vi.advanceTimersByTime(300);
		expect(el.classes()).not.toContain("is-scrolling");
	});

	it("cleans up its scroll listener on unmount", async () => {
		const w = mountHost();
		const el = w.element;
		const removeSpy = vi.spyOn(el, "removeEventListener");
		w.unmount();
		expect(removeSpy).toHaveBeenCalledWith("scroll", expect.any(Function));
	});
});
