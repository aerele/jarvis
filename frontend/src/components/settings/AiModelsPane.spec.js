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
		// The real editor exposes `loaded`: true once its first load settles.
		data: () => ({ loaded: true }),
		template: `<div class="editor" :data-ref="reconnectRef"></div>`,
	},
}));
vi.mock("@/components/settings/SettingsPane.vue", () => ({
	default: { name: "SettingsPane", template: "<div><slot /></div>" },
}));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { template: `<span class="stub-spinner"/>` },
}));

import AiModelsPane from "./AiModelsPane.vue";
import { useShellStore } from "@/stores/shell";

beforeEach(() => {
	useShellStore().settingsIntent = null;
	window.is_system_manager = true;
	window.is_jarvis_admin = false;
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

describe("AiModelsPane connection probe error", () => {
	const wrap = (message) => JSON.stringify([JSON.stringify({ message })]);
	const failWith = async (err) => {
		api.getDirectSubscriptionStatus.mockRejectedValueOnce(err);
		const w = mountPane();
		await flushPromises();
		return w.vm.directSubErr;
	};

	it("keeps the server's sentence as text, not the raw payload", async () => {
		const err = { _server_messages: wrap("<b>Plan</b> unavailable") };
		expect(await failWith(err)).toBe("Plan unavailable");
	});

	it("prefers the error's own message, and falls back when there is none", async () => {
		expect(await failWith(new Error("Timed out"))).toBe("Timed out");
		expect(await failWith({ _server_messages: "not json" })).toBe(
			"Couldn't load your AI connection."
		);
	});
});

describe("AiModelsPane first load", () => {
	const pending = () => new Promise(() => {});
	const editor = (w) => w.findComponent({ name: "LlmPoolEditor" });
	const fill = (w) => w.find(".jv-pane-fill");

	it("mounts the editor while the probe is in flight, hidden behind one spinner", async () => {
		api.getDirectSubscriptionStatus.mockImplementation(pending);
		const w = mountPane();
		await flushPromises();

		expect(editor(w).exists()).toBe(true);
		expect(fill(w).attributes("style")).toContain("display: none");
		expect(w.findAll(".stub-spinner")).toHaveLength(1);
		expect(w.text()).not.toContain("Loading");
	});

	it("keeps the spinner until the probe AND the editor have both loaded", async () => {
		const w = mountPane();
		editor(w).vm.loaded = false;
		await flushPromises(); // the probe has answered
		expect(w.find(".stub-spinner").exists()).toBe(true);

		editor(w).vm.loaded = true;
		await flushPromises();
		expect(w.find(".stub-spinner").exists()).toBe(false);
		expect(fill(w).attributes("style") || "").not.toContain("display: none");
	});

	it("does not bring the spinner back on a background re-probe", async () => {
		const w = mountPane();
		await flushPromises();
		api.getDirectSubscriptionStatus.mockImplementation(pending);
		editor(w).vm.$emit("direct-changed");
		await flushPromises();

		expect(w.find(".stub-spinner").exists()).toBe(false);
		expect(editor(w).exists()).toBe(true);
	});

	it("shows Retry, not a spinner, when the first probe fails", async () => {
		api.getDirectSubscriptionStatus.mockRejectedValue(new Error("boom"));
		const w = mountPane();
		await flushPromises();

		expect(w.text()).toContain("Retry");
		expect(w.find(".stub-spinner").exists()).toBe(false);
	});

	it("keeps the editor mounted through a failed first probe and its Retry", async () => {
		api.getDirectSubscriptionStatus.mockRejectedValueOnce(new Error("boom"));
		const w = mountPane();
		await flushPromises();
		expect(w.text()).toContain("Retry");
		expect(editor(w).exists()).toBe(true);
		expect(fill(w).attributes("style")).toContain("display: none");
		const before = editor(w).vm;

		await w.vm.loadDirectSub();
		await flushPromises();
		expect(editor(w).vm).toBe(before);
		expect(fill(w).attributes("style") || "").not.toContain("display: none");
	});

	it("does not strand a non-admin on the spinner (no probe runs)", async () => {
		window.is_system_manager = false;
		window.is_jarvis_admin = false;
		// This spec never clears call counts between tests.
		api.getDirectSubscriptionStatus.mockClear();
		const w = mountPane();
		await flushPromises();

		expect(api.getDirectSubscriptionStatus).not.toHaveBeenCalled();
		expect(w.find(".stub-spinner").exists()).toBe(false);
	});
});
