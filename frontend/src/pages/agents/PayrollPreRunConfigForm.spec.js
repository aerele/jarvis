import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

const api = vi.hoisted(() => ({ searchLink: vi.fn().mockResolvedValue([]) }));
vi.mock("@/api", () => api);

vi.mock("frappe-ui", () => ({
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "modelValue", "placeholder", "disabled"],
		emits: ["update:query", "update:modelValue"],
		template: "<div />",
	},
	Button: {
		name: "Button",
		props: ["label", "loading"],
		emits: ["click"],
		template: '<button :data-label="label" @click="$emit(\'click\')">{{ label }}</button>',
	},
	FormControl: {
		name: "FormControl",
		props: ["type", "label", "description", "modelValue", "disabled"],
		emits: ["update:modelValue"],
		template: "<div />",
	},
}));

import PayrollPreRunConfigForm from "./PayrollPreRunConfigForm.vue";

const configured = {
	payroll_entry: "PAY-2026-10",
	reviewer_of_record: "Priya (payroll owner)",
};

function mountForm(config = {}, validationErrors = {}) {
	return mount(PayrollPreRunConfigForm, {
		props: { config, validationErrors, saving: false },
	});
}

function picker(wrapper, placeholder) {
	return wrapper
		.findAllComponents({ name: "Autocomplete" })
		.find((component) => component.props("placeholder") === placeholder);
}

describe("PayrollPreRunConfigForm", () => {
	beforeEach(() => vi.clearAllMocks());

	it("saves only the declared entry and reviewer keys", async () => {
		const wrapper = mountForm({
			...configured,
			company: "Do not retain",
			from_date: "2020-01-01",
			record_scope: { doctype: "Payroll Entry", name: "Do not retain" },
		});
		await flushPromises();
		await wrapper.get('[data-label="Save configuration"]').trigger("click");

		expect(wrapper.emitted("save")[0][0]).toEqual(configured);
	});

	it("uses a link picker for a non-cancelled Payroll Entry", async () => {
		const wrapper = mountForm();
		await picker(wrapper, "Search Payroll Entry…").trigger("focusin");
		await flushPromises();

		expect(api.searchLink).toHaveBeenCalledWith(
			"Payroll Entry",
			"",
			20,
			undefined,
			undefined,
			[["Payroll Entry", "docstatus", "!=", 2]]
		);
	});

	it("keeps an incomplete setup as a saveable draft", async () => {
		const wrapper = mountForm({}, { payroll_entry: "Select a readable Payroll Entry." });
		await wrapper.get('[data-label="Save configuration"]').trigger("click");

		expect(wrapper.emitted("save")[0][0]).toEqual({});
		expect(wrapper.text()).toContain("Select a readable Payroll Entry.");
	});

	it("updates the entry and preserves the named reviewer value", async () => {
		const wrapper = mountForm();
		picker(wrapper, "Search Payroll Entry…").vm.$emit("update:modelValue", {
			label: "PAY-2026-11",
			value: "PAY-2026-11",
		});
		wrapper
			.findComponent({ name: "FormControl" })
			.vm.$emit("update:modelValue", "Priya (payroll owner)");
		await wrapper.get('[data-label="Save configuration"]').trigger("click");

		expect(wrapper.emitted("save")[0][0]).toEqual({
			payroll_entry: "PAY-2026-11",
			reviewer_of_record: "Priya (payroll owner)",
		});
	});
});
