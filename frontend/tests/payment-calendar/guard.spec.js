import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// router/index.js statically imports ChatView.vue (the whole chat SPA) and
// onboarding/readiness.js; both are stubbed so importing the module here does
// not pull in that graph. Every other page route is a dynamic import.
vi.mock("@/views/ChatView.vue", () => ({ default: { name: "ChatView", template: "<div/>" } }));
vi.mock("@/onboarding/readiness.js", () => ({ isWorkspaceReady: vi.fn(async () => true) }));

import router, { paymentCalendarGuard } from "@/router/index.js";

const REDIRECT = { name: "DashboardsPage", hash: "#saved" };

function run() {
	let arg = "next-not-called";
	paymentCalendarGuard({}, {}, (v) => (arg = v));
	return arg;
}

describe("paymentCalendarGuard", () => {
	beforeEach(() => {
		delete window.can_view_payment_calendar;
	});
	afterEach(() => {
		delete window.can_view_payment_calendar;
	});

	it("allows navigation when the flag is true", () => {
		window.can_view_payment_calendar = true;
		expect(run()).toBeUndefined();
	});

	it("redirects to the Saved dashboards tab when the flag is false", () => {
		window.can_view_payment_calendar = false;
		expect(run()).toEqual(REDIRECT);
	});

	it("redirects when the flag is undefined (older boot payload)", () => {
		expect(run()).toEqual(REDIRECT);
	});

	it('redirects when the flag is the string "true" (strict boolean check)', () => {
		window.can_view_payment_calendar = "true";
		expect(run()).toEqual(REDIRECT);
	});

	it("redirects when the flag is the number 1 (strict boolean check)", () => {
		window.can_view_payment_calendar = 1;
		expect(run()).toEqual(REDIRECT);
	});
});

describe("PaymentCalendar route", () => {
	it("is protected by paymentCalendarGuard", () => {
		const matched = router.resolve("/dashboards/payment-calendar").matched;
		expect(matched).toHaveLength(1);
		expect(matched[0].name).toBe("PaymentCalendar");
		expect(matched[0].beforeEnter).toBe(paymentCalendarGuard);
	});
});
