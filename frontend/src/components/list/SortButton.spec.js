import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

// frappe-ui's ESM entry doesn't resolve under vitest; stub the three components
// SortButton uses. The Popover stub renders BOTH slots so the picker body (which
// used to be trapped behind a portal-select) is queryable in jsdom.
vi.mock("frappe-ui", () => ({
	Popover: {
		name: "Popover",
		template: `<div>
			<slot name="target" :togglePopover="() => {}" />
			<slot name="body" :close="() => {}" />
		</div>`,
	},
	Button: {
		name: "Button",
		props: ["label", "icon", "tooltip", "variant"],
		emits: ["click"],
		template: `<button :data-icon="icon" @click="$emit('click')">{{ label }}</button>`,
	},
	FeatherIcon: {
		name: "FeatherIcon",
		props: ["name"],
		template: `<span class="feather" :data-name="name" />`,
	},
}));

import SortButton from "./SortButton.vue";

const OPTIONS = [
	{ label: "Name", value: "skill_name" },
	{ label: "Updated", value: "modified" },
	{ label: "Created", value: "creation" },
];
const DEFAULT = { field: "skill_name", dir: "asc" };

function mountBtn(sort = { field: "", dir: "" }) {
	return mount(SortButton, {
		props: { sortOptions: OPTIONS, sort, defaultSort: DEFAULT },
	});
}

describe("SortButton field picker (portal-free menu)", () => {
	it("renders EVERY sort option as an in-DOM menuitemradio button (not a portal Select)", () => {
		const w = mountBtn();
		const items = w.findAll('[role="menuitemradio"]');
		expect(items).toHaveLength(OPTIONS.length);
		expect(items.map((i) => i.text())).toEqual(["Name", "Updated", "Created"]);
	});

	it("marks the current field aria-checked (the page default when no sort is active)", () => {
		const checked = mountBtn()
			.findAll('[role="menuitemradio"]')
			.filter((i) => i.attributes("aria-checked") === "true");
		expect(checked).toHaveLength(1);
		expect(checked[0].text()).toBe("Name");
		// and it tracks the active sort when one is set
		const w2 = mountBtn({ field: "creation", dir: "desc" });
		const checked2 = w2
			.findAll('[role="menuitemradio"]')
			.filter((i) => i.attributes("aria-checked") === "true");
		expect(checked2[0].text()).toBe("Created");
	});

	it("clicking a field emits update:sort for THAT field (reachable now, was dismissed before)", async () => {
		const w = mountBtn();
		const updated = w.findAll('[role="menuitemradio"]').find((i) => i.text() === "Updated");
		await updated.trigger("click");
		const emits = w.emitted("update:sort");
		expect(emits).toBeTruthy();
		expect(emits.at(-1)[0]).toMatchObject({ field: "modified" });
	});

	it("the datetime columns (Updated/Created) are both pickable", async () => {
		const w = mountBtn();
		const created = w.findAll('[role="menuitemradio"]').find((i) => i.text() === "Created");
		await created.trigger("click");
		expect(w.emitted("update:sort").at(-1)[0]).toMatchObject({ field: "creation" });
	});

	// The always-visible direction toggle + Clear Sort are pre-existing, but the picker
	// refactor sits beside them; pin them so the whole control stays covered.
	it("the direction toggle flips dir and keeps the current field", async () => {
		const w = mountBtn(); // default dir resolves to asc → toggle shows arrow-up
		await w.find('[data-icon="arrow-up"]').trigger("click");
		expect(w.emitted("update:sort").at(-1)[0]).toMatchObject({
			field: "skill_name",
			dir: "desc",
		});
	});

	it("Clear Sort resets to the page default", async () => {
		const w = mountBtn({ field: "creation", dir: "desc" });
		const clear = w.findAll("button").find((b) => b.text() === "Clear Sort");
		await clear.trigger("click");
		expect(w.emitted("update:sort").at(-1)[0]).toEqual({ field: "skill_name", dir: "asc" });
	});
});
