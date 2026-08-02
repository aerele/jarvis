import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { frappeUiStubs } from "./stubs.js";
import { SKILLS_SCHEMA, DESCRIPTION, ENABLED, SCOPE, OWNER, CREATION, IDX } from "./fixtures.js";

vi.mock("frappe-ui", () => frappeUiStubs());
vi.mock("@/api", () => ({ searchLink: vi.fn(async () => []) }));

import FilterGroup from "@/components/list/FilterGroup.vue";
import { clauseForEntry, setOperator, fieldKey } from "@/components/list/filterModel";

/**
 * FilterGroup is CONTROLLED: it never mutates its own clause array, it emits a
 * new one. So every mutation test here asserts on the emitted array and then
 * feeds it back with setProps, which is exactly what useListPage does — a test
 * that mutated props in place would prove nothing about the real wiring.
 */
function mountPanel(props = {}) {
	const { attachTo, ...rest } = props;
	return mount(FilterGroup, {
		attachTo,
		props: {
			schema: SKILLS_SCHEMA,
			schemaState: "ready",
			clauses: [],
			...rest,
		},
	});
}

const lastClauses = (w) => {
	const events = w.emitted("update:clauses");
	return events ? events[events.length - 1][0] : null;
};

const withValue = (entry, value, extra = {}) => ({
	...clauseForEntry(entry),
	value,
	...extra,
});

beforeEach(() => {
	vi.clearAllMocks();
});

describe("rows", () => {
	it("reads Where for the first clause and And for the rest", () => {
		const w = mountPanel({
			clauses: [withValue(DESCRIPTION, "month"), withValue(ENABLED, "1")],
		});
		const text = w.text();
		expect(text).toContain("Where");
		expect(text).toContain("And");
	});

	it("shows the empty prompt with no rows", () => {
		expect(mountPanel().text()).toContain("Empty - Choose a field to filter by");
	});

	it("offers each field's OWN operator list, with the server's default preselected", () => {
		const w = mountPanel({ clauses: [clauseForEntry(ENABLED), clauseForEntry(CREATION)] });
		const selects = w.findAll("select");
		// Check exposes Equals only — the panel must not offer `like` on it.
		const checkOperators = selects[0].findAll("option").map((o) => o.attributes("value"));
		expect(checkOperators).toEqual(["="]);
		// Datetime's default is Between (D5), and it never offers like/in.
		const dtOperators = w.findAll("select")[2].findAll("option").map((o) => o.attributes("value"));
		expect(dtOperators).toContain("Between");
		expect(dtOperators).not.toContain("like");
		expect(w.findAll("select")[2].element.value).toBe("Between");
	});

	it("counts COMPLETE clauses, not rows on screen", () => {
		const w = mountPanel({
			clauses: [
				withValue(DESCRIPTION, "month"), // complete
				clauseForEntry(CREATION), // pending: no bounds
				withValue(CREATION, ["2026-01-01", ""]), // pending: one bound
			],
		});
		expect(w.find("button").text()).toContain("1");
	});

	it("says WHY a row is pending instead of silently ignoring it", () => {
		const w = mountPanel({ clauses: [withValue(CREATION, ["2026-01-01", ""])] });
		expect(w.text()).toContain("Needs a start and an end to apply.");
	});

	it("names a field that vanished from the catalog and tells you to remove it", () => {
		const gone = { ...clauseForEntry(DESCRIPTION), fieldname: "skill_bundle", value: "x" };
		const w = mountPanel({ clauses: [gone] });
		expect(w.text()).toContain("skill_bundle (unavailable)");
		expect(w.text()).toContain("This field is no longer available - remove it.");
		expect(w.find("button").text()).not.toContain("1"); // never counted
	});
});

describe("value controls, one per family", () => {
	it("Check → a Yes/No select mapped to 1/0", () => {
		const w = mountPanel({ clauses: [clauseForEntry(ENABLED)] });
		const value = w.findAll("select")[1];
		expect(value.findAll("option").map((o) => [o.text(), o.attributes("value")])).toEqual([
			["Yes", "1"],
			["No", "0"],
		]);
	});

	it("Select → metadata options with the leading blank labelled 'Not set'", () => {
		const w = mountPanel({ clauses: [clauseForEntry(SCOPE)] });
		const value = w.findAll("select")[1];
		expect(value.findAll("option").map((o) => o.text())).toEqual(["Not set", "Org", "Personal"]);
	});

	it("Date/Datetime Between → two bounds, both required", () => {
		const w = mountPanel({ clauses: [clauseForEntry(CREATION)] });
		const bounds = w.findAll('input[type="datetime-local"]');
		expect(bounds).toHaveLength(2);
		expect(bounds[0].attributes("aria-label")).toBe("Value for Created On from");
		expect(bounds[1].attributes("aria-label")).toBe("Value for Created On to");
	});

	it("numeric → a number input", () => {
		const w = mountPanel({ clauses: [clauseForEntry(IDX)] });
		expect(w.find('input[type="number"]').exists()).toBe(true);
	});

	it("Link → an autocomplete over the target DocType, not a free-text box", () => {
		const w = mountPanel({ clauses: [clauseForEntry(OWNER)] });
		expect(w.find('input[type="text"]').exists()).toBe(false);
		// the value control's picker, not the row's field picker
		const value = w.findAllComponents({ name: "Autocomplete" })[1];
		expect(value.props("placeholder")).toBe("Search User…");
		expect(value.props("multiple")).toBe(false);
	});

	it("Link in / not in → the SAME picker in multi mode (plan §5.3)", () => {
		const w = mountPanel({ clauses: [setOperator(clauseForEntry(OWNER), "in")] });
		expect(w.findAllComponents({ name: "Autocomplete" })[1].props("multiple")).toBe(true);
	});

	it("text families → a text input that says how `like` wildcards behave (D13)", () => {
		const w = mountPanel({ clauses: [clauseForEntry(DESCRIPTION)] });
		const input = w.find('input[type="text"]');
		expect(input.attributes("placeholder")).toBe("Contains… (use % to anchor)");
		expect(input.attributes("aria-label")).toBe("Value for Description");
	});

	it("`is` → set / not set, whatever the family", () => {
		const w = mountPanel({ clauses: [setOperator(clauseForEntry(SCOPE), "is")] });
		const value = w.findAll("select")[1];
		expect(value.findAll("option").map((o) => o.attributes("value"))).toEqual(["set", "not set"]);
	});

	it("`in` over a free-text family → chips, never a comma string (D10)", async () => {
		const w = mountPanel({ clauses: [setOperator(clauseForEntry(DESCRIPTION), "in")] });
		const input = w.find('input[type="text"]');
		await input.setValue("alpha");
		await input.trigger("keydown", { key: "Enter" });
		expect(lastClauses(w)[0].value).toEqual(["alpha"]);

		await w.setProps({ clauses: [{ ...w.props("clauses")[0], value: ["alpha", "beta"] }] });
		expect(w.findAll(".badge").map((b) => b.text())).toEqual(["alpha", "beta"]);
	});

	it("Timespan → the server's own token list", () => {
		const w = mountPanel({ clauses: [setOperator(clauseForEntry(CREATION), "Timespan")] });
		const value = w.findAll("select")[1];
		const tokens = value.findAll("option").map((o) => o.attributes("value"));
		expect(tokens).toContain("last week");
		expect(tokens).toContain("this quarter");
	});
});

describe("editing", () => {
	it("adds a row seeded with the field's default operator", () => {
		const w = mountPanel();
		w.findAllComponents({ name: "Autocomplete" }).at(-1).vm.$emit("update:modelValue", {
			value: fieldKey("Jarvis Custom Skill", "creation"),
		});
		expect(lastClauses(w)).toHaveLength(1);
		expect(lastClauses(w)[0]).toMatchObject({
			doctype: "Jarvis Custom Skill",
			fieldname: "creation",
			operator: "Between",
			value: ["", ""],
		});
	});

	it("allows the SAME field twice and keeps both rows independent", async () => {
		const w = mountPanel({ clauses: [withValue(DESCRIPTION, "month")] });
		w.findAllComponents({ name: "Autocomplete" }).at(-1).vm.$emit("update:modelValue", {
			value: fieldKey("Jarvis Custom Skill", "description"),
		});
		const next = lastClauses(w);
		expect(next).toHaveLength(2);
		expect(next[0].id).not.toBe(next[1].id);

		await w.setProps({ clauses: next });
		// change only the second row's operator
		await w.findAll("select")[1].setValue("not like");
		expect(lastClauses(w)[0].operator).toBe("like");
		expect(lastClauses(w)[1].operator).toBe("not like");
	});

	it("removes exactly the row whose X was pressed", async () => {
		const clauses = [withValue(DESCRIPTION, "a"), withValue(ENABLED, "1"), withValue(IDX, "3")];
		const w = mountPanel({ clauses });
		await w.find('button[aria-label="Remove filter on Enabled"]').trigger("click");
		expect(lastClauses(w).map((c) => c.fieldname)).toEqual(["description", "idx"]);
	});

	it("resets the value when the row is pointed at another family", async () => {
		const w = mountPanel({ clauses: [withValue(DESCRIPTION, "month")] });
		w.findAllComponents({ name: "Autocomplete" })[0].vm.$emit("update:modelValue", {
			value: fieldKey("Jarvis Custom Skill", "enabled"),
		});
		expect(lastClauses(w)[0]).toMatchObject({ fieldname: "enabled", operator: "=", value: "" });
	});

	// P2-3: the panel tells useListPage which mutations are still being typed.
	it("marks a typed value as debounceable and every discrete pick as immediate", async () => {
		const w = mountPanel({ clauses: [clauseForEntry(DESCRIPTION)] });
		await w.find('input[type="text"]').setValue("month");
		expect(w.emitted("update:clauses").at(-1)[1]).toEqual({ immediate: false });

		await w.findAll("select")[0].setValue("=");
		expect(w.emitted("update:clauses").at(-1)[1]).toEqual({ immediate: true });

		await w.setProps({ clauses: [clauseForEntry(ENABLED)] });
		await w.findAll("select")[1].setValue("0");
		expect(w.emitted("update:clauses").at(-1)[1]).toEqual({ immediate: true });
	});

	it("never lets the immediacy flag leak into the clause itself", async () => {
		const w = mountPanel({ clauses: [clauseForEntry(DESCRIPTION)] });
		await w.find('input[type="text"]').setValue("month");
		expect(lastClauses(w)[0]).not.toHaveProperty("immediate");
		expect(lastClauses(w)[0].value).toBe("month");
	});

	it("clears everything from either affordance", async () => {
		const w = mountPanel({ clauses: [withValue(DESCRIPTION, "a")] });
		await w.find('button[aria-label="Clear all filters"]').trigger("click");
		expect(lastClauses(w)).toEqual([]);
		const buttons = w.findAll("button").filter((b) => b.text() === "Clear All Filters");
		await buttons[0].trigger("click");
		expect(lastClauses(w)).toEqual([]);
	});

	it("hides the trailing clear-X until at least one clause is complete", () => {
		expect(
			mountPanel({ clauses: [clauseForEntry(DESCRIPTION)] })
				.find('button[aria-label="Clear all filters"]')
				.exists()
		).toBe(false);
		expect(
			mountPanel({ clauses: [withValue(DESCRIPTION, "a")] })
				.find('button[aria-label="Clear all filters"]')
				.exists()
		).toBe(true);
	});
});

describe("schema states", () => {
	// These drive the panel the way a person does — by pressing the Filter
	// button — rather than by hand-emitting the event the panel happens to
	// listen for. The distinction is the whole defect: frappe-ui's Popover does
	// not emit `open` for a trigger inside its own `#target` slot, so a spec
	// that emitted `open` itself passed while the picker sat empty in a browser.
	const openPanel = (w) => w.find('button[aria-label="Filter"]').trigger("click");

	it("asks for the catalog on FIRST open only", async () => {
		const w = mountPanel({ schema: null, schemaState: "idle" });
		expect(w.emitted("request-schema")).toBeUndefined();
		await openPanel(w);
		expect(w.emitted("request-schema")).toHaveLength(1);

		await w.setProps({ schema: SKILLS_SCHEMA, schemaState: "ready" });
		await openPanel(w);
		expect(w.emitted("request-schema")).toHaveLength(1);
	});

	// `update:show` fires on the way down as well as up, and reka-ui's own
	// open path relays it twice. Neither may turn into a second metadata walk.
	it("does not re-ask across an open → close → open cycle", async () => {
		const w = mountPanel({ schema: null, schemaState: "idle" });
		await openPanel(w);
		expect(w.emitted("request-schema")).toHaveLength(1);

		// the parent answers the way useListPage does
		await w.setProps({ schemaState: "loading" });
		await openPanel(w); // closes
		await w.setProps({ schema: SKILLS_SCHEMA, schemaState: "ready" });
		await openPanel(w); // opens again
		expect(w.emitted("request-schema")).toHaveLength(1);
	});

	it("asks once when reka-ui reports the open instead", async () => {
		const w = mountPanel({ schema: null, schemaState: "idle" });
		// The outside-driven path emits update:show twice AND open once; the
		// panel must still make exactly one request out of it.
		w.findComponent({ name: "Popover" }).vm.rekaUpdateOpen(true);
		await nextTick();
		expect(w.emitted("request-schema")).toHaveLength(1);
	});

	it("shows a retry — not a dead end — when the catalog is unavailable", async () => {
		const w = mountPanel({
			schema: null,
			schemaState: "error",
			schemaError: {
				code: "list_filter_schema_unavailable",
				kind: "transient",
				message: "Filters are unavailable for this list right now.",
			},
		});
		expect(w.text()).toContain("Filter fields couldn't be loaded");
		expect(w.text()).toContain("Filters are unavailable for this list right now.");
		const retry = w.findAll("button").find((b) => b.text() === "Try again");
		await retry.trigger("click");
		expect(w.emitted("request-schema")).toHaveLength(1);
	});

	it("still lets you clear stuck clauses while the catalog is down", async () => {
		const w = mountPanel({
			schema: null,
			schemaState: "error",
			clauses: [withValue(DESCRIPTION, "a")],
		});
		const clear = w.findAll("button").find((b) => b.text() === "Clear all filters");
		await clear.trigger("click");
		expect(lastClauses(w)).toEqual([]);
	});

	it("says it is loading rather than showing an empty picker", () => {
		expect(mountPanel({ schema: null, schemaState: "loading" }).text()).toContain(
			"Loading fields…"
		);
	});
});

describe("rejections the server coded", () => {
	it("puts a row-level rejection ON the row it names", () => {
		const clauses = [withValue(DESCRIPTION, "a"), withValue(CREATION, ["2026-01-01", "x"])];
		const w = mountPanel({
			clauses,
			error: {
				code: "list_filter_invalid_value",
				kind: "row",
				message: "Created On needs two valid dates.",
			},
		});
		const errors = w.findAll(".error-message");
		expect(errors).toHaveLength(1);
		expect(errors[0].text()).toBe("Created On needs two valid dates.");
	});

	it("keeps an unattributable rejection at panel level rather than blaming a row", () => {
		const w = mountPanel({
			clauses: [withValue(DESCRIPTION, "a")],
			error: { code: "list_filter_bad_payload", kind: "row", message: "Filters are not valid JSON." },
		});
		expect(w.find(".error-message").text()).toBe("Filters are not valid JSON.");
	});

	it("turns a cap rejection into an inline note", () => {
		const w = mountPanel({
			clauses: [withValue(DESCRIPTION, "a")],
			error: {
				code: "list_filter_too_many_clauses",
				kind: "cap",
				message: "At most 20 filters at a time.",
			},
		});
		expect(w.find(".error-message").text()).toBe("At most 20 filters at a time.");
	});

	it("stops offering + Add Filter at the server's own clause cap", () => {
		const clauses = [];
		for (let i = 0; i < 20; i += 1) clauses.push(withValue(DESCRIPTION, `v${i}`));
		const w = mountPanel({ clauses });
		expect(w.findAll("button").some((b) => b.text() === "+ Add Filter")).toBe(false);
		expect(w.text()).toContain("20 filters is the maximum for one list.");
	});
});

describe("accessibility", () => {
	// Without this, every add and remove drops focus on <body> and a keyboard
	// user restarts from the top of the page.
	it("moves focus into a row it just added", async () => {
		const w = mountPanel({ attachTo: document.body });
		w.findAllComponents({ name: "Autocomplete" }).at(-1).vm.$emit("update:modelValue", {
			value: fieldKey("Jarvis Custom Skill", "description"),
		});
		await w.setProps({ clauses: lastClauses(w) });
		await nextTick();
		expect(document.activeElement.getAttribute("aria-label")).toBe("Condition for Description");
		w.unmount();
	});

	it("moves focus to the row that took a removed row's place", async () => {
		const clauses = [withValue(DESCRIPTION, "a"), withValue(IDX, "3")];
		const w = mountPanel({ clauses, attachTo: document.body });
		await w.find('button[aria-label="Remove filter on Description"]').trigger("click");
		await w.setProps({ clauses: lastClauses(w) });
		await nextTick();
		expect(document.activeElement.getAttribute("aria-label")).toBe("Condition for Index");
		w.unmount();
	});

	// U1-2: the same field may be filtered twice on purpose. "Condition for
	// Created On" announced twice is two controls a screen-reader user cannot
	// tell apart.
	it("gives repeated fields a positional cue, and leaves unique ones clean", async () => {
		const w = mountPanel({
			clauses: [withValue(CREATION, ["2026-01-01", "2026-02-01"]), withValue(DESCRIPTION, "a"), withValue(CREATION, ["2026-03-01", "2026-04-01"])],
		});
		const labels = w.findAll("select").map((s) => s.attributes("aria-label"));
		expect(labels).toContain("Condition for Created On (filter 1)");
		expect(labels).toContain("Condition for Created On (filter 2)");
		expect(labels).toContain("Condition for Description");
		expect(w.find('button[aria-label="Remove filter on Created On (filter 2)"]').exists()).toBe(true);
		expect(w.find('button[aria-label="Remove filter on Description"]').exists()).toBe(true);
	});

	it("carries the positional cue into the value control too", () => {
		const w = mountPanel({ clauses: [withValue(DESCRIPTION, "a"), withValue(DESCRIPTION, "b")] });
		const labels = w.findAll('input[type="text"]').map((i) => i.attributes("aria-label"));
		expect(labels).toEqual(["Value for Description (filter 1)", "Value for Description (filter 2)"]);
	});

	// U1-1: clearing empties the row list, so focus has nowhere natural to go.
	it("moves focus to + Add Filter when Clear All empties the panel", async () => {
		const w = mountPanel({ clauses: [withValue(DESCRIPTION, "a")], attachTo: document.body });
		const clear = w.findAll("button").find((b) => b.text() === "Clear All Filters");
		await clear.trigger("click");
		await w.setProps({ clauses: [] });
		await nextTick();
		expect(document.activeElement.textContent).toContain("+ Add Filter");
		w.unmount();
	});

	it("returns focus to the Filter trigger when the outer clear-X is used", async () => {
		const w = mountPanel({ clauses: [withValue(DESCRIPTION, "a")], attachTo: document.body });
		await w.find('button[aria-label="Clear all filters"]').trigger("click");
		await w.setProps({ clauses: [] });
		await nextTick();
		expect(document.activeElement.getAttribute("aria-label")).toBe("Filter");
		w.unmount();
	});

	it("labels the trigger, every control and every remove button", () => {
		const w = mountPanel({ clauses: [withValue(DESCRIPTION, "a")] });
		expect(w.find("button").attributes("aria-label")).toBe("Filter (1 active)");
		expect(w.find("select").attributes("aria-label")).toBe("Condition for Description");
		expect(w.find('input[type="text"]').attributes("aria-label")).toBe("Value for Description");
		expect(w.find('button[aria-label="Remove filter on Description"]').exists()).toBe(true);
		expect(w.find('[role="group"][aria-label="Filters"]').exists()).toBe(true);
	});
});
