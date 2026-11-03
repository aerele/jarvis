import { expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
vi.mock("frappe-ui", () => ({
	Button: {
		props: ["label"],
		emits: ["click"],
		template: "<button @click=\"$emit('click')\">{{ label }}</button>",
	},
	FormControl: {
		props: ["label", "modelValue"],
		emits: ["update:modelValue"],
		template:
			'<label>{{ label }}<textarea :data-label="label" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" /></label>',
	},
}));
import Config from "./TicketTriageConfigForm.vue";
it("saves exact controlled arrays and never adds a company or assignment scope", async () => {
	const wrapper = mount(Config, {
		props: { config: { eligible_team_names: ["Support"], allowed_priorities: ["High"] } },
	});
	await wrapper.get('[data-label="Helpdesk teams"]').setValue("Support\nDispatch\nSupport\n");
	await wrapper.get("button").trigger("click");
	const saved = wrapper.emitted("save")[0][0];
	expect(saved.eligible_team_names).toEqual(["Dispatch", "Support"]);
	expect(saved.assignment_mode).toBe("none");
	expect(saved.policy_version).toBe("ticket-triage-v1");
	expect(saved).not.toHaveProperty("company");
	expect(wrapper.emitted("dirty").at(-1)[0]).toBe(true);
});
it("keeps edited values when a save returns validation errors", async () => {
	const wrapper = mount(Config);
	await wrapper.get('[data-label="Modified from (UTC)"]').setValue("2026-11-01T00:00:00Z");
	await wrapper.setProps({ validationErrors: { to_at: "Choose a past cutoff." } });
	expect(wrapper.get('[data-label="Modified from (UTC)"]').element.value).toBe(
		"2026-11-01T00:00:00Z"
	);
	expect(wrapper.get('[role="alert"]').text()).toContain("past cutoff");
});
