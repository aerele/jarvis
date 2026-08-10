import { describe, it, expect, vi, afterEach } from "vitest";
import { mount } from "@vue/test-utils";

import TourIntro from "./TourIntro.vue";

// The tour's own comment header (see TourIntro.vue) claims a fixed slide
// order: Welcome, Chat, Skills, Macros, File Box, Agents. These tests pin
// that order, since a reorder that silently regresses would only ever be
// caught by eyeballing the wizard.
const SLIDE_HEADINGS = [
	"Harness AI agents inside your ERPNext.",
	"Ask anything about your business.",
	"It already knows Frappe & ERPNext.",
	"Turn a routine into one click.",
	"Drop files in, get clean entries out.",
	"Put specialists to work in the background.",
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
