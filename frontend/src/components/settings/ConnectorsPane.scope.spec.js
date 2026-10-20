import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// The Add button under each section decides who the new connector is for.
const api = vi.hoisted(() => ({
	deleteConnector: vi.fn(),
	listConnectors: vi.fn(),
	testConnector: vi.fn(),
	updateConnector: vi.fn(),
}));
vi.mock("@/api", () => api);
vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label"],
		emits: ["click"],
		template: `<button :data-label="label" @click="$emit('click')">{{ label }}</button>`,
	},
	FeatherIcon: { name: "FeatherIcon", template: `<span />` },
	TabButtons: { name: "TabButtons", props: ["buttons", "modelValue"], template: `<span />` },
	confirmDialog: vi.fn(),
	toast: { error: vi.fn(), success: vi.fn() },
}));
vi.mock("@/components/JvSpinner.vue", () => ({ default: { template: `<span />` } }));
vi.mock("@/components/settings/SettingsPane.vue", () => ({
	default: {
		name: "SettingsPane",
		template: `<div><slot name="actions" /><slot /></div>`,
	},
}));
vi.mock("@/components/settings/ConnectorRow.vue", () => ({
	default: { name: "ConnectorRow", template: `<div />` },
}));
vi.mock("@/components/settings/ConnectorDirectory.vue", () => ({
	default: {
		name: "ConnectorDirectory",
		emits: ["add", "add-custom"],
		template: `<button class="card" @click="$emit('add', 'GitHub')">GitHub</button>`,
	},
}));
vi.mock("@/components/settings/AddConnectorDialog.vue", () => ({
	default: {
		name: "AddConnectorDialog",
		props: ["modelValue", "scope", "preset"],
		template: `<div class="dialog" :data-open="modelValue" :data-scope="scope" />`,
	},
}));

import ConnectorsPane from "./ConnectorsPane.vue";
import { useShellStore } from "@/stores/shell";

const byLabel = (wrapper, label) => wrapper.findAll(`button[data-label="${label}"]`);

async function mountPane() {
	const wrapper = mount(ConnectorsPane);
	await flushPromises();
	return wrapper;
}

// Add under Shared is the first "Add connector", Mine's is the second.
async function addFromSection(wrapper, index) {
	await byLabel(wrapper, "Add connector")[index].trigger("click");
	await wrapper.find(".card").trigger("click");
	return wrapper.find(".dialog");
}

describe("ConnectorsPane: scope of a new connector", () => {
	beforeEach(() => {
		useShellStore().settingsIntent = null;
		window.is_system_manager = true;
		window.is_jarvis_admin = false;
		api.listConnectors.mockResolvedValue({ shared: [], mine: [], catalog: [] });
	});
	afterEach(() => {
		delete window.is_system_manager;
		delete window.is_jarvis_admin;
	});

	it("Add under Shared opens the dialog with Everyone", async () => {
		const wrapper = await mountPane();
		const dialog = await addFromSection(wrapper, 0);
		expect(dialog.attributes("data-open")).toBe("true");
		expect(dialog.attributes("data-scope")).toBe("Shared");
	});

	it("Add under Mine opens the dialog with Only me", async () => {
		const wrapper = await mountPane();
		const dialog = await addFromSection(wrapper, 1);
		expect(dialog.attributes("data-scope")).toBe("Personal");
	});

	it("a plain user only has the Mine button and gets Only me", async () => {
		window.is_system_manager = false;
		const wrapper = await mountPane();
		expect(byLabel(wrapper, "Add connector")).toHaveLength(1);
		const dialog = await addFromSection(wrapper, 0);
		expect(dialog.attributes("data-scope")).toBe("Personal");
	});
});
