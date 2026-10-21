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
	m.read.mockResolvedValue(data());
});
afterEach(() => wrappers.splice(0).forEach((w) => w.unmount()));
describe("familiar shared chat layout", () => {
	it("keeps the usual composer visible but inert for View", async () => {
		const w = await open();
		expect(w.find(".jv-composer").exists()).toBe(true);
		expect(w.find("textarea,input,[contenteditable=true]").exists()).toBe(false);
		expect(w.text()).toContain("Ask the owner for Can chat access to reply.");
		for (const b of w.get("footer").findAll("button")) {
			expect(b.attributes("disabled")).toBeDefined();
			await b.trigger("click");
		}
		expect(m.send).not.toHaveBeenCalled();
		expect(m.upload).not.toHaveBeenCalled();
	});
	it("keeps Chat->View->Chat drafts while removing write inputs on downgrade", async () => {
		m.read.mockResolvedValue(data(true));
		const w = await open();
		await w.get("textarea").setValue("Keep my purchasing request");
		m.read.mockResolvedValue(data(false));
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(w.find("textarea").exists()).toBe(false);
		expect(w.find(".jv-composer").exists()).toBe(true);
		m.read.mockResolvedValue(data(true));
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(w.get("textarea").element.value).toBe("Keep my purchasing request");
	});
	it("removes both composer modes after access is revoked", async () => {
		const w = await open();
		m.read.mockRejectedValue(Object.assign(new Error("forbidden"), { status: 403 }));
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(w.find(".jv-composer").exists()).toBe(false);
		expect(w.text()).not.toContain("Supplier review");
	});
	it("provides the normal mobile navigation and theme controls", async () => {
		m.store.mobile = true;
		const w = await open();
		await w.get('button[aria-label="Open navigation"]').trigger("click");
		expect(m.store.mobileDrawerOpen).toBe(true);
		await w.get('button[aria-label="Toggle theme"]').trigger("click");
		expect(m.toggle).toHaveBeenCalledTimes(1);
		expect(w.find('button[aria-label="Share"]').exists()).toBe(false);
	});
});
