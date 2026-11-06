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
		template: `<input :aria-label="label" :value="modelValue" :disabled="disabled" @input="$emit('update:modelValue', $event.target.value)" />`,
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

beforeEach(() => {
	vi.clearAllMocks();
	apiTr.listActivityPage.mockReset();
});
afterEach(() => {
	if (wrapper) wrapper.unmount();
	wrapper = null;
	document.body.innerHTML = "";
});

const block = (w) => w.find('[data-testid="recent-block"]');
const rowEls = (w) => w.findAll('[data-testid="recent-row"]');
const count = (w) => w.find('[data-testid="recent-count"]');
const moreBtn = (w) =>
	block(w)
		.findAll("button")
		.find((b) => ["Load more", "Try again"].includes(b.attributes("data-label")));
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
		await click(wrapper, MOUSE); // 20 duplicates: nothing added
		expect(rowEls(wrapper)).toHaveLength(30);
		await click(wrapper, MOUSE); // the next click goes on from 50
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
		await click(wrapper, MOUSE);
		// The retry asked for the same page.
		expect(apiTr.listActivityPage.mock.calls[2][0].start).toBe(10);
		expect(rowEls(wrapper)).toHaveLength(30);
		expect(alertText(wrapper)).toEqual([]);
		expect(moreBtn(wrapper).attributes("data-label")).toBe("Load more");
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
		expect(moreBtn(wrapper).attributes("data-label")).toBe("Try again");
		await click(wrapper, MOUSE);
		expect(apiTr.listActivityPage.mock.calls[1][0].start).toBe(0);
		expect(apiTr.listActivityPage.mock.calls[1][0].page_length).toBe(FIRST_PAGE);
		expect(rowEls(wrapper)).toHaveLength(3);
		expect(alertText(wrapper)).toEqual([]);
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
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 40, has_more: true }))
			.mockResolvedValueOnce(
				answer(rowsOf(0, 10), { start: 10, total: 40, has_more: true })
			);
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
			if (kind === "resolving")
				slow.resolve(answer(rowsOf(10, 20), { start: 10, total: 40, has_more: true }));
			else slow.reject(new Error("Server said no"));
			await flushPromises();
			expect(vm.recentRows).toHaveLength(10);
			expect(vm.recentError).toBe("");
			expect(vm.recentMoreFailed).toBe(false);
			expect(errorLog).not.toHaveBeenCalled();
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

describe("TriggerDetail Recent activity: count when a manager's page comes back empty", () => {
	it("says Showing n of total, not Showing all n", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 30, has_more: true }))
			.mockResolvedValueOnce(answer([], { start: 10, total: 30, has_more: true }));
		await mountDetail();
		await click(wrapper, MOUSE);
		expect(moreBtn(wrapper)).toBeUndefined();
		expect(count(wrapper).text()).toBe("Showing 10 of 30");
	});
});

describe("TriggerDetail Recent activity: fix wave 1", () => {
	const emptyLine = (w) => block(w).find('[data-testid="recent-empty"]');

	it("FX-2: the focus targets keep a visible focus ring", async () => {
		apiTr.listActivityPage
			.mockResolvedValueOnce(answer(rowsOf(0, 10), { start: 0, total: 30, has_more: true }))
			.mockResolvedValueOnce(answer([], { start: 10, total: 30, has_more: true }));
		await mountDetail();
		await click(wrapper, MOUSE);
		const cls = count(wrapper).attributes("class");
		expect(cls).toContain("focus-visible:outline");
		expect(cls).not.toContain("outline-none");
	});

	it("FX-2: so does the first-load error line", async () => {
		apiTr.listActivityPage.mockRejectedValue(new Error("Could not reach the server"));
		await mountDetail();
		const cls = block(wrapper).find('[data-testid="recent-error"]').attributes("class");
		expect(cls).toContain("focus-visible:outline");
		expect(cls).not.toContain("outline-none");
	});

	it("FX-3: a key retry that finds no runs focuses the No activity yet line", async () => {
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

	it("FX-4: a repeated failure of Load more is announced again (the alert is re-created)", async () => {
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

	it("FX-5: the count line exists, empty, before the first answer", async () => {
		apiTr.listActivityPage.mockReturnValue(defer().promise);
		await mountDetail();
		expect(count(wrapper).exists()).toBe(true);
		expect(count(wrapper).attributes("aria-live")).toBe("polite");
		expect(count(wrapper).text()).toBe("");
	});

	it("FX-6: says it is loading while the first page is on its way", async () => {
		const first = defer();
		apiTr.listActivityPage.mockReturnValue(first.promise);
		await mountDetail();
		expect(block(wrapper).text()).toContain("Loading activity");
		first.resolve(answer(rowsOf(0, 3), { start: 0, total: 3, has_more: false }));
		await flushPromises();
		expect(block(wrapper).text()).not.toContain("Loading activity");
	});

	it("FX-6: not while an error is shown", async () => {
		apiTr.listActivityPage.mockRejectedValue(new Error("Could not reach the server"));
		await mountDetail();
		expect(block(wrapper).text()).not.toContain("Loading activity");
	});

	it("FX-7: a manager at the end of the list sees Showing all n, even if the total grew", async () => {
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

	it("FX-8: a slow getTrigger of the trigger left does not load the activity again", async () => {
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

	it("FX-8: a slow getTrigger does not load activity once the page became New", async () => {
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
