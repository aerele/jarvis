import { describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import FloorField from "./FloorField.vue";

const SETTINGS = {
	configured: true,
	company: "Northstar Manufacturing Pvt Ltd",
	currency: "INR",
	safety_floor: "50000.00",
	revision: 1,
	configuration_id: "northstar-floor",
};

function render(props = {}) {
	const save = vi.fn(async () => ({ ok: true }));
	const wrapper = mount(FloorField, {
		props: {
			settings: SETTINGS,
			canConfigure: true,
			saving: false,
			error: null,
			save,
			...props,
		},
		attachTo: document.body,
	});
	return { wrapper, save };
}
const input = (wrapper) => wrapper.get("#floor-input");
const saveButton = (wrapper) => wrapper.get("#floor-save");
const hidden = (wrapper) => saveButton(wrapper).attributes("hidden") !== undefined;

describe("FloorField", () => {
	it("shows the saved minimum as the template does", () => {
		const { wrapper } = render();
		expect(wrapper.get("label").text()).toBe("Minimum cash balance");
		expect(wrapper.get("label").attributes("for")).toBe("floor-input");
		expect(wrapper.get(".currency-field span").text()).toBe("₹");
		expect(input(wrapper).element.value).toBe("50,000");
		expect(input(wrapper).attributes("inputmode")).toBe("decimal");
		expect(wrapper.get("#floor-help").text()).toBe("Shared minimum · Save to apply");
		expect(wrapper.get("#floor-error").attributes("role")).toBe("alert");
		expect(hidden(wrapper)).toBe(true);
		expect(saveButton(wrapper).text()).toBe("Save");
	});

	it("shows Save only while the text differs from the saved amount", async () => {
		const { wrapper } = render();
		await input(wrapper).setValue("2,00,000");
		expect(hidden(wrapper)).toBe(false);
		await input(wrapper).setValue("50000.00");
		expect(hidden(wrapper)).toBe(true);
		await input(wrapper).setValue("50,000");
		expect(hidden(wrapper)).toBe(true);
	});

	it("never saves while typing: unsaved input has no effect", async () => {
		const { wrapper, save } = render();
		await input(wrapper).setValue("999");
		await input(wrapper).trigger("blur");
		expect(save).not.toHaveBeenCalled();
	});

	it("saves the comma-free text on Save", async () => {
		const { wrapper, save } = render();
		await input(wrapper).setValue("2,00,000");
		await saveButton(wrapper).trigger("click");
		expect(save).toHaveBeenCalledTimes(1);
		expect(save).toHaveBeenCalledWith("200000");
	});

	it("saves on Enter", async () => {
		const { wrapper, save } = render();
		await input(wrapper).setValue("75000.5");
		await input(wrapper).trigger("keydown", { key: "Enter" });
		expect(save).toHaveBeenCalledWith("75000.5");
	});

	it("does not call the server on Enter when nothing changed", async () => {
		const { wrapper, save } = render();
		await input(wrapper).trigger("keydown", { key: "Enter" });
		expect(save).not.toHaveBeenCalled();
	});

	it("explains a too-large amount with a limit message, not the decimals message", async () => {
		const { wrapper } = render();
		await input(wrapper).setValue("₹2,000,000,000");
		await saveButton(wrapper).trigger("click");
		expect(wrapper.get("#floor-error").text()).toMatch(/too large/i);
		expect(wrapper.get("#floor-error").text()).not.toMatch(/decimal/i);
	});

	it("saves an amount typed with a leading rupee sign", async () => {
		const { wrapper, save } = render();
		await input(wrapper).setValue("₹75,000");
		await saveButton(wrapper).trigger("click");
		expect(save).toHaveBeenCalledWith("75000");
	});

	it("marks the input invalid while an error shows, and clears the mark on edit", async () => {
		const { wrapper } = render();
		expect(input(wrapper).attributes("aria-invalid")).toBeUndefined();
		await input(wrapper).setValue("abc");
		await saveButton(wrapper).trigger("click");
		expect(input(wrapper).attributes("aria-invalid")).toBe("true");
		await input(wrapper).setValue("10");
		expect(input(wrapper).attributes("aria-invalid")).toBeUndefined();
	});

	it("marks the input invalid for a server error", async () => {
		const { wrapper } = render({ error: { code: "settings_changed" } });
		expect(input(wrapper).attributes("aria-invalid")).toBe("true");
	});

	it.each(["", "abc", "-1", "10.123"])(
		"refuses %j with the template message and keeps the text",
		async (text) => {
			const { wrapper, save } = render();
			await input(wrapper).setValue(text === "" ? " " : text);
			await saveButton(wrapper).trigger("click");
			expect(save).not.toHaveBeenCalled();
			expect(wrapper.get("#floor-error").text()).toBe(
				"Enter a non-negative amount with up to two decimal places."
			);
			expect(input(wrapper).element.value).toBe(text === "" ? " " : text);
		}
	);

	it("clears the message as soon as the text changes", async () => {
		const { wrapper } = render();
		await input(wrapper).setValue("abc");
		await saveButton(wrapper).trigger("click");
		await input(wrapper).setValue("abcd");
		expect(wrapper.get("#floor-error").text()).toBe("");
	});

	it("reverts on Escape without letting Escape reach the page", async () => {
		const { wrapper, save } = render();
		const outer = vi.fn();
		document.addEventListener("keydown", outer);
		await input(wrapper).setValue("abc");
		await saveButton(wrapper).trigger("click");
		await input(wrapper).trigger("keydown", { key: "Escape" });
		document.removeEventListener("keydown", outer);
		expect(input(wrapper).element.value).toBe("50,000");
		expect(hidden(wrapper)).toBe(true);
		expect(wrapper.get("#floor-error").text()).toBe("");
		expect(outer).not.toHaveBeenCalled();
		expect(save).not.toHaveBeenCalled();
	});

	it("shows the new amount formatted after the server accepts it", async () => {
		const { wrapper } = render();
		await input(wrapper).setValue("200000");
		await wrapper.setProps({
			settings: { ...SETTINGS, safety_floor: "200000.00", revision: 2 },
		});
		expect(input(wrapper).element.value).toBe("2,00,000");
		expect(hidden(wrapper)).toBe(true);
	});

	it("follows a saved change made elsewhere while the field is untouched", async () => {
		const { wrapper } = render();
		await wrapper.setProps({
			settings: { ...SETTINGS, safety_floor: "60000.00", revision: 2 },
		});
		expect(input(wrapper).element.value).toBe("60,000");
	});

	it("keeps the typed text after a conflict refusal and names the cause", async () => {
		const { wrapper } = render();
		await input(wrapper).setValue("44.00");
		await wrapper.setProps({
			settings: { ...SETTINGS, safety_floor: "70000.00", revision: 3 },
			error: { code: "settings_changed", state: "scope_changed", retryable: true },
		});
		expect(input(wrapper).element.value).toBe("44.00");
		expect(hidden(wrapper)).toBe(false);
		expect(wrapper.get("#floor-error").text()).toContain("changed by another user");
	});

	it("hides a server error once the person edits the text again", async () => {
		const { wrapper } = render();
		await input(wrapper).setValue("44");
		await wrapper.setProps({ error: { code: "settings_changed" } });
		await input(wrapper).setValue("45");
		expect(wrapper.get("#floor-error").text()).toBe("");
	});

	it("makes the field read-only and disables Save while saving", async () => {
		const { wrapper } = render();
		await input(wrapper).setValue("10");
		await wrapper.setProps({ saving: true });
		expect(input(wrapper).attributes("readonly")).toBeDefined();
		expect(input(wrapper).attributes("disabled")).toBeUndefined();
		expect(saveButton(wrapper).attributes("disabled")).toBeDefined();
		expect(saveButton(wrapper).text()).toBe("Saving…");
	});

	it("keeps focus in the input when Enter saves", async () => {
		const { wrapper } = render();
		input(wrapper).element.focus();
		await input(wrapper).setValue("75000");
		await input(wrapper).trigger("keydown", { key: "Enter" });
		await wrapper.setProps({ saving: true });
		expect(document.activeElement).toBe(input(wrapper).element);
		await wrapper.setProps({ saving: false });
		expect(document.activeElement).toBe(input(wrapper).element);
	});

	it("is read-only without permission and never offers Save", async () => {
		const { wrapper } = render({ canConfigure: false });
		expect(input(wrapper).attributes("readonly")).toBeDefined();
		expect(input(wrapper).element.value).toBe("50,000");
		expect(wrapper.find("#floor-save").exists()).toBe(false);
		expect(wrapper.get("#floor-help").text()).toBe("Shared minimum · View only");
		expect(input(wrapper).attributes("title")).toMatch(/permission/i);
	});

	it("is empty and disabled when the company access is gone", () => {
		const { wrapper } = render({ settings: null, canConfigure: false });
		expect(input(wrapper).element.value).toBe("");
		expect(input(wrapper).attributes("disabled")).toBeDefined();
		expect(input(wrapper).attributes("placeholder")).toBe("—");
		expect(wrapper.find("#floor-save").exists()).toBe(false);
	});

	it("lets a manager set a minimum that was never configured", async () => {
		const { wrapper, save } = render({
			settings: { ...SETTINGS, configured: false, safety_floor: null },
		});
		expect(input(wrapper).element.value).toBe("");
		await input(wrapper).setValue("5000");
		expect(hidden(wrapper)).toBe(false);
		await saveButton(wrapper).trigger("click");
		expect(save).toHaveBeenCalledWith("5000");
	});

	it("takes the currency symbol from the company settings", () => {
		const { wrapper } = render({ settings: { ...SETTINGS, currency: "USD" } });
		expect(wrapper.get(".currency-field span").text()).toBe("$");
	});
});
