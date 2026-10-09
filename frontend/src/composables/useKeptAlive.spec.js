import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { KeepAlive, defineComponent, h, ref } from "vue";

const store = vi.hoisted(() => ({ llmConfigVersion: 0 }));
vi.mock("@/stores/shell", () => ({ useShellStore: () => store }));

import { useKeptAlive } from "./useKeptAlive";

/** Mounts a pane inside KeepAlive next to a stub; `show` flips which one is active. */
function setup(load) {
	const show = ref(true);
	let api;
	const Pane = defineComponent({
		name: "Pane",
		setup() {
			api = useKeptAlive(load);
			return () => h("div", "pane");
		},
	});
	const Other = defineComponent({ name: "Other", render: () => h("div", "other") });
	const w = mount({
		render: () => h(KeepAlive, null, [show.value ? h(Pane) : h(Other)]),
	});
	const flip = async (v) => {
		show.value = v;
		await flushPromises();
	};
	return { w, flip, getApi: () => api };
}

beforeEach(() => {
	vi.useFakeTimers({ toFake: ["Date"] });
	store.llmConfigVersion = 0;
});
afterEach(() => vi.useRealTimers());

describe("useKeptAlive", () => {
	it("loads once on mount and not again on a quick re-show", async () => {
		const load = vi.fn(() => Promise.resolve(true));
		const { flip } = setup(load);
		await flushPromises();
		await flip(false);
		await flip(true);
		expect(load).toHaveBeenCalledTimes(1);
	});

	it("refreshes on re-show once older than 30 s", async () => {
		const load = vi.fn(() => Promise.resolve(true));
		const { flip } = setup(load);
		await flushPromises();
		await flip(false);
		vi.setSystemTime(Date.now() + 31000);
		await flip(true);
		expect(load).toHaveBeenCalledTimes(2);
	});

	it("refreshes on re-show when the AI models config changed meanwhile", async () => {
		const load = vi.fn(() => Promise.resolve(true));
		const { flip } = setup(load);
		await flushPromises();
		await flip(false);
		store.llmConfigVersion += 1;
		await flip(true);
		expect(load).toHaveBeenCalledTimes(2);
	});

	it("retries on the next show after a failed first load", async () => {
		const load = vi.fn().mockResolvedValueOnce(false).mockResolvedValue(true);
		const { flip } = setup(load);
		await flushPromises();
		await flip(false);
		await flip(true);
		expect(load).toHaveBeenCalledTimes(2);
	});

	it("ignores a refresh while a load is in flight", async () => {
		const load = vi.fn(() => new Promise(() => {}));
		const { flip } = setup(load);
		await flushPromises();
		await flip(false);
		await flip(true);
		expect(load).toHaveBeenCalledTimes(1);
	});

	it("markStale forces a refresh on next show; isActive tracks visibility", async () => {
		const load = vi.fn(() => Promise.resolve(true));
		const { flip, getApi } = setup(load);
		await flushPromises();
		await flip(false);
		expect(getApi().isActive()).toBe(false);
		getApi().markStale();
		await flip(true);
		expect(getApi().isActive()).toBe(true);
		expect(load).toHaveBeenCalledTimes(2);
	});
});
