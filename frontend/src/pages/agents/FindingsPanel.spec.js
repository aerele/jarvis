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
 *   C3. A run shows its STEP TIMELINE - a ticking elapsed-time label plus the
 *       steps the bench observed this run take (list_run_steps), polled every
 *       5s while running and kept, collapsed, once it is over.
 */

const api = vi.hoisted(() => ({
	listAgentFindings: vi.fn(),
	setFindingState: vi.fn(),
}));
vi.mock("@/api", () => api);

const apiAgents = vi.hoisted(() => ({
	takeFindingToChat: vi.fn(),
	stopAgentRun: vi.fn(),
	listRunSteps: vi.fn(),
}));
vi.mock("@/api/agents", () => apiAgents);

// jarvis#1062: Notes-on-a-run reuses useDocmeta/CommentsSection, re-targeted
// at "Jarvis Agent Run" - mock the network boundary (@/api/docmeta) and use
// the REAL useDocmeta composable, so a test can assert the exact
// (doctype, name) it calls through with; CommentsSection itself is stubbed
// (DOMPurify/Avatar/Dropdown/an async TipTap composer are someone else's
// coverage - AgentDetail.spec.js stubs it the same way) but the stub reads
// real `docmeta.meta.comments` and calls the real `docmeta.addComment`.
const apiDocmeta = vi.hoisted(() => ({
	getDocmeta: vi.fn(),
	addComment: vi.fn(),
	updateComment: vi.fn(),
	deleteComment: vi.fn(),
}));
vi.mock("@/api/docmeta", () => apiDocmeta);
vi.mock("@/components/doc/CommentsSection.vue", () => ({
	default: {
		name: "CommentsSection",
		props: ["docmeta", "canComment", "heading", "emptyText"],
		template: `<div class="notes-section">
			<div class="notes-heading">{{ heading }}</div>
			<div
				v-if="!(docmeta.meta && docmeta.meta.comments && docmeta.meta.comments.length)"
				class="notes-empty"
			>{{ emptyText }}</div>
			<div v-for="c in (docmeta.meta && docmeta.meta.comments) || []" :key="c.name" class="note-row">
				{{ c.content }}
			</div>
			<button data-testid="post-note" @click="docmeta.addComment('a note')">post</button>
		</div>`,
	},
}));

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

// list_run_steps rows: the launch dispatch plus one humanized tool call
const STEPS = [
	{
		name: "S1",
		seq: 1,
		kind: "dispatched",
		tool: "",
		label: "Dispatched to the agent",
		detail: "trigger: manual",
		status: "ok",
		duration_ms: null,
		occurred_at: "2026-09-01 10:00:00",
	},
	{
		name: "S2",
		seq: 2,
		kind: "tool",
		tool: "get_list",
		label: "Read Sales Invoice, 12 rows",
		detail: "",
		status: "ok",
		duration_ms: 340,
		occurred_at: "2026-09-01 10:00:04",
	},
];

function setVisibility(state) {
	Object.defineProperty(document, "visibilityState", {
		value: state,
		writable: true,
		configurable: true,
	});
}

beforeEach(() => {
	vi.clearAllMocks();
	setVisibility("visible");
	api.listAgentFindings.mockResolvedValue({
		rows: [],
		total: 0,
		has_more: false,
		severity_counts: {},
	});
	apiAgents.listRunSteps.mockResolvedValue({ steps: [], count: 0 });
	apiDocmeta.getDocmeta.mockResolvedValue({
		comments: [],
		assignees: [],
		liked_by: [],
		liked: false,
		attachments: [],
		shares: [],
		created: { owner: "", full_name: "", creation: "" },
		modified: { modified_by: "", full_name: "", modified: "" },
	});
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

describe("C3: the run step timeline - ticking elapsed time + observed steps", () => {
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

	it("fetches and renders THIS run's steps while it is running", async () => {
		apiAgents.listRunSteps.mockResolvedValue({ steps: STEPS, count: 2 });
		const w = mountPanel(baseRun({ status: "running", name: "RUN-0042" }));
		await flushPromises();

		expect(apiAgents.listRunSteps).toHaveBeenCalledWith("RUN-0042");
		expect(w.text()).toContain("Dispatched to the agent");
		expect(w.text()).toContain("Read Sales Invoice, 12 rows");
		expect(w.text()).toContain("2 steps");
	});

	it("marks the latest step as current while running, and only that one", async () => {
		apiAgents.listRunSteps.mockResolvedValue({ steps: STEPS, count: 2 });
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		const now = w.findAll(".badge").filter((b) => b.text() === "Now");
		expect(now.length).toBe(1);
	});

	it("shows the empty state before the first step lands", async () => {
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		expect(w.text()).toContain("Dispatched to the agent, waiting for the first step");
	});

	it("keeps the scribe placeholder wording distinct from the auditor one", async () => {
		const w = mountPanel(
			baseRun({ status: "running", nature: "Scribe", pages_json: "[]", pages_written: 0 })
		);
		await flushPromises();
		expect(w.text()).toContain("pages appear here as they are written");
	});

	it("stops polling after the component unmounts (no leaked interval)", async () => {
		vi.useFakeTimers();
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		const before = apiAgents.listRunSteps.mock.calls.length;
		w.unmount();
		await vi.advanceTimersByTimeAsync(30000);
		await flushPromises();
		expect(apiAgents.listRunSteps.mock.calls.length).toBe(before);
	});

	it("keeps the timeline on screen once findings exist", async () => {
		api.listAgentFindings.mockResolvedValue({
			rows: [{ name: "F1", severity: "blocker", title: "x", state: "open" }],
			total: 1,
			has_more: false,
			severity_counts: { blocker: 1 },
		});
		apiAgents.listRunSteps.mockResolvedValue({ steps: STEPS, count: 2 });
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		expect(w.text()).toContain("Running for");
		expect(w.text()).toContain("Read Sales Invoice, 12 rows");
	});

	it("skips the 5s re-fetch while the tab is hidden, and resumes once visible", async () => {
		vi.useFakeTimers();
		setVisibility("visible");
		mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		const callsAfterMount = apiAgents.listRunSteps.mock.calls.length;

		setVisibility("hidden");
		await vi.advanceTimersByTimeAsync(30000);
		await flushPromises();
		expect(apiAgents.listRunSteps.mock.calls.length).toBe(callsAfterMount);

		setVisibility("visible");
		await vi.advanceTimersByTimeAsync(5000);
		await flushPromises();
		expect(apiAgents.listRunSteps.mock.calls.length).toBeGreaterThan(callsAfterMount);
	});
});

describe("C3b: a finished run keeps its timeline, collapsed", () => {
	it("fetches steps once for a terminal run and does not poll", async () => {
		vi.useFakeTimers();
		apiAgents.listRunSteps.mockResolvedValue({ steps: STEPS, count: 2 });
		mountPanel(baseRun({ status: "completed" }));
		await flushPromises();
		expect(apiAgents.listRunSteps).toHaveBeenCalledTimes(1);

		await vi.advanceTimersByTimeAsync(30000);
		await flushPromises();
		expect(apiAgents.listRunSteps).toHaveBeenCalledTimes(1);
	});

	it("renders a collapsed Steps (N) disclosure that opens on click", async () => {
		apiAgents.listRunSteps.mockResolvedValue({ steps: STEPS, count: 2 });
		const w = mountPanel(baseRun({ status: "completed" }));
		await flushPromises();

		expect(w.text()).toContain("Steps (2)");
		expect(w.text()).not.toContain("Read Sales Invoice, 12 rows");

		const disclosure = w.findAll("button").find((b) => b.text().includes("Steps (2)"));
		await disclosure.trigger("click");
		expect(w.text()).toContain("Read Sales Invoice, 12 rows");
	});

	it("re-fetches once on the flip from running to terminal, then stops", async () => {
		vi.useFakeTimers();
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		const whileRunning = apiAgents.listRunSteps.mock.calls.length;

		await w.setProps({ run: baseRun({ status: "completed" }) });
		await flushPromises();
		const afterFlip = apiAgents.listRunSteps.mock.calls.length;
		expect(afterFlip).toBe(whileRunning + 1);

		await vi.advanceTimersByTimeAsync(30000);
		await flushPromises();
		expect(apiAgents.listRunSteps.mock.calls.length).toBe(afterFlip);
	});

	it("shows an error step in the red tone, with the tool's message", async () => {
		apiAgents.listRunSteps.mockResolvedValue({
			steps: [
				{
					name: "S1",
					seq: 1,
					kind: "tool",
					tool: "run_report",
					label: "Ran report Trial Balance",
					detail: "unknown Report: Trial Balance",
					status: "error",
					duration_ms: 40,
					occurred_at: "2026-09-01 10:00:01",
				},
			],
			count: 1,
		});
		const w = mountPanel(baseRun({ status: "completed" }));
		await flushPromises();
		await w
			.findAll("button")
			.find((b) => b.text().includes("Steps (1)"))
			.trigger("click");
		expect(w.html()).toContain("text-ink-red-4");
		// a bare red row explains nothing - the failure has to say what failed
		expect(w.text()).toContain("unknown Report: Trial Balance");
	});
});

describe("C3c: consecutive identical steps collapse into one row", () => {
	function repeated(n, overrides = {}) {
		return Array.from({ length: n }, (_, i) => ({
			name: `R${i}`,
			seq: i + 1,
			kind: "tool",
			tool: "get_list",
			label: "Read Sales Invoice, 12 rows",
			detail: "",
			status: "ok",
			duration_ms: 100,
			occurred_at: `2026-09-01 10:00:0${i}`,
			...overrides,
		}));
	}

	it("folds a repeated read into a single row with an xN count", async () => {
		apiAgents.listRunSteps.mockResolvedValue({ steps: repeated(3), count: 3 });
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();

		expect(w.text()).toContain("x3");
		// one rendered row, but the header still reports every recorded step
		expect(w.text().match(/Read Sales Invoice, 12 rows/g).length).toBe(1);
		expect(w.text()).toContain("3 steps");
	});

	it("does not fold steps more than 5s apart", async () => {
		const steps = repeated(2);
		steps[1].occurred_at = "2026-09-01 10:00:30";
		apiAgents.listRunSteps.mockResolvedValue({ steps, count: 2 });
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();

		expect(w.text()).not.toContain("x2");
		expect(w.text().match(/Read Sales Invoice, 12 rows/g).length).toBe(2);
	});

	it("never folds a failure into a success", async () => {
		const steps = repeated(2);
		steps[1].status = "error";
		steps[1].detail = "no permission to read Sales Invoice";
		apiAgents.listRunSteps.mockResolvedValue({ steps, count: 2 });
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();

		expect(w.text()).not.toContain("x2");
		expect(w.text()).toContain("no permission to read Sales Invoice");
	});

	it("renders a measured 0ms rather than dropping it, but nothing for a missing one", async () => {
		apiAgents.listRunSteps.mockResolvedValue({
			steps: [
				{ ...STEPS[1], name: "Z1", duration_ms: 0, occurred_at: "2026-09-01 10:00:00" },
				{
					...STEPS[0],
					name: "Z2",
					label: "Dispatched to the agent",
					duration_ms: null,
					occurred_at: "2026-09-01 10:00:20",
				},
			],
			count: 2,
		});
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		// a step that finished inside the timer's resolution still reports a time
		expect(w.text()).toContain("0ms");
		// ...while a step that recorded none reports nothing at all
		expect(w.text()).not.toContain("nullms");
		expect(w.text()).not.toContain("NaN");
	});

	it("marks only the last COLLAPSED row as current", async () => {
		apiAgents.listRunSteps.mockResolvedValue({ steps: repeated(3), count: 3 });
		const w = mountPanel(baseRun({ status: "running" }));
		await flushPromises();
		expect(w.findAll(".badge").filter((b) => b.text() === "Now").length).toBe(1);
	});
});

describe("stopped run explanation (jarvis#1062 polish)", () => {
	it("shows the error row for a stopped run with a recorded error", async () => {
		const w = mountPanel(baseRun({ status: "stopped", error: "operator stopped it" }));
		await flushPromises();
		expect(w.text()).toContain("operator stopped it");
	});

	it("falls back to a stopped-specific message when no error was recorded", async () => {
		const w = mountPanel(baseRun({ status: "stopped", error: "" }));
		await flushPromises();
		expect(w.text()).toContain("This run was stopped before it reported findings.");
	});

	it("does not render the failed-run fallback text for a stopped run", async () => {
		const w = mountPanel(baseRun({ status: "stopped", error: "" }));
		await flushPromises();
		expect(w.text()).not.toContain("This run failed before recording findings.");
	});
});

describe("Discuss in chat honors ok/reason, not just conversation (jarvis#1062 polish)", () => {
	function findingRow() {
		return {
			name: "F1",
			rule_id: "R1",
			severity: "blocker",
			title: "A finding",
			state: "open",
			amount: null,
			recurrence: "new",
			detail_md: "",
		};
	}

	async function expandAndDiscuss(w) {
		await flushPromises();
		const row = w.find('[role="button"]');
		await row.trigger("click");
		await flushPromises();
		const btn = w
			.findAll("button")
			.find((b) => b.attributes("data-label") === "Discuss in chat");
		await btn.trigger("click");
		await flushPromises();
	}

	it("does not navigate and toasts the reason when ok is false", async () => {
		api.listAgentFindings.mockResolvedValue({
			rows: [findingRow()],
			total: 1,
			has_more: false,
			severity_counts: { blocker: 1 },
		});
		apiAgents.takeFindingToChat.mockResolvedValue({
			ok: false,
			conversation: "CONV-9",
			reason: "No LLM configured for this container.",
		});
		const w = mountPanel(baseRun({ status: "completed" }));
		await expandAndDiscuss(w);
		expect(router.push).not.toHaveBeenCalled();
		const { toast } = await import("frappe-ui");
		expect(toast.error).toHaveBeenCalledWith("No LLM configured for this container.");
	});

	it("falls back to a generic message when ok is false with no reason", async () => {
		api.listAgentFindings.mockResolvedValue({
			rows: [findingRow()],
			total: 1,
			has_more: false,
			severity_counts: { blocker: 1 },
		});
		apiAgents.takeFindingToChat.mockResolvedValue({ ok: false, conversation: "CONV-9" });
		const w = mountPanel(baseRun({ status: "completed" }));
		await expandAndDiscuss(w);
		expect(router.push).not.toHaveBeenCalled();
		const { toast } = await import("frappe-ui");
		expect(toast.error).toHaveBeenCalledWith("Could not open this finding in chat.");
	});

	it("navigates to the conversation when ok is true", async () => {
		api.listAgentFindings.mockResolvedValue({
			rows: [findingRow()],
			total: 1,
			has_more: false,
			severity_counts: { blocker: 1 },
		});
		apiAgents.takeFindingToChat.mockResolvedValue({ ok: true, conversation: "CONV-9" });
		const w = mountPanel(baseRun({ status: "completed" }));
		await expandAndDiscuss(w);
		expect(router.push).toHaveBeenCalledWith("/c/CONV-9");
	});
});

// jarvis#1062 owner decision: Notes moved off the Configure tab onto the Run
// itself - the SAME CommentsSection/useDocmeta pair, re-targeted at
// "Jarvis Agent Run" + this run's name instead of the installation.
describe("Notes on the run (jarvis#1062)", () => {
	it("requests the docmeta bundle for THIS run - doctype and name, not the installation", async () => {
		const w = mountPanel(baseRun({ name: "RUN-NOTES-1", status: "completed" }));
		await flushPromises();
		expect(apiDocmeta.getDocmeta).toHaveBeenCalledWith("Jarvis Agent Run", "RUN-NOTES-1");
		// "Run notes", not "Notes": SEVERITY_LABEL.note already renders a "Notes"
		// heading in this same panel for note-severity findings.
		expect(w.find(".notes-heading").text()).toBe("Run notes");
	});

	it("renders the run's existing comments", async () => {
		apiDocmeta.getDocmeta.mockResolvedValue({
			comments: [
				{
					name: "COMM-1",
					content: "looked into the flagged ledger entry",
					owner: "reviewer@example.com",
					owner_name: "Reviewer",
					owner_image: "",
					creation: "2026-09-01 11:00:00",
					modified: "2026-09-01 11:00:00",
				},
			],
			assignees: [],
			liked_by: [],
			liked: false,
			attachments: [],
			shares: [],
			created: { owner: "", full_name: "", creation: "" },
			modified: { modified_by: "", full_name: "", modified: "" },
		});
		const w = mountPanel(baseRun({ status: "completed" }));
		await flushPromises();
		expect(w.text()).toContain("looked into the flagged ledger entry");
		expect(w.find(".notes-empty").exists()).toBe(false);
	});

	it("shows the empty copy when the run has no notes yet", async () => {
		const w = mountPanel(baseRun({ status: "completed" }));
		await flushPromises();
		expect(w.find(".notes-empty").text()).toBe("No notes on this run yet.");
	});

	it("posting calls the API with the run's reference (Jarvis Agent Run + this run's name)", async () => {
		apiDocmeta.addComment.mockResolvedValue({
			name: "COMM-9",
			content: "a note",
			owner: "me@example.com",
			owner_name: "Me",
			owner_image: "",
			creation: "2026-09-01 12:00:00",
			modified: "2026-09-01 12:00:00",
		});
		const w = mountPanel(baseRun({ name: "RUN-NOTES-2", status: "completed" }));
		await flushPromises();
		await w.find('[data-testid="post-note"]').trigger("click");
		await flushPromises();
		expect(apiDocmeta.addComment).toHaveBeenCalledWith(
			"Jarvis Agent Run",
			"RUN-NOTES-2",
			"a note"
		);
	});

	it("renders for a running run too, not only terminal statuses", async () => {
		const w = mountPanel(baseRun({ status: "running", finished_at: "" }));
		await flushPromises();
		expect(w.find(".notes-section").exists()).toBe(true);
	});

	it("renders below the findings list, for a scribe (Custom App Learning) run too", async () => {
		const w = mountPanel(
			baseRun({ status: "completed", nature: "Scribe", pages_written: 1, pages_json: "[]" })
		);
		await flushPromises();
		expect(w.find(".notes-section").exists()).toBe(true);
	});

	it("switching the selected run reloads Notes for the newly selected run", async () => {
		const w = mountPanel(baseRun({ name: "RUN-A", status: "completed" }));
		await flushPromises();
		expect(apiDocmeta.getDocmeta).toHaveBeenLastCalledWith("Jarvis Agent Run", "RUN-A");

		await w.setProps({ run: baseRun({ name: "RUN-B", status: "completed" }) });
		await flushPromises();
		expect(apiDocmeta.getDocmeta).toHaveBeenLastCalledWith("Jarvis Agent Run", "RUN-B");
	});
});
