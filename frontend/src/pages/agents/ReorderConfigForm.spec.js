import { expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
vi.mock("frappe-ui", () => ({
	Button: { props: ["label"], template: '<button type="submit">{{ label }}</button>' },
	FormControl: {
		props: ["modelValue", "label"],
		emits: ["update:modelValue"],
		template:
			'<label>{{ label }}<input :value="modelValue" @input="$emit(\'update:modelValue\',$event.target.value)" /></label>',
	},
}));
import Config from "./ReorderConfigForm.vue";
it("saves the exact native policy without fiscal or model settings", async () => {
	const wrapper = mount(Config, {
		props: { config: { negative_stock_run: "NEG-1", max_proposals: 25 } },
	});
	await wrapper.get("form").trigger("submit.prevent");
	expect(wrapper.emitted("save")[0][0]).toEqual({
		policy_version: "erpnext-reorder-v1",
		negative_stock_run: "NEG-1",
		max_proposals: 25,
	});
});
it("keeps entered settings visible when the server rejects a saved run", async () => {
	const wrapper = mount(Config, {
		props: { config: { negative_stock_run: "NEG-1", max_proposals: 25 } },
	});
	await wrapper.get("input").setValue("NEG-2");
	await wrapper.setProps({ validationErrors: { reorder: "Select a current run" } });
	expect(wrapper.get("input").element.value).toBe("NEG-2");
	expect(wrapper.get('[role="alert"]').text()).toContain("Select a current run");
});
