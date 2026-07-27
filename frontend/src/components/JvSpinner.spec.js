import { describe, it, expect, vi, afterEach } from "vitest";
import { mount } from "@vue/test-utils";

import JvSpinner from "./JvSpinner.vue";
import { BRAND_STAR_PATH } from "@/lib/brand";

/**
 * The tier is derived inside the component rather than passed in, so these
 * tests pin the derivation at its exact boundaries. A caller cannot select a
 * tier that disagrees with its size, and these assertions are what keep that
 * true.
 */

const spinner = (wrapper) => wrapper.find(".jv-spin");

afterEach(() => {
	vi.restoreAllMocks();
});

describe("JvSpinner sizing", () => {
	it("defaults to the 20px floor on the md tier", () => {
		const w = mount(JvSpinner);
		const el = spinner(w);
		expect(el.classes()).toContain("jv-spin--md");
		expect(el.attributes("style")).toContain("--jv-spin-size: 20px");
	});

	it("stays on the md tier at the top of its range", () => {
		const w = mount(JvSpinner, { props: { size: 35 } });
		expect(spinner(w).classes()).toContain("jv-spin--md");
	});

	it("crosses to the lg tier at 36", () => {
		const w = mount(JvSpinner, { props: { size: 36 } });
		const el = spinner(w);
		expect(el.classes()).toContain("jv-spin--lg");
		expect(el.classes()).not.toContain("jv-spin--md");
	});

	it("keeps large sizes on the lg tier", () => {
		const w = mount(JvSpinner, { props: { size: 72 } });
		expect(spinner(w).classes()).toContain("jv-spin--lg");
		expect(spinner(w).attributes("style")).toContain("--jv-spin-size: 72px");
	});
});

describe("JvSpinner floor", () => {
	it("clamps a below-floor size up to 20px rather than rendering a degraded mark", () => {
		vi.spyOn(console, "warn").mockImplementation(() => {});
		const w = mount(JvSpinner, { props: { size: 12 } });
		const el = spinner(w);
		expect(el.attributes("style")).toContain("--jv-spin-size: 20px");
		expect(el.classes()).toContain("jv-spin--md");
	});

	it("warns the developer when it clamps, naming the fix", () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		mount(JvSpinner, { props: { size: 16 } });
		expect(warn).toHaveBeenCalledTimes(1);
		// The message has to tell the developer to resize the container, because
		// the tempting fix (shrink the spinner) is the one that breaks the
		// single-mark property this component exists to hold.
		expect(warn.mock.calls[0][0]).toMatch(/resize the container/i);
	});

	it("does not warn at or above the floor", () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		mount(JvSpinner, { props: { size: 20 } });
		expect(warn).not.toHaveBeenCalled();
	});
});

describe("JvSpinner accessibility", () => {
	it("is a named status region when it has no label", () => {
		const w = mount(JvSpinner);
		const el = spinner(w);
		expect(el.attributes("role")).toBe("status");
		expect(el.attributes("aria-label")).toBe("Loading");
	});

	it("announces the label politely and does not double up the accessible name", () => {
		const w = mount(JvSpinner, { props: { size: 56, label: "Applying to your agent..." } });
		const region = w.find(".jv-spin-stack");
		expect(region.attributes("role")).toBe("status");
		expect(region.attributes("aria-live")).toBe("polite");
		expect(region.text()).toContain("Applying to your agent...");
		// The graphic itself is hidden from AT, so the label is the only
		// announced text rather than "Loading Applying to your agent...".
		expect(spinner(w).attributes("aria-hidden")).toBe("true");
		expect(spinner(w).attributes("aria-label")).toBeUndefined();
	});
});

describe("JvSpinner brand glyph", () => {
	it("draws the shared brand spark rather than its own copy of the path", () => {
		const w = mount(JvSpinner);
		expect(w.find("path").attributes("d")).toBe(BRAND_STAR_PATH);
	});
});
