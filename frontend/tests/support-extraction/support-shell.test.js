import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";

// SupportShell now paints its bar in frappe-ui tokens and reuses frappe-ui's
// Breadcrumbs + Button. Importing the real frappe-ui entry pulls resource modules
// that don't resolve under vitest, so stub the two components we use. useRouter is
// injection-based (module-mock it); matchMedia is absent in jsdom (theme.js needs
// it).
vi.mock("vue-router", () => ({ useRouter: () => ({ push }) }));
const push = vi.fn();
vi.mock("frappe-ui", () => ({
	Breadcrumbs: { name: "Breadcrumbs", props: ["items"], template: "<nav class='crumbs' />" },
	Button: { name: "Button", props: ["label", "icon", "variant"], template: "<button />" },
}));

import SupportShell from "@/components/support/SupportShell.vue";
import { useJarvisTheme, DARK_VARS } from "@/theme";

const stubs = { JarvisMark: true };

beforeEach(() => {
	push.mockClear();
	vi.stubGlobal("matchMedia", () => ({
		matches: false,
		addEventListener() {},
		removeEventListener() {},
	}));
});

describe("SupportShell", () => {
	it("paints a frappe-ui bar — the root is .jv-sup, NOT a jv-root palette surface", () => {
		// The bar matches the app header (surface-white / ink-gray), so the chat
		// palette must NOT be imposed on the shell root — that would drag the reused
		// ListPage controls under the index.css forms reset.
		const w = mount(SupportShell, { global: { stubs } });
		expect(w.element.classList.contains("jv-sup")).toBe(true);
		expect(w.element.classList.contains("jv-root")).toBe(false);
	});

	it("feeds the crumbs to Breadcrumbs and carries a home button back to Jarvis", async () => {
		const crumbs = [{ label: "Support", route: { name: "Support" } }, { label: "#42" }];
		const w = mount(SupportShell, { props: { crumbs }, global: { stubs } });
		expect(w.findComponent({ name: "Breadcrumbs" }).props("items")).toEqual(crumbs);

		const home = w.find(".jv-sup-home");
		expect(home.attributes("aria-label")).toBe("Back to Jarvis");
		await home.trigger("click");
		expect(push).toHaveBeenCalledWith({ name: "Chat" });
	});

	it("renders the Open-Desk button and the actions slot", () => {
		const w = mount(SupportShell, {
			slots: { actions: '<button class="probe">Resolve</button>' },
			global: { stubs },
		});
		expect(w.find(".jv-deskbtn").exists()).toBe(true);
		expect(w.find(".probe").exists()).toBe(true);
	});

	it("does NOT wrap the body in a jv-root when it is not a chat surface (the list page)", () => {
		// The default (list) page must render bare so ListPage's frappe-ui search
		// keeps its stock look instead of the chat forms reset.
		const w = mount(SupportShell, {
			slots: { default: '<div class="body-probe" />' },
			global: { stubs },
		});
		expect(w.find(".jv-sup-body .jv-root").exists()).toBe(false);
		expect(w.find(".body-probe").exists()).toBe(true);
	});

	it("wraps the body in a jv-root palette surface when chatSurface is set (thread/new)", () => {
		// Regression guard for the invariant that fails SILENTLY: the chat
		// Composer/Message need the inline palette vars + the forms reset, so
		// thread/new must render inside a jv-root carrying both.
		const w = mount(SupportShell, {
			props: { chatSurface: true },
			slots: { default: '<div class="body-probe" />' },
			global: { stubs },
		});
		const root = w.find(".jv-sup-body .jv-root");
		expect(root.exists()).toBe(true);
		expect(root.element.style.getPropertyValue("--surface")).toBeTruthy();
		expect(root.element.style.getPropertyValue("--cta")).toBeTruthy();
		expect(root.find(".body-probe").exists()).toBe(true);
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
			const root = w.find(".jv-sup-body .jv-root");
			expect(root.element.classList.contains("jv-dark")).toBe(true);
			expect(root.element.style.getPropertyValue("--surface")).toBe(DARK_VARS["--surface"]);
		} finally {
			theme.value = "system";
			await nextTick();
		}
	});
});
