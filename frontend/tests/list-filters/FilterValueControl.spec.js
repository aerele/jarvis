import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { frappeUiStubs } from "./stubs.js";
import { OWNER, SCOPE, CREATION, DESCRIPTION, ENABLED } from "./fixtures.js";

vi.mock("frappe-ui", () => frappeUiStubs());

const apiDouble = vi.hoisted(() => ({ searchLink: vi.fn(async () => []) }));
vi.mock("@/api", () => apiDouble);

import FilterValueControl from "@/components/list/FilterValueControl.vue";
import { clauseForEntry, setOperator } from "@/components/list/filterModel";

function mountControl(entry, clause, props = {}) {
	return mount(FilterValueControl, { props: { entry, clause, ...props } });
}
const patch = (w) => {
	const events = w.emitted("update:value");
	return events ? events[events.length - 1][0] : null;
};

beforeEach(() => {
	apiDouble.searchLink.mockReset();
	apiDouble.searchLink.mockResolvedValue([]);
});

describe("Link search", () => {
	afterEach(() => vi.useRealTimers());

	it("debounces the query and asks for the plan's 20-suggestion cap", async () => {
		vi.useFakeTimers();
		const w = mountControl(OWNER, clauseForEntry(OWNER));
		const picker = w.findComponent({ name: "Autocomplete" });
		picker.vm.$emit("update:query", "a");
		picker.vm.$emit("update:query", "an");
		picker.vm.$emit("update:query", "ann");
		expect(apiDouble.searchLink).not.toHaveBeenCalled();
		vi.advanceTimersByTime(300);
		expect(apiDouble.searchLink).toHaveBeenCalledTimes(1);
		expect(apiDouble.searchLink).toHaveBeenCalledWith("User", "ann", 20);
	});

	// The defect this fences: a slow "an" response landing AFTER "ann" and
	// repainting the dropdown with options for a query the user has left behind.
	it("ignores a stale response that resolves after a newer one", async () => {
		vi.useFakeTimers();
		const settle = [];
		apiDouble.searchLink.mockImplementation(
			() => new Promise((resolve) => settle.push(resolve))
		);
		const w = mountControl(OWNER, clauseForEntry(OWNER));
		const picker = w.findComponent({ name: "Autocomplete" });

		picker.vm.$emit("update:query", "an");
		vi.advanceTimersByTime(300);
		picker.vm.$emit("update:query", "ann");
		vi.advanceTimersByTime(300);
		expect(settle).toHaveLength(2);

		settle[1]([{ value: "ann@x.com", label: "Ann Fresh" }]); // newer, first
		await flushPromises();
		settle[0]([{ value: "an@x.com", label: "An Stale" }]); // older, second
		await flushPromises();

		const options = w.findComponent({ name: "Autocomplete" }).props("options");
		expect(options.map((o) => o.value)).toEqual(["ann@x.com"]);
	});

	it("shows the title and submits the stable document name", async () => {
		apiDouble.searchLink.mockResolvedValue([{ value: "ann@x.com", label: "Ann Fresh" }]);
		const w = mountControl(OWNER, clauseForEntry(OWNER));
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:modelValue", {
			label: "Ann Fresh",
			value: "ann@x.com",
		});
		expect(patch(w)).toEqual({ value: "ann@x.com", display: "Ann Fresh" });
	});

	it("keeps the row usable when the caller may not search the target DocType", async () => {
		vi.useFakeTimers();
		apiDouble.searchLink.mockRejectedValue(new Error("PermissionError"));
		const w = mountControl(OWNER, clauseForEntry(OWNER));
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:query", "x");
		vi.advanceTimersByTime(300);
		await flushPromises();
		expect(w.findComponent({ name: "Autocomplete" }).props("options")).toEqual([]);
		expect(w.findComponent({ name: "Autocomplete" }).props("loading")).toBe(false);
	});

	it("drops suggestions that belonged to the previous field", async () => {
		apiDouble.searchLink.mockResolvedValue([{ value: "ann@x.com", label: "Ann" }]);
		const clause = clauseForEntry(OWNER);
		const w = mountControl(OWNER, clause);
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:query", "a");
		await new Promise((r) => setTimeout(r, 350));
		await flushPromises();
		expect(w.findComponent({ name: "Autocomplete" }).props("options")).toHaveLength(1);

		await w.setProps({
			entry: { ...OWNER, fieldname: "modified_by", label: "Last Updated By" },
			clause: { ...clause, fieldname: "modified_by" },
		});
		expect(w.findComponent({ name: "Autocomplete" }).props("options")).toEqual([]);
	});

	it("clears to nothing rather than to a stale name", () => {
		const w = mountControl(OWNER, { ...clauseForEntry(OWNER), value: "a@x.com", display: "A" });
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:modelValue", null);
		expect(patch(w)).toEqual({ value: "", display: null });
	});
});

describe("multi-value", () => {
	it("adds a chip on Enter and on comma, ignoring blanks and duplicates", async () => {
		const clause = setOperator(clauseForEntry(DESCRIPTION), "in");
		const w = mountControl(DESCRIPTION, clause);
		const input = w.find('input[type="text"]');

		await input.setValue("  alpha  ");
		await input.trigger("keydown", { key: "Enter" });
		expect(patch(w)).toEqual({ value: ["alpha"] });

		await w.setProps({ clause: { ...clause, value: ["alpha"] } });
		await input.setValue("alpha");
		await input.trigger("keydown", { key: "Enter" });
		expect(w.emitted("update:value")).toHaveLength(1); // duplicate ignored

		await input.setValue("beta");
		await input.trigger("keydown", { key: "," });
		expect(patch(w)).toEqual({ value: ["alpha", "beta"] });
	});

	it("removes the last chip on Backspace in an empty draft", async () => {
		const clause = { ...setOperator(clauseForEntry(DESCRIPTION), "in"), value: ["a", "b"] };
		const w = mountControl(DESCRIPTION, clause);
		await w.find('input[type="text"]').trigger("keydown", { key: "Backspace" });
		expect(patch(w)).toEqual({ value: ["a"] });
	});

	it("removes the chip whose X was pressed", async () => {
		const clause = { ...setOperator(clauseForEntry(DESCRIPTION), "in"), value: ["a", "b", "c"] };
		const w = mountControl(DESCRIPTION, clause);
		await w.find('button[aria-label="Remove b"]').trigger("click");
		expect(patch(w)).toEqual({ value: ["a", "c"] });
	});

	it("stops at the server's per-condition value cap", async () => {
		const values = [];
		for (let i = 0; i < 3; i += 1) values.push(`v${i}`);
		const clause = { ...setOperator(clauseForEntry(DESCRIPTION), "in"), value: values };
		const w = mountControl(DESCRIPTION, clause, { maxValues: 3 });
		const input = w.find('input[type="text"]');
		expect(input.attributes("placeholder")).toBe("Limit reached");
		await input.setValue("v3");
		await input.trigger("keydown", { key: "Enter" });
		expect(w.emitted("update:value")).toBeUndefined();
	});

	it("a Select `in` picks from the metadata options, blank labelled", () => {
		const w = mountControl(SCOPE, setOperator(clauseForEntry(SCOPE), "in"));
		const picker = w.findComponent({ name: "Autocomplete" });
		expect(picker.props("multiple")).toBe(true);
		expect(picker.props("options").map((o) => o.label)).toEqual(["Not set", "Org", "Personal"]);
	});
});

describe("scalar families", () => {
	it("emits each Between bound independently and never loses the other", async () => {
		const clause = clauseForEntry(CREATION);
		const w = mountControl(CREATION, clause);
		const bounds = w.findAll('input[type="datetime-local"]');
		await bounds[0].setValue("2026-01-01T09:30");
		expect(patch(w)).toEqual({ value: ["2026-01-01 09:30:00", ""] });

		await w.setProps({ clause: { ...clause, value: ["2026-01-01 09:30:00", ""] } });
		// and the stored value round-trips back into the input's own T-form
		expect(w.findAll('input[type="datetime-local"]')[0].element.value).toBe("2026-01-01T09:30");
		await w.findAll('input[type="datetime-local"]')[1].setValue("2026-02-01T17:45");
		expect(patch(w)).toEqual({ value: ["2026-01-01 09:30:00", "2026-02-01 17:45:00"] });
	});

	it("a Date Between uses the repo's DatePicker, not a raw text box", () => {
		const dateField = { ...CREATION, fieldtype: "Date" };
		const w = mountControl(dateField, { ...clauseForEntry(CREATION), operator: "Between" });
		expect(w.findAll(".date-picker")).toHaveLength(2);
	});

	it("Check emits the 1/0 the compiler expects", async () => {
		const w = mountControl(ENABLED, clauseForEntry(ENABLED));
		await w.find("select").setValue("0");
		expect(patch(w)).toEqual({ value: "0", display: null });
	});
});
