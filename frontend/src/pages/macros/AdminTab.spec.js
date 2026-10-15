import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The Jarvis Admin's tab on the Macros page (/macros/admin): every user's macros,
 * the filters, the loading / empty / error states, and the row actions: Stop run,
 * Hold (the reason is asked by MacroHoldDialog, stubbed here), Release and Delete.
 * The decisions behind the cells (lib/macroRunOutcome, lib/macroSchedule) are the
 * real ones; frappe-ui and the read-only dialog are stubs.
 */

vi.mock("frappe-ui", () => ({
	// As the real Button does: it shows its slot (or its label), is NAMED by its label
	// alone, and that name replaces any aria-label attribute it was given. A stub
	// that let the attribute through once hid row buttons that all read "Delete".
	Button: {
		name: "Button",
		inheritAttrs: false,
		props: ["label", "variant", "icon", "iconLeft", "loading", "disabled", "size", "theme"],
		// No click event of its own, as the real one: the listener falls through to
		// the <button> and is handed the browser's click.
		template: `<button v-bind="$attrs" class="stub-button" :disabled="disabled" :data-loading="loading ? '1' : ''" :data-variant="variant" :aria-label="label"><slot>{{ label }}</slot></button>`,
	},
	// The menu drawn open: its button (the slot), then one button per item, classed by
	// the item's key so a test can press "Hold" on a row as an admin would pick it.
	Dropdown: {
		name: "Dropdown",
		props: ["options", "placement"],
		emits: ["update:open"],
		template: `<span class="stub-dropdown"><slot /><span class="stub-menu"><template v-for="g in options" :key="g.group"><button v-for="o in g.items" :key="o.key" class="stub-menu-item" :class="'jv-macro-admin-' + o.key" :data-group="g.group" :data-theme="o.theme || ''" :disabled="o.disabled" @click="o.onClick($event)">{{ o.label }}</button></template></span></span>`,
	},
	Badge: {
		name: "Badge",
		props: ["label", "theme"],
		template: `<span class="badge">{{ label }}</span>`,
	},
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: `<i class="stub-icon" />` },
	Tooltip: {
		name: "Tooltip",
		props: ["text"],
		template: `<span class="stub-tooltip" :data-text="text"><slot /></span>`,
	},
	Breadcrumbs: {
		name: "Breadcrumbs",
		props: ["items"],
		template: `<nav class="stub-crumbs" />`,
	},
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<span>{{ message }}</span>`,
	},
	FormControl: {
		name: "FormControl",
		props: ["type", "options", "modelValue", "label", "placeholder"],
		emits: ["update:modelValue"],
		// The filters carry no visible label: their name is the aria-label.
		template: `
			<label class="stub-control">
				<span class="stub-label">{{ label || $attrs["aria-label"] }}</span>
				<select v-if="type === 'select'" :value="modelValue"
					@change="$emit('update:modelValue', $event.target.value)">
					<option v-for="o in options" :key="o.value" :value="o.value" :disabled="o.disabled">{{ o.label }}</option>
				</select>
				<input v-else :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />
			</label>`,
	},
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
}));

vi.mock("@/utils/datetime", () => ({
	timeAgo: (d) => (d ? "a while ago" : ""),
	exactDate: (d) => String(d || ""),
	toLocalMs: (d) => (d ? Date.parse(String(d).replace(" ", "T")) : null),
	formatTime12h: (t) => (t ? "9:00 am" : ""),
}));

const api = vi.hoisted(() => ({
	adminListMacros: vi.fn(),
	adminMacroOwners: vi.fn(),
	adminStopRun: vi.fn(),
	adminRelease: vi.fn(),
	adminDelete: vi.fn(),
}));
vi.mock("@/api/macrosAdmin", () => api);

// The page header teleports in the app; here it renders in place.
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: { name: "LayoutHeader", template: `<div><slot name="left-header" /></div>` },
}));

const confirm = vi.hoisted(() => vi.fn());
vi.mock("@/composables/useConfirm", () => ({ useConfirm: () => ({ confirm }) }));

vi.mock("./MacroAdminDialog.vue", () => ({
	default: {
		name: "MacroAdminDialog",
		props: ["modelValue", "name"],
		emits: ["update:modelValue", "gone"],
		template: `<div class="stub-detail" :data-name="name" :data-open="modelValue ? '1' : ''" />`,
	},
}));

vi.mock("./MacroHoldDialog.vue", () => ({
	default: {
		name: "MacroHoldDialog",
		props: ["modelValue", "name", "macroName", "ownerLabel"],
		emits: ["update:modelValue", "held", "failed"],
		template: `<div class="stub-hold" :data-name="name" :data-open="modelValue ? '1' : ''" :data-owner="ownerLabel" />`,
	},
}));

vi.mock("./MacroHandoverDialog.vue", () => ({
	default: {
		name: "MacroHandoverDialog",
		props: ["modelValue", "name", "macroName", "owner", "ownerLabel"],
		emits: ["update:modelValue", "handed", "failed"],
		template: `<div class="stub-handover" :data-name="name" :data-open="modelValue ? '1' : ''" :data-macro="macroName" :data-owner="owner" :data-owner-label="ownerLabel" />`,
	},
}));

// Who is looking: an admin whose own macros have no Hold.
vi.mock("@/data/session", () => ({ session: { user: "admin@example.test" } }));

import { toast } from "frappe-ui";
import AdminTab from "./AdminTab.vue";

const row = (name, extra = {}) => ({
	name,
	macro_name: `Macro ${name}`,
	owner: "asha@example.test",
	owner_full_name: "Asha Rao",
	enabled: 1,
	skip_confirmation: 0,
	schedule_enabled: 0,
	schedule_frequency: "daily",
	schedule_time: "09:00:00",
	next_run_at: null,
	next_run_is_retry: 0,
	last_run: null,
	live_run: "",
	...extra,
});
const page = (rows, extra = {}) => ({
	rows,
	total: rows.length,
	has_more: false,
	start: 0,
	page_length: 20,
	...extra,
});
const OWNERS = [
	{ user: "asha@example.test", full_name: "Asha Rao", macros: 2 },
	{ user: "ben@example.test", full_name: "ben@example.test", macros: 1 },
];
// `n` rows named r0, r1, ...: a list long enough to page.
const many = (n, from = 0) => Array.from({ length: n }, (_, i) => row(`r${from + i}`));
// The server's paging over `all`: what a real list call answers for start/pageLength.
const serve = (all) => (q) =>
	Promise.resolve(
		page(all.slice(q.start, q.start + q.pageLength), {
			total: all.length,
			has_more: q.start + q.pageLength < all.length,
			start: q.start,
			page_length: q.pageLength,
		})
	);

async function mountWith(rows, extra, options) {
	api.adminListMacros.mockResolvedValue(page(rows, extra));
	const w = mount(AdminTab, options);
	await flushPromises();
	// The four selects sit behind "Filters": most cases need them in reach.
	if (!options || options.filtersClosed !== true) await openFilters(w);
	return w;
}
const openFilters = async (w) => {
	const toggle = w.find(".jv-macro-admin-filters-toggle");
	if (toggle.exists() && toggle.attributes("aria-expanded") !== "true")
		await toggle.trigger("click");
};

// By its words, or by its name when it is an icon alone (Refresh).
const button = (w, label) =>
	w
		.findAll("button.stub-button")
		.find((b) => b.text() === label || b.attributes("aria-label") === label);
const rowsOf = (w) => w.findAll(".jv-macro-admin-row");
const control = (w, label) =>
	w.findAll(".stub-control").find((c) => c.find(".stub-label").text() === label);
const lastCall = () => api.adminListMacros.mock.calls.at(-1)[0];

beforeEach(() => {
	vi.clearAllMocks();
	confirm.mockResolvedValue(true);
	api.adminListMacros.mockReset();
	api.adminMacroOwners.mockReset();
	api.adminMacroOwners.mockResolvedValue({ owners: OWNERS, more: false });
	api.adminRelease.mockResolvedValue({ ok: true, released: true });
	api.adminDelete.mockResolvedValue({ ok: true, deleted: true, stopped_runs: 0 });
	api.adminStopRun.mockResolvedValue({
		ok: true,
		stopped: true,
		status: "stopped",
		message: "",
	});
});
afterEach(() => {
	vi.useRealTimers();
});

describe("Macros AdminTab, states", () => {
	it("says Loading until the first answer, then lists the macros", async () => {
		let answer;
		api.adminListMacros.mockReturnValue(new Promise((r) => (answer = r)));
		const w = mount(AdminTab);
		await flushPromises();
		expect(w.text()).toContain("Loading…");
		expect(rowsOf(w)).toHaveLength(0);
		answer(page([row("a")]));
		await flushPromises();
		expect(w.text()).not.toContain("Loading…");
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("says so when nobody has a macro, with nothing to clear", async () => {
		const w = await mountWith([]);
		expect(w.text()).toContain("Nobody has made a macro yet.");
		expect(button(w, "Clear filters")).toBeUndefined();
	});

	it("says no macros MATCH when a filter is on, and Clear filters shows them all again", async () => {
		const w = await mountWith([row("a")]);
		api.adminListMacros.mockResolvedValue(page([]));
		await control(w, "Armed").find("select").setValue("1");
		await flushPromises();
		expect(w.text()).toContain("No macros match these filters.");

		api.adminListMacros.mockResolvedValue(page([row("a")]));
		await button(w, "Clear filters").trigger("click");
		await flushPromises();
		expect(lastCall().filters).toEqual({});
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("shows the error with Try again when the first load fails, and recovers", async () => {
		api.adminListMacros.mockRejectedValue(new Error("You need the Jarvis Admin role"));
		const w = mount(AdminTab);
		await flushPromises();
		expect(w.find("[role='alert']").text()).toContain("You need the Jarvis Admin role");
		expect(w.text()).not.toContain("Nobody has made a macro yet.");

		api.adminListMacros.mockResolvedValue(page([row("a")]));
		await button(w, "Try again").trigger("click");
		await flushPromises();
		expect(w.find("[role='alert']").exists()).toBe(false);
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("keeps the rows it had when a refresh fails, and says they are the earlier ones", async () => {
		const w = await mountWith([row("a"), row("b")]);
		api.adminListMacros.mockRejectedValue(new Error("Server is busy"));
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(2);
		const alert = w.find("[role='alert']");
		expect(alert.text()).toContain("Server is busy");
		expect(alert.text()).toContain("Showing what was loaded before.");

		api.adminListMacros.mockResolvedValue(page([row("a")]));
		await button(w, "Try again").trigger("click");
		await flushPromises();
		expect(w.find("[role='alert']").exists()).toBe(false);
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("drops the answer to an earlier request that arrives late", async () => {
		const w = await mountWith([row("a")]);
		let slow;
		api.adminListMacros.mockReturnValueOnce(new Promise((r) => (slow = r)));
		await control(w, "Armed").find("select").setValue("1");
		api.adminListMacros.mockResolvedValueOnce(page([row("new")]));
		await control(w, "Armed").find("select").setValue("0");
		await flushPromises();
		slow(page([row("stale")]));
		await flushPromises();
		expect(rowsOf(w).map((r) => r.text())).toEqual([expect.stringContaining("Macro new")]);
	});
});

describe("Macros AdminTab, frame", () => {
	it("gives the table's scroll box a position, so its hidden header cannot widen the page", async () => {
		// The screen-reader-only "Actions" header is absolutely positioned. With no
		// positioned ancestor it is laid out against the page: at phone width the
		// page then scrolls sideways (measured in a real browser: 564 px in a 375 px
		// window). jsdom has no layout, so this pins the class that prevents it.
		const w = await mountWith([row("m1")]);
		const box = w.find(".jv-macro-admin-scroll");
		expect(box.classes()).toContain("relative");
		expect(box.classes()).toContain("overflow-x-auto");
		expect(box.find(".sr-only").exists()).toBe(true);
	});
});

describe("Macros AdminTab, layout", () => {
	it("names every filter for a screen reader, and says what each 'All' is all of", async () => {
		const w = await mountWith([row("a")]);
		for (const [name, any] of [
			["Owner", "All owners"],
			["Armed", "All arming states"],
			["Schedule", "All schedules"],
			["Runs", "All run states"],
			["Hold", "All hold states"],
		]) {
			expect(control(w, name).find("option").text()).toBe(any);
		}
		expect(control(w, "Search").exists()).toBe(true);
	});

	it("offers Clear in the toolbar only while something is filtered", async () => {
		const w = await mountWith([row("a")]);
		expect(w.find(".jv-macro-admin-clear").exists()).toBe(false);
		await control(w, "Armed").find("select").setValue("1");
		await flushPromises();
		expect(w.find(".jv-macro-admin-clear").exists()).toBe(true);
		await w.find(".jv-macro-admin-clear").trigger("click");
		await flushPromises();
		expect(w.find(".jv-macro-admin-clear").exists()).toBe(false);
		expect(lastCall().filters || {}).not.toHaveProperty("armed");
	});

	it("shows Stop run on the row and everything else in one Actions menu, in one fixed order", async () => {
		const w = await mountWith([
			row("a", { live_run: "RUN-1" }),
			row("b", { enabled: 0, admin_hold: 1, admin_hold_reason: "Sends too many emails" }),
			row("mine", { owner: "admin@example.test" }),
		]);
		const cell = (i) => rowsOf(w)[i].find(".jv-macro-admin-cell-actions");
		// On the row: Stop run only where a run is going, then the menu's button.
		const onRow = (i) =>
			cell(i)
				.findAll("button.stub-button")
				.map((b) => b.text());
		expect(onRow(0)).toEqual(["Stop run", "Actions"]);
		expect(onRow(1)).toEqual(["Actions"]);
		expect(onRow(2)).toEqual(["Actions"]);
		// In the menu.
		const inMenu = (i) =>
			cell(i)
				.findAll(".stub-menu-item")
				.map((b) => b.text());
		expect(inMenu(0)).toEqual(["Hold", "Hand over", "Delete"]);
		expect(inMenu(1)).toEqual(["Release", "Hand over", "Delete"]);
		// An admin's own macro: Delete only.
		expect(inMenu(2)).toEqual(["Delete"]);
		// Delete is set apart, in a group of its own, and marked as the dangerous one.
		for (const i of [0, 1, 2]) {
			const del = cell(i).find(".jv-macro-admin-delete");
			expect(del.attributes("data-theme")).toBe("red");
			const others = cell(i)
				.findAll(".stub-menu-item")
				.filter((b) => b.text() !== "Delete");
			for (const o of others)
				expect(o.attributes("data-group")).not.toBe(del.attributes("data-group"));
		}
	});

	it("names each row's Stop run and Actions for that row, starting with the word they show", async () => {
		// Read out of context ("Actions, button" six times) they could not be told apart.
		const w = await mountWith([
			row("a", { live_run: "RUN-1" }),
			row("b", {
				enabled: 0,
				admin_hold: 1,
				owner: "ben@example.test",
				owner_full_name: "Ben",
			}),
		]);
		const names = (i) => {
			const cell = rowsOf(w)[i].find(".jv-macro-admin-cell-actions");
			return cell.findAll("button.stub-button").map((b) => b.attributes("aria-label"));
		};
		expect(names(0)).toEqual([
			"Stop run of Macro a, owned by Asha Rao (asha@example.test)",
			"Actions for Macro a, owned by Asha Rao (asha@example.test)",
		]);
		expect(names(1)).toEqual(["Actions for Macro b, owned by Ben (ben@example.test)"]);
		// The name contains the visible word, so speech input can press it by what it shows.
		for (const b of w.findAll(".jv-macro-admin-cell-actions button.stub-button"))
			expect(b.attributes("aria-label").startsWith(b.text())).toBe(true);
	});

	it("puts focus on the row's Actions button before a chosen action runs", async () => {
		// The menu item that had focus goes away with the menu: what the action opens
		// (a dialog, a confirmation) gives focus back to this button when cancelled.
		const w = await mountWith([row("z"), row("a")], undefined, { attachTo: document.body });
		const seen = [];
		confirm.mockImplementation(async () => {
			seen.push(document.activeElement);
			return false;
		});
		await rowsOf(w)[1].find(".jv-macro-admin-delete").trigger("click");
		await flushPromises();
		expect(seen).toEqual([rowsOf(w)[1].find(".jv-macro-admin-actions").element]);
		// And the dialogs: the same button has focus when they open.
		await rowsOf(w)[1].find(".jv-macro-admin-hold").trigger("click");
		await flushPromises();
		expect(w.find(".stub-hold").attributes("data-open")).toBe("1");
		expect(document.activeElement).toBe(rowsOf(w)[1].find(".jv-macro-admin-actions").element);
		w.unmount();
	});

	// A menu the library has open: what `fromMenu` waits on, and what it focuses.
	// Always taken away again, pass or fail: a stray one would hold up every later pick.
	const strays = [];
	const openMenu = () => {
		const el = document.createElement("div");
		el.setAttribute("data-reka-menu-content", "");
		el.tabIndex = -1;
		document.body.appendChild(el);
		strays.push(el);
		return el;
	};
	afterEach(() => {
		for (const el of strays.splice(0)) el.remove();
		for (const el of document.querySelectorAll('[role="alertdialog"]')) el.remove();
	});
	const wait = (ms) => new Promise((r) => setTimeout(r, ms));

	it("waits for an open menu to close before the chosen action runs", async () => {
		// A confirmation opened while the menu is still there would not take the keyboard.
		const w = await mountWith([row("a")], undefined, { attachTo: document.body });
		const open = openMenu();
		await rowsOf(w)[0].find(".jv-macro-admin-delete").trigger("click");
		await wait(60);
		expect(confirm).not.toHaveBeenCalled();
		open.remove();
		await wait(80);
		expect(confirm).toHaveBeenCalledTimes(1);
		w.unmount();
	});

	it("takes one pick at a time: a second press while the first settles does nothing", async () => {
		// An item still takes a press while its menu fades out. A second pick would move
		// focus out of the confirmation the first one opened.
		const w = await mountWith([row("a")], undefined, { attachTo: document.body });
		let cancel;
		// As the real confirmation: asked now, drawn a moment later.
		confirm.mockImplementation(() => {
			setTimeout(() => {
				const asking = document.createElement("div");
				asking.setAttribute("role", "alertdialog");
				cancel = document.createElement("button");
				asking.appendChild(cancel);
				document.body.appendChild(asking);
				cancel.focus();
			}, 5);
			return new Promise(() => {});
		});
		const del = rowsOf(w)[0].find(".jv-macro-admin-delete");
		const open = openMenu();
		del.element.click();
		del.element.click();
		await wait(40);
		open.remove();
		await wait(80);
		expect(confirm).toHaveBeenCalledTimes(1);
		expect(document.activeElement).toBe(cancel);
		w.unmount();
	});

	it("drops a pick made while something is already asking", async () => {
		// A question opened from elsewhere on the tab (Stop run): focus stays in it.
		const w = await mountWith([row("a")], undefined, { attachTo: document.body });
		const asking = document.createElement("div");
		asking.setAttribute("role", "alertdialog");
		const cancel = document.createElement("button");
		asking.appendChild(cancel);
		document.body.appendChild(asking);
		cancel.focus();
		await rowsOf(w)[0].find(".jv-macro-admin-hold").trigger("click");
		await rowsOf(w)[0].find(".jv-macro-admin-delete").trigger("click");
		await wait(40);
		expect(document.activeElement).toBe(cancel);
		expect(w.find(".stub-hold").exists()).toBe(false);
		expect(confirm).not.toHaveBeenCalled();
		w.unmount();
	});

	it("drops the pick when a menu is still open after the wait, or the tab was left", async () => {
		// Another row's menu was opened meanwhile: nothing may open over it.
		let w = await mountWith([row("a")], undefined, { attachTo: document.body });
		openMenu();
		await rowsOf(w)[0].find(".jv-macro-admin-delete").trigger("click");
		await wait(900);
		expect(confirm).not.toHaveBeenCalled();
		w.unmount();
		for (const el of strays.splice(0)) el.remove();

		// The tab is gone before the menu has closed: no question about a list not on screen.
		w = await mountWith([row("a")], undefined, { attachTo: document.body });
		const open = openMenu();
		await rowsOf(w)[0].find(".jv-macro-admin-delete").trigger("click");
		w.unmount();
		open.remove();
		await wait(80);
		expect(confirm).not.toHaveBeenCalled();
	});

	it("moves focus into the menu when it opens, and only then", async () => {
		// The library leaves focus on the button: the arrow keys would not reach the items.
		const w = await mountWith([row("a")], undefined, { attachTo: document.body });
		const content = openMenu();
		const menu = w.findComponent({ name: "Dropdown" });
		menu.vm.$emit("update:open", false);
		await new Promise((r) => requestAnimationFrame(r));
		expect(document.activeElement).not.toBe(content);
		menu.vm.$emit("update:open", true);
		await new Promise((r) => requestAnimationFrame(r));
		expect(document.activeElement).toBe(content);
		w.unmount();
	});

	it("opens the menu from the side its button is on: the row's end, or a card's start", async () => {
		// jsdom lays nothing out, so the list's width is given here.
		const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get");
		width.mockReturnValue(1100);
		let w = await mountWith([row("a")]);
		expect(w.findComponent({ name: "Dropdown" }).props("placement")).toBe("right");
		w.unmount();
		width.mockReturnValue(360);
		w = await mountWith([row("a")]);
		expect(w.findComponent({ name: "Dropdown" }).props("placement")).toBe("left");
		w.unmount();
		width.mockRestore();
	});

	it("decides card or row by the styles' own 48rem: the limit included, whatever the root font size", async () => {
		// The styles say `max-width: 48rem`, so exactly 48rem is a card, and 48rem is
		// 960 px when the browser's default font size is 20px.
		const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get");
		const side = async () => {
			const w = await mountWith([row("a")]);
			const placement = w.findComponent({ name: "Dropdown" }).props("placement");
			w.unmount();
			return placement;
		};
		width.mockReturnValue(768);
		expect(await side()).toBe("left");
		width.mockReturnValue(769);
		expect(await side()).toBe("right");
		document.documentElement.style.fontSize = "20px";
		width.mockReturnValue(900);
		expect(await side()).toBe("left");
		width.mockReturnValue(961);
		expect(await side()).toBe("right");
		document.documentElement.style.fontSize = "";
		width.mockRestore();
	});

	it("reads the list's fractional width, so 768.4px is not a card though clientWidth rounds to 768", async () => {
		const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect");
		const side = async (px) => {
			rect.mockReturnValue({ width: px });
			const w = await mountWith([row("a")]);
			const placement = w.findComponent({ name: "Dropdown" }).props("placement");
			w.unmount();
			return placement;
		};
		expect(await side(767.6)).toBe("left");
		expect(await side(768.4)).toBe("right");
		rect.mockRestore();
	});

	it("gives each cell its lane, for the table and for the folded layouts", async () => {
		// The lanes are CSS (by the list's own width); jsdom lays nothing out, so this
		// pins the names the styles hang on.
		const w = await mountWith([row("a")]);
		const lanes = rowsOf(w)[0]
			.findAll('[role="cell"]')
			.map((c) => c.classes().find((k) => k.startsWith("jv-macro-admin-cell-")));
		expect(lanes).toEqual([
			"jv-macro-admin-cell-macro",
			"jv-macro-admin-cell-owner",
			"jv-macro-admin-cell-schedule",
			"jv-macro-admin-cell-last",
			"jv-macro-admin-cell-actions",
		]);
		expect(w.find(".jv-macro-admin-head").findAll('[role="columnheader"]')).toHaveLength(5);
		// Every filter sits in a bounded slot: a long owner's name cannot stretch it.
		expect(w.findAll(".jv-macro-admin-filter")).toHaveLength(5);
		for (const slot of w.findAll(".jv-macro-admin-filter")) {
			expect(slot.classes()).toContain("min-w-0");
		}
	});
});

describe("Macros AdminTab, rows", () => {
	it("shows the owner, the name, on or off, armed, the schedule and the last run", async () => {
		const w = await mountWith([
			row("a", {
				skip_confirmation: 1,
				schedule_enabled: 1,
				next_run_at: "2999-01-01 09:00:00",
				last_run: { status: "failed", started_at: "2026-01-01 09:00:00" },
			}),
			row("b", {
				enabled: 0,
				owner: "ben@example.test",
				owner_full_name: "ben@example.test",
			}),
		]);
		const [a, b] = rowsOf(w);
		expect(a.text()).toContain("Macro a");
		expect(a.text()).toContain("Asha Rao");
		expect(a.text()).toContain("asha@example.test");
		// The Macros list's own words for the switch (its Status filter).
		expect(a.findAll(".badge").map((x) => x.text())).toEqual(["Enabled", "Armed"]);
		expect(a.text()).toContain("Daily · 9:00 am");
		expect(a.text()).toContain("Failed");
		expect(b.findAll(".badge").map((x) => x.text())).toEqual(["Draft"]);
		expect(b.text()).toContain("Not scheduled");
		expect(b.text()).toContain("Never ran");
		// An owner with no full name is shown once, not twice.
		// In the owner's cell only: the row's action buttons name the owner too.
		expect(b.findAll('[role="cell"]')[1].text().split("ben@example.test")).toHaveLength(2);
	});

	it("gives every cell that can be cut short its full value on hover", async () => {
		const w = await mountWith([
			row("a", {
				macro_name: "A very long macro name that the column cuts short",
				owner: "a.very.long.address@a-long-domain.example.test",
				owner_full_name: "Ashalatha Venkataraman Rao",
				schedule_enabled: 1,
				schedule_frequency: "weekly",
				schedule_weekday: "Wednesday",
			}),
		]);
		const a = rowsOf(w)[0];
		expect(a.find(".jv-macro-admin-open").attributes("title")).toBe(
			"A very long macro name that the column cuts short"
		);
		const titles = a.findAll("[title]").map((el) => el.attributes("title"));
		expect(titles).toContain("Ashalatha Venkataraman Rao");
		expect(titles).toContain("a.very.long.address@a-long-domain.example.test");
		const schedule = a.find(".jv-macro-admin-schedule");
		expect(schedule.text()).toContain("Weekly");
		expect(schedule.attributes("title")).toBe(schedule.text());
	});

	it("is a table to a screen reader, in a box the keyboard can scroll", async () => {
		const w = await mountWith([row("a"), row("b")]);
		const table = w.find("[role='table']");
		expect(table.findAll("[role='columnheader']").map((h) => h.text())).toEqual([
			"Macro",
			"Owner",
			"Schedule",
			"Last run",
			"Actions",
		]);
		const lines = table.findAll("[role='row']");
		expect(lines).toHaveLength(3);
		expect(lines[1].findAll("[role='cell']")).toHaveLength(5);
		const box = w.find(".jv-macro-admin-scroll");
		expect(box.attributes("tabindex")).toBe("0");
		expect(box.attributes("aria-label")).toBe("Macros list");
		expect(box.classes()).toContain("overflow-x-auto");
		expect(box.find("[role='table']").exists()).toBe(true);
	});

	it("says in words that an administrator stopped the last run", async () => {
		const w = await mountWith([
			row("a", { last_run: { status: "stopped", stopped_by_admin: 1 } }),
			row("b", { last_run: { status: "stopped", stopped_by_admin: 0 } }),
		]);
		const [a, b] = rowsOf(w).map((r) => r.find(".jv-macro-admin-last"));
		expect(a.text()).toBe("Stopped by an admin");
		// A warning, as an engine stop is on the owner's list; the owner's own Stop is not.
		expect(a.find(".text-ink-amber-3").exists()).toBe(true);
		expect(b.text()).toBe("Stopped");
		expect(b.find(".text-ink-amber-3").exists()).toBe(false);
	});

	it("says Running under the last run when the live run is an older one", async () => {
		// The last run failed; an earlier one is still going, and that is what Stop
		// acts on. The row read "Failed" next to a Stop button with no explanation.
		const w = await mountWith([
			row("a", { live_run: "RUN-0", last_run: { status: "failed" } }),
			row("b", { live_run: "RUN-1", last_run: { status: "running" } }),
			row("c", { last_run: { status: "failed" } }),
		]);
		const [a, b, c] = rowsOf(w);
		expect(a.find(".jv-macro-admin-last").text()).toBe("Failed");
		expect(a.find(".jv-macro-admin-running").text()).toBe("Running");
		// Its last-run cell already says so.
		expect(b.find(".jv-macro-admin-running").exists()).toBe(false);
		expect(b.find(".jv-macro-admin-last").text()).toBe("Running");
		expect(c.find(".jv-macro-admin-running").exists()).toBe(false);
	});

	it("offers Stop run only on a row with a live run", async () => {
		const w = await mountWith([
			row("a", { live_run: "RUN-1", last_run: { status: "running" } }),
			row("b", { last_run: { status: "completed" } }),
		]);
		const [a, b] = rowsOf(w);
		expect(a.find(".jv-macro-admin-stop").exists()).toBe(true);
		expect(a.text()).toContain("Running");
		expect(b.find(".jv-macro-admin-stop").exists()).toBe(false);
	});

	it("opens a row in the read-only dialog, from a real button that names it", async () => {
		const w = await mountWith([row("a")]);
		expect(w.find(".stub-detail").exists()).toBe(false);
		const open = rowsOf(w)[0].find("button.jv-macro-admin-open");
		expect(open.attributes("type")).toBe("button");
		expect(open.attributes("aria-label")).toBe(
			"Open Macro a, owned by Asha Rao (asha@example.test). Read-only."
		);
		await open.trigger("click");
		const detail = w.find(".stub-detail");
		expect(detail.attributes("data-name")).toBe("a");
		expect(detail.attributes("data-open")).toBe("1");
	});

	it("loads the next page after the rows it has, and never lists a macro twice", async () => {
		const w = await mountWith([row("a"), row("b")], { total: 3, has_more: true });
		expect(w.text()).toContain("Showing 2 of 3");
		api.adminListMacros.mockResolvedValue(page([row("b"), row("c")], { total: 3, start: 2 }));
		await button(w, "Load more").trigger("click");
		await flushPromises();
		expect(lastCall().start).toBe(2);
		expect(rowsOf(w)).toHaveLength(3);
		expect(w.text()).toContain("Showing 3 of 3");
		expect(button(w, "Load more")).toBeUndefined();
	});

	it("says a failed Load more beside the button, not in the banner a screen above", async () => {
		const w = await mountWith(many(20), { total: 45, has_more: true });
		api.adminListMacros.mockRejectedValue(new Error("Server is busy."));
		await button(w, "Load more").trigger("click");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(20);
		expect(w.find(".jv-macro-admin-more-error").text()).toBe("Server is busy.");
		expect(w.text()).not.toContain("Showing what was loaded before");
		// The same button tries again, and the message goes when it works.
		api.adminListMacros.mockResolvedValue(page(many(20, 20), { total: 45, has_more: true }));
		await w.find(".jv-macro-admin-more").trigger("click");
		await flushPromises();
		expect(lastCall().start).toBe(20);
		expect(rowsOf(w)).toHaveLength(40);
		expect(w.find(".jv-macro-admin-more-error").exists()).toBe(false);
		expect(w.find(".jv-macro-admin-more").text()).toBe("Load more");
	});

	// A loading button is disabled, and a browser takes focus off a disabled button.
	// jsdom does neither (and will not blur a disabled one), so the stub stays enabled
	// and these tests take the focus away themselves. `press` is a key press on a
	// control (how the tab knows the keyboard is in use), then its click.
	const pending = () => {
		let answer;
		let fail;
		api.adminListMacros.mockReturnValue(
			new Promise((r, j) => {
				answer = r;
				fail = j;
			})
		);
		const settle = (v) => answer(v);
		settle.fail = (e) => fail(e);
		return settle;
	};
	// A browser's click says how many pointer clicks made it: none when it came
	// from a key (or from assistive technology pressing the button), one for a mouse
	// or a finger.
	const fire = async (el, detail) => {
		el.element.dispatchEvent(new MouseEvent("click", { bubbles: true, detail }));
		await flushPromises();
	};
	const press = async (el) => {
		el.element.focus();
		await el.trigger("keydown", { key: "Enter" });
		await fire(el, 0);
	};
	const click = async (el) => {
		el.element.focus(); // a click focuses a button in Chrome
		await el.trigger("pointerdown");
		await fire(el, 1);
	};
	const at = () => document.activeElement;
	const longList = () =>
		mountWith(many(20), { total: 45, has_more: true }, { attachTo: document.body });

	it("keeps keyboard focus through Load more, Refresh and Clear", async () => {
		const w = await longList();

		// Load more: focus goes to the first row it brought.
		let answer = pending();
		await press(w.find(".jv-macro-admin-more"));
		at().blur();
		answer(page(many(20, 20), { total: 45, has_more: true, start: 20 }));
		await flushPromises();
		expect(at()).toBe(rowsOf(w)[20].find(".jv-macro-admin-open").element);

		// Refresh: back on Refresh.
		answer = pending();
		await press(w.find(".jv-macro-admin-refresh"));
		at().blur();
		answer(page(many(40), { total: 45, has_more: true }));
		await flushPromises();
		expect(at()).toBe(w.find(".jv-macro-admin-refresh").element);

		// Clear removes itself: focus goes to the search box.
		api.adminListMacros.mockResolvedValue(page(many(3)));
		await control(w, "Hold").find("select").setValue("1");
		await flushPromises();
		answer = pending();
		await press(w.find(".jv-macro-admin-clear"));
		at().blur();
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(w.find(".jv-macro-admin-clear").exists()).toBe(false);
		expect(at()).toBe(control(w, "Search").find("input").element);
		w.unmount();
	});

	it("leaves focus alone after a click: a tooltip or a phone's keyboard would follow it", async () => {
		const w = await longList();
		let answer = pending();
		await click(w.find(".jv-macro-admin-refresh"));
		at().blur();
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(at()).toBe(document.body);

		api.adminListMacros.mockResolvedValue(page(many(3)));
		await control(w, "Hold").find("select").setValue("1");
		await flushPromises();
		answer = pending();
		await click(w.find(".jv-macro-admin-clear"));
		at().blur();
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(w.find(".jv-macro-admin-clear").exists()).toBe(false);
		expect(at()).toBe(document.body);
		w.unmount();
	});

	it("goes by the click itself: a key press earlier does not make a mouse click a key press", async () => {
		// The commonest path: type a search, then click Refresh.
		const w = await longList();
		const search = control(w, "Search").find("input");
		await search.trigger("keydown", { key: "i" });
		const answer = pending();
		// No pointerdown is sent: the key flag still says "keyboard", and only the
		// click's own count says otherwise.
		const refresh = w.find(".jv-macro-admin-refresh");
		refresh.element.focus();
		await fire(refresh, 1);
		at().blur();
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(at()).toBe(document.body);
		w.unmount();
	});

	it("restores focus for a press with no key and no pointer, as assistive technology makes, on every control", async () => {
		// Nothing has been pressed in the tab: only the click says how it came. Each
		// control has to hand its click on for that.
		const assistive = async (w, el) => {
			const answer = pending();
			el.element.focus();
			await fire(el, 0);
			at().blur();
			return answer;
		};
		// Refresh, Load more (to the first new row), Clear (to the search box).
		let w = await longList();
		let answer = await assistive(w, w.find(".jv-macro-admin-refresh"));
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(at()).toBe(w.find(".jv-macro-admin-refresh").element);

		answer = await assistive(w, w.find(".jv-macro-admin-more"));
		answer(page(many(20, 20), { total: 45, has_more: true, start: 20 }));
		await flushPromises();
		expect(at()).toBe(rowsOf(w)[20].find(".jv-macro-admin-open").element);

		api.adminListMacros.mockResolvedValue(page(many(3)));
		await control(w, "Hold").find("select").setValue("1");
		await flushPromises();
		answer = await assistive(w, w.find(".jv-macro-admin-clear"));
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(at()).toBe(control(w, "Search").find("input").element);

		// The banner's Try again, after a re-read that failed.
		api.adminListMacros.mockRejectedValue(new Error("Busy."));
		await w.find(".jv-macro-admin-refresh").trigger("click");
		await flushPromises();
		const bannerRetry = w
			.findAll("button.stub-button")
			.find((b) => b.text() === "Try again" && !b.classes().includes("jv-macro-admin-more"));
		answer = await assistive(w, bannerRetry);
		answer.fail(new Error("Still busy."));
		await flushPromises();
		expect(at()).toBe(bannerRetry.element);

		// "Clear filters" in the empty state.
		api.adminListMacros.mockResolvedValue(page([]));
		await control(w, "Hold").find("select").setValue("1");
		await flushPromises();
		answer = await assistive(w, button(w, "Clear filters"));
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(at()).toBe(control(w, "Search").find("input").element);
		w.unmount();

		// Try again when nothing could be loaded.
		api.adminListMacros.mockRejectedValue(new Error("Server is busy."));
		w = mount(AdminTab, { attachTo: document.body });
		await flushPromises();
		const firstRetry = button(w, "Try again");
		answer = await assistive(w, firstRetry);
		answer.fail(new Error("Still busy."));
		await flushPromises();
		expect(at()).toBe(firstRetry.element);
		w.unmount();
	});

	it("leaves focus alone when the pointer was used while it loaded", async () => {
		const added = vi.spyOn(document, "addEventListener");
		const w = await longList();
		const [, pageListener, capture] = added.mock.calls.find(
			([type]) => type === "pointerdown"
		);
		added.mockRestore();
		const answer = pending();
		await press(w.find(".jv-macro-admin-refresh"));
		at().blur();
		// A click on a part of the tab that takes no focus.
		await w.find(".jv-macro-admin-toolbar").trigger("pointerdown");
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(at()).toBe(document.body);

		// And one outside the tab (the sidebar, the page header): the same.
		const outside = document.createElement("div");
		document.body.appendChild(outside);
		const again = pending();
		await press(w.find(".jv-macro-admin-refresh"));
		at().blur();
		outside.dispatchEvent(new Event("pointerdown", { bubbles: true }));
		again(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(at()).toBe(document.body);
		outside.remove();

		// The page's own listener goes with the tab: the same function, the same phase.
		const removed = vi.spyOn(document, "removeEventListener");
		w.unmount();
		expect(removed).toHaveBeenCalledWith("pointerdown", pageListener, capture);
		removed.mockRestore();
	});

	it("keeps focus in the tab after Try again when nothing could be loaded", async () => {
		api.adminListMacros.mockRejectedValue(new Error("Server is busy."));
		const w = mount(AdminTab, { attachTo: document.body });
		await flushPromises();
		const retry = button(w, "Try again");
		const answer = pending();
		await press(retry);
		at().blur();
		answer(page(many(3)));
		await flushPromises();
		// The button is gone with the error: the list has it.
		expect(retry.element.isConnected).toBe(false);
		expect(at()).toBe(w.find(".jv-macro-admin-scroll").element);
		w.unmount();
	});

	it("does not take focus from one of the tab's own dialogs, or give it to a disabled button", async () => {
		const w = await longList();
		const refresh = w.find(".jv-macro-admin-refresh");
		for (const open of [
			() => rowsOf(w)[0].find(".jv-macro-admin-hold").trigger("click"),
			() => rowsOf(w)[0].find(".jv-macro-admin-handover").trigger("click"),
			() => rowsOf(w)[0].find(".jv-macro-admin-open").trigger("click"),
		]) {
			const answer = pending();
			await press(refresh);
			await open();
			// A pick from the Actions menu settles a moment after the press, with focus
			// on the row's Actions button, which the open dialog disables. A browser
			// then drops the focus; jsdom will not blur a disabled button, so focus is
			// taken by something that then goes away.
			await flushPromises();
			const gone = document.createElement("button");
			document.body.appendChild(gone);
			gone.focus();
			gone.remove();
			answer(page(many(20), { total: 45, has_more: true }));
			await flushPromises();
			expect(at()).toBe(document.body);
			for (const name of ["MacroHoldDialog", "MacroHandoverDialog", "MacroAdminDialog"]) {
				const d = w.findComponent({ name });
				if (d.exists()) d.vm.$emit("update:modelValue", false);
			}
			await flushPromises();
		}
		// What had focus is disabled when the load answers: the list, not a dead button.
		const answer = pending();
		await press(refresh);
		at().blur();
		refresh.element.disabled = true;
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(at()).toBe(w.find(".jv-macro-admin-scroll").element);
		w.unmount();
	});

	it("leaves focus where the user moved it, and out of an open dialog", async () => {
		const w = await longList();
		const search = control(w, "Search").find("input");
		let answer = pending();
		await press(w.find(".jv-macro-admin-refresh"));
		search.element.focus();
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(at()).toBe(search.element);

		// A dialog opened over the tab meanwhile: focus is its business.
		answer = pending();
		await press(w.find(".jv-macro-admin-refresh"));
		const dialog = document.createElement("div");
		dialog.setAttribute("role", "alertdialog");
		document.body.appendChild(dialog);
		at().blur();
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(at()).toBe(document.body);
		dialog.remove();
		w.unmount();
	});

	it("keeps focus on Load more when it brings nothing or fails, and on the list when the control is gone", async () => {
		const w = await longList();
		// No new row (they were deleted meanwhile): stays on the button.
		let answer = pending();
		await press(w.find(".jv-macro-admin-more"));
		at().blur();
		answer(page([], { total: 45, has_more: true, start: 20 }));
		await flushPromises();
		expect(at()).toBe(w.find(".jv-macro-admin-more").element);

		// It failed: the same button, now Try again.
		answer = pending();
		await press(w.find(".jv-macro-admin-more"));
		at().blur();
		answer.fail(new Error("Server is busy."));
		await flushPromises();
		expect(w.find(".jv-macro-admin-more").text()).toBe("Try again");
		expect(at()).toBe(w.find(".jv-macro-admin-more").element);

		// The control that had focus is gone after the load (the banner's Try again,
		// once the re-read works): the list takes it.
		api.adminListMacros.mockRejectedValue(new Error("Still busy."));
		await press(w.find(".jv-macro-admin-refresh"));
		await flushPromises();
		const retry = w
			.findAll("button.stub-button")
			.find((b) => b.text() === "Try again" && !b.classes().includes("jv-macro-admin-more"));
		answer = pending();
		await press(retry);
		at().blur();
		answer(page(many(20), { total: 45, has_more: true }));
		await flushPromises();
		expect(retry.element.isConnected).toBe(false);
		expect(at()).toBe(w.find(".jv-macro-admin-scroll").element);
		w.unmount();
	});

	it("says each failure in its own place as one follows another", async () => {
		const w = await longList();
		api.adminListMacros.mockRejectedValue(new Error("Page failed."));
		await w.find(".jv-macro-admin-more").trigger("click");
		await flushPromises();
		expect(w.find(".jv-macro-admin-more-error").text()).toBe("Page failed.");
		// A Refresh that fails next is a re-read: the banner, not the footer.
		api.adminListMacros.mockRejectedValue(new Error("Re-read failed."));
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(w.find(".jv-macro-admin-more-error").exists()).toBe(false);
		expect(w.text()).toContain("Re-read failed. Showing what was loaded before.");
		// A filter change that fails clears the rows: the error stands alone.
		api.adminListMacros.mockRejectedValue(new Error("Filter failed."));
		await control(w, "Hold").find("select").setValue("1");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(0);
		expect(w.text()).toContain("Filter failed.");
		expect(w.text()).not.toContain("Showing what was loaded before");
		w.unmount();
	});

	it("sends one request when Refresh or Load more is pressed with a search still waiting", async () => {
		const w = await longList();
		for (const cls of [".jv-macro-admin-refresh", ".jv-macro-admin-more"]) {
			api.adminListMacros.mockClear();
			api.adminListMacros.mockResolvedValue(page(many(20), { total: 45, has_more: true }));
			vi.useFakeTimers();
			await control(w, "Search").find("input").setValue(`inv${cls.length}`);
			await w.find(cls).trigger("click");
			vi.advanceTimersByTime(1000);
			vi.useRealTimers();
			await flushPromises();
			expect(api.adminListMacros).toHaveBeenCalledTimes(1);
		}
		w.unmount();
	});

	it("puts focus back on Refresh when a typed search disabled it under the keyboard", async () => {
		// Tab from the search box lands on Refresh; the search then runs.
		const w = await longList();
		vi.useFakeTimers();
		const search = control(w, "Search").find("input");
		search.element.focus();
		await search.trigger("keydown", { key: "i" });
		await search.setValue("inv");
		const refresh = w.find(".jv-macro-admin-refresh");
		refresh.element.focus();
		const answer = pending();
		vi.advanceTimersByTime(300);
		vi.useRealTimers();
		at().blur();
		answer(page([row("inv")]));
		await flushPromises();
		expect(at()).toBe(refresh.element);
		w.unmount();
	});

	it("re-reads every row on screen on Refresh, however many were loaded", async () => {
		// It asked for one page of at most 100, so past 100 rows a Refresh (or a
		// stop) cut the list back to its first 100.
		const all = many(130);
		api.adminListMacros.mockImplementation(serve(all));
		const w = mount(AdminTab);
		await flushPromises();
		for (let i = 0; i < 6; i++) {
			await button(w, "Load more").trigger("click");
			await flushPromises();
		}
		expect(rowsOf(w)).toHaveLength(130);

		api.adminListMacros.mockClear();
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(130);
		expect(w.text()).toContain("Showing 130 of 130");
		// In pages the server accepts: never one request for more than 100.
		expect(api.adminListMacros.mock.calls.map((c) => [c[0].start, c[0].pageLength])).toEqual([
			[0, 100],
			[100, 30],
		]);
	});

	it("keeps Load more going after a Refresh of a long list", async () => {
		const all = many(150);
		api.adminListMacros.mockImplementation(serve(all));
		const w = mount(AdminTab);
		await flushPromises();
		for (let i = 0; i < 5; i++) {
			await button(w, "Load more").trigger("click");
			await flushPromises();
		}
		expect(rowsOf(w)).toHaveLength(120);
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(120);
		await button(w, "Load more").trigger("click");
		await flushPromises();
		expect(lastCall()).toMatchObject({ start: 120, pageLength: 20 });
		expect(rowsOf(w)).toHaveLength(140);
	});

	it("reloads the list when the opened macro turns out to be deleted", async () => {
		const w = await mountWith([row("a"), row("b")]);
		await rowsOf(w)[0].find("button.jv-macro-admin-open").trigger("click");
		api.adminListMacros.mockResolvedValue(page([row("b")]));
		w.findComponent({ name: "MacroAdminDialog" }).vm.$emit("gone", "a");
		await flushPromises();
		expect(rowsOf(w).map((r) => r.attributes("data-macro"))).toEqual(["b"]);
	});
});

describe("Macros AdminTab, filters", () => {
	it("keeps the four selects behind one Filters control, owner and search in sight", async () => {
		const w = await mountWith([row("a")], undefined, { filtersClosed: true });
		const shown = () =>
			["Owner", "Armed", "Schedule", "Runs", "Hold"].map(
				(n) => !control(w, n).element.closest('[style*="display: none"]')
			);
		expect(shown()).toEqual([true, false, false, false, false]);
		const toggle = w.find(".jv-macro-admin-filters-toggle");
		expect(toggle.text()).toBe("Filters");
		expect(toggle.attributes("aria-expanded")).toBe("false");
		// The panel stays in the page, hidden, so aria-controls always has its target.
		expect(toggle.attributes("aria-controls")).toBe("jv-macro-admin-more-filters");
		expect(w.find("#jv-macro-admin-more-filters").exists()).toBe(true);
		await toggle.trigger("click");
		expect(shown()).toEqual([true, true, true, true, true]);
		expect(toggle.attributes("aria-expanded")).toBe("true");
		await toggle.trigger("click");
		expect(shown()).toEqual([true, false, false, false, false]);
	});

	it("counts the filters set behind it, and opens them for the Armed and scheduled preset", async () => {
		const w = await mountWith([row("a")], undefined, { filtersClosed: true });
		await button(w, "Armed and scheduled").trigger("click");
		await flushPromises();
		expect(w.find("#jv-macro-admin-more-filters").attributes("style") || "").not.toContain(
			"none"
		);
		expect(w.find(".jv-macro-admin-filters-toggle").text()).toBe("Filters (2)");
		await control(w, "Runs").find("select").setValue("1");
		await flushPromises();
		expect(w.find(".jv-macro-admin-filters-toggle").text()).toBe("Filters (3)");
		await w.find(".jv-macro-admin-filters-toggle").trigger("click");
		expect(w.find(".jv-macro-admin-filters-toggle").text()).toBe("Filters (3)");
	});

	it("labels every control", async () => {
		const w = await mountWith([row("a")]);
		expect(w.findAll(".stub-label").map((l) => l.text())).toEqual([
			"Search",
			"Owner",
			"Armed",
			"Schedule",
			"Runs",
			"Hold",
		]);
	});

	it("explains Armed where the admin sees it: on the filter and on the badge", async () => {
		const HELP = "Armed: this macro's runs write without asking for confirmation.";
		const w = await mountWith([row("a", { skip_confirmation: 1 }), row("b")]);
		expect(control(w, "Armed").attributes("title")).toBe(HELP);
		const [a, b] = rowsOf(w);
		expect(a.find(".jv-macro-admin-armed").attributes("title")).toBe(HELP);
		expect(a.find(".jv-macro-admin-armed .badge").text()).toBe("Armed");
		expect(b.find(".jv-macro-admin-armed").exists()).toBe(false);
	});

	it("finds the armed and scheduled macros in one step, and shows what it set", async () => {
		const w = await mountWith([row("a")]);
		const preset = () => w.find(".jv-macro-admin-preset");
		expect(preset().text()).toBe("Armed and scheduled");
		expect(preset().attributes("aria-pressed")).toBe("false");
		const calls = api.adminListMacros.mock.calls.length;
		await preset().trigger("click");
		await flushPromises();
		// One request, with both.
		expect(api.adminListMacros.mock.calls.length).toBe(calls + 1);
		expect(lastCall()).toMatchObject({ filters: { armed: "1", scheduled: "1" }, start: 0 });
		expect(control(w, "Armed").find("select").element.value).toBe("1");
		expect(control(w, "Schedule").find("select").element.value).toBe("1");
		expect(preset().attributes("aria-pressed")).toBe("true");
		// Changing either select by hand un-presses it; pressing it again clears both.
		await control(w, "Schedule").find("select").setValue("0");
		await flushPromises();
		expect(preset().attributes("aria-pressed")).toBe("false");
		await preset().trigger("click");
		await preset().trigger("click");
		await flushPromises();
		expect(lastCall().filters).toEqual({});
	});

	it("asks for the owner choices once, not with every page or filter", async () => {
		const w = await mountWith([row("a"), row("b")], { total: 3, has_more: true });
		expect(api.adminMacroOwners).toHaveBeenCalledTimes(1);
		await control(w, "Armed").find("select").setValue("1");
		await control(w, "Armed").find("select").setValue("");
		await flushPromises();
		await button(w, "Load more").trigger("click");
		await flushPromises();
		expect(api.adminMacroOwners).toHaveBeenCalledTimes(1);
		// Refresh is the one thing that asks again.
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(api.adminMacroOwners).toHaveBeenCalledTimes(2);
	});

	it("lists the macros when the owner choices cannot be loaded, and says when there are more", async () => {
		api.adminMacroOwners.mockRejectedValue(new Error("nope"));
		let w = await mountWith([row("a")]);
		expect(rowsOf(w)).toHaveLength(1);
		expect(w.find("[role='alert']").exists()).toBe(false);
		expect(control(w, "Owner").findAll("option")).toHaveLength(1);

		api.adminMacroOwners.mockResolvedValue({ owners: OWNERS, more: true });
		w = await mountWith([row("a")]);
		const last = control(w, "Owner").findAll("option").at(-1);
		expect(last.text()).toBe("More owners: use Search");
		expect(last.attributes("disabled")).toBeDefined();
	});

	it("drops the old filter's rows when a filter change fails, so Load more cannot mix them", async () => {
		// The rows stayed, with Load more: it then fetched the NEW filter's second
		// page and appended it to the OLD filter's rows.
		const w = await mountWith(many(20), { total: 60, has_more: true });
		api.adminListMacros.mockRejectedValue(new Error("Server is busy"));
		await control(w, "Armed").find("select").setValue("1");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(0);
		expect(button(w, "Load more")).toBeUndefined();
		expect(w.find("[role='alert']").text()).toContain("Server is busy");
		expect(w.text()).not.toContain("Showing what was loaded before.");

		// Try again reads the new filter from its first row.
		api.adminListMacros.mockResolvedValue(page([row("armed")]));
		await button(w, "Try again").trigger("click");
		await flushPromises();
		expect(lastCall()).toMatchObject({ filters: { armed: "1" }, start: 0 });
		expect(rowsOf(w).map((r) => r.attributes("data-macro"))).toEqual(["armed"]);
	});

	it("starts over, not at the next page, when Load more is pressed with a search still pending", async () => {
		const w = await mountWith(many(20), { total: 60, has_more: true });
		vi.useFakeTimers();
		await control(w, "Search").find("input").setValue("inv");
		api.adminListMacros.mockResolvedValue(page([row("inv")]));
		await button(w, "Load more").trigger("click");
		vi.useRealTimers();
		await flushPromises();
		expect(lastCall()).toMatchObject({ search: "inv", start: 0 });
		expect(rowsOf(w).map((r) => r.attributes("data-macro"))).toEqual(["inv"]);
	});

	it("offers the owners the server sent, with how many macros each has", async () => {
		const w = await mountWith([row("a")]);
		const options = control(w, "Owner")
			.findAll("option")
			.map((o) => [o.attributes("value"), o.text()]);
		expect(options).toEqual([
			["", "All owners"],
			["asha@example.test", "Asha Rao (2)"],
			["ben@example.test", "ben@example.test (1)"],
		]);
	});

	it("sends each filter, and only the ones that are set, from the first row", async () => {
		const w = await mountWith([row("a")]);
		expect(lastCall()).toEqual({ search: "", filters: {}, start: 0, pageLength: 20 });
		for (const [label, value, sent] of [
			["Owner", "ben@example.test", { owner: "ben@example.test" }],
			["Armed", "1", { owner: "ben@example.test", armed: "1" }],
			["Schedule", "0", { owner: "ben@example.test", armed: "1", scheduled: "0" }],
			[
				"Runs",
				"1",
				{ owner: "ben@example.test", armed: "1", scheduled: "0", live_run: "1" },
			],
			["Armed", "", { owner: "ben@example.test", scheduled: "0", live_run: "1" }],
		]) {
			await control(w, label).find("select").setValue(value);
			await flushPromises();
			expect(lastCall().filters, label).toEqual(sent);
			expect(lastCall().start).toBe(0);
		}
	});

	it("waits for the typing to stop before it searches", async () => {
		const w = await mountWith([row("a")]);
		vi.useFakeTimers();
		const calls = api.adminListMacros.mock.calls.length;
		const input = control(w, "Search").find("input");
		await input.setValue("in");
		await input.setValue("  invoices ");
		expect(api.adminListMacros.mock.calls.length).toBe(calls);
		vi.advanceTimersByTime(300);
		expect(api.adminListMacros.mock.calls.length).toBe(calls + 1);
		expect(lastCall().search).toBe("invoices");
	});

	it("asks once when a filter changes while a typed search is still waiting", async () => {
		const w = await mountWith([row("a")]);
		api.adminListMacros.mockClear();
		vi.useFakeTimers();
		await control(w, "Search").find("input").setValue("stock");
		await button(w, "Armed and scheduled").trigger("click");
		vi.advanceTimersByTime(1000);
		vi.useRealTimers();
		await flushPromises();
		expect(api.adminListMacros).toHaveBeenCalledTimes(1);
		expect(lastCall()).toMatchObject({
			search: "stock",
			filters: { armed: "1", scheduled: "1" },
		});
	});

	it("marks the list busy while another filter's rows are still on screen", async () => {
		const w = await mountWith([row("a")]);
		const box = () => w.find(".jv-macro-admin-scroll");
		expect(box().attributes("aria-busy")).toBe("false");
		let answer;
		api.adminListMacros.mockReturnValue(new Promise((r) => (answer = r)));
		await control(w, "Hold").find("select").setValue("1");
		expect(box().attributes("aria-busy")).toBe("true");
		expect(box().classes()).toContain("opacity-50");
		answer(page([row("b")]));
		await flushPromises();
		expect(box().attributes("aria-busy")).toBe("false");
		// A Refresh shows the same rows again: nothing on screen is stale.
		api.adminListMacros.mockReturnValue(new Promise((r) => (answer = r)));
		await button(w, "Refresh").trigger("click");
		expect(box().attributes("aria-busy")).toBe("false");
		answer(page([row("b")]));
		await flushPromises();
	});

	it("does not ask for the owner choices again when a search runs", async () => {
		const w = await mountWith([row("a")]);
		expect(api.adminMacroOwners).toHaveBeenCalledTimes(1);
		vi.useFakeTimers();
		const calls = api.adminListMacros.mock.calls.length;
		const input = control(w, "Search").find("input");
		await input.setValue("in");
		await input.setValue("invoices");
		vi.advanceTimersByTime(300);
		vi.useRealTimers();
		await flushPromises();
		expect(api.adminListMacros.mock.calls.length).toBe(calls + 1);
		expect(api.adminMacroOwners).toHaveBeenCalledTimes(1);
	});
});

describe("Macros AdminTab, Stop run", () => {
	const HOSTILE = `<img src=x onerror=alert(1)> & "co"`;
	const live = (extra = {}) =>
		row("a", { live_run: "RUN-1", last_run: { status: "running" }, ...extra });
	const stopButton = (w) => rowsOf(w)[0].find(".jv-macro-admin-stop");

	it("asks first, naming the macro and its owner in the first sentence", async () => {
		const w = await mountWith([live()]);
		await stopButton(w).trigger("click");
		await flushPromises();
		const asked = confirm.mock.calls[0][0];
		expect(asked.title).toBe("Stop this run?");
		expect(asked.message.split("?")[0]).toBe(
			"Stop the run of “Macro a”, owned by Asha Rao (asha@example.test)"
		);
		expect(asked.message).toContain("The owner will see that an administrator stopped it.");
		expect(asked.confirmLabel).toBe("Stop run");
		expect(asked.danger).toBe(true);
		expect(stopButton(w).attributes("aria-label")).toBe(
			"Stop run of Macro a, owned by Asha Rao (asha@example.test)"
		);
	});

	it("says a scheduled macro will run again, and only then", async () => {
		// A stop ends one run: an admin who wants it not to start again needs Hold.
		const asked = async (extra) => {
			confirm.mockClear();
			const w = await mountWith([live(extra)]);
			await stopButton(w).trigger("click");
			await flushPromises();
			return confirm.mock.calls[0][0].message;
		};
		expect(await asked({ schedule_enabled: 1 })).toContain(
			"the macro stays on its schedule and will run again. To prevent that, put it on hold."
		);
		expect(await asked({ schedule_enabled: 0 })).not.toContain("will run again");
		expect(await asked({ schedule_enabled: 1, enabled: 0 })).not.toContain("will run again");
		expect(await asked({ schedule_enabled: 1, admin_hold: 1 })).not.toContain(
			"will run again"
		);
	});

	it("does nothing when the admin cancels", async () => {
		confirm.mockResolvedValue(false);
		const w = await mountWith([live()]);
		const calls = api.adminListMacros.mock.calls.length;
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(api.adminStopRun).not.toHaveBeenCalled();
		expect(api.adminListMacros.mock.calls.length).toBe(calls);
		expect(toast.success).not.toHaveBeenCalled();
	});

	it("stops the live run, says so, and re-reads the rows on screen", async () => {
		const w = await mountWith([live(), row("b")]);
		api.adminListMacros.mockResolvedValue(
			page([row("a", { last_run: { status: "stopped", stopped_by_admin: 1 } }), row("b")])
		);
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(api.adminStopRun).toHaveBeenCalledWith("RUN-1");
		expect(toast.success).toHaveBeenCalledWith("Stopped the run of “Macro a”");
		expect(lastCall()).toMatchObject({ start: 0, pageLength: 20 });
		expect(stopButton(w).exists()).toBe(false);
		// The row's new state, in words.
		expect(rowsOf(w)[0].find(".jv-macro-admin-last").text()).toBe("Stopped by an admin");
	});

	it("puts focus on the row's own button after a stop, not nowhere", async () => {
		// The Stop button that had focus is removed by the reload.
		const w = await mountWith([row("z"), live()], undefined, { attachTo: document.body });
		api.adminListMacros.mockResolvedValue(
			page([row("z"), row("a", { last_run: { status: "stopped", stopped_by_admin: 1 } })])
		);
		const stopOf = () => rowsOf(w)[1].find(".jv-macro-admin-stop");
		stopOf().element.focus();
		await stopOf().trigger("click");
		await flushPromises();
		expect(stopOf().exists()).toBe(false);
		expect(document.activeElement).toBe(rowsOf(w)[1].find(".jv-macro-admin-open").element);

		// The row itself went (its macro was deleted meanwhile): the list takes it.
		api.adminListMacros.mockResolvedValue(page([row("z"), live()]));
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		api.adminListMacros.mockResolvedValue(page([row("z")]));
		await stopOf().trigger("click");
		await flushPromises();
		expect(document.activeElement).toBe(w.find(".jv-macro-admin-scroll").element);
		w.unmount();
	});

	it("puts a name in the confirmation as text and in the toast escaped", async () => {
		// The confirmation renders text ({{ }}): escaping there would show the
		// entities. The toast renders HTML: unescaped, the name would be markup.
		const w = await mountWith([live({ macro_name: HOSTILE, owner_full_name: HOSTILE })]);
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(confirm.mock.calls[0][0].message).toContain(`“${HOSTILE}”, owned by ${HOSTILE} (`);
		const said = toast.success.mock.calls[0][0];
		expect(said).toBe(
			"Stopped the run of “&lt;img src=x onerror=alert(1)&gt; &amp; &quot;co&quot;”"
		);
		expect(said).not.toContain("<img");
		// And on the page it is text: no element was made from it.
		expect(w.find("img").exists()).toBe(false);
		expect(rowsOf(w)[0].text()).toContain(HOSTILE);
	});

	it("says the run had already ended when the server says so, escaped", async () => {
		api.adminStopRun.mockResolvedValue({
			ok: true,
			stopped: false,
			status: "completed",
			message: "This run had <b>already</b> ended.",
		});
		const w = await mountWith([live()]);
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(toast.success).not.toHaveBeenCalled();
		expect(toast.create).toHaveBeenCalledWith({
			message: "This run had &lt;b&gt;already&lt;/b&gt; ended.",
			type: "info",
		});
		expect(api.adminListMacros.mock.calls.length).toBe(2);
	});

	it("shows why a stop failed, escaped, and re-reads the row", async () => {
		// The server escapes what it throws; the error formatter decodes it back to
		// text. Into a toast (HTML) it has to be escaped again.
		api.adminStopRun.mockRejectedValue(
			new Error("This run could not be stopped: &lt;img src=x onerror=alert(1)&gt;")
		);
		const w = await mountWith([live()]);
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith(
			"This run could not be stopped: &lt;img src=x onerror=alert(1)&gt;"
		);
		expect(toast.success).not.toHaveBeenCalled();
		// Still live as far as the server says: the button is back, and usable.
		expect(api.adminListMacros.mock.calls.length).toBe(2);
		expect(stopButton(w).exists()).toBe(true);
		expect(stopButton(w).attributes("disabled")).toBeUndefined();
	});

	it("does not leave a Stop button on a run that turned out to be gone", async () => {
		// "This run no longer exists.": the row was stale. It kept its Stop button,
		// and every later click failed the same way until a manual Refresh.
		api.adminStopRun.mockRejectedValue(new Error("This run no longer exists."));
		const w = await mountWith([live()]);
		api.adminListMacros.mockResolvedValue(
			page([row("a", { last_run: { status: "failed" } })])
		);
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith("This run no longer exists.");
		expect(stopButton(w).exists()).toBe(false);
		expect(rowsOf(w)[0].find(".jv-macro-admin-last").text()).toBe("Failed");
	});

	it("disables every Stop while one is in flight", async () => {
		let answer;
		api.adminStopRun.mockReturnValue(new Promise((r) => (answer = r)));
		const w = await mountWith([live(), row("b", { live_run: "RUN-2" })]);
		await stopButton(w).trigger("click");
		await flushPromises();
		const stops = w.findAll(".jv-macro-admin-stop");
		expect(stops.map((s) => s.attributes("disabled"))).toEqual(["", ""]);
		expect(stops[0].attributes("data-loading")).toBe("1");
		await stops[1].trigger("click");
		expect(confirm).toHaveBeenCalledTimes(1);
		answer({ ok: true, stopped: true });
		await flushPromises();
	});
});

describe("Macros AdminTab, hold, release and delete", () => {
	const HOSTILE = `<img src=x onerror=alert(1)> & "co"`;
	const holdButton = (w, i = 0) => rowsOf(w)[i].find(".jv-macro-admin-hold");
	const releaseButton = (w, i = 0) => rowsOf(w)[i].find(".jv-macro-admin-release");
	const deleteButton = (w, i = 0) => rowsOf(w)[i].find(".jv-macro-admin-delete");
	const held = (name = "a", extra = {}) =>
		row(name, {
			enabled: 0,
			admin_hold: 1,
			admin_hold_reason: "Sends too many emails",
			...extra,
		});

	it("shows On hold, with its reason, and offers Release instead of Hold", async () => {
		const w = await mountWith([held(), row("b")]);
		const [a, b] = rowsOf(w);
		expect(a.find(".jv-macro-admin-held").text()).toBe("On hold");
		// The reason is printed in the row: a hover title cannot be read on a phone.
		const why = a.find(".jv-macro-admin-hold-reason");
		expect(why.text()).toBe("Held: Sends too many emails");
		// In the text only: a title as well would be read out twice.
		expect(why.attributes("title")).toBeUndefined();
		expect(b.find(".jv-macro-admin-hold-reason").exists()).toBe(false);
		expect(b.find(".jv-macro-admin-own").exists()).toBe(false);
		expect(holdButton(w).exists()).toBe(false);
		expect(releaseButton(w).exists()).toBe(true);
		expect(b.find(".jv-macro-admin-held").exists()).toBe(false);
		expect(holdButton(w, 1).exists()).toBe(true);
		expect(releaseButton(w, 1).exists()).toBe(false);
	});

	it("offers no Hold, Release or Hand over on the admin's own macro, only Delete", async () => {
		const w = await mountWith([
			row("mine", { owner: "admin@example.test" }),
			held("mine-held", { owner: "admin@example.test" }),
		]);
		for (const i of [0, 1]) {
			expect(holdButton(w, i).exists()).toBe(false);
			expect(releaseButton(w, i).exists()).toBe(false);
			expect(rowsOf(w)[i].find(".jv-macro-admin-handover").exists()).toBe(false);
			expect(deleteButton(w, i).exists()).toBe(true);
			// Marked and said, not just left out.
			const own = rowsOf(w)[i].find(".jv-macro-admin-own");
			expect(own.find(".badge").text()).toBe("Yours");
			for (const said of [own.attributes("title"), own.find(".sr-only").text()]) {
				expect(said).toBe("You cannot hold, release or hand over your own macro.");
			}
		}
	});

	it("filters on hold", async () => {
		const w = await mountWith([row("a")]);
		await control(w, "Hold").find("select").setValue("1");
		await flushPromises();
		expect(lastCall().filters).toEqual({ on_hold: "1" });
	});

	it("asks for a reason in the hold dialog, and re-reads the rows once it is held", async () => {
		const w = await mountWith([row("a")]);
		expect(w.find(".stub-hold").exists()).toBe(false);
		await holdButton(w).trigger("click");
		const dialog = w.find(".stub-hold");
		expect(dialog.attributes("data-name")).toBe("a");
		expect(dialog.attributes("data-open")).toBe("1");
		expect(dialog.attributes("data-owner")).toBe("Asha Rao (asha@example.test)");
		// Nothing is sent from the pane itself: the dialog holds, with the reason.
		expect(confirm).not.toHaveBeenCalled();

		api.adminListMacros.mockResolvedValue(page([held()]));
		const hold = w.findComponent({ name: "MacroHoldDialog" });
		hold.vm.$emit("update:modelValue", false);
		hold.vm.$emit("held", { name: "a", stopped_runs: 1 });
		await flushPromises();
		expect(lastCall()).toMatchObject({ start: 0, pageLength: 20 });
		expect(releaseButton(w).exists()).toBe(true);
	});

	it("re-reads the rows when the hold answers an error: the hold may still stand", async () => {
		const w = await mountWith([row("a")]);
		await holdButton(w).trigger("click");
		const calls = api.adminListMacros.mock.calls.length;
		api.adminListMacros.mockResolvedValue(page([held()]));
		w.findComponent({ name: "MacroHoldDialog" }).vm.$emit("failed", { name: "a" });
		await flushPromises();
		expect(api.adminListMacros.mock.calls.length).toBeGreaterThan(calls);
		expect(releaseButton(w).exists()).toBe(true);
	});

	it("takes the closed hold dialog away once its macro is on hold, so the next one starts empty", async () => {
		// The dialog keeps a typed reason for its macro. A hold can be made and still
		// answer an error: the reason has served then too.
		const w = await mountWith([row("a")]);
		await holdButton(w).trigger("click");
		const dialog = () => w.findComponent({ name: "MacroHoldDialog" });
		// Closed without holding: it stays, with what was typed.
		dialog().vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(dialog().exists()).toBe(true);
		// Held, with an error answered: still open, so still there.
		await holdButton(w).trigger("click");
		api.adminListMacros.mockResolvedValue(page([held()]));
		dialog().vm.$emit("failed", { name: "a" });
		await flushPromises();
		expect(dialog().exists()).toBe(true);
		// Closed now: gone.
		dialog().vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(dialog().exists()).toBe(false);
	});

	it("takes it away when a later re-read first shows the hold, and leaves focus to that re-read", async () => {
		// Held with an error answered, and the re-read failed as well: the rows still
		// show it not on hold when the dialog closes. The next Refresh shows the hold.
		const dialog = (w) => w.findComponent({ name: "MacroHoldDialog" });
		const w = await mountWith([row("z"), row("a")], undefined, { attachTo: document.body });
		await holdButton(w, 1).trigger("click");
		api.adminListMacros.mockRejectedValue(new Error("Busy."));
		dialog(w).vm.$emit("failed", { name: "a" });
		await flushPromises();
		dialog(w).vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(dialog(w).exists()).toBe(true);

		// Refresh by key: the draft goes, and focus is back on Refresh, not on the row.
		const refresh = w.find(".jv-macro-admin-refresh");
		let answer;
		api.adminListMacros.mockReturnValue(new Promise((r) => (answer = r)));
		refresh.element.focus();
		await refresh.trigger("keydown", { key: "Enter" });
		refresh.element.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 0 }));
		await flushPromises();
		document.activeElement.blur();
		answer(page([row("z"), held()]));
		await flushPromises();
		expect(dialog(w).exists()).toBe(false);
		expect(document.activeElement).toBe(refresh.element);
		w.unmount();
	});

	it("does not move focus when a pointer Refresh shows the hold", async () => {
		const dialog = (w) => w.findComponent({ name: "MacroHoldDialog" });
		const w = await mountWith([row("z"), row("a")], undefined, { attachTo: document.body });
		await holdButton(w, 1).trigger("click");
		dialog(w).vm.$emit("update:modelValue", false);
		await flushPromises();
		let answer;
		api.adminListMacros.mockReturnValue(new Promise((r) => (answer = r)));
		const refresh = w.find(".jv-macro-admin-refresh");
		refresh.element.focus();
		await refresh.trigger("pointerdown");
		refresh.element.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 }));
		await flushPromises();
		document.activeElement.blur();
		answer(page([row("z"), held()]));
		await flushPromises();
		expect(dialog(w).exists()).toBe(false);
		expect(document.activeElement).toBe(document.body);
		w.unmount();
	});

	it("keeps the draft while its macro is not on hold: listed, hidden by a filter, or not on this page", async () => {
		const dialog = (w) => w.findComponent({ name: "MacroHoldDialog" });
		const w = await mountWith([row("a"), row("b")]);
		await holdButton(w).trigger("click");
		dialog(w).vm.$emit("update:modelValue", false);
		await flushPromises();
		// Read again with the macro still listed and not held.
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(dialog(w).exists()).toBe(true);
		// A search that does not show it: not on screen is not gone.
		api.adminListMacros.mockResolvedValue(page([row("b")]));
		await control(w, "Hold").find("select").setValue("0");
		await flushPromises();
		expect(dialog(w).exists()).toBe(true);
		// A filter change that fails clears the rows: still not gone.
		api.adminListMacros.mockRejectedValue(new Error("Busy."));
		await control(w, "Hold").find("select").setValue("1");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(0);
		expect(dialog(w).exists()).toBe(true);
	});

	it("puts focus on the row when the dialog that is taken away had given it to a button that is gone", async () => {
		// The dialog hands focus back to the Actions button, and the row now shows Release.
		const w = await mountWith([row("a")], undefined, { attachTo: document.body });
		await holdButton(w).trigger("click");
		api.adminListMacros.mockResolvedValue(page([held()]));
		const dialog = w.findComponent({ name: "MacroHoldDialog" });
		dialog.vm.$emit("failed", { name: "a" });
		await flushPromises();
		document.activeElement.blur();
		dialog.vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(document.activeElement).toBe(rowsOf(w)[0].find(".jv-macro-admin-open").element);
		w.unmount();
	});

	it("puts focus on the list when a filter dropped the held macro from it", async () => {
		// Under "Not on hold" the hold removes the macro: its button is gone, and so is its row.
		const w = await mountWith([row("z"), row("a")], undefined, { attachTo: document.body });
		await holdButton(w, 1).trigger("click");
		api.adminListMacros.mockResolvedValue(page([row("z")]));
		const dialog = w.findComponent({ name: "MacroHoldDialog" });
		dialog.vm.$emit("failed", { name: "a" });
		await flushPromises();
		document.activeElement.blur();
		dialog.vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(document.activeElement).toBe(w.find(".jv-macro-admin-scroll").element);
		w.unmount();
	});

	it("does not decide the held macro is gone while the reload is still on its way", async () => {
		const w = await mountWith([row("z"), row("a")], undefined, { attachTo: document.body });
		await holdButton(w, 1).trigger("click");
		let answer;
		api.adminListMacros.mockReturnValue(new Promise((r) => (answer = r)));
		const dialog = w.findComponent({ name: "MacroHoldDialog" });
		dialog.vm.$emit("failed", { name: "a" });
		await flushPromises();
		// Closed while the re-read is still on its way: nothing is decided yet.
		const before = document.activeElement;
		dialog.vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(document.activeElement).toBe(before);
		answer(page([row("z"), row("a")]));
		await flushPromises();
		w.unmount();
	});

	it("moves focus nowhere on a plain Cancel, even under a filter that hides the macro", async () => {
		const w = await mountWith([row("z"), row("a")], undefined, { attachTo: document.body });
		await holdButton(w, 1).trigger("click");
		api.adminListMacros.mockResolvedValue(page([row("z")]));
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		document.activeElement.blur();
		w.findComponent({ name: "MacroHoldDialog" }).vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(document.activeElement).toBe(document.body);
		w.unmount();
	});

	it("puts focus on the search box when the filter left no list at all", async () => {
		const w = await mountWith([row("a")], undefined, { attachTo: document.body });
		await holdButton(w).trigger("click");
		api.adminListMacros.mockResolvedValue(page([]));
		const dialog = w.findComponent({ name: "MacroHoldDialog" });
		dialog.vm.$emit("failed", { name: "a" });
		await flushPromises();
		document.activeElement.blur();
		dialog.vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(document.activeElement).toBe(control(w, "Search").find("input").element);
		w.unmount();
	});

	it("leaves focus where it is when the dialog is taken away and the user is somewhere else", async () => {
		const close = async (w) => {
			await holdButton(w).trigger("click");
			api.adminListMacros.mockResolvedValue(page([held()]));
			const dialog = w.findComponent({ name: "MacroHoldDialog" });
			dialog.vm.$emit("failed", { name: "a" });
			await flushPromises();
			return dialog;
		};
		// Focus was put somewhere on purpose: it stays.
		let w = await mountWith([row("a")], undefined, { attachTo: document.body });
		let dialog = await close(w);
		const search = control(w, "Search").find("input");
		search.element.focus();
		dialog.vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(document.activeElement).toBe(search.element);
		w.unmount();

		// A confirmation is open over the tab: focus is its business.
		w = await mountWith([row("a")], undefined, { attachTo: document.body });
		dialog = await close(w);
		const question = document.createElement("div");
		question.setAttribute("role", "alertdialog");
		document.body.appendChild(question);
		document.activeElement.blur();
		dialog.vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(document.activeElement).toBe(document.body);
		question.remove();
		w.unmount();
	});

	it("releases after asking, says nothing turns back on, and re-reads the rows", async () => {
		const w = await mountWith([held()]);
		api.adminListMacros.mockResolvedValue(page([row("a", { enabled: 0 })]));
		await releaseButton(w).trigger("click");
		await flushPromises();
		const asked = confirm.mock.calls[0][0];
		expect(asked.title).toBe("Release the hold?");
		expect(asked.message).toContain("“Macro a”, owned by Asha Rao (asha@example.test)");
		expect(asked.message).toContain("Nothing turns itself back on");
		expect(api.adminRelease).toHaveBeenCalledWith("a");
		expect(toast.success).toHaveBeenCalledWith("Released the hold on “Macro a”");
		expect(holdButton(w).exists()).toBe(true);
	});

	it("does not release or delete when the admin cancels", async () => {
		confirm.mockResolvedValue(false);
		const w = await mountWith([held()]);
		const calls = api.adminListMacros.mock.calls.length;
		await releaseButton(w).trigger("click");
		await deleteButton(w).trigger("click");
		await flushPromises();
		expect(api.adminRelease).not.toHaveBeenCalled();
		expect(api.adminDelete).not.toHaveBeenCalled();
		expect(api.adminListMacros.mock.calls.length).toBe(calls);
	});

	it("says a macro that was not on hold was not, as information", async () => {
		api.adminRelease.mockResolvedValue({ ok: true, released: false });
		const w = await mountWith([held()]);
		await releaseButton(w).trigger("click");
		await flushPromises();
		expect(toast.success).not.toHaveBeenCalled();
		expect(toast.create).toHaveBeenCalledWith({
			message: "This macro was not on hold.",
			type: "info",
		});
	});

	it("deletes after a danger confirmation that says the run is stopped first", async () => {
		const w = await mountWith([row("a", { live_run: "RUN-1" }), row("b")]);
		api.adminDelete.mockResolvedValue({ ok: true, deleted: true, stopped_runs: 1 });
		api.adminListMacros.mockResolvedValue(page([row("b")]));
		await deleteButton(w).trigger("click");
		await flushPromises();
		const asked = confirm.mock.calls[0][0];
		expect(asked.danger).toBe(true);
		expect(asked.confirmLabel).toBe("Delete");
		expect(asked.message).toContain("Its run is stopped first.");
		expect(asked.message).toContain("The owner is told an admin deleted it.");
		expect(api.adminDelete).toHaveBeenCalledWith("a");
		expect(toast.success).toHaveBeenCalledWith("Deleted “Macro a”; 1 run stopped first");
		expect(rowsOf(w).map((r) => r.attributes("data-macro"))).toEqual(["b"]);
	});

	it("shows why an action failed, escaped, and re-reads the rows", async () => {
		api.adminDelete.mockRejectedValue(new Error("This macro was deleted. &lt;b&gt;"));
		const w = await mountWith([row("a")]);
		api.adminListMacros.mockResolvedValue(page([]));
		await deleteButton(w).trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith("This macro was deleted. &lt;b&gt;");
		expect(toast.success).not.toHaveBeenCalled();
		expect(rowsOf(w)).toHaveLength(0);
	});

	it("shows the action in flight and disables every other one meanwhile", async () => {
		let answer;
		api.adminRelease.mockReturnValue(new Promise((r) => (answer = r)));
		const w = await mountWith([held(), row("b", { live_run: "RUN-2" })]);
		await releaseButton(w).trigger("click");
		await flushPromises();
		// The row's Actions button shows it; every menu and every item is off meanwhile.
		const actions = (i) => rowsOf(w)[i].find(".jv-macro-admin-actions");
		expect(actions(0).attributes("data-loading")).toBe("1");
		expect(actions(1).attributes("data-loading")).toBe("");
		for (const b of [
			actions(0),
			actions(1),
			deleteButton(w),
			holdButton(w, 1),
			deleteButton(w, 1),
		]) {
			expect(b.attributes("disabled")).toBe("");
		}
		expect(rowsOf(w)[1].find(".jv-macro-admin-stop").attributes("disabled")).toBe("");
		await deleteButton(w, 1).trigger("click");
		expect(confirm).toHaveBeenCalledTimes(1);
		answer({ ok: true, released: true });
		await flushPromises();
		expect(deleteButton(w).attributes("disabled")).toBeUndefined();
	});

	it("shows a delete in flight on its row's Actions button too", async () => {
		let answer;
		api.adminDelete.mockReturnValue(new Promise((r) => (answer = r)));
		const w = await mountWith([row("a"), row("b")]);
		await deleteButton(w).trigger("click");
		await flushPromises();
		const actions = (i) => rowsOf(w)[i].find(".jv-macro-admin-actions");
		expect(actions(0).attributes("data-loading")).toBe("1");
		expect(actions(1).attributes("data-loading")).toBe("");
		expect(actions(1).attributes("disabled")).toBe("");
		answer({ ok: true, deleted: true, stopped_runs: 0 });
		await flushPromises();
	});

	it("puts a name in the confirmation as text and in the toast escaped", async () => {
		const w = await mountWith([held("a", { macro_name: HOSTILE })]);
		await releaseButton(w).trigger("click");
		await flushPromises();
		expect(confirm.mock.calls[0][0].message).toContain(`“${HOSTILE}”`);
		const said = toast.success.mock.calls[0][0];
		expect(said).not.toContain("<img");
		expect(w.find("img").exists()).toBe(false);
	});
});

describe("Macros AdminTab, hand over", () => {
	const handoverButton = (w, i = 0) => rowsOf(w)[i].find(".jv-macro-admin-handover");

	it("offers Hand over on another user's macro, held or not", async () => {
		const w = await mountWith([row("a"), row("b", { admin_hold: 1, enabled: 0 })]);
		for (const i of [0, 1]) {
			const b = handoverButton(w, i);
			expect(b.exists()).toBe(true);
			expect(b.text()).toBe("Hand over");
		}
	});

	it("opens the hand-over dialog for the row, and sends nothing itself", async () => {
		const w = await mountWith([row("a")]);
		expect(w.find(".stub-handover").exists()).toBe(false);
		await handoverButton(w).trigger("click");
		const dialog = w.find(".stub-handover");
		expect(dialog.attributes("data-name")).toBe("a");
		expect(dialog.attributes("data-open")).toBe("1");
		expect(dialog.attributes("data-macro")).toBe("Macro a");
		expect(dialog.attributes("data-owner")).toBe("asha@example.test");
		expect(dialog.attributes("data-owner-label")).toBe("Asha Rao (asha@example.test)");
		expect(confirm).not.toHaveBeenCalled();
	});

	it("disables the other actions while the dialog is open", async () => {
		const w = await mountWith([row("a"), row("b", { live_run: "RUN-2" })]);
		await handoverButton(w).trigger("click");
		for (const b of [
			handoverButton(w, 1),
			rowsOf(w)[1].find(".jv-macro-admin-delete"),
			rowsOf(w)[1].find(".jv-macro-admin-hold"),
			rowsOf(w)[1].find(".jv-macro-admin-stop"),
		]) {
			expect(b.attributes("disabled")).toBe("");
		}
	});

	it("re-reads the rows once it is handed over, and when it answers an error", async () => {
		for (const event of ["handed", "failed"]) {
			const w = await mountWith([row("a")]);
			await handoverButton(w).trigger("click");
			const calls = api.adminListMacros.mock.calls.length;
			api.adminListMacros.mockResolvedValue(
				page([row("a", { owner: "ben@example.test", owner_full_name: "Ben", enabled: 0 })])
			);
			const dialog = w.findComponent({ name: "MacroHandoverDialog" });
			dialog.vm.$emit("update:modelValue", false);
			dialog.vm.$emit(event, { name: "a" });
			await flushPromises();
			expect(api.adminListMacros.mock.calls.length).toBeGreaterThan(calls);
			expect(rowsOf(w)[0].text()).toContain("Ben");
			w.unmount();
		}
	});

	it("after a refusal, shows the open dialog the owner the re-read found", async () => {
		const w = await mountWith([row("a")]);
		await handoverButton(w).trigger("click");
		api.adminListMacros.mockResolvedValue(
			page([row("a", { owner: "ben@example.test", owner_full_name: "Ben" })])
		);
		const dialog = w.findComponent({ name: "MacroHandoverDialog" });
		dialog.vm.$emit("failed", { name: "a", message: "This macro changed hands." });
		await flushPromises();
		const stub = w.find(".stub-handover");
		expect(stub.attributes("data-open")).toBe("1");
		expect(stub.attributes("data-owner")).toBe("ben@example.test");
		expect(stub.attributes("data-owner-label")).toBe("Ben (ben@example.test)");
		expect(toast.error).not.toHaveBeenCalled();
	});

	it("after a refusal, closes the dialog with its message when the macro is no longer listed", async () => {
		const w = await mountWith([row("a"), row("b")]);
		await handoverButton(w).trigger("click");
		api.adminListMacros.mockResolvedValue(page([row("b")]));
		const dialog = w.findComponent({ name: "MacroHandoverDialog" });
		dialog.vm.$emit("failed", { name: "a", message: "<b>Changed</b> hands." });
		await flushPromises();
		expect(w.find(".stub-handover").attributes("data-open")).toBe("");
		expect(toast.error).toHaveBeenCalledWith("&lt;b&gt;Changed&lt;/b&gt; hands.");
	});

	it("leaves the open dialog alone when another macro's action fails", async () => {
		const w = await mountWith([row("a"), row("b")]);
		await handoverButton(w).trigger("click");
		api.adminListMacros.mockResolvedValue(page([row("a")]));
		const dialog = w.findComponent({ name: "MacroHandoverDialog" });
		dialog.vm.$emit("failed", { name: "b", message: "Gone." });
		await flushPromises();
		const stub = w.find(".stub-handover");
		expect(stub.attributes("data-open")).toBe("1");
		expect(stub.attributes("data-name")).toBe("a");
		expect(stub.attributes("data-owner")).toBe("asha@example.test");
		expect(toast.error).not.toHaveBeenCalled();
	});
});
