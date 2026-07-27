import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
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

describe("UserMenu resting support badge", () => {
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

	it("shows no dot when nothing is awaiting", () => {
		const w = mount(UserMenu, opts);
		expect(w.find('[role="status"]').exists()).toBe(false);
	});

	it("shows the dot with an accessible label when a reply is waiting", () => {
		// The whole point of the resting dot: it must register while the user is
		// heads-down in chat, WITHOUT opening the menu — and a bare coloured dot
		// is invisible to a screen reader, so the label is not optional. The
		// count is TICKETS (Replied/Resolved), not individual replies — the
		// original "N support reply/replies awaiting you" wording misnamed the
		// unit.
		store.awaitingCount = 2;
		const w = mount(UserMenu, opts);
		const dot = w.find('[role="status"]');
		expect(dot.exists()).toBe(true);
		expect(dot.attributes("aria-label")).toBe("2 tickets awaiting your reply");
	});

	it("singularises one ticket", () => {
		store.awaitingCount = 1;
		const w = mount(UserMenu, opts);
		expect(w.find('[role="status"]').attributes("aria-label")).toBe(
			"1 ticket awaiting your reply"
		);
	});

	it("never shows the dot when support is switched off, even with a stale count", () => {
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

	it("chat (default): title is the agent name; menu links to Support, not chat", () => {
		const w = mount(UserMenu, opts);
		expect(w.text()).toContain(agentName);
		expect(w.text()).not.toContain(`${agentName} Support`);
		const labels = menuLabels(w);
		expect(labels).toContain("Support");
		expect(labels).not.toContain(`Switch to ${agentName} chat`);
		expect(labels).toContain("Change theme");
	});

	it("support: title is '<agent> Support'; menu swaps Support for 'Switch to <agent> chat' and keeps the theme switcher", () => {
		const w = mount(UserMenu, { ...opts, props: { variant: "support" } });
		expect(w.text()).toContain(`${agentName} Support`);
		const labels = menuLabels(w);
		expect(labels).toContain(`Switch to ${agentName} chat`);
		expect(labels).not.toContain("Support");
		expect(labels).toContain("Change theme");
	});
});
