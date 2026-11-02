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
import Reviews from "./ReorderReviews.vue";
const run = () => ({
	run: "RUN-1",
	mode: "live",
	execution_user: "runner@example.test",
	reviewer: "buyer@example.test",
	rows: [
		{
			id: "ROW-1",
			item: "PUMP",
			warehouse: "Stores",
			stock_uom: "Nos",
			projected_qty: "5",
			reorder_level: "10",
			proposed_qty: "8",
			status: "Pending",
			reason: "",
		},
	],
});
beforeEach(() => api.call.mockReset());
it("shows the source units and identities and records a review without claiming a purchase", async () => {
	api.call
		.mockResolvedValueOnce({ rows: [run()], next_start: 1, has_more: false })
		.mockResolvedValueOnce({ ok: true, status: "Approved" });
	const wrapper = mount(Reviews);
	await flushPromises();
	expect(wrapper.text()).toContain("Executed as runner@example.test");
	expect(wrapper.text()).toContain("Reviewer buyer@example.test");
	expect(wrapper.text()).toContain("8 Nos");
	await wrapper.get('[data-label="Approve suggestion"]').trigger("click");
	await flushPromises();
	expect(api.call).toHaveBeenLastCalledWith("jarvis.chat.reorder_review.decide", {
		name: "RUN-1",
		row_id: "ROW-1",
		approve: 1,
	});
	expect(wrapper.text()).toContain("Approved suggestion");
	expect(wrapper.text()).toContain("Approval records your review only");
});
it("keeps an unknown outcome pending until refresh gets the recorded state", async () => {
	const approved = run();
	approved.rows[0].status = "Approved";
	api.call
		.mockResolvedValueOnce({ rows: [run()], next_start: 1, has_more: false })
		.mockRejectedValueOnce(new Error("lost response"))
		.mockResolvedValueOnce({ rows: [approved], next_start: 1, has_more: false });
	const wrapper = mount(Reviews);
	await flushPromises();
	await wrapper.get('[data-label="Approve suggestion"]').trigger("click");
	await flushPromises();
	expect(wrapper.text()).toContain("Awaiting your review");
	expect(wrapper.get('[role="alert"]').text()).toContain("Refresh before you retry");
	await wrapper.get('[data-label="Refresh"]').trigger("click");
	await flushPromises();
	expect(wrapper.text()).toContain("Approved suggestion");
});
it("shows preview and held cases without decision controls", async () => {
	const preview = run();
	preview.mode = "shadow";
	const held = run();
	held.run = "RUN-2";
	held.rows[0].status = "Held";
	held.rows[0].proposed_qty = null;
	held.rows[0].reason = "draft_supply_exists";
	api.call.mockResolvedValueOnce({ rows: [preview, held], next_start: 2, has_more: false });
	const wrapper = mount(Reviews);
	await flushPromises();
	expect(wrapper.find('[data-label="Approve suggestion"]').exists()).toBe(false);
	expect(wrapper.text()).toContain("A draft request or order already exists");
});
