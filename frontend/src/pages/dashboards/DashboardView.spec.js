import { describe, it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const api = vi.hoisted(() => ({
	getDashboard: vi.fn(async () => ({
		name: "sales",
		dashboard_title: "Sales",
		html: "<h1>Sales</h1>",
		filters: [{ fieldname: "item", default: "DEFAULT" }],
		theme: "Jarvis",
	})),
	getDashboardsCaps: vi.fn(async () => ({})),
	deleteDashboard: vi.fn(),
	saveDashboard: vi.fn(),
	getDashboardConversation: vi.fn(),
	listDashboardConversations: vi.fn(async () => []),
}));
vi.mock("@/api/dashboards", () => api);
vi.mock("@/api", () => ({ getCanvas: vi.fn() }));
vi.mock("@/data/session", () => ({ session: { user: "review@example.test" } }));
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ setSpaciousView: vi.fn() }) }));
vi.mock("vue-router", () => ({
	useRoute: () => ({ query: { f_item: "ITEM-1" } }),
	useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("frappe-ui", () => ({
	Button: {
		props: ["label", "loading", "disabled"],
		template: '<button :disabled="disabled || loading">{{ label }}</button>',
	},
	Badge: { template: "<span />" },
	Breadcrumbs: { template: "<span />" },
	Dialog: { template: "<span />" },
	Dropdown: { template: "<div><slot /></div>" },
	ErrorMessage: { template: "<span />" },
	FeatherIcon: { template: "<span />" },
	toast: { error: vi.fn(), success: vi.fn() },
}));
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: {
		template:
			'<header><slot name="left-header" /><slot name="right-header" /></header>',
	},
}));
vi.mock("./SaveDashboardDialog.vue", () => ({ default: { template: "<div />" } }));
vi.mock("./DashboardFilterBar.vue", () => ({ default: { template: "<div />" } }));
vi.mock("./DashboardCanvas.vue", () => ({
	default: {
		name: "DashboardCanvas",
		props: ["filters", "theme"],
		data: () => ({ loading: false, rebuilds: 0 }),
		methods: {
			rebuild() {
				this.rebuilds++;
				this.loading = true;
			},
		},
		template: '<div class="canvas" />',
	},
}));
vi.mock("./DashboardChatPane.vue", () => ({ default: { template: "<div />" } }));
vi.mock("./SavedDashboardsTab.vue", () => ({ default: { template: "<div />" } }));
vi.mock("@/components/list/TabBar.vue", () => ({ default: { template: "<div />" } }));
import DashboardView from "./DashboardView.vue";
import DashboardsPage from "./DashboardsPage.vue";

describe("dashboard refresh button", () => {
	it("reloads the canvas, preserves filters and theme, and blocks duplicate clicks", async () => {
		const w = mount(DashboardView, { props: { id: "sales" } });
		await flushPromises();
		const canvas = w.findComponent({ name: "DashboardCanvas" });
		const refresh = w.findAll("button").find((b) => b.text() === "Refresh");
		const theme = canvas.props("theme");
		await refresh.trigger("click");
		expect(canvas.vm.rebuilds).toBe(1);
		expect(canvas.props("filters")).toEqual({ item: "ITEM-1" });
		expect(canvas.props("theme")).toBe(theme);
		expect(refresh.element.disabled).toBe(true);
		await refresh.trigger("click");
		expect(canvas.vm.rebuilds).toBe(1);
		expect(api.getDashboard).toHaveBeenCalledTimes(1);
		canvas.vm.loading = false;
		await flushPromises();
		expect(refresh.element.disabled).toBe(false);
		w.unmount();
	});
});

describe("builder refresh button", () => {
	it("disables refresh on an empty canvas and preserves unsaved content when refreshed", async () => {
		const w = mount(DashboardsPage, { global: { stubs: { "router-link": true } } });
		await flushPromises();
		const refresh = w.findAll("button").find((b) => b.text() === "Refresh");
		expect(refresh.element.disabled).toBe(true);
		const html = "<h1>Unsaved dashboard</h1>";
		w.vm.builderHtml = html;
		w.vm.builderFilters = { item: "UNSAVED" };
		w.vm.builderAppliedFilters = { item: "UNSAVED" };
		await flushPromises();
		const canvas = w.findComponent({ name: "DashboardCanvas" });
		const theme = canvas.props("theme");
		await refresh.trigger("click");
		expect(canvas.vm.rebuilds).toBe(1);
		expect(w.vm.builderHtml).toBe(html);
		expect(w.vm.builderFilters).toEqual({ item: "UNSAVED" });
		expect(canvas.props("filters")).toEqual({ item: "UNSAVED" });
		expect(canvas.props("theme")).toBe(theme);
		expect(refresh.element.disabled).toBe(true);
		await refresh.trigger("click");
		expect(canvas.vm.rebuilds).toBe(1);
		canvas.vm.loading = false;
		await flushPromises();
		expect(refresh.element.disabled).toBe(false);
		w.unmount();
	});
});
