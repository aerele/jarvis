import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi } from "vitest";

vi.mock("@/api", () => ({ getToolActivity: vi.fn().mockResolvedValue({ runs: [] }) }));
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ chatContext: null }) }));
vi.mock("frappe-ui", () => ({
	FeatherIcon: { props: ["name"], template: '<i :data-icon="name" />' },
}));
vi.mock("@/components/settings/SettingsPane.vue", () => ({
	default: { props: ["title"], template: "<section><h1>{{title}}</h1><slot/></section>" },
}));
vi.mock("@/components/JvSpinner.vue", () => ({ default: { template: "<i/>" } }));
import ActivityPane from "./ActivityPane.vue";

const TEXT =
	"Your admin can see what you do in Jarvis: which records you read, export or change, and how much you use it. Not your messages.";

describe("ActivityPane privacy notice", () => {
	it("shows the exact notice above the tool runs, icon hidden from assistive tech", async () => {
		const w = mount(ActivityPane);
		await flushPromises();
		const notice = w.find('[data-testid="activity-privacy-notice"]');
		expect(notice.exists()).toBe(true);
		expect(notice.text()).toBe(TEXT);
		expect(notice.text()).not.toContain("—");
		expect(notice.find("[data-icon=info]").attributes("aria-hidden")).toBe("true");
		const html = w.html();
		expect(html.indexOf("activity-privacy-notice")).toBeLessThan(
			html.indexOf("Recent tool runs")
		);
	});
});
