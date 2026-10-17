import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ExportCard from "./ExportCard.vue";
import { exportJobs, refreshExports } from "../../lib/exportJobs.js";

let wrappers;
beforeEach(() => {
	wrappers = [];
	for (const id of Object.keys(exportJobs)) delete exportJobs[id];
	vi.stubGlobal(
		"fetch",
		vi.fn(async () => ({ ok: true, json: async () => ({ message: {} }) }))
	);
});
afterEach(() => {
	wrappers.forEach((wrapper) => wrapper.unmount());
	vi.useRealTimers();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});
function card(id = "job1") {
	const wrapper = mount(ExportCard, {
		props: { summary: { export_job: id, title: "Suppliers" } },
	});
	wrappers.push(wrapper);
	return wrapper;
}
describe("persistent export card", () => {
	it("batches cards and renders authoritative state instead of the saved summary", async () => {
		fetch.mockResolvedValue({
			ok: true,
			json: async () => ({
				message: {
					job1: { title: "Current title", status: "running", rows_written: 12000 },
					job2: { title: "Other export", status: "queued" },
				},
			}),
		});
		const first = card();
		card("job2");
		await refreshExports();
		await flushPromises();
		expect(JSON.parse(fetch.mock.calls[0][1].body).names).toEqual(["job1", "job2"]);
		expect(first.text()).toContain("Current title");
		expect(first.text()).toContain("12,000 rows");
		expect(first.text()).not.toContain("%");
	});
	it("shows a download only for a private ready file", async () => {
		exportJobs.job1 = {
			state: {
				status: "ready",
				filename: "suppliers.xlsx",
				file_url: "/private/files/suppliers.xlsx",
				format: "xlsx",
				rows_written: 200000,
				size_bytes: 1024,
			},
		};
		const wrapper = card();
		expect(wrapper.get("a").attributes("href")).toBe("/private/files/suppliers.xlsx");
		exportJobs.job1.state.file_url = "javascript:alert(1)";
		await flushPromises();
		expect(wrapper.find("a").exists()).toBe(false);
	});
	it("requests cancellation and waits for the server to confirm", async () => {
		exportJobs.job1 = { state: { status: "running", rows_written: 2 } };
		fetch.mockResolvedValue({
			ok: true,
			json: async () => ({ message: { status: "running", cancel_requested: true } }),
		});
		const wrapper = card();
		await wrapper.get("button").trigger("click");
		await flushPromises();
		expect(fetch.mock.calls[0][0]).toContain(".cancel");
		expect(wrapper.text()).toContain("Cancellation requested");
		expect(wrapper.get("button").attributes("disabled")).toBeDefined();
	});
	it("keeps a useful fallback after the job is deleted", () => {
		exportJobs.job1 = { state: null };
		const wrapper = card();
		expect(wrapper.text()).toContain("Suppliers");
		expect(wrapper.text()).toContain("no longer available");
		expect(wrapper.find("a").exists()).toBe(false);
	});
	it("surfaces refresh failures without presenting a stale download as current", async () => {
		fetch.mockRejectedValue(new Error("Offline"));
		const wrapper = card();
		await refreshExports();
		await flushPromises();
		expect(wrapper.get('[role="alert"]').text()).toContain("Offline");
	});
	it("stops polling terminal cards and refreshes them on focus", async () => {
		vi.useFakeTimers();
		const states = {
			job1: { status: "ready", expires_at: new Date(Date.now() + 86400000).toISOString() },
			job2: { status: "failed" },
			job3: { status: "cancelled" },
			job4: { status: "expired" },
			job5: null,
		};
		fetch.mockResolvedValue({ ok: true, json: async () => ({ message: states }) });
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
		vi.useFakeTimers();
		fetch.mockResolvedValue({
			ok: true,
			json: async () => ({
				message: {
					job1: { status: "running" },
					job2: { status: "failed" },
				},
			}),
		});
		card();
		card("job2");
		await vi.advanceTimersByTimeAsync(1);
		fetch.mockClear();
		await vi.advanceTimersByTimeAsync(5000);
		expect(fetch).toHaveBeenCalledTimes(1);
		expect(JSON.parse(fetch.mock.calls[0][1].body).names).toEqual(["job1"]);
	});
	it("does not poll a hidden tab", async () => {
		vi.useFakeTimers();
		fetch.mockResolvedValue({
			ok: true,
			json: async () => ({ message: { job1: { status: "running" } } }),
		});
		card();
		await vi.advanceTimersByTimeAsync(1);
		vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
		fetch.mockClear();
		await vi.advanceTimersByTimeAsync(15000);
		expect(fetch).not.toHaveBeenCalled();
	});
	it("refreshes a ready card when its file expires", async () => {
		vi.useFakeTimers();
		fetch.mockResolvedValueOnce({
			ok: true,
			json: async () => ({
				message: {
					job1: {
						status: "ready",
						expires_at: new Date(Date.now() + 10000).toISOString(),
					},
				},
			}),
		});
		fetch.mockResolvedValue({
			ok: true,
			json: async () => ({ message: { job1: { status: "expired" } } }),
		});
		const wrapper = card();
		await vi.advanceTimersByTimeAsync(1);
		fetch.mockClear();
		await vi.advanceTimersByTimeAsync(5000);
		expect(fetch).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(10000);
		expect(fetch).toHaveBeenCalledTimes(1);
		expect(wrapper.text()).toContain("expired");
	});
});
