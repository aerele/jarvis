import { describe, it, expect, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * Under an open confirmation only the app is inert (sidebar and routed page): the
 * notification toasts, and the dialogs drawn beside the app, stay reachable.
 */

vi.mock("vue-router", () => ({
	useRoute: () => ({ name: "FilesList", meta: {} }),
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
	Button: { template: "<button class='btn'><slot /></button>" },
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

import { useShellStore } from "@/stores/shell";
import LayoutHeader from "@/components/LayoutHeader.vue";
import AppShell from "./AppShell.vue";

// A page whose header rides LayoutHeader's Teleport into the shell's #app-header.
const Page = {
	components: { LayoutHeader },
	template:
		'<main><LayoutHeader><template #right-header><span id="probe">controls</span></template></LayoutHeader></main>',
};

let w;
afterEach(() => {
	if (w) w.unmount();
	w = null;
	useShellStore().mobile = false;
});

async function mountShell() {
	window.time_zone = "UTC";
	w = mount(AppShell, {
		attachTo: document.body,
		global: { provide: { $socket: null }, stubs: { "router-view": Page } },
	});
	await flushPromises();
}

describe("AppShell header slot across a live resize", () => {
	it.each([
		[false, true],
		[true, false],
	])("keeps a teleported page header when mobile goes %s -> %s", async (from, to) => {
		const store = useShellStore();
		store.mobile = from;
		await mountShell();
		expect(document.querySelector("#probe")).not.toBeNull();
		store.mobile = to;
		await flushPromises();
		expect(document.querySelectorAll("#app-header").length).toBe(1);
		expect(document.querySelector("#app-header #probe")).not.toBeNull();
	});
});
