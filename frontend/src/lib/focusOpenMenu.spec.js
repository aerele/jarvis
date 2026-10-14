import fs from "node:fs";
import path from "node:path";
import { describe, it, expect, vi, afterEach } from "vitest";
import { mount } from "@vue/test-utils";

/**
 * Menus take keyboard focus when they open: the shared helper, the sidebar user menu
 * (a library Dropdown) and the chat "Action" menu (a hand-made one).
 */

vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("frappe-ui", () => ({
	Dropdown: {
		name: "Dropdown",
		props: ["options"],
		emits: ["update:open"],
		template: '<div><slot name="trigger" :open="false" /></div>',
	},
	FeatherIcon: { template: "<i/>" },
}));
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ openSettings: vi.fn() }) }));
vi.mock("@/stores/support", () => ({ useSupportStore: () => ({ awaitingCount: 0 }) }));
vi.mock("@/branding", () => ({ brand: { agentName: "Jarvis" } }));
vi.mock("@/components/JarvisMark.vue", () => ({ default: { template: "<i/>" } }));
vi.mock("@/theme", async () => {
	const { ref } = await import("vue");
	return { useJarvisTheme: () => ({ effectiveDark: ref(false), toggleTheme: vi.fn() }) };
});
vi.mock("@/maintenanceGate", async () => {
	const { ref } = await import("vue");
	return { holdActive: ref(false) };
});

import { focusOpenMenu } from "./focusOpenMenu";
import UserMenu from "@/components/shell/UserMenu.vue";

const strays = [];
function drawMenu(attrs = { "data-reka-menu-content": "" }) {
	const el = document.createElement("div");
	for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
	el.tabIndex = -1;
	document.body.appendChild(el);
	strays.push(el);
	return el;
}

afterEach(() => {
	for (const el of strays.splice(0)) el.remove();
	vi.unstubAllGlobals();
});

describe("focusOpenMenu", () => {
	it("focuses the library's menu once it is drawn, not before", () => {
		const frames = [];
		vi.stubGlobal("requestAnimationFrame", (cb) => frames.push(cb));
		const menu = drawMenu();
		focusOpenMenu();
		expect(document.activeElement).not.toBe(menu);
		frames.forEach((cb) => cb());
		expect(document.activeElement).toBe(menu);
	});

	it("focuses what the selector names", () => {
		vi.stubGlobal("requestAnimationFrame", (cb) => cb());
		drawMenu();
		const item = drawMenu({ "data-create-item": "" });
		focusOpenMenu("[data-create-item]");
		expect(document.activeElement).toBe(item);
	});

	it("does nothing when there is no menu", () => {
		vi.stubGlobal("requestAnimationFrame", (cb) => cb());
		expect(() => focusOpenMenu()).not.toThrow();
	});
});

describe("sidebar user menu", () => {
	it("moves focus into the menu when it opens, and only then", () => {
		vi.stubGlobal("requestAnimationFrame", (cb) => cb());
		const menu = drawMenu();
		const w = mount(UserMenu, {
			global: {
				provide: { $session: { user: "a@b.c", logout: vi.fn() } },
			},
		});
		const dropdown = w.findComponent({ name: "Dropdown" });
		dropdown.vm.$emit("update:open", false);
		expect(document.activeElement).not.toBe(menu);
		dropdown.vm.$emit("update:open", true);
		expect(document.activeElement).toBe(menu);
		w.unmount();
	});
});

describe("chat Action menu", () => {
	// ChatView is too large to mount here: the wiring is read from its source.
	const src = fs.readFileSync(path.join(process.cwd(), "src/views/ChatView.vue"), "utf8");

	it("focuses its first item when it opens", () => {
		expect(src).toContain(
			'watch(createMenuOpen, (open) => open && focusOpenMenu("[data-create-item]"))'
		);
		const items = src.match(/data-create-item\n/g) || [];
		expect(items.length).toBe(2);
	});

	it("closes on Escape and gives focus back to its button", () => {
		expect(src).toContain('@keydown.esc.stop="closeCreateMenu"');
		expect(src).toContain('ref="createBtnEl"');
		expect(src).toMatch(
			/function closeCreateMenu\(\) \{\s+createMenuOpen\.value = false;\s+createBtnEl\.value\?\.focus\(\);/
		);
	});
});
