import { beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
const api = vi.hoisted(() => ({ call: vi.fn() }));
vi.mock("frappe-ui", () => ({
	call: api.call,
	Breadcrumbs: { template: "<span />" },
	Button: {
		props: ["label", "disabled", "loading"],
		emits: ["click"],
		template:
			'<button :disabled="disabled || loading" :data-label="label" @click="$emit(\'click\')">{{ label }}</button>',
	},
}));
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: { template: '<div><slot name="left-header" /><slot name="right-header" /></div>' },
}));
import Reviews from "./TicketTriageReviews.vue";
const row = () => ({
	name: "AR-1",
	ticket: "0001",
	status: "Pending",
	apply_status: "Pending",
	execution_user: "triage@example.test",
	reviewer: "reviewer@example.test",
	current: { ticket_type: "Question", priority: "Low" },
	proposed: { ticket_type: null, priority: "High" },
	can_confirm: true,
});
beforeEach(() => {
	api.call.mockReset();
});
it("confirms exactly one typed proposal and displays its recorded outcome", async () => {
	api.call
		.mockResolvedValueOnce({ rows: [row()], next_start: 1, has_more: false })
		.mockResolvedValueOnce({ ok: true, status: "Approved" });
	const wrapper = mount(Reviews);
	await flushPromises();
	expect(wrapper.text()).toContain("Low → High");
	expect(wrapper.text()).toContain("Question (no change)");
	expect(wrapper.text()).toContain("Executed as triage@example.test");
	expect(wrapper.text()).toContain("Reviewer reviewer@example.test");
	await wrapper.get('[data-label="Confirm change"]').trigger("click");
	await flushPromises();
	expect(api.call).toHaveBeenLastCalledWith(
		"jarvis.chat.ticket_triage.confirm_ticket_triage_proposal",
		{ name: "AR-1" }
	);
	expect(wrapper.text()).toContain("Change applied");
});
it("keeps an unknown outcome visible and lets refresh recover the recorded state", async () => {
	api.call
		.mockResolvedValueOnce({ rows: [row()], next_start: 1, has_more: false })
		.mockRejectedValueOnce(new Error("lost response"))
		.mockResolvedValueOnce({
			rows: [{ ...row(), status: "Approved", apply_status: "Applied" }],
			next_start: 1,
			has_more: false,
		});
	const wrapper = mount(Reviews);
	await flushPromises();
	await wrapper.get('[data-label="Confirm change"]').trigger("click");
	await flushPromises();
	expect(wrapper.get('[role="alert"]').text()).toContain("Refresh before you retry");
	expect(wrapper.text()).toContain("Awaiting your review");
	await wrapper.get('[data-label="Refresh"]').trigger("click");
	await flushPromises();
	expect(wrapper.text()).toContain("Change applied");
});
it("preserves the list when a refresh fails", async () => {
	api.call
		.mockResolvedValueOnce({ rows: [row()], next_start: 1, has_more: false })
		.mockRejectedValueOnce(new Error("offline"));
	const wrapper = mount(Reviews);
	await flushPromises();
	await wrapper.get('[data-label="Refresh"]').trigger("click");
	await flushPromises();
	expect(wrapper.text()).toContain("Ticket 0001");
	expect(wrapper.get('[role="alert"]').text()).toContain("Could not load reviews");
});
