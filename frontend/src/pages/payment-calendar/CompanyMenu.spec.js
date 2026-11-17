import { afterEach, describe, expect, it } from "vitest";
import { defineComponent, nextTick, ref } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import CompanyMenu from "./CompanyMenu.vue";
import { provideOverlayStack } from "./overlayStack";

const COMPANIES = [
	{ name: "Northstar Manufacturing Pvt Ltd" },
	{ name: "Northstar Services Pvt Ltd" },
	{ name: "Northstar Exports Pvt Ltd" },
];

// A page-like host: it owns `open`, as the filter bar does, and the shared Escape stack.
const Host = defineComponent({
	components: { CompanyMenu },
	props: { company: { type: String, default: COMPANIES[0].name }, disabled: Boolean },
	emits: ["select"],
	setup() {
		provideOverlayStack();
		return { open: ref(false), companies: COMPANIES };
	},
	template: `<div><button id="outside">outside</button>
		<CompanyMenu v-model:open="open" :companies="companies" :model-value="company" :disabled="disabled" @select="$emit('select', $event)" /></div>`,
});

let wrapper;
afterEach(() => {
	wrapper?.unmount();
	document.body.innerHTML = "";
});
const render = (props = {}) => {
	wrapper = mount(Host, { props, attachTo: document.body });
	return wrapper;
};
const trigger = () => wrapper.get("#company-trigger");
const options = () => wrapper.findAll('#company-options [role="option"]');
const open = async () => {
	await trigger().trigger("click");
	await flushPromises();
};
const key = (target, name) =>
	target.dispatchEvent(
		new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true })
	);

describe("CompanyMenu", () => {
	it("shows the selected company on the trigger", () => {
		render();
		expect(wrapper.get("#company-label").text()).toBe("Company");
		expect(wrapper.get("#company-value").text()).toBe("Northstar Manufacturing Pvt Ltd");
		expect(trigger().attributes("aria-haspopup")).toBe("listbox");
		expect(trigger().attributes("aria-expanded")).toBe("false");
		expect(trigger().attributes("aria-labelledby")).toBe("company-label company-value");
		expect(wrapper.find("#company-menu").exists()).toBe(false);
	});

	it("asks for a company when none is chosen", () => {
		render({ company: "" });
		expect(wrapper.get("#company-value").text()).toBe("Select a company");
	});

	it("opens with a cleared, focused search and every company listed", async () => {
		render();
		await open();
		expect(trigger().attributes("aria-expanded")).toBe("true");
		const search = wrapper.get("#company-search");
		expect(search.attributes("role")).toBe("combobox");
		expect(search.attributes("aria-label")).toBe("Search companies");
		expect(search.attributes("placeholder")).toBe("Search companies…");
		expect(search.element.value).toBe("");
		expect(document.activeElement).toBe(search.element);
		expect(wrapper.get("#company-options").attributes("role")).toBe("listbox");
		expect(wrapper.get("#company-options").attributes("aria-label")).toBe("Companies");
		expect(options().map((option) => option.text())).toEqual([
			"Northstar Manufacturing Pvt Ltd✓",
			"Northstar Services Pvt Ltd",
			"Northstar Exports Pvt Ltd",
		]);
		expect(options()[0].attributes("aria-selected")).toBe("true");
		expect(options()[1].attributes("aria-selected")).toBe("false");
	});

	it("toggles closed on a second click", async () => {
		render();
		await open();
		await open();
		expect(wrapper.find("#company-menu").exists()).toBe(false);
	});

	it("filters by a case-insensitive substring", async () => {
		render();
		await open();
		await wrapper.get("#company-search").setValue("SERVICES");
		expect(options().map((option) => option.text())).toEqual(["Northstar Services Pvt Ltd"]);
		await wrapper.get("#company-search").setValue("nothing");
		expect(options()).toHaveLength(0);
		expect(wrapper.get("#company-options").text()).toBe("No matching companies.");
	});

	it("moves from the search to the first option with ArrowDown, then cycles", async () => {
		render();
		await open();
		await wrapper.get("#company-search").trigger("keydown", { key: "ArrowDown" });
		expect(document.activeElement).toBe(options()[0].element);
		await options()[0].trigger("keydown", { key: "ArrowDown" });
		expect(document.activeElement).toBe(options()[1].element);
		await options()[1].trigger("keydown", { key: "ArrowDown" });
		await options()[2].trigger("keydown", { key: "ArrowDown" });
		expect(document.activeElement).toBe(options()[0].element);
		await options()[0].trigger("keydown", { key: "ArrowUp" });
		expect(document.activeElement).toBe(options()[2].element);
	});

	it("selects on click, closes and returns focus to the trigger", async () => {
		render();
		await open();
		await options()[1].trigger("click");
		await flushPromises();
		expect(wrapper.emitted("select")).toEqual([["Northstar Services Pvt Ltd"]]);
		expect(wrapper.find("#company-menu").exists()).toBe(false);
		expect(document.activeElement).toBe(trigger().element);
	});

	it("closes on Escape and focuses the trigger", async () => {
		render();
		await open();
		key(document.activeElement, "Escape");
		await flushPromises();
		expect(wrapper.find("#company-menu").exists()).toBe(false);
		expect(document.activeElement).toBe(trigger().element);
		expect(wrapper.emitted("select")).toBeUndefined();
	});

	it("closes on a pointerdown outside, without taking focus", async () => {
		render();
		await open();
		const outside = wrapper.get("#outside").element;
		outside.focus();
		outside.dispatchEvent(new Event("pointerdown", { bubbles: true }));
		await nextTick();
		expect(wrapper.find("#company-menu").exists()).toBe(false);
		expect(document.activeElement).toBe(outside);
	});

	it("stays open on a pointerdown inside the menu", async () => {
		render();
		await open();
		wrapper
			.get("#company-search")
			.element.dispatchEvent(new Event("pointerdown", { bubbles: true }));
		await nextTick();
		expect(wrapper.find("#company-menu").exists()).toBe(true);
	});

	it("cannot open while disabled", async () => {
		render({ disabled: true });
		expect(trigger().attributes("disabled")).toBeDefined();
	});
});
