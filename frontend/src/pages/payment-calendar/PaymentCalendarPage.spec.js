import { vi, afterEach } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { mount, flushPromises } from "@vue/test-utils";
vi.mock("@/api/paymentCalendar", async () =>
	(await import("../../../tests/payment-calendar/fixtures")).fakeApi()
);
vi.mock("@/utils/datetime", () => ({ siteTimezone: () => "Asia/Kolkata" }));
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ mobile: false }) }));
const router = vi.hoisted(() => ({
	back: vi.fn(),
	push: vi.fn(),
	resolve: vi.fn(({ params }) => ({ fullPath: `/c/${params.id}` })),
}));
vi.mock("vue-router", () => ({ useRoute: () => ({ query: {} }), useRouter: () => router }));
vi.mock("frappe-ui", () => ({
	Breadcrumbs: { template: "<nav>Dashboards</nav>", props: ["items"] },
	FeatherIcon: { template: '<span class="fake-icon" :data-name="name" />', props: ["name"] },
	Dialog: {
		props: ["modelValue", "options"],
		template:
			'<div v-if="modelValue" role="dialog"><slot name="body-content"/><slot name="actions"/></div>',
	},
}));
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: { template: '<header><slot name="left-header"/></header>' },
}));
import PaymentCalendarPage from "./PaymentCalendarPage.vue";
import * as api from "@/api/paymentCalendar";
import { ok, refusal, complete, deferred } from "../../../tests/payment-calendar/fixtures";

describe("Native Payment Calendar page", () => {
	let wrapper;
	beforeEach(() => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date("2026-10-24T12:00:00Z"));
		vi.clearAllMocks();
	});
	afterEach(() => {
		wrapper?.unmount();
		vi.useRealTimers();
	});
	async function render() {
		wrapper = mount(PaymentCalendarPage);
		await flushPromises();
	}
	it("connects a native date selection to one snapshot and independent detail tabs", async () => {
		await render();
		expect(wrapper.text()).toContain("Payment Calendar");
		await wrapper.get('[data-date="2026-10-25"]').trigger("click");
		await flushPromises();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(1);
		expect(api.getDayDetail.mock.calls.at(-1)[1]).toBe("2026-10-25");
		await wrapper.get(".pcal-tabs button:nth-child(3)").trigger("click");
		await flushPromises();
		expect(api.getDayDetail.mock.calls.at(-1)[2]).toBe("Receivable");
	});
	it("moves focus to the title on arrival from the chat hand-off, and only then", async () => {
		const host = document.createElement("div");
		document.body.appendChild(host);
		try {
			window.history.replaceState({}, "");
			wrapper = mount(PaymentCalendarPage, { attachTo: host });
			await flushPromises();
			const title = wrapper.get("#pcal-title");
			expect(title.attributes("tabindex")).toBe("-1");
			expect(document.activeElement).not.toBe(title.element);
			wrapper.unmount();
			window.history.replaceState({ fromChat: true, chatConv: "c-1" }, "");
			wrapper = mount(PaymentCalendarPage, { attachTo: host });
			await flushPromises();
			expect(document.activeElement).toBe(wrapper.get("#pcal-title").element);
			// one-shot: the flag is cleared, so a remount (reload / Forward) does not re-focus
			expect(window.history.state?.fromChat).toBeUndefined();
			wrapper.unmount();
			(document.activeElement || document.body).blur?.();
			wrapper = mount(PaymentCalendarPage, { attachTo: host });
			await flushPromises();
			expect(document.activeElement).not.toBe(wrapper.get("#pcal-title").element);
		} finally {
			window.history.replaceState({}, "");
			host.remove();
		}
	});
	it("never draws a ring on the title: it is not interactive", () => {
		const css = fs.readFileSync(path.resolve(__dirname, "payment-calendar.css"), "utf8");
		expect(css).toMatch(/#pcal-title:focus\s*\{\s*outline:\s*none;\s*\}/);
		expect(css).not.toMatch(/#pcal-title:focus:not\(:focus-visible\)/);
	});
	it("Back to chat uses the page's own focus ring, with no rule of its own", async () => {
		window.history.replaceState({ fromChat: true, chatConv: "c-1", back: "/c/c-1" }, "");
		wrapper = mount(PaymentCalendarPage);
		await flushPromises();
		window.history.replaceState({}, "");
		const back = wrapper.find(".pcal-back");
		// a plain button of the page, not a frappe-ui Button with a ring of its own
		expect(back.element.tagName).toBe("BUTTON");
		expect(back.attributes("type")).toBe("button");
		expect(back.classes()).toContain("pcal-text-button");
		expect(back.element.closest(".pcal-root")).not.toBeNull();
		const css = fs.readFileSync(path.resolve(__dirname, "payment-calendar.css"), "utf8");
		expect(css).toMatch(/\.pcal-root button:focus-visible/);
		expect(css).not.toMatch(/\.pcal-back[^{,]*:focus-visible/);
	});
	describe("Back to chat", () => {
		const backBtn = () => wrapper.find(".pcal-back");
		async function arrive(state) {
			window.history.replaceState(state, "");
			wrapper = mount(PaymentCalendarPage);
			await flushPromises();
		}
		afterEach(() => window.history.replaceState({}, ""));
		it("is shown above the eyebrow when the hand-off carried a conversation", async () => {
			await arrive({ fromChat: true, chatConv: "c-1", back: "/c/c-1" });
			expect(backBtn().exists()).toBe(true);
			expect(backBtn().text()).toBe("Back to chat");
			const html = wrapper.html();
			expect(html.indexOf("pcal-back")).toBeLessThan(html.indexOf("pcal-eyebrow"));
		});
		it("shows an arrow icon that assistive tech skips, in front of the label", async () => {
			await arrive({ fromChat: true, chatConv: "c-1", back: "/c/c-1" });
			const icon = backBtn().find(".fake-icon");
			expect(icon.attributes("data-name")).toBe("arrow-left");
			expect(icon.attributes("aria-hidden")).toBe("true");
			expect(backBtn().text()).toBe("Back to chat");
			expect(backBtn().html().indexOf("fake-icon")).toBeLessThan(
				backBtn().html().indexOf("Back to chat")
			);
		});
		it("sits above the title row, so the Refresh button does not move", async () => {
			await arrive({ fromChat: true, chatConv: "c-1", back: "/c/c-1" });
			const row = wrapper.get(".pcal-title-row");
			expect(row.find(".pcal-back").exists()).toBe(false);
			const html = wrapper.html();
			expect(html.indexOf("pcal-back")).toBeLessThan(html.indexOf("pcal-title-row"));
			expect(row.text()).toContain("Refresh");
		});
		it("is there on the FIRST paint, not one tick after mount", async () => {
			window.history.replaceState({ fromChat: true, chatConv: "c-1", back: "/c/c-1" }, "");
			wrapper = mount(PaymentCalendarPage);
			// no await: the state is read in setup, so the first render already has the button
			expect(wrapper.find(".pcal-back").exists()).toBe(true);
			await flushPromises();
		});
		it("takes only a string conversation id from the history state", async () => {
			for (const chatConv of [123, true, {}, ["c-1"], null, ""]) {
				await arrive({ fromChat: true, chatConv, back: "/c/c-1" });
				expect(backBtn().exists()).toBe(false);
				wrapper.unmount();
			}
		});
		it("goes back in history when the previous entry is that conversation", async () => {
			await arrive({ fromChat: true, chatConv: "c-1", back: "/c/c-1" });
			await backBtn().trigger("click");
			expect(router.back).toHaveBeenCalledTimes(1);
			expect(router.push).not.toHaveBeenCalled();
		});
		it("pushes the conversation route when the previous entry is something else", async () => {
			await arrive({ fromChat: true, chatConv: "c-1", back: "/dashboards" });
			await backBtn().trigger("click");
			expect(router.back).not.toHaveBeenCalled();
			expect(router.push).toHaveBeenCalledWith({
				name: "Conversation",
				params: { id: "c-1" },
			});
		});
		it("keeps the key after the one-shot clear, with chatConv intact", async () => {
			await arrive({ fromChat: true, chatConv: "c-1", back: "/c/c-1" });
			expect("fromChat" in window.history.state).toBe(true);
			expect(window.history.state.fromChat).toBeUndefined();
			expect(window.history.state.chatConv).toBe("c-1");
		});
		it("still shows after a remount (reload) once fromChat was cleared", async () => {
			await arrive({ fromChat: undefined, chatConv: "c-1", back: "/c/c-1" });
			expect(backBtn().exists()).toBe(true);
		});
		it("is absent without state, or without a conversation", async () => {
			await arrive({});
			expect(backBtn().exists()).toBe(false);
			wrapper.unmount();
			await arrive({ fromChat: true, back: "/c/c-1" });
			expect(backBtn().exists()).toBe(false);
		});
	});
	it("removes financial panels at expiry and offers explicit recovery", async () => {
		await render();
		expect(wrapper.find('[data-kpi="cash"]').exists()).toBe(true);
		await vi.advanceTimersByTimeAsync(300001);
		expect(wrapper.find('[data-kpi="cash"]').exists()).toBe(false);
		expect(wrapper.text()).toContain("Snapshot expired");
		expect(wrapper.text()).toContain("Refresh calendar");
	});
	it("keeps the entered floor after a concurrent update refusal", async () => {
		await render();
		await wrapper.get(".minimum-field button").trigger("click");
		await wrapper.get('[role="dialog"] input').setValue("44.00");
		api.configureFloor.mockResolvedValueOnce(refusal("settings_changed", "scope_changed"));
		await wrapper.get('[role="dialog"] .pcal-primary').trigger("click");
		await flushPromises();
		expect(wrapper.get('[role="dialog"] input').element.value).toBe("44.00");
		expect(wrapper.get('[role="dialog"]').text()).toContain("changed by another user");
	});
	it("hides the floor mutation action for an accounts reader", async () => {
		const normal = await api.getCapabilities("Manufacturing");
		api.getCapabilities
			.mockResolvedValueOnce(normal)
			.mockResolvedValueOnce(ok({ ...normal.data, can_configure: false }));
		await render();
		expect(wrapper.find(".minimum-field button").exists()).toBe(false);
	});
	it("clears floor and account controls when company access is revoked", async () => {
		await render();
		api.getDayDetail.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		await wrapper.get('[data-date="2026-10-25"]').trigger("click");
		await flushPromises();
		expect(wrapper.find(".minimum-field button").exists()).toBe(false);
		expect(wrapper.get(".minimum-value strong").text()).toBe("—");
		expect(wrapper.get(".pcal-select-button").attributes("disabled")).toBeDefined();
	});
	it("keeps the calendar on screen with a refreshing status during automatic replacement", async () => {
		await render();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		await vi.advanceTimersByTimeAsync(270000);
		expect(wrapper.find('[data-kpi="cash"]').exists()).toBe(true);
		expect(wrapper.text()).toContain("Refreshing data…");
		expect(wrapper.get(".pcal-title-row button").attributes("disabled")).toBeDefined();
		const fresh = complete();
		fresh.expires_at = new Date(Date.now() + 300000).toISOString();
		late.resolve(ok(fresh));
		await flushPromises();
		expect(wrapper.text()).toContain("Updates automatically while this page is visible");
		expect(wrapper.text()).not.toContain("Snapshot expired");
	});
	it("keeps native labelled checkbox selection and cancel/apply behavior", async () => {
		await render();
		await wrapper.get(".pcal-select-button").trigger("click");
		const box = wrapper.get('.account-choices input[type="checkbox"]');
		expect(box.element.checked).toBe(true);
		expect(box.element.closest("label").textContent).toContain("Operating bank");
		await box.setValue(false);
		await wrapper.get(".pcal-dialog-actions button").trigger("click");
		expect(api.setPreferences).not.toHaveBeenCalled();
		await wrapper.get(".pcal-select-button").trigger("click");
		expect(wrapper.get(".account-choices input").element.checked).toBe(true);
		await wrapper.get(".account-choices input").setValue(false);
		await wrapper.get(".pcal-dialog-actions .pcal-primary").trigger("click");
		await flushPromises();
		expect(api.setPreferences).toHaveBeenLastCalledWith("Manufacturing", []);
	});
	it("keeps the calendar mounted through worker contention instead of showing a full-page error", async () => {
		await render();
		const grid = wrapper.get(".calendar-grid").element;
		api.requestSnapshot.mockResolvedValueOnce(refusal("worker_busy"));
		await vi.advanceTimersByTimeAsync(270000);
		expect(wrapper.get(".calendar-grid").element).toBe(grid);
		expect(wrapper.text()).not.toContain("Calendar unavailable");
		expect(wrapper.text()).toContain("Waiting to load");
	});
	it("keeps an empty calendar frame while expired balances await replacement", async () => {
		await render();
		const grid = wrapper.get(".calendar-grid").element;
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		await vi.advanceTimersByTimeAsync(300001);
		expect(wrapper.get(".calendar-grid").element).toBe(grid);
		expect(wrapper.get('[data-kpi="cash"] .kpi-value').text()).toBe("—");
		expect(wrapper.text()).not.toContain("Preparing your calendar");
		expect(wrapper.text()).toContain("Loading current balances");
	});
	it("waits automatically when workers are busy on first open", async () => {
		api.requestSnapshot.mockResolvedValueOnce(refusal("worker_busy"));
		await render();
		expect(wrapper.text()).not.toContain("Calendar unavailable");
		expect(wrapper.text()).toContain("This page will update automatically.");
		await vi.advanceTimersByTimeAsync(1000);
		expect(wrapper.find(".calendar-grid").exists()).toBe(true);
	});
});
