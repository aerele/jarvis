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
		props: [
			"label",
			"tooltip",
			"disabled",
			"loading",
			"variant",
			"icon",
			"iconLeft",
			"iconRight",
		],
		emits: ["click"],
		template: `<button :disabled="disabled" @click="$emit('click')">{{ label }}</button>`,
	},
	Dropdown: { name: "Dropdown", props: ["options"], template: "<div><slot /></div>" },
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
vi.mock("@/components/chat/ContextRing.vue", () => ({
	default: {
		name: "ContextRing",
		props: ["context", "compacting", "compacted"],
		template: "<div />",
	},
}));
vi.mock("@/components/chat/CompactDialog.vue", () => ({
	default: { name: "CompactDialog", template: "<div />" },
}));
vi.mock("@/components/chat/ModelEffortPicker.vue", () => ({
	default: { name: "ModelEffortPicker", template: "<div />" },
}));
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ openSettings: vi.fn() }) }));
vi.mock("@/markdown", () => ({ renderMarkdown: (s) => s }));

const api = vi.hoisted(() => ({
	sendDashboardChat: vi.fn(async () => ({ ok: true, conversation_id: "conv1" })),
	getDashboardConversation: vi.fn(async () => ({ messages: [] })),
	listDashboardConversations: vi.fn(async () => ({ rows: [] })),
}));
vi.mock("@/api/dashboards", () => api);
vi.mock("@/api", () => ({
	listPendingConfirmations: vi.fn(async () => ({ ok: true, data: { pending: [] } })),
	confirmTool: vi.fn(),
	dismissTool: vi.fn(),
	getChatUiSettings: vi.fn(async () => ({})),
	setConversationModel: vi.fn(async () => ({ ok: true })),
	setConversationThinking: vi.fn(async () => ({ ok: true })),
	getConversationContext: vi.fn(async () => null),
	compactConversation: vi.fn(async () => ({ ok: true })),
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

describe("DashboardChatPane surfaces a run that failed before its first token", () => {
	// The transcript row for such a run has EMPTY content and the reason in
	// `error`. Filtering bubbles on content alone dropped it, so a rate-limited
	// or failed build showed nothing at all and the composer silently unlocked.
	it("renders the error reason for an empty errored assistant row", async () => {
		localStorage.setItem("jarvis-dash-conv-u@x.com", "conv1");
		api.getDashboardConversation.mockResolvedValueOnce({
			conversation: { name: "conv1" },
			messages: [
				{ name: "m1", role: "user", content: "Build me a dashboard" },
				{
					name: "m2",
					role: "assistant",
					content: "",
					error: "API rate limit reached. Please try again later.",
				},
			],
		});
		const { wrapper } = mountPane();
		await flushPromises();
		const note = wrapper.find(".text-ink-red-4");
		expect(note.exists()).toBe(true);
		expect(note.text()).toContain("API rate limit reached");
		localStorage.removeItem("jarvis-dash-conv-u@x.com");
	});
});

describe("DashboardChatPane history menu", () => {
	it("labels a never-titled thread as untitled, not with the 'New chat' placeholder", async () => {
		api.listDashboardConversations.mockResolvedValueOnce({
			rows: [
				{ name: "c-titled", title: "Receivables by customer" },
				{ name: "c-saved", title: "New chat", dashboard_title: "Cash dashboard" },
				{ name: "c-untitled", title: "New chat" },
			],
		});
		const { wrapper } = mountPane();
		await flushPromises();
		const labels = wrapper
			.findComponent({ name: "Dropdown" })
			.props("options")
			.map((o) => o.label);
		expect(labels).toEqual([
			"Receivables by customer",
			"Cash dashboard",
			"Untitled dashboard chat",
		]);
	});
});
