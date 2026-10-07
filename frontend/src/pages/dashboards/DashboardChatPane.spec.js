import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
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
// A getter, so a test can give the agent an admin-set name (an XSS probe).
const brand = vi.hoisted(() => ({ name: "Jarvis" }));
vi.mock("@/branding", () => ({
	get agentName() {
		return brand.name;
	},
}));

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
	retryMessage: vi.fn(async () => ({ ok: true, run_id: "r2" })),
}));

import {
	confirmTool,
	dismissTool,
	getConversationContext,
	listPendingConfirmations,
	retryMessage,
} from "@/api";
import { toast } from "frappe-ui";
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

// Every pane a test mounts is unmounted after it, so no timer of one test runs in the next.
const mounted = [];
afterEach(() => {
	while (mounted.length) mounted.pop().unmount();
});

function mountPane({ socket = fakeSocket() } = {}) {
	const wrapper = mount(DashboardChatPane, {
		global: { provide: { $socket: socket } },
	});
	mounted.push(wrapper);
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
		// The row renders the classified headline and guidance, not the raw
		// provider text; the raw text sits behind the inline Details toggle.
		expect(note.text()).toContain("The model’s request limit was reached");
		expect(note.text()).not.toContain("API rate limit reached");
		const details = note.findAll("button").find((b) => b.text() === "Details");
		expect(details).toBeTruthy();
		await details.trigger("click");
		expect(note.text()).toContain("API rate limit reached. Please try again later.");
		expect(note.find("button[aria-expanded='true']").text()).toBe("Hide details");
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

describe("DashboardChatPane typed replies to its parked cards (PR-3b)", () => {
	const cards = [
		{ token: "tok-late", conversation: "conv1", tool: "create_doc", created_at: 200 },
		{ token: "tok-early", conversation: "conv1", tool: "create_doc", created_at: 100 },
	];

	async function typeAndSend(wrapper, text) {
		const box = wrapper.find("textarea");
		await box.setValue(text);
		await box.trigger("keydown", { key: "Enter" });
		await flushPromises();
	}

	it("sends the cards' tokens in the order shown, like ChatView", async () => {
		listPendingConfirmations.mockResolvedValue({ ok: true, data: { pending: cards } });
		api.sendDashboardChat.mockClear();
		const { wrapper } = mountPane();
		await flushPromises();
		await typeAndSend(wrapper, "discard 1");
		expect(api.sendDashboardChat.mock.calls[0].at(-1)).toEqual(["tok-early", "tok-late"]);
		listPendingConfirmations.mockResolvedValue({ ok: true, data: { pending: [] } });
	});

	it("drops the card a typed no discarded", async () => {
		listPendingConfirmations.mockResolvedValueOnce({ ok: true, data: { pending: cards } });
		const { wrapper } = mountPane();
		await flushPromises();
		api.sendDashboardChat.mockResolvedValueOnce({
			ok: true,
			conversation_id: "conv1",
			typed_rejection: { discarded: [{ token: "tok-early", position: 1 }], skipped: [] },
		});
		await typeAndSend(wrapper, "discard 1");
		expect(wrapper.findAll("button").filter((b) => b.text() === "Approve")).toHaveLength(1);
	});

	it("never waits for a run a typed go-ahead did not start", async () => {
		listPendingConfirmations.mockResolvedValueOnce({ ok: true, data: { pending: cards } });
		const { wrapper } = mountPane();
		await flushPromises();
		api.sendDashboardChat.mockResolvedValueOnce({
			ok: true,
			confirmed: true,
			tokens: ["tok-early", "tok-late"],
			conversation_id: "conv1",
		});
		await typeAndSend(wrapper, "yes");
		expect(wrapper.findAll("button").filter((b) => b.text() === "Approve")).toHaveLength(0);
		// Not busy: the composer takes the next message straight away.
		api.sendDashboardChat.mockClear();
		await typeAndSend(wrapper, "thanks");
		expect(api.sendDashboardChat).toHaveBeenCalledTimes(1);
	});
});

// D3: the pane's Approve/Dismiss used to always show one generic "may have
// expired" toast on any ok:false besides a storage blip. Now that cards carry
// a reason_code (stale/target_missing/tampered/unverifiable, busy, …), reuse
// the SPA's chatCardActions mapping so the words - and the keep-vs-drop
// decision - match ChatView's, instead of a second, cruder copy here.
describe("DashboardChatPane shows the specific refusal reason (D3)", () => {
	const cards = [{ token: "tok1", conversation: "conv1", tool: "update_doc" }];

	it("settles a card as Failed with its specific reason, not the opaque guess", async () => {
		listPendingConfirmations.mockResolvedValueOnce({ ok: true, data: { pending: cards } });
		confirmTool.mockResolvedValueOnce({
			ok: false,
			reason_code: "stale",
			pa_status: "Failed",
			error: { type: "InvalidConfirmation" },
		});
		const { wrapper } = mountPane();
		await flushPromises();
		const approve = wrapper.findAll("button").find((b) => b.text() === "Approve");
		await approve.trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("record changed"));
		expect(wrapper.findAll("button").filter((b) => b.text() === "Approve")).toHaveLength(0);
	});

	it("keeps a busy card on screen and says so, instead of dropping it", async () => {
		listPendingConfirmations.mockResolvedValueOnce({ ok: true, data: { pending: cards } });
		confirmTool.mockResolvedValueOnce({ ok: false, reason_code: "busy" });
		const { wrapper } = mountPane();
		await flushPromises();
		const approve = wrapper.findAll("button").find((b) => b.text() === "Approve");
		await approve.trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith(
			"This action is being handled right now. Try again in a moment."
		);
		expect(wrapper.findAll("button").filter((b) => b.text() === "Approve")).toHaveLength(1);
	});

	it("dismiss shows the specific reason too", async () => {
		listPendingConfirmations.mockResolvedValueOnce({ ok: true, data: { pending: cards } });
		dismissTool.mockResolvedValueOnce({ ok: false, reason_code: "executing" });
		const { wrapper } = mountPane();
		await flushPromises();
		const dismiss = wrapper.findAll("button").find((b) => b.text() === "Dismiss");
		await dismiss.trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith("This action is already running.");
		expect(wrapper.findAll("button").filter((b) => b.text() === "Dismiss")).toHaveLength(1);
	});
});

describe("DashboardChatPane Retry on a failed reply", () => {
	const EMPTY_REPLY = "⚠️ Agent couldn't generate a response. Please try again.";
	const failed = (error = EMPTY_REPLY, after = []) => ({
		conversation: { name: "conv1" },
		messages: [
			{ name: "m1", role: "user", content: "Build me a dashboard" },
			{ name: "m2", role: "assistant", content: "", error },
			...after,
		],
	});
	const retryButton = (wrapper) =>
		wrapper.findAll("button").find((b) => /^Retry/.test(b.text()));
	const deferred = () => {
		let resolve, reject;
		const promise = new Promise((res, rej) => ((resolve = res), (reject = rej)));
		return { promise, resolve, reject };
	};

	async function mountFailed(transcript = failed(), options) {
		api.getDashboardConversation.mockResolvedValue(transcript);
		const mountedPane = mountPane(options);
		await flushPromises();
		return mountedPane;
	}

	beforeEach(() => {
		retryMessage.mockReset();
		retryMessage.mockResolvedValue({ ok: true, run_id: "r2" });
		toast.error.mockClear();
	});
	afterEach(() => {
		vi.useRealTimers();
		api.getDashboardConversation.mockReset();
		api.getDashboardConversation.mockResolvedValue({ messages: [] });
		getConversationContext.mockReset();
		getConversationContext.mockResolvedValue(null);
		brand.name = "Jarvis";
	});

	it("retries an empty reply through the chat retry API", async () => {
		const { wrapper } = await mountFailed();
		expect(wrapper.find(".text-ink-red-4").text()).toContain(
			"The model returned an empty reply"
		);
		await retryButton(wrapper).trigger("click");
		await flushPromises();
		expect(retryMessage).toHaveBeenCalledWith("m2");
		// The retried run is in progress: the button stays, disabled, and says so.
		expect(retryButton(wrapper).text()).toBe("Retrying…");
		expect(retryButton(wrapper).attributes("disabled")).toBeDefined();
	});

	it("is disabled while the retry request is in flight, and ignores a second click", async () => {
		const pending = deferred();
		retryMessage.mockReturnValueOnce(pending.promise);
		const { wrapper } = await mountFailed();
		await retryButton(wrapper).trigger("click");
		await flushPromises();
		expect(retryButton(wrapper).text()).toBe("Retrying…");
		expect(retryButton(wrapper).attributes("disabled")).toBeDefined();
		// The function checks too, not only the disabled button.
		const button = wrapper
			.findAllComponents({ name: "Button" })
			.find((b) => /^Retry/.test(b.text()));
		button.vm.$emit("click");
		expect(retryMessage).toHaveBeenCalledTimes(1);
		pending.resolve({ ok: true, run_id: "r2" });
		await flushPromises();
	});

	it("shows the refusal and offers Retry again", async () => {
		retryMessage.mockResolvedValueOnce({
			ok: false,
			reason: "Only the latest reply can be retried.",
		});
		const { wrapper } = await mountFailed();
		await retryButton(wrapper).trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith("Only the latest reply can be retried.");
		expect(retryButton(wrapper).text()).toBe("Retry");
		expect(retryButton(wrapper).attributes("disabled")).toBeUndefined();
	});

	it("says it could not retry for a refusal code it does not know", async () => {
		retryMessage.mockResolvedValueOnce({ ok: false, reason: "maintenance" });
		const { wrapper } = await mountFailed();
		await retryButton(wrapper).trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith("Couldn&#39;t retry that.");
	});

	it("escapes the whole refusal message, the agent name too", async () => {
		brand.name = "<img src=x onerror=1>";
		retryMessage.mockResolvedValueOnce({ ok: false, reason: "usage_limit" });
		const { wrapper } = await mountFailed();
		await retryButton(wrapper).trigger("click");
		await flushPromises();
		const [message] = toast.error.mock.calls.at(-1);
		expect(message).not.toContain("<img");
		expect(message).toContain("&lt;img src=x onerror=1&gt;");
	});

	it("shows the error of a retry that throws and offers Retry again", async () => {
		retryMessage.mockRejectedValueOnce(new Error("Network down"));
		const { wrapper } = await mountFailed();
		await retryButton(wrapper).trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith("Network down");
		expect(retryButton(wrapper).text()).toBe("Retry");
		expect(retryButton(wrapper).attributes("disabled")).toBeUndefined();
	});

	// The retried run's own terminal: a new placeholder (m3) of this pane's run (r2).
	const retriedError = {
		kind: "run:error",
		conversation_id: "conv1",
		message_id: "m3",
		run_id: "r2",
	};

	it("offers Retry again when the retried run fails too", async () => {
		const { wrapper, socket } = await mountFailed();
		await retryButton(wrapper).trigger("click");
		await flushPromises();
		expect(retryButton(wrapper).text()).toBe("Retrying…");
		socket.fire({ ...retriedError, code: "empty-reply" });
		await flushPromises();
		expect(retryButton(wrapper).text()).toBe("Retry");
	});

	it("is not left running when the run fails before the retry request returns", async () => {
		const pending = deferred();
		retryMessage.mockReturnValueOnce(pending.promise);
		const { wrapper, socket } = await mountFailed();
		await retryButton(wrapper).trigger("click");
		socket.fire(retriedError);
		pending.resolve({ ok: true, run_id: "r2" });
		await flushPromises();
		expect(retryButton(wrapper).text()).toBe("Retry");
	});

	it("ignores the failed turn's re-published terminal while the retry is pending", async () => {
		// Finalize re-publishes the failed turn's terminal (same message, its own run).
		const republished = { conversation_id: "conv1", message_id: "m2", run_id: "r1" };
		for (const kind of ["run:error", "run:end"]) {
			const pending = deferred();
			retryMessage.mockReturnValueOnce(pending.promise);
			const { wrapper, socket } = await mountFailed();
			await retryButton(wrapper).trigger("click");
			socket.fire({ ...republished, kind });
			pending.resolve({ ok: true, run_id: "r2" });
			await flushPromises();
			expect(retryButton(wrapper).text(), `${kind} before the response`).toBe("Retrying…");
			socket.fire({ ...republished, kind, message_id: "m9" });
			await flushPromises();
			expect(retryButton(wrapper).text(), `${kind} of another run`).toBe("Retrying…");
		}
	});

	it("takes the terminal of a second retry as its own, not as another run's", async () => {
		// The second retry's id is not known before its response; the first one's is stale.
		const { wrapper, socket } = await mountWithFakeTimers();
		await retryButton(wrapper).trigger("click");
		await vi.advanceTimersByTimeAsync(10);
		api.getDashboardConversation.mockResolvedValue(
			failed(EMPTY_REPLY, [
				{ name: "m3", role: "assistant", content: "", error: EMPTY_REPLY },
			])
		);
		socket.fire({
			kind: "run:error",
			conversation_id: "conv1",
			message_id: "m3",
			run_id: "r2",
		});
		await vi.advanceTimersByTimeAsync(400);
		const pending = deferred();
		retryMessage.mockReturnValueOnce(pending.promise);
		await retryButton(wrapper).trigger("click");
		socket.fire({
			kind: "run:error",
			conversation_id: "conv1",
			message_id: "m4",
			run_id: "r3",
		});
		pending.resolve({ ok: true, run_id: "r3" });
		await vi.advanceTimersByTimeAsync(10);
		expect(retryButton(wrapper).text()).toBe("Retry");
	});

	it("never ends a run that started while a refused retry was in flight", async () => {
		const pending = deferred();
		retryMessage.mockReturnValueOnce(pending.promise);
		const { wrapper, socket } = await mountFailed();
		await retryButton(wrapper).trigger("click");
		socket.fire({ kind: "run:start", conversation_id: "conv1", run_id: "other" });
		pending.resolve({
			ok: false,
			reason: "A reply is already in progress. Wait for it to finish.",
		});
		await flushPromises();
		expect(retryButton(wrapper).text(), "the other run is still live").toBe("Retrying…");
	});

	it("never ends a run that started while a failed retry request was in flight", async () => {
		const pending = deferred();
		retryMessage.mockReturnValueOnce(pending.promise);
		const { wrapper, socket } = await mountFailed();
		await retryButton(wrapper).trigger("click");
		socket.fire({ kind: "run:start", conversation_id: "conv1", run_id: "other" });
		pending.reject(new Error("Network down"));
		await flushPromises();
		expect(retryButton(wrapper).text()).toBe("Retrying…");
	});

	// Production then writes a visible cancelled row (the refetch shows it); this pins
	// the moment before it: the run state ends at once.
	it("ends the run when the queued retry is cancelled", async () => {
		const { wrapper, socket } = await mountFailed();
		await retryButton(wrapper).trigger("click");
		await flushPromises();
		socket.fire({ kind: "turn:cancelled", conversation_id: "conv1", run_id: "other" });
		await flushPromises();
		expect(retryButton(wrapper).text()).toBe("Retrying…");
		socket.fire({ kind: "turn:cancelled", conversation_id: "conv1", run_id: "r2" });
		await flushPromises();
		expect(retryButton(wrapper).text()).toBe("Retry");
	});

	// Fake timers: flushPromises would wait on a faked timer, so time is advanced instead.
	async function mountWithFakeTimers(options) {
		vi.useFakeTimers();
		api.getDashboardConversation.mockResolvedValue(failed());
		const mountedPane = mountPane(options);
		await vi.advanceTimersByTimeAsync(10);
		return mountedPane;
	}

	it("reads the transcript again when an accepted retry never starts, and keeps its Retry off", async () => {
		toast.info.mockClear();
		const { wrapper, socket } = await mountWithFakeTimers();
		await retryButton(wrapper).trigger("click");
		await vi.advanceTimersByTimeAsync(10);
		api.getDashboardConversation.mockClear();
		await vi.advanceTimersByTimeAsync(89000);
		expect(api.getDashboardConversation).not.toHaveBeenCalled();
		expect(retryButton(wrapper).text()).toBe("Retrying…");
		await vi.advanceTimersByTimeAsync(1500);
		expect(api.getDashboardConversation).toHaveBeenCalled();
		expect(toast.info).toHaveBeenCalledWith(
			"The retry has not started. Reload the chat to try again."
		);
		expect(retryButton(wrapper).text(), "no live run in the transcript").toBe("Retry");
		expect(
			retryButton(wrapper).attributes("disabled"),
			"no second run from here"
		).toBeDefined();
		// A frame of the retried turn turns it back on.
		socket.fire({
			kind: "queue:position",
			conversation_id: "conv1",
			run_id: "r2",
			position: 1,
		});
		await vi.advanceTimersByTimeAsync(10);
		expect(retryButton(wrapper).attributes("disabled")).toBeUndefined();
	});

	it("keeps the fallback armed through a frame of another turn", async () => {
		toast.info.mockClear();
		const { wrapper, socket } = await mountWithFakeTimers();
		await retryButton(wrapper).trigger("click");
		await vi.advanceTimersByTimeAsync(30000);
		socket.fire({
			kind: "queue:position",
			conversation_id: "conv1",
			run_id: "other",
			position: 2,
		});
		await vi.advanceTimersByTimeAsync(65000);
		expect(toast.info).toHaveBeenCalled();
	});

	it("does not take the transcript's state when a run starts during the fallback's load", async () => {
		const { wrapper, socket } = await mountWithFakeTimers();
		await retryButton(wrapper).trigger("click");
		await vi.advanceTimersByTimeAsync(10);
		const load = deferred();
		api.getDashboardConversation.mockReturnValueOnce(load.promise);
		await vi.advanceTimersByTimeAsync(90000);
		socket.fire({ kind: "run:start", conversation_id: "conv1", run_id: "r2" });
		load.resolve(failed());
		await vi.advanceTimersByTimeAsync(10);
		expect(retryButton(wrapper).text()).toBe("Retrying…");
	});

	it("arms the fallback only for a retry whose run has not started yet", async () => {
		const pending = deferred();
		retryMessage.mockReturnValueOnce(pending.promise);
		const { wrapper, socket } = await mountWithFakeTimers();
		await retryButton(wrapper).trigger("click");
		socket.fire({ kind: "run:start", conversation_id: "conv1", run_id: "r2" });
		pending.resolve({ ok: true, run_id: "r2" });
		await vi.advanceTimersByTimeAsync(1000);
		api.getDashboardConversation.mockClear();
		await vi.advanceTimersByTimeAsync(95000);
		expect(api.getDashboardConversation).not.toHaveBeenCalled();
		expect(retryButton(wrapper).text()).toBe("Retrying…");
	});

	it("stops the fallback on a chat switch or an unmount", async () => {
		for (const leave of [(w) => w.vm.resetChat(), (w) => mounted.pop().unmount()]) {
			toast.info.mockClear();
			const { wrapper } = await mountWithFakeTimers();
			await retryButton(wrapper).trigger("click");
			await vi.advanceTimersByTimeAsync(10);
			leave(wrapper);
			api.getDashboardConversation.mockClear();
			await vi.advanceTimersByTimeAsync(95000);
			expect(api.getDashboardConversation).not.toHaveBeenCalled();
			expect(toast.info).not.toHaveBeenCalled();
		}
	});

	it("does nothing for a pane unmounted while the retry request was in flight", async () => {
		const pending = deferred();
		retryMessage.mockReturnValueOnce(pending.promise);
		const { wrapper } = await mountWithFakeTimers();
		await retryButton(wrapper).trigger("click");
		expect(wrapper.exists()).toBe(true);
		mounted.pop().unmount();
		pending.resolve({ ok: false, reason: "Only the latest reply can be retried." });
		api.getDashboardConversation.mockClear();
		await vi.advanceTimersByTimeAsync(95000);
		expect(api.getDashboardConversation).not.toHaveBeenCalled();
		expect(vi.getTimerCount()).toBe(0);
	});

	it("keeps waiting when a frame shows the retried turn is alive, or it is queued", async () => {
		for (const [label, response, frame] of [
			[
				"a frame",
				{ ok: true, run_id: "r2" },
				{ kind: "queue:position", run_id: "r2", position: 1 },
			],
			["queued", { ok: true, run_id: "r2", queued: true }, null],
		]) {
			retryMessage.mockResolvedValueOnce(response);
			const { wrapper, socket } = await mountWithFakeTimers();
			await retryButton(wrapper).trigger("click");
			await vi.advanceTimersByTimeAsync(10);
			if (frame) socket.fire({ ...frame, conversation_id: "conv1" });
			await vi.advanceTimersByTimeAsync(95000);
			expect(retryButton(wrapper).text(), label).toBe("Retrying…");
		}
	});

	it("is disabled while the chat is compacting", async () => {
		getConversationContext.mockResolvedValue({ fresh: true, compacting: true });
		const { wrapper } = await mountFailed();
		expect(retryButton(wrapper).attributes("disabled")).toBeDefined();
	});

	it("refetches the chat after a refusal, so a stale Retry goes away", async () => {
		retryMessage.mockResolvedValueOnce({
			ok: false,
			reason: "Only the latest reply can be retried.",
		});
		const { wrapper } = await mountWithFakeTimers();
		api.getDashboardConversation.mockClear();
		await retryButton(wrapper).trigger("click");
		await vi.advanceTimersByTimeAsync(310);
		expect(api.getDashboardConversation).toHaveBeenCalledTimes(1);
	});

	it("leaves a chat the user has since left alone, after a refusal or an error", async () => {
		for (const settle of [
			(p) => p.resolve({ ok: false, reason: "Only the latest reply can be retried." }),
			(p) => p.reject(new Error("Network down")),
		]) {
			toast.error.mockClear();
			const pending = deferred();
			retryMessage.mockReturnValueOnce(pending.promise);
			const { wrapper } = await mountFailed();
			await retryButton(wrapper).trigger("click");
			wrapper.vm.resetChat();
			settle(pending);
			await flushPromises();
			expect(toast.error).not.toHaveBeenCalled();
		}
	});

	it("offers no Retry for a cause a retry cannot fix", async () => {
		const { wrapper } = await mountFailed(failed("401 Unauthorized"));
		expect(wrapper.find(".text-ink-red-4").exists()).toBe(true);
		expect(retryButton(wrapper)).toBeUndefined();
	});

	it("offers no Retry once another message follows the failed reply", async () => {
		const { wrapper } = await mountFailed(
			failed(EMPTY_REPLY, [{ name: "m3", role: "user", content: "Try a bar chart" }])
		);
		expect(retryButton(wrapper)).toBeUndefined();
	});

	it("offers no Retry when a blank reply the server counts follows the error", async () => {
		const { wrapper } = await mountFailed(
			failed(EMPTY_REPLY, [{ name: "m3", role: "assistant", content: "", streaming: 1 }])
		);
		expect(retryButton(wrapper)).toBeUndefined();
	});

	it("still offers Retry this step above a macro's closing message", async () => {
		const closing = {
			name: "m3",
			role: "assistant",
			content: "Macro failed. Step 1 failed.",
			ref_doctype: "Jarvis Macro Run",
		};
		const { wrapper } = await mountFailed(failed(EMPTY_REPLY, [closing]));
		expect(retryButton(wrapper).text()).toBe("Retry this step");
	});

	// The ladder ends the run only on a settled reply, and the transcript's run state is
	// not applied while the retry is pending: the failed reply it replaces is not one.
	it("without a socket, waits for a reply newer than the retried error", async () => {
		const transcript = failed();
		const { wrapper } = await mountWithFakeTimers({ socket: null });
		await retryButton(wrapper).trigger("click");
		await vi.advanceTimersByTimeAsync(3100);
		expect(wrapper.emitted("activity")).toBeUndefined();
		expect(retryButton(wrapper).text()).toBe("Retrying…");
		api.getDashboardConversation.mockResolvedValue({
			...transcript,
			messages: [
				...transcript.messages,
				{ name: "m3", role: "assistant", content: "Done." },
			],
		});
		await vi.advanceTimersByTimeAsync(5000);
		expect(wrapper.emitted("activity")).toBeDefined();
	});

	it("without a socket, a refused retry stops its refetch ladder", async () => {
		retryMessage.mockResolvedValueOnce({
			ok: false,
			reason: "Only the latest reply can be retried.",
		});
		const { wrapper } = await mountWithFakeTimers({ socket: null });
		await retryButton(wrapper).trigger("click");
		await vi.advanceTimersByTimeAsync(400); // the refusal's own refetch
		api.getDashboardConversation.mockClear();
		await vi.advanceTimersByTimeAsync(61000);
		expect(api.getDashboardConversation).not.toHaveBeenCalled();
	});

	it("without a socket, never arms the no-start fallback", async () => {
		toast.info.mockClear();
		const { wrapper } = await mountWithFakeTimers({ socket: null });
		await retryButton(wrapper).trigger("click");
		await vi.advanceTimersByTimeAsync(95000);
		expect(toast.info).not.toHaveBeenCalled();
	});
});

describe("DashboardChatPane send", () => {
	afterEach(() => {
		brand.name = "Jarvis";
		api.sendDashboardChat.mockReset();
		api.sendDashboardChat.mockResolvedValue({ ok: true, conversation_id: "conv1" });
	});

	async function send(wrapper, text = "Monthly sales") {
		const box = wrapper.find("textarea");
		await box.setValue(text);
		await box.trigger("keydown", { key: "Enter" });
		await flushPromises();
	}

	it("shows the run as started, and ends it when its queued turn is cancelled", async () => {
		api.sendDashboardChat.mockResolvedValueOnce({
			ok: true,
			conversation_id: "conv1",
			run_id: "s1",
		});
		const { wrapper, socket } = mountPane();
		await flushPromises();
		await send(wrapper);
		expect(wrapper.find('[aria-live="polite"]').exists()).toBe(true);
		socket.fire({ kind: "turn:cancelled", conversation_id: "conv1", run_id: "s1" });
		await flushPromises();
		expect(wrapper.find('[aria-live="polite"]').exists()).toBe(false);
	});

	it("escapes the whole refusal message, the agent name too", async () => {
		brand.name = "<img src=x onerror=1>";
		toast.error.mockClear();
		api.sendDashboardChat.mockResolvedValueOnce({ ok: false, reason: "usage_limit" });
		const { wrapper } = mountPane();
		await flushPromises();
		await send(wrapper);
		const [message] = toast.error.mock.calls.at(-1);
		expect(message).not.toContain("<img");
		expect(message).toContain("&lt;img src=x onerror=1&gt;");
	});
});
