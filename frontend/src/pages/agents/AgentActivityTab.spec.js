import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis#1062 owner feedback on the Activity feed:
 *   1. a run row's Badge reflects the run's CURRENT status (run_status, a
 *      live join - agents_api.list_agent_activity_page), not the action
 *      verb's point-in-time label.
 *   2. clicking a row navigates: a run row -> that agent's Runs tab with the
 *      run preselected (?run=<id>); anything else -> Overview.
 *   3. the detail line clamps to one line, full text in a title attribute.
 */

const apiAgents = vi.hoisted(() => ({ listAgentActivityPage: vi.fn() }));
vi.mock("@/api/agents", () => apiAgents);

const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("vue-router", () => ({ useRouter: () => router }));

// jsdom here has no usable localStorage, and useListPage's persisted view
// state is beside the point for this test (see WikiTab.spec.js precedent).
vi.mock("@vueuse/core", async (importOriginal) => {
	const actual = await importOriginal();
	const { ref } = await import("vue");
	return { ...actual, useStorage: (_key, initial) => ref(initial) };
});

vi.mock("frappe-ui", () => ({
	call: vi.fn(),
	dayjs: () => ({ format: () => "" }),
	dayjsLocal: (d) => ({
		format: () => String(d || ""),
		fromNow: () => "",
		isValid: () => !!d,
		valueOf: () => (d ? new Date(String(d).replace(" ", "T")).getTime() : 0),
	}),
	getConfig: () => null,
	toast: { error: vi.fn(), success: vi.fn() },
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="badge" :data-theme="theme">{{ label }}</span>`,
	},
	FeatherIcon: { name: "FeatherIcon", template: "<i />" },
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "placeholder"],
		emits: ["update:modelValue"],
		template: `<input :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	ListFooter: { name: "ListFooter", template: "<div />" },
	Tooltip: { name: "Tooltip", template: `<span><slot /></span>` },
}));

import AgentActivityTab from "./AgentActivityTab.vue";

function activityRow(overrides = {}) {
	return {
		name: "ACT-0001",
		agent: "close-auditor",
		agent_title: "Close Auditor",
		action: "run_started",
		run: "RUN-0001",
		run_status: null,
		detail: "",
		creation: "2026-09-01 10:00:00",
		...overrides,
	};
}

function envelope(rows) {
	return { rows, total: rows.length, has_more: false, start: 0, page_length: 20 };
}

async function mountTab(rows) {
	apiAgents.listAgentActivityPage.mockResolvedValue(envelope(rows));
	const w = mount(AgentActivityTab);
	await flushPromises();
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
});

describe("the badge reflects the run's live status, not the action label", () => {
	it("shows run_status even though the action still reads 'Run started'", async () => {
		const w = await mountTab([activityRow({ action: "run_started", run_status: "failed" })]);
		expect(w.text()).toContain("Run started");
		const badge = w.find(".badge");
		expect(badge.exists()).toBe(true);
		expect(badge.text()).toBe("failed");
		expect(badge.attributes("data-theme")).toBe("red");
	});

	it("uses the app's STATUS_THEME colours for every status", async () => {
		const cases = [
			["running", "blue"],
			["completed", "green"],
			["partial", "orange"],
			["failed", "red"],
			["stopped", "gray"],
		];
		for (const [status, theme] of cases) {
			const w = await mountTab([activityRow({ run_status: status })]);
			expect(w.find(".badge").attributes("data-theme")).toBe(theme);
		}
	});

	it("renders no badge when run_status is null (no run, or the run was deleted)", async () => {
		const w = await mountTab([activityRow({ action: "enabled", run: "", run_status: null })]);
		expect(w.find(".badge").exists()).toBe(false);
	});
});

describe("clicking a row navigates", () => {
	it("a run row opens the agent's Runs tab with the run preselected", async () => {
		const w = await mountTab([activityRow({ action: "run_failed", run: "RUN-0007" })]);
		await w.find('[role="button"]').trigger("click");
		expect(router.push).toHaveBeenCalledWith({
			name: "AgentDetail",
			params: { slug: "close-auditor" },
			hash: "#runs",
			query: { run: "RUN-0007" },
		});
	});

	it("a non-run row opens Overview", async () => {
		const w = await mountTab([activityRow({ action: "enabled", run: "" })]);
		await w.find('[role="button"]').trigger("click");
		expect(router.push).toHaveBeenCalledWith({
			name: "AgentDetail",
			params: { slug: "close-auditor" },
			hash: "#overview",
		});
	});

	it("Enter on a focused row navigates the same as a click", async () => {
		const w = await mountTab([activityRow({ action: "promoted_to_live", run: "" })]);
		await w.find('[role="button"]').trigger("keydown.enter");
		expect(router.push).toHaveBeenCalledWith({
			name: "AgentDetail",
			params: { slug: "close-auditor" },
			hash: "#overview",
		});
	});

	it("a run action with no run recorded falls back to Overview, not a query-less Runs tab", async () => {
		const w = await mountTab([activityRow({ action: "run_completed", run: "" })]);
		await w.find('[role="button"]').trigger("click");
		expect(router.push).toHaveBeenCalledWith({
			name: "AgentDetail",
			params: { slug: "close-auditor" },
			hash: "#overview",
		});
	});
});

describe("the detail line clamps to one line with the full text in a title", () => {
	it("truncates and carries the full text as a title attribute", async () => {
		const longNote =
			"Partial scan - coverage gaps: GL Entry, Account, Company were not fully evaluated this run, re-run advised";
		const w = await mountTab([activityRow({ detail: longNote })]);
		const detail = w.find(".truncate");
		expect(detail.exists()).toBe(true);
		expect(detail.attributes("title")).toBe(longNote);
		expect(detail.text()).toBe(longNote);
	});
});
