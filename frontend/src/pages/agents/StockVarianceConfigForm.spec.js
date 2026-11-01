import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

const api = vi.hoisted(() => ({ searchLink: vi.fn().mockResolvedValue([]) }));
vi.mock("@/api", () => api);

vi.mock("frappe-ui", () => ({
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "modelValue", "placeholder", "disabled"],
		emits: ["update:query", "update:modelValue"],
		template:
			'<div><button v-for="option in options" :key="option.value" :data-option="option.value" @click="$emit(\'update:modelValue\', option)">{{ option.label }}</button></div>',
	},
	Button: {
		name: "Button",
		props: ["label", "loading"],
		emits: ["click"],
		template: '<button :data-label="label" @click="$emit(\'click\')">{{ label }}</button>',
	},
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "label", "description"],
		emits: ["update:modelValue"],
		template:
			'<textarea :data-label="label" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
	},
}));

import StockVarianceConfigForm from "./StockVarianceConfigForm.vue";

const configured = {
	company: "Example Ltd",
	from_date: "2026-04-01",
	to_date: "2026-10-31",
	record_scope: { doctype: "Warehouse", name: "Example Root - EX" },
	stock_variance: {
		reviewer: "reviewer@example.invalid",
		test_declaration: "Synthetic counts only.",
	},
};

function mountForm(config = {}) {
	return mount(StockVarianceConfigForm, {
		props: { config, saving: false, validationErrors: {} },
	});
}

function picker(wrapper, placeholder) {
	return wrapper
		.findAllComponents({ name: "Autocomplete" })
		.find((component) => component.props("placeholder") === placeholder);
}

describe("StockVarianceConfigForm", () => {
	beforeEach(() => vi.clearAllMocks());

	it("saves the Warehouse record scope separately from the evaluator namespace", async () => {
		const wrapper = mountForm(configured);
		await flushPromises();
		await wrapper.get('[data-label="Save configuration"]').trigger("click");

		expect(wrapper.emitted("save")[0][0]).toEqual(configured);
		expect(wrapper.emitted("save")[0][0].record_scope).toEqual({
			doctype: "Warehouse",
			name: "Example Root - EX",
		});
		expect(wrapper.emitted("save")[0][0].stock_variance).toEqual({
			reviewer: "reviewer@example.invalid",
			test_declaration: "Synthetic counts only.",
		});
	});

	it("clears the prior Warehouse when the company changes", async () => {
		const wrapper = mountForm(configured);
		await flushPromises();
		picker(wrapper, "Search Company…").vm.$emit("update:modelValue", {
			label: "Other Ltd",
			value: "Other Ltd",
		});
		await flushPromises();

		expect(picker(wrapper, "Search Warehouse group or leaf…").props("modelValue")).toBeNull();
		picker(wrapper, "Search Warehouse group or leaf…").vm.$emit("update:modelValue", {
			label: "Other Leaf - OL",
			value: "Other Leaf - OL",
		});
		await wrapper.get('[data-label="Save configuration"]').trigger("click");

		expect(wrapper.emitted("save")[0][0]).toMatchObject({
			company: "Other Ltd",
			record_scope: { doctype: "Warehouse", name: "Other Leaf - OL" },
			stock_variance: configured.stock_variance,
		});
	});

	it("lists only leaf companies", async () => {
		const wrapper = mountForm();
		await picker(wrapper, "Search Company…").trigger("focusin");
		await flushPromises();

		expect(api.searchLink).toHaveBeenCalledWith("Company", "", 20, undefined, undefined, [
			["Company", "is_group", "=", 0],
		]);
	});

	it("does not emit an incomplete root or reviewer configuration", async () => {
		const wrapper = mountForm({ company: "Example Ltd" });
		await wrapper.get('[data-label="Save configuration"]').trigger("click");

		expect(wrapper.emitted("save")).toBeUndefined();
		expect(wrapper.text()).toContain("Select an enabled Warehouse group or leaf.");
		expect(wrapper.text()).toContain("Select an enabled stock reviewer.");
	});
});
