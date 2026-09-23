import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("frappe-ui", () => ({
	Badge: { props: ["label"], template: "<span>{{ label }}</span>" },
	Button: {
		props: ["label"],
		emits: ["click"],
		template: "<button @click=\"$emit('click')\">{{ label }}</button>",
	},
	FeatherIcon: { template: "<i/>" },
	Tooltip: { template: "<span><slot/></span>" },
}));
vi.mock("vue-router", () => ({ useRoute: () => ({ query: { held: "PA-2" } }) }));
vi.mock("@/api/approvals", () => ({ listPendingActionsLane: vi.fn() }));
const refreshApprovalsCount = vi.fn();
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ refreshApprovalsCount }) }));
vi.mock("@/utils/datetime", () => ({ timeAgo: () => "5 minutes ago", exactDate: () => "" }));
vi.mock("./PendingActionDetail.vue", () => ({
	default: {
		props: ["name"],
		emits: ["decided"],
		template:
			'<div class="detail" @click="$emit(\'decided\', {ok: true})">detail {{ name }}</div>',
	},
}));

import * as api from "@/api/approvals";
import PendingActionLane from "./PendingActionLane.vue";

const HOSTILE = "<script>window.__pwned=1</script>";
const rows = [
	{
		name: "PA-1",
		status: "Pending",
		summary: "New supplier: Acme",
		waiters_count: 1,
		created_at: "",
		for_user: "",
	},
	{
		name: "PA-2",
		status: "Executing",
		summary: HOSTILE,
		waiters_count: 3,
		created_at: "",
		for_user: "Asha",
	},
];

function mountLane(socket = null) {
	return mount(PendingActionLane, { global: { provide: { $socket: socket } } });
}

describe("PendingActionLane", () => {
	beforeEach(() => vi.clearAllMocks());
	afterEach(() => vi.useRealTimers());

	it("renders nothing while nothing is held", async () => {
		api.listPendingActionsLane.mockResolvedValue({ rows: [] });
		const w = mountLane();
		await flushPromises();
		expect(w.find("section").exists()).toBe(false);
	});

	it("lists held rows as text and opens the deep-linked one", async () => {
		api.listPendingActionsLane.mockResolvedValue({ rows });
		const w = mountLane();
		await flushPromises();
		expect(w.text()).toContain("New records waiting for approval");
		expect(w.text()).toContain("1 file waiting");
		expect(w.text()).toContain("3 files waiting");
		expect(w.text()).toContain("dropped by Asha");
		expect(w.text()).toContain("Creating…");
		expect(w.html()).not.toContain("<script>");
		expect(w.text()).toContain(HOSTILE);
		expect(w.find(".detail").text()).toBe("detail PA-2");
		const opened = w
			.findAll("button")
			.find((b) => b.attributes("aria-controls") === "held-detail-PA-2");
		expect(opened.attributes("aria-expanded")).toBe("true");
	});

	it("caps the lane body so the rail and an open detail's buttons stay reachable", async () => {
		api.listPendingActionsLane.mockResolvedValue({ rows });
		const w = mountLane();
		await flushPromises();
		const body = w.find("#held-lane-body");
		expect(body.classes()).toContain("max-h-[45vh]");
		expect(body.classes()).toContain("overflow-y-auto");
		expect(body.find(".detail").exists()).toBe(true);
	});

	it("shows a load error with a retry", async () => {
		api.listPendingActionsLane.mockRejectedValueOnce(new Error("boom"));
		const w = mountLane();
		await flushPromises();
		expect(w.find('[role="alert"]').text()).toContain("boom");
		api.listPendingActionsLane.mockResolvedValueOnce({ rows });
		await w
			.findAll("button")
			.find((b) => b.text() === "Try again")
			.trigger("click");
		await flushPromises();
		expect(w.find('[role="alert"]').exists()).toBe(false);
	});

	it("drops a decided row and refreshes the badge", async () => {
		api.listPendingActionsLane
			.mockResolvedValueOnce({ rows })
			.mockResolvedValue({ rows: [rows[0]] });
		const w = mountLane();
		await flushPromises();
		await w.find(".detail").trigger("click");
		await flushPromises();
		expect(refreshApprovalsCount).toHaveBeenCalled();
		expect(w.text()).not.toContain("3 files waiting");
	});

	it("refetches once per burst of approval:new / action:pending frames", async () => {
		vi.useFakeTimers();
		const handlers = {};
		const socket = { on: (ev, fn) => (handlers[ev] = fn), off: vi.fn() };
		api.listPendingActionsLane.mockResolvedValue({ rows });
		mountLane(socket);
		await flushPromises();
		expect(api.listPendingActionsLane).toHaveBeenCalledTimes(1);
		handlers["jarvis:event"]({ kind: "approval:new" });
		handlers["jarvis:event"]({ kind: "action:pending" });
		handlers["jarvis:event"]({ kind: "run:end" });
		vi.advanceTimersByTime(1000);
		await flushPromises();
		expect(api.listPendingActionsLane).toHaveBeenCalledTimes(2);
	});
});
