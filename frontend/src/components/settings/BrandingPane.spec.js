import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

// admin-v2#622: Save applies the brand at once; the user does not have to reload.
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "loading", "disabled", "variant"],
		emits: ["click"],
		template: `<button :disabled="disabled" @click="$emit('click')">{{ label }}</button>`,
	},
	FormControl: {
		props: ["modelValue", "label"],
		emits: ["update:modelValue"],
		template: `<input class="name" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	toast,
}));
vi.mock("@/components/settings/SettingsPane.vue", () => ({
	default: { template: "<div><slot /></div>" },
}));
vi.mock("@/api", () => ({
	getBranding: vi.fn(async () => ({
		ok: true,
		data: { agent_name: "", brand_logo_url: "", brand_favicon_url: "" },
	})),
	uploadBrandAsset: vi.fn(async () => ({ file_url: "/files/acme.png" })),
	updateBranding: vi.fn(async (p) => ({
		ok: true,
		data: {
			agent_name: p.agent_name,
			brand_logo_url: p.logo_url,
			brand_favicon_url: p.favicon_url,
		},
	})),
}));

import { brand } from "@/branding";
import BrandingPane from "./BrandingPane.vue";

describe("BrandingPane: Save", () => {
	it("applies the saved name and logo at once, with no reload", async () => {
		const w = mount(BrandingPane);
		await flushPromises();
		await w.find("input.name").setValue("Acme Assistant");
		const logoInput = w.findAll('input[type="file"]')[0];
		Object.defineProperty(logoInput.element, "files", {
			value: [new File(["x"], "acme.png", { type: "image/png" })],
		});
		await logoInput.trigger("change");
		await flushPromises();

		const save = w
			.findAllComponents({ name: "Button" })
			.find((b) => b.props("label") === "Save branding");
		await save.trigger("click");
		await flushPromises();

		expect(brand.agentName).toBe("Acme Assistant");
		expect(brand.logoUrl).toBe("/files/acme.png");
		expect(toast.success).toHaveBeenCalledTimes(1);
		expect(toast.success.mock.calls[0][0]).not.toMatch(/refresh|reload/i);
	});
});
