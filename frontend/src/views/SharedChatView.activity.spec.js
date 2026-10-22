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

const wrappers = [];
const base = () => ({
	conversation: { name: "C1", title: "Activity QA", owner_name: "Owner" },
	messages: [
		{
			name: "A",
			seq: 1,
			role: "assistant",
			actor: { id: "A", name: "Anita" },
			tool_call_ids: ["C1", "C2"],
			content: "Summary",
			steps: [{ text: "Reading the supplier" }],
		},
		{
			name: "T1",
			seq: 2,
			role: "tool",
			actor: { id: "A", name: "Anita" },
			tool_call_id: "C1",
			tool_name: "get_doc",
			tool_status: "completed",
			modified: "one",
		},
		{
			name: "T2",
			seq: 3,
			role: "tool",
			actor: { id: "A", name: "Anita" },
			tool_call_id: "C2",
			tool_name: "search",
			tool_status: "error",
		},
		{ name: "P", seq: 4, role: "tool", awaiting_owner: true, tool_name: "create_doc" },
	],
	paging: { has_more: false },
	older_pending: [],
	refreshed: [],
});
async function open() {
	const w = mount(SharedChatView);
	wrappers.push(w);
	await flushPromises();
	return w;
}
beforeEach(() => {
	vi.clearAllMocks();
	mocks.route = reactive({ params: { id: "C1" } });
	mocks.get.mockResolvedValue(base());
	mocks.detail.mockResolvedValue({ tool_result: "Projected supplier" });
});
afterEach(() => wrappers.splice(0).forEach((w) => w.unmount()));
describe("shared activity view integration", () => {
	it("groups tool activity once under its author and preserves open details across a poll", async () => {
		const w = await open();
		expect(w.findAll("article")).toHaveLength(2);
		expect(w.text().match(/for Anita/g)).toHaveLength(1);
		expect(w.text()).toContain("Awaiting the owner's confirmation");
		await w.get(".jv-steps-head").trigger("click");
		await w.get(".jv-tool-head").trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Projected supplier");
		expect(w.text()).toContain("Failed");
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(w.text()).toContain("Projected supplier");
		expect(mocks.detail).toHaveBeenCalledTimes(1);
		mocks.get.mockResolvedValue({ refused: "not_shared" });
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(w.text()).not.toContain("Projected supplier");
		expect(w.findAll(".jv-tool")).toHaveLength(0);
	});
	it("regroups an expanded orphan after earlier history arrives without losing its details", async () => {
		const data = base();
		data.messages = data.messages.slice(1);
		data.paging = { has_more: true, before: 2, start: 2 };
		mocks.get
			.mockResolvedValueOnce(data)
			.mockResolvedValue({ ...base(), messages: [base().messages[0]] });
		const w = await open();
		await w.get(".jv-tool-head").trigger("click");
		await flushPromises();
		await w
			.findAll("button")
			.find((b) => b.text() === "Load earlier")
			.trigger("click");
		await flushPromises();
		expect(w.findAll(".jv-steps-box")).toHaveLength(1);
		expect(w.findAll(".jv-tool")).toHaveLength(2);
		expect(w.text()).toContain("Projected supplier");
		expect(mocks.detail).toHaveBeenCalledTimes(1);
	});
	it("drops an in-flight detail read on route change even when the next chat reuses a row name", async () => {
		let finish;
		mocks.detail.mockImplementationOnce(() => new Promise((r) => (finish = r)));
		const w = await open();
		await w.get(".jv-steps-head").trigger("click");
		await w.get(".jv-tool-head").trigger("click");
		mocks.get.mockResolvedValue({
			...base(),
			conversation: { name: "C2", title: "Other chat" },
		});
		mocks.route.params.id = "C2";
		await flushPromises();
		finish({ tool_result: "OLD PRIVATE DETAIL" });
		await flushPromises();
		expect(w.text()).not.toContain("OLD PRIVATE DETAIL");
		expect(w.findAll(".jv-tool-detail")).toHaveLength(0);
	});
});
