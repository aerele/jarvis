import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";

// frappe-ui's ESM entry doesn't resolve under vitest; OperatorSelect only pulls
// FeatherIcon from it.
vi.mock("frappe-ui", () => ({
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: `<span :data-icon="name" />` },
}));

import PanelSelect from "./PanelSelect.vue";

const OPTIONS = [
	{ label: "equals", value: "=" },
	{ label: "not equals", value: "!=" },
	{ label: "greater than or equals", value: ">=" },
];

function mountSel(modelValue = "=", extra = {}) {
	return mount(PanelSelect, {
		attachTo: document.body,
		props: { options: OPTIONS, modelValue, ariaLabel: "Condition for Enabled", ...extra },
	});
}

describe("PanelSelect (portal-free select)", () => {
	it("shows the current operator's label on the trigger", () => {
		const w = mountSel("!=");
		expect(w.find("button").text()).toContain("not equals");
		w.unmount();
	});

	it("is a labelled listbox button, collapsed by default", () => {
		const w = mountSel();
		const btn = w.find("button");
		expect(btn.attributes("aria-label")).toBe("Condition for Enabled");
		expect(btn.attributes("aria-haspopup")).toBe("listbox");
		expect(btn.attributes("aria-expanded")).toBe("false");
		expect(w.find('[role="listbox"]').exists()).toBe(false);
		w.unmount();
	});

	it("opens IN-DOM on click with EVERY option reachable (not a reka portal)", async () => {
		const w = mountSel();
		await w.find("button").trigger("click");
		expect(w.find("button").attributes("aria-expanded")).toBe("true");
		// the listbox is a real child in the component tree, not teleported to <body>
		expect(w.find('[role="listbox"]').exists()).toBe(true);
		const opts = w.findAll('[role="option"]');
		expect(opts).toHaveLength(OPTIONS.length);
		expect(opts.map((o) => o.text().trim())).toEqual([
			"equals",
			"not equals",
			"greater than or equals",
		]);
		w.unmount();
	});

	it("marks the current option aria-selected", async () => {
		const w = mountSel(">=");
		await w.find("button").trigger("click");
		const sel = w
			.findAll('[role="option"]')
			.filter((o) => o.attributes("aria-selected") === "true");
		expect(sel).toHaveLength(1);
		expect(sel[0].text()).toContain("greater than or equals");
		w.unmount();
	});

	it("applies a clicked option (emits update:modelValue) and closes", async () => {
		const w = mountSel("=");
		await w.find("button").trigger("click");
		const notEq = w.findAll('[role="option"]').find((o) => o.text().includes("not equals"));
		await notEq.trigger("mousedown");
		expect(w.emitted("update:modelValue").at(-1)).toEqual(["!="]);
		expect(w.find('[role="listbox"]').exists()).toBe(false);
		w.unmount();
	});

	// The keyboard reach past the first option is exactly what the native <select>
	// had and JvCombo's plain picker lost.
	it("is keyboard operable to ANY option (arrow-move + enter)", async () => {
		const w = mountSel("=");
		const btn = w.find("button");
		await btn.trigger("keydown", { key: "ArrowDown" }); // open, highlight 0
		await btn.trigger("keydown", { key: "ArrowDown" }); // -> 1
		await btn.trigger("keydown", { key: "ArrowDown" }); // -> 2
		await btn.trigger("keydown", { key: "Enter" }); // pick highlighted (>=)
		expect(w.emitted("update:modelValue").at(-1)).toEqual([">="]);
		w.unmount();
	});

	it("Escape closes without emitting", async () => {
		const w = mountSel();
		await w.find("button").trigger("click");
		await w.find("button").trigger("keydown", { key: "Escape" });
		expect(w.find('[role="listbox"]').exists()).toBe(false);
		expect(w.emitted("update:modelValue")).toBeFalsy();
		w.unmount();
	});

	it("an outside click closes the menu on its own (never leans on the parent Popover)", async () => {
		const w = mountSel();
		await w.find("button").trigger("click");
		expect(w.find('[role="listbox"]').exists()).toBe(true);
		document.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
		await nextTick();
		expect(w.find('[role="listbox"]').exists()).toBe(false);
		w.unmount();
	});
});
