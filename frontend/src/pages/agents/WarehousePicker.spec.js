import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import WarehousePicker from "./WarehousePicker.vue";

describe("WarehousePicker", () => {
	it("keeps warehouse names as separate array values and leaves its inputs unchanged", async () => {
		const selected = ["Fake store, north"],
			options = ["Fake store, north", "Fake store south"];
		const wrapper = mount(WarehousePicker, { props: { modelValue: selected, options } });
		await wrapper.findAll('input[type="checkbox"]')[1].setValue(true);
		expect(wrapper.emitted("update:modelValue").at(-1)[0]).toEqual(options);
		expect(selected).toEqual(["Fake store, north"]);
		expect(options).toEqual(["Fake store, north", "Fake store south"]);
	});

	it("filters visible choices without dropping selected warehouses", async () => {
		const wrapper = mount(WarehousePicker, {
			props: { modelValue: ["Fake north"], options: ["Fake north", "Fake south"] },
		});
		await wrapper.get('input[type="search"]').setValue("south");
		expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(1);
		expect(wrapper.get('ul[aria-label="Selected warehouses"]').text()).toContain("Fake north");
		await wrapper.get('button[aria-label="Remove Fake north"]').trigger("click");
		expect(wrapper.emitted("update:modelValue").at(-1)[0]).toEqual([]);
	});

	it("keeps chosen values through loading and empty list states", async () => {
		const wrapper = mount(WarehousePicker, {
			props: { modelValue: ["Fake retained"], options: [], loading: true },
		});
		expect(wrapper.text()).toContain("Loading warehouses");
		expect(wrapper.text()).toContain("Fake retained");
		await wrapper.setProps({ loading: false });
		expect(wrapper.text()).toContain("load its warehouse list");
		expect(wrapper.emitted("update:modelValue")).toBeUndefined();
	});

	it("allows removal at the ten-warehouse limit and prevents an extra choice", async () => {
		const selected = Array.from({ length: 10 }, (_, i) => `Fake ${i}`);
		const wrapper = mount(WarehousePicker, {
			props: { modelValue: selected, options: [...selected, "Fake tail"] },
		});
		const checkboxes = wrapper.findAll('input[type="checkbox"]');
		expect(checkboxes.at(-1).element.disabled).toBe(true);
		expect(checkboxes[0].element.disabled).toBe(false);
		await checkboxes[0].setValue(false);
		expect(wrapper.emitted("update:modelValue").at(-1)[0]).toEqual(selected.slice(1));
	});

	it("renders names as text and honours the saving lock", async () => {
		const name = '<img src=x onerror="bad()">';
		const wrapper = mount(WarehousePicker, {
			props: { modelValue: [name], options: [name], disabled: true },
		});
		expect(wrapper.find("img").exists()).toBe(false);
		expect(wrapper.text()).toContain(name);
		expect(wrapper.get("fieldset").element.disabled).toBe(true);
		await wrapper.get("button").trigger("click");
		expect(wrapper.emitted("update:modelValue")).toBeUndefined();
	});
});
