import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";

// SupportShell is now a 3-region layout: SupportSidebar + a main column (a
// frappe-ui Breadcrumbs bar over the content). SupportSidebar is stubbed (it has
// its own suite + pulls the router); only Breadcrumbs is needed from frappe-ui.
// matchMedia is absent in jsdom (theme.js needs it).
vi.mock("frappe-ui", () => ({
	Breadcrumbs: { name: "Breadcrumbs", props: ["items"], template: "<nav class='crumbs' />" },
}));

// UserMenu (reused in SupportSidebar) pulls theme.js/frappe-ui at import time — module-mock it (own suite).
vi.mock("@/components/shell/UserMenu.vue", () => ({
	default: { name: "UserMenu", template: "<div/>" },
}));
import SupportShell from "@/components/support/SupportShell.vue";
import SupportSidebar from "@/components/support/SupportSidebar.vue";
import { useJarvisTheme, DARK_VARS } from "@/theme";

const stubs = { SupportSidebar: true };

beforeEach(() => {
	vi.stubGlobal("matchMedia", () => ({
		matches: false,
		addEventListener() {},
		removeEventListener() {},
	}));
});

describe("SupportShell", () => {
	it("root is .jv-sup, NOT a jv-root palette surface (the rail + bar are frappe-ui-tokened)", () => {
		const w = mount(SupportShell, { global: { stubs } });
		expect(w.element.classList.contains("jv-sup")).toBe(true);
		expect(w.element.classList.contains("jv-root")).toBe(false);
	});

	it("renders the nav rail and feeds the crumbs to Breadcrumbs", () => {
		const crumbs = [{ label: "Support", route: { name: "Support" } }, { label: "#42" }];
		const w = mount(SupportShell, { props: { crumbs }, global: { stubs } });
		expect(w.findComponent(SupportSidebar).exists()).toBe(true);
		expect(w.findComponent({ name: "Breadcrumbs" }).props("items")).toEqual(crumbs);
	});

	it("renders the actions slot and the default (center) slot", () => {
		const w = mount(SupportShell, {
			slots: {
				actions: '<button class="probe">Resolve</button>',
				default: '<div class="body-probe" />',
			},
			global: { stubs },
		});
		expect(w.find(".probe").exists()).toBe(true);
		expect(w.find(".jv-sup-center .body-probe").exists()).toBe(true);
	});

	it("renders the aside slot ONLY when provided (thread), not by default (list/new)", () => {
		const without = mount(SupportShell, { global: { stubs } });
		expect(without.find(".jv-sup-aside").exists()).toBe(false);

		const withAside = mount(SupportShell, {
			props: { chatSurface: true },
			slots: { aside: '<div class="panel-probe" />' },
			global: { stubs },
		});
		expect(withAside.find(".jv-sup-aside .panel-probe").exists()).toBe(true);
	});

	it("does NOT apply jv-root to the content when it is not a chat surface (the list page)", () => {
		// The list page must render bare so ListPage's frappe-ui search keeps its stock
		// look instead of the chat forms reset.
		const w = mount(SupportShell, {
			slots: { default: '<div class="body-probe" />' },
			global: { stubs },
		});
		const content = w.find(".jv-sup-content");
		expect(content.exists()).toBe(true);
		expect(content.classes()).not.toContain("jv-root");
	});

	it("applies jv-root + palette vars to the content when chatSurface is set (thread/new)", () => {
		// The chat Composer/Message + details panel need the inline palette vars, so
		// the content row must be a jv-root carrying them.
		const w = mount(SupportShell, {
			props: { chatSurface: true },
			slots: { default: '<div class="body-probe" />' },
			global: { stubs },
		});
		const content = w.find(".jv-sup-content");
		expect(content.classes()).toContain("jv-root");
		expect(content.element.style.getPropertyValue("--surface")).toBeTruthy();
		expect(content.element.style.getPropertyValue("--cta")).toBeTruthy();
		expect(content.find(".body-probe").exists()).toBe(true);
	});

	it("flips the chat surface to dark when the shared theme ref changes", async () => {
		const { theme } = useJarvisTheme();
		const w = mount(SupportShell, {
			props: { chatSurface: true },
			slots: { default: "<div />" },
			global: { stubs },
		});
		try {
			theme.value = "dark";
			await nextTick();
			const content = w.find(".jv-sup-content");
			expect(content.classes()).toContain("jv-dark");
			expect(content.element.style.getPropertyValue("--surface")).toBe(
				DARK_VARS["--surface"]
			);
		} finally {
			theme.value = "system";
			await nextTick();
		}
	});
});
