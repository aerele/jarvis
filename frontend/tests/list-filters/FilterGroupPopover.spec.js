import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { frappeUiStubs } from "./stubs.js";
import { SKILLS_SCHEMA, DESCRIPTION, ENABLED } from "./fixtures.js";
import { clauseForEntry } from "@/components/list/filterModel";

/**
 * FilterGroup against the REAL frappe-ui Popover.
 *
 * Every other spec in this folder runs on the doubles in stubs.js, which is
 * what keeps them fast and focused. That trade only holds while the double
 * tells the truth, and once it did not: it handed `#target` a `togglePopover`
 * that emitted `open`, so "asks for the catalog on FIRST open" passed against a
 * panel that, in a browser, never asked at all and showed an empty field picker
 * on every migrated list.
 *
 * So this file pins the two things the double cannot vouch for itself:
 *   1. what frappe-ui's Popover actually emits, on each of its two open paths;
 *   2. that FilterGroup requests its schema when a person presses Filter.
 *
 * frappe-ui's package entry does not resolve under vitest (see stubs.js), so
 * the component is imported by path. If a frappe-ui upgrade moves it, re-point
 * this import — do not delete the file: the stub's shape is only justified by
 * what is asserted here.
 */
import Popover from "../../node_modules/frappe-ui/src/components/Popover/Popover.vue";

vi.mock("frappe-ui", async () => {
	const real = await import("../../node_modules/frappe-ui/src/components/Popover/Popover.vue");
	return { ...frappeUiStubs(), Popover: real.default };
});
vi.mock("@/api", () => ({ searchLink: vi.fn(async () => []) }));

const { default: FilterGroup } = await import("@/components/list/FilterGroup.vue");

// reka-ui settles its open state over several ticks.
const settle = async () => {
	for (let i = 0; i < 6; i += 1) await nextTick();
};

describe("frappe-ui Popover's open contract", () => {
	const mountPopover = () =>
		mount(Popover, {
			attachTo: document.body,
			slots: {
				target: `<template #target="{ togglePopover }">
					<button id="trigger" @click="togglePopover()">open</button>
				</template>`,
				body: `<div id="body">body</div>`,
			},
		});

	// THE DEFECT, stated as a fact about the dependency: `togglePopover` runs
	// frappe-ui's own `isOpen` setter, which emits `update:show` and nothing
	// else. `open` is emitted only from `onUpdateOpen`, i.e. only when reka-ui's
	// PopoverRoot reports a change it initiated — and since frappe-ui binds
	// `v-model:open` with a real boolean, that root is fully controlled and
	// stays silent for a change pushed into it from outside.
	it("does NOT emit `open` when the #target slot opens it", async () => {
		const w = mountPopover();
		await w.find("#trigger").trigger("click");
		await settle();

		expect(w.emitted("update:show")).toEqual([[true]]);
		expect(w.emitted("open")).toBeUndefined();
		w.unmount();
	});

	// The other path, and why FilterGroup cannot simply act on every
	// `update:show`: reka relays one dismissal as two identical emissions.
	it("relays a reka-driven dismissal as `update:show` TWICE plus `close`", async () => {
		const w = mountPopover();
		await w.find("#trigger").trigger("click");
		await settle();

		document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
		await settle();

		expect(w.emitted("update:show")).toEqual([[true], [false], [false]]);
		expect(w.emitted("close")).toHaveLength(1);
		w.unmount();
	});
});

describe("FilterGroup on the real Popover", () => {
	const mountPanel = (props = {}) =>
		mount(FilterGroup, {
			attachTo: document.body,
			props: { schema: null, schemaState: "idle", clauses: [], ...props },
		});

	// The end-to-end claim the hollow spec only appeared to make.
	it("asks for the field catalog when the Filter button is pressed", async () => {
		const w = mountPanel();
		expect(w.emitted("request-schema")).toBeUndefined();

		await w.find('button[aria-label="Filter"]').trigger("click");
		await settle();

		expect(w.emitted("request-schema")).toHaveLength(1);
		w.unmount();
	});

	it("does not ask a second time once the catalog is in hand", async () => {
		const w = mountPanel();
		await w.find('button[aria-label="Filter"]').trigger("click");
		await settle();
		await w.setProps({ schemaState: "ready" });

		await w.find('button[aria-label="Filter"]').trigger("click"); // close
		await settle();
		await w.find('button[aria-label="Filter"]').trigger("click"); // open
		await settle();

		expect(w.emitted("request-schema")).toHaveLength(1);
		w.unmount();
	});

	// A dismissal must not be mistaken for an open, and its doubled
	// `update:show` must not become two metadata walks.
	it("makes no request out of a dismissal, and still asks on the next open", async () => {
		const w = mountPanel();
		await w.find('button[aria-label="Filter"]').trigger("click");
		await settle();
		expect(w.emitted("request-schema")).toHaveLength(1);

		// still idle — the parent has not answered yet
		document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
		await settle();
		expect(w.emitted("request-schema")).toHaveLength(1);

		await w.find('button[aria-label="Filter"]').trigger("click");
		await settle();
		expect(w.emitted("request-schema")).toHaveLength(2);
		w.unmount();
	});
});

// The regression this whole change exists to fix: a FormControl select's reka
// SelectPortal would NOT open inside the teleported Popover, so the operator was
// unpickable. PanelSelect keeps the listbox inline; prove it opens + applies
// against the REAL Popover, which the stubbed specs cannot vouch for.
describe("FilterGroup operator picker on the real Popover", () => {
	it("opens the operator listbox in-panel and applies a pick without dismissing it", async () => {
		const w = mount(FilterGroup, {
			attachTo: document.body,
			props: {
				schema: SKILLS_SCHEMA,
				schemaState: "ready",
				clauses: [clauseForEntry(DESCRIPTION)],
			},
		});
		await w.find('button[aria-label="Filter"]').trigger("click");
		await settle();

		const trigger = document.querySelector('button[aria-label^="Condition for"]');
		expect(trigger).toBeTruthy();
		trigger.dispatchEvent(new MouseEvent("click", { bubbles: true }));
		await settle();

		// the listbox is inline in the panel's own DOM - a SelectPortal would not open here
		const options = document.querySelectorAll('[role="listbox"] [role="option"]');
		expect(options.length).toBeGreaterThan(1);

		// pick a NON-current operator; the panel must stay open and the clause update
		const current = w.props("clauses")[0].operator;
		let target = null;
		options.forEach((o) => {
			if (!target && o.getAttribute("aria-selected") === "false") target = o;
		});
		target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
		await settle();

		expect(w.emitted("update:clauses").at(-1)[0][0].operator).not.toBe(current);
		expect(document.querySelectorAll("[data-filter-row]")).toHaveLength(1); // still open
		w.unmount();
	});

	// The exact case the owner hit: Enabled -> equals -> No. The Check VALUE select is
	// also a PanelSelect now; prove its dropdown opens in-panel and applies "No".
	it("opens the Check value dropdown in-panel and applies 'No' without dismissing", async () => {
		const w = mount(FilterGroup, {
			attachTo: document.body,
			props: {
				schema: SKILLS_SCHEMA,
				schemaState: "ready",
				clauses: [clauseForEntry(ENABLED)],
			},
		});
		await w.find('button[aria-label="Filter"]').trigger("click");
		await settle();

		const valueTrigger = document.querySelector('button[aria-label^="Value for"]');
		expect(valueTrigger).toBeTruthy();
		valueTrigger.dispatchEvent(new MouseEvent("click", { bubbles: true }));
		await settle();

		const options = Array.from(document.querySelectorAll('[role="listbox"] [role="option"]'));
		expect(options.map((o) => o.textContent.trim())).toEqual(["Yes", "No"]);
		options
			.find((o) => o.textContent.includes("No"))
			.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
		await settle();

		expect(w.emitted("update:clauses").at(-1)[0][0].value).toBe("0"); // No -> "0"
		expect(document.querySelectorAll("[data-filter-row]")).toHaveLength(1); // still open
		w.unmount();
	});
});
