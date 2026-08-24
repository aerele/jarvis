import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis#887 / builder blank-canvas regressions. Two guarantees:
 *
 *  1. Overlapping rebuilds are ordered by a generation counter, so a SLOWER
 *     earlier rebuild that resolves last cannot re-navigate the iframe to a
 *     stale document (the intermittent blank canvas).
 *  2. The spinner spans the DATA phase for live dashboards - `loading` stays
 *     true past the iframe's DOM-parse `ready` until the first data batch
 *     resolves; static dashboards (no declared sources) clear on `ready`.
 */

// echarts load is the async boundary rebuild() races on. Default: resolve
// immediately. Guard test flips to "manual" to hold each call open and resolve
// them out of order.
const echartsGate = vi.hoisted(() => {
	const state = { mode: "auto", queue: [] };
	return {
		state,
		setManual: () => (state.mode = "manual"),
		load: vi.fn(() =>
			state.mode === "manual"
				? new Promise((resolve) => state.queue.push(resolve))
				: Promise.resolve(""),
		),
	};
});
vi.mock("@/lib/dashboardEcharts", () => ({ loadEchartsSource: echartsGate.load }));

// buildSrcdoc → a marker carrying the html it was built from, so a test can read
// the iframe's srcdoc and know which rebuild won; spied so a test can count how
// many rebuilds actually re-navigated the frame. parseSourcesBlock → "live" iff
// the html opts in with the word "sources".
const srcdoc = vi.hoisted(() => ({ build: vi.fn((html) => "DOC:" + html) }));
vi.mock("@/lib/dashboardSrcdoc", () => ({
	buildSrcdoc: srcdoc.build,
	parseSourcesBlock: (html) =>
		String(html || "").includes("sources")
			? [{ source_name: "s1", tool: "query", spec: {} }]
			: [],
}));

vi.mock("@/lib/dashboardThemes", () => ({
	THEMES: { jarvis: {} },
	DEFAULT_THEME: "jarvis",
	themeKey: () => "jarvis",
}));
vi.mock("@/lib/dashboardExport", () => ({
	loadCaptureLib: vi.fn(async () => ""),
	downloadPng: vi.fn(),
	downloadPdf: vi.fn(),
}));
vi.mock("@/lib/errors", () => ({ errMessage: (e) => String((e && e.message) || e) }));

const api = vi.hoisted(() => ({
	runDashboardSource: vi.fn(async () => ({ ok: true, data: [{ a: 1 }] })),
	callDashboardTool: vi.fn(async () => ({ ok: true, data: [{ a: 1 }] })),
}));
vi.mock("@/api/dashboards", () => api);

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "loading"],
		template: "<button>{{ label }}</button>",
	},
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: "<div>{{ message }}</div>",
	},
	FeatherIcon: { name: "FeatherIcon", template: "<i />" },
}));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { name: "JvSpinner", template: "<i class='spinner' />" },
}));

import DashboardCanvas from "./DashboardCanvas.vue";

// Deliver a validated frame message: onMessage requires e.source === the iframe's
// own contentWindow and a jarvis:1 stamp.
function sendFrameMessage(wrapper, data) {
	const fw = wrapper.find("iframe").element.contentWindow;
	const ev = new MessageEvent("message", { data: { jarvis: 1, ...data } });
	Object.defineProperty(ev, "source", { value: fw });
	window.dispatchEvent(ev);
}

const spinnerShown = (wrapper) => wrapper.find(".spinner").exists();

beforeEach(() => {
	echartsGate.state.mode = "auto";
	echartsGate.state.queue.length = 0;
	vi.clearAllMocks();
});

describe("DashboardCanvas rebuild ordering", () => {
	it("drops a stale rebuild that resolves after a newer one (no blank canvas)", async () => {
		echartsGate.setManual();
		const wrapper = mount(DashboardCanvas, {
			props: { mode: "view", html: "A echarts", dashboard: { name: "d1" } },
		});
		// First rebuild (A) is now awaiting its echarts load.
		await wrapper.setProps({ html: "B echarts" });
		// Two rebuilds in flight; resolve the NEWER (B) first, the stale (A) last.
		expect(echartsGate.state.queue.length).toBe(2);
		srcdoc.build.mockClear();
		echartsGate.state.queue[1]("");
		echartsGate.state.queue[0]("");
		await flushPromises();

		// Only the winning (newer) rebuild re-navigates the frame; the stale one
		// bails instead of re-assigning `doc` (which would blank the booted frame).
		expect(srcdoc.build).toHaveBeenCalledTimes(1);
		expect(wrapper.find("iframe").attributes("srcdoc")).toBe("DOC:B echarts");
	});
});

describe("DashboardCanvas data-phase spinner", () => {
	it("keeps the spinner until a live dashboard's first data batch resolves", async () => {
		// attachTo: the iframe only gets a real contentWindow (which onMessage
		// validates e.source against) when attached to the document.
		const wrapper = mount(DashboardCanvas, {
			attachTo: document.body,
			props: { mode: "view", html: "live sources", dashboard: { name: "d1" } },
		});
		await flushPromises();
		expect(spinnerShown(wrapper)).toBe(true);

		// DOM parsed, but live data has not arrived - spinner must stay.
		sendFrameMessage(wrapper, { type: "ready" });
		await flushPromises();
		expect(spinnerShown(wrapper)).toBe(true);

		// The widget requests its data; once it drains, the spinner clears.
		sendFrameMessage(wrapper, { type: "data", id: "1", name: "s1", tool: "query", spec: {} });
		await flushPromises();
		expect(api.runDashboardSource).toHaveBeenCalledWith("d1", "s1");
		expect(spinnerShown(wrapper)).toBe(false);
		wrapper.unmount();
	});

	it("clears the spinner on ready for a static dashboard (no sources)", async () => {
		const wrapper = mount(DashboardCanvas, {
			attachTo: document.body,
			props: { mode: "view", html: "static only", dashboard: { name: "d1" } },
		});
		await flushPromises();
		expect(spinnerShown(wrapper)).toBe(true);

		sendFrameMessage(wrapper, { type: "ready" });
		await flushPromises();
		expect(spinnerShown(wrapper)).toBe(false);
		wrapper.unmount();
	});
});

describe("DashboardCanvas re-drive reload (#965)", () => {
	it("remounts the iframe on a rebuild with identical html (forces a real reload)", async () => {
		const wrapper = mount(DashboardCanvas, {
			attachTo: document.body,
			props: { mode: "builder", html: "static only" },
		});
		await flushPromises();
		const first = wrapper.find("iframe").element;

		// The tab-return re-drive: rebuild() with the SAME html. Assigning an
		// identical srcdoc string would not reload the frame; a changing :key must.
		await wrapper.vm.rebuild();
		await flushPromises();
		const second = wrapper.find("iframe").element;

		expect(second).not.toBe(first);
		wrapper.unmount();
	});
});
