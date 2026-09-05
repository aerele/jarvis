import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * UsagePane now also carries the tenant-wide pool status/cost sections merged
 * in from the retired standalone "Billing and metering" pane. The one thing
 * that must not regress: an ordinary member keeps seeing exactly the old
 * Usage pane, and only a System Manager or Jarvis Admin sees the merged-in
 * sections (same gate the rail's "Account and billing" group used to enforce
 * by hiding the whole pane).
 */

vi.hoisted(() => {
	window.matchMedia = (q) => ({
		matches: false,
		media: q,
		onchange: null,
		addEventListener() {},
		removeEventListener() {},
		addListener() {},
		removeListener() {},
		dispatchEvent: () => false,
	});
});

vi.mock("frappe-ui", () => {
	const passthrough = (name, tag) => ({
		name,
		props: ["label", "theme", "variant", "iconLeft", "loading", "disabled", "size"],
		template: `<${tag} class="stub-${name.toLowerCase()}" :data-label="label">{{ label }}</${tag}>`,
	});
	return {
		Badge: passthrough("Badge", "span"),
		Button: passthrough("Button", "button"),
		ErrorMessage: passthrough("ErrorMessage", "span"),
	};
});

vi.mock("@/theme", () => ({
	useJarvisTheme: () => ({ effectiveDark: { value: false } }),
}));

vi.mock("@/stores/shell", () => ({
	useShellStore: () => ({ chatContext: null }),
}));

// Real echarts/JvChart machinery needs a DOM ResizeObserver + async "echarts"
// import that jsdom can't give it; these charts are exercised elsewhere
// (chartTheme/usageCharts unit tests). Here only the gating matters.
vi.mock("@/charts/JvChart.vue", () => ({
	default: {
		name: "JvChart",
		props: ["spec", "dark"],
		template: `<div class="stub-jvchart" />`,
	},
}));
vi.mock("@/charts/EChart.vue", () => ({
	default: { name: "EChart", props: ["option"], template: `<div class="stub-echart" />` },
}));

const api = vi.hoisted(() => ({
	getUsage: vi.fn(),
	getLlmConfig: vi.fn(),
	getLlmUsage: vi.fn(),
	getLlmSyncStatus: vi.fn(),
}));
vi.mock("@/api", () => api);

import UsagePane from "./UsagePane.vue";

function meteringConfigResp() {
	return {
		models: [{ provider: "openai", model: "gpt-5.5", enabled: true }],
		preset: "",
		routing_mode: "failover",
		proxy_active: 0,
	};
}
function meteringUsageResp() {
	return {
		applicable: true,
		period: "August 2026",
		tokens_in: 1000,
		tokens_out: 500,
		cost_usd: 0.42,
		per_model: [],
		used_vs_limit: { used_usd: 0.42, limit_usd: null },
	};
}
function meteringSyncResp() {
	return { last_sync_status: "ok (restart via admin)", last_sync_at: "" };
}

/** Every h3/h4 section heading actually rendered, trimmed. */
function headings(w) {
	return w.findAll("h3, h4").map((n) => n.text().trim());
}

async function mountAs({ isSM = false, isAdmin = false } = {}) {
	window.is_system_manager = isSM;
	window.is_jarvis_admin = isAdmin;
	const w = mount(UsagePane);
	await flushPromises();
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
	api.getUsage.mockResolvedValue(null);
	api.getLlmConfig.mockResolvedValue(meteringConfigResp());
	api.getLlmUsage.mockResolvedValue(meteringUsageResp());
	api.getLlmSyncStatus.mockResolvedValue(meteringSyncResp());
});

afterEach(() => {
	delete window.is_system_manager;
	delete window.is_jarvis_admin;
});

describe("UsagePane, ordinary member", () => {
	it("renders exactly today's Usage pane, with none of the pool sections", async () => {
		const w = await mountAs({ isSM: false, isAdmin: false });
		expect(w.text()).not.toContain("Workspace pool");
		expect(w.text()).not.toContain("Active pool");
		expect(w.text()).not.toContain("Metered cost");
		expect(headings(w)).not.toContain("Status");
		expect(headings(w)).not.toContain("Active pool");
	});

	it("never calls the admin-gated metering endpoints", async () => {
		await mountAs({ isSM: false, isAdmin: false });
		expect(api.getLlmConfig).not.toHaveBeenCalled();
		expect(api.getLlmUsage).not.toHaveBeenCalled();
		expect(api.getLlmSyncStatus).not.toHaveBeenCalled();
	});

	it("still loads the member's own usage", async () => {
		await mountAs({ isSM: false, isAdmin: false });
		expect(api.getUsage).toHaveBeenCalled();
	});
});

describe("UsagePane, System Manager", () => {
	it("renders the merged-in pool status, active pool and metered-cost sections", async () => {
		const w = await mountAs({ isSM: true });
		expect(w.text()).toContain("Workspace pool");
		const heads = headings(w);
		expect(heads).toContain("Status");
		expect(heads).toContain("Active pool");
		// Retitled away from "Usage" (would collide with the pane's own h2 title)
		// per the merge - the old BillingMeteringPane called this section "Usage".
		expect(heads.some((h) => h.startsWith("Metered cost"))).toBe(true);
		expect(heads).not.toContain("Usage");
	});

	it("calls all three metering endpoints on mount", async () => {
		await mountAs({ isSM: true });
		expect(api.getLlmConfig).toHaveBeenCalled();
		expect(api.getLlmUsage).toHaveBeenCalled();
		expect(api.getLlmSyncStatus).toHaveBeenCalled();
	});

	it("shows the pool mode and preset rows", async () => {
		const w = await mountAs({ isSM: true });
		expect(w.text()).toContain("openai · gpt-5.5");
	});
});

describe("UsagePane, Jarvis Admin (not System Manager)", () => {
	it("also sees the merged-in pool sections", async () => {
		const w = await mountAs({ isSM: false, isAdmin: true });
		expect(w.text()).toContain("Workspace pool");
		expect(api.getLlmConfig).toHaveBeenCalled();
	});
});

describe("UsagePane metering error/retry", () => {
	it("shows the pane-level error and a Retry button when the metering fetch fails", async () => {
		api.getLlmUsage.mockRejectedValue(new Error("boom"));
		const w = await mountAs({ isSM: true });
		expect(w.find(".stub-errormessage").exists()).toBe(true);
		const retry = w.findAll(".stub-button").find((b) => b.text() === "Retry");
		expect(retry).toBeDefined();
	});

	it("a failed config fetch alone also surfaces the error, not a wrong pool state", async () => {
		api.getLlmConfig.mockRejectedValue(new Error("boom"));
		const w = await mountAs({ isSM: true });
		expect(w.find(".stub-errormessage").exists()).toBe(true);
	});
});

describe("UsagePane, this chat context", () => {
	// This row lives in its own unbadged "Context" section, independent of the
	// "Measured usage" block below it - it must show even when there are no
	// account-wide measured totals yet (measured.total_tokens is 0, hasMeasured
	// false), as long as this chat's own context reading is fresh.
	it("shows context in use for this chat when measured", async () => {
		api.getUsage.mockResolvedValue({
			chat_tokens: 999,
			measured: { total_tokens: 500000, month_tokens: 100000 },
			context: { used: 42000, capacity: 200000, fresh: true },
		});
		const w = await mountAs({ isSM: false, isAdmin: false });
		expect(w.text()).toContain("42k of 200k context in use");
	});

	it("shows the row even with no measured totals yet, when this chat's context is fresh", async () => {
		api.getUsage.mockResolvedValue({
			chat_tokens: 999,
			measured: { total_tokens: 0, month_tokens: 0 },
			context: { used: 42000, capacity: 200000, fresh: true },
		});
		const w = await mountAs({ isSM: false, isAdmin: false });
		expect(w.text()).toContain("42k of 200k context in use");
		expect(w.text()).not.toContain("Measured usage");
	});

	it("falls back to not measured", async () => {
		api.getUsage.mockResolvedValue({
			chat_tokens: 999,
			measured: { total_tokens: 500000, month_tokens: 100000 },
			context: { used: 0, capacity: 0, fresh: false },
		});
		const w = await mountAs({ isSM: false, isAdmin: false });
		expect(w.text()).toContain("Not measured yet");
	});
});
