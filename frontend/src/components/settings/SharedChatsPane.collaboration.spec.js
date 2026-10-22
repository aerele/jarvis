import { mount, flushPromises } from "@vue/test-utils";
import { it, expect, vi } from "vitest";
const mocks = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock("@/api/chatSharing", () => ({
	getSharingSetting: async () => ({ enabled: true }),
	setSharingSetting: vi.fn(),
	listAllShares: mocks.list,
	revokeShare: vi.fn(),
}));
vi.mock("frappe-ui", () => ({
	Button: { template: "<button><slot/></button>" },
	FeatherIcon: { template: "<i/>" },
}));
vi.mock("@/components/settings/SettingsPane.vue", () => ({
	default: { template: "<section><slot/></section>" },
}));
import SharedChatsPane from "./SharedChatsPane.vue";
it("shows each recipient's current mode and treats older or unknown values as Can view", async () => {
	mocks.list.mockResolvedValue({
		rows: ["Chat", "View", undefined, "Write"].map((access_mode, i) => ({
			name: "S" + i,
			conversation_title: "Supplier review " + i,
			owner_name: "Purchasing owner",
			recipient_name: "Participant " + i,
			access_mode,
			shared_on: "2026-10-21",
		})),
		total: 4,
	});
	const w = mount(SharedChatsPane);
	await flushPromises();
	const rows = w.findAll('[role="row"]').filter((row) => row.find('[role="cell"]').exists());
	expect(rows).toHaveLength(4);
	expect(rows[0].text()).toContain("Can chat");
	for (const row of rows.slice(1)) {
		expect(row.text()).toContain("Can view");
		expect(row.text()).not.toContain("Can chat");
	}
	w.unmount();
});
