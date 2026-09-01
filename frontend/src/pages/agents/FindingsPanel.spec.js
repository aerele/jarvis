import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis#1062 - FindingsPanel's Open Chat gate, the operator Stop control, and
 * the running-run progress display:
 *
 *   C1. "Open Chat" only renders once the run is no longer running, and a
 *       `stopped` run's status pill carries the gray theme (not the "unknown
 *       status" fallback gray by accident - the map has an explicit entry).
 *   C2. A running run offers Stop -> stop_agent_run(run.name); success toasts
 *       and asks the parent (AgentRunsBoard) to refresh; failure toasts the
 *       error and leaves the run showing running.
 *   C3. A running run with no findings/pages yet shows a ticking elapsed-time
 *       label and the agent's recent activity, instead of a static sentence.
 */

const api = vi.hoisted(() => ({
	listAgentFindings: vi.fn(),
	setFindingState: vi.fn(),
}));
vi.mock("@/api", () => api);

const apiAgents = vi.hoisted(() => ({
	takeFindingToChat: vi.fn(),
	stopAgentRun: vi.fn(),
	listAgentActivityPage: vi.fn(),
}));
vi.mock("@/api/agents", () => apiAgents);

const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("vue-router", () => ({ useRouter: () => router }));

// frappe-ui's ESM entry does not resolve under vitest (see LlmPoolEditor.spec.js).
// dayjsLocal backs @/utils/datetime's toLocalMs/timeAgo/exactDate - it needs a
// REAL epoch (valueOf) so the elapsed-time math under fake timers is honest,
// not just a stub that always reads 0.
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
	toast: { success: vi.fn(), error: vi.fn() },
	Badge: {
		name: "Badge",
		props: ["label", "theme", "variant"],
		template: `<span class="badge" :data-theme="theme">{{ label }}</span>`,
	},
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading", "variant", "theme", "iconLeft", "tooltip"],
		emits: ["click"],
		template: `<button :disabled="disabled" :data-label="label" @click="$emit('click')"><slot>{{ label }}</slot></button>`,
	},
	FeatherIcon: { name: "FeatherIcon", template: "<i />" },
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "options", "disabled"],
		emits: ["update:modelValue"],
		template: `<select :value="modelValue" @change="$emit('update:modelValue', $event.target.value)"></select>`,
	},
	Tooltip: { name: "Tooltip", template: `<span><slot /></span>` },
}));

vi.mock("@/components/JvSpinner.vue", () => ({
	default: { name: "JvSpinner", template: `<div class="spinner" />` },
}));
vi.mock("@/components/Banner.vue", () => ({
	default: {
		name: "Banner",
		props: ["type", "message"],
		template: `<div class="banner">{{ message }}</div>`,
	},
}));
vi.mock("@/markdown", () => ({ renderMarkdown: (s) => s }));
vi.mock("@/lib/errors", () => ({
	errMessage: (e) => (e && e.message) || String(e),
	errHtml: (e) => (e && e.message) || String(e),
}));

import FindingsPanel from "./FindingsPanel.vue";

function baseRun(overrides = {}) {
	return {
		name: "RUN-0001",
		agent: "close-auditor",
		status: "completed",
		trigger: "manual",
		started_at: "2026-09-01 10:00:00",
		finished_at: "2026-09-01 10:05:00",
		conversation: "CONV-0001",
		dashboard: null,
		findings_count: 0,
		blocker_count: 0,
		error: "",
		coverage_note: "",
		nature: "Auditor",
		pages_written: 0,
		pages_json: "[]",
		...overrides,
	};
}

function mountPanel(run) {
	return mount(FindingsPanel, { props: { run } });
}

beforeEach(() => {
	vi.clearAllMocks();
	api.listAgentFindings.mockResolvedValue({
		rows: [],
		total: 0,
		has_more: false,
		severity_counts: {},
	});
	apiAgents.listAgentActivityPage.mockResolvedValue({ rows: [], total: 0, has_more: false });
});

afterEach(() => {
	vi.useRealTimers();
});

describe("C1: Open Chat is gated on status, stopped renders a gray pill", () => {
	it("hides Open Chat while the run is running, even with a conversation", async () => {
		const w = mountPanel(baseRun({ status: "running", conversation: "CONV-0001" }));
		await flushPromises();
		expect(w.findAll("button").some((b) => b.text().includes("Open Chat"))).toBe(false);
	});

	it("shows Open Chat once the run is no longer running", async () => {
		const w = mountPanel(baseRun({ status: "completed", conversation: "CONV-0001" }));
		await flushPromises();
		expect(w.findAll("button").some((b) => b.text().includes("Open Chat"))).toBe(true);
	});

	it("renders the stopped status pill with the gray theme", async () => {
		const w = mountPanel(baseRun({ status: "stopped" }));
		await flushPromises();
		expect(w.find(".badge").attributes("data-theme")).toBe("gray");
	});
});

describe("C2: Stop is reachable while running and idempotent-safe (no confirm)", () => {
	function stopButton(w) {
		return w.findAll("button").find((b) => b.text() === "Stop");
	}

	it("renders Stop only while running", async () => {
		const running = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		expect(stopButton(running)).toBeTruthy();

		const completed = mountPanel(baseRun({ status: "completed" }));
		await flushPromises();
		expect(stopButton(completed)).toBeUndefined();
	});

	it("calls stop_agent_run with the run name and emits stopped on success", async () => {
		apiAgents.stopAgentRun.mockResolvedValue({ ok: true, status: "stopped" });
		const w = mountPanel(baseRun({ status: "running", name: "RUN-0042" }));
		await flushPromises();
		await stopButton(w).trigger("click");
		await flushPromises();

		expect(apiAgents.stopAgentRun).toHaveBeenCalledWith("RUN-0042");
		expect(w.emitted("stopped")).toBeTruthy();
	});

	it("toasts the error and does not emit stopped on failure", async () => {
		const { toast } = await import("frappe-ui");
		apiAgents.stopAgentRun.mockRejectedValue({ message: "That run no longer exists." });
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		await stopButton(w).trigger("click");
		await flushPromises();

		expect(toast.error).toHaveBeenCalledWith("That run no longer exists.");
		expect(w.emitted("stopped")).toBeFalsy();
	});

	it("does not render a confirm dialog before stopping", async () => {
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		expect(w.find(".dialog").exists()).toBe(false);
	});
});

describe("C3: running-run progress - ticking elapsed time + recent activity", () => {
	it("shows a ticking elapsed-time label instead of the static placeholder", async () => {
		vi.useFakeTimers();
		// setSystemTime only stamps Date.now() - only advanceTimersByTimeAsync
		// moves BOTH the clock and the interval queue together, so every
		// forward step below uses it exclusively (mixing the two produces a
		// timer queue that disagrees with Date.now()).
		vi.setSystemTime(new Date("2026-09-01T10:00:00"));
		const w = mountPanel(
			baseRun({ status: "running", started_at: "2026-09-01 10:00:00", conversation: "" })
		);
		await flushPromises();
		expect(w.text()).toContain("Running for");
		expect(w.text()).toContain("00:00");

		await vi.advanceTimersByTimeAsync(30000);
		await flushPromises();
		expect(w.text()).toContain("00:30");

		await vi.advanceTimersByTimeAsync(35000);
		await flushPromises();
		expect(w.text()).toContain("01:05");
	});

	it("fetches and renders the agent's recent activity while running", async () => {
		apiAgents.listAgentActivityPage.mockResolvedValue({
			rows: [
				{
					name: "ACT-1",
					action: "run_started",
					detail: "",
					creation: "2026-09-01 10:00:00",
				},
			],
			total: 1,
			has_more: false,
		});
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();

		expect(apiAgents.listAgentActivityPage).toHaveBeenCalledWith({
			agent: "close-auditor",
			page_length: 5,
		});
		expect(w.text()).toContain("run started");
	});

	it("keeps the scribe placeholder wording distinct from the auditor one", async () => {
		const w = mountPanel(
			baseRun({ status: "running", nature: "Scribe", pages_json: "[]", pages_written: 0 })
		);
		await flushPromises();
		expect(w.text()).toContain("pages appear here as they are written");
	});

	it("stops ticking after the component unmounts (no leaked interval)", async () => {
		vi.useFakeTimers();
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		const callsBeforeUnmount = apiAgents.listAgentActivityPage.mock.calls.length;
		w.unmount();
		await vi.advanceTimersByTimeAsync(30000);
		await flushPromises();
		expect(apiAgents.listAgentActivityPage.mock.calls.length).toBe(callsBeforeUnmount);
	});

	it("does not show the running-progress block once findings exist", async () => {
		api.listAgentFindings.mockResolvedValue({
			rows: [{ name: "F1", severity: "blocker", title: "x", state: "open" }],
			total: 1,
			has_more: false,
			severity_counts: { blocker: 1 },
		});
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		expect(w.text()).not.toContain("Running for");
	});
});
