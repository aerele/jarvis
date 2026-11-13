import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";

/**
 * jarvis-admin-v2#820: the dialog fetches the full activity log on open, only
 * for a manager row whose list copy was cut (`detail_truncated`). The fetch
 * runs once per open, a late or failed response never overwrites a newer
 * state, and the loading / error text sits in a persistent live region.
 */

const api = vi.hoisted(() => ({ getActivityDetail: vi.fn() }));
vi.mock("@/api/triggers", () => api);

vi.mock("frappe-ui", () => ({
	dayjs: () => ({ format: () => "" }),
	dayjsLocal: (d) => ({ format: () => String(d || ""), fromNow: () => "" }),
	getConfig: () => null,
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="badge">{{ label }}</span>`,
	},
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		template: `<div v-if="modelValue" role="dialog"><slot name="body-content" /></div>`,
	},
	FeatherIcon: { name: "FeatherIcon", template: "<i />" },
	Tooltip: { name: "Tooltip", template: `<span><slot /></span>` },
}));

import ActivityDetailDialog from "./ActivityDetailDialog.vue";

const CLIPPED = "clipped log text…";
const FULL = "the full stored log, much longer than the clipped copy";
const DONE_TEXT = "Showing the full log.";
const ERROR_TEXT =
	"Could not load the full log. The text below is cut short. Close and open this row to try again.";

function activityRow(overrides = {}) {
	return {
		name: "ACT-0001",
		trigger: "TRG-1",
		trigger_label: "Notify on invoice",
		status: "Failed",
		action_type: "Script",
		summary: "Script failed",
		detail: CLIPPED,
		detail_truncated: true,
		creation: "2026-09-01 10:00:00",
		...overrides,
	};
}

function deferred() {
	let resolve, reject;
	const promise = new Promise((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

// keepMounted mimics the frappe-ui leave transition: the body stays in the DOM
// (hidden) after the dialog closes.
const KeptMountedDialog = {
	name: "Dialog",
	props: ["modelValue", "options"],
	template: `<div v-show="modelValue" role="dialog"><slot name="body-content" /></div>`,
};

function mountDialog({ keepMounted = false } = {}) {
	const stubs = { "router-link": { template: "<a><slot /></a>" } };
	if (keepMounted) stubs.Dialog = KeptMountedDialog;
	return mount(ActivityDetailDialog, {
		props: { modelValue: false, row: null },
		global: { stubs },
	});
}

// Both surfaces set `row` and open the dialog in the same tick.
async function openWith(w, row) {
	await w.setProps({ row, modelValue: true });
	await flushPromises();
}

const pre = (w) => w.find("pre");
const status = (w) => w.find('[role="status"]');

describe("ActivityDetailDialog - full log on open", () => {
	beforeEach(() => {
		api.getActivityDetail.mockReset();
		vi.restoreAllMocks();
	});

	it("makes no request when the row was not cut", async () => {
		const w = mountDialog();
		await openWith(w, activityRow({ detail: "short", detail_truncated: false }));
		expect(api.getActivityDetail).not.toHaveBeenCalled();
		expect(pre(w).text()).toBe("short");
		expect(status(w).text()).toBe("");
	});

	it("makes no request for a row without the flag (non-manager shape)", async () => {
		const w = mountDialog();
		const row = activityRow();
		delete row.detail;
		delete row.detail_truncated;
		await openWith(w, row);
		expect(api.getActivityDetail).not.toHaveBeenCalled();
	});

	it("makes exactly one request per open of a cut row", async () => {
		api.getActivityDetail.mockResolvedValue({ name: "ACT-0001", detail: FULL });
		const w = mountDialog();
		await openWith(w, activityRow());
		expect(api.getActivityDetail).toHaveBeenCalledTimes(1);
		expect(api.getActivityDetail).toHaveBeenCalledWith("ACT-0001");
	});

	it("shows the full text once it arrives", async () => {
		const d = deferred();
		api.getActivityDetail.mockReturnValue(d.promise);
		const w = mountDialog();
		await openWith(w, activityRow());
		expect(pre(w).text()).toBe(CLIPPED);
		expect(pre(w).attributes("aria-busy")).toBe("true");
		expect(status(w).text()).toBe("Loading the full log…");
		d.resolve({ name: "ACT-0001", detail: FULL });
		await flushPromises();
		expect(pre(w).text()).toBe(FULL);
		expect(pre(w).attributes("aria-busy")).not.toBe("true");
		expect(status(w).text()).toBe(DONE_TEXT);
	});

	it("keeps the clipped text and shows the error line on failure", async () => {
		vi.spyOn(console, "warn").mockImplementation(() => {});
		api.getActivityDetail.mockRejectedValue(new Error("boom"));
		const w = mountDialog();
		await openWith(w, activityRow());
		expect(pre(w).text()).toBe(CLIPPED);
		expect(status(w).text()).toBe(ERROR_TEXT);
		expect(w.find("button").exists()).toBe(false);
	});

	it("keeps the full text when an older request rejects after a newer one resolved", async () => {
		const first = deferred();
		const second = deferred();
		api.getActivityDetail
			.mockReturnValueOnce(first.promise)
			.mockReturnValueOnce(second.promise);
		const w = mountDialog();
		const row = activityRow();
		await openWith(w, row); // open A, request 1 in flight
		await w.setProps({ modelValue: false }); // close
		await w.setProps({ modelValue: true }); // open A again, request 2
		await flushPromises();
		expect(api.getActivityDetail).toHaveBeenCalledTimes(2);
		second.resolve({ name: "ACT-0001", detail: FULL });
		await flushPromises();
		expect(pre(w).text()).toBe(FULL);
		first.reject(new Error("late failure"));
		await flushPromises();
		expect(pre(w).text()).toBe(FULL);
		expect(status(w).text()).toBe(DONE_TEXT);
	});

	it("ignores a response for row A that arrives after close and open of row B", async () => {
		const a = deferred();
		api.getActivityDetail.mockReturnValueOnce(a.promise);
		const w = mountDialog();
		await openWith(w, activityRow({ name: "ACT-A" }));
		await w.setProps({ modelValue: false }); // close with A in flight
		await openWith(
			w,
			activityRow({ name: "ACT-B", detail: "B text", detail_truncated: false })
		);
		a.resolve({ name: "ACT-A", detail: "A FULL LOG, LONGER THAN THE CLIPPED COPY" });
		await flushPromises();
		expect(pre(w).text()).toBe("B text");
		expect(api.getActivityDetail).toHaveBeenCalledTimes(1);
	});

	it("drops a response that arrives after close, keeps the text on close, and refetches on reopen", async () => {
		const a = deferred();
		api.getActivityDetail.mockReturnValueOnce(a.promise);
		const w = mountDialog({ keepMounted: true });
		await openWith(w, activityRow());
		await w.setProps({ modelValue: false });
		// the text is not cleared on close (no jump during the leave transition)
		expect(pre(w).text()).toBe(CLIPPED);
		a.resolve({ name: "ACT-0001", detail: FULL });
		await flushPromises();
		// the late response was dropped
		expect(pre(w).text()).toBe(CLIPPED);
		api.getActivityDetail.mockReturnValueOnce(new Promise(() => {}));
		await w.setProps({ modelValue: true });
		// reopened: state was reset on open and a fresh request ran
		expect(api.getActivityDetail).toHaveBeenCalledTimes(2);
		expect(pre(w).text()).toBe(CLIPPED);
	});

	it("keeps the loading state when an older request rejects while the newer one is pending", async () => {
		const first = deferred();
		const second = deferred();
		api.getActivityDetail
			.mockReturnValueOnce(first.promise)
			.mockReturnValueOnce(second.promise);
		const w = mountDialog();
		await openWith(w, activityRow());
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		first.reject(new Error("late failure"));
		await flushPromises();
		expect(status(w).text()).toBe("Loading the full log\u2026");
		expect(status(w).classes()).toContain("text-ink-gray-5");
		expect(status(w).classes()).not.toContain("text-ink-red-4");
		expect(pre(w).attributes("aria-busy")).toBe("true");
	});

	it("refetches when the row changes while open and ignores the older response", async () => {
		const a = deferred();
		const b = deferred();
		api.getActivityDetail.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
		const w = mountDialog();
		await openWith(w, activityRow({ name: "ACT-A", detail: "A clipped" }));
		await w.setProps({ row: activityRow({ name: "ACT-B", detail: "B clipped" }) });
		await flushPromises();
		expect(api.getActivityDetail).toHaveBeenCalledTimes(2);
		expect(api.getActivityDetail).toHaveBeenLastCalledWith("ACT-B");
		a.resolve({ name: "ACT-A", detail: "A FULL LOG, LONGER THAN THE CLIPPED COPY" });
		await flushPromises();
		expect(pre(w).text()).toBe("B clipped");
		b.resolve({ name: "ACT-B", detail: "B FULL LOG, LONGER THAN THE CLIPPED COPY" });
		await flushPromises();
		expect(pre(w).text()).toBe("B FULL LOG, LONGER THAN THE CLIPPED COPY");
	});

	it("drops a late response after close while the content is still mounted", async () => {
		const a = deferred();
		api.getActivityDetail.mockReturnValueOnce(a.promise);
		const w = mountDialog({ keepMounted: true });
		await openWith(w, activityRow());
		await w.setProps({ modelValue: false });
		a.resolve({ name: "ACT-0001", detail: FULL });
		await flushPromises();
		expect(pre(w).text()).toBe(CLIPPED);
	});

	it("shows the status region from open, with its text set after nextTick", async () => {
		const d = deferred();
		api.getActivityDetail.mockReturnValue(d.promise);
		const w = mountDialog();
		await w.setProps({ row: activityRow(), modelValue: true });
		const region = status(w);
		expect(region.exists()).toBe(true);
		expect(region.attributes("aria-live")).toBe("polite");
		await nextTick();
		await nextTick();
		expect(status(w).text()).toBe("Loading the full log…");
	});

	it("renders the log block as a labelled, focusable region", async () => {
		api.getActivityDetail.mockResolvedValue({ name: "ACT-0001", detail: FULL });
		const w = mountDialog();
		await openWith(w, activityRow());
		const el = pre(w);
		expect(el.attributes("role")).toBe("region");
		expect(el.attributes("aria-label")).toBe("Full log");
		expect(el.attributes("tabindex")).toBe("0");
	});

	it("keeps the clipped text and shows the error when the full log is empty", async () => {
		api.getActivityDetail.mockResolvedValue({ name: "ACT-0001", detail: "" });
		const w = mountDialog();
		await openWith(w, activityRow());
		expect(pre(w).text()).toBe(CLIPPED);
		expect(status(w).text()).toBe(ERROR_TEXT);
		expect(status(w).classes()).toContain("text-ink-red-4");
		expect(pre(w).attributes("aria-busy")).not.toBe("true");
	});

	it("keeps the clipped text and shows the error when the response has no detail", async () => {
		api.getActivityDetail.mockResolvedValue({ name: "ACT-0001" });
		const w = mountDialog();
		await openWith(w, activityRow());
		expect(pre(w).text()).toBe(CLIPPED);
		expect(status(w).text()).toBe(ERROR_TEXT);
	});

	it("keeps the clipped text and shows the error when the full log is not longer than the clipped copy", async () => {
		// "clipped log text" is the clipped copy without its trailing ellipsis
		api.getActivityDetail.mockResolvedValue({
			name: "ACT-0001",
			detail: "clipped log text",
		});
		const w = mountDialog();
		await openWith(w, activityRow());
		expect(pre(w).text()).toBe(CLIPPED);
		expect(status(w).text()).toBe(ERROR_TEXT);
	});

	it("accepts a full log one character longer than the clipped copy without its ellipsis", async () => {
		api.getActivityDetail.mockResolvedValue({
			name: "ACT-0001",
			detail: "clipped log text!",
		});
		const w = mountDialog();
		await openWith(w, activityRow());
		expect(pre(w).text()).toBe("clipped log text!");
		expect(status(w).text()).toBe(DONE_TEXT);
	});

	it("makes no request when the cut row has no name", async () => {
		const w = mountDialog();
		await openWith(w, activityRow({ name: undefined }));
		expect(api.getActivityDetail).not.toHaveBeenCalled();
		expect(pre(w).text()).toBe(CLIPPED);
		expect(status(w).text()).toBe("");
		expect(pre(w).attributes("aria-busy")).not.toBe("true");
	});

	it("warns with the row name and the error on failure, never the log text", async () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		const err = new Error("boom");
		api.getActivityDetail.mockRejectedValue(err);
		const w = mountDialog();
		await openWith(w, activityRow());
		expect(warn).toHaveBeenCalledTimes(1);
		expect(warn).toHaveBeenCalledWith("get_activity_detail failed", "ACT-0001", err);
		expect(JSON.stringify(warn.mock.calls)).not.toContain(CLIPPED);
	});

	it("does not warn for a stale rejection", async () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		const first = deferred();
		api.getActivityDetail
			.mockReturnValueOnce(first.promise)
			.mockReturnValueOnce(new Promise(() => {}));
		const w = mountDialog();
		await openWith(w, activityRow());
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		first.reject(new Error("late"));
		await flushPromises();
		expect(warn).not.toHaveBeenCalled();
	});

	it("puts the status line before the log block in the DOM, with no gap while empty", async () => {
		const w = mountDialog();
		await openWith(w, activityRow({ detail: "short", detail_truncated: false }));
		const html = w.html();
		expect(html.indexOf('role="status"')).toBeGreaterThan(-1);
		expect(html.indexOf('role="status"')).toBeLessThan(html.indexOf("<pre"));
		expect(status(w).text()).toBe("");
		expect(status(w).classes()).not.toContain("mt-2");
	});

	it("reserves a stable status slot for a cut row while loading, after success and after an error", async () => {
		const SLOT = ["mt-2", "min-h-5"];
		const d = deferred();
		api.getActivityDetail.mockReturnValueOnce(d.promise);
		const w = mountDialog();
		await openWith(w, activityRow());
		expect(status(w).text()).toBe("Loading the full log…");
		SLOT.forEach((c) => expect(status(w).classes()).toContain(c));
		d.resolve({ name: "ACT-0001", detail: FULL });
		await flushPromises();
		expect(status(w).text()).toBe(DONE_TEXT);
		SLOT.forEach((c) => expect(status(w).classes()).toContain(c));
		// error state, on a second cut row
		vi.spyOn(console, "warn").mockImplementation(() => {});
		api.getActivityDetail.mockRejectedValueOnce(new Error("boom"));
		await openWith(w, activityRow({ name: "ACT-0002" }));
		expect(status(w).text()).toBe(ERROR_TEXT);
		SLOT.forEach((c) => expect(status(w).classes()).toContain(c));
	});

	it("takes no space for a row that was not cut", async () => {
		const w = mountDialog();
		await openWith(w, activityRow({ detail: "short", detail_truncated: false }));
		expect(status(w).exists()).toBe(true);
		expect(status(w).classes()).not.toContain("mt-2");
		expect(status(w).classes()).not.toContain("min-h-5");
	});

	it("gives the log block a visible keyboard focus ring", async () => {
		const w = mountDialog();
		await openWith(w, activityRow({ detail: "short", detail_truncated: false }));
		const cls = pre(w).classes();
		expect(cls).toContain("focus-visible:outline-none");
		expect(cls).toContain("focus-visible:ring-2");
		expect(cls).toContain("focus-visible:ring-outline-gray-3");
	});
});
