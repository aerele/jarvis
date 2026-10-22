import { describe, it, expect, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
const m = vi.hoisted(() => ({ route: { name: "SharedConversation", meta: { ownHeader: true } } }));

// A route owning its header keeps the sidebar without a duplicate shell strip.

vi.mock("vue-router", () => ({
	useRoute: () => m.route,
	useRouter: () => ({
		afterEach: () => () => {},
		isReady: async () => {},
		replace: vi.fn(),
	}),
}));
vi.mock("frappe-ui", () => ({
	FrappeUIProvider: { template: "<div><slot /></div>" },
	Dialogs: { template: "<div class='stub-dialogs' />" },
	FeatherIcon: { template: "<i/>" },
	setConfig: vi.fn(),
}));
vi.mock("@/api", () => ({ getChatUiSettings: vi.fn(async () => ({})) }));
vi.mock("@/stores/shell", async () => {
	const { reactive } = await import("vue");
	const store = reactive({
		mobile: false,
		mobileDrawerOpen: false,
		sidebarCollapsed: false,
		paletteOpen: false,
		openSettings: vi.fn(),
		requestNewChat: vi.fn(),
		loadConversations: vi.fn(),
		refreshBadges: vi.fn(),
		stopBadgeTimer: vi.fn(),
	});
	return { useShellStore: () => store };
});
vi.mock("@/composables/useShortcuts", () => ({ useShortcuts: vi.fn() }));
vi.mock("@/notify/globalNotifier", () => ({ attachGlobalNotifier: () => () => {} }));
vi.mock("@/notify/NotifyToaster.vue", () => ({
	default: { template: "<div class='stub-toaster'><button>Dismiss</button></div>" },
}));
vi.mock("@/onboarding/readiness.js", () => ({
	needsOnboarding: async () => false,
	regateOnRouteChange: vi.fn(),
}));
vi.mock("@/noticeGate", async () => {
	const { ref } = await import("vue");
	return { showNotice: ref(false) };
});
vi.mock("./Sidebar.vue", () => ({ default: { template: "<nav class='stub-sidebar' />" } }));
vi.mock("./JarvisCommandPalette.vue", () => ({ default: { template: "<div />" } }));
vi.mock("./SettingsDialog.vue", () => ({
	default: { template: "<div class='stub-settings' />" },
}));
vi.mock("./ConfirmDialog.vue", () => ({ default: { template: "<div class='stub-confirm' />" } }));
vi.mock("@/components/support/SupportCopyPromptDialog.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("./OnboardingGate.vue", () => ({ default: { template: "<div />" } }));
vi.mock("./UpdateNoticeGate.vue", () => ({ default: { template: "<div />" } }));
vi.mock("@/assets/settings.css", () => ({}));

import AppShell from "./AppShell.vue";

import { useShellStore } from "@/stores/shell";
let w;
afterEach(() => {
	w?.unmount();
	w = null;
});
async function open(meta, mobile) {
	m.route = { name: "TestRoute", meta };
	useShellStore().mobile = mobile;
	window.time_zone = "UTC";
	w = mount(AppShell, {
		global: {
			provide: { $socket: null },
			stubs: { "router-view": { template: "<main class='routed-page'/>" } },
		},
	});
	await flushPromises();
}
describe("routes owning their header", () => {
	it.each([false, true])(
		"keeps navigation and removes the duplicate header on mobile=%s",
		async (mobile) => {
			await open({ ownHeader: true }, mobile);
			expect(w.find("#app-header").exists()).toBe(false);
			expect(w.find(".stub-sidebar").exists()).toBe(true);
			expect(w.find(".routed-page").exists()).toBe(true);
			expect(m.route.meta.chat).toBeUndefined();
		}
	);
	it.each([false, true])("keeps the ordinary page header on mobile=%s", async (mobile) => {
		await open({}, mobile);
		expect(w.find("#app-header").exists()).toBe(true);
		expect(w.find(".stub-sidebar").exists()).toBe(true);
	});
});
