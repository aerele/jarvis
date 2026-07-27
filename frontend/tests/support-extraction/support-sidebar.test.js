import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";

// SupportSidebar composes JarvisMark + SidebarLink (both stubbed) and reads the
// router. SidebarLink is stubbed to a prop-exposing button so we can assert the
// targets/active/collapse SupportSidebar passes, and drive its onClick.
let routePath = "/support";
const push = vi.fn();
vi.mock("vue-router", () => ({
	useRoute: () => ({
		get path() {
			return routePath;
		},
	}),
	useRouter: () => ({ push }),
}));

// Module-mock SidebarLink (not just a render stub): its real module imports
// frappe-ui's FeatherIcon/Tooltip, whose entry pulls resource modules that don't
// resolve under vitest. The prop-exposing stub lets us assert what SupportSidebar
// passes and drive onClick.
vi.mock("@/components/shell/SidebarLink.vue", () => ({
	default: {
		name: "SidebarLink",
		props: ["icon", "label", "to", "isCollapsed", "isActive", "onClick"],
		template:
			"<button class='slink' :data-label='label' @click=\"onClick && onClick()\">{{ label }}</button>",
	},
}));
// UserMenu (the reused chat user card) pulls frappe-ui's Dropdown at import time —
// module-mock it too (it has its own suite).
vi.mock("@/components/shell/UserMenu.vue", () => ({
	default: { name: "UserMenu", props: ["isCollapsed"], template: "<div class='user-menu'/>" },
}));

import SupportSidebar from "@/components/support/SupportSidebar.vue";
import SidebarLink from "@/components/shell/SidebarLink.vue"; // = the mock above

const mountSidebar = () => mount(SupportSidebar);
const links = (w) => w.findAllComponents(SidebarLink);
const linkBy = (w, label) => links(w).find((l) => l.props("label") === label);

beforeEach(() => {
	push.mockClear();
	routePath = "/support";
	try {
		localStorage.removeItem("jv-support-sidebar-collapsed");
	} catch {
		/* ignore */
	}
});

describe("SupportSidebar", () => {
	it("offers New ticket + Support tickets + Jarvis chat, each to the right route", () => {
		const w = mountSidebar();
		expect(linkBy(w, "New ticket").props("to")).toEqual({ name: "SupportNew" });
		expect(linkBy(w, "Support tickets").props("to")).toEqual({ name: "Support" });
		expect(linkBy(w, "Jarvis chat").props("to")).toEqual({ name: "Chat" });
	});

	it("marks 'Support tickets' active across support routes, not otherwise", () => {
		routePath = "/support/TCK-1";
		expect(linkBy(mountSidebar(), "Support tickets").props("isActive")).toBe(true);
		routePath = "/"; // (contrived — the rail only shows on support routes) pins the computed
		expect(linkBy(mountSidebar(), "Support tickets").props("isActive")).toBe(false);
	});

	it("renders the reused chat user card at the top", () => {
		expect(mountSidebar().find(".user-menu").exists()).toBe(true);
	});

	it("collapses/expands on the toggle and persists the choice", async () => {
		const w = mountSidebar();
		// starts expanded: labels shown, toggle says "Collapse"
		expect(linkBy(w, "Support tickets").props("isCollapsed")).toBe(false);
		expect(linkBy(w, "Collapse")).toBeTruthy();

		await linkBy(w, "Collapse").trigger("click");
		expect(linkBy(w, "Support tickets").props("isCollapsed")).toBe(true);
		expect(linkBy(w, "Expand")).toBeTruthy(); // label flipped
		expect(localStorage.getItem("jv-support-sidebar-collapsed")).toBe("1");

		await linkBy(w, "Expand").trigger("click");
		expect(linkBy(w, "Support tickets").props("isCollapsed")).toBe(false);
		expect(localStorage.getItem("jv-support-sidebar-collapsed")).toBe("0");
	});

	it("starts collapsed when the persisted choice says so", () => {
		localStorage.setItem("jv-support-sidebar-collapsed", "1");
		const w = mountSidebar();
		expect(linkBy(w, "Support tickets").props("isCollapsed")).toBe(true);
		expect(w.classes()).toContain("jv-supsb-collapsed");
	});
});
