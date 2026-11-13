import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis-admin-v2#819: the Recent activity block pages through ALL of a trigger's
 * runs ("Load more") instead of ending at ten rows and a "View all" link.
 *
 * The block asks the server for the next page by CURSOR (`start + rows returned`),
 * never by the count it shows: a burst of new runs would otherwise make the next
 * page the same rows again, and the list would stall.
 */

const apiTr = vi.hoisted(() => ({
	getTrigger: vi.fn(),
	getTriggersCaps: vi.fn(),
	listActivityPage: vi.fn(),
	updateTrigger: vi.fn(),
	createTrigger: vi.fn(),
	setTriggerEnabled: vi.fn(),
	deleteTrigger: vi.fn(),
	testTriggerCondition: vi.fn(),
	listIntakeFolders: vi.fn(),
	getIntakeFieldOptions: vi.fn(),
}));
vi.mock("@/api/triggers", () => apiTr);
vi.mock("@/api", () => ({ searchLink: vi.fn(() => Promise.resolve([])) }));

const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("vue-router", () => ({
	useRouter: () => router,
	onBeforeRouteLeave: vi.fn(),
}));

vi.mock("frappe-ui", () => ({
	Autocomplete: { name: "Autocomplete", template: "<div />" },
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: "<span>{{ label }}</span>",
	},
	// Forwards the click event, so a test can say whether it came from a key
	// (`detail` 0) or a pointer (`detail` > 0). Disabled while loading, like the real one.
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading", "variant", "icon"],
		emits: ["click"],
		template: `<button :disabled="disabled || loading" :data-label="label" @click="$emit('click', $event)"><slot>{{ label }}</slot></button>`,
	},
	Dialog: { name: "Dialog", template: "<div />" },
	Dropdown: { name: "Dropdown", props: ["options"], template: "<div><slot /></div>" },
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<div v-if="message" class="error-message">{{ message }}</div>`,
	},
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: "<i />" },
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "label", "options", "disabled"],
		emits: ["update:modelValue"],
		template: `<input :type="type === 'checkbox' ? 'checkbox' : 'text'" :aria-label="label" :value="modelValue" :checked="type === 'checkbox' ? !!modelValue : undefined" :disabled="disabled" @input="type !== 'checkbox' && $emit('update:modelValue', $event.target.value)" @change="type === 'checkbox' && $emit('update:modelValue', $event.target.checked)" />`,
	},
	Switch: {
		name: "Switch",
		props: ["modelValue", "label", "disabled"],
		emits: ["update:modelValue"],
		template: `<button :disabled="disabled" @click="$emit('update:modelValue', !modelValue)">{{ label }}</button>`,
	},
	Tooltip: { name: "Tooltip", props: ["text"], template: "<span><slot /></span>" },
	toast: { success: vi.fn(), error: vi.fn() },
	confirmDialog: vi.fn(),
	// utils/datetime reads these
	dayjs: () => ({ format: () => "" }),
	dayjsLocal: () => ({ format: () => "date", fromNow: () => "ago" }),
	getConfig: () => "",
}));

vi.mock("@/components/doc/DocPage.vue", () => ({
	default: {
		name: "DocPage",
		props: ["breadcrumbs", "title", "statusBadge", "dirty", "loading", "error"],
		template: `<div><slot name="actions" /><slot name="main" /><slot name="aside" /><slot name="footer" /></div>`,
	},
}));
vi.mock("@/components/doc/DocSection.vue", () => ({
	default: {
		name: "DocSection",
		props: ["label", "opened"],
		template: `<div><slot /><slot name="header-suffix" /></div>`,
	},
}));
vi.mock("./ActivityDetailDialog.vue", () => ({
	default: {
		name: "ActivityDetailDialog",
		props: ["modelValue", "row", "caps"],
		template: "<div />",
	},
}));

import TriggerDetail from "./TriggerDetail.vue";

const FIRST_PAGE = 10;
const NEXT_PAGE = 20;

function trigger(id = "TRG-1") {
	return {
		name: id,
		trigger_name: "Big invoice " + id,
		description: "",
		enabled: 1,
		target_doctype: "Sales Invoice",
		doc_event: "on_submit",
		condition: "",
		action_type: "LLM",
		script_body: "",
		llm_instruction: "Look at it",
		llm_daily_cap: 25,
		llm_allow_lookups: 0,
	};
}

// Row `i` of a trigger's runs, newest first (ACT-0 is the newest).
const row = (i, tag = "ACT") => ({
	name: `${tag}-${i}`,
	status: "Success",
	summary: `Run ${tag}-${i}`,
	creation: "2026-01-01 10:00:00",
});
const rowsOf = (from, n, tag = "ACT") => Array.from({ length: n }, (_, k) => row(from + k, tag));

// An answer of list_activity_page.
function answer(rows, { start = 0, total = 0, has_more = false, approximate = false } = {}) {
	return { rows, total, has_more, start, page_length: rows.length, approximate };
}

// A server holding `total` runs for the trigger (a manager sees all of them).
// `newer` is the number of runs made since the page was opened: they sit on top.
function serverOf(total, { approximate = false } = {}) {
	const state = { newer: 0 };
	apiTr.listActivityPage.mockImplementation(async (p) => {
		const all = Array.from({ length: total + state.newer }, (_, i) => row(i - state.newer));
		const page = all.slice(p.start, p.start + p.page_length);
		return answer(page, {
			start: p.start,
			total: all.length,
			has_more: p.start + page.length < all.length,
			approximate,
		});
	});
	return state;
}

function defer() {
	let resolve;
	let reject;
	const promise = new Promise((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

let wrapper;
async function mountDetail(id = "TRG-1") {
	apiTr.getTrigger.mockImplementation(async (name) => trigger(name));
	apiTr.getTriggersCaps.mockResolvedValue({ can_manage: true });
	wrapper = mount(TriggerDetail, {
		props: { id, isNew: false },
		attachTo: document.body,
		global: { provide: { $socket: null } },
	});
	await flushPromises();
	await flushPromises();
	return wrapper;
}

// A failed load logs a console.warn on purpose. Every test stubs it, so a run prints no
// stderr noise; the tests that care assert the calls on `warn`.
let warn;
beforeEach(() => {
	vi.clearAllMocks();
	apiTr.listActivityPage.mockReset();
	warn = vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
	if (wrapper) wrapper.unmount();
	wrapper = null;
	document.body.innerHTML = "";
	warn.mockRestore();
});

const block = (w) => w.find('[data-testid="recent-block"]');
const rowEls = (w) => w.findAll('[data-testid="recent-row"]');
const count = (w) => w.find('[data-testid="recent-count"]');
const moreBtn = (w) =>
	block(w)
		.findAll("button")
		.find((b) => ["Load more", "Try again"].includes(b.attributes("data-label")));
const emptyLine = (w) => block(w).find('[data-testid="recent-empty"]');
const alertText = (w) =>
	block(w)
		.findAll('[role="alert"]')
		.map((a) => a.text());
// VTU cannot set `detail` on a click, so the event is made here.
function press(el, init) {
	el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, ...init }));
}
async function click(w, init) {
	press(moreBtn(w).element, init);
	await flushPromises();
}
// A mouse click has `detail` 1, a key press on a button has 0.
const MOUSE = { detail: 1 };
const KEY = { detail: 0 };

describe("TriggerDetail Recent activity: first page", () => {
	it("asks for 10 rows from 0, for this trigger, newest first", async () => {
		serverOf(37);
		await mountDetail();
		expect(apiTr.listActivityPage).toHaveBeenCalledTimes(1);
		expect(apiTr.listActivityPage.mock.calls[0][0]).toEqual({
			filters: { trigger: "TRG-1" },
			sort_field: "creation",
			sort_dir: "desc",
			start: 0,
			page_length: FIRST_PAGE,
		});
		expect(rowEls(wrapper)).toHaveLength(10);
	});

	it("has no View all link any more", async () => {
		serverOf(37);
		await mountDetail();
		expect(block(wrapper).text()).not.toContain("View all");
		expect(wrapper.find("a").exists()).toBe(false);
	});
});

describe("TriggerDetail Recent activity: Load more", () => {
	it("asks for 20 rows from the previous start plus the rows it got", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockResolvedValueOnce(answer(rowsOf(10, 7), { start: 10, total: 40, has_more: true }))
			.mockResolvedValueOnce(
				answer(rowsOf(17, 7), { start: 17, total: 40, has_more: true })
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage.mock.calls[1][0]).toMatchObject({
			start: 10,
			page_length: NEXT_PAGE,
			filters: { trigger: "TRG-1" },
			sort_field: "creation",
			sort_dir: "desc",
		});
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage.mock.calls[2][0]).toMatchObject({
			start: 17,
			page_length: NEXT_PAGE,
		});
		expect(rowEls(wrapper)).toHaveLength(24);
	});

	it("a burst of new runs does not stall the list (the cursor counts server rows)", async () => {
		// 80 runs. Page 3 (start 30) is answered with 20 rows already on screen, as it
		// is when 20 runs were made meanwhile; the cursor must still move on to 50.
		// (PM-1: that all-duplicate page is followed at once by the next one, in the same click.)
		const byStart = {
			0: answer(rowsOf(0, 10), { start: 0, total: 80, has_more: true }),
			10: answer(rowsOf(10, 20), { start: 10, total: 80, has_more: true }),
			30: answer(rowsOf(10, 20), { start: 30, total: 100, has_more: true }),
			50: answer(rowsOf(30, 20), { start: 50, total: 100, has_more: true }),
		};
		apiTr.listActivityPage.mockImplementation(async (p) => byStart[p.start]);
		await mountDetail();
		await click(wrapper, MOUSE); // 30 shown
		expect(rowEls(wrapper)).toHaveLength(30);
		await click(wrapper, MOUSE); // 20 duplicates, then the page from 50 in the same click
		expect(apiTr.listActivityPage.mock.calls.map((c) => c[0].start)).toEqual([0, 10, 30, 50]);
		expect(rowEls(wrapper)).toHaveLength(50);
	});

	it("a real burst: new runs on top push rows down, none is shown twice or skipped", async () => {
		const state = serverOf(60);
		await mountDetail(); // rows 0..9
		state.newer = 5; // five runs made since: every row moved down five places
		await click(wrapper, MOUSE); // start 10 -> old rows 5..24 (5..9 are shown already)
		const names = rowEls(wrapper).map((r) => r.attributes("data-name"));
		expect(new Set(names).size).toBe(names.length);
		// Old rows 0..24 are all there; none was skipped.
		expect(names).toEqual(rowsOf(0, 25).map((r) => r.name));
	});

	it("skips a row whose name is already shown", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockResolvedValueOnce(
				answer([...rowsOf(5, 5), ...rowsOf(10, 15)], {
					start: 10,
					total: 40,
					has_more: true,
				})
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(rowEls(wrapper)).toHaveLength(25);
		// The cursor moved by the 20 rows the server sent, not the 15 that were new.
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage.mock.calls[2][0].start).toBe(30);
	});
});

describe("TriggerDetail Recent activity: counts and the end of the list", () => {
	it("a manager sees Showing 10 of 37, then 30 of 37, then Showing all 37", async () => {
		serverOf(37);
		await mountDetail();
		expect(count(wrapper).text()).toBe("Showing 10 of 37");
		await click(wrapper, MOUSE);
		expect(count(wrapper).text()).toBe("Showing 30 of 37");
		await click(wrapper, MOUSE);
		expect(count(wrapper).text()).toBe("Showing all 37");
		expect(rowEls(wrapper)).toHaveLength(37);
		expect(moreBtn(wrapper)).toBeUndefined();
	});

	it("the count line is a polite live region that can take focus", async () => {
		serverOf(37);
		await mountDetail();
		expect(count(wrapper).attributes("aria-live")).toBe("polite");
		expect(count(wrapper).attributes("tabindex")).toBe("-1");
	});

	it("a non-manager sees Showing 10 so far; an empty page with more ends the list", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(
				answer(rowsOf(0, 10), { start: 0, total: 0, has_more: true, approximate: true })
			)
			.mockResolvedValueOnce(
				answer([], { start: 10, total: 0, has_more: true, approximate: true })
			);
		await mountDetail();
		expect(count(wrapper).text()).toBe("Showing 10 so far");
		expect(moreBtn(wrapper)).toBeDefined();
		await click(wrapper, MOUSE);
		expect(moreBtn(wrapper)).toBeUndefined();
		expect(rowEls(wrapper)).toHaveLength(10);
		expect(block(wrapper).text()).toContain("Older runs are not listed here.");
	});

	it("an approximate page that is short but has more ends the list at once (scan limit)", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(
				answer(rowsOf(0, 10), { start: 0, has_more: true, approximate: true })
			)
			.mockResolvedValueOnce(
				answer(rowsOf(10, 7), { start: 10, has_more: true, approximate: true })
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(rowEls(wrapper)).toHaveLength(17);
		expect(moreBtn(wrapper)).toBeUndefined();
		expect(block(wrapper).text()).toContain("Older runs are not listed here.");
		// Only two requests were made: the user never clicks for nothing.
		expect(apiTr.listActivityPage).toHaveBeenCalledTimes(2);
	});

	it("an empty page with has_more false says nothing about older runs", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(
				answer(rowsOf(0, 10), { start: 0, has_more: true, approximate: true })
			)
			.mockResolvedValueOnce(answer([], { start: 10, has_more: false, approximate: true }));
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(moreBtn(wrapper)).toBeUndefined();
		expect(block(wrapper).text()).not.toContain("Older runs are not listed here.");
		expect(count(wrapper).text()).toBe("Showing all 10");
	});

	it("a manager's empty page ends the list without the non-manager message", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 30, has_more: true }))
			.mockResolvedValueOnce(answer([], { start: 10, total: 30, has_more: true }));
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(moreBtn(wrapper)).toBeUndefined();
		expect(block(wrapper).text()).not.toContain("Older runs are not listed here.");
	});

	it("a non-manager first page that is empty but has more shows the end message, not No activity yet", async () => {
		apiTr.listActivityPage.mockResolvedValue(
			answer([], { start: 0, has_more: true, approximate: true })
		);
		await mountDetail();
		expect(block(wrapper).text()).toContain("Older runs are not listed here.");
		expect(block(wrapper).text()).not.toContain("No activity yet");
		expect(moreBtn(wrapper)).toBeUndefined();
	});

	it("a manager at the end of the list sees Showing all n, even if the total grew", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 12, has_more: true }))
			.mockResolvedValueOnce(
				answer(rowsOf(10, 2), { start: 10, total: 15, has_more: false })
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(moreBtn(wrapper)).toBeUndefined();
		expect(count(wrapper).text()).toBe("Showing all 12");
	});
});

describe("TriggerDetail Recent activity: a page that fails", () => {
	it("keeps the rows, says so in an alert, and Try again works", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockRejectedValueOnce(new Error("Server said no"))
			.mockResolvedValueOnce(
				answer(rowsOf(10, 20), { start: 10, total: 40, has_more: true })
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(rowEls(wrapper)).toHaveLength(10);
		expect(moreBtn(wrapper).attributes("data-label")).toBe("Try again");
		expect(alertText(wrapper)).toEqual(["Server said no"]);
		expect(warn).toHaveBeenCalledTimes(1);
		await click(wrapper, MOUSE);
		// The retry asked for the same page.
		expect(apiTr.listActivityPage.mock.calls[2][0].start).toBe(10);
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(alertText(wrapper)).toEqual([]);
		expect(moreBtn(wrapper).attributes("data-label")).toBe("Load more");
	});

	it("a repeated failure of Load more is announced again (the alert is re-created)", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockRejectedValueOnce(new Error("Server said no"))
			.mockRejectedValueOnce(new Error("Server said no"));
		await mountDetail();
		await click(wrapper, MOUSE);
		const first = block(wrapper).find('[role="alert"]').element;
		await click(wrapper, MOUSE);
		const second = block(wrapper).find('[role="alert"]').element;
		expect(second).not.toBe(first);
		expect(second.textContent.trim()).toBe("Server said no");
	});
});

describe("TriggerDetail Recent activity: first load", () => {
	it("says nothing about no activity before the answer, then says it", async () => {
		const first = defer();
		apiTr.listActivityPage.mockReturnValue(first.promise);
		await mountDetail();
		expect(block(wrapper).text()).not.toContain("No activity yet");
		first.resolve(answer([], { start: 0, total: 0, has_more: false }));
		await flushPromises();
		expect(block(wrapper).text()).toContain("No activity yet");
	});

	it("a failed first load shows an error and Try again, not No activity yet", async () => {
		apiTr.listActivityPage
			.mockRejectedValueOnce(new Error("Could not reach the server"))
			.mockResolvedValueOnce(answer(rowsOf(0, 3), { start: 0, total: 3, has_more: false }));
		await mountDetail();
		expect(block(wrapper).text()).not.toContain("No activity yet");
		expect(alertText(wrapper)).toEqual(["Could not reach the server"]);
		expect(warn).toHaveBeenCalledTimes(1);
		expect(moreBtn(wrapper).attributes("data-label")).toBe("Try again");
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage.mock.calls[1][0].start).toBe(0);
		expect(apiTr.listActivityPage.mock.calls[1][0].page_length).toBe(FIRST_PAGE);
		expect(rowEls(wrapper)).toHaveLength(3);
		expect(alertText(wrapper)).toEqual([]);
	});

	it("the count line exists, empty, before the first answer", async () => {
		apiTr.listActivityPage.mockReturnValue(defer().promise);
		await mountDetail();
		expect(count(wrapper).exists()).toBe(true);
		expect(count(wrapper).attributes("aria-live")).toBe("polite");
		expect(count(wrapper).text()).toBe("");
	});

	it("says it is loading while the first page is on its way", async () => {
		const first = defer();
		apiTr.listActivityPage.mockReturnValue(first.promise);
		await mountDetail();
		expect(block(wrapper).text()).toContain("Loading activity");
		first.resolve(answer(rowsOf(0, 3), { start: 0, total: 3, has_more: false }));
		await flushPromises();
		expect(block(wrapper).text()).not.toContain("Loading activity");
	});

	it("does not say it is loading while an error is shown", async () => {
		apiTr.listActivityPage.mockRejectedValue(new Error("Could not reach the server"));
		await mountDetail();
		expect(block(wrapper).text()).not.toContain("Loading activity");
	});
});

describe("TriggerDetail Recent activity: switching trigger", () => {
	it("clears the old trigger's rows at once, and drops its late answer", async () => {
		const late = defer();
		apiTr.listActivityPage
			.mockResolvedValueOnce(
				answer(rowsOf(0, 10, "A"), { start: 0, total: 50, has_more: true })
			)
			.mockReturnValueOnce(late.promise)
			.mockResolvedValueOnce(
				answer(rowsOf(0, 4, "B"), { start: 0, total: 4, has_more: false })
			);
		await mountDetail("TRG-1");
		expect(rowEls(wrapper)).toHaveLength(10);
		// A Load more of trigger 1 is on its way ...
		press(moreBtn(wrapper).element, MOUSE);
		await flushPromises();
		// ... and the user opens trigger 2.
		let release;
		apiTr.getTrigger.mockImplementationOnce(
			() =>
				new Promise((res) => {
					release = () => res(trigger("TRG-2"));
				})
		);
		await wrapper.setProps({ id: "TRG-2" });
		await flushPromises();
		expect(rowEls(wrapper)).toHaveLength(0);
		release();
		await flushPromises();
		expect(rowEls(wrapper).map((r) => r.attributes("data-name"))).toEqual(
			rowsOf(0, 4, "B").map((r) => r.name)
		);
		// Trigger 1's late answer must not land in trigger 2's list.
		late.resolve(answer(rowsOf(10, 20, "A"), { start: 10, total: 50, has_more: true }));
		await flushPromises();
		expect(rowEls(wrapper).map((r) => r.attributes("data-name"))).toEqual(
			rowsOf(0, 4, "B").map((r) => r.name)
		);
		expect(apiTr.listActivityPage.mock.calls[2][0].filters).toEqual({ trigger: "TRG-2" });
	});

	it("clears the old rows at once even when the next trigger fails to load", async () => {
		serverOf(37);
		await mountDetail("TRG-1");
		expect(rowEls(wrapper)).toHaveLength(10);
		apiTr.getTrigger.mockRejectedValueOnce(new Error("Not found"));
		await wrapper.setProps({ id: "TRG-2" });
		await flushPromises();
		expect(rowEls(wrapper)).toHaveLength(0);
		expect(block(wrapper).text()).not.toContain("Load more");
	});

	it("a slow getTrigger of the trigger left does not load the activity again", async () => {
		const slowTrigger = defer();
		apiTr.getTrigger.mockImplementation(() => slowTrigger.promise);
		apiTr.getTriggersCaps.mockResolvedValue({ can_manage: true });
		apiTr.listActivityPage.mockResolvedValue(
			answer(rowsOf(0, 4, "B"), { start: 0, total: 4, has_more: false })
		);
		wrapper = mount(TriggerDetail, {
			props: { id: "TRG-1", isNew: false },
			attachTo: document.body,
			global: { provide: { $socket: null } },
		});
		await flushPromises();
		apiTr.getTrigger.mockImplementation(async (name) => trigger(name));
		await wrapper.setProps({ id: "TRG-2" });
		await flushPromises();
		expect(apiTr.listActivityPage).toHaveBeenCalledTimes(1);
		slowTrigger.resolve(trigger("TRG-1"));
		await flushPromises();
		expect(apiTr.listActivityPage).toHaveBeenCalledTimes(1);
		expect(apiTr.listActivityPage.mock.calls[0][0].filters).toEqual({ trigger: "TRG-2" });
	});

	it("a slow getTrigger does not load activity once the page became New", async () => {
		const slowTrigger = defer();
		apiTr.getTrigger.mockImplementation(() => slowTrigger.promise);
		apiTr.getTriggersCaps.mockResolvedValue({ can_manage: true });
		wrapper = mount(TriggerDetail, {
			props: { id: "TRG-1", isNew: false },
			attachTo: document.body,
			global: { provide: { $socket: null } },
		});
		await flushPromises();
		await wrapper.setProps({ isNew: true });
		slowTrigger.resolve(trigger("TRG-1"));
		await flushPromises();
		expect(apiTr.listActivityPage).not.toHaveBeenCalled();
	});
});

describe("TriggerDetail Recent activity: saving", () => {
	it("does not reload or collapse the list", async () => {
		serverOf(37);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(rowEls(wrapper)).toHaveLength(30);
		const callsBefore = apiTr.listActivityPage.mock.calls.length;
		apiTr.updateTrigger.mockResolvedValue({ ...trigger(), trigger_name: "Renamed" });
		const name = wrapper.find('input[aria-label="Name"]');
		await name.setValue("Renamed");
		const save = wrapper.findAll("button").find((b) => b.attributes("data-label") === "Save");
		await save.trigger("click");
		await flushPromises();
		expect(apiTr.updateTrigger).toHaveBeenCalledTimes(1);
		expect(apiTr.listActivityPage).toHaveBeenCalledTimes(callsBefore);
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(count(wrapper).text()).toBe("Showing 30 of 37");
	});
});

describe("TriggerDetail Recent activity: focus", () => {
	it("a row, where focus lands after Load more, has the inset focus ring", async () => {
		serverOf(37);
		await mountDetail();
		const cls = rowEls(wrapper)[0].attributes("class");
		for (const c of [
			"focus-visible:outline-none",
			"focus-visible:ring-2",
			"focus-visible:ring-inset",
			"focus-visible:ring-outline-gray-3",
		])
			expect(cls).toContain(c);
	});

	it("a mouse click never moves focus to a row", async () => {
		serverOf(37);
		await mountDetail();
		const btn = moreBtn(wrapper).element;
		btn.focus();
		await click(wrapper, MOUSE);
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(document.activeElement).not.toBe(rowEls(wrapper)[10].element);
		expect(rowEls(wrapper).map((r) => r.element)).not.toContain(document.activeElement);
	});

	it("a key press moves focus to the first new row", async () => {
		serverOf(37);
		await mountDetail();
		moreBtn(wrapper).element.focus();
		await click(wrapper, KEY);
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(document.activeElement).toBe(rowEls(wrapper)[10].element);
	});

	it("when the button goes away with no new rows, focus goes to the count line", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 30, has_more: true }))
			.mockResolvedValueOnce(answer([], { start: 10, total: 30, has_more: true }));
		await mountDetail();
		moreBtn(wrapper).element.focus();
		await click(wrapper, KEY);
		expect(moreBtn(wrapper)).toBeUndefined();
		expect(document.activeElement).toBe(count(wrapper).element);
	});

	it("a pointer used during the load cancels the move", async () => {
		const slow = defer();
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockReturnValueOnce(slow.promise);
		await mountDetail();
		moreBtn(wrapper).element.focus();
		press(moreBtn(wrapper).element, KEY);
		await flushPromises();
		// The user clicks somewhere else while the page loads.
		document.dispatchEvent(new Event("pointerdown", { bubbles: true }));
		slow.resolve(answer(rowsOf(10, 20), { start: 10, total: 40, has_more: true }));
		await flushPromises();
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(rowEls(wrapper).map((r) => r.element)).not.toContain(document.activeElement);
	});

	it("the count line keeps a visible focus outline", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 30, has_more: true }))
			.mockResolvedValueOnce(answer([], { start: 10, total: 30, has_more: true }));
		await mountDetail();
		await click(wrapper, MOUSE);
		const cls = count(wrapper).attributes("class");
		expect(cls).toContain("focus-visible:outline");
		expect(cls).not.toContain("outline-none");
	});

	it("the first-load error line keeps a visible focus outline", async () => {
		apiTr.listActivityPage.mockRejectedValue(new Error("Could not reach the server"));
		await mountDetail();
		const cls = block(wrapper).find('[data-testid="recent-error"]').attributes("class");
		expect(cls).toContain("focus-visible:outline");
		expect(cls).not.toContain("outline-none");
	});
});

describe("TriggerDetail Recent activity: first-load Try again, by key", () => {
	const errorLine = (w) => block(w).find('[data-testid="recent-error"]');

	it("a key press focuses the first row after a successful retry", async () => {
		apiTr.listActivityPage
			.mockRejectedValueOnce(new Error("Could not reach the server"))
			.mockResolvedValueOnce(answer(rowsOf(0, 3), { start: 0, total: 3, has_more: false }));
		await mountDetail();
		moreBtn(wrapper).element.focus();
		await click(wrapper, KEY);
		expect(rowEls(wrapper)).toHaveLength(3);
		expect(document.activeElement).toBe(rowEls(wrapper)[0].element);
	});

	it("a key press focuses the error line after a repeated failure", async () => {
		apiTr.listActivityPage.mockRejectedValue(new Error("Could not reach the server"));
		await mountDetail();
		moreBtn(wrapper).element.focus();
		await click(wrapper, KEY);
		expect(errorLine(wrapper).attributes("tabindex")).toBe("-1");
		expect(document.activeElement).toBe(errorLine(wrapper).element);
	});

	it("a mouse click moves no focus", async () => {
		apiTr.listActivityPage
			.mockRejectedValueOnce(new Error("Could not reach the server"))
			.mockResolvedValueOnce(answer(rowsOf(0, 3), { start: 0, total: 3, has_more: false }));
		await mountDetail();
		moreBtn(wrapper).element.focus();
		await click(wrapper, MOUSE);
		expect(rowEls(wrapper)).toHaveLength(3);
		expect(rowEls(wrapper).map((r) => r.element)).not.toContain(document.activeElement);
	});

	it("a key retry that finds no runs focuses the No activity yet line", async () => {
		apiTr.listActivityPage
			.mockRejectedValueOnce(new Error("Could not reach the server"))
			.mockResolvedValueOnce(answer([], { start: 0, total: 0, has_more: false }));
		await mountDetail();
		moreBtn(wrapper).element.focus();
		await click(wrapper, KEY);
		expect(emptyLine(wrapper).exists()).toBe(true);
		expect(emptyLine(wrapper).attributes("tabindex")).toBe("-1");
		expect(emptyLine(wrapper).attributes("class")).toContain("focus-visible:outline");
		expect(document.activeElement).toBe(emptyLine(wrapper).element);
	});
});

describe("TriggerDetail Recent activity: focus is left alone", () => {
	it("when the user moved focus during the load", async () => {
		const slow = defer();
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockReturnValueOnce(slow.promise);
		await mountDetail();
		moreBtn(wrapper).element.focus();
		press(moreBtn(wrapper).element, KEY);
		await flushPromises();
		const other = document.createElement("input");
		document.body.appendChild(other);
		other.focus();
		slow.resolve(answer(rowsOf(10, 20), { start: 10, total: 40, has_more: true }));
		await flushPromises();
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(document.activeElement).toBe(other);
	});

	it("when a dialog is open over the page", async () => {
		const slow = defer();
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockReturnValueOnce(slow.promise);
		await mountDetail();
		moreBtn(wrapper).element.focus();
		press(moreBtn(wrapper).element, KEY);
		await flushPromises();
		await rowEls(wrapper)[0].trigger("click"); // opens the activity dialog
		expect(wrapper.findComponent({ name: "ActivityDetailDialog" }).props("modelValue")).toBe(
			true
		);
		slow.resolve(answer(rowsOf(10, 20), { start: 10, total: 40, has_more: true }));
		await flushPromises();
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(rowEls(wrapper).map((r) => r.element)).not.toContain(document.activeElement);
	});

	it("when the user switched trigger during the load", async () => {
		const slow = defer();
		apiTr.listActivityPage
			.mockResolvedValueOnce(
				answer(rowsOf(0, 10, "A"), { start: 0, total: 40, has_more: true })
			)
			.mockReturnValueOnce(slow.promise)
			.mockResolvedValueOnce(
				answer(rowsOf(0, 15, "B"), { start: 0, total: 15, has_more: false })
			);
		await mountDetail("TRG-1");
		moreBtn(wrapper).element.focus();
		press(moreBtn(wrapper).element, KEY);
		await flushPromises();
		await wrapper.setProps({ id: "TRG-2" });
		await flushPromises();
		expect(rowEls(wrapper)).toHaveLength(15);
		slow.resolve(answer(rowsOf(10, 20, "A"), { start: 10, total: 40, has_more: true }));
		await flushPromises();
		expect(rowEls(wrapper)).toHaveLength(15);
		expect(rowEls(wrapper).map((r) => r.element)).not.toContain(document.activeElement);
	});

	it("focus stays on the button when every row of the page was shown already", async () => {
		// Every page is rows already on screen (PM-1: the click asks up to 3 more times).
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 400, has_more: true }))
			.mockResolvedValue(answer(rowsOf(0, 10), { start: 10, total: 400, has_more: true }));
		await mountDetail();
		const btn = moreBtn(wrapper).element;
		btn.focus();
		await click(wrapper, KEY);
		expect(rowEls(wrapper)).toHaveLength(10);
		expect(document.activeElement).toBe(btn);
	});
});

describe("TriggerDetail Recent activity: answers that are nobody's", () => {
	// The component's own state after it is gone: rows unchanged, no error recorded,
	// nothing logged. (The DOM is gone, so only the state can tell.)
	for (const kind of ["resolving", "rejecting"]) {
		it(`a late ${kind} answer after the page is gone changes nothing`, async () => {
			const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
			const warnLog = warn;
			const slow = defer();
			apiTr.listActivityPage
				.mockResolvedValueOnce(
					answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true })
				)
				.mockReturnValueOnce(slow.promise);
			await mountDetail();
			const vm = wrapper.vm;
			press(moreBtn(wrapper).element, MOUSE);
			await flushPromises();
			wrapper.unmount();
			wrapper = null;
			errorLog.mockClear();
			warnLog.mockClear();
			if (kind === "resolving")
				slow.resolve(answer(rowsOf(10, 20), { start: 10, total: 40, has_more: true }));
			else slow.reject(new Error("Server said no"));
			await flushPromises();
			expect(vm.recentRows).toBeDefined();
			expect(vm.recentRows).toHaveLength(10);
			expect(vm.recentError).toBe("");
			expect(vm.recentMoreFailed).toBe(false);
			expect(errorLog).not.toHaveBeenCalled();
			expect(warnLog).not.toHaveBeenCalled();
			errorLog.mockRestore();
		});
	}

	it("an answer from an older load of the SAME trigger is dropped", async () => {
		const slow = defer();
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockReturnValueOnce(slow.promise)
			.mockResolvedValueOnce(
				answer(rowsOf(0, 3, "R"), { start: 0, total: 3, has_more: false })
			);
		await mountDetail("TRG-1");
		press(moreBtn(wrapper).element, MOUSE);
		await flushPromises();
		// The same trigger is loaded again (new then saved again): the counter moves, the id does not.
		await wrapper.setProps({ isNew: true });
		await wrapper.setProps({ isNew: false });
		await flushPromises();
		expect(rowEls(wrapper).map((r) => r.attributes("data-name"))).toEqual(
			rowsOf(0, 3, "R").map((r) => r.name)
		);
		slow.resolve(answer(rowsOf(10, 20), { start: 10, total: 40, has_more: true }));
		await flushPromises();
		expect(rowEls(wrapper).map((r) => r.attributes("data-name"))).toEqual(
			rowsOf(0, 3, "R").map((r) => r.name)
		);
	});
});

describe("TriggerDetail Recent activity: a manager's page comes back empty", () => {
	// The count said more, but the rows were deleted before the page was read. The
	// empty page at the cursor means nothing lies past it, so the list is whole:
	// "Showing all n" is true, and the stale total (30) is not.
	it("ends the list as Showing all n, with no button and no stale total", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 30, has_more: true }))
			.mockResolvedValueOnce(answer([], { start: 10, total: 30, has_more: true }));
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(moreBtn(wrapper)).toBeUndefined();
		expect(count(wrapper).text()).toBe("Showing all 10");
		expect(block(wrapper).text()).not.toContain("Older runs are not listed here.");
		expect(rowEls(wrapper)).toHaveLength(10);
	});

	it("an empty first page with has_more says No activity yet, not a dead end", async () => {
		apiTr.listActivityPage.mockResolvedValue(
			answer([], { start: 0, total: 5, has_more: true })
		);
		await mountDetail();
		expect(moreBtn(wrapper)).toBeUndefined();
		expect(emptyLine(wrapper).exists()).toBe(true);
	});
});

describe("TriggerDetail Recent activity: a failure is logged", () => {
	it("a failed first load warns with the trigger and the start", async () => {
		const err = new Error("Could not reach the server");
		apiTr.listActivityPage.mockRejectedValue(err);
		await mountDetail("TRG-7");
		expect(warn).toHaveBeenCalledTimes(1);
		expect(warn).toHaveBeenCalledWith(
			"[TriggerDetail] activity load failed",
			{ trigger: "TRG-7", start: 0 },
			err
		);
	});

	it("a failed Load more warns with the start it asked for", async () => {
		const err = new Error("Server said no");
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockRejectedValueOnce(err);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(warn).toHaveBeenCalledTimes(1);
		expect(warn).toHaveBeenCalledWith(
			"[TriggerDetail] activity load failed",
			{ trigger: "TRG-1", start: 10 },
			err
		);
	});

	it("a failure that arrives after a newer load took over is not logged", async () => {
		const slow = defer();
		apiTr.listActivityPage
			.mockReturnValueOnce(slow.promise)
			.mockResolvedValueOnce(answer(rowsOf(0, 3), { start: 0, total: 3, has_more: false }));
		await mountDetail("TRG-1");
		await wrapper.setProps({ isNew: true });
		await wrapper.setProps({ isNew: false });
		await flushPromises();
		slow.reject(new Error("Late failure"));
		await flushPromises();
		expect(warn).not.toHaveBeenCalled();
	});
});

describe("TriggerDetail Recent activity: odd answers", () => {
	it("an answer without start: the next request uses the start sent plus the rows returned", async () => {
		const noStart = answer(rowsOf(0, 10), { total: 40, has_more: true });
		delete noStart.start;
		apiTr.listActivityPage
			.mockResolvedValueOnce(noStart)
			.mockResolvedValueOnce(
				answer(rowsOf(10, 20), { start: 10, total: 40, has_more: true })
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage.mock.calls[1][0].start).toBe(10);
		expect(rowEls(wrapper)).toHaveLength(30);
	});

	// PM-3 (replaces the PF-4 expectations): a null / non-object answer is a failed
	// load, not "No activity yet" and not "the list ended".
	for (const bad of [null, undefined, "oops", 42]) {
		it(`a ${
			bad === null ? "null" : typeof bad
		} first answer is a failed load with Try again`, async () => {
			apiTr.listActivityPage
				.mockResolvedValueOnce(bad)
				.mockResolvedValueOnce(
					answer(rowsOf(0, 3), { start: 0, total: 3, has_more: false })
				);
			await mountDetail();
			expect(rowEls(wrapper)).toHaveLength(0);
			expect(emptyLine(wrapper).exists()).toBe(false);
			expect(block(wrapper).text()).not.toContain("No activity yet");
			expect(alertText(wrapper)).toEqual(["Could not load the activity. Try again."]);
			expect(moreBtn(wrapper).attributes("data-label")).toBe("Try again");
			expect(warn).toHaveBeenCalledTimes(1);
			await click(wrapper, MOUSE);
			expect(rowEls(wrapper)).toHaveLength(3);
			expect(alertText(wrapper)).toEqual([]);
		});
	}

	it("a null Load more answer keeps the rows and is a failed page with Try again", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockResolvedValueOnce(null)
			.mockResolvedValueOnce(
				answer(rowsOf(10, 20), { start: 10, total: 40, has_more: true })
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(rowEls(wrapper)).toHaveLength(10);
		expect(moreBtn(wrapper).attributes("data-label")).toBe("Try again");
		expect(count(wrapper).text()).toBe("Showing 10 of 40");
		expect(alertText(wrapper)).toEqual(["Could not load more activity. Try again."]);
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage.mock.calls[2][0].start).toBe(10);
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(alertText(wrapper)).toEqual([]);
	});

	// PM-4: has_more counts only when it is the boolean true.
	it("a has_more that is not the boolean true counts as no more", async () => {
		apiTr.listActivityPage.mockResolvedValue(
			answer(rowsOf(0, 10), { start: 0, total: 40, has_more: 1 })
		);
		await mountDetail();
		expect(moreBtn(wrapper)).toBeUndefined();
		expect(count(wrapper).text()).toBe("Showing all 10");
	});
});

describe("TriggerDetail Recent activity: a page of rows already shown (PM-1)", () => {
	const dupPage = (start, total = 400) =>
		answer(rowsOf(0, 10), { start, total, has_more: true });

	it("asks for the next page at once, in the same click", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockResolvedValueOnce(dupPage(10, 40))
			.mockResolvedValueOnce(
				answer(rowsOf(10, 20), { start: 20, total: 40, has_more: true })
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		// The cursor moved by the rows the server sent: 10, then 20.
		expect(apiTr.listActivityPage.mock.calls.map((c) => c[0].start)).toEqual([0, 10, 20]);
		expect(apiTr.listActivityPage.mock.calls[2][0].page_length).toBe(NEXT_PAGE);
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(count(wrapper).text()).toBe("Showing 30 of 40");
	});

	it("asks at most 3 extra times per click, then stops with the button still there", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 400, has_more: true }))
			.mockImplementation(async (p) => dupPage(p.start));
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage.mock.calls.map((c) => c[0].start)).toEqual([
			0, 10, 20, 30, 40,
		]);
		expect(rowEls(wrapper)).toHaveLength(10);
		expect(count(wrapper).text()).toBe("Showing 10 of 400");
		expect(moreBtn(wrapper).attributes("data-label")).toBe("Load more");
		// The next click goes on from the cursor, with the same bound.
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage.mock.calls.map((c) => c[0].start).slice(5)).toEqual([
			50, 60, 70, 80,
		]);
	});

	it("does not ask again when the page added a row", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 400, has_more: true }))
			.mockResolvedValueOnce(
				answer([...rowsOf(5, 5), row(10)], { start: 10, total: 400, has_more: true })
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage).toHaveBeenCalledTimes(2);
		expect(rowEls(wrapper)).toHaveLength(11);
	});

	it("keeps the button disabled and the count line unchanged until the last page is in", async () => {
		const seen = [];
		const second = defer();
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockResolvedValueOnce(dupPage(10, 41))
			.mockReturnValueOnce(second.promise);
		await mountDetail();
		const observer = new MutationObserver(() => seen.push(count(wrapper).text()));
		observer.observe(count(wrapper).element, {
			childList: true,
			subtree: true,
			characterData: true,
		});
		press(moreBtn(wrapper).element, MOUSE);
		await flushPromises();
		// The all-duplicate page is in, the next one is still on its way.
		expect(apiTr.listActivityPage).toHaveBeenCalledTimes(3);
		expect(moreBtn(wrapper).element.disabled).toBe(true);
		expect(count(wrapper).text()).toBe("Showing 10 of 40");
		second.resolve(answer(rowsOf(10, 20), { start: 20, total: 45, has_more: true }));
		await flushPromises();
		observer.disconnect();
		// The live region changed once, to the final text.
		expect(seen).toEqual(["Showing 30 of 45"]);
		expect(moreBtn(wrapper).element.disabled).toBe(false);
	});

	it("a failure on an extra page keeps the rows, shows Try again, and stops asking", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 400, has_more: true }))
			.mockResolvedValueOnce(dupPage(10))
			.mockRejectedValueOnce(new Error("Server said no"))
			.mockResolvedValueOnce(
				answer(rowsOf(10, 20), { start: 20, total: 400, has_more: true })
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage).toHaveBeenCalledTimes(3);
		expect(rowEls(wrapper)).toHaveLength(10);
		expect(alertText(wrapper)).toEqual(["Server said no"]);
		expect(moreBtn(wrapper).attributes("data-label")).toBe("Try again");
		await click(wrapper, MOUSE);
		// The cursor kept what the all-duplicate page had moved it to.
		expect(apiTr.listActivityPage.mock.calls[3][0].start).toBe(20);
		expect(rowEls(wrapper)).toHaveLength(30);
	});

	it("an all-duplicate page followed by an empty one ends the list as usual", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 30, has_more: true }))
			.mockResolvedValueOnce(dupPage(10, 30))
			.mockResolvedValueOnce(answer([], { start: 20, total: 30, has_more: true }));
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(moreBtn(wrapper)).toBeUndefined();
		expect(count(wrapper).text()).toBe("Showing all 10");
	});

	it("a key press still moves focus to the first NEW row the click brought", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockResolvedValueOnce(dupPage(10, 40))
			.mockResolvedValueOnce(
				answer(rowsOf(10, 20), { start: 20, total: 40, has_more: true })
			);
		await mountDetail();
		moreBtn(wrapper).element.focus();
		await click(wrapper, KEY);
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(document.activeElement).toBe(rowEls(wrapper)[10].element);
	});

	it("a switch of trigger during the extra pages drops them", async () => {
		const slow = defer();
		apiTr.listActivityPage
			.mockResolvedValueOnce(
				answer(rowsOf(0, 10, "A"), { start: 0, total: 400, has_more: true })
			)
			.mockResolvedValueOnce(
				answer(rowsOf(0, 10, "A"), { start: 10, total: 400, has_more: true })
			)
			.mockReturnValueOnce(slow.promise)
			.mockResolvedValueOnce(
				answer(rowsOf(0, 4, "B"), { start: 0, total: 4, has_more: false })
			);
		await mountDetail("TRG-1");
		press(moreBtn(wrapper).element, MOUSE);
		await flushPromises();
		await wrapper.setProps({ id: "TRG-2" });
		await flushPromises();
		slow.resolve(answer(rowsOf(10, 20, "A"), { start: 20, total: 400, has_more: true }));
		await flushPromises();
		expect(rowEls(wrapper).map((r) => r.attributes("data-name"))).toEqual(
			rowsOf(0, 4, "B").map((r) => r.name)
		);
		// No further request was made for the old trigger.
		expect(apiTr.listActivityPage).toHaveBeenCalledTimes(4);
	});
});

describe("TriggerDetail Recent activity: a request that takes too long (PM-2)", () => {
	const TOO_LONG = "Loading the activity took too long. Try again.";
	beforeEach(() => {
		// Only the timers: flushPromises waits on setImmediate.
		vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
	});
	afterEach(() => {
		vi.useRealTimers();
	});
	const tick = async (ms) => {
		await vi.advanceTimersByTimeAsync(ms);
		await flushPromises();
	};

	it("the first load fails after 30 s with a message and Try again, not before", async () => {
		apiTr.listActivityPage.mockReturnValue(defer().promise);
		await mountDetail();
		await tick(29999);
		expect(alertText(wrapper)).toEqual([]);
		expect(block(wrapper).text()).toContain("Loading activity");
		await tick(1);
		expect(alertText(wrapper)).toEqual([TOO_LONG]);
		expect(block(wrapper).find('[data-testid="recent-error"]').exists()).toBe(true);
		expect(block(wrapper).text()).not.toContain("Loading activity");
		expect(block(wrapper).text()).not.toContain("No activity yet");
		expect(moreBtn(wrapper).attributes("data-label")).toBe("Try again");
		expect(moreBtn(wrapper).element.disabled).toBe(false);
		expect(warn).toHaveBeenCalledTimes(1);
	});

	it("its late answer is dropped, and Try again works", async () => {
		const late = defer();
		apiTr.listActivityPage
			.mockReturnValueOnce(late.promise)
			.mockResolvedValueOnce(
				answer(rowsOf(0, 3, "R"), { start: 0, total: 3, has_more: false })
			);
		await mountDetail();
		await tick(30000);
		late.resolve(answer(rowsOf(0, 10, "LATE"), { start: 0, total: 10, has_more: false }));
		await flushPromises();
		expect(rowEls(wrapper)).toHaveLength(0);
		expect(alertText(wrapper)).toEqual([TOO_LONG]);
		await click(wrapper, MOUSE);
		expect(rowEls(wrapper).map((r) => r.attributes("data-name"))).toEqual(
			rowsOf(0, 3, "R").map((r) => r.name)
		);
		expect(alertText(wrapper)).toEqual([]);
	});

	it("a late answer after the retry has started does not replace the retry's", async () => {
		const late = defer();
		const retry = defer();
		apiTr.listActivityPage
			.mockReturnValueOnce(late.promise)
			.mockReturnValueOnce(retry.promise);
		await mountDetail();
		await tick(30000);
		press(moreBtn(wrapper).element, MOUSE);
		await flushPromises();
		late.resolve(answer(rowsOf(0, 10, "LATE"), { start: 0, total: 10, has_more: false }));
		await flushPromises();
		expect(rowEls(wrapper)).toHaveLength(0);
		retry.resolve(answer(rowsOf(0, 3, "R"), { start: 0, total: 3, has_more: false }));
		await flushPromises();
		expect(rowEls(wrapper).map((r) => r.attributes("data-name"))).toEqual(
			rowsOf(0, 3, "R").map((r) => r.name)
		);
	});

	it("a Load more fails after 30 s beside the button; the rows stay; its late answer is dropped", async () => {
		const late = defer();
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockReturnValueOnce(late.promise)
			.mockResolvedValueOnce(
				answer(rowsOf(10, 20), { start: 10, total: 40, has_more: true })
			);
		await mountDetail();
		press(moreBtn(wrapper).element, MOUSE);
		await flushPromises();
		expect(moreBtn(wrapper).element.disabled).toBe(true);
		await tick(30000);
		expect(alertText(wrapper)).toEqual([TOO_LONG]);
		expect(moreBtn(wrapper).attributes("data-label")).toBe("Try again");
		expect(moreBtn(wrapper).element.disabled).toBe(false);
		expect(rowEls(wrapper)).toHaveLength(10);
		late.resolve(answer(rowsOf(10, 20, "LATE"), { start: 10, total: 40, has_more: true }));
		await flushPromises();
		expect(rowEls(wrapper)).toHaveLength(10);
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage.mock.calls[2][0].start).toBe(10);
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(alertText(wrapper)).toEqual([]);
	});

	it("each request of an all-duplicate chain has its own 30 s", async () => {
		const third = defer();
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 400, has_more: true }))
			.mockResolvedValueOnce(
				answer(rowsOf(0, 10), { start: 10, total: 400, has_more: true })
			)
			.mockReturnValueOnce(third.promise);
		await mountDetail();
		press(moreBtn(wrapper).element, MOUSE);
		await tick(0);
		await tick(29000);
		expect(alertText(wrapper)).toEqual([]);
		await tick(1000);
		expect(alertText(wrapper)).toEqual([TOO_LONG]);
	});

	it("a quick answer clears the timer", async () => {
		serverOf(37);
		await mountDetail();
		expect(vi.getTimerCount()).toBe(0);
		await click(wrapper, MOUSE);
		expect(vi.getTimerCount()).toBe(0);
	});

	it("a failed request clears the timer", async () => {
		apiTr.listActivityPage.mockRejectedValue(new Error("Server said no"));
		await mountDetail();
		expect(vi.getTimerCount()).toBe(0);
	});

	it("the timer is cleared when the page goes away, and nothing is shown after", async () => {
		apiTr.listActivityPage.mockReturnValue(defer().promise);
		await mountDetail();
		expect(vi.getTimerCount()).toBe(1);
		const vm = wrapper.vm;
		wrapper.unmount();
		wrapper = null;
		expect(vi.getTimerCount()).toBe(0);
		await tick(60000);
		expect(vm.recentError).toBe("");
		expect(warn).not.toHaveBeenCalled();
	});
});

describe("TriggerDetail Recent activity: rows without a name (PM-5)", () => {
	const noName = (tag) => ({ status: "Success", summary: `Run ${tag}`, creation: "2026-01-01" });
	const duplicateKeyWarnings = () =>
		warn.mock.calls.filter((c) => String(c[0]).includes("Duplicate keys"));

	it("a Load more page keeps every row that has no name, and shows no duplicate-key warning", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockResolvedValueOnce(
				answer([noName("x"), noName("y"), row(10)], {
					start: 10,
					total: 40,
					has_more: true,
				})
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(rowEls(wrapper)).toHaveLength(13);
		expect(block(wrapper).text()).toContain("Run x");
		expect(block(wrapper).text()).toContain("Run y");
		expect(duplicateKeyWarnings()).toEqual([]);
	});

	it("a named row is still shown once next to nameless ones", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockResolvedValueOnce(
				answer([noName("x"), row(3), noName("y")], {
					start: 10,
					total: 40,
					has_more: true,
				})
			);
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(rowEls(wrapper)).toHaveLength(12);
	});

	it("nameless rows on the first page all show", async () => {
		apiTr.listActivityPage.mockResolvedValue(
			answer([noName("x"), noName("y")], { start: 0, total: 2, has_more: false })
		);
		await mountDetail();
		expect(rowEls(wrapper)).toHaveLength(2);
		expect(duplicateKeyWarnings()).toEqual([]);
	});
});

describe("TriggerDetail Recent activity: an empty list that a non-manager sees (PM-6)", () => {
	it("says No runs you can view yet for an approximate, complete, empty list", async () => {
		apiTr.listActivityPage.mockResolvedValue(
			answer([], { start: 0, total: 0, has_more: false, approximate: true })
		);
		await mountDetail();
		expect(emptyLine(wrapper).exists()).toBe(true);
		expect(emptyLine(wrapper).text()).toBe("No runs you can view yet.");
		expect(block(wrapper).text()).not.toContain("No activity yet");
		expect(emptyLine(wrapper).attributes("tabindex")).toBe("-1");
	});

	it("a manager keeps No activity yet", async () => {
		apiTr.listActivityPage.mockResolvedValue(
			answer([], { start: 0, total: 0, has_more: false })
		);
		await mountDetail();
		expect(emptyLine(wrapper).text()).toBe(
			"No activity yet - runs appear here when the trigger fires."
		);
	});

	it("a key retry that finds an empty non-manager list focuses that line", async () => {
		apiTr.listActivityPage
			.mockRejectedValueOnce(new Error("Could not reach the server"))
			.mockResolvedValueOnce(answer([], { start: 0, has_more: false, approximate: true }));
		await mountDetail();
		moreBtn(wrapper).element.focus();
		await click(wrapper, KEY);
		expect(emptyLine(wrapper).text()).toBe("No runs you can view yet.");
		expect(document.activeElement).toBe(emptyLine(wrapper).element);
	});
});

describe("TriggerDetail Recent activity: the loading line (PM-7)", () => {
	it("is a status region", async () => {
		apiTr.listActivityPage.mockReturnValue(defer().promise);
		await mountDetail();
		const loading = block(wrapper)
			.findAll("div")
			.find((d) => d.text().startsWith("Loading activity"));
		expect(loading.attributes("role")).toBe("status");
	});
});

describe("TriggerDetail: the form follows the newest init only (PM-8)", () => {
	const nameInput = (w) => w.find('input[aria-label="Name"]');
	const docPageLoading = (w) => w.findComponent({ name: "DocPage" }).props("loading");
	function mountSlow(slow) {
		apiTr.getTrigger.mockImplementation(() => slow.promise);
		apiTr.getTriggersCaps.mockResolvedValue({ can_manage: true });
		apiTr.listActivityPage.mockResolvedValue(
			answer(rowsOf(0, 2, "B"), { start: 0, total: 2, has_more: false })
		);
		wrapper = mount(TriggerDetail, {
			props: { id: "TRG-1", isNew: false },
			attachTo: document.body,
			global: { provide: { $socket: null } },
		});
	}

	it("trigger A answers late: the form stays B's and the page is not loading", async () => {
		const slow = defer();
		mountSlow(slow);
		await flushPromises();
		expect(docPageLoading(wrapper)).toBe(true);
		apiTr.getTrigger.mockImplementation(async (name) => trigger(name));
		await wrapper.setProps({ id: "TRG-2" });
		await flushPromises();
		expect(nameInput(wrapper).element.value).toBe("Big invoice TRG-2");
		expect(docPageLoading(wrapper)).toBe(false);
		slow.resolve(trigger("TRG-1"));
		await flushPromises();
		expect(nameInput(wrapper).element.value).toBe("Big invoice TRG-2");
		expect(docPageLoading(wrapper)).toBe(false);
	});

	it("trigger A answers while B is still loading: A seeds nothing and loading stays", async () => {
		const slowA = defer();
		const slowB = defer();
		mountSlow(slowA);
		await flushPromises();
		apiTr.getTrigger.mockImplementation(() => slowB.promise);
		await wrapper.setProps({ id: "TRG-2" });
		await flushPromises();
		slowA.resolve(trigger("TRG-1"));
		await flushPromises();
		expect(nameInput(wrapper).element.value).toBe("");
		expect(docPageLoading(wrapper)).toBe(true);
		slowB.resolve(trigger("TRG-2"));
		await flushPromises();
		expect(nameInput(wrapper).element.value).toBe("Big invoice TRG-2");
		expect(docPageLoading(wrapper)).toBe(false);
	});

	it("A answers late after the page became New: the form stays blank, not loading", async () => {
		const slow = defer();
		mountSlow(slow);
		await flushPromises();
		await wrapper.setProps({ isNew: true });
		await flushPromises();
		expect(nameInput(wrapper).element.value).toBe("");
		expect(docPageLoading(wrapper)).toBe(false);
		slow.resolve(trigger("TRG-1"));
		await flushPromises();
		expect(nameInput(wrapper).element.value).toBe("");
		expect(wrapper.vm.form.target_doctype).toBe("");
		expect(docPageLoading(wrapper)).toBe(false);
	});

	it("A fails late after the page moved on: no error on the new page", async () => {
		const slow = defer();
		mountSlow(slow);
		await flushPromises();
		apiTr.getTrigger.mockImplementation(async (name) => trigger(name));
		await wrapper.setProps({ id: "TRG-2" });
		await flushPromises();
		slow.reject(new Error("Not found"));
		await flushPromises();
		expect(wrapper.findComponent({ name: "DocPage" }).props("error")).toBe("");
		expect(nameInput(wrapper).element.value).toBe("Big invoice TRG-2");
	});
});

// ── Intake kind ──────────────────────────────────────────────────────────────
const TODO_OPTIONS = {
	fields: [
		{
			fieldname: "description",
			label: "Description",
			fieldtype: "Text Editor",
			options: "",
			mandatory: true,
		},
		{
			fieldname: "status",
			label: "Status",
			fieldtype: "Select",
			options: "Open\nClosed",
			mandatory: false,
		},
	],
	tables: [],
};

function intakeTrigger(over = {}) {
	return {
		...trigger("TRG-9"),
		action_type: "Intake",
		doc_event: "",
		target_doctype: "",
		llm_instruction: "",
		llm_daily_cap: 100,
		intake_source: "Folder",
		intake_folder: "Home/Production Sheets",
		intake_target_doctype: "ToDo",
		intake_spec: JSON.stringify({
			fields: ["description"],
			table: null,
			match: [],
			defaults: {},
		}),
		intake_instructions: "",
		allow_incomplete_drafts: 0,
		intake_status_field: "",
		intake_link_field: "",
		...over,
	};
}

async function mountIntake(over = {}) {
	apiTr.listIntakeFolders.mockResolvedValue(["Home/Production Sheets"]);
	apiTr.getIntakeFieldOptions.mockResolvedValue(TODO_OPTIONS);
	apiTr.getTrigger.mockImplementation(async () => intakeTrigger(over));
	apiTr.getTriggersCaps.mockResolvedValue({ can_manage: true });
	wrapper = mount(TriggerDetail, {
		props: { id: "TRG-9", isNew: false },
		attachTo: document.body,
		global: { provide: { $socket: null } },
	});
	await flushPromises();
	await flushPromises();
	return wrapper;
}
const ctl = (w, label) => w.find(`input[aria-label="${label}"]`);

describe("TriggerDetail Intake kind", () => {
	it("intake kind hides event and condition", async () => {
		const w = await mountIntake();
		expect(ctl(w, "Event").exists()).toBe(false);
		expect(w.text()).not.toContain("Test condition");
		expect(ctl(w, "Instruction").exists()).toBe(false);
		expect(ctl(w, "Script").exists()).toBe(false);
		expect(ctl(w, "Name").exists()).toBe(true);
	});

	it("mandatory fields pre-checked", async () => {
		const w = await mountIntake({ intake_spec: "" });
		expect(ctl(w, "Description (required)").element.checked).toBe(true);
		expect(ctl(w, "Status").element.checked).toBe(false);
	});

	it("payload carries intake_spec json without empty defaults", async () => {
		const w = await mountIntake({
			intake_spec: JSON.stringify({
				fields: ["description"],
				table: null,
				match: [],
				defaults: { status: "" },
			}),
		});
		apiTr.updateTrigger.mockResolvedValue(intakeTrigger());
		await ctl(w, "Status").setValue(true);
		await w
			.findAll("button")
			.find((b) => b.attributes("data-label") === "Save")
			.trigger("click");
		await flushPromises();
		const [name, payload] = apiTr.updateTrigger.mock.calls[0];
		expect(name).toBe("TRG-9");
		expect(Object.keys(payload)).toEqual(["intake_spec"]);
		expect(JSON.parse(payload.intake_spec)).toEqual({
			fields: ["description", "status"],
			table: null,
			match: [],
			defaults: {},
		});
	});

	it("allow incomplete drafts label and default off", async () => {
		const w = await mountIntake();
		expect(ctl(w, "Allow incomplete drafts").element.checked).toBe(false);
		expect(w.text()).toContain(
			"Create the draft even when a mandatory field could not be read."
		);
	});
});
