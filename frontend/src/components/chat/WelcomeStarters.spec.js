import { describe, it, expect, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createRouter, createMemoryHistory } from "vue-router";

// The catalog fetches capabilities on mount; it is not under test here.
vi.mock("@/components/CapabilityCatalog.vue", () => ({
	default: { name: "CapabilityCatalog", template: '<div data-testid="catalog"/>' },
}));

import WelcomeStarters from "./WelcomeStarters.vue";

const CARDS = [{ title: "Dues", prompt: "What is due this week?" }];
const tint = () => ({ bg: "#eee", fg: "#333", icon: "" });

const mounted = [];
afterEach(() => {
	mounted.forEach((w) => w.unmount());
	mounted.length = 0;
	delete window.can_view_payment_calendar;
});

// The flag is a boot-time global, read once in setup, so it is set BEFORE mount.
async function mountStarters(flag) {
	if (flag === undefined) delete window.can_view_payment_calendar;
	else window.can_view_payment_calendar = flag;
	const router = createRouter({
		history: createMemoryHistory(),
		routes: [
			{ path: "/", component: { template: "<div/>" } },
			{ path: "/dashboards/payment-calendar", component: { template: "<div/>" } },
		],
	});
	await router.push("/");
	const w = mount(WelcomeStarters, {
		props: { cards: CARDS, tint },
		global: { plugins: [router] },
	});
	mounted.push(w);
	await flushPromises();
	return w;
}

const CALENDAR_LINK = 'a[href$="/dashboards/payment-calendar"]';

describe("WelcomeStarters: Payment Calendar link", () => {
	it("shows the link when the flag is true", async () => {
		const w = await mountStarters(true);
		const link = w.find(CALENDAR_LINK);
		expect(link.exists()).toBe(true);
		expect(link.text()).toContain("Payment Calendar");
	});

	it("hides the link when the flag is false", async () => {
		const w = await mountStarters(false);
		expect(w.find(CALENDAR_LINK).exists()).toBe(false);
		expect(w.text()).not.toContain("Payment Calendar");
	});

	it("hides the link when the flag is undefined", async () => {
		const w = await mountStarters(undefined);
		expect(w.find(CALENDAR_LINK).exists()).toBe(false);
	});

	it('hides the link when the flag is the string "true" (strict boolean check)', async () => {
		const w = await mountStarters("true");
		expect(w.find(CALENDAR_LINK).exists()).toBe(false);
	});

	it("keeps the starter cards and the capability catalog when the link is hidden", async () => {
		const w = await mountStarters(false);
		expect(w.findAll("button.jv-suggest")).toHaveLength(CARDS.length);
		expect(w.find('[data-testid="catalog"]').exists()).toBe(true);
	});
});
