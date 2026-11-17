// Shared mounting helpers for the calendar grid specs: the golden November snapshot with a
// server-style per-day `net`, and a `.pcal-layout` host whose width the specs can set.
import { mount } from "@vue/test-utils";
import { afterEach, beforeEach, vi } from "vitest";
import CalendarGrid from "../../src/pages/payment-calendar/CalendarGrid.vue";
import { scenario } from "./templateFixtures";

// Server-provided nets for the dates the specs read (receivable minus payable, as the server sends them).
const NETS = {
	"2026-11-11": "44202.00",
	"2026-11-12": "12506.00",
	"2026-11-23": "-19373.00",
	"2026-11-24": "0.00",
};

export function novemberSnapshot() {
	const snapshot = scenario("default");
	for (const [date, net] of Object.entries(NETS)) snapshot.projection.days[date].net = net;
	return snapshot;
}

let observed = [];
export class FakeResizeObserver {
	constructor(callback) {
		this.callback = callback;
		observed.push(this);
	}
	observe() {}
	disconnect() {}
}
export function resizeLayout(width) {
	for (const observer of observed) observer.callback([{ contentRect: { width } }]);
}

// width: the `.pcal-layout` container width; omit for "unmeasured" (jsdom has no layout).
export function mountGrid({ width, props = {} } = {}) {
	const host = document.createElement("div");
	host.className = "pcal-layout";
	if (width !== undefined) Object.defineProperty(host, "clientWidth", { value: width });
	document.body.appendChild(host);
	const wrapper = mount(CalendarGrid, {
		props: {
			snapshot: novemberSnapshot(),
			selected: "2026-11-11",
			month: "2026-11",
			...props,
		},
		attachTo: host,
	});
	// onMounted measures the layout and re-renders; flush it so specs see the final DOM.
	wrapper.vm.$.update();
	const unmount = wrapper.unmount.bind(wrapper);
	wrapper.unmount = () => {
		unmount();
		host.remove();
	};
	return wrapper;
}

export function useFakeResizeObserver() {
	beforeEach(() => {
		observed = [];
		vi.stubGlobal("ResizeObserver", FakeResizeObserver);
	});
	afterEach(() => vi.unstubAllGlobals());
}
