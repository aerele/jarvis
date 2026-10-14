import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The Jarvis Admin's tab on the Skills page (#admin): every user's skills, the
 * filters, the loading / empty / error states, and the row actions Hold, Release
 * and Delete. The dialogs are stubs here (their own specs read them); the load
 * state machine and its focus rules are the Macros tab's, shared through
 * useAdminList and covered in full by that tab's spec, so only what a Skills row
 * adds is checked for focus.
 */

vi.mock("frappe-ui", () => ({
	// As the real Button does: it shows its slot (or its label), is NAMED by its label
	// alone, and that name replaces any aria-label attribute it was given.
	Button: {
		name: "Button",
		inheritAttrs: false,
		props: ["label", "variant", "icon", "iconLeft", "loading", "disabled", "size", "theme"],
		template: `<button v-bind="$attrs" class="stub-button" :disabled="disabled" :data-loading="loading ? '1' : ''" :data-variant="variant" :aria-label="label"><slot>{{ label }}</slot></button>`,
	},
	// The menu drawn open: its button (the slot), then one button per item, classed by
	// the item's key.
	Dropdown: {
		name: "Dropdown",
		props: ["options", "placement"],
		emits: ["update:open"],
		template: `<span class="stub-dropdown"><slot /><span class="stub-menu"><template v-for="g in options" :key="g.group"><button v-for="o in g.items" :key="o.key" class="stub-menu-item" :class="'jv-skill-admin-' + o.key" :data-group="g.group" :data-theme="o.theme || ''" :disabled="o.disabled" @click="o.onClick($event)">{{ o.label }}</button></template></span></span>`,
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
		template: `<nav class="stub-crumbs">{{ items.map((i) => i.label).join(" / ") }}</nav>`,
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
}));

const api = vi.hoisted(() => ({
	adminListSkills: vi.fn(),
	adminSkillOwners: vi.fn(),
	adminGetSkill: vi.fn(),
	adminDelete: vi.fn(),
}));
vi.mock("@/api/skillsAdmin", () => api);

// The apply call the Skills list's sync pill makes.
const shared = vi.hoisted(() => ({ applyCustomSkills: vi.fn(), getCustomSkillsSyncStatus: vi.fn() }));
vi.mock("@/api", () => shared);

// The page header teleports in the app; here it renders in place.
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: { name: "LayoutHeader", template: `<div><slot name="left-header" /></div>` },
}));

const confirm = vi.hoisted(() => vi.fn());
vi.mock("@/composables/useConfirm", () => ({ useConfirm: () => ({ confirm }) }));

vi.mock("./SkillAdminDialog.vue", () => ({
	default: {
		name: "SkillAdminDialog",
		props: ["modelValue", "name"],
		emits: ["update:modelValue", "gone"],
		template: `<div class="stub-detail" :data-name="name" :data-open="modelValue ? '1' : ''" />`,
	},
}));
vi.mock("./SkillHoldDialog.vue", () => ({
	default: {
		name: "SkillHoldDialog",
		props: ["modelValue", "name", "skillName", "ownerLabel", "company"],
		emits: ["update:modelValue", "held", "failed"],
		template: `<div class="stub-hold" :data-name="name" :data-open="modelValue ? '1' : ''" :data-company="company ? '1' : ''" :data-owner="ownerLabel" />`,
	},
}));
vi.mock("./SkillReleaseDialog.vue", () => ({
	default: {
		name: "SkillReleaseDialog",
		props: ["modelValue", "name", "skillName", "ownerLabel", "shared"],
		emits: ["update:modelValue", "released", "failed"],
		template: `<div class="stub-release" :data-name="name" :data-open="modelValue ? '1' : ''" :data-shared="shared ? '1' : ''" />`,
	},
}));

import { toast } from "frappe-ui";
import AdminTab from "./AdminTab.vue";

const row = (name, extra = {}) => ({
	name,
	skill_name: `skill-${name}`,
	description: "",
	owner: "asha@example.test",
	owner_name: "Asha Rao",
	scope: "User",
	target_role: "",
	enabled: 1,
	allow_approve_run: 0,
	admin_hold: 0,
	admin_hold_reason: "",
	shared_with_count: 0,
	modified: "2026-10-01 10:00:00",
	is_mine: 0,
	is_published: 0,
	is_learned: 0,
	...extra,
});
const PAGE_SIZE = 20;
const page = (rows, extra = {}) => ({
	rows,
	page: 1,
	page_size: PAGE_SIZE,
	has_more: false,
	hold_available: true,
	sync: { pending: false, needs_apply: false, last_synced_at: null },
	...extra,
});
const OWNERS = [
	{ user: "asha@example.test", full_name: "Asha Rao" },
	{ user: "ben@example.test", full_name: "ben@example.test" },
];
// `n` rows named r0, r1, ...: a list long enough to page.
const many = (n, from = 0) => Array.from({ length: n }, (_, i) => row(`r${from + i}`));
// The server's paging over `all`: what a real list call answers for a page number.
const serve = (all) => (q) =>
	Promise.resolve(
		page(all.slice((q.page - 1) * PAGE_SIZE, q.page * PAGE_SIZE), {
			page: q.page,
			has_more: q.page * PAGE_SIZE < all.length,
		})
	);

async function mountWith(rows, extra, options) {
	api.adminListSkills.mockResolvedValue(page(rows, extra));
	const w = mount(AdminTab, options);
	await flushPromises();
	return w;
}

// By its words, or by its name when it is an icon alone (Refresh).
const button = (w, label) =>
	w
		.findAll("button.stub-button")
		.find((b) => b.text() === label || b.attributes("aria-label") === label);
const rowsOf = (w) => w.findAll(".jv-skill-admin-row");
const control = (w, label) =>
	w.findAll(".stub-control").find((c) => c.find(".stub-label").text() === label);
const lastCall = () => api.adminListSkills.mock.calls.at(-1)[0];
const menuItem = (w, name, key) => rowsOf(w).find((r) => r.attributes("data-skill") === name).find(`.jv-skill-admin-${key}`);

beforeEach(() => {
	vi.clearAllMocks();
	confirm.mockResolvedValue(true);
	api.adminListSkills.mockReset();
	api.adminSkillOwners.mockReset();
	api.adminSkillOwners.mockResolvedValue({ owners: OWNERS, more: false });
	api.adminGetSkill.mockResolvedValue({ used_by_macros: { total: 0, armed: 0 } });
	api.adminDelete.mockResolvedValue({ ok: true, needs_apply: false, used_by_macros: { total: 0, armed: 0 } });
	shared.applyCustomSkills.mockResolvedValue({});
	shared.getCustomSkillsSyncStatus.mockResolvedValue({ pending: false });
});
afterEach(() => {
	vi.useRealTimers();
});

describe("Skills AdminTab, rows", () => {
	it("names the tab in the app header, as the other tabs of the page do", async () => {
		const w = await mountWith([]);
		expect(w.find(".stub-crumbs").text()).toBe("Skills / Admin");
	});

	it("shows each skill with its state, audience and flags in words", async () => {
		const w = await mountWith([
			row("a", { enabled: 0 }),
			row("b", { scope: "Role", target_role: "Accounts User", is_published: 1 }),
			row("c", {
				scope: "Org",
				allow_approve_run: 1,
				admin_hold: 1,
				admin_hold_reason: "Emails customers",
			}),
			row("d", { is_mine: 1 }),
		]);
		const badges = (name) =>
			rowsOf(w)
				.find((r) => r.attributes("data-skill") === name)
				.findAll(".badge")
				.map((b) => b.text());
		expect(badges("a")).toEqual(["Off", "Private"]);
		expect(badges("b")).toEqual(["On", "Role: Accounts User", "Published"]);
		expect(badges("c")).toEqual(["On", "Everyone", "Writes without asking", "On hold"]);
		expect(badges("d")).toEqual(["On", "Private", "Yours"]);
		// The reason is printed in the row, not only on hover.
		expect(rowsOf(w)[2].find(".jv-skill-admin-hold-reason").text()).toBe(
			"Held: Emails customers"
		);
		expect(rowsOf(w)[0].find(".jv-skill-admin-hold-reason").exists()).toBe(false);
	});

	it("says who it is shared with in the Audience lane", async () => {
		const w = await mountWith([
			row("a"),
			row("b", { shared_with_count: 1 }),
			row("c", { shared_with_count: 3 }),
			row("d", { scope: "Role", target_role: "Accounts User" }),
			row("e", { scope: "Org" }),
		]);
		const lane = (i) => rowsOf(w)[i].find(".jv-skill-admin-audience-text").text();
		expect(lane(0)).toBe("Not shared");
		expect(lane(1)).toBe("Shared with 1 person");
		expect(lane(2)).toBe("Shared with 3 people");
		expect(lane(3)).toBe("Everyone with the role Accounts User");
		expect(lane(4)).toBe("Everyone on this site");
	});

	it("shows the owner by name with the address under it, and the change time", async () => {
		const w = await mountWith([row("a"), row("b", { owner: "ben@example.test", owner_name: "ben@example.test" })]);
		const owner = (i) => rowsOf(w)[i].find(".jv-skill-admin-cell-owner").text();
		expect(owner(0)).toBe("Asha Raoasha@example.test");
		expect(owner(1)).toBe("ben@example.test");
		expect(rowsOf(w)[0].find(".jv-skill-admin-cell-changed").text()).toBe("a while ago");
	});

	it("never uses the server's words (Org, scope, armed) on screen", async () => {
		const w = await mountWith(
			[
				row("a", { scope: "Org", allow_approve_run: 1 }),
				row("b", { scope: "Role", target_role: "Accounts User" }),
				row("c", { admin_hold: 1, admin_hold_reason: "Paused" }),
			],
			{ sync: { pending: false, needs_apply: true } }
		);
		await button(w, "Filters").trigger("click");
		expect(w.text()).not.toMatch(/\bOrg\b|scope|armed/i);
		for (const m of w.findAll(".stub-menu-item")) expect(m.text()).not.toMatch(/\bOrg\b|scope|armed/i);
	});

	it("gives an admin's own skill Delete only, and says why", async () => {
		const w = await mountWith([row("a", { is_mine: 1 }), row("b", { is_mine: 1, admin_hold: 1 })]);
		for (const i of [0, 1]) {
			const items = rowsOf(w)[i].findAll(".stub-menu-item").map((m) => m.text());
			expect(items).toEqual(["Delete"]);
		}
		expect(rowsOf(w)[0].find(".jv-skill-admin-own").text()).toContain("Yours");
		expect(rowsOf(w)[0].find(".jv-skill-admin-own .sr-only").text()).toBe(
			"You cannot hold or release your own skill."
		);
	});

	it("opens a row read-only", async () => {
		const w = await mountWith([row("a")]);
		expect(w.find(".stub-detail").exists()).toBe(false);
		await w.find(".jv-skill-admin-open").trigger("click");
		expect(w.find(".stub-detail").attributes("data-name")).toBe("a");
		expect(w.find(".stub-detail").attributes("data-open")).toBe("1");
	});

	it("names each Actions button for its row, starting with its visible word", async () => {
		const w = await mountWith([row("a"), row("b", { owner: "ben@example.test", owner_name: "Ben" })]);
		const names = [...w.findAll(".jv-skill-admin-actions")].map((b) => b.attributes("aria-label"));
		expect(names).toEqual([
			"Actions for skill-a, owned by Asha Rao (asha@example.test)",
			"Actions for skill-b, owned by Ben (ben@example.test)",
		]);
		for (const b of w.findAll(".jv-skill-admin-actions"))
			expect(b.attributes("aria-label").startsWith(b.text())).toBe(true);
	});
});

describe("Skills AdminTab, filters", () => {
	it("asks for the first page with nothing filtered, and lists the owners", async () => {
		const w = await mountWith([row("a")]);
		expect(lastCall()).toEqual({ search: "", filters: {}, page: 1, pageSize: 20 });
		const options = control(w, "Owner").findAll("option").map((o) => o.text());
		expect(options).toEqual(["All owners", "Asha Rao", "ben@example.test"]);
	});

	it("searches after a pause, not on every key", async () => {
		const w = await mountWith([row("a")]);
		vi.useFakeTimers();
		const input = control(w, "Search").find("input");
		await input.setValue("bil");
		await input.setValue("bills");
		vi.advanceTimersByTime(299);
		expect(api.adminListSkills).toHaveBeenCalledTimes(1);
		vi.advanceTimersByTime(1);
		await flushPromises();
		expect(api.adminListSkills).toHaveBeenCalledTimes(2);
		expect(lastCall().search).toBe("bills");
	});

	it("filters by owner", async () => {
		const w = await mountWith([row("a")]);
		await control(w, "Owner").find("select").setValue("ben@example.test");
		await flushPromises();
		expect(lastCall().filters).toEqual({ owner: "ben@example.test" });
	});

	it("keeps Audience, State and Hold in a disclosure, closed until opened", async () => {
		const w = await mountWith([row("a")]);
		for (const name of ["Audience", "State", "Hold"]) expect(control(w, name)).toBeUndefined();
		const toggle = button(w, "Filters");
		expect(toggle.attributes("aria-expanded")).toBe("false");
		await toggle.trigger("click");
		expect(toggle.attributes("aria-expanded")).toBe("true");
		for (const name of ["Audience", "State", "Hold"]) expect(control(w, name)).toBeDefined();
		expect(button(w, "Filters")).toBeDefined();
	});

	it("sends audience as the server's value and the flags as numbers", async () => {
		const w = await mountWith([row("a")]);
		await button(w, "Filters").trigger("click");
		await control(w, "Audience").find("select").setValue("Role");
		await flushPromises();
		expect(lastCall().filters).toEqual({ audience: "Role" });
		await control(w, "State").find("select").setValue("0");
		await flushPromises();
		await control(w, "Hold").find("select").setValue("1");
		await flushPromises();
		expect(lastCall().filters).toEqual({ audience: "Role", enabled: 0, on_hold: 1 });
		// The disclosure's own button counts what is filtered inside it.
		expect(button(w, "Filters (3)")).toBeDefined();
	});

	it("labels the audiences in plain words", async () => {
		const w = await mountWith([row("a")]);
		await button(w, "Filters").trigger("click");
		expect(control(w, "Audience").findAll("option").map((o) => o.text())).toEqual([
			"All audiences",
			"Private",
			"Shared with a role",
			"Everyone",
		]);
	});

	it("the preset sets the company audience and writes-without-asking in one step, and again undoes both", async () => {
		const w = await mountWith([row("a")]);
		const preset = () => button(w, "Company-wide and writes without asking");
		expect(preset().attributes("aria-pressed")).toBe("false");
		await preset().trigger("click");
		await flushPromises();
		expect(lastCall().filters).toEqual({ audience: "Org", armed: 1 });
		expect(preset().attributes("aria-pressed")).toBe("true");
		// What it filters stays in sight: the disclosure opens on the Audience it set.
		expect(control(w, "Audience").find("select").element.value).toBe("Org");
		await preset().trigger("click");
		await flushPromises();
		expect(lastCall().filters).toEqual({});
		expect(preset().attributes("aria-pressed")).toBe("false");
	});

	it("drops writes-without-asking when the audience is changed away from the company", async () => {
		const w = await mountWith([row("a")]);
		await button(w, "Company-wide and writes without asking").trigger("click");
		await flushPromises();
		await control(w, "Audience").find("select").setValue("Role");
		await flushPromises();
		expect(lastCall().filters).toEqual({ audience: "Role" });
	});

	it("Clear puts everything back", async () => {
		const w = await mountWith([row("a")]);
		await control(w, "Owner").find("select").setValue("ben@example.test");
		await flushPromises();
		await button(w, "Clear").trigger("click");
		await flushPromises();
		expect(lastCall()).toEqual({ search: "", filters: {}, page: 1, pageSize: 20 });
		expect(button(w, "Clear")).toBeUndefined();
	});
});

describe("Skills AdminTab, states", () => {
	it("says Loading until the first answer, then lists the skills", async () => {
		let answer;
		api.adminListSkills.mockReturnValue(new Promise((r) => (answer = r)));
		const w = mount(AdminTab);
		await flushPromises();
		expect(w.text()).toContain("Loading…");
		expect(rowsOf(w)).toHaveLength(0);
		answer(page([row("a")]));
		await flushPromises();
		expect(w.text()).not.toContain("Loading…");
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("says so when nobody has a skill, with nothing to clear", async () => {
		const w = await mountWith([]);
		expect(w.text()).toContain("Nobody has made a skill yet.");
		expect(button(w, "Clear filters")).toBeUndefined();
	});

	it("says no skills MATCH when a filter is on, and Clear filters shows them all again", async () => {
		const w = await mountWith([row("a")]);
		api.adminListSkills.mockResolvedValue(page([]));
		await control(w, "Owner").find("select").setValue("ben@example.test");
		await flushPromises();
		expect(w.text()).toContain("No skills match these filters.");
		api.adminListSkills.mockResolvedValue(page([row("a")]));
		await button(w, "Clear filters").trigger("click");
		await flushPromises();
		expect(lastCall().filters).toEqual({});
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("shows the error with Try again when the first load fails, and recovers", async () => {
		api.adminListSkills.mockRejectedValue(new Error("You need the Jarvis Admin role"));
		const w = mount(AdminTab);
		await flushPromises();
		expect(w.find("[role='alert']").text()).toContain("You need the Jarvis Admin role");
		expect(w.text()).not.toContain("Nobody has made a skill yet.");
		api.adminListSkills.mockResolvedValue(page([row("a")]));
		await button(w, "Try again").trigger("click");
		await flushPromises();
		expect(w.find("[role='alert']").exists()).toBe(false);
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("keeps the rows it had when a refresh fails, and says they are the earlier ones", async () => {
		const w = await mountWith([row("a"), row("b")]);
		api.adminListSkills.mockRejectedValue(new Error("Server is busy"));
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(2);
		expect(w.find("[role='alert']").text()).toContain("Server is busy");
		expect(w.find("[role='alert']").text()).toContain("Showing what was loaded before.");
	});

	it("pages by number: Load more asks for the next page and appends", async () => {
		api.adminListSkills.mockImplementation(serve(many(45)));
		const w = mount(AdminTab);
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(20);
		expect(w.text()).toContain("Showing 20 so far");
		// Each request says how big a page it wants.
		expect(lastCall().pageSize).toBe(20);
		await w.find(".jv-skill-admin-more").trigger("click");
		await flushPromises();
		expect(lastCall().page).toBe(2);
		expect(rowsOf(w)).toHaveLength(40);
		await w.find(".jv-skill-admin-more").trigger("click");
		await flushPromises();
		expect(lastCall().page).toBe(3);
		expect(rowsOf(w)).toHaveLength(45);
		expect(w.find(".jv-skill-admin-more").exists()).toBe(false);
		expect(w.text()).toContain("Showing 45");
		expect(w.text()).not.toContain("so far");
	});

	it("says how many there are in all when the server counts them", async () => {
		const all = many(45);
		api.adminListSkills.mockImplementation((q) =>
			serve(all)(q).then((p) => ({ ...p, total: all.length }))
		);
		const w = mount(AdminTab);
		await flushPromises();
		expect(w.text()).toContain("Showing 20 of 45");
		await w.find(".jv-skill-admin-more").trigger("click");
		await w.find(".jv-skill-admin-more").trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Showing 45");
		expect(w.text()).not.toContain("of 45");
	});

	it("says a failed next page beside Load more, and keeps the rows", async () => {
		api.adminListSkills.mockImplementation(serve(many(45)));
		const w = mount(AdminTab);
		await flushPromises();
		api.adminListSkills.mockRejectedValue(new Error("Timed out"));
		await w.find(".jv-skill-admin-more").trigger("click");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(20);
		expect(w.find(".jv-skill-admin-more-error").text()).toBe("Timed out");
		expect(w.find(".jv-skill-admin-more").text()).toBe("Try again");
		api.adminListSkills.mockImplementation(serve(many(45)));
		await w.find(".jv-skill-admin-more").trigger("click");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(40);
		expect(w.find(".jv-skill-admin-more-error").exists()).toBe(false);
	});

	it("re-reads every page on screen on Refresh", async () => {
		api.adminListSkills.mockImplementation(serve(many(45)));
		const w = mount(AdminTab);
		await flushPromises();
		await w.find(".jv-skill-admin-more").trigger("click");
		await flushPromises();
		api.adminListSkills.mockClear();
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(api.adminListSkills.mock.calls.map((c) => c[0].page)).toEqual([1, 2]);
		expect(rowsOf(w)).toHaveLength(40);
	});

	it("re-reads in whole pages when the rows on screen are not a multiple of the page size", async () => {
		const all = many(43);
		api.adminListSkills.mockImplementation(serve(all));
		const w = mount(AdminTab);
		await flushPromises();
		await w.find(".jv-skill-admin-more").trigger("click");
		await flushPromises();
		await w.find(".jv-skill-admin-more").trigger("click");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(43);
		api.adminListSkills.mockClear();
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		const asked = api.adminListSkills.mock.calls.map((c) => c[0]);
		expect(asked.map((q) => q.page)).toEqual([1, 2, 3]);
		expect(asked.every((q) => q.pageSize === 20)).toBe(true);
		expect(rowsOf(w)).toHaveLength(43);
		expect(w.find(".jv-skill-admin-more").exists()).toBe(false);
	});

	it("dims the old rows while a new filter's answer is on its way", async () => {
		const w = await mountWith([row("a")]);
		let answer;
		api.adminListSkills.mockReturnValue(new Promise((r) => (answer = r)));
		await control(w, "Owner").find("select").setValue("ben@example.test");
		await flushPromises();
		const list = () => w.find(".jv-skill-admin-scroll");
		expect(list().attributes("aria-busy")).toBe("true");
		expect(list().classes()).toContain("opacity-50");
		answer(page([row("b")]));
		await flushPromises();
		expect(list().attributes("aria-busy")).toBe("false");
		expect(list().classes()).not.toContain("opacity-50");
	});

	it("drops an answer that a newer filter has overtaken", async () => {
		const w = await mountWith([row("a")]);
		let slow;
		api.adminListSkills.mockReturnValueOnce(new Promise((r) => (slow = r)));
		await control(w, "Owner").find("select").setValue("ben@example.test");
		api.adminListSkills.mockResolvedValue(page([row("new")]));
		await control(w, "Owner").find("select").setValue("asha@example.test");
		await flushPromises();
		slow(page([row("stale")]));
		await flushPromises();
		expect(rowsOf(w).map((r) => r.attributes("data-skill"))).toEqual(["new"]);
	});
});

describe("Skills AdminTab, a site not yet migrated", () => {
	const mountUnmigrated = () =>
		mountWith([row("a"), row("b", { admin_hold: 1 }), row("c", { scope: "Org" })], {
			hold_available: false,
		});

	it("says holding is not available and disables Hold and Release", async () => {
		const w = await mountUnmigrated();
		expect(w.find(".jv-skill-admin-unmigrated").text()).toBe(
			"Holding is not available yet on this site."
		);
		expect(menuItem(w, "a", "hold").attributes("disabled")).toBeDefined();
		expect(menuItem(w, "b", "release").attributes("disabled")).toBeDefined();
		expect(menuItem(w, "c", "hold").attributes("disabled")).toBeDefined();
		// Delete still works.
		expect(menuItem(w, "a", "delete").attributes("disabled")).toBeUndefined();
	});

	it("does not open the hold dialog from a disabled item", async () => {
		const w = await mountUnmigrated();
		await menuItem(w, "a", "hold").trigger("click");
		await flushPromises();
		expect(w.find(".stub-hold").exists()).toBe(false);
	});

	it("says nothing about it on a migrated site", async () => {
		const w = await mountWith([row("a")]);
		expect(w.find(".jv-skill-admin-unmigrated").exists()).toBe(false);
		expect(menuItem(w, "a", "hold").attributes("disabled")).toBeUndefined();
	});
});

describe("Skills AdminTab, the Actions menu", () => {
	it("offers Hold on a skill that is not held, Release on one that is, and Delete apart", async () => {
		const w = await mountWith([row("a"), row("b", { admin_hold: 1 })]);
		const items = (i) =>
			rowsOf(w)[i]
				.findAll(".stub-menu-item")
				.map((m) => [m.text(), m.attributes("data-group")]);
		expect(items(0)).toEqual([["Hold", "Skill"], ["Delete", "Delete"]]);
		expect(items(1)).toEqual([["Release", "Skill"], ["Delete", "Delete"]]);
		expect(menuItem(w, "a", "delete").attributes("data-theme")).toBe("red");
	});

	it("says for everyone on a skill shared with everyone, and only there", async () => {
		const w = await mountWith([
			row("a", { scope: "Org" }),
			row("b", { scope: "Org", admin_hold: 1 }),
			row("c", { scope: "Role", target_role: "Accounts User" }),
		]);
		const items = (i) => rowsOf(w)[i].findAll(".stub-menu-item").map((m) => m.text());
		expect(items(0)).toEqual(["Hold for everyone", "Delete for everyone"]);
		expect(items(1)).toEqual(["Release", "Delete for everyone"]);
		expect(items(2)).toEqual(["Hold", "Delete"]);
	});

	it("leaves a clearly marked place for Hand over, and has none yet", async () => {
		const w = await mountWith([row("a")]);
		expect(w.text()).not.toContain("Hand over");
	});

	it("does not let a learned skill be deleted here", async () => {
		const w = await mountWith([row("a", { is_learned: 1 })]);
		expect(rowsOf(w)[0].findAll(".stub-menu-item").map((m) => m.text())).toEqual(["Delete"]);
		expect(menuItem(w, "a", "delete").attributes("disabled")).toBeDefined();
	});

	it("opens the hold dialog with the skill, its owner and whether it is for everyone", async () => {
		const w = await mountWith([row("a"), row("b", { scope: "Org" })]);
		await menuItem(w, "b", "hold").trigger("click");
		await flushPromises();
		const dialog = w.find(".stub-hold");
		expect(dialog.attributes("data-name")).toBe("b");
		expect(dialog.attributes("data-open")).toBe("1");
		expect(dialog.attributes("data-company")).toBe("1");
		expect(dialog.attributes("data-owner")).toBe("Asha Rao (asha@example.test)");
	});

	it("opens the release dialog, telling it whether the skill is shared", async () => {
		const w = await mountWith([
			row("a", { admin_hold: 1 }),
			row("b", { admin_hold: 1, scope: "Role", target_role: "Accounts User" }),
			row("c", { admin_hold: 1, scope: "Org" }),
		]);
		const shared = async (name) => {
			await menuItem(w, name, "release").trigger("click");
			await flushPromises();
			const flag = w.find(".stub-release").attributes("data-shared");
			w.findComponent({ name: "SkillReleaseDialog" }).vm.$emit("update:modelValue", false);
			await flushPromises();
			return flag;
		};
		expect(await shared("a")).toBe("");
		expect(await shared("b")).toBe("1");
		expect(await shared("c")).toBe("1");
	});

	it("re-reads the list after a hold, and says a company skill needs an Apply", async () => {
		const w = await mountWith([row("a", { scope: "Org" })]);
		await menuItem(w, "a", "hold").trigger("click");
		await flushPromises();
		api.adminListSkills.mockClear();
		api.adminListSkills.mockResolvedValue(page([row("a", { scope: "Org", admin_hold: 1 })]));
		w.findComponent({ name: "SkillHoldDialog" }).vm.$emit("held", { name: "a", needs_apply: true });
		await flushPromises();
		expect(api.adminListSkills).toHaveBeenCalledTimes(1);
		expect(rowsOf(w)[0].findAll(".badge").map((b) => b.text())).toContain("On hold");
		expect(w.find(".jv-skill-admin-catalog").exists()).toBe(true);
	});

	it("re-reads the list after a release, with no Apply when none is needed", async () => {
		const w = await mountWith([row("a", { admin_hold: 1 })]);
		await menuItem(w, "a", "release").trigger("click");
		await flushPromises();
		api.adminListSkills.mockResolvedValue(page([row("a")]));
		w.findComponent({ name: "SkillReleaseDialog" }).vm.$emit("released", { name: "a", needs_apply: false });
		await flushPromises();
		expect(menuItem(w, "a", "hold")).toBeDefined();
		expect(w.find(".jv-skill-admin-catalog").exists()).toBe(false);
	});

	it("keeps the hold dialog for a skill when it is cancelled, so its draft survives", async () => {
		const w = await mountWith([row("a"), row("b")]);
		await menuItem(w, "a", "hold").trigger("click");
		await flushPromises();
		w.findComponent({ name: "SkillHoldDialog" }).vm.$emit("update:modelValue", false);
		await flushPromises();
		// Closed, and still mounted: the typed reason lives in it.
		expect(w.find(".stub-hold").attributes("data-open")).toBe("");
		expect(w.find(".stub-hold").attributes("data-name")).toBe("a");
		await menuItem(w, "a", "hold").trigger("click");
		await flushPromises();
		expect(w.find(".stub-hold").attributes("data-open")).toBe("1");
		expect(w.findAll(".stub-hold")).toHaveLength(1);
	});

	it("takes the closed hold dialog away once the list shows the skill held", async () => {
		const w = await mountWith([row("a")]);
		await menuItem(w, "a", "hold").trigger("click");
		await flushPromises();
		w.findComponent({ name: "SkillHoldDialog" }).vm.$emit("update:modelValue", false);
		await flushPromises();
		api.adminListSkills.mockResolvedValue(page([row("a", { admin_hold: 1 })]));
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(w.find(".stub-hold").exists()).toBe(false);
	});
});

describe("Skills AdminTab, Delete", () => {
	it("reads how many macros use the skill before asking, and says so in the question", async () => {
		api.adminGetSkill.mockResolvedValue({ used_by_macros: { total: 3, armed: 1 } });
		const w = await mountWith([row("a")]);
		await menuItem(w, "a", "delete").trigger("click");
		await flushPromises();
		expect(api.adminGetSkill).toHaveBeenCalledWith("a");
		const ask = confirm.mock.calls[0][0];
		expect(ask.title).toBe("Delete this skill?");
		expect(ask.confirmLabel).toBe("Delete");
		expect(ask.danger).toBe(true);
		expect(ask.message).toContain("Delete “skill-a”, owned by Asha Rao (asha@example.test)?");
		expect(ask.message).toContain("3 macros use it (1 of them writes without asking).");
		expect(ask.message).toContain("The owner is told an admin deleted it.");
		expect(ask.message).not.toContain("for everyone");
	});

	it("says for everyone, and what Apply does, for a company skill", async () => {
		const w = await mountWith([row("a", { scope: "Org" })]);
		await menuItem(w, "a", "delete").trigger("click");
		await flushPromises();
		const ask = confirm.mock.calls[0][0];
		expect(ask.title).toBe("Delete this skill for everyone?");
		expect(ask.confirmLabel).toBe("Delete for everyone");
		expect(ask.message).toContain("Delete “skill-a” for everyone?");
		expect(ask.message).toContain("goes for all of them");
		expect(ask.message).toContain("after you press Apply");
		expect(ask.message).toContain("No macro uses it.");
		expect(ask.message).not.toMatch(/\bOrg\b|scope|armed/i);
	});

	it("deletes on confirm, says so, and re-reads the list", async () => {
		const w = await mountWith([row("a"), row("b")]);
		api.adminListSkills.mockResolvedValue(page([row("b")]));
		await menuItem(w, "a", "delete").trigger("click");
		await flushPromises();
		expect(api.adminDelete).toHaveBeenCalledWith("a");
		expect(toast.success).toHaveBeenCalledWith("Deleted “skill-a”");
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("does nothing when the question is cancelled", async () => {
		confirm.mockResolvedValue(false);
		const w = await mountWith([row("a")]);
		await menuItem(w, "a", "delete").trigger("click");
		await flushPromises();
		expect(api.adminDelete).not.toHaveBeenCalled();
	});

	it("shows the Apply notice when the delete says the catalog changed", async () => {
		api.adminDelete.mockResolvedValue({ ok: true, needs_apply: true, used_by_macros: { total: 0, armed: 0 } });
		const w = await mountWith([row("a", { scope: "Org" })]);
		expect(w.find(".jv-skill-admin-catalog").exists()).toBe(false);
		await menuItem(w, "a", "delete").trigger("click");
		await flushPromises();
		expect(w.find(".jv-skill-admin-catalog").exists()).toBe(true);
	});

	it("does not ask when the skill cannot be read first, and says why", async () => {
		api.adminGetSkill.mockRejectedValue(new Error("This skill was deleted."));
		const w = await mountWith([row("a")]);
		api.adminListSkills.mockClear();
		await menuItem(w, "a", "delete").trigger("click");
		await flushPromises();
		expect(confirm).not.toHaveBeenCalled();
		expect(api.adminDelete).not.toHaveBeenCalled();
		expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("This skill was deleted."));
		// The row is stale: the list is read again.
		expect(api.adminListSkills).toHaveBeenCalledTimes(1);
	});

	it("says what went wrong when the delete is refused, and re-reads the list", async () => {
		api.adminDelete.mockRejectedValue(new Error("You cannot delete a learned skill."));
		const w = await mountWith([row("a")]);
		api.adminListSkills.mockClear();
		await menuItem(w, "a", "delete").trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith(
			expect.stringContaining("You cannot delete a learned skill.")
		);
		expect(api.adminListSkills).toHaveBeenCalledTimes(1);
	});

	it("puts focus on the row's Actions button before the question opens", async () => {
		const w = await mountWith([row("z"), row("a")], undefined, { attachTo: document.body });
		const seen = [];
		confirm.mockImplementation(async () => {
			seen.push(document.activeElement);
			return false;
		});
		await rowsOf(w)[1].find(".jv-skill-admin-delete").trigger("click");
		await flushPromises();
		expect(seen).toEqual([rowsOf(w)[1].find(".jv-skill-admin-actions").element]);
		// And the hold dialog: the same button has focus when it opens.
		await rowsOf(w)[1].find(".jv-skill-admin-hold").trigger("click");
		await flushPromises();
		expect(w.find(".stub-hold").attributes("data-open")).toBe("1");
		expect(document.activeElement).toBe(rowsOf(w)[1].find(".jv-skill-admin-actions").element);
		w.unmount();
	});
});

describe("Skills AdminTab, the catalog update", () => {
	const needsApply = { sync: { pending: false, needs_apply: true, last_synced_at: null } };

	it("says nothing when the catalog is up to date", async () => {
		const w = await mountWith([row("a")]);
		expect(w.find(".jv-skill-admin-catalog").exists()).toBe(false);
	});

	it("says a catalog update is pending, with Apply, when the server says it needs one", async () => {
		const w = await mountWith([row("a")], needsApply);
		const note = w.find(".jv-skill-admin-catalog");
		expect(note.text()).toContain("Catalog update pending");
		expect(note.find(".jv-skill-admin-apply").text()).toBe("Apply");
	});

	it("shows it while an apply is running, with Apply busy", async () => {
		shared.getCustomSkillsSyncStatus.mockResolvedValue({ pending: true });
		const w = await mountWith([row("a")], { sync: { pending: true, needs_apply: false } });
		expect(w.find(".jv-skill-admin-catalog").exists()).toBe(true);
		expect(w.find(".jv-skill-admin-apply").attributes("data-loading")).toBe("1");
		w.unmount();
	});

	it("applies with the Skills list's own call, polls until it is done, then re-reads", async () => {
		const w = await mountWith([row("a")], needsApply);
		vi.useFakeTimers();
		shared.getCustomSkillsSyncStatus.mockResolvedValueOnce({ pending: true });
		api.adminListSkills.mockClear();
		api.adminListSkills.mockResolvedValue(page([row("a")]));
		await w.find(".jv-skill-admin-apply").trigger("click");
		await flushPromises();
		expect(shared.applyCustomSkills).toHaveBeenCalledTimes(1);
		expect(w.find(".jv-skill-admin-apply").attributes("data-loading")).toBe("1");
		// Still applying after the first poll.
		await vi.advanceTimersByTimeAsync(3000);
		expect(api.adminListSkills).not.toHaveBeenCalled();
		expect(w.find(".jv-skill-admin-catalog").exists()).toBe(true);
		// Done after the second: the list is read again and the notice goes.
		await vi.advanceTimersByTimeAsync(3000);
		await flushPromises();
		expect(api.adminListSkills).toHaveBeenCalledTimes(1);
		expect(w.find(".jv-skill-admin-catalog").exists()).toBe(false);
		w.unmount();
	});

	it("says what went wrong when Apply is refused, and keeps the notice", async () => {
		shared.applyCustomSkills.mockRejectedValue(new Error("Skills sync is locked."));
		const w = await mountWith([row("a")], needsApply);
		await w.find(".jv-skill-admin-apply").trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("Skills sync is locked."));
		expect(w.find(".jv-skill-admin-catalog").exists()).toBe(true);
		expect(w.find(".jv-skill-admin-apply").attributes("data-loading")).toBe("");
	});
});

describe("Skills AdminTab, the keyboard", () => {
	// A browser's click says how many pointer clicks made it: none when it came
	// from a key, one for a mouse or a finger.
	const fire = async (el, detail) => {
		el.element.dispatchEvent(new MouseEvent("click", { bubbles: true, detail }));
		await flushPromises();
	};
	const press = async (el) => {
		el.element.focus();
		await el.trigger("keydown", { key: "Enter" });
		await fire(el, 0);
	};
	const pending = () => {
		let answer;
		api.adminListSkills.mockReturnValue(new Promise((r) => (answer = r)));
		return (v) => answer(v);
	};

	it("keeps keyboard focus through Load more, Refresh and Clear", async () => {
		api.adminListSkills.mockImplementation(serve(many(45)));
		const w = mount(AdminTab, { attachTo: document.body });
		await flushPromises();

		// Load more: focus goes to the first row it brought.
		let answer = pending();
		await press(w.find(".jv-skill-admin-more"));
		document.activeElement.blur();
		answer(page(many(20, 20), { page: 2, has_more: true }));
		await flushPromises();
		expect(document.activeElement).toBe(rowsOf(w)[20].find(".jv-skill-admin-open").element);

		// Refresh: back on Refresh.
		answer = pending();
		await press(w.find(".jv-skill-admin-refresh"));
		document.activeElement.blur();
		answer(page(many(40), { has_more: true }));
		await flushPromises();
		expect(document.activeElement).toBe(w.find(".jv-skill-admin-refresh").element);

		// Clear removes itself: focus goes to the search box.
		api.adminListSkills.mockResolvedValue(page(many(3)));
		await control(w, "Owner").find("select").setValue("ben@example.test");
		await flushPromises();
		answer = pending();
		await press(w.find(".jv-skill-admin-clear"));
		document.activeElement.blur();
		answer(page(many(20), { has_more: true }));
		await flushPromises();
		expect(w.find(".jv-skill-admin-clear").exists()).toBe(false);
		expect(document.activeElement).toBe(control(w, "Search").find("input").element);
		w.unmount();
	});

	it("leaves focus alone after a click", async () => {
		const w = await mountWith(many(3), undefined, { attachTo: document.body });
		const refresh = w.find(".jv-skill-admin-refresh");
		const answer = pending();
		refresh.element.focus();
		await refresh.trigger("pointerdown");
		await fire(refresh, 1);
		document.activeElement.blur();
		answer(page(many(3)));
		await flushPromises();
		expect(document.activeElement).toBe(document.body);
		w.unmount();
	});

	it("moves focus into the Actions menu when it opens, and only then", async () => {
		const w = await mountWith([row("a")], undefined, { attachTo: document.body });
		const content = document.createElement("div");
		content.setAttribute("data-reka-menu-content", "");
		content.tabIndex = -1;
		document.body.appendChild(content);
		const menu = w.findComponent({ name: "Dropdown" });
		menu.vm.$emit("update:open", false);
		await new Promise((r) => requestAnimationFrame(r));
		expect(document.activeElement).not.toBe(content);
		menu.vm.$emit("update:open", true);
		await new Promise((r) => requestAnimationFrame(r));
		expect(document.activeElement).toBe(content);
		content.remove();
		w.unmount();
	});

	it("opens the menu from the side its button is on: the row's end, or a card's start", async () => {
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
});
