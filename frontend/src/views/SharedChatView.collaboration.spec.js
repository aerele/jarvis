import { mount, flushPromises } from "@vue/test-utils";
import { reactive, ref } from "vue";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
const m = vi.hoisted(() => ({
	legacy: vi.fn(),
	read: vi.fn(),
	send: vi.fn(),
	stop: vi.fn(),
	retry: vi.fn(),
	confirm: vi.fn(),
	discard: vi.fn(),
	answer: vi.fn(),
	on: vi.fn(),
	off: vi.fn(),
	route: { params: { id: "C1" } },
}));
vi.mock("@/api/chatSharing", () => ({ getSharedConversation: m.legacy }));
vi.mock("@/api/chatCollaboration", () => ({
	getCollaboration: m.read,
	sendSharedMessage: m.send,
	stopSharedRun: m.stop,
	retrySharedRun: m.retry,
	confirmSharedAction: m.confirm,
	discardSharedAction: m.discard,
	answerSharedQuestion: m.answer,
	uploadSharedFile: vi.fn(),
	getCollaborationTool: vi.fn(),
	getCollaborationCanvas: vi.fn(),
}));
vi.mock("vue-router", () => ({
	useRoute: () => m.route,
	useRouter: () => ({ replace: vi.fn() }),
}));
vi.mock("@/stores/shell", () => ({
	useShellStore: () => ({ markSharedSeen: vi.fn(), loadSharedConversations: vi.fn() }),
}));
vi.mock("@/theme", () => ({
	useJarvisTheme: () => ({ effectiveDark: ref(false), paletteVars: {} }),
}));
vi.mock("@/branding", () => ({ brand: { agentName: "Jarvis" } }));
vi.mock("@/charts/JvChart.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/components/chat/SharedCanvas.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/components/chat/SharedToolActivity.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/components/ReceiptChip.vue", () => ({ default: { template: "<div/>" } }));
import SharedChatView from "./SharedChatView.vue";
const notice =
	"Messages, replies, ERP data and files added here are visible to everyone with access to this chat. Jarvis uses your own permissions for new requests and actions.";
function payload(canChat = true) {
	return {
		conversation: {
			name: "C1",
			title: "Shared purchase review",
			activated: true,
			access_mode: canChat ? "Chat" : "View",
			can_chat: canChat,
			notice,
		},
		messages: [
			{
				name: "U1",
				seq: 1,
				role: "user",
				content: "Compare suppliers",
				actor: { id: "a@example.com", name: "Anita" },
			},
			{
				name: "A1",
				seq: 2,
				role: "assistant",
				content: "Supplier comparison",
				actor: { id: "a@example.com", name: "Anita" },
			},
		],
		turns: [],
		questions: [],
		has_more: false,
	};
}
const wrappers = [];
async function open() {
	const w = mount(SharedChatView, {
		global: { provide: { $socket: { on: m.on, off: m.off } } },
	});
	wrappers.push(w);
	await flushPromises();
	return w;
}
beforeEach(() => {
	vi.clearAllMocks();
	m.route = reactive({ params: { id: "C1" } });
	m.legacy.mockResolvedValue({
		conversation: { name: "C1", collaboration_available: true },
		messages: [],
	});
	m.read.mockResolvedValue(payload());
	m.send.mockResolvedValue({ ok: true, run_id: "R1" });
	m.stop.mockResolvedValue({ ok: true });
	m.retry.mockResolvedValue({ ok: true });
	m.confirm.mockResolvedValue({ ok: true });
});
afterEach(() => wrappers.splice(0).forEach((w) => w.unmount()));
const button = (w, text) => w.findAll("button").find((b) => b.text() === text);
describe("shared collaboration view", () => {
	it("reattaches the composer when a stopped queued request is retried", async () => {
		const data = payload();
		const turn = { run_id: "R1", state: "queued", actor: { name: "Anita" }, can_stop: true };
		data.turns = [turn];
		m.read.mockResolvedValue(data);
		const w = await open();
		await w.get("textarea").setValue("Create one Note");
		await w.get("textarea").trigger("keydown", { key: "Enter" });
		await flushPromises();
		turn.state = "cancelled";
		turn.can_stop = false;
		turn.can_retry = true;
		await button(w, "Stop").trigger("click");
		await flushPromises();
		m.retry.mockImplementation(async () => {
			turn.state = "queued";
			turn.can_retry = false;
			turn.can_stop = true;
			return { ok: true, run_id: "R1" };
		});
		await button(w, "Retry").trigger("click");
		await flushPromises();
		await w.get("textarea").trigger("keydown", { key: "Enter" });
		await flushPromises();
		expect(m.send).toHaveBeenCalledTimes(1);
		turn.state = "streaming";
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(w.get("textarea").element.value).toBe("");
	});
	it("reports a confirmed partial action honestly instead of saying it did not start", async () => {
		const data = payload();
		data.messages.push({
			name: "P1",
			seq: 3,
			role: "tool",
			awaiting_actor: { name: "Anita" },
			control: { action: "PA1", summary: "Create Note" },
		});
		m.read.mockResolvedValue(data);
		m.confirm.mockResolvedValue({
			ok: false,
			outcome: "partial",
			error: {
				message:
					"The confirmed action may have partly run; check the Note before retrying.",
			},
		});
		const w = await open();
		await button(w, "Confirm").trigger("click");
		await flushPromises();
		expect(w.text()).toContain("may have partly run");
		expect(w.text()).not.toContain("This action could not start");
	});
	it("shows the exact proposed field values before confirmation", async () => {
		const data = payload();
		data.messages.push({
			name: "P1",
			seq: 3,
			role: "tool",
			awaiting_actor: { name: "Anita" },
			control: {
				action: "PA1",
				summary: "Create Note",
				card: {
					kind: "create",
					doctype: "Note",
					rows: [{ label: "Title", value: "Exact proposed title" }],
				},
			},
		});
		m.read.mockResolvedValue(data);
		const w = await open();
		expect(w.text()).toContain("Exact proposed title");
		expect(m.confirm).not.toHaveBeenCalled();
	});
	it("shows the disclosure, participant authors and Can chat composer", async () => {
		const w = await open();
		expect(w.text()).toContain("Can chat");
		expect(w.text()).toContain(notice);
		expect(w.text()).toContain("Anita");
		expect(w.text()).toContain("Jarvis for Anita");
		expect(w.find("textarea").exists()).toBe(true);
	});
	it("keeps View inert even when pending rows contain another actor's controls", async () => {
		const data = payload(false);
		data.messages.push({
			name: "P1",
			seq: 3,
			role: "tool",
			awaiting_actor: { name: "Anita" },
			control: { action: "PA1", summary: "Create Note" },
		});
		m.read.mockResolvedValue(data);
		const w = await open();
		expect(w.find("textarea").exists()).toBe(false);
		expect(button(w, "Confirm")).toBeUndefined();
		expect(w.text()).toContain("Awaiting Anita's confirmation");
	});
	it("sends through the shared API rather than the owner's chat handlers", async () => {
		const w = await open();
		await w.get("textarea").setValue("My permitted request");
		await w.get("textarea").trigger("keydown", { key: "Enter", shiftKey: false });
		await flushPromises();
		expect(m.send).toHaveBeenCalledWith("C1", "My permitted request", expect.any(String), []);
	});
	it("only renders and spends explicitly supplied own-action controls", async () => {
		const data = payload();
		data.messages.push({
			name: "P1",
			seq: 3,
			role: "tool",
			awaiting_actor: { name: "Anita" },
			control: { action: "PA1", summary: "Create Note" },
		});
		m.read.mockResolvedValue(data);
		const w = await open();
		await button(w, "Confirm").trigger("click");
		await flushPromises();
		expect(m.confirm).toHaveBeenCalledWith("C1", "PA1");
	});
	it("refreshes inert realtime notices without rendering their untrusted payload", async () => {
		const w = await open();
		const handler = m.on.mock.calls.find(([key]) => key === "jarvis:event")[1];
		const data = payload();
		data.messages[1].content = "Fresh projected reply";
		m.read.mockResolvedValue(data);
		handler({
			kind: "collaboration:changed",
			conversation_id: "C1",
			content: "RAW_EVENT_SENTINEL",
		});
		await flushPromises();
		expect(w.text()).toContain("Fresh projected reply");
		expect(w.text()).not.toContain("RAW_EVENT_SENTINEL");
	});
	it("removes composer and controls on a downgrade while preserving the draft", async () => {
		const w = await open();
		await w.get("textarea").setValue("Keep this draft");
		m.read.mockResolvedValue(payload(false));
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(w.find("textarea").exists()).toBe(false);
		m.read.mockResolvedValue(payload());
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(w.get("textarea").element.value).toBe("Keep this draft");
	});
	it("offers Stop and Retry only when the server explicitly permits them", async () => {
		const data = payload();
		data.turns = [
			{ run_id: "R1", state: "queued", actor: { name: "Anita" }, can_stop: true },
			{ run_id: "R2", state: "errored", actor: { name: "Anita" }, can_retry: true },
			{
				run_id: "R3",
				state: "streaming",
				actor: { name: "Ben" },
				can_stop: false,
				can_retry: false,
			},
		];
		m.read.mockResolvedValue(data);
		const w = await open();
		expect(w.findAll("button").filter((b) => b.text() === "Stop")).toHaveLength(1);
		await button(w, "Stop").trigger("click");
		await flushPromises();
		expect(m.stop).toHaveBeenCalledWith("C1", "R1");
		await button(w, "Retry").trigger("click");
		await flushPromises();
		expect(m.retry).toHaveBeenCalledWith("C1", "R2");
	});
});
