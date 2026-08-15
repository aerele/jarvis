import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { frappeUiStubs } from "./stubs.js";

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
