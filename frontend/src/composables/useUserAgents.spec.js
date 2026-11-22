import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { defineComponent, h } from "vue";
import { mount, flushPromises } from "@vue/test-utils";

const api = vi.hoisted(() => ({ listUserAgents: vi.fn() }));
vi.mock("@/api/userAgents", () => api);
// lib/userAgents reaches frappe-ui through the macros schedule label; only dayjs is read.
vi.mock("frappe-ui", () => ({ dayjs: () => ({ format: () => "" }) }));

import { useUserAgents } from "./useUserAgents";

const agent = (name, status = "Synced", extra = {}) => ({
	name,
	agent_name: name,
	enabled: true,
	sync: { status, error: "", message: "", synced_at: null },
	...extra,
});

function harness() {
	let state;
	const w = mount(
		defineComponent({
			setup() {
				state = useUserAgents();
				return () => h("div");
			},
		})
	);
	return { w, state };
}

beforeEach(() => {
	vi.useFakeTimers();
	api.listUserAgents.mockReset();
});
afterEach(() => vi.useRealTimers());

describe("useUserAgents", () => {
	it("stays off until the site says user agents are on", async () => {
		api.listUserAgents.mockResolvedValue({ enabled: false, max_agents: 5, agents: [] });
		const { state } = harness();
		expect(state.enabled.value).toBe(false);
		await state.load();
		expect(state.enabled.value).toBe(false);
		expect(state.agents.value).toEqual([]);
	});

	it("reads the agents and the cap", async () => {
		api.listUserAgents.mockResolvedValue({
			enabled: true,
			max_agents: 2,
			agents: [agent("a"), agent("b")],
		});
		const { state } = harness();
		await state.load();
		expect(state.enabled.value).toBe(true);
		expect(state.agents.value.map((a) => a.name)).toEqual(["a", "b"]);
		expect(state.atCap.value).toBe(true);
	});

	it("hides the feature and keeps the reason when the first read fails", async () => {
		api.listUserAgents.mockRejectedValue({ messages: ["Jarvis is offline."] });
		const { state } = harness();
		await state.load();
		expect(state.enabled.value).toBe(false);
		expect(state.error.value).toBe("Jarvis is offline.");
	});

	it("re-reads while a sync is pending and stops once it settles", async () => {
		api.listUserAgents
			.mockResolvedValueOnce({
				enabled: true,
				max_agents: 5,
				agents: [agent("a", "Pending")],
			})
			.mockResolvedValueOnce({
				enabled: true,
				max_agents: 5,
				agents: [agent("a", "Pending")],
			})
			.mockResolvedValue({ enabled: true, max_agents: 5, agents: [agent("a", "Synced")] });
		const { state } = harness();
		await state.load();
		expect(api.listUserAgents).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(3000);
		expect(api.listUserAgents).toHaveBeenCalledTimes(2);
		await vi.advanceTimersByTimeAsync(3000);
		expect(api.listUserAgents).toHaveBeenCalledTimes(3);
		expect(state.agents.value[0].sync.status).toBe("Synced");
		await vi.advanceTimersByTimeAsync(30000);
		expect(api.listUserAgents).toHaveBeenCalledTimes(3);
	});

	it("does not poll a pending sync that already carries a reason", async () => {
		const stuck = agent("a", "Pending", {
			sync: {
				status: "Pending",
				error: "rate_limited",
				message: "Try again soon.",
				synced_at: null,
			},
		});
		api.listUserAgents.mockResolvedValue({ enabled: true, max_agents: 5, agents: [stuck] });
		const { state } = harness();
		await state.load();
		await vi.advanceTimersByTimeAsync(10000);
		expect(api.listUserAgents).toHaveBeenCalledTimes(1);
	});

	it("keeps the list on screen when a quiet re-read fails", async () => {
		api.listUserAgents
			.mockResolvedValueOnce({
				enabled: true,
				max_agents: 5,
				agents: [agent("a", "Pending")],
			})
			.mockRejectedValueOnce(new Error("offline"));
		const { state } = harness();
		await state.load();
		await vi.advanceTimersByTimeAsync(3000);
		await flushPromises();
		expect(state.enabled.value).toBe(true);
		expect(state.agents.value).toHaveLength(1);
	});

	it("upserts and removes without another read", async () => {
		api.listUserAgents.mockResolvedValue({
			enabled: true,
			max_agents: 5,
			agents: [agent("a")],
		});
		const { state } = harness();
		await state.load();
		state.upsert(agent("b"));
		state.upsert({ ...agent("a"), agent_name: "Renamed" });
		expect(state.agents.value.map((a) => a.agent_name)).toEqual(["Renamed", "b"]);
		state.remove("a");
		expect(state.agents.value.map((a) => a.name)).toEqual(["b"]);
		expect(api.listUserAgents).toHaveBeenCalledTimes(1);
	});
});
