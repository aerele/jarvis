import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
	Dialog: {
		props: ["modelValue", "options"],
		template: "<div><slot name='body-content'/><slot name='actions'/></div>",
	},
	Button: {
		props: ["label", "variant", "disabled", "loading"],
		emits: ["click"],
		template: "<button :disabled='disabled' @click=\"$emit('click')\">{{ label }}</button>",
	},
	FormControl: {
		props: ["modelValue", "label", "placeholder", "type"],
		emits: ["update:modelValue"],
		template:
			"<textarea :value='modelValue' @input=\"$emit('update:modelValue', $event.target.value)\"/>",
	},
}));

import CompactDialog from "./CompactDialog.vue";

describe("CompactDialog", () => {
	it("confirms with the trimmed hint", async () => {
		const w = mount(CompactDialog, { props: { modelValue: true } });
		await w.find("textarea").setValue("  keep the invoice inputs ");
		await w.findAll("button").at(-1).trigger("click");
		expect(w.emitted("confirm")[0]).toEqual(["keep the invoice inputs"]);
	});
	it("disables Compact with the busy reason", () => {
		const w = mount(CompactDialog, {
			props: { modelValue: true, busyReason: "A reply is in progress" },
		});
		expect(w.findAll("button").at(-1).attributes("disabled")).toBeDefined();
		expect(w.text()).toContain("A reply is in progress");
	});
});
