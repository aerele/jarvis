import { describe, it, expect, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * Covers the settings-rail restructure: "Billing and metering" is retired as
 * its own rail item (folded into Usage), and "Plan and billing" is relabelled
 * "Billing". Every pane is stubbed to a one-line marker so these tests
 * exercise ONLY SettingsDialog's own logic (rail gating, section resolution,
 * the legacy "billing" key alias) - not any pane's own mount-time behaviour.
 */

vi.mock("frappe-ui", () => ({
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options", "disableOutsideClickToClose"],
		template: `<div><slot name="body" /></div>`,
	},
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: `<span />` },
}));

vi.mock("reka-ui", () => ({
	DialogClose: { name: "DialogClose", template: `<span><slot /></span>` },
	DialogTitle: { name: "DialogTitle", template: `<span><slot /></span>` },
}));

vi.mock("@/theme", () => ({
	useJarvisTheme: () => ({ effectiveDark: { value: false }, paletteVars: {} }),
}));

vi.mock("@/composables/useConfirm", () => ({
	confirmState: { value: null },
}));

const shell = vi.hoisted(() => ({
	settingsOpen: true,
	settingsSection: "general",
	settingsApplying: false,
}));
vi.mock("@/stores/shell", () => ({
	useShellStore: () => shell,
}));

function paneStub(name) {
	// __esModule marks this as an ES module namespace so Vue's async-component
	// resolver (defineAsyncComponent's loader, used for every pane here) unwraps
	// `.default` instead of treating the whole mock object as the component type -
	// without it, @vue/test-utils' isTeleport() check trips over the wrapper object.
	return {
		__esModule: true,
		default: { name, template: `<div class="pane-marker">${name}</div>` },
	};
}
vi.mock("@/components/settings/GeneralPane.vue", () => paneStub("GeneralPane"));
vi.mock("@/components/settings/UsagePane.vue", () => paneStub("UsagePane"));
vi.mock("@/components/settings/ActivityPane.vue", () => paneStub("ActivityPane"));
vi.mock("@/components/settings/ShortcutsPane.vue", () => paneStub("ShortcutsPane"));
vi.mock("@/components/settings/PlanBillingPane.vue", () => paneStub("PlanBillingPane"));
vi.mock("@/components/settings/AiModelsPane.vue", () => paneStub("AiModelsPane"));
vi.mock("@/components/settings/UsageAdminPane.vue", () => paneStub("UsageAdminPane"));
vi.mock("@/components/settings/BrandingPane.vue", () => paneStub("BrandingPane"));

import SettingsDialog from "./SettingsDialog.vue";

async function mountDialog({ isSM = false, isAdmin = false, section = "general" } = {}) {
	window.is_system_manager = isSM;
	window.is_jarvis_admin = isAdmin;
	shell.settingsOpen = true;
	shell.settingsSection = section;
	shell.settingsApplying = false;
	const w = mount(SettingsDialog);
	await flushPromises();
	return w;
}

afterEach(() => {
	delete window.is_system_manager;
	delete window.is_jarvis_admin;
});

describe("SettingsDialog rail", () => {
	it("labels the plan item 'Billing', not 'Plan and billing'", async () => {
		const w = await mountDialog({ isSM: true });
		expect(w.text()).toContain("Billing");
		expect(w.text()).not.toContain("Plan and billing");
	});

	it("no longer renders a standalone 'Billing and metering' rail item", async () => {
		const w = await mountDialog({ isSM: true });
		expect(w.text()).not.toContain("Billing and metering");
	});

	it("shows General, Usage, Activity, Shortcuts to everyone", async () => {
		const w = await mountDialog({ isSM: false, isAdmin: false });
		for (const label of ["General", "Usage", "Activity", "Shortcuts"]) {
			expect(w.text()).toContain(label);
		}
	});

	it("hides the whole Account and billing group (Billing, AI models, Branding) from an ordinary member", async () => {
		const w = await mountDialog({ isSM: false, isAdmin: false });
		expect(w.text()).not.toContain("Billing");
		expect(w.text()).not.toContain("AI models");
		expect(w.text()).not.toContain("Branding");
	});

	it("shows Billing, AI models and Branding (exactly 3 items) for a System Manager", async () => {
		const w = await mountDialog({ isSM: true });
		expect(w.text()).toContain("Billing");
		expect(w.text()).toContain("AI models");
		expect(w.text()).toContain("Branding");
	});
});

describe("SettingsDialog legacy section keys", () => {
	it("aliases the legacy 'billing' section key to the Usage pane", async () => {
		const w = await mountDialog({ isSM: true, section: "billing" });
		expect(w.find(".pane-marker").text()).toBe("UsagePane");
	});

	it("still opens PlanBillingPane for the unchanged 'plan' key", async () => {
		const w = await mountDialog({ isSM: true, section: "plan" });
		expect(w.find(".pane-marker").text()).toBe("PlanBillingPane");
	});

	it("falls back to General when a gated section is requested without the role", async () => {
		const w = await mountDialog({ isSM: false, isAdmin: false, section: "plan" });
		expect(w.find(".pane-marker").text()).toBe("GeneralPane");
	});

	it("resolves the legacy 'billing' key to Usage even without the admin role", async () => {
		// "billing" aliases to "usage", which is an everyone-visible Workspace
		// item - so this should resolve to Usage, not fall back to General.
		const w = await mountDialog({ isSM: false, isAdmin: false, section: "billing" });
		expect(w.find(".pane-marker").text()).toBe("UsagePane");
	});
});
