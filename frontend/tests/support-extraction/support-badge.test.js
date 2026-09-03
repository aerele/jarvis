import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
	toast: { info: vi.fn(), success: vi.fn(), error: vi.fn() },
	// Expose `options` so the variant tests can assert the menu items; the stub
	// still renders only the trigger slot (the button), like the real closed Dropdown.
	Dropdown: {
		name: "Dropdown",
		props: ["options"],
		template: "<div><slot name='trigger' :open='false'/></div>",
	},
	FeatherIcon: { template: "<i/>" },
}));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));

const store = { awaitingCount: 0, refreshAwaiting: vi.fn() };
vi.mock("@/stores/support", () => ({ useSupportStore: () => store }));

// stores/shell.js reaches window.matchMedia at MODULE TOP LEVEL (an
// unconditional `if (typeof window !== "undefined")` block, not inside a
// function), so it runs the instant this file's real import of UserMenu.vue
// pulls it in — before beforeEach's vi.stubGlobal has a chance to run. Mock it
// out; only openSettings() is ever reached from this component.
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ openSettings: vi.fn() }) }));

import UserMenu from "@/components/shell/UserMenu.vue";
import { agentName } from "@/branding";

const menuLabels = (w) =>
	w
		.findComponent({ name: "Dropdown" })
		.props("options")
		.flatMap((g) => g.items.map((i) => i.label));

// UserMenu calls useJarvisTheme() (→ matchMedia) at :57 and reads
// inject("$session").user at :87 when no cookie is present — jsdom has neither,
// and both are hard TypeErrors at mount (Constraint 16).
const opts = {
	global: {
		stubs: { JarvisMark: true },
		provide: { $session: { user: "someone@example.com", logout() {} } },
	},
};

describe("UserMenu resting support badge (removed - see the chat header pill)", () => {
	// The avatar's resting dot (a bare `[role="status"]` div, fed by this same
	// store) is GONE: a waiting reply now surfaces on the chat header's
	// headphones button instead (ChatView.vue's jv-support-btn count pill), so
	// it registers on the one screen the customer is actually looking at,
	// rather than a corner of the sidebar. These assertions guard against the
	// dot coming back by accident; the "Support tickets · N" menu row below is
	// still the read path and is unaffected.
	beforeEach(() => {
		vi.stubGlobal("matchMedia", () => ({
			matches: false,
			addEventListener() {},
			removeEventListener() {},
		}));
		window.support_available = true;
		window.has_support_access = true;
		store.awaitingCount = 0;
	});

	it("never renders a status dot when nothing is awaiting", () => {
		const w = mount(UserMenu, opts);
		expect(w.find('[role="status"]').exists()).toBe(false);
	});

	it("never renders a status dot even with a reply waiting - that signal moved to the header pill", () => {
		store.awaitingCount = 2;
		const w = mount(UserMenu, opts);
		expect(w.find('[role="status"]').exists()).toBe(false);
	});

	it("never renders a status dot when support is switched off, even with a stale count", () => {
		store.awaitingCount = 5;
		window.support_available = false;
		const w = mount(UserMenu, opts);
		expect(w.find('[role="status"]').exists()).toBe(false);
	});
});

describe("UserMenu variant (chat vs support)", () => {
	beforeEach(() => {
		vi.stubGlobal("matchMedia", () => ({
			matches: false,
			addEventListener() {},
			removeEventListener() {},
		}));
		window.support_available = true;
		window.has_support_access = true;
		store.awaitingCount = 0;
	});

	it("chat (default): title is the agent name; carries a 'Support tickets' inbox item (the read-path — new-ticket lives in the header icon)", () => {
		const w = mount(UserMenu, opts);
		expect(w.text()).toContain(agentName);
		expect(w.text()).not.toContain(`${agentName} Support`);
		const labels = menuLabels(w);
		expect(labels).toContain("Support tickets"); // the inbox read-path (no unread here)
		expect(labels).not.toContain(`Switch to ${agentName} chat`);
		expect(labels).toContain("Change theme");
		expect(labels).toContain("Settings"); // chat/LLM settings belong here
	});

	it("chat: the Support tickets item carries the awaiting count as a flag", () => {
		store.awaitingCount = 3;
		const w = mount(UserMenu, opts);
		expect(menuLabels(w)).toContain("Support tickets · 3");
	});

	it("chat: no Support tickets item when support is switched off", () => {
		window.support_available = false;
		const w = mount(UserMenu, opts);
		expect(menuLabels(w).some((l) => l.startsWith("Support tickets"))).toBe(false);
	});

	it("support: title is '<agent> Support'; menu swaps Support for 'Switch to <agent> chat' and keeps the theme switcher", () => {
		const w = mount(UserMenu, { ...opts, props: { variant: "support" } });
		expect(w.text()).toContain(`${agentName} Support`);
		const labels = menuLabels(w);
		expect(labels).toContain(`Switch to ${agentName} chat`);
		// The inbox item is chat-only; on the support rail you're already there.
		expect(labels.some((l) => l.startsWith("Support tickets"))).toBe(false);
		expect(labels).toContain("Change theme");
		// The Jarvis chat/LLM Settings dialog is out of place in the customer portal.
		expect(labels).not.toContain("Settings");
	});
});
