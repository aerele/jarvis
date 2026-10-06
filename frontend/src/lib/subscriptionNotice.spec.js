import { describe, it, expect, vi, beforeEach } from "vitest";

const api = vi.hoisted(() => ({ getSubscriptionNotice: vi.fn() }));
vi.mock("@/api", () => api);

import {
	subscriptionNotice,
	loadSubscriptionNotice,
	watchSubscriptionNotice,
	expiredModelMap,
} from "./subscriptionNotice.js";

beforeEach(() => {
	vi.clearAllMocks();
	subscriptionNotice.loaded = false;
	subscriptionNotice.expired = [];
	subscriptionNotice.upstreams = null;
});

describe("loadSubscriptionNotice", () => {
	it("stores the expired list and the admin-only upstreams", async () => {
		api.getSubscriptionNotice.mockResolvedValue({
			expired: [{ upstream: "openai", label: "OpenAI", account_ref: "A1", fallback: "" }],
			upstreams: ["openai"],
		});
		await loadSubscriptionNotice();
		expect(subscriptionNotice.loaded).toBe(true);
		expect(subscriptionNotice.expired[0].account_ref).toBe("A1");
		expect(subscriptionNotice.upstreams).toEqual(["openai"]);
	});

	it("keeps upstreams null for a member and treats a junk reply as nothing expired", async () => {
		api.getSubscriptionNotice.mockResolvedValue({ expired: "x", upstreams: null });
		await loadSubscriptionNotice();
		expect(subscriptionNotice.expired).toEqual([]);
		expect(subscriptionNotice.upstreams).toBeNull();
	});

	it("never rejects and keeps the last reading when the call fails", async () => {
		subscriptionNotice.expired = [{ upstream: "kimi", label: "Kimi (Moonshot)" }];
		api.getSubscriptionNotice.mockRejectedValue(new Error("down"));
		await expect(loadSubscriptionNotice()).resolves.toBeUndefined();
		expect(subscriptionNotice.expired).toHaveLength(1);
	});

	it("coalesces concurrent calls into one request", async () => {
		api.getSubscriptionNotice.mockResolvedValue({ expired: [], upstreams: null });
		await Promise.all([loadSubscriptionNotice(), loadSubscriptionNotice()]);
		expect(api.getSubscriptionNotice).toHaveBeenCalledTimes(1);
	});
});

describe("watchSubscriptionNotice", () => {
	it("refetches on the realtime event and unsubscribes cleanly", async () => {
		api.getSubscriptionNotice.mockResolvedValue({ expired: [], upstreams: null });
		const handlers = {};
		const socket = {
			on: vi.fn((ev, fn) => (handlers[ev] = fn)),
			off: vi.fn(),
		};
		const stop = watchSubscriptionNotice(socket);
		expect(socket.on).toHaveBeenCalledWith("jarvis:subscription_health", expect.any(Function));
		handlers["jarvis:subscription_health"]({ changed: true });
		await Promise.resolve();
		expect(api.getSubscriptionNotice).toHaveBeenCalledTimes(1);
		stop();
		expect(socket.off).toHaveBeenCalledWith(
			"jarvis:subscription_health",
			handlers["jarvis:subscription_health"]
		);
	});

	it("tolerates a missing socket", () => {
		expect(() => watchSubscriptionNotice(null)()).not.toThrow();
	});
});

describe("expiredModelMap", () => {
	it("maps every model of an expired entry to its upstream, first entry winning", () => {
		const map = expiredModelMap([
			{ upstream: "openai", label: "OpenAI", models: ["a", "b"] },
			{ upstream: "anthropic", label: "Anthropic", models: ["b", "c"] },
			{ upstream: "xai", label: "xAI" },
		]);
		expect(map).toEqual({
			a: { upstream: "openai", label: "OpenAI" },
			b: { upstream: "openai", label: "OpenAI" },
			c: { upstream: "anthropic", label: "Anthropic" },
		});
	});

	it("is empty for anything that is not a list", () => {
		expect(expiredModelMap(undefined)).toEqual({});
		expect(expiredModelMap(null)).toEqual({});
	});
});
