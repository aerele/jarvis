import { describe, it, expect, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * A question taller than the window: the title and the buttons stay pinned and only
 * the message scrolls. Focus then starts on the message, so Space pages it instead
 * of pressing Cancel.
 */

vi.mock("@/theme", async () => {
	const { ref } = await import("vue");
	return { useJarvisTheme: () => ({ effectiveDark: ref(false), paletteVars: {} }) };
});

import { confirm, settleConfirm } from "@/composables/useConfirm";
import ConfirmDialog from "./ConfirmDialog.vue";

const dialog = () => document.querySelector('[role="alertdialog"]');
const body = () => dialog().querySelector(".jv-cdialog-body");
const buttons = () => [...dialog().querySelectorAll("button")];
const press = (key, extra = {}) => {
	const e = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...extra });
	window.dispatchEvent(e);
	return e;
};

// jsdom lays nothing out: say how tall the message is and how much room it has.
function layout({ scrollHeight, clientHeight }) {
	const sh = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight");
	const ch = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight");
	Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
		configurable: true,
		get() {
			return this.classList.contains("jv-cdialog-body") ? scrollHeight : 0;
		},
	});
	Object.defineProperty(HTMLElement.prototype, "clientHeight", {
		configurable: true,
		get() {
			return this.classList.contains("jv-cdialog-body") ? clientHeight : 0;
		},
	});
	return () => {
		if (sh) Object.defineProperty(HTMLElement.prototype, "scrollHeight", sh);
		else delete HTMLElement.prototype.scrollHeight;
		if (ch) Object.defineProperty(HTMLElement.prototype, "clientHeight", ch);
		else delete HTMLElement.prototype.clientHeight;
	};
}

let w;
let restore = () => {};
async function ask(size, opts = {}) {
	restore = layout(size);
	w = mount(ConfirmDialog, { attachTo: document.body });
	confirm({ title: "Stop all runs?", message: "A very long list.", danger: true, ...opts });
	await flushPromises();
}
const LONG = { scrollHeight: 900, clientHeight: 300 };
const SHORT = { scrollHeight: 40, clientHeight: 40 };

afterEach(async () => {
	settleConfirm(false);
	await flushPromises();
	w?.unmount();
	restore();
});

describe("ConfirmDialog, a question taller than the window", () => {
	it("keeps the title and the buttons outside the part that scrolls", async () => {
		await ask(LONG, { warning: "Three runs will stop." });
		expect(body().contains(dialog().querySelector(".jv-cdialog-msg"))).toBe(true);
		expect(body().contains(dialog().querySelector(".jv-cdialog-warn"))).toBe(true);
		expect(body().contains(dialog().querySelector(".jv-cdialog-title"))).toBe(false);
		expect(body().contains(dialog().querySelector(".jv-cdialog-foot"))).toBe(false);
		expect(body().contains(buttons()[0])).toBe(false);
	});

	it("starts focus on the message, which scrolls, so Space does not press Cancel", async () => {
		await ask(LONG);
		expect(body().getAttribute("tabindex")).toBe("0");
		expect(body().getAttribute("role")).toBe("region");
		expect(body().getAttribute("aria-labelledby")).toBe("jv-confirm-title");
		expect(document.activeElement).toBe(body());
		// Space is left to the browser to page the message, and answers nothing.
		expect(press(" ").defaultPrevented).toBe(false);
		await flushPromises();
		expect(dialog()).not.toBeNull();
	});

	it("walks message, Cancel, Confirm and round again with Tab, and back with Shift+Tab", async () => {
		await ask(LONG);
		const [cancel, ok] = buttons();
		press("Tab");
		expect(document.activeElement).toBe(cancel);
		press("Tab");
		expect(document.activeElement).toBe(ok);
		press("Tab");
		expect(document.activeElement).toBe(body());
		press("Tab", { shiftKey: true });
		expect(document.activeElement).toBe(ok);
	});

	it("opens without scrolling past the title", async () => {
		const options = [];
		const real = HTMLElement.prototype.focus;
		HTMLElement.prototype.focus = function (o) {
			options.push(o);
			return real.call(this, o);
		};
		try {
			await ask(LONG);
		} finally {
			HTMLElement.prototype.focus = real;
		}
		expect(options).toContainEqual({ preventScroll: true });
	});
});

describe("ConfirmDialog, a question that fits", () => {
	it("still starts on Cancel, with no tab stop on the message", async () => {
		await ask(SHORT);
		expect(body().hasAttribute("tabindex")).toBe(false);
		expect(body().hasAttribute("role")).toBe(false);
		expect(document.activeElement).toBe(buttons()[0]);
		press("Tab");
		expect(document.activeElement).toBe(buttons()[1]);
		press("Tab");
		expect(document.activeElement).toBe(buttons()[0]);
	});

	it("forgets that the last question scrolled", async () => {
		await ask(LONG);
		settleConfirm(false);
		await flushPromises();
		restore();
		restore = layout(SHORT);
		confirm({ title: "Next?", message: "Short." });
		await flushPromises();
		expect(body().hasAttribute("tabindex")).toBe(false);
		expect(document.activeElement).toBe(buttons()[0]);
	});
});

describe("ConfirmDialog, a message whose size changes while it is open", () => {
	const observers = [];
	class FakeObserver {
		constructor(cb) {
			this.cb = cb;
			observers.push(this);
		}
		observe() {}
		unobserve() {}
		disconnect() {}
	}
	const resizeTo = async (size) => {
		restore();
		restore = layout(size);
		observers.forEach((o) => o.cb());
		await flushPromises();
	};

	afterEach(() => {
		observers.length = 0;
		vi.unstubAllGlobals();
	});

	it("moves focus to Cancel when the message stops scrolling under it", async () => {
		vi.stubGlobal("ResizeObserver", FakeObserver);
		await ask(LONG);
		expect(document.activeElement).toBe(body());
		await resizeTo(SHORT);
		expect(body().hasAttribute("tabindex")).toBe(false);
		expect(document.activeElement).toBe(buttons()[0]);
	});

	it("gives the message a tab stop when it starts to scroll", async () => {
		vi.stubGlobal("ResizeObserver", FakeObserver);
		await ask(SHORT);
		expect(body().hasAttribute("tabindex")).toBe(false);
		await resizeTo(LONG);
		expect(body().getAttribute("tabindex")).toBe("0");
		// Focus stays where the user had it.
		expect(document.activeElement).toBe(buttons()[0]);
	});
});
