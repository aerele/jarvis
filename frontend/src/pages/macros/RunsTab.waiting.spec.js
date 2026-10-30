import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * A run parked for a prepared report on the Runs tab: it says which reports (from the
 * server's `waiting_reports`, so a reload keeps it), it can be stopped, it can be
 * filtered for, it counts as live, and it follows the run's realtime events.
 */

const api = vi.hoisted(() => ({
	listMacroRuns: vi.fn(),
	macroRunStats: vi.fn(),
	listMacros: vi.fn(),
	stopMacroRun: vi.fn(),
}));
vi.mock("@/api", () => api);
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@vueuse/core", async () => {
	const { ref } = await import("vue");
	return { useStorage: (_key, initial) => ref(initial) };
});
vi.mock("@/utils/datetime", () => ({ timeAgo: () => "20 minutes ago", exactDate: () => "" }));
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: { name: "LayoutHeader", template: "<div />" },
}));
vi.mock("frappe-ui", () => ({
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
	// Renders the page's own cells, which is what these tests are about.
	ListView: {
		name: "ListView",
		props: ["rows", "columns"],
		template: `<div><div v-for="r in rows" :key="r.name" class="row" :data-name="r.name"><template v-for="c in columns" :key="c.key"><span :class="'cell-' + c.key"><slot name="cell" :column="c" :row="r" :item="r[c.key]" align="left" /></span></template></div><slot /></div>`,
	},
	ListHeader: { template: "<div />" },
	ListHeaderItem: { template: "<div />" },
	ListRows: { template: "<div />" },
	ListRowItem: { props: ["item"], template: "<span>{{ item }}</span>" },
	ListFooter: { template: "<div />" },
	Breadcrumbs: { template: "<div />" },
	Button: {
		name: "Button",
		props: ["tooltip", "icon", "loading", "variant", "theme", "label"],
		emits: ["click"],
		template: `<button :data-tooltip="tooltip" @click="$emit('click')">{{ label }}</button>`,
	},
	Badge: { props: ["label"], template: `<span class="badge">{{ label }}</span>` },
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "options"],
		emits: ["update:modelValue"],
		template: "<div />",
	},
	Dialog: { template: "<div />" },
	FeatherIcon: { template: "<i />" },
	Tooltip: { props: ["text"], template: `<span :data-tip="text"><slot /></span>` },
}));

import RunsTab from "./RunsTab.vue";

const run = (name, extra = {}) => ({
	name,
	macro: "M1",
	macro_name: "Month-end close",
	status: "completed",
	trigger: "manual",
	current_step: 1,
	total_steps: 2,
	waiting_reports: [],
	waiting_since: "",
	...extra,
});
const waiting = (name = "run-w") =>
	run(name, {
		status: "waiting_report",
		waiting_reports: ["Stock Ageing", "Sales Register"],
		waiting_since: "2026-10-28 09:00:00",
	});

async function mountTab(rows, stats = {}) {
	api.listMacroRuns.mockResolvedValue({ runs: rows, has_more: false, total: rows.length });
	api.macroRunStats.mockResolvedValue({ total: rows.length, ...stats });
	api.listMacros.mockResolvedValue([]);
	const handlers = [];
	const socket = { on: (_e, fn) => handlers.push(fn), off: () => {} };
	const w = mount(RunsTab, { global: { provide: { $socket: socket } } });
	await flushPromises();
	return { w, emit: (p) => handlers.forEach((fn) => fn(p)) };
}

const rowEl = (w, name) => w.find(`.row[data-name="${name}"]`);
const stopBtn = (r) => r.findAll("button").find((b) => b.attributes("data-tooltip") === "Stop");

beforeEach(() => vi.clearAllMocks());

describe("RunsTab: a run waiting for a report", () => {
	it("names the reports it awaits, and the badge says it is waiting", async () => {
		const { w } = await mountTab([waiting()]);
		const row = rowEl(w, "run-w");
		expect(row.find('[data-testid="run-waiting"]').text()).toBe(
			"Waiting for report: Stock Ageing, Sales Register"
		);
		expect(row.find(".badge").text()).toBe("Waiting for report");
		expect(row.find("[data-testid='run-waiting']").attributes("title")).toBe(
			"Waiting for report: Stock Ageing, Sales Register. Waiting since 20 minutes ago"
		);
	});

	it("names them again after a reload: the names are the server's, not remembered", async () => {
		const first = await mountTab([waiting()]);
		first.w.unmount();
		const second = await mountTab([waiting()]);
		expect(rowEl(second.w, "run-w").text()).toContain("Stock Ageing, Sales Register");
	});

	it("a run that is not waiting shows no waiting text", async () => {
		const { w } = await mountTab([run("run-c")]);
		expect(w.find('[data-testid="run-waiting"]').exists()).toBe(false);
	});

	it("can be stopped, like a running run, and a finished one cannot", async () => {
		const { w } = await mountTab([
			waiting(),
			run("run-r", { status: "running" }),
			run("run-q", { status: "queued" }),
			run("run-c"),
			run("run-f", { status: "failed" }),
		]);
		for (const name of ["run-w", "run-r", "run-q"])
			expect(stopBtn(rowEl(w, name)), name).toBeTruthy();
		for (const name of ["run-c", "run-f"]) expect(stopBtn(rowEl(w, name)), name).toBeFalsy();
		// Icon-only: its accessible name is the Button's `label`.
		const stopButton = w
			.findAllComponents({ name: "Button" })
			.find((b) => b.props("tooltip") === "Stop");
		expect(stopButton.props("label")).toBe("Stop run");
		api.stopMacroRun.mockResolvedValue({ ok: true });
		await stopBtn(rowEl(w, "run-w")).trigger("click");
		expect(api.stopMacroRun).toHaveBeenCalledWith("run-w");
	});

	it("is a choice in the status filter, and filtering asks the server for it", async () => {
		const { w } = await mountTab([waiting()]);
		const select = w.findAllComponents({ name: "FormControl" })[0];
		expect(select.props("options")).toContainEqual({
			label: "Waiting for report",
			value: "waiting_report",
		});
		api.listMacroRuns.mockClear();
		select.vm.$emit("update:modelValue", "waiting_report");
		await flushPromises();
		expect(api.listMacroRuns).toHaveBeenCalledWith(
			expect.objectContaining({ status: "waiting_report" })
		);
	});

	it("counts with the running ones in the stats tile", async () => {
		const { w } = await mountTab([waiting()], {
			running: 2,
			waiting_report: 1,
			waiting_capacity: 1,
		});
		const tile = w.findAll(".rounded-lg").find((t) => t.text().includes("Running or waiting"));
		expect(tile.text()).toContain("4");
	});

	it("follows the run: running, waiting, running again, without a reload", async () => {
		const { w, emit } = await mountTab([run("run-w", { status: "running" })]);
		expect(rowEl(w, "run-w").text()).not.toContain("Waiting for report");
		api.listMacroRuns.mockResolvedValue({ runs: [waiting()], has_more: false, total: 1 });
		emit({ kind: "macro:progress", macro: "M1", status: "waiting_report", step: 1 });
		await flushPromises();
		expect(rowEl(w, "run-w").text()).toContain(
			"Waiting for report: Stock Ageing, Sales Register"
		);
		api.listMacroRuns.mockResolvedValue({
			runs: [run("run-w", { status: "running" })],
			has_more: false,
			total: 1,
		});
		emit({ kind: "macro:progress", macro: "M1", status: "running", step: 1 });
		await flushPromises();
		expect(rowEl(w, "run-w").text()).not.toContain("Waiting for report");
		expect(stopBtn(rowEl(w, "run-w"))).toBeTruthy();
	});

	it("keeps its error state when the load fails", async () => {
		api.listMacroRuns.mockRejectedValue(new Error("boom"));
		api.macroRunStats.mockResolvedValue({});
		api.listMacros.mockResolvedValue([]);
		const w = mount(RunsTab, { global: { provide: { $socket: { on() {}, off() {} } } } });
		await flushPromises();
		expect(w.text()).toContain("Couldn't load macro runs");
	});
});
