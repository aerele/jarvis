// The admin "User activity" page: the Summary (one row per person, flagged first, flag
// chips), the drill-down into Events for one person with the dates kept, "Load more" over
// the cursor, and the empty / error / notes states. The endpoint, the person search and the
// shell store are mocked; frappe-ui is replaced by thin stubs that keep each control's name,
// value and events so a test drives the page as a person would.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const api = vi.hoisted(() => ({ getUserActivity: vi.fn() }));
vi.mock("@/api/userActivity", () => api);
vi.mock("@/api/agents", () => ({ searchUsers: vi.fn(async () => []) }));

// The route the page syncs its filters and tab to. `push` behaves like the real router for
// the page's purposes: the new query becomes the route's query (a link, back, or forward
// is a test setting `nav.route.query` directly).
const nav = vi.hoisted(() => ({ route: null, push: null, replace: null }));
vi.mock("vue-router", async () => {
	const { reactive } = await import("vue");
	nav.route = reactive({ query: {} });
	nav.push = vi.fn(async (loc) => {
		nav.route.query = { ...(loc.query || {}) };
	});
	nav.replace = vi.fn(async (loc) => {
		nav.route.query = { ...(loc.query || {}) };
	});
	return {
		useRoute: () => nav.route,
		useRouter: () => ({ push: nav.push, replace: nav.replace }),
	};
});

vi.mock("frappe-ui", () => ({
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "modelValue", "placeholder", "loading"],
		emits: ["update:modelValue", "update:query"],
		template: `<div class="autocomplete" :data-value="modelValue && modelValue.value"><slot name="target" :togglePopover="() => {}" :isOpen="false" /><button v-for="o in options" :key="o.value" type="button" class="ac-option" @click="$emit('update:modelValue', o)">{{ o.label }}</button></div>`,
	},
	Badge: {
		name: "Badge",
		props: ["label", "theme"],
		template: `<span class="badge" :data-theme="theme">{{ label }}</span>`,
	},
	Breadcrumbs: { name: "Breadcrumbs", template: `<nav />` },
	Button: {
		name: "Button",
		props: ["label", "loading", "disabled", "variant", "type"],
		emits: ["click"],
		template: `<button :type="type || 'button'" :disabled="disabled || loading" :data-loading="loading ? '1' : ''" @click="$emit('click', $event)">{{ label }}</button>`,
	},
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: `<i />` },
	FormControl: {
		name: "FormControl",
		inheritAttrs: false,
		props: ["type", "label", "modelValue", "options", "disabled"],
		emits: ["update:modelValue"],
		template: `<label class="fc"><span>{{ label }}</span><select v-if="type === 'select'" v-bind="$attrs" :disabled="disabled" :value="modelValue" @change="$emit('update:modelValue', $event.target.value)"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select><input v-else v-bind="$attrs" :type="type" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" /></label>`,
	},
}));

import UserActivityPage from "./UserActivityPage.vue";
import { defaultRange } from "./activityFormat";

const THRESHOLDS = {
	delete_burst_count: 50,
	delete_burst_minutes: 60,
	big_export_rows: 50000,
	daily_export_rows: 100000,
	refusals_per_day: 5,
	business_hours_start: 8,
	business_hours_end: 20,
};
const SCOPE =
	"Covers actions taken in Jarvis only (not Desk). Agent activity recorded from 2026-10-01.";

function person(over = {}) {
	return {
		user: "ann@example.com",
		name: "Ann Lee",
		turns: 12,
		tokens: 45000,
		reads: 30,
		rows_read: 1200,
		exports: 0,
		rows_exported: 0,
		writes: 3,
		erp_deletes: 0,
		refused_denied: 0,
		failed: 0,
		actions: 4,
		flags: [],
		...over,
	};
}

function summary(over = {}) {
	return {
		people: [
			person({
				user: "bob@example.com",
				name: "Bob Ray",
				erp_deletes: 62,
				flags: [
					{ key: "delete_burst", label: "62 deletes in 1 hour", value: 62 },
					{ key: "off_hours", label: "3 events outside 08:00 to 20:00", value: 3 },
				],
			}),
			person(),
		],
		thresholds: THRESHOLDS,
		first_event_at: { agent: "2026-10-01 09:00:00", app: null },
		logging_health: { warn: false, rollup_lag_days: 0, scheduler_disabled: false },
		stale_days: 0,
		clamped: false,
		partial: false,
		excluded_days: 0,
		preparing: false,
		notes: [SCOPE],
		...over,
	};
}

function eventRow(i, over = {}) {
	return {
		at: `2026-11-1${i % 10} 10:0${i % 10}:00`,
		actor: "bob@example.com",
		actor_name: "Bob Ray",
		activity: "delete",
		method: "delete_doc",
		label: `Delete doc ${i}`,
		ref_doctype: "ToDo",
		ref_name: `TODO-${i}`,
		rows: 1,
		outcome: "applied",
		channel: "chat",
		code: "",
		source: "write",
		...over,
	};
}

function events(rows, next = null, over = {}) {
	return {
		rows,
		next_cursor: next,
		thresholds: THRESHOLDS,
		preparing: false,
		notes: [SCOPE],
		...over,
	};
}

const mounted = [];
async function mountPage() {
	const w = mount(UserActivityPage, {
		attachTo: document.body,
		global: { stubs: { LayoutHeader: true } },
	});
	mounted.push(w);
	await flushPromises();
	return w;
}

beforeEach(() => {
	api.getUserActivity.mockReset();
	nav.route.query = {};
	nav.push.mockClear();
	nav.replace.mockClear();
	api.getUserActivity.mockImplementation(async ({ view }) =>
		view === "events" ? events([eventRow(1), eventRow(2)]) : summary()
	);
});
afterEach(() => {
	mounted.splice(0).forEach((w) => w.unmount());
});

const lastCall = () => api.getUserActivity.mock.calls.at(-1)[0];
const tabButton = (w, key) => w.find(`#ua-tab-${key}`);

describe("UserActivityPage summary", () => {
	it("opens on the Summary for the last 30 days, open-ended (To empty)", async () => {
		const w = await mountPage();
		const range = defaultRange();
		expect(api.getUserActivity).toHaveBeenCalledTimes(1);
		expect(lastCall()).toMatchObject({
			view: "summary",
			from: range.from,
			to: "",
			user: "",
		});
		expect(tabButton(w, "summary").attributes("aria-selected")).toBe("true");
		expect(w.text()).toContain(SCOPE);
	});

	it("shows one row per person in the server's order with flag chips", async () => {
		const w = await mountPage();
		const rows = w.findAll("[data-test=person-row]");
		expect(rows).toHaveLength(2);
		expect(rows[0].text()).toContain("Bob Ray");
		expect(rows[0].text()).toContain("bob@example.com");
		expect(rows[1].text()).toContain("Ann Lee");
		const chips = rows[0].findAll("[data-test=flag-chip]");
		expect(chips.map((c) => c.text())).toEqual([
			"62 deletes in 1 hour",
			"3 events outside 08:00 to 20:00",
		]);
		expect(chips[0].attributes("data-theme")).toBe("red");
		expect(chips[1].attributes("data-theme")).toBe("orange");
		expect(rows[1].findAll("[data-test=flag-chip]")).toHaveLength(0);
		expect(rows[1].text()).toContain("45,000");
		expect(w.find("[data-test=summary-count]").text()).toContain("2 people");
		expect(w.find("[data-test=summary-count]").text()).toContain("1 flagged");
	});

	it("uses the Desk report's columns in its order", async () => {
		const w = await mountPage();
		const heads = w.findAll("[role=columnheader]").map((h) => h.text());
		expect(heads).toEqual([
			"Person",
			"Turns",
			"Tokens",
			"Reads",
			"Records read",
			"Exports",
			"Records exported",
			"Writes",
			"ERP deletes",
			"Refused or denied",
			"Failed",
			"Actions",
		]);
	});

	it("explains each flag from the server's thresholds", async () => {
		const w = await mountPage();
		const legend = w.find("#ua-legend").element.parentElement.textContent;
		expect(legend).toContain("50 or more ERP deletes by one person within 1 hour");
		expect(legend).toContain("50,000 or more records");
		expect(legend).toContain("100,000 or more records exported");
		expect(legend).toContain("5 or more refused or denied calls");
		expect(legend).toContain("before 08:00 or from 20:00, site time");
	});

	it("says there is no activity when the range is empty", async () => {
		api.getUserActivity.mockResolvedValue(summary({ people: [] }));
		const w = await mountPage();
		expect(w.text()).toContain("No activity in this range");
		expect(w.findAll("[data-test=person-row]")).toHaveLength(0);
	});

	it("leaves the 'No person' bucket as plain text: there is no one to drill into", async () => {
		api.getUserActivity.mockResolvedValue(
			summary({ people: [person({ user: "", name: "No person" })] })
		);
		const w = await mountPage();
		expect(w.find("[data-test=person-link]").exists()).toBe(false);
		expect(w.find("[data-test=person-row]").text()).toContain("No person");
	});
});

describe("UserActivityPage drill-down", () => {
	it("opens Events for the person with the same dates", async () => {
		const w = await mountPage();
		const range = defaultRange();
		await w.findAll("[data-test=person-link]")[0].trigger("click");
		await flushPromises();
		expect(lastCall()).toMatchObject({
			view: "events",
			from: range.from,
			to: "",
			user: "bob@example.com",
		});
		expect(tabButton(w, "events").attributes("aria-selected")).toBe("true");
		expect(w.find("[data-test=events-title]").text()).toBe("Events for Bob Ray");
		expect(w.findAll("[data-test=event-row]")).toHaveLength(2);
		// focus moves into the panel so a keyboard user lands on the result
		expect(document.activeElement).toBe(w.find("#ua-panel").element);
	});

	it("keeps dates the admin applied, not the default", async () => {
		const w = await mountPage();
		const [from, to] = w.findAll("input[type=date]");
		await from.setValue("2026-11-02");
		await to.setValue("2026-11-09");
		await w.find("form").trigger("submit");
		await flushPromises();
		await w.findAll("[data-test=person-link]")[1].trigger("click");
		await flushPromises();
		expect(lastCall()).toMatchObject({
			view: "events",
			from: "2026-11-02",
			to: "2026-11-09",
			user: "ann@example.com",
		});
	});

	it("goes back to the summary for everyone", async () => {
		const w = await mountPage();
		await w.findAll("[data-test=person-link]")[0].trigger("click");
		await flushPromises();
		const back = w.findAll("button").find((b) => b.text() === "Back to summary");
		await back.trigger("click");
		await flushPromises();
		expect(tabButton(w, "summary").attributes("aria-selected")).toBe("true");
		expect(lastCall()).toMatchObject({ view: "summary", user: "" });
		expect(w.findAll("[data-test=person-row]")).toHaveLength(2);
	});
});

describe("UserActivityPage events", () => {
	it("loads the next page with the cursor and appends it", async () => {
		api.getUserActivity.mockImplementation(async ({ view, cursor }) => {
			if (view === "summary") return summary();
			if (cursor === "c1") return events([eventRow(3)], null, { notes: [] });
			return events([eventRow(1), eventRow(2)], "c1");
		});
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(w.findAll("[data-test=event-row]")).toHaveLength(2);
		const more = w.findAll("button").find((b) => b.text() === "Load more");
		await more.trigger("click");
		await flushPromises();
		expect(lastCall()).toMatchObject({ view: "events", cursor: "c1" });
		expect(w.findAll("[data-test=event-row]")).toHaveLength(3);
		expect(w.findAll("button").some((b) => b.text() === "Load more")).toBe(false);
		// the first page's scope line still stands after a page with no notes
		expect(w.text()).toContain(SCOPE);
	});

	it("keeps the rows and offers another try when Load more fails", async () => {
		api.getUserActivity.mockImplementation(async ({ view, cursor }) => {
			if (view === "summary") return summary();
			if (cursor) throw new Error("boom");
			return events([eventRow(1)], "c1");
		});
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		await w
			.findAll("button")
			.find((b) => b.text() === "Load more")
			.trigger("click");
		await flushPromises();
		expect(w.findAll("[data-test=event-row]")).toHaveLength(1);
		expect(w.find("[data-test=more-error]").exists()).toBe(true);
		expect(w.findAll("button").some((b) => b.text() === "Load more")).toBe(true);
	});

	it("sends the activity and outcome filters to Events only", async () => {
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		const [activity, outcome] = w.findAll("select");
		expect(activity.attributes("disabled")).toBeUndefined();
		await activity.setValue("delete");
		await outcome.setValue("denied");
		await w.find("form").trigger("submit");
		await flushPromises();
		expect(lastCall()).toMatchObject({
			view: "events",
			activity: "delete",
			outcome: "denied",
		});
		await tabButton(w, "summary").trigger("click");
		await flushPromises();
		expect(lastCall().view).toBe("summary");
		expect(lastCall().activity).toBeUndefined();
		expect(lastCall().outcome).toBeUndefined();
	});

	it("says there are no events when the range is empty", async () => {
		api.getUserActivity.mockImplementation(async ({ view }) =>
			view === "events" ? events([]) : summary()
		);
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(w.text()).toContain("No activity in this range");
	});

	it("shows times as the site stored them", async () => {
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(w.findAll("[data-test=event-row]")[0].text()).toContain("Nov 11, 10:01");
	});
});

describe("UserActivityPage states", () => {
	it("shows a loading placeholder while the summary is being fetched", async () => {
		let release;
		api.getUserActivity.mockImplementation(
			() => new Promise((resolve) => (release = () => resolve(summary())))
		);
		const w = mount(UserActivityPage, { global: { stubs: { LayoutHeader: true } } });
		mounted.push(w);
		await flushPromises();
		expect(w.text()).toContain("Loading activity");
		expect(w.find("#ua-panel").attributes("aria-busy")).toBe("true");
		release();
		await flushPromises();
		expect(w.text()).not.toContain("Loading activity");
		expect(w.findAll("[data-test=person-row]")).toHaveLength(2);
	});

	it("shows the error with a Retry that tries again", async () => {
		api.getUserActivity.mockRejectedValueOnce(
			Object.assign(new Error("denied"), { messages: ["Not permitted."] })
		);
		const w = await mountPage();
		expect(w.find("[role=alert]").text()).toContain("Not permitted.");
		await w
			.findAll("button")
			.find((b) => b.text() === "Retry")
			.trigger("click");
		await flushPromises();
		expect(api.getUserActivity).toHaveBeenCalledTimes(2);
		expect(w.findAll("[data-test=person-row]")).toHaveLength(2);
		expect(w.find("[role=alert]").exists()).toBe(false);
	});

	it("shows the server's notes: partial totals, excluded days, paused scheduler, clamp", async () => {
		const notes = [
			SCOPE,
			"Data is kept for 90 days.",
			"Totals for 2 days are still being prepared.",
			"Totals for the 3 oldest days in this range are not included yet.",
			"Background jobs are paused on this site, so daily totals are not being updated.",
		];
		api.getUserActivity.mockResolvedValue(
			summary({
				notes,
				clamped: true,
				stale_days: 2,
				partial: true,
				excluded_days: 3,
				logging_health: { warn: false, rollup_lag_days: 5, scheduler_disabled: true },
			})
		);
		const w = await mountPage();
		const banner = w.find("[data-test=notes-banner]");
		for (const n of notes.slice(1)) expect(banner.text()).toContain(n);
		// the scope line is the header's, not repeated in the banner
		expect(banner.text()).not.toContain("Covers actions taken");
		expect(w.text()).toContain(SCOPE);
		expect(banner.html()).toContain("bg-surface-amber-2");
	});

	it("shows only the preparing note when the totals are not ready", async () => {
		const note =
			"Activity totals are still being prepared. Please try again in a few minutes.";
		api.getUserActivity.mockResolvedValue(
			summary({ people: [], preparing: true, notes: [note] })
		);
		const w = await mountPage();
		expect(w.find("[data-test=notes-banner]").text()).toContain(note);
	});

	it("shows no banner when nothing needs saying", async () => {
		const w = await mountPage();
		expect(w.find("[data-test=notes-banner]").exists()).toBe(false);
	});

	it("ignores a late answer to filters that have since changed", async () => {
		const pending = [];
		api.getUserActivity.mockImplementation(
			(args) => new Promise((resolve) => pending.push({ args, resolve }))
		);
		const w = mount(UserActivityPage, { global: { stubs: { LayoutHeader: true } } });
		mounted.push(w);
		await flushPromises();
		// Apply is off while loading, but Back / Forward still change the filters mid-flight
		nav.route.query = { from: "2026-11-03" };
		await flushPromises();
		expect(pending).toHaveLength(2);
		pending[1].resolve(summary({ people: [person({ name: "Newer" })] }));
		await flushPromises();
		pending[0].resolve(summary({ people: [person({ name: "Older" })] }));
		await flushPromises();
		expect(w.text()).toContain("Newer");
		expect(w.text()).not.toContain("Older");
	});

	it("reloads Events with the new filters when the old request is still in flight", async () => {
		// Events loading (first answer not back) -> Summary -> Apply new dates -> back to
		// Events: the pending request answers the OLD filters, so it must not stand in.
		const pending = [];
		api.getUserActivity.mockImplementation(({ view, ...rest }) => {
			if (view === "summary") return Promise.resolve(summary());
			return new Promise((resolve) => pending.push({ args: { view, ...rest }, resolve }));
		});
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(pending).toHaveLength(1);
		await tabButton(w, "summary").trigger("click");
		await flushPromises();
		await w.findAll("input[type=date]")[0].setValue("2026-11-03");
		await w.find("form").trigger("submit");
		await flushPromises();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(pending).toHaveLength(2);
		expect(pending[1].args).toMatchObject({ view: "events", from: "2026-11-03" });
		// the late answer to the old filters is dropped; the new one is shown
		pending[0].resolve(events([eventRow(1)]));
		await flushPromises();
		expect(w.find("[data-test=event-row]").exists()).toBe(false);
		pending[1].resolve(events([eventRow(2), eventRow(3)]));
		await flushPromises();
		expect(w.findAll("[data-test=event-row]")).toHaveLength(2);
	});

	it("does not fetch twice when the same request is already on its way", async () => {
		const pending = [];
		api.getUserActivity.mockImplementation(({ view }) =>
			view === "summary"
				? Promise.resolve(summary())
				: new Promise((resolve) => pending.push(resolve))
		);
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		await tabButton(w, "summary").trigger("click");
		await flushPromises();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(pending).toHaveLength(1);
	});
});

describe("UserActivityPage wording", () => {
	afterEach(() => {
		delete window.__;
	});

	it("passes its copy through the translator, placeholders included", async () => {
		window.__ = (text, args = []) =>
			"~" + text.replace(/\{(\d+)\}/g, (m, i) => (args[i] != null ? args[i] : m));
		const w = await mountPage();
		const text = w.text();
		for (const s of [
			"~User activity",
			"~Apply",
			"~Summary",
			"~Events",
			"~Person",
			"~Records read",
		]) {
			expect(text).toContain(s);
		}
		expect(w.find("[data-test=summary-count]").text()).toBe("~2 people, 1 flagged");
		expect(w.find("[data-test=export-link]").text()).toContain(
			"~Export to Excel in the Desk report"
		);
		expect(text).toContain("~50 or more ERP deletes by one person within 1 hour.");
		expect(w.find("[data-test=person-link]").attributes("aria-label")).toBe(
			"~View events for Bob Ray"
		);
	});

	it("labels the footer link for what it does and says it opens a new tab (E2)", async () => {
		const w = await mountPage();
		const link = w.find("[data-test=export-link]");
		expect(link.text()).toContain("Export to Excel in the Desk report");
		expect(link.find(".sr-only").text()).toBe("(opens in a new tab)");
		expect(link.attributes("target")).toBe("_blank");
		expect(link.find("i").exists()).toBe(true);
	});
});

describe("UserActivityPage filters", () => {
	it("applies the person picked in the filters", async () => {
		const searchUsers = (await import("@/api/agents")).searchUsers;
		searchUsers.mockResolvedValue([{ name: "cy@example.com", full_name: "Cy Dee" }]);
		const w = await mountPage();
		w.find("[data-test=person-group]").element.dispatchEvent(
			new Event("focusin", { bubbles: true })
		);
		await new Promise((r) => setTimeout(r, 0));
		await flushPromises();
		await w.find(".ac-option").trigger("click");
		await w.find("form").trigger("submit");
		await flushPromises();
		expect(lastCall()).toMatchObject({ view: "summary", user: "cy@example.com" });
	});

	it("refuses a range that ends before it starts", async () => {
		const w = await mountPage();
		const [from, to] = w.findAll("input[type=date]");
		await from.setValue("2026-11-10");
		await to.setValue("2026-11-01");
		const calls = api.getUserActivity.mock.calls.length;
		await w.find("form").trigger("submit");
		await flushPromises();
		expect(w.text()).toContain("From must be on or before To.");
		expect(api.getUserActivity).toHaveBeenCalledTimes(calls);
	});
});

describe("UserActivityPage tabs", () => {
	it("is a tablist with the selected tab in the tab order", async () => {
		const w = await mountPage();
		expect(w.find("[role=tablist]").exists()).toBe(true);
		const tabs = w.findAll("[role=tab]");
		expect(tabs.map((t) => t.text())).toEqual(["Summary", "Events"]);
		expect(tabs.map((t) => t.attributes("tabindex"))).toEqual(["0", "-1"]);
		expect(w.find("[role=tabpanel]").attributes("aria-labelledby")).toBe("ua-tab-summary");
	});

	it("moves between tabs with the arrow keys and focuses the new tab", async () => {
		const w = await mountPage();
		await w.find("[role=tablist]").trigger("keydown", { key: "ArrowRight" });
		await flushPromises();
		expect(tabButton(w, "events").attributes("aria-selected")).toBe("true");
		expect(document.activeElement).toBe(tabButton(w, "events").element);
		expect(tabButton(w, "events").attributes("tabindex")).toBe("0");
		await w.find("[role=tablist]").trigger("keydown", { key: "Home" });
		await flushPromises();
		expect(tabButton(w, "summary").attributes("aria-selected")).toBe("true");
	});
});

describe("UserActivityPage footer", () => {
	it("links the Desk report on the same view and filters", async () => {
		const w = await mountPage();
		const range = defaultRange();
		let href = w.find("[data-test=export-link]").attributes("href");
		expect(href).toContain("/app/query-report/Jarvis%20User%20Activity?");
		expect(href).toContain(`from_date=${range.from}`);
		expect(href).toContain("view=Summary");
		await w.findAll("[data-test=person-link]")[0].trigger("click");
		await flushPromises();
		href = w.find("[data-test=export-link]").attributes("href");
		expect(href).toContain("view=Events");
		expect(href).toContain("user=bob%40example.com");
	});

	it("has no unrelated Token limits button (E3)", async () => {
		const w = await mountPage();
		expect(w.findAll("button").some((b) => b.text() === "Token limits")).toBe(false);
	});
});

describe("UserActivityPage empty and first-run states (E1)", () => {
	const none = { agent: null, app: null };

	it("says nothing has been recorded yet, and still explains the flags", async () => {
		api.getUserActivity.mockResolvedValue(summary({ people: [], first_event_at: none }));
		const w = await mountPage();
		expect(w.text()).toContain(
			"Nothing has been recorded yet. Activity appears here as people use Jarvis."
		);
		expect(w.text()).not.toContain("Try a wider date range");
		expect(w.find("#ua-legend").exists()).toBe(true);
		expect(w.text()).toContain("50 or more ERP deletes");
	});

	it("says from when recording started when the range starts before it", async () => {
		nav.route.query = { from: "2026-11-01" };
		api.getUserActivity.mockResolvedValue(
			summary({
				people: [],
				first_event_at: { agent: "2026-11-10 09:00:00", app: "2026-11-12 08:00:00" },
			})
		);
		const w = await mountPage();
		expect(w.text()).toContain("No activity in this range");
		expect(w.text()).toContain("Activity is recorded from Nov 10, 2026.");
	});

	it("keeps the plain hint when the range starts after recording began", async () => {
		nav.route.query = { from: "2026-11-11" };
		api.getUserActivity.mockResolvedValue(
			summary({ people: [], first_event_at: { agent: "2026-11-10 09:00:00", app: null } })
		);
		const w = await mountPage();
		expect(w.text()).toContain("Try a wider date range, or clear the person filter.");
		expect(w.text()).not.toContain("Activity is recorded from");
	});

	it("does not claim nothing was recorded while the totals are still being prepared", async () => {
		api.getUserActivity.mockResolvedValue(
			summary({ people: [], preparing: true, first_event_at: none, notes: ["Preparing."] })
		);
		const w = await mountPage();
		expect(w.text()).not.toContain("Nothing has been recorded yet");
	});

	it("uses the same first-run line on an empty Events tab", async () => {
		api.getUserActivity.mockImplementation(async ({ view }) =>
			view === "events" ? events([]) : summary({ people: [], first_event_at: none })
		);
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Nothing has been recorded yet.");
	});
});

describe("UserActivityPage banner and scope line (E4, E5)", () => {
	it("shows the same severity on both tabs", async () => {
		api.getUserActivity.mockImplementation(async ({ view }) =>
			view === "events"
				? events([eventRow(1)], null, { notes: [SCOPE, "Data is kept for 90 days."] })
				: summary({ partial: true, notes: [SCOPE, "Totals for 3 days are not included."] })
		);
		const w = await mountPage();
		expect(w.find("[data-test=notes-banner]").html()).toContain("bg-surface-amber-2");
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(w.find("[data-test=notes-banner]").text()).toContain("Data is kept for 90 days.");
		expect(w.find("[data-test=notes-banner]").html()).toContain("bg-surface-amber-2");
	});

	it("treats an app-action logging warning as a warning", async () => {
		api.getUserActivity.mockResolvedValue(
			summary({
				notes: [SCOPE, "Jarvis app actions are not being recorded."],
				logging_health: { warn: false, app_warn: true, rollup_lag_days: 0 },
			})
		);
		const w = await mountPage();
		expect(w.find("[data-test=notes-banner]").html()).toContain("bg-surface-amber-2");
	});

	it("shows only the active tab's scope line, blank while it loads", async () => {
		api.getUserActivity.mockImplementation(({ view }) =>
			view === "summary" ? Promise.resolve(summary()) : new Promise(() => {})
		);
		const w = await mountPage();
		expect(w.text()).toContain(SCOPE);
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(w.text()).not.toContain(SCOPE);
	});
});

describe("UserActivityPage filters (E6, E7, E9, E15)", () => {
	it("drilling in sets only the person and keeps the other draft edits", async () => {
		const w = await mountPage();
		const [, to] = w.findAll("input[type=date]");
		await to.setValue("2026-11-09");
		await w.findAll("select")[1].setValue("denied");
		await w.findAll("[data-test=person-link]")[0].trigger("click");
		await flushPromises();
		// what was fetched is still the applied filters, plus the person
		expect(lastCall()).toMatchObject({ view: "events", to: "", user: "bob@example.com" });
		expect(lastCall().outcome).toBe("");
		// the unapplied edits are still in the inputs
		expect(w.findAll("input[type=date]")[1].element.value).toBe("2026-11-09");
		expect(w.findAll("select")[1].element.value).toBe("denied");
	});

	it("keeps Activity and Outcome enabled on Summary, says what they do, and applies them to Events", async () => {
		const w = await mountPage();
		const [activity, outcome] = w.findAll("select");
		expect(activity.attributes("disabled")).toBeUndefined();
		expect(outcome.attributes("disabled")).toBeUndefined();
		const hint = w.find("#ua-events-only");
		expect(hint.text()).toContain("Activity and outcome narrow the Events tab only.");
		expect(activity.attributes("aria-describedby")).toBe("ua-events-only");
		expect(outcome.attributes("aria-describedby")).toBe("ua-events-only");
		await outcome.setValue("denied");
		await w.find("form").trigger("submit");
		await flushPromises();
		// the summary ignores them
		expect(lastCall().view).toBe("summary");
		expect(lastCall().outcome).toBeUndefined();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(lastCall()).toMatchObject({ view: "events", outcome: "denied" });
		expect(w.find("#ua-events-only").exists()).toBe(false);
	});

	it("ties the range error to From and To", async () => {
		const w = await mountPage();
		const [from, to] = w.findAll("input[type=date]");
		expect(from.attributes("aria-invalid")).toBeUndefined();
		await from.setValue("2026-11-10");
		await to.setValue("2026-11-01");
		await w.find("form").trigger("submit");
		await flushPromises();
		const err = w.find("#ua-range-error");
		expect(err.text()).toBe("From must be on or before To.");
		for (const el of w.findAll("input[type=date]")) {
			expect(el.attributes("aria-invalid")).toBe("true");
			expect(el.attributes("aria-describedby")).toBe("ua-range-error");
		}
	});

	it("disables Apply while a load is in flight and ignores Enter then", async () => {
		let release;
		api.getUserActivity.mockImplementation(
			() => new Promise((resolve) => (release = () => resolve(summary())))
		);
		const w = mount(UserActivityPage, { global: { stubs: { LayoutHeader: true } } });
		mounted.push(w);
		await flushPromises();
		const applyBtn = () => w.findAll("button").find((b) => b.text() === "Apply");
		expect(applyBtn().attributes("disabled")).toBeDefined();
		await w.find("form").trigger("submit");
		expect(api.getUserActivity).toHaveBeenCalledTimes(1);
		release();
		await flushPromises();
		expect(applyBtn().attributes("disabled")).toBeUndefined();
	});
});

describe("UserActivityPage events table (E8, E11, E12, E16)", () => {
	it("labels every value in a stacked row", async () => {
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		const labels = w
			.find("[data-test=event-row]")
			.findAll("[data-test=cell-label]")
			.map((l) => l.text());
		expect(labels).toEqual([
			"When",
			"Person",
			"What",
			"Reference",
			"Records",
			"Outcome",
			"Channel",
		]);
	});

	it("omits the label of a value that is not there", async () => {
		api.getUserActivity.mockImplementation(async ({ view }) =>
			view === "events"
				? events([eventRow(1, { ref_doctype: "", ref_name: "", outcome: "" })])
				: summary()
		);
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		const labels = w
			.find("[data-test=event-row]")
			.findAll("[data-test=cell-label]")
			.map((l) => l.text());
		expect(labels).not.toContain("Reference");
		expect(labels).not.toContain("Outcome");
	});

	it("says how many events are showing", async () => {
		api.getUserActivity.mockImplementation(async ({ view, cursor }) => {
			if (view === "summary") return summary();
			if (cursor) return events([eventRow(3)], null);
			return events([eventRow(1), eventRow(2)], "c1");
		});
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(w.find("[data-test=events-count]").text()).toBe("Showing 2 events");
		await w
			.findAll("button")
			.find((b) => b.text() === "Load more")
			.trigger("click");
		await flushPromises();
		expect(w.find("[data-test=events-count]").text()).toBe("Showing 3 events");
	});

	it("says '1 event' for one", async () => {
		api.getUserActivity.mockImplementation(async ({ view }) =>
			view === "events" ? events([eventRow(1)]) : summary()
		);
		const w = await mountPage();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(w.find("[data-test=events-count]").text()).toBe("Showing 1 event");
	});

	it("shows the server's truncation note once, in the banner, and adds no line of its own", async () => {
		const note =
			"Showing the 500 people with red flags or the most activity. Narrow the dates or pick a person to see others.";
		api.getUserActivity.mockResolvedValue(
			summary({ people_truncated: true, notes: [SCOPE, note] })
		);
		const w = await mountPage();
		expect(w.find("[data-test=truncated-note]").exists()).toBe(false);
		expect(w.text().split(note).length - 1).toBe(1);
		expect(w.find("[data-test=notes-banner]").text()).toContain(note);
	});
});

describe("UserActivityPage URL (E13)", () => {
	const lastQuery = () => nav.push.mock.calls.at(-1)[0].query;

	it("writes the applied filters to the query, once, without a second fetch", async () => {
		const w = await mountPage();
		await w.findAll("input[type=date]")[0].setValue("2026-11-02");
		await w.findAll("input[type=date]")[1].setValue("2026-11-09");
		await w.find("form").trigger("submit");
		await flushPromises();
		expect(nav.push).toHaveBeenCalledTimes(1);
		expect(lastQuery()).toEqual({ from: "2026-11-02", to: "2026-11-09" });
		expect(api.getUserActivity).toHaveBeenCalledTimes(2);
	});

	it("writes the tab and the drilled person", async () => {
		const w = await mountPage();
		await w.findAll("[data-test=person-link]")[0].trigger("click");
		await flushPromises();
		expect(lastQuery()).toMatchObject({ tab: "events", user: "bob@example.com" });
		await tabButton(w, "summary").trigger("click");
		await flushPromises();
		expect(lastQuery().tab).toBeUndefined();
	});

	it("does not write the query for a draft that was not applied", async () => {
		const w = await mountPage();
		await w.findAll("input[type=date]")[0].setValue("2026-11-02");
		expect(nav.push).not.toHaveBeenCalled();
	});

	it("opens a deep link on its tab with its filters", async () => {
		nav.route.query = {
			tab: "events",
			from: "2026-11-02",
			to: "2026-11-09",
			user: "bob@example.com",
			activity: "delete",
			outcome: "denied",
		};
		const w = await mountPage();
		expect(api.getUserActivity).toHaveBeenCalledTimes(1);
		expect(lastCall()).toMatchObject({
			view: "events",
			from: "2026-11-02",
			to: "2026-11-09",
			user: "bob@example.com",
			activity: "delete",
			outcome: "denied",
		});
		expect(tabButton(w, "events").attributes("aria-selected")).toBe("true");
		expect(w.findAll("input[type=date]")[0].element.value).toBe("2026-11-02");
		expect(w.findAll("select")[0].element.value).toBe("delete");
	});

	it("ignores invalid values in the link", async () => {
		nav.route.query = { tab: "bogus", from: "yesterday", activity: "nope", outcome: ["a"] };
		const w = await mountPage();
		expect(tabButton(w, "summary").attributes("aria-selected")).toBe("true");
		expect(lastCall()).toMatchObject({ view: "summary", from: defaultRange().from, to: "" });
		expect(lastCall().activity).toBeUndefined();
	});

	it("restores the earlier state on back and forward", async () => {
		const w = await mountPage();
		const range = defaultRange();
		await w.findAll("input[type=date]")[0].setValue("2026-11-02");
		await w.find("form").trigger("submit");
		await flushPromises();
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(lastCall()).toMatchObject({ view: "events", from: "2026-11-02" });
		// back to the Apply entry (summary, new dates)
		nav.route.query = { from: "2026-11-02" };
		await flushPromises();
		expect(tabButton(w, "summary").attributes("aria-selected")).toBe("true");
		expect(w.findAll("input[type=date]")[0].element.value).toBe("2026-11-02");
		// back again to the page as it first opened
		nav.route.query = { from: range.from };
		await flushPromises();
		expect(w.findAll("input[type=date]")[0].element.value).toBe(range.from);
		expect(lastCall()).toMatchObject({ view: "summary", from: range.from });
		// forward to the Events entry
		nav.route.query = { tab: "events", from: "2026-11-02" };
		await flushPromises();
		expect(tabButton(w, "events").attributes("aria-selected")).toBe("true");
		// the Events answer for these filters is still held, so it shows without a refetch
		expect(w.findAll("[data-test=event-row]")).toHaveLength(2);
		expect(w.findAll("input[type=date]")[0].element.value).toBe("2026-11-02");
	});
});

describe("UserActivityPage browser-check fixes", () => {
	it("gives every truncated value a title with the full text", async () => {
		const w = await mountPage();
		const row = w.find("[data-test=person-row]");
		expect(row.find("[title='Bob Ray']").exists()).toBe(true);
		expect(row.find("[title='bob@example.com']").exists()).toBe(true);
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		const ev1 = w.find("[data-test=event-row]");
		expect(ev1.find("[title='Bob Ray']").exists()).toBe(true);
		expect(ev1.find("[title='bob@example.com']").exists()).toBe(true);
		expect(ev1.find("[title='Delete doc 1']").exists()).toBe(true);
		expect(ev1.find("[title='Delete']").exists()).toBe(true);
		expect(ev1.find("[title='ToDo TODO-1']").exists()).toBe(true);
	});

	it("does not let the person picker submit the filter form", async () => {
		const w = await mountPage();
		const trigger = w.find("[data-test=person-trigger]");
		expect(trigger.attributes("type")).toBe("button");
		expect(trigger.text()).toBe("Anyone");
		const submits = w.findAll("form button").filter((b) => b.attributes("type") === "submit");
		expect(submits.map((b) => b.text())).toEqual(["Apply"]);
	});

	it("shows the picked person on the trigger and returns focus to it", async () => {
		const searchUsers = (await import("@/api/agents")).searchUsers;
		searchUsers.mockResolvedValue([{ name: "cy@example.com", full_name: "Cy Dee" }]);
		const w = await mountPage();
		w.find("[data-test=person-group]").element.dispatchEvent(
			new Event("focusin", { bubbles: true })
		);
		await new Promise((r) => setTimeout(r, 0));
		await flushPromises();
		const option = w.find(".ac-option");
		option.element.focus();
		await option.trigger("click");
		await flushPromises();
		await new Promise((r) => setTimeout(r, 5));
		const trigger = w.find("[data-test=person-trigger]");
		expect(trigger.text()).toBe("Cy Dee");
		expect(document.activeElement).toBe(trigger.element);
	});

	it("keeps secondary text at ink-gray-6 or darker", async () => {
		const w = await mountPage();
		expect(w.html()).not.toContain("text-ink-gray-5");
		await tabButton(w, "events").trigger("click");
		await flushPromises();
		expect(w.html()).not.toContain("text-ink-gray-5");
		expect(w.html()).not.toContain("text-ink-gray-4 max-");
	});
});

describe("UserActivityPage deep link to Events (N3) and picker name (N4)", () => {
	const deepLinkEvents = (over) => {
		nav.route.query = { tab: "events" };
		api.getUserActivity.mockImplementation(async ({ view }) =>
			view === "events"
				? events([eventRow(1)], null, { notes: [SCOPE, "A note."], ...over })
				: summary()
		);
	};

	it("takes the banner severity from the first Events page when no Summary was loaded", async () => {
		for (const over of [
			{ logging_health: { app_warn: true } },
			{ logging_health: { warn: true } },
			{ logging_health: { scheduler_disabled: true } },
			{ logging_health: { rollup_lag_days: 4 } },
			{ partial: true },
		]) {
			deepLinkEvents(over);
			const w = await mountPage();
			expect(api.getUserActivity.mock.calls.map((c) => c[0].view)).toEqual(["events"]);
			expect(w.find("[data-test=notes-banner]").html()).toContain("bg-surface-amber-2");
			w.unmount();
			mounted.pop();
			api.getUserActivity.mockClear();
		}
	});

	it("stays informational when the first Events page reports nothing wrong", async () => {
		deepLinkEvents({ logging_health: { warn: false, rollup_lag_days: 0 } });
		const w = await mountPage();
		expect(w.find("[data-test=notes-banner]").html()).not.toContain("bg-surface-amber-2");
	});

	it("shows the first-run text on an empty Events deep link", async () => {
		nav.route.query = { tab: "events" };
		api.getUserActivity.mockResolvedValue(
			events([], null, { first_event_at: { agent: null, app: null } })
		);
		const w = await mountPage();
		expect(w.find("[data-test=first-run]").text()).toContain("Nothing has been recorded yet.");
	});

	it("degrades to the plain hint when the server sends neither", async () => {
		nav.route.query = { tab: "events" };
		api.getUserActivity.mockResolvedValue(events([]));
		const w = await mountPage();
		expect(w.find("[data-test=first-run]").exists()).toBe(false);
		expect(w.text()).toContain("Try a wider date range, or clear the filters.");
	});

	it("lets a screen reader hear the label and the chosen person, once", async () => {
		const w = await mountPage();
		const trigger = w.find("[data-test=person-trigger]");
		expect(trigger.attributes("aria-labelledby")).toBe("ua-person-label ua-person-value");
		expect(trigger.find("#ua-person-value").text()).toBe("Anyone");
		expect(w.find("#ua-person-label").text()).toBe("Person");
		expect(w.find("[data-test=person-group]").attributes("role")).toBeUndefined();
		expect(w.find("[data-test=person-group]").attributes("aria-labelledby")).toBeUndefined();
	});
});

describe("UserActivityPage table or cards by content width", () => {
	const ro = { cb: null };
	const width = { value: 0 };
	let savedRO;
	let savedDesc;

	beforeEach(() => {
		savedRO = globalThis.ResizeObserver;
		globalThis.ResizeObserver = class {
			constructor(cb) {
				ro.cb = cb;
			}
			observe() {}
			disconnect() {}
		};
		savedDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
		Object.defineProperty(HTMLElement.prototype, "clientWidth", {
			configurable: true,
			get() {
				return this.id === "ua-panel" ? width.value : 0;
			},
		});
	});
	afterEach(() => {
		globalThis.ResizeObserver = savedRO;
		if (savedDesc) Object.defineProperty(HTMLElement.prototype, "clientWidth", savedDesc);
		else delete HTMLElement.prototype.clientWidth;
	});

	const mode = (w, test) => {
		const c = w.find(`[data-test=${test}]`).classes();
		return c.includes("ua-wide") ? "table" : c.includes("ua-narrow") ? "cards" : "?";
	};
	const openEvents = async (w) => {
		await tabButton(w, "events").trigger("click");
		await flushPromises();
	};

	it("shows the summary as a table only when the content area fits its minimum", async () => {
		width.value = 1028;
		let w = await mountPage();
		expect(mode(w, "summary-table")).toBe("table");
		w.unmount();
		mounted.pop();
		width.value = 1027;
		w = await mountPage();
		expect(mode(w, "summary-table")).toBe("cards");
	});

	it("has its own, smaller threshold for the events table", async () => {
		width.value = 900;
		let w = await mountPage();
		expect(mode(w, "summary-table")).toBe("cards");
		await openEvents(w);
		expect(mode(w, "events-table")).toBe("table");
		w.unmount();
		mounted.pop();
		width.value = 899;
		w = await mountPage();
		await openEvents(w);
		expect(mode(w, "events-table")).toBe("cards");
	});

	it("follows the content width as it changes (sidebar, window resize)", async () => {
		width.value = 800;
		const w = await mountPage();
		expect(mode(w, "summary-table")).toBe("cards");
		width.value = 1104;
		ro.cb();
		await flushPromises();
		expect(mode(w, "summary-table")).toBe("table");
		width.value = 700;
		ro.cb();
		await flushPromises();
		expect(mode(w, "summary-table")).toBe("cards");
	});

	it("keys the layout off the wrapper, not the window, and never scrolls sideways", async () => {
		width.value = 1104;
		const w = await mountPage();
		const html = w.find("[data-test=summary-table]").html();
		expect(html).not.toContain("min-[769px]");
		expect(html).not.toContain("max-[768px]");
		// the minimum applies only in table mode; cards have none
		expect(w.find("[role=table]").classes()).toContain("[.ua-wide_&]:min-w-[1024px]");
		expect(html).toContain("repeat(11,minmax(4rem,1fr))");
		await openEvents(w);
		expect(w.find("[data-test=events-table] [role=table]").classes()).toContain(
			"[.ua-wide_&]:min-w-[896px]"
		);
	});

	it("keeps every value labelled in cards mode and the same rows in both", async () => {
		width.value = 600;
		const w = await mountPage();
		expect(w.findAll("[data-test=person-row]")).toHaveLength(2);
		await openEvents(w);
		expect(w.findAll("[data-test=event-row]")).toHaveLength(2);
		expect(
			w.find("[data-test=event-row]").findAll("[data-test=cell-label]").length
		).toBeGreaterThanOrEqual(5);
	});
});
