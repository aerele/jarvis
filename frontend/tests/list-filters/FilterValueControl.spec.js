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
		// P3-2: the dropdown is not empty when it opens — one query fires on mount
		expect(apiDouble.searchLink).toHaveBeenCalledWith("User", "", 20);
		apiDouble.searchLink.mockClear();
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
		expect(settle).toHaveLength(3); // [0] is the on-open query

		settle[2]([{ value: "ann@x.com", label: "Ann Fresh" }]); // newer, first
		await flushPromises();
		settle[1]([{ value: "an@x.com", label: "An Stale" }]); // older, second
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
		expect(patch(w)).toEqual({ value: "ann@x.com", display: "Ann Fresh", immediate: true });
	});

	// P2-5: the comment always claimed the row stays usable by typing a name.
	// Now it is true — the control actually becomes that input.
	it("falls back to a plain name input when the caller may not search the DocType", async () => {
		apiDouble.searchLink.mockRejectedValue(new Error("PermissionError"));
		const w = mountControl(OWNER, clauseForEntry(OWNER));
		await flushPromises();
		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(false);
		const input = w.find('input[type="text"]');
		expect(input.exists()).toBe(true);
		expect(w.text()).toContain("Can't search User — enter the name directly.");
		await input.setValue("someone@x.com");
		expect(patch(w)).toEqual({ value: "someone@x.com", display: null, immediate: false });
	});

	it("falls back when a typed query matches nothing, and back again on a new field", async () => {
		apiDouble.searchLink.mockResolvedValue([]);
		const clause = clauseForEntry(OWNER);
		const w = mountControl(OWNER, clause);
		await flushPromises();
		// the on-open query returning nothing is NOT a failure — no query was asked
		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(true);

		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:query", "zzz");
		await new Promise((r) => setTimeout(r, 350));
		await flushPromises();
		expect(w.find('input[type="text"]').exists()).toBe(true);
		expect(w.text()).toContain("No User matched — enter the name directly.");

		await w.setProps({
			entry: { ...OWNER, fieldname: "modified_by", label: "Last Updated By" },
			clause: { ...clause, fieldname: "modified_by" },
		});
		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(true);
	});

	// R1: the fallback must be a detour, not a one-way door. A typo used to
	// downgrade the row to a text input for the rest of its life.
	it("restores the picker as soon as a query matches again", async () => {
		apiDouble.searchLink.mockResolvedValue([]);
		const w = mountControl(OWNER, clauseForEntry(OWNER));
		await flushPromises();

		// a typo: nothing matches, the row degrades
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:query", "annn");
		await new Promise((r) => setTimeout(r, 350));
		await flushPromises();
		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(false);
		expect(w.text()).toContain("No User matched — enter the name directly.");

		// the correction is typed into the fallback input, which keeps searching
		apiDouble.searchLink.mockResolvedValue([{ value: "ann@x.com", label: "Ann" }]);
		await w.find('input[type="text"]').setValue("ann");
		await new Promise((r) => setTimeout(r, 350));
		await flushPromises();

		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(true);
		expect(w.text()).not.toContain("enter the name directly");
		expect(w.findComponent({ name: "Autocomplete" }).props("options")).toHaveLength(1);
	});

	it("recovers a multi-Link too, whose fallback is chips so the list stays a list", async () => {
		apiDouble.searchLink.mockResolvedValue([]);
		const clause = setOperator(clauseForEntry(OWNER), "in");
		const w = mountControl(OWNER, clause);
		await flushPromises();
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:query", "zzz");
		await new Promise((r) => setTimeout(r, 350));
		await flushPromises();
		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(false);

		// chips, not a bare text box: an `in` clause must not come back a string
		await w.find('input[type="text"]').setValue("someone@x.com");
		await w.find('input[type="text"]').trigger("keydown", { key: "Enter" });
		expect(patch(w)).toEqual({ value: ["someone@x.com"], immediate: true });

		apiDouble.searchLink.mockResolvedValue([{ value: "ann@x.com", label: "Ann" }]);
		await w.find('input[type="text"]').setValue("ann");
		await new Promise((r) => setTimeout(r, 350));
		await flushPromises();
		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(true);
	});

	it("names both Autocomplete controls the way the 16 plain ones are named", async () => {
		apiDouble.searchLink.mockResolvedValue([]);
		const link = mountControl(OWNER, clauseForEntry(OWNER), { rowName: "Created By (filter 2)" });
		await flushPromises();
		expect(link.find('[role="group"]').attributes("aria-label")).toBe(
			"Value for Created By (filter 2)"
		);

		const multi = mountControl(SCOPE, setOperator(clauseForEntry(SCOPE), "in"));
		expect(multi.find('[role="group"]').attributes("aria-label")).toBe("Value for Scope");
	});

	it("has no picker at all for a Dynamic Link, whose target is not yet known", () => {
		const dynamic = { ...OWNER, fieldtype: "Dynamic Link", options: "ref_doctype" };
		const w = mountControl(dynamic, { ...clauseForEntry(OWNER), operator: "=" });
		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(false);
		expect(w.find('input[type="text"]').exists()).toBe(true);
	});

	it("drops suggestions that belonged to the previous field", async () => {
		apiDouble.searchLink.mockResolvedValue([{ value: "ann@x.com", label: "Ann" }]);
		const clause = clauseForEntry(OWNER);
		const w = mountControl(OWNER, clause);
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:query", "a");
		await new Promise((r) => setTimeout(r, 350));
		await flushPromises();
		expect(w.findComponent({ name: "Autocomplete" }).props("options")).toHaveLength(1);

		apiDouble.searchLink.mockResolvedValue([]);
		await w.setProps({
			entry: { ...OWNER, fieldname: "modified_by", label: "Last Updated By" },
			clause: { ...clause, fieldname: "modified_by" },
		});
		// synchronously emptied; the fresh query for the NEW field follows
		expect(w.findComponent({ name: "Autocomplete" }).props("options")).toEqual([]);
	});

	it("clears to nothing rather than to a stale name", () => {
		const w = mountControl(OWNER, { ...clauseForEntry(OWNER), value: "a@x.com", display: "A" });
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:modelValue", null);
		expect(patch(w)).toEqual({ value: "", display: null, immediate: true });
	});
});

describe("multi-value", () => {
	it("adds a chip on Enter and on comma, ignoring blanks and duplicates", async () => {
		const clause = setOperator(clauseForEntry(DESCRIPTION), "in");
		const w = mountControl(DESCRIPTION, clause);
		const input = w.find('input[type="text"]');

		await input.setValue("  alpha  ");
		await input.trigger("keydown", { key: "Enter" });
		expect(patch(w)).toEqual({ value: ["alpha"], immediate: true });

		await w.setProps({ clause: { ...clause, value: ["alpha"] } });
		await input.setValue("alpha");
		await input.trigger("keydown", { key: "Enter" });
		expect(w.emitted("update:value")).toHaveLength(1); // duplicate ignored

		await input.setValue("beta");
		await input.trigger("keydown", { key: "," });
		expect(patch(w)).toEqual({ value: ["alpha", "beta"], immediate: true });
	});

	it("removes the last chip on Backspace in an empty draft", async () => {
		const clause = { ...setOperator(clauseForEntry(DESCRIPTION), "in"), value: ["a", "b"] };
		const w = mountControl(DESCRIPTION, clause);
		await w.find('input[type="text"]').trigger("keydown", { key: "Backspace" });
		expect(patch(w)).toEqual({ value: ["a"], immediate: true });
	});

	it("removes the chip whose X was pressed", async () => {
		const clause = { ...setOperator(clauseForEntry(DESCRIPTION), "in"), value: ["a", "b", "c"] };
		const w = mountControl(DESCRIPTION, clause);
		await w.find('button[aria-label="Remove b"]').trigger("click");
		expect(patch(w)).toEqual({ value: ["a", "c"], immediate: true });
	});

	// U2: a refusal that clears the box looks exactly like a successful add whose
	// chip failed to render — the user retypes the same value and watches it
	// vanish again.
	it("keeps the typed text and says why on a duplicate", async () => {
		const clause = { ...setOperator(clauseForEntry(DESCRIPTION), "in"), value: ["alpha"] };
		const w = mountControl(DESCRIPTION, clause);
		const input = w.find('input[type="text"]');
		await input.setValue("alpha");
		await input.trigger("keydown", { key: "Enter" });
		expect(w.emitted("update:value")).toBeUndefined();
		expect(w.text()).toContain('"alpha" is already in this filter.');
		expect(input.element.value).toBe("alpha");

		// editing the draft clears the note
		await input.setValue("alph");
		expect(w.text()).not.toContain("already in this filter");
	});

	it("says the cap was hit instead of swallowing the value", async () => {
		const clause = { ...setOperator(clauseForEntry(DESCRIPTION), "in"), value: ["a", "b"] };
		const w = mountControl(DESCRIPTION, clause, { maxValues: 2 });
		const input = w.find('input[type="text"]');
		await input.setValue("c");
		await input.trigger("keydown", { key: "Enter" });
		expect(w.emitted("update:value")).toBeUndefined();
		expect(w.text()).toContain("Limit reached — 2 values is the maximum.");
		expect(input.element.value).toBe("c");
	});

	it("reports truncation on a multi-select pick, not just on chips", async () => {
		const w = mountControl(SCOPE, setOperator(clauseForEntry(SCOPE), "in"), { maxValues: 1 });
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:modelValue", [
			{ label: "Org", value: "Org" },
			{ label: "Personal", value: "Personal" },
		]);
		await w.vm.$nextTick();
		expect(patch(w).value).toEqual(["Org"]);
		expect(w.text()).toContain("Limit reached — only the first 1 values are used.");
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
		expect(w.text()).toContain("Limit reached");
	});

	it("a Select `in` picks from the metadata options, blank labelled", () => {
		const w = mountControl(SCOPE, setOperator(clauseForEntry(SCOPE), "in"));
		const picker = w.findComponent({ name: "Autocomplete" });
		expect(picker.props("multiple")).toBe(true);
		expect(picker.props("options").map((o) => o.label)).toEqual(["Not set", "Org", "Personal"]);
	});
});

describe("scalar families", () => {
	it("sentence-cases the timespan menu without touching the wire token", () => {
		const w = mountControl(CREATION, setOperator(clauseForEntry(CREATION), "Timespan"));
		const options = w.find("select").findAll("option");
		const pairs = options.map((o) => [o.text(), o.attributes("value")]);
		expect(pairs).toContainEqual(["Last 7 days", "last 7 days"]);
		expect(pairs).toContainEqual(["This quarter", "this quarter"]);
	});

	it("marks a typed number as debounceable and a picked date as immediate", async () => {
		const number = mountControl(
			{ ...CREATION, fieldtype: "Int", label: "Index", operators: ["="], default_operator: "=" },
			{ ...clauseForEntry(CREATION), operator: "=", value: "" }
		);
		await number.find('input[type="number"]').setValue("12");
		expect(patch(number).immediate).toBe(false);

		const between = mountControl(CREATION, clauseForEntry(CREATION));
		await between.findAll('input[type="datetime-local"]')[0].setValue("2026-01-01T09:30");
		expect(patch(between).immediate).toBe(true);
	});

	it("emits each Between bound independently and never loses the other", async () => {
		const clause = clauseForEntry(CREATION);
		const w = mountControl(CREATION, clause);
		const bounds = w.findAll('input[type="datetime-local"]');
		await bounds[0].setValue("2026-01-01T09:30");
		expect(patch(w)).toEqual({ value: ["2026-01-01 09:30:00", ""], immediate: true });

		await w.setProps({ clause: { ...clause, value: ["2026-01-01 09:30:00", ""] } });
		// and the stored value round-trips back into the input's own T-form
		expect(w.findAll('input[type="datetime-local"]')[0].element.value).toBe("2026-01-01T09:30");
		await w.findAll('input[type="datetime-local"]')[1].setValue("2026-02-01T17:45");
		expect(patch(w)).toEqual({
			value: ["2026-01-01 09:30:00", "2026-02-01 17:45:00"],
			immediate: true,
		});
	});

	it("a Date Between uses the repo's DatePicker, not a raw text box", () => {
		const dateField = { ...CREATION, fieldtype: "Date" };
		const w = mountControl(dateField, { ...clauseForEntry(CREATION), operator: "Between" });
		expect(w.findAll(".date-picker")).toHaveLength(2);
	});

	it("Check emits the 1/0 the compiler expects", async () => {
		const w = mountControl(ENABLED, clauseForEntry(ENABLED));
		await w.find("select").setValue("0");
		expect(patch(w)).toEqual({ value: "0", display: null, immediate: true });
	});
});
