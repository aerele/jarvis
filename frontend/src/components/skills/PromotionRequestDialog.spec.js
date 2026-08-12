import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import fs from "node:fs";
import path from "node:path";

/**
 * The "Request promotion" dialog's role picker.
 *
 * The picker is an INLINE search box + clickable list, NOT the frappe-ui
 * Autocomplete: the Autocomplete's search box renders in a popover portaled
 * OUTSIDE this reka-ui Dialog, where the Dialog's focus scope left it
 * unclickable. The behaviour half is mounted here; the stacking half (the
 * app-wide z-index guard other portaled pickers still rely on) is asserted
 * against the stylesheet.
 */

const rolesMock = vi.fn(async () => ({ roles: ["Sales Manager", "Accounts"] }));
vi.mock("@/api/skills", () => ({ promotableTargetRoles: () => rolesMock() }));

// frappe-ui's ESM entry does not resolve under vitest (every other spec here
// stubs it the same way). The stub keeps the contracts this dialog relies on:
// FormControl select emits the value on change; a text field emits on input.
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
		</select><input v-else class="fc-input" :type="type" :placeholder="placeholder" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	Button: {
		name: "Button",
		props: ["label", "loading", "disabled"],
		emits: ["click"],
		template: "<button :disabled='disabled' @click=\"$emit('click')\">{{ label }}</button>",
	},
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: "<i class='feather' />" },
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

// Type into the inline role search box (a plain input, clickable inside the Dialog).
const typeSearch = async (w, value) => {
	const input = w.find('input[placeholder="Search your roles"]');
	input.element.value = value;
	await input.trigger("input");
	await flushPromises();
};

// The role list is inline clickable options (role="option"), not a portaled popover.
const roleOptionTexts = (w) => w.findAll('[role="option"]').map((b) => b.text());
const pickRole = async (w, name) => {
	const btn = w.findAll('[role="option"]').find((b) => b.text() === name);
	await btn.trigger("click");
	await flushPromises();
};

beforeEach(() => rolesMock.mockClear());

describe("PromotionRequestDialog role picker", () => {
	it("shows no role options on the default Org scope", async () => {
		const w = await openDialog();
		expect(roleOptionTexts(w)).toEqual([]);
	});

	it("lists the requester's own targetable roles when the scope becomes Role", async () => {
		const w = await openDialog();
		await chooseScope(w, "Role");
		expect(roleOptionTexts(w)).toEqual(["Sales Manager", "Accounts"]);
	});

	it("filters the list as the requester types in the (clickable) search box", async () => {
		const w = await openDialog();
		await chooseScope(w, "Role");
		await typeSearch(w, "sales");
		expect(roleOptionTexts(w)).toEqual(["Sales Manager"]);
		await typeSearch(w, "zzz");
		expect(roleOptionTexts(w)).toEqual([]);
		expect(w.text()).toContain("No roles match your search");
	});

	it("explains itself instead of showing an empty picker when the user holds no roles", async () => {
		rolesMock.mockResolvedValueOnce({ roles: [] });
		const w = await openDialog();
		await chooseScope(w, "Role");
		expect(w.text()).toContain("You hold no roles that can be targeted");
		expect(roleOptionTexts(w)).toEqual([]);
	});

	it("cannot submit a Role promotion until a role is picked", async () => {
		const w = await openDialog();
		await chooseScope(w, "Role");
		expect(w.findComponent({ name: "Button" }).props("disabled")).toBe(true);
		await pickRole(w, "Accounts");
		expect(w.findComponent({ name: "Button" }).props("disabled")).toBe(false);
	});

	it("emits a single target_role and no target_roles in single-select mode", async () => {
		const w = await openDialog();
		await chooseScope(w, "Role");
		await pickRole(w, "Accounts");
		const send = w.findAll("button").find((b) => b.text() === "Send request");
		await send.trigger("click");
		const payload = w.emitted("submit")[0][0];
		expect(payload.target_role).toBe("Accounts");
		expect(payload.target_roles).toBeUndefined();
	});
});

describe("PromotionRequestDialog multiselect (skills only)", () => {
	const openMulti = async () => {
		const w = mount(PromotionRequestDialog, {
			props: { modelValue: false, noun: "skill", multiple: true },
		});
		await w.setProps({ modelValue: true });
		await flushPromises();
		return w;
	};
	// In multiselect mode the rows are checkboxes, not single-select options.
	const boxes = (w) => w.findAll('[role="checkbox"]');
	const pickBox = async (w, name) => {
		const b = boxes(w).find((x) => x.text() === name);
		await b.trigger("click");
		await flushPromises();
	};
	const send = (w) => w.findAll("button").find((b) => b.text() === "Send request");

	it("renders roles as toggleable checkboxes and counts the selection", async () => {
		const w = await openMulti();
		await chooseScope(w, "Role");
		expect(boxes(w).map((b) => b.text())).toEqual(["Sales Manager", "Accounts"]);
		await pickBox(w, "Sales Manager");
		await pickBox(w, "Accounts");
		expect(w.text()).toContain("2 roles selected");
		await pickBox(w, "Sales Manager"); // toggling a selected role removes it
		expect(w.text()).toContain("1 role selected");
	});

	it("emits the full target_roles set plus the primary target_role", async () => {
		const w = await openMulti();
		await chooseScope(w, "Role");
		await pickBox(w, "Sales Manager");
		await pickBox(w, "Accounts");
		await send(w).trigger("click");
		const payload = w.emitted("submit")[0][0];
		expect(payload.to_scope).toBe("Role");
		expect(payload.target_roles).toEqual(["Sales Manager", "Accounts"]);
		expect(payload.target_role).toBe("Sales Manager");
	});

	it("stays un-submittable until at least one role is chosen", async () => {
		const w = await openMulti();
		await chooseScope(w, "Role");
		expect(w.findComponent({ name: "Button" }).props("disabled")).toBe(true);
		await pickBox(w, "Accounts");
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
