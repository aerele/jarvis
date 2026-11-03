import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi } from "vitest";
vi.mock("@/api/chatSharing", () => ({
	getSharedCanvas: vi.fn(async () => ({
		content:
			'<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src none"><h1>Summary</h1>',
	})),
}));
import SharedCanvas from "./SharedCanvas.vue";
import { getSharedCanvas } from "@/api/chatSharing";
const url =
	"/api/method/jarvis.chat.sharing.get_shared_file?conversation=C1&file_url=%2Fprivate%2Ffiles%2Fsummary.html";
describe("static shared attachments", () => {
	it("uses an empty sandbox and the shared endpoint, with an original-file download", async () => {
		vi.clearAllMocks();
		const spy = vi.spyOn(window, "addEventListener");
		const w = mount(SharedCanvas, {
			props: {
				conversation: "C1",
				message: "M1",
				artifact: { name: "html", type: "html", title: "Summary", file_url: url },
			},
		});
		await flushPromises();
		expect(getSharedCanvas).toHaveBeenCalledWith("M1", "html", false);
		const frame = w.get("iframe");
		expect(frame.attributes("sandbox")).toBe("");
		expect(frame.attributes("srcdoc")).toContain("Summary");
		expect(w.get("a").attributes("href")).toBe(url);
		expect(spy.mock.calls.some(([type]) => type === "message")).toBe(false);
		spy.mockRestore();
		w.unmount();
	});
	it("does not load an owner file path or invoke a spreadsheet preview", async () => {
		vi.clearAllMocks();
		const w = mount(SharedCanvas, {
			props: {
				conversation: "C1",
				message: "M1",
				artifact: {
					name: "csv",
					type: "csv",
					title: "Sales.csv",
					file_url: "/private/files/sales.csv",
				},
			},
		});
		await flushPromises();
		expect(w.find("a,iframe,img").exists()).toBe(false);
		expect(getSharedCanvas).not.toHaveBeenCalled();
		w.unmount();
	});
});
