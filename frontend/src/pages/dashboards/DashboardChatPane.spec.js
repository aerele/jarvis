import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis#884 - dashboard builds no longer land as a canvas artifact; the new
 * save_dashboard tool saves a Jarvis Dashboard row directly and publishes a
 * realtime {kind: "dashboard", conversation_id, name} frame on the SAME
 * jarvis:event channel every other turn frame already rides. This pins that
 * the pane recognises the new frame kind, gates it on OUR conversation the
 * same way every other frame here does, and bubbles it up as emit("dashboard",
 * {name}) for the page to load.
 */

// Deterministic per-user storage keys without touching real localStorage
// (WikiTab.spec.js precedent: jsdom's localStorage is not reliably usable
// here). The pane's conversation slot is seeded to a known id up front so a
// realtime frame naming it passes the "OUR conversation" gate.
vi.mock("@vueuse/core", async (importOriginal) => {
	const actual = await importOriginal();
	const { ref } = await import("vue");
	return {
		...actual,
		useStorage: (key, initial) => ref(key.startsWith("jarvis-dash-conv-") ? "conv1" : initial),
	};
});

vi.mock("@/data/session", () => ({ session: { user: "u@x.com" } }));
vi.mock("@/branding", () => ({ agentName: "Jarvis" }));

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "disabled", "loading", "variant", "iconLeft"],
		emits: ["click"],
		template: `<button :disabled="disabled" @click="$emit('click')">{{ label }}</button>`,
	},
	FeatherIcon: { name: "FeatherIcon", template: "<i />" },
	TabButtons: { name: "TabButtons", props: ["buttons", "modelValue"], template: "<div />" },
	toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

vi.mock("@/components/JvSpinner.vue", () => ({
	default: { name: "JvSpinner", template: "<i />" },
}));
vi.mock("@/components/VoiceRecorder.vue", () => ({
	default: { name: "VoiceRecorder", template: "<i />" },
}));
vi.mock("@/components/chat/AskCard.vue", () => ({
	default: { name: "AskCard", template: "<div />" },
}));
vi.mock("@/markdown", () => ({ renderMarkdown: (s) => s }));

const api = vi.hoisted(() => ({
	sendDashboardChat: vi.fn(async () => ({ ok: true, conversation_id: "conv1" })),
	getDashboardConversation: vi.fn(async () => ({ messages: [] })),
}));
vi.mock("@/api/dashboards", () => api);
vi.mock("@/api", () => ({
	listPendingConfirmations: vi.fn(async () => ({ ok: true, data: { pending: [] } })),
	confirmTool: vi.fn(),
	dismissTool: vi.fn(),
}));

import DashboardChatPane from "./DashboardChatPane.vue";

// useJarvisTheme() (AskCard's paletteVars binding) reads the OS color scheme
// on its first call; jsdom has no matchMedia at all.
window.matchMedia =
	window.matchMedia ||
	(() => ({
		matches: false,
		addEventListener: vi.fn(),
		removeEventListener: vi.fn(),
	}));

function fakeSocket() {
	let handler = null;
	return {
		on: vi.fn((event, fn) => {
			if (event === "jarvis:event") handler = fn;
		}),
		off: vi.fn(),
		fire(payload) {
			handler && handler(payload);
		},
	};
}

function mountPane() {
	const socket = fakeSocket();
	const wrapper = mount(DashboardChatPane, {
		global: { provide: { $socket: socket } },
	});
	return { wrapper, socket };
}

describe('DashboardChatPane realtime: kind="dashboard"', () => {
	beforeEach(() => {
		api.sendDashboardChat.mockClear();
		api.getDashboardConversation.mockClear();
	});

	it("emits dashboard with the saved row's name for OUR conversation", async () => {
		const { wrapper, socket } = mountPane();
		await flushPromises();
		socket.fire({ kind: "dashboard", conversation_id: "conv1", name: "DASH-001" });
		await flushPromises();
		expect(wrapper.emitted("dashboard")).toEqual([[{ name: "DASH-001" }]]);
	});

	it("ignores a dashboard frame for a DIFFERENT conversation", async () => {
		const { wrapper, socket } = mountPane();
		await flushPromises();
		socket.fire({ kind: "dashboard", conversation_id: "some-other-conv", name: "DASH-002" });
		await flushPromises();
		expect(wrapper.emitted("dashboard")).toBeUndefined();
	});

	it("never schedules a transcript refetch for a dashboard frame (canvas precedent)", async () => {
		const { wrapper, socket } = mountPane();
		await flushPromises();
		api.getDashboardConversation.mockClear();
		socket.fire({ kind: "dashboard", conversation_id: "conv1", name: "DASH-003" });
		// scheduleRefetch debounces 300ms; give it a chance to fire and confirm it never does
		await new Promise((r) => setTimeout(r, 350));
		expect(api.getDashboardConversation).not.toHaveBeenCalled();
	});
});
