import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";

import StepProgress from "./StepProgress.vue";

const STEPS = [
	{ id: "a", label: "Details" },
	{ id: "b", label: "Plan" },
	{ id: "c", label: "Pay" },
];

// ---- variant="bar" (default): the onboarding wait screens' one continuous
// track, redesigned 2026-08-16 to replace the per-segment tiles below. ----
describe("StepProgress bar (default variant)", () => {
	it("renders one progressbar, not a list of segments", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 1 } });
		expect(w.find('[role="progressbar"]').exists()).toBe(true);
		expect(w.find('[role="list"]').exists()).toBe(false);
		expect(w.findAll('[role="listitem"]')).toHaveLength(0);
	});

	it("fills to the inclusive fraction: (currentIndex + 1) / total", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 1 } });
		// 2 of 3 steps filled (currentIndex 1 means step 0 AND the current step 1 fill).
		expect(w.find(".step-progress-fill").attributes("style")).toContain("width: 66.66");
		expect(w.find('[role="progressbar"]').attributes("aria-valuenow")).toBe("67");
	});

	it("fills one step's worth on the first step, not zero", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 0 } });
		expect(w.find(".step-progress-fill").attributes("style")).toContain("width: 33.33");
		expect(w.find('[role="progressbar"]').attributes("aria-valuenow")).toBe("33");
	});

	it("renders full fill once every step is complete", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 3 } });
		expect(w.find(".step-progress-fill").attributes("style")).toContain("width: 100%");
		expect(w.find('[role="progressbar"]').attributes("aria-valuenow")).toBe("100");
	});

	it("renders full fill when currentIndex signals all-done (-1)", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: -1 } });
		expect(w.find(".step-progress-fill").attributes("style")).toContain("width: 100%");
		expect(w.find('[role="progressbar"]').attributes("aria-valuenow")).toBe("100");
	});

	it("exposes progressbar min/max regardless of state", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 1 } });
		const bar = w.find('[role="progressbar"]');
		expect(bar.attributes("aria-valuemin")).toBe("0");
		expect(bar.attributes("aria-valuemax")).toBe("100");
	});
});

describe("StepProgress bar indeterminate state", () => {
	it("pulses the fill and omits aria-valuenow", () => {
		const w = mount(StepProgress, {
			props: { steps: STEPS, currentIndex: 1, indeterminate: true },
		});
		expect(w.find(".step-progress-fill--indeterminate").exists()).toBe(true);
		expect(w.find('[role="progressbar"]').attributes("aria-valuenow")).toBeUndefined();
	});

	it("does not pulse and does report aria-valuenow by default", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 1 } });
		expect(w.find(".step-progress-fill--indeterminate").exists()).toBe(false);
		expect(w.find('[role="progressbar"]').attributes("aria-valuenow")).toBe("67");
	});
});

describe("StepProgress bar caption and accessible name", () => {
	it("shows the caption line and labels the bar from it", () => {
		const w = mount(StepProgress, {
			props: { steps: STEPS, currentIndex: 1, label: "Step 2 of 3 · Plan" },
		});
		expect(w.text()).toContain("Step 2 of 3 · Plan");
		const bar = w.find('[role="progressbar"]');
		expect(bar.attributes("aria-label")).toBeUndefined();
		const labelledBy = bar.attributes("aria-labelledby");
		expect(labelledBy).toBeTruthy();
		expect(w.find(`#${labelledBy}`).text()).toBe("Step 2 of 3 · Plan");
	});

	it("falls back to ariaLabel when there is no visible caption", () => {
		const w = mount(StepProgress, {
			props: { steps: STEPS, currentIndex: 0, ariaLabel: "Setup steps" },
		});
		expect(w.find('[role="progressbar"]').attributes("aria-label")).toBe("Setup steps");
	});
});

// ---- variant="steps": the wizard rail's always-visible per-step labels,
// unchanged by the 2026-08-16 wait-bar redesign. ----
const segments = (wrapper) => wrapper.findAll('[role="listitem"] > .rounded-full');
const items = (wrapper) => wrapper.findAll('[role="listitem"]');

describe("StepProgress steps variant (wizard rail)", () => {
	it("renders one segment per step, not a progressbar", () => {
		const w = mount(StepProgress, {
			props: { steps: STEPS, currentIndex: 1, variant: "steps" },
		});
		expect(segments(w)).toHaveLength(3);
		expect(w.find('[role="progressbar"]').exists()).toBe(false);
	});

	it("fills done and current steps, leaves upcoming steps on the track colour", () => {
		const w = mount(StepProgress, {
			props: { steps: STEPS, currentIndex: 1, variant: "steps" },
		});
		const segs = segments(w);
		expect(segs[0].classes()).toContain("bg-surface-gray-7"); // done
		expect(segs[1].classes()).toContain("bg-surface-gray-7"); // current
		expect(segs[2].classes()).toContain("bg-surface-gray-3"); // upcoming
	});

	it("marks only the current segment as indeterminate, not the done ones", () => {
		const w = mount(StepProgress, {
			props: { steps: STEPS, currentIndex: 1, indeterminate: true, variant: "steps" },
		});
		const segs = segments(w);
		expect(segs[0].classes()).not.toContain("step-progress-segment--indeterminate");
		expect(segs[1].classes()).toContain("step-progress-segment--indeterminate");
		expect(segs[2].classes()).not.toContain("step-progress-segment--indeterminate");
	});

	it("marks the current step with aria-current=step, and no other step", () => {
		const w = mount(StepProgress, {
			props: { steps: STEPS, currentIndex: 1, variant: "steps" },
		});
		const rows = items(w);
		expect(rows[0].attributes("aria-current")).toBeUndefined();
		expect(rows[1].attributes("aria-current")).toBe("step");
		expect(rows[2].attributes("aria-current")).toBeUndefined();
	});

	it("uses the ariaLabel prop to name the list when there is no visible caption", () => {
		const w = mount(StepProgress, {
			props: { steps: STEPS, currentIndex: 0, ariaLabel: "Setup steps", variant: "steps" },
		});
		expect(w.find('[role="list"]').attributes("aria-label")).toBe("Setup steps");
	});

	it("names the list from the visible label instead, when one is given", () => {
		const w = mount(StepProgress, {
			props: {
				steps: [{}, {}, {}],
				currentIndex: 0,
				label: "Step 1 of 3",
				variant: "steps",
			},
		});
		const list = w.find('[role="list"]');
		expect(list.attributes("aria-label")).toBeUndefined();
		const labelledBy = list.attributes("aria-labelledby");
		expect(labelledBy).toBeTruthy();
		expect(w.find(`#${labelledBy}`).text()).toBe("Step 1 of 3");
	});

	it("gives an unlabelled step an accessible name instead of leaving it silent", () => {
		const w = mount(StepProgress, {
			props: { steps: [{}, {}, {}], currentIndex: 1, variant: "steps" },
		});
		expect(items(w)[1].find(".sr-only").text()).toBe("Step 2 of 3");
	});
});
