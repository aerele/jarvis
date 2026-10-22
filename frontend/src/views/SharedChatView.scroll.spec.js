import { mount, flushPromises } from "@vue/test-utils";
import { reactive, ref } from "vue";
import { beforeEach, afterEach, describe, it, expect, vi } from "vitest";
const m = vi.hoisted(() => ({
	read: vi.fn(),
	legacy: vi.fn(),
	send: vi.fn(),
	upload: vi.fn(),
	toggle: vi.fn(),
	store: null,
}));
vi.mock("vue-router", () => ({
	useRoute: () => ({ params: { id: "C1" } }),
	useRouter: () => ({ replace: vi.fn() }),
}));
vi.mock("@/stores/shell", () => ({ useShellStore: () => m.store }));
vi.mock("@/theme", () => ({
	useJarvisTheme: () => ({
		effectiveDark: ref(false),
		paletteVars: {},
		toggleTheme: m.toggle,
	}),
}));
vi.mock("@/api/chatSharing", () => ({ getSharedConversation: m.legacy }));
vi.mock("@/api/chatCollaboration", () => ({
	getCollaboration: m.read,
	sendSharedMessage: m.send,
	uploadSharedFile: m.upload,
	stopSharedRun: vi.fn(),
	retrySharedRun: vi.fn(),
	confirmSharedAction: vi.fn(),
	discardSharedAction: vi.fn(),
	answerSharedQuestion: vi.fn(),
	getCollaborationTool: vi.fn(),
	getCollaborationCanvas: vi.fn(),
}));
vi.mock("@/branding", () => ({ brand: { agentName: "Jarvis" } }));
vi.mock("frappe-ui", () => ({
	FeatherIcon: { template: "<i/>" },
	Button: { template: "<button><slot/></button>" },
}));
vi.mock("@/charts/JvChart.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/components/chat/SharedCanvas.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/components/chat/SharedToolActivity.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/components/ReceiptChip.vue", () => ({ default: { template: "<div/>" } }));
import SharedChatView from "./SharedChatView.vue";
const data = (canChat = false) => ({
	conversation: { name: "C1", title: "Purchasing team", owner_name: "Anita", can_chat: canChat },
	messages: [{ name: "A1", seq: 1, role: "assistant", content: "Supplier review" }],
	turns: [],
	questions: [],
});
const wrappers = [];
let heightSpy, clientSpy, extraHeight;
const page = (start, count, more = false) => ({
	...data(),
	messages: Array.from({ length: count }, (_, i) => ({
		name: "A" + (start + i),
		seq: start + i,
		role: "assistant",
		content: "Supplier update " + (start + i),
	})),
	has_more: more,
	oldest_seq: start,
});
async function open() {
	const w = mount(SharedChatView);
	wrappers.push(w);
	await flushPromises();
	return w;
}
beforeEach(() => {
	vi.clearAllMocks();
	m.store = reactive({
		mobile: false,
		mobileDrawerOpen: false,
		markSharedSeen: vi.fn(),
		loadSharedConversations: vi.fn(),
	});
	m.legacy.mockResolvedValue({ conversation: { collaboration_available: true } });
	m.read.mockResolvedValue(page(10, 3, true));
	extraHeight = 0;
	heightSpy = vi
		.spyOn(HTMLElement.prototype, "scrollHeight", "get")
		.mockImplementation(function () {
			return this.getAttribute("role") === "log"
				? this.querySelectorAll("article").length * 300 + extraHeight
				: 0;
		});
	clientSpy = vi
		.spyOn(HTMLElement.prototype, "clientHeight", "get")
		.mockImplementation(function () {
			return this.getAttribute("role") === "log" ? 500 : 0;
		});
});
afterEach(() => {
	wrappers.splice(0).forEach((w) => w.unmount());
	heightSpy.mockRestore();
	clientSpy.mockRestore();
});
describe("shared chat reply visibility", () => {
	it("keeps the latest reply visible after a viewport layout change", async () => {
		const w = await open();
		const thread = w.get('[role="log"]');
		const frame = vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
			callback();
			return 1;
		});
		try {
			extraHeight = 800;
			window.dispatchEvent(new Event("resize"));
			await flushPromises();
			expect(thread.element.scrollTop).toBe(1200);
		} finally {
			frame.mockRestore();
		}
	});
	it("does not move an earlier reading position when the viewport changes", async () => {
		const w = await open();
		const thread = w.get('[role="log"]');
		thread.element.scrollTop = 50;
		await thread.trigger("scroll");
		extraHeight = 800;
		window.dispatchEvent(new Event("resize"));
		await flushPromises();
		expect(thread.element.scrollTop).toBe(50);
	});
	it("does not drag a streaming reply and follows it once settled", async () => {
		const w = await open();
		const thread = w.get('[role="log"]');
		extraHeight = 500;
		m.read.mockResolvedValue({ ...page(10, 3), turns: [{ run_id: "R", state: "streaming" }] });
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(thread.element.scrollTop).toBe(400);
		m.read.mockResolvedValue(page(10, 6));
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(thread.element.scrollTop).toBe(1800);
	});
	it("reveals newly arriving rows during an active turn without following later chunks", async () => {
		const w = await open();
		const thread = w.get('[role="log"]');
		m.read.mockResolvedValue({ ...page(10, 4), turns: [{ run_id: "R", state: "streaming" }] });
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(thread.element.scrollTop).toBe(700);
		extraHeight = 500;
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(thread.element.scrollTop).toBe(700);
	});
	it("reveals the request when a send coalesces with an older fetch", async () => {
		m.read.mockResolvedValue({ ...page(10, 3), conversation: data(true).conversation });
		const w = await open();
		const thread = w.get('[role="log"]');
		let resolveOld;
		m.read.mockImplementationOnce(
			() =>
				new Promise((resolve) => {
					resolveOld = resolve;
				})
		);
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		m.send.mockResolvedValue({ ok: true, run_id: "R" });
		m.read.mockResolvedValue({
			...page(10, 4),
			conversation: data(true).conversation,
			turns: [{ run_id: "R", state: "streaming" }],
		});
		await w.get("textarea").setValue("Queued purchasing request");
		await w.get("textarea").trigger("keydown", { key: "Enter" });
		await flushPromises();
		resolveOld({ ...page(10, 3), conversation: data(true).conversation });
		await flushPromises();
		expect(thread.element.scrollTop).toBe(700);
	});
	it("reveals a newly submitted request while reading older messages", async () => {
		m.read.mockResolvedValue({ ...page(10, 3), conversation: data(true).conversation });
		const w = await open();
		const thread = w.get('[role="log"]');
		thread.element.scrollTop = 50;
		await thread.trigger("scroll");
		m.send.mockResolvedValue({ ok: true, run_id: "R" });
		m.read.mockResolvedValue({
			...page(10, 4),
			conversation: data(true).conversation,
			turns: [{ run_id: "R", state: "queued" }],
		});
		await w.get("textarea").setValue("New purchasing request");
		await w.get("textarea").trigger("keydown", { key: "Enter" });
		await flushPromises();
		expect(m.send).toHaveBeenCalledTimes(1);
		expect(thread.element.scrollTop).toBe(700);
	});
	it("opens at the latest message", async () => {
		const w = await open();
		expect(w.get('[role="log"]').element.scrollTop).toBe(400);
	});
	it("follows new settled replies when the reader is at the bottom", async () => {
		const w = await open();
		const thread = w.get('[role="log"]');
		thread.element.scrollTop = 400;
		await thread.trigger("scroll");
		m.read.mockResolvedValue(page(10, 4, true));
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(thread.element.scrollTop).toBe(700);
	});
	it("keeps an earlier reading position and offers a working jump to latest", async () => {
		const w = await open();
		const thread = w.get('[role="log"]');
		thread.element.scrollTop = 50;
		await thread.trigger("scroll");
		m.read.mockResolvedValue(page(10, 4, true));
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(thread.element.scrollTop).toBe(50);
		await w.get('button[aria-label="Jump to latest"]').trigger("click");
		expect(thread.element.scrollTop).toBe(700);
	});
	it("preserves the visible history anchor when loading earlier messages", async () => {
		const w = await open();
		const thread = w.get('[role="log"]');
		thread.element.scrollTop = 50;
		await thread.trigger("scroll");
		m.read.mockResolvedValue(page(7, 3, false));
		await w
			.findAll("button")
			.find((b) => b.text() === "Load earlier")
			.trigger("click");
		await flushPromises();
		expect(thread.element.scrollTop).toBe(950);
	});
});
