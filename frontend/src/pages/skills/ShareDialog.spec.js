import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi } from "vitest";
vi.mock("frappe-ui", () => ({
	Dialog: {
		props: ["modelValue", "options"],
		template:
			'<div v-if="modelValue"><h1>{{options.title}}</h1><slot name="body-content"/><slot name="actions"/></div>',
	},
	Button: {
		props: ["label", "disabled", "loading"],
		template: '<button :disabled="disabled || loading">{{label}}<slot/></button>',
	},
	Avatar: { template: "<i/>" },
	FeatherIcon: { template: "<i/>" },
	toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/api", () => ({
	listShareableUsers: vi.fn(async () => [{ name: "reader@example.com", full_name: "Reader" }]),
	getSkillShares: vi.fn(async () => ({
		users: [{ name: "disabled@example.com", full_name: "Previous reader" }],
	})),
	shareCustomSkill: vi.fn(async () => ({ ok: true })),
}));
import ShareDialog from "./ShareDialog.vue";
import * as api from "@/api";
describe("existing skill sharing contract", () => {
	it("retains its props, existing recipients, replace-save API and emitted events", async () => {
		const w = mount(ShareDialog, {
			props: { modelValue: true, skill: { name: "SK-1", skill_name: "Monthly close" } },
		});
		await flushPromises();

		expect(api.getSkillShares).toHaveBeenCalledWith("SK-1");
		expect(w.text()).toContain("Previous reader");
		expect(w.text()).toContain("They can use this skill in chat");
		await w
			.findAll("button")
			.find((b) => b.text().includes("Reader") && !b.attributes("aria-label"))
			.trigger("click");
		await w
			.findAll("button")
			.find((b) => b.text() === "Save")
			.trigger("click");
		await flushPromises();
		expect(api.shareCustomSkill).toHaveBeenCalledWith("SK-1", [
			"disabled@example.com",
			"reader@example.com",
		]);
		expect(w.emitted("saved")).toHaveLength(1);
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
		w.unmount();
	});
});
