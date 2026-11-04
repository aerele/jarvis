import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

const api = vi.hoisted(() => ({ searchLink: vi.fn().mockResolvedValue([]) }));
vi.mock("@/api", () => api);
const agentsApi = vi.hoisted(() => ({ getCycleCountReviewDefaults: vi.fn() }));
vi.mock("@/api/agents", () => agentsApi);

vi.mock("frappe-ui", () => ({
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "modelValue", "placeholder"],
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
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: '<div class="error">{{ message }}</div>',
	},
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "label", "options", "description"],
		emits: ["update:modelValue"],
		template:
			'<div><input v-if="type !== \'select\'" :data-label="label" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" /><select v-else :data-label="label" :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option></select></div>',
	},
}));

import CycleCountConfigForm from "./CycleCountConfigForm.vue";

const defaults = {
	defaults: {
		company: "Example Ltd",
		from_date: "2026-08-01",
		to_date: "2026-10-30",
		count_history_from: "2025-10-31",
		line_limit: 30,
		recount_after_days: 30,
		basis: "current_records",
		review_scope: "count_plan_only",
		policy_version: "CYCLE-COUNT-v1",
	},
	warehouse_options: ["Example - North", "Example - South"],
	message: "",
};

function configured(overrides = {}) {
	return {
		...defaults.defaults,
		warehouses: [],
		...overrides,
	};
}

function mountForm(config = {}) {
	return mount(CycleCountConfigForm, {
		props: { config, saving: false, validationErrors: {} },
	});
}

beforeEach(() => {
	vi.clearAllMocks();
	agentsApi.getCycleCountReviewDefaults.mockResolvedValue(defaults);
});

describe("CycleCountConfigForm", () => {
	it("saves only the accepted count-plan keys and keeps warehouses as an array", async () => {
		const wrapper = mountForm(configured());
		await flushPromises();
		const boxes = wrapper.findAll('input[type="checkbox"]');
		expect(boxes).toHaveLength(2);
		await boxes[0].setValue(true);
		await wrapper.get('[data-label="Save configuration"]').trigger("click");

		expect(wrapper.emitted("save")[0][0]).toEqual({
			...configured(),
			warehouses: ["Example - North"],
		});
		expect(wrapper.emitted("save")[0][0]).not.toHaveProperty("reviewer");
		expect(wrapper.emitted("save")[0][0]).not.toHaveProperty("plan_date");
	});

	it("clears a previous company selection's warehouses before loading the new company", async () => {
		const wrapper = mountForm(configured({ warehouses: ["Example - North"] }));
		await flushPromises();
		expect(wrapper.text()).toContain("Example - North");

		const company = wrapper.findComponent({ name: "Autocomplete" });
		company.vm.$emit("update:modelValue", { label: "New Co", value: "New Co" });
		await flushPromises();

		expect(wrapper.find('ul[aria-label="Selected warehouses"]').exists()).toBe(false);
		expect(agentsApi.getCycleCountReviewDefaults).toHaveBeenLastCalledWith("New Co");
	});

	it("does not mark an unchanged saved form as dirty", async () => {
		const wrapper = mountForm(configured());
		await flushPromises();
		const dirty = wrapper.emitted("dirty").map((event) => event[0]);
		expect(dirty.at(-1)).toBe(false);
	});
});
