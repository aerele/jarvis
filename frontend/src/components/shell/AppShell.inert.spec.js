import { describe, it, expect, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * Under an open confirmation only the app is inert (sidebar and routed page): the
 * notification toasts, and the dialogs drawn beside the app, stay reachable.
 */

vi.mock("vue-router", () => ({
	useRoute: () => ({ name: "Chat", meta: { chat: true } }),
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

import { confirm, settleConfirm } from "@/composables/useConfirm";
import AppShell from "./AppShell.vue";

let w;
afterEach(async () => {
	settleConfirm(false);
	await flushPromises();
	if (w) w.unmount();
	w = null;
});

async function mountShell() {
	window.time_zone = "UTC";
	w = mount(AppShell, {
		attachTo: document.body,
		global: {
			provide: { $socket: null },
			stubs: { "router-view": { template: "<main class='stub-page' />" } },
		},
	});
	await flushPromises();
}
const inertEls = () => [...document.querySelectorAll("[inert]")];

describe("AppShell under a confirmation", () => {
	it("is not inert when no question is open", async () => {
		await mountShell();
		expect(inertEls()).toEqual([]);
		expect(document.querySelector(".stub-sidebar")).not.toBeNull();
	});

	it("makes the sidebar and the page inert, and nothing else, while one is open", async () => {
		await mountShell();
		confirm({ title: "Delete?" });
		await flushPromises();
		const [shell, ...more] = inertEls();
		expect(more).toEqual([]);
		expect(shell.contains(document.querySelector(".stub-sidebar"))).toBe(true);
		expect(shell.contains(document.querySelector(".stub-page"))).toBe(true);
		for (const sel of [".stub-toaster", ".stub-dialogs", ".stub-settings", ".stub-confirm"]) {
			expect(shell.contains(document.querySelector(sel))).toBe(false);
			expect(document.querySelector(sel).closest("[inert]")).toBeNull();
		}
	});

	it("takes the attribute off again when the question is answered, with no inert=false left", async () => {
		await mountShell();
		const answer = confirm({ title: "Delete?" });
		await flushPromises();
		expect(inertEls().length).toBe(1);
		settleConfirm(true);
		expect(await answer).toBe(true);
		await flushPromises();
		expect(inertEls()).toEqual([]);
		expect(document.querySelector("[inert='false']")).toBeNull();
	});
});
