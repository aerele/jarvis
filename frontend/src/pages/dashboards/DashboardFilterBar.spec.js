import { describe, it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// frappe-ui's ESM entry does not resolve under vitest (see every other spec
// in this app that imports from "frappe-ui", e.g. FilterValueControl.spec.js).
vi.mock("frappe-ui", () => ({
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "loading", "modelValue", "placeholder", "bodyClasses"],
		emits: ["update:query", "update:modelValue"],
		template: `<div>
			<input :placeholder="placeholder" @focus="$emit('update:query', '')" />
			<button
				v-for="o in options"
				:key="o.value"
				:data-option="o.value"
				@click="$emit('update:modelValue', o)"
			>{{ o.label }}</button>
		</div>`,
	},
}));

vi.mock("@/api", () => ({
	searchLink: vi.fn(async () => [{ value: "ITEM-001", description: "Widget" }]),
}));
import DashboardFilterBar from "./DashboardFilterBar.vue";

const DEFS = [
	{ fieldname: "item", label: "Item", fieldtype: "Link", options: "Item", default: "", reqd: 1 },
];

describe("DashboardFilterBar", () => {
	it("renders nothing without defs", () => {
		const w = mount(DashboardFilterBar, { props: { defs: [], modelValue: {} } });
		expect(w.html()).toBe("<!--v-if-->");
	});
	it("renders one control per def with its label and required marker", () => {
		const w = mount(DashboardFilterBar, { props: { defs: DEFS, modelValue: { item: "" } } });
		expect(w.text()).toContain("Item");
		expect(w.find("[data-reqd]").exists()).toBe(true);
	});
	it("emits update:modelValue with the picked name", async () => {
		const w = mount(DashboardFilterBar, { props: { defs: DEFS, modelValue: { item: "" } } });
		await w.vm.pick("item", { value: "ITEM-001", label: "Widget" });
		expect(w.emitted("update:modelValue")[0][0]).toEqual({ item: "ITEM-001" });
	});
	it("marks a control when errors flags it", () => {
		const w = mount(DashboardFilterBar, {
			props: { defs: DEFS, modelValue: { item: "" }, errors: { item: true } },
		});
		expect(w.find("[data-error]").exists()).toBe(true);
	});
});
