import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const api = vi.hoisted(() => ({
	getDirectSubscriptionStatus: vi.fn(),
	getSubscriptionNotice: vi.fn(),
}));
vi.mock("@/api", () => api);
vi.mock("frappe-ui", () => ({
	call: vi.fn(),
	toast: { error: vi.fn(), success: vi.fn() },
	Button: { name: "Button", props: ["label"], template: "<button>{{ label }}</button>" },
}));
vi.mock("@/components/LlmPoolEditor.vue", () => ({
	default: {
		name: "LlmPoolEditor",
		props: ["reconnectRef", "expiredEntries", "directStatus", "editable", "hostScrim"],
		emits: ["reconnect-handled", "saved", "direct-changed"],
		template: `<div class="editor" :data-ref="reconnectRef"></div>`,
	},
}));
vi.mock("@/components/settings/SettingsPane.vue", () => ({
	default: { name: "SettingsPane", template: "<div><slot /></div>" },
}));
vi.mock("@/components/JvSpinner.vue", () => ({ default: { template: "<span/>" } }));

import AiModelsPane from "./AiModelsPane.vue";
import { useShellStore } from "@/stores/shell";

beforeEach(() => {
	useShellStore().settingsIntent = null;
	window.is_system_manager = true;
	api.getDirectSubscriptionStatus.mockResolvedValue({ is_direct_subscription: false });
	api.getSubscriptionNotice.mockResolvedValue({ expired: [], upstreams: [] });
});

// The shell store is a module singleton: a pane left mounted would also consume the intent.
const mounted = [];
const mountPane = () => {
	const w = mount(AiModelsPane);
	mounted.push(w);
	return w;
};
afterEach(() => mounted.splice(0).forEach((w) => w.unmount()));

describe("AiModelsPane reconnect intent", () => {
	it("reads an intent left before it mounted", async () => {
		const store = useShellStore();
		store.settingsIntent = { reconnect: "A1" };
		const w = mountPane();
		await flushPromises();
		expect(w.find(".editor").attributes("data-ref")).toBe("A1");
		expect(store.settingsIntent).toBeNull();
	});

	it("consumes and clears an intent that arrives while the pane is already open", async () => {
		const store = useShellStore();
		const w = mountPane();
		await flushPromises();
		expect(w.find(".editor").attributes("data-ref")).toBe("");
		store.settingsIntent = { reconnect: "A2" };
		await flushPromises();
		expect(w.find(".editor").attributes("data-ref")).toBe("A2");
		expect(store.settingsIntent).toBeNull();
	});
});
