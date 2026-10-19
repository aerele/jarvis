import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
const mocks = vi.hoisted(() => ({
	setting: vi.fn(),
	set: vi.fn(),
	list: vi.fn(),
	revoke: vi.fn(),
}));
vi.mock("@/api/chatSharing", () => ({
	getSharingSetting: mocks.setting,
	setSharingSetting: mocks.set,
	listAllShares: mocks.list,
	revokeShare: mocks.revoke,
}));
vi.mock("frappe-ui", () => ({
	Button: {
		props: ["label", "disabled"],
		template: '<button :disabled="disabled"><slot/>{{label}}</button>',
	},
	FeatherIcon: { template: "<i/>" },
}));
vi.mock("@/components/settings/SettingsPane.vue", () => ({
	default: { props: ["title"], template: "<section><h1>{{title}}</h1><slot/></section>" },
}));
import SharedChatsPane from "./SharedChatsPane.vue";
beforeEach(() => {
	vi.resetAllMocks();
	mocks.setting.mockResolvedValue({ enabled: true });
	mocks.set.mockResolvedValue({ enabled: false });
	mocks.list.mockResolvedValue({
		rows: [
			{
				name: "S1",
				conversation_title: "Invoice review",
				owner_name: "Owner",
				recipient_name: "Reader",
				shared_on: "2026-10-19",
				hidden: false,
			},
		],
		total: 1,
	});
	mocks.revoke.mockResolvedValue({ ok: true });
});
afterEach(() => vi.useRealTimers());
describe("admin share metadata", () => {
	it("keeps the setting-load failure visible while searching share metadata", async () => {
		mocks.setting.mockRejectedValueOnce(new Error("offline"));
		const w = mount(SharedChatsPane);
		await flushPromises();
		expect(w.text()).toContain("Couldn't read the sharing setting.");
		vi.useFakeTimers();
		await w.get('input[type="search"]').setValue("Owner");
		await vi.advanceTimersByTimeAsync(250);
		await flushPromises();
		expect(w.text()).toContain("Invoice review");
		expect(w.get('input[type="checkbox"]').element.disabled).toBe(true);
		expect(w.text()).toContain("Couldn't read the sharing setting.");
		w.unmount();
	});
	it("returns to the previous page after revoking the last row on a page", async () => {
		const base = {
			name: "S1",
			conversation_title: "Earlier review",
			owner_name: "Owner",
			recipient_name: "Reader",
			shared_on: "2026-10-19",
			hidden: false,
		};
		mocks.list
			.mockReset()
			.mockResolvedValueOnce({ rows: [base], total: 26 })
			.mockResolvedValueOnce({
				rows: [{ ...base, name: "S26", conversation_title: "Last review" }],
				total: 26,
			})
			.mockResolvedValueOnce({ rows: [], total: 25 })
			.mockResolvedValueOnce({ rows: [base], total: 25 });
		const w = mount(SharedChatsPane);
		await flushPromises();
		await w
			.findAll("button")
			.find((b) => b.text() === "Next")
			.trigger("click");
		await flushPromises();
		await w
			.findAll("button")
			.find((b) => b.text() === "Revoke")
			.trigger("click");
		await w
			.findAll("button")
			.find((b) => b.text() === "Confirm revoke")
			.trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Earlier review");
		expect(mocks.list.mock.calls.map(([args]) => args.start)).toEqual([0, 25, 25, 0]);
		w.unmount();
	});
	it("clears a previous load error after searching successfully", async () => {
		mocks.list.mockRejectedValueOnce(new Error("offline"));
		const w = mount(SharedChatsPane);
		await flushPromises();
		expect(w.text()).toContain("Couldn't load shared chats.");
		vi.useFakeTimers();
		await w.get('input[type="search"]').setValue("Owner");
		await vi.advanceTimersByTimeAsync(250);
		await flushPromises();
		expect(w.text()).toContain("Invoice review");
		expect(w.text()).not.toContain("Couldn't load shared chats.");
		w.unmount();
	});

	it("requires an in-page confirmation before revoking", async () => {
		const w = mount(SharedChatsPane);
		await flushPromises();
		expect(w.text()).toContain("Invoice review");
		await w
			.findAll("button")
			.find((b) => b.text() === "Revoke")
			.trigger("click");
		expect(mocks.revoke).not.toHaveBeenCalled();
		expect(w.text()).toContain("Revoke access?");
		await w
			.findAll("button")
			.find((b) => b.text() === "Confirm revoke")
			.trigger("click");
		await flushPromises();
		expect(mocks.revoke).toHaveBeenCalledWith("S1");
		w.unmount();
	});
	it("retains the old toggle state when saving fails", async () => {
		mocks.set.mockRejectedValue(new Error("offline"));
		const w = mount(SharedChatsPane);
		await flushPromises();
		const input = w.get('input[type="checkbox"]');
		expect(input.element.checked).toBe(true);
		await input.setValue(false);
		await flushPromises();
		expect(input.element.checked).toBe(true);
		expect(w.text()).toContain("Couldn't update sharing");
		w.unmount();
	});
});
