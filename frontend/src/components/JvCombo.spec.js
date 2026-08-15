import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import JvCombo from "./JvCombo.vue";

/**
 * The AI models "Add a model" dialog prefills Model with the provider's
 * default (e.g. "claude-sonnet-5") before the user ever opens the dropdown.
 * `filtered` used to key off `modelValue` directly, so opening a combobox
 * that already held a valid pick filtered the list down to that ONE option -
 * the dropdown looked like the catalog had a single model. A combobox must
 * show the full list on open regardless of what's prefilled, and filter only
 * once the user actually types a new query (jarvis: AI models model dropdown
 * showing only the prefilled model).
 */

const MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"];

function openCombo(props = {}) {
	const w = mount(JvCombo, {
		props: { options: MODELS, allowCustom: true, modelValue: "claude-sonnet-5", ...props },
	});
	return w;
}

describe("JvCombo allowCustom dropdown", () => {
	it("shows every option on focus, even though a matching value is prefilled", async () => {
		const w = openCombo();
		await w.find("input").trigger("focus");
		expect(w.findAll(".jvc-opt")).toHaveLength(MODELS.length);
	});

	it("filters as the user types a new query", async () => {
		const w = openCombo();
		const input = w.find("input");
		await input.trigger("focus");
		await input.setValue("opus");
		expect(w.findAll(".jvc-opt")).toHaveLength(1);
		expect(w.find(".jvc-opt").text()).toContain("claude-opus-5");
	});

	it("shows the full list again on reopen after choosing an option", async () => {
		const w = openCombo();
		const input = w.find("input");
		await input.trigger("focus");
		await input.setValue("opus");
		await w.find(".jvc-opt").trigger("mousedown");
		const emissions = w.emitted("update:modelValue");
		expect(emissions[emissions.length - 1]).toEqual(["claude-opus-5"]);

		// Host re-renders with the chosen value; simulate that round trip.
		await w.setProps({ modelValue: "claude-opus-5" });
		await input.trigger("focus");
		expect(w.findAll(".jvc-opt")).toHaveLength(MODELS.length);
	});

	it("keeps filtering while the user keeps typing after picking once", async () => {
		const w = openCombo();
		const input = w.find("input");
		await input.trigger("focus");
		await input.setValue("opus");
		await w.find(".jvc-opt").trigger("mousedown");
		await w.setProps({ modelValue: "claude-opus-5" });

		// A click back into the (still-focused) input must not wipe an
		// in-progress query - only a fresh open (blur+refocus) resets it.
		await input.setValue("haiku");
		expect(w.findAll(".jvc-opt")).toHaveLength(1);
		expect(w.find(".jvc-opt").text()).toContain("claude-haiku-4-5");
	});
});
