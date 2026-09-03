import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis#1062 - AgentRunsBoard's stopped status theme (C1) and the 10s poll
 * that keeps a running run's rail row fresh without a manual refresh (C3):
 * started while any VISIBLE run is running, cleared on tab-hidden and on
 * unmount, and never a second competing refresh path alongside the existing
 * visibilitychange machinery.
 */

const apiAgents = vi.hoisted(() => ({ listRunsPage: vi.fn() }));
vi.mock("@/api/agents", () => apiAgents);

const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
const routeMock = vi.hoisted(() => ({ hash: "#runs", query: {} }));
vi.mock("vue-router", () => ({
	useRouter: () => router,
	useRoute: () => routeMock,
}));

// frappe-ui's ESM entry does not resolve under vitest (see LlmPoolEditor.spec.js).
vi.mock("frappe-ui", () => ({
	dayjs: () => ({ format: () => "" }),
	dayjsLocal: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	getConfig: () => null,
	toast: { error: vi.fn(), success: vi.fn() },
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="badge" :data-theme="theme">{{ label }}</span>`,
	},
	Button: {
		name: "Button",
		props: ["label", "loading", "variant", "icon", "tooltip"],
		emits: ["click"],
		template: `<button @click="$emit('click')"><slot>{{ label }}</slot></button>`,
	},
	FeatherIcon: { name: "FeatherIcon", template: "<i />" },
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "options", "placeholder"],
		emits: ["update:modelValue"],
		template: `<input :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	Tooltip: { name: "Tooltip", template: `<span><slot /></span>` },
}));

vi.mock("@/components/JvSpinner.vue", () => ({
	default: { name: "JvSpinner", template: `<div class="spinner" />` },
}));
// stubbed out entirely - its own behaviour is covered by FindingsPanel.spec.js;
// mounting it for real here would also need a vue-router mock for no reason.
vi.mock("@/pages/agents/FindingsPanel.vue", () => ({
	default: {
		name: "FindingsPanel",
		props: ["run"],
		emits: ["stopped"],
		template: `<div class="findings-panel" />`,
	},
}));

import AgentRunsBoard from "./AgentRunsBoard.vue";

function runRow(overrides = {}) {
	return {
		name: "RUN-0001",
		status: "running",
		trigger: "manual",
		started_at: "2026-09-01 10:00:00",
		findings_count: 0,
		blocker_count: 0,
		nature: "Auditor",
		...overrides,
	};
}

function envelope(rows) {
	return { rows, total: rows.length, has_more: false, start: 0, page_length: 20 };
}

function mountBoard() {
	return mount(AgentRunsBoard, { props: { agentName: "close-auditor" } });
}

function setVisibility(state) {
	Object.defineProperty(document, "visibilityState", {
		value: state,
		writable: true,
		configurable: true,
	});
	document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
	vi.clearAllMocks();
	setVisibility("visible");
	routeMock.hash = "#runs";
	routeMock.query = {};
});

afterEach(() => {
	vi.useRealTimers();
});

describe("C1: the rail's status theme covers stopped", () => {
	it("renders the stopped status pill with the gray theme", async () => {
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow({ status: "stopped" })]));
		const w = mountBoard();
		await flushPromises();
		const badges = w.findAll(".badge");
		expect(
			badges.some((b) => b.attributes("data-theme") === "gray" && b.text() === "stopped")
		).toBe(true);
	});
});

describe("C3: 10s poll while a visible run is running", () => {
	it("polls the runs list every 10s while a running row is loaded", async () => {
		vi.useFakeTimers();
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow({ status: "running" })]));
		mountBoard();
		await flushPromises();
		const callsAfterMount = apiAgents.listRunsPage.mock.calls.length;

		await vi.advanceTimersByTimeAsync(10000);
		await flushPromises();
		expect(apiAgents.listRunsPage.mock.calls.length).toBe(callsAfterMount + 1);

		await vi.advanceTimersByTimeAsync(10000);
		await flushPromises();
		expect(apiAgents.listRunsPage.mock.calls.length).toBe(callsAfterMount + 2);
	});

	it("stops polling once no loaded row is running", async () => {
		vi.useFakeTimers();
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow({ status: "running" })]));
		mountBoard();
		await flushPromises();

		// the next poll observes the run finished - no more running rows loaded
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow({ status: "completed" })]));
		await vi.advanceTimersByTimeAsync(10000);
		await flushPromises();
		const callsOnceFinished = apiAgents.listRunsPage.mock.calls.length;

		await vi.advanceTimersByTimeAsync(30000);
		await flushPromises();
		expect(apiAgents.listRunsPage.mock.calls.length).toBe(callsOnceFinished);
	});

	it("never runs while a running row is loaded but the tab is hidden", async () => {
		vi.useFakeTimers();
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow({ status: "running" })]));
		mountBoard();
		await flushPromises();
		const callsAfterMount = apiAgents.listRunsPage.mock.calls.length;

		setVisibility("hidden");
		await vi.advanceTimersByTimeAsync(30000);
		await flushPromises();
		expect(apiAgents.listRunsPage.mock.calls.length).toBe(callsAfterMount);

		// visible again: the existing visibilitychange refresh fires once, and
		// the poll resumes for subsequent ticks.
		setVisibility("visible");
		await flushPromises();
		const callsOnVisible = apiAgents.listRunsPage.mock.calls.length;
		expect(callsOnVisible).toBeGreaterThan(callsAfterMount);

		await vi.advanceTimersByTimeAsync(10000);
		await flushPromises();
		expect(apiAgents.listRunsPage.mock.calls.length).toBeGreaterThan(callsOnVisible);
	});

	it("clears the interval on unmount (no leaked poll)", async () => {
		vi.useFakeTimers();
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow({ status: "running" })]));
		const w = mountBoard();
		await flushPromises();
		const callsBeforeUnmount = apiAgents.listRunsPage.mock.calls.length;

		w.unmount();
		await vi.advanceTimersByTimeAsync(30000);
		await flushPromises();
		expect(apiAgents.listRunsPage.mock.calls.length).toBe(callsBeforeUnmount);
	});

	it("never polls at all when no loaded row is running", async () => {
		vi.useFakeTimers();
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow({ status: "completed" })]));
		mountBoard();
		await flushPromises();
		const callsAfterMount = apiAgents.listRunsPage.mock.calls.length;

		await vi.advanceTimersByTimeAsync(30000);
		await flushPromises();
		expect(apiAgents.listRunsPage.mock.calls.length).toBe(callsAfterMount);
	});
});

describe("jarvis#1062: ?run=<id> query-param preselection (deep-linked from the Activity feed)", () => {
	it("selects the row matching the query instead of the newest row", async () => {
		routeMock.query = { run: "RUN-0002" };
		apiAgents.listRunsPage.mockResolvedValue(
			envelope([runRow({ name: "RUN-0001" }), runRow({ name: "RUN-0002" })])
		);
		const w = mountBoard();
		await flushPromises();
		expect(w.findComponent({ name: "FindingsPanel" }).props("run").name).toBe("RUN-0002");
	});

	it("clears the run param from the URL once applied, WITHOUT dropping the hash", async () => {
		// jarvis#1062 fix: router.replace({query}) alone is a partial location
		// that drops the current hash entirely (verified against real
		// vue-router - {query} alone resolves to the bare path, no "#runs").
		// That silently knocked AgentDetail back onto Overview a moment after
		// landing on Runs, since route.hash changing to "" is exactly what its
		// hash watcher reads as "go to the default tab".
		routeMock.hash = "#runs";
		routeMock.query = { run: "RUN-0002", other: "kept" };
		apiAgents.listRunsPage.mockResolvedValue(
			envelope([runRow({ name: "RUN-0001" }), runRow({ name: "RUN-0002" })])
		);
		mountBoard();
		await flushPromises();
		expect(router.replace).toHaveBeenCalledWith({ hash: "#runs", query: { other: "kept" } });
	});

	it("falls back to the first row (and still clears the query, keeping the hash) when the id isn't in this page", async () => {
		routeMock.hash = "#runs";
		routeMock.query = { run: "RUN-NOT-LOADED" };
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow({ name: "RUN-0001" })]));
		const w = mountBoard();
		await flushPromises();
		expect(w.findComponent({ name: "FindingsPanel" }).props("run").name).toBe("RUN-0001");
		expect(router.replace).toHaveBeenCalledWith({ hash: "#runs", query: {} });
	});

	it("is ignored on a later refresh once an explicit selection exists", async () => {
		routeMock.query = {};
		apiAgents.listRunsPage.mockResolvedValue(
			envelope([runRow({ name: "RUN-0001" }), runRow({ name: "RUN-0002" })])
		);
		const w = mountBoard();
		await flushPromises();
		expect(w.findComponent({ name: "FindingsPanel" }).props("run").name).toBe("RUN-0001");

		// a query appearing after the initial load (e.g. a stale route object in
		// a test) must not hijack an already-explicit selection.
		routeMock.query = { run: "RUN-0002" };
		apiAgents.listRunsPage.mockResolvedValue(
			envelope([runRow({ name: "RUN-0001" }), runRow({ name: "RUN-0002" })])
		);
		await w.vm.reload();
		await flushPromises();
		expect(w.findComponent({ name: "FindingsPanel" }).props("run").name).toBe("RUN-0001");
	});
});

// jarvis#1062 P1-7 (production-readiness audit): failed and stopped runs
// both showed "0 findings" in the rail with no way to tell them apart
// without opening the run.
describe("run rows show a short reason for failed/stopped (jarvis#1062 P1-7)", () => {
	it("shows the recorded error under a failed run's row", async () => {
		apiAgents.listRunsPage.mockResolvedValue(
			envelope([runRow({ status: "failed", error: "LLM timed out after 30s." })])
		);
		const w = mountBoard();
		await flushPromises();
		expect(w.text()).toContain("LLM timed out after 30s.");
	});

	it("shows the fixed operator message under a stopped run's row", async () => {
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow({ status: "stopped" })]));
		const w = mountBoard();
		await flushPromises();
		expect(w.text()).toContain("Stopped by operator.");
	});

	it("shows no reason line for a completed or running run", async () => {
		apiAgents.listRunsPage.mockResolvedValue(
			envelope([runRow({ status: "completed" }), runRow({ status: "running" })])
		);
		const w = mountBoard();
		await flushPromises();
		expect(w.text()).not.toContain("Stopped by operator.");
	});

	it("failed and stopped read differently even with identical 0-findings counts", async () => {
		apiAgents.listRunsPage.mockResolvedValue(
			envelope([
				runRow({ name: "RUN-F", status: "failed", error: "boom" }),
				runRow({ name: "RUN-S", status: "stopped" }),
			])
		);
		const w = mountBoard();
		await flushPromises();
		expect(w.text()).toContain("boom");
		expect(w.text()).toContain("Stopped by operator.");
	});
});

// jarvis#1062 P1-5 (production-readiness audit): a raw <button> with no
// focus-visible ring - keyboard tabbing through the runs rail was invisible.
describe("run row keyboard focus (jarvis#1062 P1-5)", () => {
	it("every run row carries a focus-visible ring", async () => {
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow()]));
		const w = mountBoard();
		await flushPromises();
		const row = w.find("button.flex.w-full.items-start");
		expect(row.exists()).toBe(true);
		expect(row.classes()).toContain("focus-visible:ring-2");
		expect(row.classes()).toContain("focus-visible:ring-outline-gray-3");
	});
});

// jarvis#1062 P2-12 (production-readiness audit): the rail was a hard
// w-[360px] flex row item with no min-w-0 on the details pane - it
// overflowed the page horizontally on a narrow viewport instead of
// collapsing to a stacked layout.
describe("responsive layout: stacks below lg, no fixed-width overflow (jarvis#1062 P2-12)", () => {
	it("the rail is full-width and border-b below lg, fixed-width and border-r at lg+", async () => {
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow()]));
		const w = mountBoard();
		await flushPromises();
		const rail = w.find(".overflow-y-auto.border-b");
		expect(rail.exists()).toBe(true);
		expect(rail.classes()).toContain("w-full");
		expect(rail.classes()).toContain("lg:w-[360px]");
		expect(rail.classes()).toContain("lg:border-r");
	});

	it("the details pane carries min-w-0 so it can shrink instead of forcing overflow", async () => {
		apiAgents.listRunsPage.mockResolvedValue(envelope([runRow()]));
		const w = mountBoard();
		await flushPromises();
		const pane = w.find(".min-w-0.flex-1.overflow-y-auto");
		expect(pane.exists()).toBe(true);
	});
});
