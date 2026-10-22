import { mount, flushPromises } from "@vue/test-utils";
import { reactive } from "vue";
import { describe, it, expect, vi, afterEach } from "vitest";
import SharedToolActivity from "./SharedToolActivity.vue";
import { clearSharedToolDetails } from "@/lib/sharedActivity";
vi.mock("@/api/chatSharing", () => ({ getSharedToolDetail: vi.fn() }));
const wrappers = [];
const message = (extra = {}) => ({
	name: "T1",
	tool_name: "get_doc",
	tool_status: "completed",
	modified: "one",
	...extra,
});
const render = (loadDetail, cache = new Map(), extra = {}) => {
	const w = mount(SharedToolActivity, {
		props: { message: message(), loadDetail, ...extra },
		global: { provide: { sharedToolDetails: cache } },
	});
	wrappers.push(w);
	return w;
};
afterEach(() => {
	wrappers.splice(0).forEach((w) => w.unmount());
});
describe("shared tool presentation and detail lifetime", () => {
	it("shows status, fetches lazily, and preserves expanded details on identical polls", async () => {
		const load = vi.fn(async () => ({ tool_args: '{"name":"INV-7"}', tool_result: "ok" }));
		const w = render(load);
		expect(w.text()).toContain("Completed");
		expect(load).not.toHaveBeenCalled();
		await w.get("button").trigger("click");
		await flushPromises();
		await w.setProps({ message: message() });
		await flushPromises();
		expect(w.get("button").attributes("aria-expanded")).toBe("true");
		expect(w.text()).toContain("INV-7");
		expect(load).toHaveBeenCalledTimes(1);
	});
	it("preserves an open orphan when it is reparented into a reply", async () => {
		const cache = reactive(new Map()),
			load = vi.fn(async () => ({ tool_result: "still visible" }));
		const w = render(load, cache);
		await w.get("button").trigger("click");
		await flushPromises();
		w.unmount();
		wrappers.pop();
		const regrouped = render(load, cache);
		await flushPromises();
		expect(regrouped.text()).toContain("still visible");
		expect(load).toHaveBeenCalledTimes(1);
	});
	it("refreshes revised rows and ignores obsolete responses", async () => {
		let first;
		const load = vi
			.fn()
			.mockImplementationOnce(() => new Promise((r) => (first = r)))
			.mockResolvedValue({ tool_result: "new result" });
		const cache = new Map(),
			w = render(load, cache);
		await w.get("button").trigger("click");
		await w.setProps({ message: message({ modified: "two" }) });
		await flushPromises();
		first({ tool_result: "obsolete" });
		await flushPromises();
		expect(w.text()).toContain("new result");
		expect(w.text()).not.toContain("obsolete");
		clearSharedToolDetails(cache);
		await flushPromises();
		expect(w.text()).not.toContain("new result");
	});
	it("bounds long details and retries a failed read", async () => {
		const load = vi
			.fn()
			.mockRejectedValueOnce(new Error("denied"))
			.mockResolvedValue({ tool_result: "x".repeat(9000) });
		const w = render(load);
		await w.get("button").trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Couldn't load");
		await w.get("button").trigger("click");
		await w.get("button").trigger("click");
		await flushPromises();
		expect(load).toHaveBeenCalledTimes(2);
		expect(w.findAll("pre").at(-1).text().length).toBeLessThan(4200);
		expect(w.text()).toContain("truncated");
	});
	it("never fetches pending confirmation details", async () => {
		const load = vi.fn();
		const w = render(load, new Map(), {
			message: message({ tool_status: "pending", awaiting_owner: true }),
		});
		if (w.find("button").exists()) await w.get("button").trigger("click");
		await flushPromises();
		expect(load).not.toHaveBeenCalled();
	});
});
