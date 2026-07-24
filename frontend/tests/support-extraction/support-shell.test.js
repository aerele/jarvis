import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";

// SupportShell calls useJarvisTheme() → _start() → matchMedia, which jsdom does
// not implement (Constraint 16). And useRouter() is injection-based, so it has
// to be module-mocked — global.mocks.$router feeds it nothing.
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import SupportShell from "@/components/support/SupportShell.vue";
import { useJarvisTheme, DARK_VARS } from "@/theme";

const stubs = { JarvisMark: true };

beforeEach(() => {
	vi.stubGlobal("matchMedia", () => ({
		matches: false,
		addEventListener() {},
		removeEventListener() {},
	}));
});

describe("SupportShell", () => {
	it("carries ALL THREE palette hooks — jv-root, the palette vars, and jv-dark", () => {
		// Regression guard for the invariant that fails SILENTLY: dropping jv-root
		// still renders colors (they come from the inline vars) but kills
		// color-scheme, ::placeholder and the forms reset Composer relies on.
		const w = mount(SupportShell, { global: { stubs } });
		const root = w.element;
		expect(root.classList.contains("jv-root")).toBe(true);
		expect(root.style.getPropertyValue("--surface")).toBeTruthy();
		expect(root.style.getPropertyValue("--cta")).toBeTruthy();
	});

	it("renders the title and the actions slot", () => {
		const w = mount(SupportShell, {
			props: { title: "Ticket #7" },
			slots: { actions: '<button class="probe">Resolve</button>' },
			global: { stubs },
		});
		expect(w.text()).toContain("Ticket #7");
		expect(w.find(".probe").exists()).toBe(true);
	});

	it("flips to dark when the shared theme ref changes — jv-dark AND the inline palette both update", async () => {
		// Every test above mounts with the theme defaulting to "system" and
		// matchMedia stubbed to matches:false, so effectiveDark is false for the
		// whole file — a suite that never proves jv-dark stays green even if the
		// binding is deleted from the template. `theme` is a module-scope
		// singleton (src/theme.js), so flipping `theme.value` directly is the
		// real trigger a UserMenu/SettingsDialog toggle produces; setTheme()
		// additionally fires a dynamic import("@/api") this test has no reason to
		// exercise.
		const { theme } = useJarvisTheme();
		const w = mount(SupportShell, { global: { stubs } });
		try {
			theme.value = "dark";
			await nextTick();
			const root = w.element;
			expect(root.classList.contains("jv-dark")).toBe(true);
			expect(root.style.getPropertyValue("--surface")).toBe(DARK_VARS["--surface"]);
		} finally {
			// Restore the singleton so later tests in this file (and any file that
			// shares the module registry) still see the default light theme.
			theme.value = "system";
			await nextTick();
		}
	});
});
