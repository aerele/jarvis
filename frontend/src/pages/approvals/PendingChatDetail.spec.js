import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("frappe-ui", () => ({
	Button: {
		props: ["label", "disabled", "loading"],
		emits: ["click"],
		template:
			'<button :disabled="disabled" :aria-label="$attrs[\'aria-label\']" @click="$emit(\'click\')">{{ label }}</button>',
	},
	toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/api/approvals", () => ({ getPendingAction: vi.fn() }));
vi.mock("@/api", () => ({ confirmTool: vi.fn(), dismissTool: vi.fn() }));
const push = vi.fn();
vi.mock("vue-router", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { props: ["label"], template: '<span role="status">{{ label }}</span>' },
}));

import { toast } from "frappe-ui";
import * as approvals from "@/api/approvals";
import * as core from "@/api";
import PendingChatDetail from "./PendingChatDetail.vue";

const HOSTILE = '<img src=x onerror="window.__pwned=1">';

const rec = (over = {}) => ({
	name: "PA-9",
	kind: "chat",
	status: "Pending",
	tool: "create_doc",
	summary: "Create a ToDo",
	card: {
		kind: "create",
		doctype: "ToDo",
		name: null,
		rows: [{ label: "Description", value: "Call the bank" }],
		tables: [],
	},
	conversation: "conv-1",
	conversation_title: "Quarter close",
	origin_page: "",
	can_act: 1,
	reason_code: "",
	reason: "",
	...over,
});

const button = (w, label) => w.findAll("button").find((b) => b.text() === label);

async function mountWith(r) {
	approvals.getPendingAction.mockResolvedValue(r);
	const w = mount(PendingChatDetail, { props: { name: "PA-9" }, attachTo: document.body });
	await flushPromises();
	return w;
}

describe("PendingChatDetail", () => {
	beforeEach(() => vi.clearAllMocks());

	it("shows loading, then the chat's card with Confirm, Discard and Open chat", async () => {
		let resolve;
		approvals.getPendingAction.mockReturnValue(new Promise((r) => (resolve = r)));
		const w = mount(PendingChatDetail, { props: { name: "PA-9" } });
		expect(w.text()).toContain("Loading the proposed action");
		expect(w.attributes("aria-busy")).toBe("true");
		resolve(rec());
		await flushPromises();
		expect(w.attributes("aria-busy")).toBe("false");
		expect(w.text()).toContain("Quarter close");
		expect(w.text()).toContain("Call the bank");
		expect(button(w, "Confirm")).toBeTruthy();
		expect(button(w, "Discard")).toBeTruthy();
		expect(button(w, "Open chat").attributes("aria-label")).toBe("Open chat: Quarter close");
	});

	it("shows a load error with a retry", async () => {
		approvals.getPendingAction.mockRejectedValueOnce(new Error("no longer available"));
		const w = mount(PendingChatDetail, { props: { name: "PA-9" } });
		await flushPromises();
		expect(w.find('[role="alert"]').text()).toContain("no longer available");
		approvals.getPendingAction.mockResolvedValueOnce(rec());
		await button(w, "Try again").trigger("click");
		await flushPromises();
		expect(button(w, "Confirm")).toBeTruthy();
	});

	it("renders hostile model text as text, never HTML", async () => {
		const w = await mountWith(
			rec({
				summary: HOSTILE,
				conversation_title: HOSTILE,
				card: {
					kind: "create",
					doctype: "ToDo",
					rows: [{ label: HOSTILE, value: HOSTILE }],
					tables: [],
				},
			})
		);
		expect(w.element.querySelectorAll("img, script").length).toBe(0);
		expect(w.text()).toContain(HOSTILE);
		expect(window.__pwned).toBeUndefined();
	});

	it("falls back to the summary when the card can't render", async () => {
		const w = await mountWith(rec({ card: "not a card" }));
		expect(w.text()).toContain("Create a ToDo");
	});

	it("confirms through the chat's confirm_tool, then says the chat continues", async () => {
		const w = await mountWith(rec());
		core.confirmTool.mockResolvedValue({ ok: true, pa_status: "Executed" });
		await button(w, "Confirm").trigger("click");
		await flushPromises();
		expect(core.confirmTool).toHaveBeenCalledWith("PA-9", "conv-1");
		expect(toast.success).toHaveBeenCalledWith("Confirmed. Jarvis continues in the chat.");
		expect(w.emitted("decided")).toHaveLength(1);
	});

	it("discards through the chat's dismiss_tool", async () => {
		const w = await mountWith(rec());
		core.dismissTool.mockResolvedValue({ ok: true, reason_code: "discarded" });
		await button(w, "Discard").trigger("click");
		await flushPromises();
		expect(core.dismissTool).toHaveBeenCalledWith("PA-9", "conv-1");
		expect(toast.success).toHaveBeenCalledWith("Discarded. Nothing ran.");
		expect(w.emitted("decided")).toHaveLength(1);
	});

	it("keeps a busy card open with the reason, and drops a decided one", async () => {
		const w = await mountWith(rec());
		core.confirmTool.mockResolvedValueOnce({
			ok: false,
			reason_code: "busy",
			error: { type: "InvalidConfirmation", message: "x" },
		});
		await button(w, "Confirm").trigger("click");
		await flushPromises();
		expect(w.find('[role="alert"]').text()).toContain("being handled right now");
		expect(w.emitted("decided")).toBeUndefined();
		core.confirmTool.mockResolvedValueOnce({
			ok: false,
			reason_code: "stale",
			pa_status: "Failed",
			error: { type: "InvalidConfirmation", message: "x" },
		});
		await button(w, "Confirm").trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith(
			"The record changed after this was proposed. Nothing ran."
		);
		expect(w.emitted("decided")).toHaveLength(1);
	});

	it("escapes a tool's failure words before the toast", async () => {
		const w = await mountWith(rec());
		core.confirmTool.mockResolvedValue({
			ok: false,
			reason_code: "failed",
			pa_status: "Failed",
			error: { message: HOSTILE },
		});
		await button(w, "Confirm").trigger("click");
		await flushPromises();
		const [html] = toast.error.mock.calls[0];
		expect(html).not.toContain("<img");
		expect(html).toContain("&lt;img");
	});

	it("disables both decisions while one is in flight", async () => {
		const w = await mountWith(rec());
		let resolve;
		core.confirmTool.mockReturnValue(new Promise((r) => (resolve = r)));
		await button(w, "Confirm").trigger("click");
		expect(button(w, "Discard").attributes("disabled")).toBeDefined();
		await button(w, "Discard").trigger("click");
		expect(core.dismissTool).not.toHaveBeenCalled();
		resolve({ ok: true });
		await flushPromises();
	});

	it("takes the lane's id on its root so the row's aria-controls resolves", async () => {
		approvals.getPendingAction.mockResolvedValue(rec());
		const w = mount(PendingChatDetail, {
			props: { name: "PA-9" },
			attrs: { id: "held-detail-PA-9" },
		});
		await flushPromises();
		expect(w.attributes("id")).toBe("held-detail-PA-9");
	});

	it("opens the conversation, or the dashboard pane it came from", async () => {
		const w = await mountWith(rec());
		await button(w, "Open chat").trigger("click");
		expect(push).toHaveBeenCalledWith("/c/conv-1");
		const d = await mountWith(rec({ origin_page: "dashboards" }));
		await button(d, "Open chat").trigger("click");
		expect(push).toHaveBeenLastCalledWith({
			name: "DashboardsPage",
			query: { conversation: "conv-1" },
		});
	});

	it("shows a closed card's status instead of the decisions", async () => {
		const w = await mountWith(rec({ status: "Executing", can_act: 0 }));
		expect(w.find('[role="status"]').text()).toBe("Running now…");
		expect(button(w, "Confirm")).toBeFalsy();
		expect(button(w, "Open chat")).toBeTruthy();
	});
});
