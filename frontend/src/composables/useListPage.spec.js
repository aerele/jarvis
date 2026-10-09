import { describe, it, expect, vi, beforeEach } from "vitest";
import { defineComponent, h, nextTick } from "vue";
import { mount, flushPromises } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({ toast: { error: vi.fn(), create: vi.fn() } }));

import { useListPage } from "./useListPage.js";

// `onEnvelope`: called with each answer the list keeps (the current request's), never
// with a rejection or a stale answer. Without it the list behaves as it always has.
function mountList(fetchFn, extra = {}) {
	let list;
	const Host = defineComponent({
		setup() {
			list = useListPage({ fetchFn, storageKey: `spec-${Math.random()}`, ...extra });
			return () => h("div");
		},
	});
	const wrapper = mount(Host);
	return { wrapper, list: () => list };
}

function deferred() {
	let resolve, reject;
	const promise = new Promise((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

describe("useListPage onEnvelope", () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it("is called with the envelope of a kept answer", async () => {
		const envelope = { rows: [{ name: "a" }], total: 1, notices: [{ name: "n1" }] };
		const onEnvelope = vi.fn();
		const { list } = mountList(vi.fn().mockResolvedValue(envelope), { onEnvelope });
		await flushPromises();
		expect(onEnvelope).toHaveBeenCalledTimes(1);
		expect(onEnvelope).toHaveBeenCalledWith(envelope);
		expect(list().rows.value).toEqual([{ name: "a" }]);
	});

	it("is not called for a failed request or a rejection envelope", async () => {
		const onEnvelope = vi.fn();
		const fetchFn = vi
			.fn()
			.mockRejectedValueOnce(new Error("boom"))
			.mockResolvedValueOnce({ ok: false, error: { code: "list_filter_bad_payload" } });
		const { list } = mountList(fetchFn, { onEnvelope });
		await flushPromises();
		list().resetLoad();
		await flushPromises();
		expect(fetchFn).toHaveBeenCalledTimes(2);
		expect(onEnvelope).not.toHaveBeenCalled();
	});

	it("is not called with a stale answer", async () => {
		const first = deferred();
		const second = deferred();
		const fetchFn = vi
			.fn()
			.mockReturnValueOnce(first.promise)
			.mockReturnValueOnce(second.promise);
		const onEnvelope = vi.fn();
		const { list } = mountList(fetchFn, { onEnvelope });
		await nextTick();
		list().resetLoad(); // supersedes the first request
		second.resolve({ rows: [], total: 0, notices: ["new"] });
		await flushPromises();
		first.resolve({ rows: [], total: 0, notices: ["old"] });
		await flushPromises();
		expect(onEnvelope.mock.calls).toEqual([[{ rows: [], total: 0, notices: ["new"] }]]);
	});

	it("left out, the list loads as before", async () => {
		const { list } = mountList(vi.fn().mockResolvedValue({ rows: [{ name: "a" }], total: 1 }));
		await flushPromises();
		expect(list().rows.value).toEqual([{ name: "a" }]);
		expect(list().error.value).toBe("");
	});
});
