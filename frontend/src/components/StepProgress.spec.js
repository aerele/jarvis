import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";

import StepProgress from "./StepProgress.vue";

const STEPS = [
	{ id: "a", label: "Details" },
	{ id: "b", label: "Plan" },
	{ id: "c", label: "Pay" },
];

const segments = (wrapper) => wrapper.findAll('[role="listitem"] > .rounded-full');
const items = (wrapper) => wrapper.findAll('[role="listitem"]');

describe("StepProgress segments", () => {
	it("renders one segment per step", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 1 } });
		expect(segments(w)).toHaveLength(3);
	});

	it("fills done and current steps, leaves upcoming steps on the track colour", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 1 } });
		const segs = segments(w);
		expect(segs[0].classes()).toContain("bg-surface-gray-7"); // done
		expect(segs[1].classes()).toContain("bg-surface-gray-7"); // current
		expect(segs[2].classes()).toContain("bg-surface-gray-3"); // upcoming
	});

	it("fills only the first segment when currentIndex is 0", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 0 } });
		const segs = segments(w);
		expect(segs[0].classes()).toContain("bg-surface-gray-7");
		expect(segs[1].classes()).toContain("bg-surface-gray-3");
		expect(segs[2].classes()).toContain("bg-surface-gray-3");
	});
});

describe("StepProgress indeterminate state", () => {
	it("marks only the current segment as indeterminate, not the done ones", () => {
		const w = mount(StepProgress, {
			props: { steps: STEPS, currentIndex: 1, indeterminate: true },
		});
		const segs = segments(w);
		expect(segs[0].classes()).not.toContain("step-progress-segment--indeterminate");
		expect(segs[1].classes()).toContain("step-progress-segment--indeterminate");
		expect(segs[2].classes()).not.toContain("step-progress-segment--indeterminate");
	});

	it("does not mark anything indeterminate by default", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 1 } });
		expect(w.find(".step-progress-segment--indeterminate").exists()).toBe(false);
	});
});

describe("StepProgress accessibility", () => {
	it("renders step semantics as a labelled list, not a progressbar", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 1 } });
		expect(w.find('[role="list"]').exists()).toBe(true);
		expect(items(w)).toHaveLength(3);
		expect(w.find('[role="progressbar"]').exists()).toBe(false);
	});

	it("marks the current step with aria-current=step, and no other step", () => {
		const w = mount(StepProgress, { props: { steps: STEPS, currentIndex: 1 } });
		const rows = items(w);
		expect(rows[0].attributes("aria-current")).toBeUndefined();
		expect(rows[1].attributes("aria-current")).toBe("step");
		expect(rows[2].attributes("aria-current")).toBeUndefined();
	});

	it("uses the ariaLabel prop to name the list when there is no visible caption", () => {
		const w = mount(StepProgress, {
			props: { steps: STEPS, currentIndex: 0, ariaLabel: "Setup steps" },
		});
		expect(w.find('[role="list"]').attributes("aria-label")).toBe("Setup steps");
	});

	it("names the list from the visible label instead, when one is given", () => {
		const w = mount(StepProgress, {
			props: { steps: [{}, {}, {}], currentIndex: 0, label: "Step 1 of 3" },
		});
		const list = w.find('[role="list"]');
		expect(list.attributes("aria-label")).toBeUndefined();
		const labelledBy = list.attributes("aria-labelledby");
		expect(labelledBy).toBeTruthy();
		expect(w.find(`#${labelledBy}`).text()).toBe("Step 1 of 3");
	});

	it("gives an unlabelled step an accessible name instead of leaving it silent", () => {
		const w = mount(StepProgress, {
			props: { steps: [{}, {}, {}], currentIndex: 1 },
		});
		expect(items(w)[1].find(".sr-only").text()).toBe("Step 2 of 3");
	});

	it("collapseLabels tags labels collapsible (visually hidden under the breakpoint, still announced)", () => {
		const w = mount(StepProgress, {
			props: { steps: STEPS, currentIndex: 1, collapseLabels: true },
		});
		// jsdom computes no CSS: assert the hook class, whose media query does
		// the hiding, and that the label text itself is untouched.
		const labels = w.findAll(".step-progress-label--collapsible");
		expect(labels).toHaveLength(3);
		expect(labels[0].text()).toBe("Details");
		const plain = mount(StepProgress, { props: { steps: STEPS, currentIndex: 1 } });
		expect(plain.find(".step-progress-label--collapsible").exists()).toBe(false);
	});
});
