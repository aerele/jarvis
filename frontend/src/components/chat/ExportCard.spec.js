import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ExportCard from "./ExportCard.vue";
import { exportJobs, refreshExports, exportSummary } from "../../lib/exportJobs.js";

// Every test runs at 12:00 local on 19 Oct 2026, so "today" is stable and the clock texts below
// are built with the same locale call the card uses.
const NOON = new Date(2026, 9, 19, 12, 0, 0);
const at = (minutes, day = 0) => new Date(2026, 9, 19 + day, 12, minutes, 0).toISOString();
const clock = (iso) =>
	new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const reply = (message) => ({ ok: true, json: async () => ({ message }) });

let wrappers;
beforeEach(() => {
	vi.useFakeTimers();
	vi.setSystemTime(NOON);
	wrappers = [];
	for (const id of Object.keys(exportJobs)) delete exportJobs[id];
	vi.stubGlobal(
		"fetch",
		vi.fn(async () => reply({}))
	);
});
afterEach(() => {
	wrappers.forEach((wrapper) => wrapper.unmount());
	vi.useRealTimers();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	document.body.innerHTML = "";
});
function card(id = "job1") {
	const wrapper = mount(ExportCard, {
		props: { summary: { export_job: id, title: "Suppliers" } },
		attachTo: document.body,
	});
	wrappers.push(wrapper);
	return wrapper;
}
const base = (over = {}) => ({
	export_job: "job1",
	title: "Supplier list",
	status: "running",
	rows_written: 18400,
	started_at: at(-1),
	finished_at: null,
	asked_at: at(-2),
	expires_at: null,
	next_attempt: null,
	error: null,
	format: "xlsx",
	column_count: 12,
	source_doctype: "Supplier",
	size_bytes: 0,
	attempts: 1,
	retries: 0,
	cancel_requested: false,
	...over,
});
function show(over, id = "job1") {
	exportJobs[id] = { state: base(over), error: "" };
	return card(id);
}
const pill = (wrapper) => wrapper.get(".jv-pill");
const buttons = (wrapper) => wrapper.findAll(".jv-xbtn").map((b) => b.text());

describe("persistent export card", () => {
	it("batches cards and renders authoritative state instead of the saved summary", async () => {
		fetch.mockResolvedValue(
			reply({
				job1: base({ title: "Current title", rows_written: 12000 }),
				job2: base({ title: "Other export", status: "queued", rows_written: 0 }),
			})
		);
		const first = card();
		card("job2");
		await refreshExports();
		await flushPromises();
		expect(JSON.parse(fetch.mock.calls[0][1].body).names).toEqual(["job1", "job2"]);
		expect(first.text()).toContain("Current title");
		expect(first.text()).toContain("12,000");
		expect(first.text()).not.toContain("%");
	});

	it("says it is checking until the first answer, and announces nothing yet", () => {
		const wrapper = card();
		expect(pill(wrapper).text()).toBe("Checking");
		expect(wrapper.get("[role=status]").text()).toBe("");
		expect(wrapper.find(".jv-xbody").exists()).toBe(false);
	});

	describe("one card per state: pill, note, footer, buttons", () => {
		const cases = [
			{
				name: "queued",
				over: { status: "queued", started_at: null, rows_written: 0 },
				pill: ["Queued", ""],
				note: "Waiting for a free background worker; it starts automatically.",
				footer: `Asked at ${clock(at(-2))}`,
				buttons: ["Cancel"],
				track: "empty",
			},
			{
				name: "queued after the worker died",
				over: {
					status: "queued",
					started_at: null,
					retries: 1,
					error: "The export worker stopped. Retrying once.",
				},
				pill: ["Queued", ""],
				note: "The export worker stopped. Retrying once.",
				noteTone: "warn",
				footer: `Asked at ${clock(at(-2))} · attempt 2 of 2 next`,
				buttons: ["Cancel"],
				track: "empty",
			},
			{
				name: "running",
				over: {},
				pill: ["Preparing", "run"],
				stats: "18,400 rows written1:00 so farExcel, 12 columns",
				footer: `Started at ${clock(at(-1))}`,
				buttons: ["Cancel"],
				track: "indet",
				bar: "activity",
			},
			{
				name: "running with a count",
				over: { total_rows: 46214 },
				pill: ["Preparing", "run"],
				stats: "18,400 of 46,214 rows \u00b7 40%1:00 so farExcel, 12 columns",
				footer: `Started at ${clock(at(-1))}`,
				buttons: ["Cancel"],
				track: "value",
				bar: { now: "40", text: "18,400 of 46,214 rows, 40%" },
				fill: "40%",
			},
			{
				name: "running, all rows written",
				over: { total_rows: 46214, rows_written: 46214 },
				pill: ["Preparing", "run"],
				stats: "46,214 of 46,214 rows \u00b7 99%1:00 so farExcel, 12 columns",
				buttons: ["Cancel"],
				track: "value",
				bar: { now: "99", text: "46,214 of 46,214 rows, 99%" },
				fill: "99%",
			},
			{
				name: "running, count stale",
				over: { total_rows: 46214, rows_written: 50000 },
				pill: ["Preparing", "run"],
				stats: "50,000 rows written1:00 so farExcel, 12 columns",
				buttons: ["Cancel"],
				track: "indet",
				bar: "activity",
			},
			{
				name: "running, no count",
				over: { total_rows: null },
				pill: ["Preparing", "run"],
				stats: "18,400 rows written1:00 so farExcel, 12 columns",
				buttons: ["Cancel"],
				track: "indet",
				bar: "activity",
			},
			{
				name: "running, counted, 0 rows written",
				over: { total_rows: 46214, rows_written: 0 },
				pill: ["Preparing", "run"],
				stats: "0 rows written1:00 so farExcel, 12 columns",
				buttons: ["Cancel"],
				track: "indet",
				bar: "activity",
			},
			{
				name: "finalizing xlsx",
				over: { total_rows: 46214, rows_written: 46214, phase: "finalizing" },
				pill: ["Preparing", "run"],
				stats: "Finalizing spreadsheet\u202646,214 rows written1:00 so farExcel, 12 columns",
				buttons: ["Cancel"],
				track: "indet",
				bar: "activity",
				first: "Finalizing spreadsheet\u2026",
			},
			{
				name: "finalizing csv",
				over: { format: "csv", rows_written: 46214, phase: "finalizing" },
				pill: ["Preparing", "run"],
				stats: "Finalizing file\u202646,214 rows written1:00 so farCSV, 12 columns",
				buttons: ["Cancel"],
				track: "indet",
				bar: "activity",
				first: "Finalizing file\u2026",
			},
			{
				name: "finalizing, cancel requested",
				over: {
					total_rows: 46214,
					phase: "finalizing",
					cancel_requested: true,
				},
				pill: ["Preparing", "run"],
				stats: "18,400 of 46,214 rows \u00b7 40%1:00 so farExcel, 12 columns",
				buttons: ["Cancelling..."],
				track: "value",
				bar: { now: "40", text: "18,400 of 46,214 rows, 40%" },
				fill: "40%",
			},
			{
				name: "running on the second attempt",
				over: { retries: 1 },
				pill: ["Preparing", "run"],
				footer: `Started at ${clock(at(-1))} · attempt 2 of 2`,
				buttons: ["Cancel"],
				track: "indet",
			},
			{
				name: "paused",
				over: { status: "paused", retries: 1, attempts: 0, next_attempt: at(10) },
				pill: ["Paused", "warn"],
				note: `The site is being updated, so this export stopped to stay out of its way. It starts again automatically after ${clock(
					at(10)
				)}.`,
				noteTone: "warn",
				footer: "Attempt 1 of 2",
				buttons: ["Cancel"],
				track: "empty",
			},
			{
				name: "paused with the retry time already past",
				over: { status: "paused", retries: 1, next_attempt: at(-5) },
				pill: ["Paused", "warn"],
				note: "The site is being updated, so this export stopped to stay out of its way. It starts again automatically shortly.",
				noteTone: "warn",
				footer: "Attempt 1 of 2",
				buttons: ["Cancel"],
				track: "empty",
			},
			{
				name: "ready",
				over: {
					status: "ready",
					rows_written: 46214,
					finished_at: at(1),
					expires_at: at(0, 1),
					size_bytes: 3.8 * 1024 * 1024,
					file_url: "/private/files/x.xlsx",
					filename: "x.xlsx",
				},
				pill: ["Ready", "ok"],
				stats: "46,214 rows3.8 MBExcel, 12 columnstook 2:00",
				note: `Made at ${clock(
					at(1)
				)} from the data at that time. Ask again for fresh numbers.`,
				footer: `Available until tomorrow ${clock(at(0, 1))}`,
				buttons: ["Download .xlsx"],
			},
			{
				name: "ready, kept with the chat",
				over: {
					status: "ready",
					finished_at: at(1),
					size_bytes: 2048,
					format: "csv",
					file_url: "/private/files/x.csv",
				},
				pill: ["Ready", "ok"],
				stats: "18,400 rows2 KBCSV, 12 columnstook 2:00",
				footer: "Kept until you delete this chat",
				buttons: ["Download .csv"],
			},
			{
				name: "ready with a top-N and shortened cells",
				over: {
					status: "ready",
					finished_at: at(1),
					limited_by_spec: 100,
					cells_truncated: true,
					file_url: "/private/files/x.xlsx",
				},
				pill: ["Ready", "ok"],
				notes: [
					"Holds only the first 100 rows the query asked for.",
					"Some long cells were shortened; the row count is complete.",
				],
				buttons: ["Download .xlsx"],
			},
			{
				name: "too large",
				over: { status: "too large", error: "Narrow it by a date or a status." },
				pill: ["Not exported", "bad"],
				note: "Narrow it by a date or a status.",
				noteTone: "bad",
				footer: "Nothing was saved",
				buttons: [],
			},
			{
				name: "too slow",
				over: { status: "too slow", error: "Still running after 10 minutes." },
				pill: ["Stopped", "bad"],
				note: "Still running after 10 minutes.",
				noteTone: "bad",
				footer: "Nothing was saved",
				buttons: [],
			},
			{
				name: "failed once",
				over: { status: "failed", error: "The export could not finish." },
				pill: ["Failed", "bad"],
				note: "The export could not finish.",
				noteTone: "bad",
				footer: "Nothing was saved",
				buttons: [],
			},
			{
				name: "failed twice",
				over: { status: "failed", retries: 1, error: "The export could not finish." },
				pill: ["Failed", "bad"],
				footer: "Attempt 2 of 2",
				buttons: [],
			},
			{
				name: "cancelled",
				over: { status: "cancelled", finished_at: at(1), error: "Export cancelled." },
				pill: ["Cancelled", ""],
				note: `You cancelled this export at ${clock(at(1))}. Nothing was saved.`,
				buttons: [],
			},
			{
				name: "expired",
				over: {
					status: "expired",
					error: "This export has expired; ask for a new export.",
				},
				pill: ["Expired", ""],
				note: "This file is no longer kept. Ask again for a fresh export.",
				buttons: [],
			},
		];
		for (const c of cases)
			it(c.name, () => {
				const wrapper = show(c.over);
				expect(pill(wrapper).text()).toBe(c.pill[0]);
				expect(pill(wrapper).classes()).toContain(c.pill[1] || "jv-pill");
				const notes = wrapper.findAll(".jv-note").map((n) => n.text());
				if (c.note) expect(notes[0]).toBe(c.note);
				if (c.notes) expect(notes.slice(-c.notes.length)).toEqual(c.notes);
				if (c.noteTone) expect(wrapper.get(".jv-note").classes()).toContain(c.noteTone);
				if (c.stats)
					expect(wrapper.get(".jv-stats").text().replace(/\s+/g, "")).toBe(
						c.stats.replace(/\s+/g, "")
					);
				if (c.footer !== undefined)
					expect(wrapper.find(".jv-xfoot small").text()).toBe(c.footer);
				expect(buttons(wrapper)).toEqual(c.buttons);
				const track = wrapper.find(".jv-track");
				expect(track.exists()).toBe(!!c.track);
				if (c.track) expect(track.classes().includes("indet")).toBe(c.track === "indet");
				if (c.fill)
					expect(wrapper.get(".jv-fill").attributes("style")).toContain(
						`width: ${c.fill}`
					);
				if (c.bar) {
					expect(track.attributes("role")).toBe("progressbar");
					expect(track.attributes("aria-label")).toBe("Progress: Supplier list");
					if (c.bar === "activity") {
						// An indeterminate bar has no value, and nothing on the card shows a percent.
						expect(track.attributes("aria-valuenow")).toBeUndefined();
						expect(track.attributes("aria-valuetext")).toBeUndefined();
						expect(wrapper.text()).not.toContain("%");
					} else {
						expect(track.attributes("aria-valuemin")).toBe("0");
						expect(track.attributes("aria-valuemax")).toBe("100");
						expect(track.attributes("aria-valuenow")).toBe(c.bar.now);
						expect(track.attributes("aria-valuetext")).toBe(c.bar.text);
					}
				} else if (c.track === "empty") expect(track.attributes("role")).toBeUndefined(); // the empty queued/paused track
				if (c.first)
					expect(wrapper.get(".jv-stats span").text().startsWith(c.first)).toBe(true);
				if (c.over.phase === "finalizing" && c.over.cancel_requested)
					expect(wrapper.text()).not.toContain("Finalizing");
				else if (c.over.phase === "finalizing")
					expect(wrapper.text()).not.toContain("Ready");
				// The title and its source line are always there.
				expect(wrapper.get(".jv-xtitle b").text()).toBe("Supplier list");
				expect(wrapper.get(".jv-xtitle span").text()).toBe("Export · Supplier");
			});

		it("shows a stored summary when the job row is gone, and 'made at' only for a ready one", () => {
			exportJobs.job1 = { state: null };
			const wrapper = mount(ExportCard, {
				props: {
					summary: {
						export_job: "job1",
						title: "Suppliers",
						status: "ready",
						rows_written: 2,
						finished_at: at(3),
					},
				},
				attachTo: document.body,
			});
			wrappers.push(wrapper);
			expect(pill(wrapper).text()).toBe("Unavailable");
			expect(wrapper.get(".jv-stats").text()).toContain("2 rows");
			expect(wrapper.get(".jv-stats").text()).toContain(`Made at ${clock(at(3))}`);
			expect(buttons(wrapper)).toEqual([]);
			const failed = card("job2");
			exportJobs.job2 = { state: null };
			expect(failed.text()).not.toContain("Made at");
		});

		it("never says the same thing twice for a cancelled export", () => {
			const wrapper = show({
				status: "cancelled",
				finished_at: at(1),
				error: "Export cancelled.",
			});
			// Visible text only: the screen-reader line repeats the pill by design.
			const visible = wrapper.get(".jv-xhead").text() + wrapper.get(".jv-xbody").text();
			expect((visible.toLowerCase().match(/cancel/g) || []).length).toBeLessThanOrEqual(2);
			expect(wrapper.text()).not.toContain("Export cancelled.");
			expect(wrapper.text()).not.toContain("Cancellation can take");
		});

		it("derives 'Attempt N of 2' from retries, not attempts", () => {
			// A maintenance pause raises before attempts moves: attempts 0, retries 1.
			const paused = show({ status: "paused", attempts: 0, retries: 1 });
			expect(paused.find(".jv-xfoot small").text()).toBe("Attempt 1 of 2");
			// Mutation killed: deriving it from attempts would read "Attempt 4 of 2".
			const failed = show({ status: "failed", attempts: 4, retries: 1 }, "job2");
			expect(failed.find(".jv-xfoot small").text()).toBe("Attempt 2 of 2");
		});

		it("shows small files in KB, never 0.00 MB", () => {
			const wrapper = show({
				status: "ready",
				size_bytes: 2048,
				file_url: "/private/files/x.xlsx",
			});
			expect(wrapper.text()).toContain("2 KB");
			expect(wrapper.text()).not.toContain("0.00 MB");
		});
	});

	describe("clock and live region", () => {
		it("counts time only while running, and keeps the live region calm", async () => {
			const running = show({ started_at: new Date(Date.now() - 21000).toISOString() });
			const paused = show({ status: "paused", retries: 1 }, "job2");
			const queued = show({ status: "queued", started_at: null }, "job3");
			expect(running.get(".jv-stats").text()).toContain("0:21 so far");
			const before = [paused.text(), queued.text(), running.get("[role=status]").text()];
			await vi.advanceTimersByTimeAsync(10000);
			expect(running.get(".jv-stats").text()).toContain("0:31 so far");
			// Mutation killed: a clock on paused or queued cards changes their text.
			expect([paused.text(), queued.text(), running.get("[role=status]").text()]).toEqual(
				before
			);
			expect(running.get("[role=status]").text()).toBe("");
			expect(running.findAll("[role=status]")).toHaveLength(1);
		});

		it("stops its timer when the card goes away", async () => {
			const wrapper = show({});
			wrapper.unmount();
			wrappers.pop();
			expect(vi.getTimerCount()).toBe(0);
		});
	});

	describe("live region: open silently, announce later state changes", () => {
		const live = (wrapper) => wrapper.get("[role=status]");

		it("announces nothing for a card that opens already in a state", async () => {
			const cards = [
				show({ status: "ready", finished_at: at(-3) }, "job1"),
				show({ status: "running" }, "job2"),
				show({ status: "failed", error: "Boom." }, "job3"),
			];
			await flushPromises();
			// Mutation killed: announcing the first resolved state fills every card on open.
			expect(cards.map((wrapper) => live(wrapper).text())).toEqual(["", "", ""]);
		});

		it("announces nothing for the first answer of a card that was still checking", async () => {
			fetch.mockResolvedValue(
				reply({ job1: base({ status: "ready", finished_at: at(-3) }) })
			);
			const wrapper = card();
			expect(live(wrapper).text()).toBe("");
			await refreshExports();
			await flushPromises();
			expect(pill(wrapper).text()).toBe("Ready");
			expect(live(wrapper).text()).toBe("");
		});

		it("stays silent while a counted export ticks and finalizes, then announces ready once", async () => {
			// Opened already running with a count, so the region starts empty and any write shows.
			const wrapper = show({ total_rows: 46214, rows_written: 18400 });
			const region = live(wrapper).element;
			const writes = [];
			const observer = new MutationObserver(() => writes.push(region.textContent));
			observer.observe(region, { childList: true, characterData: true, subtree: true });
			const step = async (change) => {
				Object.assign(exportJobs.job1.state, change);
				await flushPromises();
				expect(live(wrapper).text()).toBe("");
				expect(writes).toEqual([]);
			};
			await step({ rows_written: 25000 });
			await step({ rows_written: 30000 });
			// Mutation killed: watching [status, phase] writes "Preparing" here.
			await step({ phase: "finalizing" });
			exportJobs.job1 = { state: base({ status: "ready", finished_at: at(0) }), error: "" };
			await flushPromises();
			observer.disconnect();
			expect(writes).toHaveLength(1);
			expect(writes[0]).toMatch(/^Ready\. Made at /);
		});

		it("announces nothing for a card that opens already finalizing", async () => {
			const wrapper = show({ total_rows: 46214, phase: "finalizing" });
			await flushPromises();
			expect(live(wrapper).text()).toBe("");
		});

		it("announces a later status change once, then stays quiet", async () => {
			const wrapper = show({ status: "queued", started_at: null });
			exportJobs.job1 = { state: base({ status: "running" }), error: "" };
			await flushPromises();
			expect(live(wrapper).text()).toBe("Preparing");
			exportJobs.job1 = { state: base({ status: "ready", finished_at: at(0) }), error: "" };
			await flushPromises();
			expect(live(wrapper).text()).toMatch(/^Ready\. Made at /);
			await vi.advanceTimersByTimeAsync(5000);
			expect(live(wrapper).text()).toMatch(/^Ready\. Made at /);
			expect(wrapper.findAll("[role=status]")).toHaveLength(1);
		});

		it("says why a job stopped when it moves to too large, too slow or failed", async () => {
			const wrapper = show({});
			exportJobs.job1 = {
				state: base({ status: "too large", error: "Narrow it." }),
				error: "",
			};
			await flushPromises();
			expect(live(wrapper).text()).toBe("Not exported. Narrow it.");
			exportJobs.job1 = { state: base({ status: "paused", retries: 1 }), error: "" };
			await flushPromises();
			expect(live(wrapper).text()).toMatch(/^Paused\. The site is being updated/);
		});

		it("does not announce row counts, the ticking clock or a repeat of the same status", async () => {
			const wrapper = show({ status: "queued", started_at: null });
			exportJobs.job1 = { state: base({ status: "running" }), error: "" };
			await flushPromises();
			await vi.advanceTimersByTimeAsync(12000);
			exportJobs.job1.state.rows_written = 90000;
			exportJobs.job1.state.cancel_requested = true;
			await flushPromises();
			exportJobs.job1 = {
				state: base({ status: "running", rows_written: 99000 }),
				error: "",
			};
			await flushPromises();
			expect(live(wrapper).text()).toBe("Preparing");
		});

		it("announces the cancelled state once the server confirms Cancel", async () => {
			fetch.mockResolvedValue(reply(base({ status: "cancelled", finished_at: at(1) })));
			const wrapper = show({});
			expect(live(wrapper).text()).toBe("");
			await wrapper.get('[data-testid="cancel-export"]').trigger("click");
			await flushPromises();
			expect(live(wrapper).text()).toMatch(/^Cancelled\. You cancelled this export/);
		});
	});

	it("shows a download only for a private ready file", async () => {
		const wrapper = show({
			status: "ready",
			filename: "suppliers.xlsx",
			file_url: "/private/files/suppliers.xlsx",
			rows_written: 200000,
			size_bytes: 1024,
		});
		const click = vi
			.spyOn(HTMLAnchorElement.prototype, "click")
			.mockImplementation(function () {
				expect(this.getAttribute("href")).toBe("/private/files/suppliers.xlsx");
				expect(this.download).toBe("suppliers.xlsx");
			});
		await wrapper.get('[data-testid="download-export"]').trigger("click");
		expect(click).toHaveBeenCalledOnce();
		exportJobs.job1.state.file_url = "javascript:alert(1)";
		await flushPromises();
		expect(wrapper.find('[data-testid="download-export"]').exists()).toBe(false);
	});

	describe("cancel", () => {
		it("requests cancellation, ignores a second press and waits for the server", async () => {
			fetch.mockResolvedValue(reply(base({ cancel_requested: true })));
			const wrapper = show({});
			const button = wrapper.get('[data-testid="cancel-export"]');
			await button.trigger("click");
			await button.trigger("click");
			await flushPromises();
			const cancels = fetch.mock.calls.filter((call) => call[0].includes(".cancel"));
			expect(cancels).toHaveLength(1);
			const after = wrapper.get('[data-testid="cancel-export"]');
			expect(after.text()).toBe("Cancelling...");
			expect(after.attributes("aria-disabled")).toBe("true");
			expect(after.attributes("aria-label")).toBe("Cancelling export");
			expect(after.attributes("disabled")).toBeUndefined();
			expect(wrapper.find(".jv-xfoot small").text()).toContain("stops within a few seconds");
		});

		it("stops saying Finalizing once Cancel is pressed, before the server answers", async () => {
			fetch.mockReturnValue(new Promise(() => {}));
			const wrapper = show({ total_rows: 46214, phase: "finalizing" });
			expect(wrapper.text()).toContain("Finalizing spreadsheet");
			await wrapper.get('[data-testid="cancel-export"]').trigger("click");
			await nextTick();
			// Mutation killed: not passing the local pending flag keeps the label here.
			expect(wrapper.text()).not.toContain("Finalizing");
			const button = wrapper.get('[data-testid="cancel-export"]');
			expect(button.text()).toBe("Cancelling...");
			expect(button.attributes("aria-disabled")).toBe("true");
		});

		it("moves focus to the card when the control it was on goes away", async () => {
			fetch.mockResolvedValue(reply(base({ status: "cancelled", finished_at: at(1) })));
			const wrapper = show({});
			wrapper.get('[data-testid="cancel-export"]').element.focus();
			expect(document.activeElement.dataset.testid).toBe("cancel-export");
			await wrapper.get('[data-testid="cancel-export"]').trigger("click");
			await flushPromises();
			await nextTick();
			// Mutation killed: no refocus, or a post-flush check, leaves focus on <body>.
			expect(document.activeElement).toBe(wrapper.get("section").element);
			expect(wrapper.get("section").attributes("tabindex")).toBe("-1");
		});

		it("does not steal focus from elsewhere on the page", async () => {
			const outside = document.createElement("input");
			document.body.appendChild(outside);
			const wrapper = show({});
			outside.focus();
			exportJobs.job1 = { state: base({ status: "failed", error: "x" }), error: "" };
			await flushPromises();
			expect(wrapper.find('[data-testid="cancel-export"]').exists()).toBe(false);
			expect(document.activeElement).toBe(outside);
		});

		it("shows the server's reason when cancel fails, and Try again sends it again", async () => {
			const refused = {
				ok: false,
				status: 417,
				json: async () => ({
					_server_messages: JSON.stringify([
						JSON.stringify({ message: "Too late to cancel." }),
					]),
				}),
			};
			fetch.mockResolvedValue(refused);
			const wrapper = show({});
			await wrapper.get('[data-testid="cancel-export"]').trigger("click");
			await flushPromises();
			expect(wrapper.get('[role="alert"]').text()).toContain("Too late to cancel.");
			fetch.mockClear();
			fetch.mockResolvedValue(reply(base({ status: "cancelled", finished_at: at(1) })));
			wrapper.get('[data-testid="retry-cancel"]').element.focus();
			await wrapper.get('[data-testid="retry-cancel"]').trigger("click");
			await flushPromises();
			await nextTick();
			expect(fetch.mock.calls.filter((c) => c[0].includes(".cancel"))).toHaveLength(1);
			expect(wrapper.find('[role="alert"]').exists()).toBe(false);
			expect(pill(wrapper).text()).toBe("Cancelled");
			expect(document.activeElement).toBe(wrapper.get("section").element);
		});

		it("moves focus to the card when Download goes away because the file expired", async () => {
			const wrapper = show({
				status: "ready",
				filename: "x.xlsx",
				file_url: "/private/files/x.xlsx",
			});
			wrapper.get('[data-testid="download-export"]').element.focus();
			exportJobs.job1 = { state: base({ status: "expired" }), error: "" };
			await flushPromises();
			await nextTick();
			expect(document.activeElement).toBe(wrapper.get("section").element);
		});
	});

	it("keeps a useful fallback after the job is deleted", () => {
		exportJobs.job1 = { state: null };
		const wrapper = card();
		expect(wrapper.text()).toContain("Suppliers");
		expect(wrapper.text()).toContain("Unavailable");
		expect(wrapper.find('[data-testid="download-export"]').exists()).toBe(false);
	});

	it("surfaces refresh failures with a Refresh button, not a stale download", async () => {
		fetch.mockRejectedValue(new Error("Offline"));
		const wrapper = card();
		await refreshExports();
		await flushPromises();
		expect(wrapper.get('[role="alert"]').text()).toContain("Offline");
		fetch.mockClear();
		fetch.mockResolvedValue(reply({ job1: base({ status: "queued", started_at: null }) }));
		await wrapper.get('[data-testid="refresh-export"]').trigger("click");
		await flushPromises();
		expect(fetch).toHaveBeenCalledTimes(1);
		expect(wrapper.find('[role="alert"]').exists()).toBe(false);
	});

	it("names the region after the export and has a single status line", () => {
		const wrapper = show({});
		expect(wrapper.get("section").attributes("aria-label")).toBe("Export: Supplier list");
		expect(wrapper.findAll("[role=status]")).toHaveLength(1);
		expect(wrapper.get(".jv-xtitle b").attributes("title")).toBe("Supplier list");
	});

	it("stops polling terminal cards and refreshes them on focus", async () => {
		const states = {
			job1: base({
				status: "ready",
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			}),
			job2: base({ status: "failed" }),
			job3: base({ status: "cancelled" }),
			job4: base({ status: "expired" }),
			job5: null,
		};
		fetch.mockResolvedValue(reply(states));
		Object.keys(states).forEach(card);
		await vi.advanceTimersByTimeAsync(1);
		fetch.mockClear();
		await vi.advanceTimersByTimeAsync(60000);
		expect(fetch).not.toHaveBeenCalled();
		window.dispatchEvent(new Event("focus"));
		await vi.advanceTimersByTimeAsync(1);
		expect(fetch).toHaveBeenCalledTimes(1);
	});

	it("polls only active jobs in a mixed conversation", async () => {
		fetch.mockResolvedValue(
			reply({ job1: base({ status: "running" }), job2: base({ status: "failed" }) })
		);
		card();
		card("job2");
		await vi.advanceTimersByTimeAsync(1);
		fetch.mockClear();
		await vi.advanceTimersByTimeAsync(5000);
		expect(fetch).toHaveBeenCalledTimes(1);
		expect(JSON.parse(fetch.mock.calls[0][1].body).names).toEqual(["job1"]);
	});

	it("does not poll a hidden tab", async () => {
		fetch.mockResolvedValue(reply({ job1: base({ status: "running" }) }));
		card();
		await vi.advanceTimersByTimeAsync(1);
		vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
		fetch.mockClear();
		await vi.advanceTimersByTimeAsync(15000);
		expect(fetch).not.toHaveBeenCalled();
	});

	it("refreshes a ready card when its file expires", async () => {
		fetch.mockResolvedValueOnce(
			reply({
				job1: base({
					status: "ready",
					expires_at: new Date(Date.now() + 10000).toISOString(),
				}),
			})
		);
		fetch.mockResolvedValue(reply({ job1: base({ status: "expired" }) }));
		const wrapper = card();
		await vi.advanceTimersByTimeAsync(1);
		fetch.mockClear();
		await vi.advanceTimersByTimeAsync(5000);
		expect(fetch).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(10000);
		expect(fetch).toHaveBeenCalledTimes(1);
		expect(pill(wrapper).text()).toBe("Expired");
	});

	it("renders only the persistent card, not state announcements or tool receipts", () => {
		const tool_result = JSON.stringify({ ok: true, data: { export_job: "job1" } });
		expect(exportSummary({ tool_result, tool_call_id: "export:job1:card" })).toEqual({
			export_job: "job1",
		});
		for (const suffix of ["ready", "failed", "cancelled", "expired", "retry"])
			expect(
				exportSummary({ tool_result, tool_call_id: `export:job1:${suffix}` })
			).toBeNull();
		expect(exportSummary({ tool_result })).toBeNull();
	});

	it("socket progress refreshes only its active card and respects hidden tabs", async () => {
		const states = {
			job1: base({ status: "running" }),
			job2: base({ status: "ready" }),
			job3: base({ status: "running" }),
		};
		fetch.mockResolvedValue(reply(states));
		Object.keys(states).forEach(card);
		await vi.advanceTimersByTimeAsync(1);
		fetch.mockClear();
		const event = (kind, id) =>
			window.dispatchEvent(
				new CustomEvent("jarvis:export-event", { detail: { kind, export_job: id } })
			);
		event("export:update", "job1");
		await flushPromises();
		expect(fetch).toHaveBeenCalledTimes(1);
		expect(JSON.parse(fetch.mock.calls[0][1].body).names).toEqual(["job1"]);
		fetch.mockClear();
		event("export:update", "job2");
		await flushPromises();
		expect(fetch).not.toHaveBeenCalled();
		// A terminal transition must still refresh that card, e.g. ready -> expired.
		event("export:changed", "job2");
		await flushPromises();
		expect(JSON.parse(fetch.mock.calls[0][1].body).names).toEqual(["job2"]);
		fetch.mockClear();
		vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
		event("export:update", "job1");
		event("export:changed", "job2");
		await flushPromises();
		expect(fetch).not.toHaveBeenCalled();
	});
});
