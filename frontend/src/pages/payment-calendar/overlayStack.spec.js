import { afterEach, describe, expect, it, vi } from "vitest";
import { defineComponent, h, ref } from "vue";
import { mount } from "@vue/test-utils";
import { OVERLAY_PRIORITY, OverlayStack, provideOverlayStack, useOverlay } from "./overlayStack";

const NAMES = ["aux", "details", "picker", "company"];

// One page-like host: owns the stack and registers five overlays in the WORST order
// (lowest priority first) so only the priority, not the open order, can explain the result.
const Overlay = defineComponent({
	props: { name: String, open: Object, closed: Array },
	setup(props) {
		useOverlay(
			() => props.open[props.name],
			OVERLAY_PRIORITY[props.name],
			() => {
				props.open[props.name] = false;
				props.closed.push(props.name);
			}
		);
		return () => h("i");
	},
});
const Host = defineComponent({
	props: { open: Object, closed: Array },
	setup(props) {
		provideOverlayStack();
		return () =>
			[...NAMES]
				.reverse()
				.map((name) =>
					h(Overlay, { key: name, name, open: props.open, closed: props.closed })
				);
	},
});

function escape(init = {}) {
	const event = new KeyboardEvent("keydown", {
		key: "Escape",
		bubbles: true,
		cancelable: true,
		...init,
	});
	document.dispatchEvent(event);
	return event;
}

describe("overlay stack", () => {
	let wrapper;
	afterEach(() => {
		wrapper?.unmount();
		vi.restoreAllMocks();
	});
	function mountHost(openNames) {
		const open = ref(Object.fromEntries(NAMES.map((n) => [n, openNames.includes(n)]))).value;
		const closed = [];
		wrapper = mount(Host, { props: { open, closed } });
		return { open, closed };
	}

	it("orders aux > details > picker > company", () => {
		expect(NAMES.map((n) => OVERLAY_PRIORITY[n])).toEqual(
			[...NAMES.map((n) => OVERLAY_PRIORITY[n])].sort((a, b) => b - a)
		);
	});

	it("Escape closes only the top overlay, one per press, in priority order", async () => {
		const { open, closed } = mountHost(NAMES);
		for (const name of NAMES) {
			escape();
			await wrapper.vm.$nextTick();
			expect(closed.at(-1)).toBe(name);
		}
		expect(closed).toEqual(NAMES);
		expect(Object.values(open)).toEqual([false, false, false, false]);
	});

	it("ignores closed overlays and leaves the event alone when nothing is open", async () => {
		const { closed } = mountHost(["picker", "company"]);
		expect(escape().defaultPrevented).toBe(true);
		await wrapper.vm.$nextTick();
		expect(closed).toEqual(["picker"]);
		escape();
		await wrapper.vm.$nextTick();
		expect(escape().defaultPrevented).toBe(false);
		expect(closed).toEqual(["picker", "company"]);
	});

	it("has no priority for the dropped hover preview", () => {
		expect(OVERLAY_PRIORITY).not.toHaveProperty("preview");
	});

	it("ignores Escape that ends an IME composition", () => {
		const { closed } = mountHost(["details"]);
		const event = escape({ isComposing: true });
		expect(event.defaultPrevented).toBe(false);
		expect(closed).toEqual([]);
		escape();
		expect(closed).toEqual(["details"]);
	});

	it("an overlay that opens later is tried before the ones already open below it", async () => {
		const { open, closed } = mountHost(["company"]);
		open.details = true;
		await wrapper.vm.$nextTick();
		escape();
		expect(closed).toEqual(["details"]);
	});

	it("uses one document keydown listener, and removes it on unmount", () => {
		const add = vi.spyOn(document, "addEventListener");
		const remove = vi.spyOn(document, "removeEventListener");
		mountHost(NAMES);
		expect(add.mock.calls.filter(([type]) => type === "keydown")).toHaveLength(1);
		wrapper.unmount();
		wrapper = null;
		expect(remove.mock.calls.filter(([type]) => type === "keydown")).toHaveLength(1);
	});

	it("an unmounted overlay is no longer closed by Escape", () => {
		const stack = new OverlayStack();
		const close = vi.fn();
		const off = stack.register(OVERLAY_PRIORITY.aux, close);
		off();
		expect(stack.closeTop()).toBe(false);
		expect(close).not.toHaveBeenCalled();
	});

	it("equal priorities close the most recently opened first", () => {
		const stack = new OverlayStack();
		const order = [];
		stack.register(10, () => order.push("first"));
		stack.register(10, () => order.push("second"));
		stack.closeTop();
		expect(order).toEqual(["second"]);
	});
});
