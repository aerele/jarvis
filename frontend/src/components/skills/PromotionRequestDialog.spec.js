import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import fs from "node:fs";
import path from "node:path";

/**
 * The "Request promotion" dialog's role picker.
 *
 * Two failure modes are pinned here, because the bug that prompted them was
 * invisible to a normal unit test: the picker RENDERED fine and was simply
 * painted over. So the behaviour half is mounted, and the stacking half is
 * asserted against the stylesheet — the only place it can be checked without a
 * real browser.
 */

const rolesMock = vi.fn(async () => ({ roles: ["Sales Manager", "Accounts"] }));
vi.mock("@/api/skills", () => ({ promotableTargetRoles: () => rolesMock() }));

// frappe-ui's ESM entry does not resolve under vitest (every other spec here
// stubs it the same way). The stubs keep the contracts this dialog relies on:
// FormControl emits update:modelValue, Autocomplete emits an {label,value}.
vi.mock("frappe-ui", () => ({
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		template: "<div class='dialog'><slot name='body-content'/><slot name='actions'/></div>",
	},
	FormControl: {
		name: "FormControl",
		props: ["type", "label", "options", "modelValue", "rows", "placeholder"],
		emits: ["update:modelValue"],
		template: `<select v-if="type === 'select'" class="fc-select" @change="$emit('update:modelValue', $event.target.value)">
			<option v-for="o in options || []" :key="o.value" :value="o.value">{{ o.label }}</option>
		</select><textarea v-else class="fc-textarea" />`,
	},
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "modelValue", "placeholder"],
		emits: ["update:modelValue"],
		template: "<div class='autocomplete' />",
	},
	Button: { name: "Button", props: ["label", "loading", "disabled"], template: "<button />" },
	toast: { error: vi.fn(), success: vi.fn() },
}));

import PromotionRequestDialog from "./PromotionRequestDialog.vue";

const openDialog = async () => {
	const w = mount(PromotionRequestDialog, { props: { modelValue: false, noun: "skill" } });
	await w.setProps({ modelValue: true }); // the watcher resets + loads on open
	await flushPromises();
	return w;
};

// Drive the "Promote to" select the way a user does.
const chooseScope = async (w, value) => {
	const select = w.find("select.fc-select");
	select.element.value = value;
	await select.trigger("change");
	await flushPromises();
};

beforeEach(() => rolesMock.mockClear());

describe("PromotionRequestDialog role picker", () => {
	it("hides the role picker on the default Org scope", async () => {
		const w = await openDialog();
		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(false);
	});

	it("reveals the role picker when the scope becomes Role", async () => {
		const w = await openDialog();
		await chooseScope(w, "Role");
		expect(w.findComponent({ name: "Autocomplete" }).exists()).toBe(true);
	});

	it("offers the requester's own targetable roles", async () => {
		const w = await openDialog();
		await chooseScope(w, "Role");
		expect(w.findComponent({ name: "Autocomplete" }).props("options")).toEqual([
			{ label: "Sales Manager", value: "Sales Manager" },
			{ label: "Accounts", value: "Accounts" },
		]);
	});

	it("explains itself instead of showing an empty picker when the user holds no roles", async () => {
		rolesMock.mockResolvedValueOnce({ roles: [] });
		const w = await openDialog();
		await chooseScope(w, "Role");
		expect(w.text()).toContain("You hold no roles that can be targeted");
	});

	it("cannot submit a Role promotion until a role is chosen", async () => {
		const w = await openDialog();
		await chooseScope(w, "Role");
		expect(w.findComponent({ name: "Button" }).props("disabled")).toBe(true);
		w.findComponent({ name: "Autocomplete" }).vm.$emit("update:modelValue", {
			label: "Accounts",
			value: "Accounts",
		});
		await flushPromises();
		expect(w.findComponent({ name: "Button" }).props("disabled")).toBe(false);
	});
});

describe("portalled pickers stack above dialogs (source guard)", () => {
	// frappe-ui portals Popover content to <body> with no z-index of its own, so a
	// picker opened inside a dialog is painted over by the dialog's own overlay.
	// jsdom computes no stacking, so this is asserted against the stylesheet.
	const css = fs.readFileSync(path.resolve(process.cwd(), "src/main.css"), "utf8");
	const zOf = (selector) => {
		const rule = new RegExp(`\\${selector}\\s*\\{[^}]*z-index:\\s*(\\d+)`, "m").exec(css);
		return rule ? Number(rule[1]) : null;
	};

	it("gives .PopoverContent a z-index above .dialog-overlay", () => {
		const popover = zOf(".PopoverContent");
		const dialog = zOf(".dialog-overlay");
		expect(popover, ".PopoverContent needs an explicit z-index").not.toBeNull();
		expect(dialog).not.toBeNull();
		expect(popover).toBeGreaterThan(dialog);
	});

	it("keeps it below the confirm dialog, so a confirm still wins", () => {
		expect(zOf(".PopoverContent")).toBeLessThan(200);
	});
});
