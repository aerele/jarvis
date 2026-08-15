import { describe, it, expect, vi, afterEach } from "vitest";
import { mount } from "@vue/test-utils";

import TourIntro from "./TourIntro.vue";

// The tour's own comment header (see TourIntro.vue) claims a fixed slide
// order: Welcome, Skills & Knowledge, Macros, File Box, Dashboards. These
// tests pin that order, since a reorder that silently regresses would only
// ever be caught by eyeballing the wizard.
const SLIDE_HEADINGS = [
	"Harness AI agents inside your ERPNext.",
	"It learns how your business runs.",
	"Turn a routine into one click.",
	"Drop files in, get clean entries out.",
	"Ask for a chart. Watch it build.",
];

function heading(wrapper) {
	return wrapper.find(".slide-copy h2").text();
}

function clickNext(wrapper) {
	return wrapper.find(".tour-nav .btn--primary").trigger("click");
}

afterEach(() => {
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

describe("TourIntro slide order", () => {
	it("opens on the business-outcome Welcome slide, not the chat demo", async () => {
		const wrapper = mount(TourIntro);
		expect(heading(wrapper)).toBe(SLIDE_HEADINGS[0]);
		wrapper.unmount();
	});

	it("walks every slide in the documented order via Next", async () => {
		const wrapper = mount(TourIntro);
		for (const [i, text] of SLIDE_HEADINGS.entries()) {
			expect(heading(wrapper)).toBe(text);
			if (i < SLIDE_HEADINGS.length - 1) {
				await clickNext(wrapper);
			}
		}
		wrapper.unmount();
	});

	it("the final slide's Next button is the Onboard CTA, not another slide", async () => {
		const wrapper = mount(TourIntro);
		for (let i = 0; i < SLIDE_HEADINGS.length - 1; i++) {
			await clickNext(wrapper);
		}
		expect(heading(wrapper)).toBe(SLIDE_HEADINGS[SLIDE_HEADINGS.length - 1]);
		await clickNext(wrapper);
		expect(wrapper.emitted("finish")).toHaveLength(1);
		wrapper.unmount();
	});
});

describe("TourIntro animation timers", () => {
	it("clears the running slide's timer on unmount", () => {
		vi.useFakeTimers();
		const wrapper = mount(TourIntro);
		vi.advanceTimersByTime(1000);
		expect(vi.getTimerCount()).toBeGreaterThan(0);
		wrapper.unmount();
		expect(vi.getTimerCount()).toBe(0);
	});

	it("keeps at most one slide's clock running at a time while navigating", async () => {
		vi.useFakeTimers();
		const wrapper = mount(TourIntro);
		for (let i = 0; i < SLIDE_HEADINGS.length - 1; i++) {
			// A leaked interval running behind the other five slides is the
			// failure mode this guards: only the visible slide's phase clock
			// may hold a pending timer.
			expect(vi.getTimerCount()).toBeLessThanOrEqual(1);
			await clickNext(wrapper);
			vi.advanceTimersByTime(50);
		}
		expect(vi.getTimerCount()).toBeLessThanOrEqual(1);
		wrapper.unmount();
		expect(vi.getTimerCount()).toBe(0);
	});

	it("restarts the visible slide's clock when the tab comes back", () => {
		// Round-1 review of #762: every mock gates its rows on how far its clock has
		// advanced, so a tab returning from throttled timers would show a half-drawn
		// slide. Coming back must re-enter the sequence, and must not stack a second
		// clock on top of the one already running.
		vi.useFakeTimers();
		const wrapper = mount(TourIntro);
		expect(vi.getTimerCount()).toBe(1);

		vi.advanceTimersByTime(1200);
		document.dispatchEvent(new Event("visibilitychange"));

		expect(vi.getTimerCount()).toBe(1);
		wrapper.unmount();
		expect(vi.getTimerCount()).toBe(0);
	});

	it("never starts a clock under prefers-reduced-motion", () => {
		vi.useFakeTimers();
		vi.stubGlobal("matchMedia", (query) => ({
			matches: true,
			media: query,
			addEventListener: () => {},
			removeEventListener: () => {},
		}));
		const wrapper = mount(TourIntro);
		vi.advanceTimersByTime(5000);
		expect(vi.getTimerCount()).toBe(0);
		// Static settled frame still renders the copy; motion is what's absent.
		expect(heading(wrapper)).toBe(SLIDE_HEADINGS[0]);
		wrapper.unmount();
	});
});

// A phase clock never starts under prefers-reduced-motion, so it sits on the
// LAST step of its sequence (see createPhaseClock in TourIntro.vue). Both
// slides reworked for the two-scene / chat-scene tweaks must settle there on
// a complete frame, not a mid-transition one, or reduced-motion users see a
// broken mock forever.
describe("TourIntro settled (reduced-motion) frames", () => {
	function stubReducedMotion() {
		vi.stubGlobal("matchMedia", (query) => ({
			matches: true,
			media: query,
			addEventListener: () => {},
			removeEventListener: () => {},
		}));
	}

	it("Skills & Knowledge settles on the Wiki tab with the graph drawn, not the skills list", async () => {
		stubReducedMotion();
		const wrapper = mount(TourIntro);
		await clickNext(wrapper);
		expect(heading(wrapper)).toBe(SLIDE_HEADINGS[1]);
		const tabs = wrapper.findAll(".m-tab");
		expect(tabs[0].classes()).not.toContain("on"); // Skills
		expect(tabs[2].classes()).toContain("on"); // Wiki
		expect(wrapper.find(".m-graph-svg").exists()).toBe(true);
		expect(wrapper.find(".m-row-skill").exists()).toBe(false);
		wrapper.unmount();
	});

	it("Macros settles with the macro saved and the run done", async () => {
		stubReducedMotion();
		const wrapper = mount(TourIntro);
		await clickNext(wrapper);
		await clickNext(wrapper);
		expect(heading(wrapper)).toBe(SLIDE_HEADINGS[2]);
		expect(wrapper.find(".macro-card-btn").text()).toContain("Saved");
		expect(wrapper.find(".macro-card-btn").classes()).toContain("saved");
		expect(wrapper.text()).toContain("done");
		expect(wrapper.find(".meta-fill").classes()).toContain("done");
		wrapper.unmount();
	});
});
