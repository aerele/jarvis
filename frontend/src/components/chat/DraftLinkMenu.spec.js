import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import DraftLinkMenu from "./DraftLinkMenu.vue";

describe("draft Link menu overlay", () => {
	let wrapper, anchor, rect, observer, tick;
	const items = [{ value: "ITEM-1", label: "First item" }];
	beforeEach(() => {
		rect = { left: 100, top: 100, bottom: 130, width: 80 };
		anchor = document.createElement("input");
		anchor.getBoundingClientRect = () => rect;
		document.body.append(anchor);
		vi.stubGlobal(
			"requestAnimationFrame",
			vi.fn((callback) => {
				tick = callback;
				return 1;
			})
		);
		vi.stubGlobal("cancelAnimationFrame", vi.fn());
		document.elementFromPoint = vi.fn(() => anchor);
		vi.stubGlobal("innerWidth", 1024);
		vi.stubGlobal("innerHeight", 768);
		vi.stubGlobal(
			"ResizeObserver",
			class {
				constructor(callback) {
					this.callback = callback;
					observer = this;
				}
				observe = vi.fn();
				disconnect = vi.fn();
			}
		);
	});
	afterEach(() => {
		wrapper?.unmount();
		anchor.remove();
		delete document.elementFromPoint;
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});
	async function open() {
		wrapper = mount(DraftLinkMenu, {
			props: { anchor, items, palette: { "--surface": "#123456" } },
		});
		await nextTick();
		return document.querySelector(".jv-action-linkmenu");
	}
	it("escapes clipping ancestors and gives narrow cells a readable, themed menu", async () => {
		const menu = await open();
		expect(menu.parentElement).toBe(document.body);
		expect(menu.style.width).toBe("300px");
		expect(menu.style.left).toBe("100px");
		expect(menu.style.top).toBe("134px");
		expect(menu.style.maxHeight).toBe("220px");
		expect(menu.style.getPropertyValue("--surface")).toBe("#123456");
		menu.querySelector("button").click();
		expect(wrapper.emitted("pick")).toEqual([[items[0]]]);
	});
	it("flips above a low row and clamps against the right edge", async () => {
		rect = { left: 950, top: 700, bottom: 730, width: 80 };
		const menu = await open();
		expect(menu.style.left).toBe("716px");
		expect(menu.style.bottom).toBe("72px");
		expect(menu.style.top).toBe("auto");
	});
	it("fits a narrow/short viewport instead of overflowing it", async () => {
		vi.stubGlobal("innerWidth", 280);
		vi.stubGlobal("innerHeight", 200);
		const menu = await open();
		expect(menu.style.width).toBe("264px");
		expect(menu.style.left).toBe("8px");
		expect(menu.style.maxHeight).toBe("88px");
	});
	it("follows viewport and anchor resizing", async () => {
		const menu = await open();
		rect = { left: 400, top: 100, bottom: 130, width: 400 };
		observer.callback();
		await nextTick();
		expect(menu.style.left).toBe("400px");
		expect(menu.style.width).toBe("400px");
		vi.stubGlobal("innerWidth", 600);
		window.dispatchEvent(new Event("resize"));
		await nextTick();
		expect(menu.style.left).toBe("192px");
	});
	it("follows entrance transforms without depending on resize or scroll events", async () => {
		const menu = await open();
		rect = { ...rect, left: 240 };
		tick();
		await nextTick();
		expect(menu.style.left).toBe("240px");
		const calls = document.elementFromPoint.mock.calls.length;
		tick();
		expect(document.elementFromPoint).toHaveBeenCalledTimes(calls);
	});
	it("keeps scrolling usable and hides only while the field is clipped", async () => {
		const menu = await open();
		menu.dispatchEvent(new Event("scroll"));
		expect(wrapper.emitted("close")).toBeUndefined();
		// Focus scrolling during panel entrance must reposition, not dismiss.
		rect = { ...rect, left: rect.left - 20 };
		document.body.dispatchEvent(new Event("scroll"));
		expect(wrapper.emitted("close")).toBeUndefined();
		await nextTick();
		expect(menu.style.left).toBe("80px");
		document.elementFromPoint.mockReturnValue(document.body);
		document.body.dispatchEvent(new Event("scroll"));
		await nextTick();
		expect(menu.style.visibility).toBe("hidden");
		document.elementFromPoint.mockReturnValue(anchor);
		document.body.dispatchEvent(new Event("scroll"));
		await nextTick();
		expect(menu.style.visibility).toBe("visible");
	});
	it("cleans up the portal, observer and global handlers when the panel closes", async () => {
		await open();
		const remove = vi.spyOn(window, "removeEventListener");
		wrapper.unmount();
		expect(document.querySelector(".jv-action-linkmenu")).toBeNull();
		expect(observer.disconnect).toHaveBeenCalledOnce();
		expect(cancelAnimationFrame).toHaveBeenCalledWith(1);
		expect(remove).toHaveBeenCalledWith("resize", expect.any(Function));
		expect(remove).toHaveBeenCalledWith("scroll", expect.any(Function), true);
		wrapper = null;
	});
});
