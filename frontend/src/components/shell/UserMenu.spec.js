import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { ref } from "vue";

// The shell's brand card: its mark is a HolidayMark, so a holiday item can show on it.
// The menu library and the stores are stand-ins; this is about the mark and its props.
const state = vi.hoisted(() => ({ hold: null }));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("frappe-ui", () => ({
	// The menu renders its trigger and nothing else.
	Dropdown: { template: '<div><slot name="trigger" :open="false" /></div>' },
	FeatherIcon: { template: "<i />" },
}));
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ openSettings: vi.fn() }) }));
vi.mock("@/stores/support", () => ({
	useSupportStore: () => ({ awaitingCount: 0, refreshAwaiting: vi.fn() }),
}));
vi.mock("@/theme", () => ({
	useJarvisTheme: () => ({ effectiveDark: { value: false }, toggleTheme: vi.fn() }),
}));
vi.mock("@/maintenanceGate", async () => {
	const { ref: vueRef } = await import("vue");
	state.hold = vueRef(false);
	return { holdActive: state.hold };
});
vi.mock("@/lib/focusOpenMenu", () => ({ focusOpenMenu: vi.fn() }));
// The mark is replaced by a stand-in that keeps its props visible.
vi.mock("@/components/holiday/HolidayMark.vue", () => ({
	default: {
		name: "HolidayMark",
		props: {
			size: Number,
			radius: Number,
			mood: String,
			hoverPeek: Boolean,
			peek: Boolean,
			idlePeek: Boolean,
			welcome: Boolean,
		},
		template: '<span class="mark" />',
	},
}));

import UserMenu from "./UserMenu.vue";
import HolidayMark from "@/components/holiday/HolidayMark.vue";

const mountMenu = (props = {}) =>
	mount(UserMenu, {
		props,
		global: {
			provide: { $session: ref({ user: "priya@example.test", logout: vi.fn() }).value },
		},
	});

describe("UserMenu brand mark", () => {
	beforeEach(() => {
		state.hold.value = false;
	});

	it("is a HolidayMark, 28px with a 7px corner, resting and idle-blinking", () => {
		const w = mountMenu();
		const mark = w.findComponent(HolidayMark);
		expect(mark.exists()).toBe(true);
		expect(mark.props()).toMatchObject({
			size: 28,
			radius: 7,
			mood: "star",
			peek: false,
			idlePeek: true,
			welcome: false,
		});
		// The one mark on the card, and no plain JarvisMark beside it.
		expect(w.findAllComponents(HolidayMark)).toHaveLength(1);
		expect(w.findComponent({ name: "JarvisMark" }).exists()).toBe(false);
	});

	it("peeks while the whole card is hovered", async () => {
		const w = mountMenu();
		const card = w.get("button");
		await card.trigger("mouseenter");
		expect(w.findComponent(HolidayMark).props("peek")).toBe(true);
		await card.trigger("mouseleave");
		expect(w.findComponent(HolidayMark).props("peek")).toBe(false);
	});

	it("takes the sleepy mood in the upgrade hold", async () => {
		const w = mountMenu();
		state.hold.value = true;
		await w.vm.$nextTick();
		expect(w.findComponent(HolidayMark).props("mood")).toBe("upgrading");
	});
});
