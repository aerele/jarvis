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

describe("admin sharing usability and recovery", () => {
	const button = (w, label) => w.findAll("button").find((b) => b.text() === label);
	it("retries reading the setting without closing Settings", async () => {
		mocks.setting.mockRejectedValueOnce(new Error("offline"));
		const w = mount(SharedChatsPane);
		await flushPromises();
		expect(w.get('input[type="checkbox"]').element.disabled).toBe(true);
		expect(button(w, "Retry setting")).toBeDefined();
		await button(w, "Retry setting").trigger("click");
		await flushPromises();
		expect(mocks.setting).toHaveBeenCalledTimes(2);
		expect(w.get('input[type="checkbox"]').element.disabled).toBe(false);
		expect(w.text()).not.toContain("Couldn't read the sharing setting.");
		w.unmount();
	});
	it("retries metadata reads directly and does not present a failure as an empty result", async () => {
		mocks.list.mockRejectedValueOnce(new Error("offline"));
		const w = mount(SharedChatsPane);
		await flushPromises();
		expect(w.text()).not.toContain("No shared chats match your search.");
		expect(button(w, "Retry loading shares")).toBeDefined();
		await button(w, "Retry loading shares").trigger("click");
		await flushPromises();
		expect(mocks.list).toHaveBeenCalledTimes(2);
		expect(w.text()).toContain("Invoice review");
		expect(w.text()).not.toContain("Couldn't load shared chats.");
		w.unmount();
	});
	it("explains an empty grant list without pretending a search is active", async () => {
		mocks.list.mockResolvedValue({ rows: [], total: 0 });
		const w = mount(SharedChatsPane);
		await flushPromises();
		expect(w.text()).toContain("No chats have been shared yet.");
		expect(w.text()).not.toContain("No shared chats match your search.");
		w.unmount();
	});
	it("clears a people search and restores the unfiltered first page", async () => {
		vi.useFakeTimers();
		const w = mount(SharedChatsPane);
		await flushPromises();
		mocks.list.mockResolvedValueOnce({ rows: [], total: 0 });
		await w.get('input[type="search"]').setValue("Nobody");
		await vi.advanceTimersByTimeAsync(250);
		await flushPromises();
		expect(w.text()).toContain("No shared chats match your search.");
		expect(button(w, "Clear search")).toBeDefined();
		await button(w, "Clear search").trigger("click");
		await vi.advanceTimersByTimeAsync(250);
		await flushPromises();
		expect(w.get('input[type="search"]').element.value).toBe("");
		expect(mocks.list).toHaveBeenLastCalledWith({ start: 0, page_length: 25, search: "" });
		expect(w.text()).toContain("Invoice review");
		w.unmount();
	});
	it("keeps the sharing setting usable when only metadata is loading", async () => {
		mocks.list.mockReturnValue(new Promise(() => {}));
		const w = mount(SharedChatsPane);
		await flushPromises();
		expect(w.get('input[type="checkbox"]').element.disabled).toBe(false);
		w.unmount();
	});
	it("reads authoritative state after a save loses its acknowledgement", async () => {
		mocks.setting
			.mockResolvedValueOnce({ enabled: true })
			.mockResolvedValueOnce({ enabled: false });
		mocks.set.mockRejectedValueOnce(new Error("response lost after server commit"));
		const w = mount(SharedChatsPane);
		await flushPromises();
		await w.get('input[type="checkbox"]').setValue(false);
		await flushPromises();
		expect(mocks.setting).toHaveBeenCalledTimes(2);
		expect(w.get('input[type="checkbox"]').element.checked).toBe(false);
		expect(w.text()).not.toContain("The previous setting is kept.");
		expect(w.text()).toContain("Sharing is off");
		w.unmount();
	});
	it("names the chat and recipient in confirmation, and Cancel performs no revoke", async () => {
		const w = mount(SharedChatsPane);
		await flushPromises();
		await button(w, "Revoke").trigger("click");
		const confirmation = w.get('[role="group"][aria-label="Revoke access?"]');
		expect(confirmation.text()).toContain("Reader");
		expect(confirmation.text()).toContain("Invoice review");
		await button(w, "Cancel").trigger("click");
		expect(w.find('[role="group"][aria-label="Revoke access?"]').exists()).toBe(false);
		expect(mocks.revoke).not.toHaveBeenCalled();
		w.unmount();
	});
	it("disables stale revoke controls immediately while a new search is pending", async () => {
		vi.useFakeTimers();
		const w = mount(SharedChatsPane);
		await flushPromises();
		await w.get('input[type="search"]').setValue("new recipient");
		expect(button(w, "Revoke").element.disabled).toBe(true);
		w.unmount();
	});
	it("blocks setting changes when the save and its authoritative recheck both fail", async () => {
		mocks.set.mockRejectedValueOnce(new Error("timeout"));
		mocks.setting
			.mockResolvedValueOnce({ enabled: true })
			.mockRejectedValueOnce(new Error("offline"));
		const w = mount(SharedChatsPane);
		await flushPromises();
		await w.get('input[type="checkbox"]').setValue(false);
		await flushPromises();
		expect(w.get('input[type="checkbox"]').element.disabled).toBe(true);
		expect(button(w, "Retry setting")).toBeDefined();
		expect(w.text()).not.toContain("The previous setting is kept.");
		w.unmount();
	});
});

describe("uncertain admin revocation", () => {
	it("reloads metadata after a lost acknowledgement without claiming a confirmed revoke", async () => {
		mocks.revoke.mockRejectedValueOnce(new Error("response lost after commit"));
		const w = mount(SharedChatsPane);
		await flushPromises();
		mocks.list.mockResolvedValueOnce({ rows: [], total: 0 });
		await w
			.findAll("button")
			.find((b) => b.text() === "Revoke")
			.trigger("click");
		await w
			.findAll("button")
			.find((b) => b.text() === "Confirm revoke")
			.trigger("click");
		await flushPromises();
		expect(mocks.list).toHaveBeenCalledTimes(2);
		expect(w.text()).not.toContain("Invoice review");
		expect(w.text()).not.toContain("Access revoked.");
		expect(w.text()).toContain("Couldn't confirm revocation.");
		w.unmount();
	});
});
