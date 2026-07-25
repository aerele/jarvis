// Sanity test for the verification harness itself (Task 1, Step 5) — proves
// mountWithPalette works before any component extraction happens.
import { expect, test } from "vitest";
import { h } from "vue";
import { mountWithPalette } from "./fixtures.js";

const Trivial = {
	name: "Trivial",
	render() {
		return h("div", { class: "trivial-probe" }, "hi");
	},
};

test("mountWithPalette wraps the component in .jv-root with palette vars bound", () => {
	const w = mountWithPalette(Trivial);
	expect(w.classes()).toContain("jv-root");
	expect(w.attributes("style")).toContain("--cta:");
	expect(w.find(".trivial-probe").text()).toBe("hi");
});

test("mountWithPalette({ dark: true }) adds jv-dark and swaps the palette", () => {
	const w = mountWithPalette(Trivial, {}, { dark: true });
	expect(w.classes()).toContain("jv-root");
	expect(w.classes()).toContain("jv-dark");
	expect(w.attributes("style")).toContain("--cta:");
});
