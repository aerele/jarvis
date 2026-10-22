import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, afterEach } from "vitest";
const legacy = vi.hoisted(() => ({ getSharedCanvas: vi.fn(), getSharedToolDetail: vi.fn() }));
vi.mock("@/api/chatSharing", () => legacy);
import { sharedFileUrl } from "@/lib/sharedTranscript";
import SharedCanvas from "./SharedCanvas.vue";
import SharedToolActivity from "./SharedToolActivity.vue";
const wrappers = [];
afterEach(() => {
	wrappers.splice(0).forEach((w) => w.unmount());
	vi.clearAllMocks();
});
describe("common shared artifact reader", () => {
	it("accepts only a group-file endpoint for this exact conversation", () => {
		const url =
			"/api/method/jarvis.chat.collaboration_api.get_file?conversation=C1&file_url=%2Fprivate%2Ffiles%2Fa.html";
		expect(sharedFileUrl(url, "C1")).toBe(url);
		for (const bad of [
			url.replace("C1", "C2"),
			url + "&conversation=C1",
			"/private/files/a.html",
			"https://evil.invalid" + url,
		])
			expect(sharedFileUrl(bad, "C1")).toBe("");
	});
	it("uses the common HTML reader and keeps the iframe static", async () => {
		const load = vi.fn(async () => ({ content: "<p>Safe static preview</p>" }));
		const w = mount(SharedCanvas, {
			props: {
				conversation: "C1",
				message: "M1",
				artifact: { name: "a", type: "html" },
				loadCanvas: load,
			},
		});
		wrappers.push(w);
		await flushPromises();
		expect(load).toHaveBeenCalledWith("M1", "a", false);
		expect(legacy.getSharedCanvas).not.toHaveBeenCalled();
		expect(w.get("iframe").attributes("sandbox")).toBe("");
		expect(w.get("iframe").attributes("srcdoc")).toContain("Safe static preview");
	});
	it("fetches expanded tool details through the supplied common reader", async () => {
		const load = vi.fn(async () => ({ tool_args: "{}", tool_result: "Projected result" }));
		const w = mount(SharedToolActivity, {
			props: { message: { name: "T1", tool_name: "get_doc" }, loadDetail: load },
		});
		wrappers.push(w);
		await w.get("button").trigger("click");
		await flushPromises();
		expect(load).toHaveBeenCalledWith("T1");
		expect(legacy.getSharedToolDetail).not.toHaveBeenCalled();
		expect(w.text()).toContain("Projected result");
	});
});
