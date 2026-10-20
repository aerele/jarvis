import { mount, flushPromises } from "@vue/test-utils";
import { reactive, ref } from "vue";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	detail: vi.fn(),
	canvas: vi.fn(),
	replace: vi.fn(),
	seen: vi.fn(),
	reloadIndex: vi.fn(),
	route: { params: { id: "C1" } },
}));
vi.mock("@/api/chatSharing", () => ({
	getSharedConversation: mocks.get,
	getSharedToolDetail: mocks.detail,
	getSharedCanvas: mocks.canvas,
}));
vi.mock("vue-router", () => ({
	useRoute: () => mocks.route,
	useRouter: () => ({ replace: mocks.replace }),
}));
vi.mock("@/stores/shell", () => ({
	useShellStore: () => ({
		markSharedSeen: mocks.seen,
		loadSharedConversations: mocks.reloadIndex,
	}),
}));
vi.mock("@/theme", () => ({
	useJarvisTheme: () => ({ effectiveDark: ref(false), paletteVars: {} }),
	LIGHT_VARS: {},
	DARK_VARS: {},
}));
vi.mock("frappe-ui", () => ({
	Button: {
		props: ["label", "disabled"],
		template: '<button :disabled="disabled"><slot/>{{label}}</button>',
	},
	FeatherIcon: { template: "<i/>" },
}));
vi.mock("@/charts/JvChart.vue", () => ({
	default: {
		name: "JvChart",
		props: ["spec"],
		template: '<div class="chart">{{spec.title}}</div>',
	},
}));
vi.mock("@/branding", () => ({ brand: { agentName: "Aerele Assistant" } }));
import SharedChatView from "./SharedChatView.vue";
const url =
	"/api/method/jarvis.chat.sharing.get_shared_file?conversation=C1&file_url=%2Fprivate%2Ffiles%2Fa.png";
const payload = () => ({
	conversation: { name: "C1", title: "Supplier review", owner_name: "Owner" },
	messages: [
		{ name: "M1", seq: 1, role: "user", content: "Please review" },
		{
			name: "M2",
			seq: 2,
			role: "assistant",
			content:
				'Saved summary\n```jarvis-chart\n{"type":"bar","title":"Totals","x":["A"],"series":[{"name":"Value","data":[3]}]}\n```\n```jarvis-action\n{"action":"submit","name":"INV-7"}\n```',
			canvas: [{ name: "img", type: "image", title: "Image", file_url: url }],
		},
		{
			name: "T1",
			seq: 3,
			role: "tool",
			tool_name: "get_doc",
			tool_status: "completed",
			tool_result_compact: 1,
		},
		{
			name: "R1",
			seq: 3.5,
			role: "tool",
			tool_name: "create_doc",
			action_outcome: "confirmed",
			tool_args: JSON.stringify({ doctype: "ToDo", doc: { description: "Follow up" } }),
			tool_result: JSON.stringify({
				ok: true,
				data: { doctype: "ToDo", name: "TD-1", suggested_next: { kind: "submit" } },
			}),
		},
		{ name: "P1", seq: 4, role: "tool", tool_name: "submit_doc", awaiting_owner: true },
	],
	paging: { has_more: false },
	older_pending: [],
	refreshed: [],
});
const wrappers = [];
async function open() {
	const w = mount(SharedChatView, {
		global: { provide: { $socket: { on: vi.fn(), off: vi.fn() } } },
	});
	wrappers.push(w);
	await flushPromises();
	return w;
}
beforeEach(() => {
	vi.clearAllMocks();
	mocks.route = reactive({ params: { id: "C1" } });
	mocks.get.mockResolvedValue(payload());
	mocks.detail.mockResolvedValue({
		tool_args: '{"doctype":"Sales Invoice"}',
		tool_result: '{"name":"INV-7"}',
	});
});
afterEach(() => {
	wrappers.splice(0).forEach((w) => w.unmount());
	vi.useRealTimers();
});
describe("shared chat read-only boundary", () => {
	it("uses the site assistant name", async () => {
		const w = await open();
		expect(w.text()).toContain("Aerele Assistant");
	});

	it.each([401, 403, 404])(
		"clears displayed content after an untyped %s denial",
		async (status) => {
			const w = await open();
			mocks.get.mockRejectedValue(Object.assign(new Error("unavailable"), { status }));
			window.dispatchEvent(new Event("focus"));
			await flushPromises();
			expect(w.text()).toContain("This chat is not shared with you");
			expect(w.text()).not.toContain("Saved summary");
			expect(w.find("img").exists()).toBe(false);
		}
	);

	it("renders history and charts without owner controls, action handlers or data bridge", async () => {
		const listener = vi.spyOn(window, "addEventListener");
		const w = await open();
		expect(w.text()).toContain("Shared by Owner");
		expect(w.text()).toContain("Read only");
		expect(w.text()).toContain("Awaiting the owner's confirmation");
		expect(w.find(".chart").text()).toBe("Totals");
		expect(w.find("textarea,input,[contenteditable=true]").exists()).toBe(false);
		for (const text of [
			"Stop",
			"Retry",
			"Edit",
			"Feedback",
			"Confirm",
			"Submit",
			"Save as macro",
		])
			expect(w.findAll("button").some((b) => b.text() === text)).toBe(false);
		expect(listener.mock.calls.some(([kind]) => kind === "message")).toBe(false);
		const count = mocks.get.mock.calls.length;
		window.dispatchEvent(
			new MessageEvent("message", { data: { jarvis: 1, type: "data", doctype: "User" } })
		);
		await flushPromises();
		expect(mocks.get).toHaveBeenCalledTimes(count);
		expect(mocks.canvas).not.toHaveBeenCalled();
		expect(w.get("img").attributes("src")).toBe(url);
		expect(mocks.seen).toHaveBeenCalledWith("C1");
		listener.mockRestore();
	});
	it.each([
		["not_shared", "This chat is not shared with you"],
		["sharing_off", "Sharing is turned off on this site"],
	])("maps %s and removes previously rendered content", async (reason, text) => {
		const w = await open();
		mocks.get.mockResolvedValue({ refused: reason });
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(w.text()).toContain(text);
		expect(w.text()).not.toContain("Saved summary");
		expect(w.find("img").exists()).toBe(false);
	});
	it("redirects an owner to their own chat", async () => {
		mocks.get.mockResolvedValue({ refused: "owner" });
		await open();
		expect(mocks.replace).toHaveBeenCalledWith("/c/C1");
	});
	it("retains the current transcript through a transient failure", async () => {
		const w = await open();
		mocks.get.mockRejectedValue(new Error("network"));
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(w.text()).toContain("Reconnecting…");
		expect(w.text()).toContain("Saved summary");
	});
	it("loads tool details only when expanded", async () => {
		const w = await open();
		expect(mocks.detail).not.toHaveBeenCalled();
		await w
			.findAll("button")
			.find((b) => b.text().includes("get_doc"))
			.trigger("click");
		await flushPromises();
		expect(mocks.detail).toHaveBeenCalledWith("T1");
		expect(w.text()).toContain("INV-7");
	});
	it("pages earlier messages without losing current rows", async () => {
		const d = payload();
		d.paging = { has_more: true, before: 1, start: 1 };
		mocks.get.mockResolvedValueOnce(d).mockResolvedValueOnce({
			...payload(),
			messages: [{ name: "M0", seq: 0, role: "user", content: "Earlier question" }],
			paging: { has_more: false, before: 0 },
		});
		const w = await open();
		await w
			.findAll("button")
			.find((b) => b.text() === "Load earlier")
			.trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Earlier question");
		expect(w.text()).toContain("Saved summary");
		expect(mocks.get.mock.calls[1][1]).toMatchObject({ before: 1 });
	});
	it("polls only while a row is streaming and stops on unmount", async () => {
		vi.useFakeTimers();
		const d = payload();
		d.messages[1].streaming = 1;
		mocks.get.mockResolvedValue(d);
		const w = await open();
		await vi.advanceTimersByTimeAsync(15000);
		expect(mocks.get).toHaveBeenCalledTimes(2);
		w.unmount();
		await vi.advanceTimersByTimeAsync(30000);
		expect(mocks.get).toHaveBeenCalledTimes(2);
	});
	it("never draws stale history after navigating to another shared chat", async () => {
		let finish;
		mocks.get
			.mockImplementationOnce(() => new Promise((r) => (finish = r)))
			.mockResolvedValueOnce({
				...payload(),
				conversation: { name: "C2", title: "Current chat", owner_name: "Other" },
				messages: [
					{ name: "current", seq: 1, role: "assistant", content: "Current content" },
				],
			});
		const w = await open();
		mocks.route.params.id = "C2";
		await flushPromises();
		finish(payload());
		await flushPromises();
		expect(w.text()).toContain("Current content");
		expect(w.text()).not.toContain("Saved summary");
		expect(w.text()).not.toContain("Supplier review");
	});
});
