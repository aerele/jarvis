import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// frappe-ui's ESM entry does not resolve under vitest (see every other spec
// in this app that imports from "frappe-ui").
vi.mock("frappe-ui", () => ({
	FormControl: {
		name: "FormControl",
		props: ["type", "label", "options", "modelValue", "placeholder", "ariaLabel"],
		emits: ["update:modelValue"],
		template: `<input class="fc-input" :type="type" :placeholder="placeholder" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "loading", "modelValue", "placeholder", "multiple"],
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
	DatePicker: { name: "DatePicker", template: "<div />" },
	Badge: { name: "Badge", template: "<div><slot /><slot name='suffix' /></div>" },
	FeatherIcon: { name: "FeatherIcon", template: "<i />" },
}));

const api = vi.hoisted(() => ({ searchLink: vi.fn().mockResolvedValue([]) }));
vi.mock("@/api", () => api);

import FilterValueControl from "./FilterValueControl.vue";

// jarvis#1062 (composable extraction, useLinkSearch): FilterValueControl.vue's
// Link search was the third of three near-identical debounced+fenced+primed
// remote search copies, refactored onto the shared composable. It had no
// spec at all before this - these cover the Link family end to end so the
// refactor is actually verified, not just "the other two sites' tests still
// pass".
function linkEntry(overrides = {}) {
	return {
		fieldtype: "Link",
		doctype: "Row DocType",
		fieldname: "company",
		options: "Company",
		label: "Company",
		...overrides,
	};
}

function mountControl(entry, clauseOverrides = {}) {
	return mount(FilterValueControl, {
		props: {
			entry,
			clause: { fieldname: "company", operator: "=", value: "", ...clauseOverrides },
		},
	});
}

describe("FilterValueControl: Link search", () => {
	beforeEach(() => {
		vi.useFakeTimers();
		api.searchLink.mockReset().mockResolvedValue([]);
	});
	afterEach(() => vi.useRealTimers());

	it("primes on mount - the first page loads without a keystroke", async () => {
		api.searchLink.mockResolvedValue([{ value: "Acme Ltd" }]);
		const w = mountControl(linkEntry());
		await vi.runAllTimersAsync();
		await flushPromises();
		expect(api.searchLink).toHaveBeenCalledWith("Company", "", 20, "Row DocType", "company");
		expect(w.find('button[data-option="Acme Ltd"]').exists()).toBe(true);
	});

	it("debounces typing and picking a result emits the name, with the label kept as display", async () => {
		api.searchLink.mockResolvedValue([{ value: "Acme Ltd" }]);
		const w = mountControl(linkEntry());
		await vi.runAllTimersAsync();
		await flushPromises();
		api.searchLink.mockClear();

		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:query", "ac");
		expect(api.searchLink).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(300);
		await flushPromises();
		expect(api.searchLink).toHaveBeenCalledWith("Company", "ac", 20, "Row DocType", "company");

		await w.find('button[data-option="Acme Ltd"]').trigger("click");
		const emitted = w.emitted("update:value");
		expect(emitted[emitted.length - 1][0]).toEqual({
			value: "Acme Ltd",
			display: null,
			immediate: true,
		});
	});

	it("sets a fallback reason and degrades to a text input when a typed query matches nothing, then recovers once results return", async () => {
		const w = mountControl(linkEntry());
		await vi.runAllTimersAsync();
		await flushPromises();

		api.searchLink.mockResolvedValueOnce([]);
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:query", "zz");
		await vi.advanceTimersByTimeAsync(300);
		await flushPromises();
		expect(w.text()).toContain("No Company matched. Enter the name directly.");
		// the Link picker itself is gone - a single Link degrades to a name input
		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(false);

		api.searchLink.mockResolvedValueOnce([{ value: "Acme Ltd" }]);
		await w.find(".fc-input").setValue("ac");
		await vi.advanceTimersByTimeAsync(300);
		await flushPromises();
		expect(w.text()).not.toContain("No Company matched");
		// results came back - the picker is offered again
		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(true);
	});

	it("re-primes on a field/operator switch instead of keeping the old target's stale menu", async () => {
		api.searchLink.mockResolvedValue([{ value: "Acme Ltd" }]);
		const w = mountControl(linkEntry());
		await vi.runAllTimersAsync();
		await flushPromises();
		expect(api.searchLink).toHaveBeenCalledTimes(1);

		await w.setProps({
			entry: linkEntry({ fieldname: "vendor", options: "Supplier" }),
			clause: { fieldname: "vendor", operator: "=", value: "" },
		});
		await vi.runAllTimersAsync();
		await flushPromises();
		expect(api.searchLink).toHaveBeenCalledTimes(2);
		expect(api.searchLink).toHaveBeenLastCalledWith(
			"Supplier",
			"",
			20,
			"Row DocType",
			"vendor"
		);
	});
});
