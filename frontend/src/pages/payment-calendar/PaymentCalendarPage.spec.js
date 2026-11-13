import { vi, afterEach } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { mount, flushPromises } from "@vue/test-utils";
vi.mock("@/api/paymentCalendar", async () =>
	(await import("../../../tests/payment-calendar/fixtures")).fakeApi()
);
vi.mock("@/utils/datetime", () => ({ siteTimezone: () => "Asia/Kolkata" }));
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ mobile: false }) }));
const theme = vi.hoisted(() => ({ dark: null, toggleTheme: vi.fn() }));
vi.mock("@/theme", async () => {
	const { ref } = await import("vue");
	theme.dark = ref(false);
	return {
		useJarvisTheme: () => ({ effectiveDark: theme.dark, toggleTheme: theme.toggleTheme }),
	};
});
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
import {
	ok,
	refusal,
	complete,
	deferred,
	installMonthlyApi,
	settleReads,
} from "../../../tests/payment-calendar/fixtures";
import { modernStatus } from "../../../tests/payment-calendar/headlineFixtures";

describe("Native Payment Calendar page", () => {
	let wrapper;
	beforeEach(() => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date("2026-10-24T12:00:00Z"));
		vi.clearAllMocks();
		theme.dark.value = false;
	});
	afterEach(() => {
		wrapper?.unmount();
		vi.useRealTimers();
	});
	async function render() {
		wrapper = mount(PaymentCalendarPage);
		await settleReads();
	}
	it("connects a native date selection to one snapshot and one ledger read per click", async () => {
		await render();
		expect(wrapper.text()).toContain("Payment Calendar");
		const reads = api.getDayDetail.mock.calls.length;
		await wrapper.get('[data-date="2026-10-25"]').trigger("click");
		await settleReads();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(1);
		expect(api.getDayDetail).toHaveBeenCalledTimes(reads + 1);
		expect(api.getDayDetail.mock.calls.at(-1)[1]).toBe("2026-10-25");
		await wrapper.get('[data-panel-side="Receivable"]').trigger("click");
		await settleReads();
		expect(api.getDayDetail).toHaveBeenCalledTimes(reads + 2);
		expect(api.getDayDetail.mock.calls.at(-1)[2]).toBe("Receivable");
	});
	it("moves focus to the title on arrival from the chat hand-off, and only then", async () => {
		const host = document.createElement("div");
		document.body.appendChild(host);
		try {
			window.history.replaceState({}, "");
			wrapper = mount(PaymentCalendarPage, { attachTo: host });
			await settleReads();
			const title = wrapper.get("#pcal-title");
			expect(title.attributes("tabindex")).toBe("-1");
			expect(document.activeElement).not.toBe(title.element);
			wrapper.unmount();
			window.history.replaceState({ fromChat: true, chatConv: "c-1" }, "");
			wrapper = mount(PaymentCalendarPage, { attachTo: host });
			await settleReads();
			expect(document.activeElement).toBe(wrapper.get("#pcal-title").element);
			// one-shot: the flag is cleared, so a remount (reload / Forward) does not re-focus
			expect(window.history.state?.fromChat).toBeUndefined();
			wrapper.unmount();
			(document.activeElement || document.body).blur?.();
			wrapper = mount(PaymentCalendarPage, { attachTo: host });
			await settleReads();
			expect(document.activeElement).not.toBe(wrapper.get("#pcal-title").element);
		} finally {
			window.history.replaceState({}, "");
			host.remove();
		}
	});
	it("never draws a ring on the title: it is not interactive", () => {
		const css = fs.readFileSync(path.resolve(__dirname, "PaymentCalendarPage.css"), "utf8");
		expect(css).toMatch(/#pcal-title:focus\s*\{\s*outline:\s*none;\s*\}/);
		expect(css).not.toMatch(/#pcal-title:focus:not\(:focus-visible\)/);
	});
	it("Back to chat uses the page's own focus ring, with no rule of its own", async () => {
		window.history.replaceState({ fromChat: true, chatConv: "c-1", back: "/c/c-1" }, "");
		wrapper = mount(PaymentCalendarPage);
		await settleReads();
		window.history.replaceState({}, "");
		const back = wrapper.find(".pcal-back");
		// a plain button of the page, not a frappe-ui Button with a ring of its own
		expect(back.element.tagName).toBe("BUTTON");
		expect(back.attributes("type")).toBe("button");
		expect(back.classes()).toContain("pcal-text-button");
		expect(back.element.closest(".pcal-root")).not.toBeNull();
		const tokens = fs.readFileSync(path.resolve(__dirname, "tokens.css"), "utf8");
		const css = fs.readFileSync(path.resolve(__dirname, "PaymentCalendarPage.css"), "utf8");
		// the template ring lives in tokens.css; the page sheet must not draw a second one
		expect(tokens).toMatch(/\.pcal-root :is\(button[^)]*\):focus-visible/);
		expect(css).not.toMatch(/:focus-visible/);
	});
	describe("Back to chat", () => {
		const backBtn = () => wrapper.find(".pcal-back");
		async function arrive(state) {
			window.history.replaceState(state, "");
			wrapper = mount(PaymentCalendarPage);
			await settleReads();
		}
		afterEach(() => window.history.replaceState({}, ""));
		it("is shown above the heading when the hand-off carried a conversation", async () => {
			await arrive({ fromChat: true, chatConv: "c-1", back: "/c/c-1" });
			expect(backBtn().exists()).toBe(true);
			expect(backBtn().text()).toBe("Back to chat");
			const html = wrapper.html();
			expect(html.indexOf("pcal-back")).toBeLessThan(html.indexOf("pcal-page-heading"));
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
		it("sits above the heading row, so the Refresh button does not move", async () => {
			await arrive({ fromChat: true, chatConv: "c-1", back: "/c/c-1" });
			const row = wrapper.get(".pcal-page-heading");
			expect(row.find(".pcal-back").exists()).toBe(false);
			const html = wrapper.html();
			expect(html.indexOf("pcal-back")).toBeLessThan(html.indexOf("pcal-page-heading"));
			expect(row.text()).toContain("Refresh");
		});
		it("is there on the FIRST paint, not one tick after mount", async () => {
			window.history.replaceState({ fromChat: true, chatConv: "c-1", back: "/c/c-1" }, "");
			wrapper = mount(PaymentCalendarPage);
			// no await: the state is read in setup, so the first render already has the button
			expect(wrapper.find(".pcal-back").exists()).toBe(true);
			await settleReads();
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
	describe("Shell and header", () => {
		const css = () =>
			fs.readFileSync(path.resolve(__dirname, "PaymentCalendarPage.css"), "utf8");

		it("shows the title with no intro text, then theme switch, As of time and Refresh", async () => {
			await render();
			expect(wrapper.get("h1").text()).toBe("Payment Calendar");
			expect(wrapper.text()).not.toContain("CASH PLANNING");
			expect(wrapper.text()).not.toContain("Know what is due");
			const tools = wrapper.get(".pcal-page-heading .pcal-heading-tools");
			const order = [...tools.element.children].map(
				(node) => node.id || node.className.split(" ")[0]
			);
			expect(order).toEqual(["theme-toggle", "pcal-updated", "refresh-button"]);
			expect(tools.get(".pcal-updated").text()).toMatch(/^As of /);
			expect(tools.get("#refresh-button").text()).toBe("Refresh");
		});

		it("labels Refresh by its text and shows the refreshing copy while it runs", async () => {
			await render();
			expect(wrapper.get("#refresh-button").attributes("disabled")).toBeUndefined();
			const late = deferred();
			api.getSnapshotStatus.mockReturnValueOnce(late.promise);
			await vi.advanceTimersByTimeAsync(270000);
			expect(wrapper.get("#refresh-button").text()).toBe("Refreshing…");
			late.resolve(ok(complete()));
			await settleReads();
		});

		it("starts a background refresh from Refresh", async () => {
			await render();
			const before = api.requestSnapshot.mock.calls.length;
			await wrapper.get("#refresh-button").trigger("click");
			await settleReads();
			expect(api.requestSnapshot.mock.calls.length).toBe(before + 1);
		});

		it("shows the light-mode switch from effectiveDark", async () => {
			await render();
			const toggle = wrapper.get("#theme-toggle");
			expect(toggle.attributes("type")).toBe("button");
			// One fixed name: aria-pressed carries the state, so the label never flips.
			expect(toggle.attributes("aria-label")).toBe("Dark mode");
			expect(toggle.attributes("title")).toBe("Dark mode");
			expect(toggle.attributes("aria-pressed")).toBe("false");
			expect(toggle.get("svg").attributes("data-icon")).toBe("moon");
		});

		it("shows the dark-mode switch and follows a theme change made elsewhere", async () => {
			theme.dark.value = true;
			await render();
			const toggle = wrapper.get("#theme-toggle");
			expect(toggle.attributes("aria-label")).toBe("Dark mode");
			expect(toggle.attributes("title")).toBe("Dark mode");
			expect(toggle.attributes("aria-pressed")).toBe("true");
			expect(toggle.get("svg").attributes("data-icon")).toBe("sun");
			theme.dark.value = false;
			await settleReads();
			expect(toggle.attributes("aria-pressed")).toBe("false");
			expect(toggle.get("svg").attributes("data-icon")).toBe("moon");
		});

		it("flips the Jarvis theme through toggleTheme and writes nothing else", async () => {
			await render();
			await wrapper.get("#theme-toggle").trigger("click");
			expect(theme.toggleTheme).toHaveBeenCalledTimes(1);
			expect(wrapper.get("#theme-toggle").attributes("aria-pressed")).toBe("false");
			expect(api.setPreferences).not.toHaveBeenCalled();
		});

		it("wraps the content in an unpadded container-query layout inside the padded page", async () => {
			await render();
			const layout = wrapper.get(".pcal-main > .pcal-layout");
			expect(layout.find(".pcal-page-heading").exists()).toBe(true);
			expect(layout.find(".pcal-cal").exists()).toBe(true);
			const rule = css().match(/\.pcal-layout\s*\{([^}]*)\}/)[1];
			expect(rule).toMatch(/container-type:\s*inline-size/);
			expect(rule).not.toMatch(/padding/);
			expect(css()).toMatch(/\.pcal-main\s*\{[^}]*padding:/);
		});

		it("hides the As of time at a 540 px container, not at a 540 px viewport", () => {
			expect(css()).toMatch(
				/@container\s*\(max-width:\s*540px\)\s*\{[^@]*\.pcal-as-of[^{]*\{\s*display:\s*none/
			);
		});

		it("keeps the update status visible on a phone: only the time and the dot go", () => {
			const phone = css().match(
				/@container\s*\(max-width:\s*540px\)\s*\{([\s\S]*?)\n\}\n/
			)[1];
			expect(phone).not.toMatch(/\.pcal-updated\s*\{[^}]*display:\s*none/);
			expect(phone).toMatch(/\.pcal-updated\s*\{[^}]*flex-basis:\s*100%/);
		});

		it("marks the As of time, so the status beside it stays when the time is hidden", async () => {
			await render();
			expect(wrapper.get(".pcal-updated .pcal-as-of").text()).toMatch(/^As of /);
		});

		it("owns exactly one document keydown listener and removes it on unmount", async () => {
			const add = vi.spyOn(document, "addEventListener");
			const remove = vi.spyOn(document, "removeEventListener");
			await render();
			const keydown = (spy) => spy.mock.calls.filter(([type]) => type === "keydown");
			expect(keydown(add)).toHaveLength(1);
			wrapper.unmount();
			wrapper = null;
			expect(keydown(remove)).toHaveLength(1);
			add.mockRestore();
			remove.mockRestore();
		});
	});
	it("removes financial panels at expiry and offers explicit recovery", async () => {
		await render();
		expect(wrapper.find("#lowest-card").exists()).toBe(true);
		await vi.advanceTimersByTimeAsync(300001);
		expect(wrapper.find("#lowest-card").exists()).toBe(false);
		expect(wrapper.text()).toContain("Calendar data expired");
		expect(wrapper.text()).toContain("Refresh calendar");
	});
	describe("at the automatic recovery cap", () => {
		const LIMIT_COPY = "The calendar keeps changing. Refresh to try again.";
		// These tests replace api behaviour for good; give it back to the later tests.
		let original;
		beforeEach(() => {
			original = Object.fromEntries(
				Object.entries(api).map(([name, fn]) => [name, fn.getMockImplementation()])
			);
		});
		afterEach(() => {
			Object.entries(original).forEach(([name, impl]) =>
				api[name].mockReset().mockImplementation(impl)
			);
		});
		const refreshButton = () =>
			wrapper.findAll("button").filter((button) => button.text() === "Refresh calendar");
		const expectManualRefresh = () => {
			expect(wrapper.text()).toContain(LIMIT_COPY);
			expect(wrapper.text()).toContain("Calendar keeps changing");
			expect(wrapper.text()).not.toContain("Access or company settings changed");
			expect(refreshButton()).toHaveLength(1);
			expect(wrapper.find("#lowest-card").exists()).toBe(false);
		};
		it("shows the copy and the Refresh action when the access recovery ends failed", async () => {
			await render();
			api.getDayDetail.mockResolvedValue(refusal("scope_changed"));
			await wrapper.get('[data-date="2026-10-25"]').trigger("click");
			await settleReads();
			await settleReads();
			await settleReads();
			expectManualRefresh();
			const requests = api.requestSnapshot.mock.calls.length;
			api.getDayDetail.mockResolvedValue(ok({ rows: [], total: 0 }));
			await refreshButton()[0].trigger("click");
			await settleReads();
			expect(api.requestSnapshot).toHaveBeenCalledTimes(requests + 1);
		});
		it("shows the copy and the Refresh action when the month recovery ends expired", async () => {
			installMonthlyApi(api);
			await render();
			api.getDayDetail.mockResolvedValueOnce(refusal("scope_changed"));
			await wrapper.get('[data-date="2026-10-25"]').trigger("click");
			await settleReads();
			api.getDayDetail.mockResolvedValueOnce(refusal("scope_changed"));
			await wrapper.get('[data-date="2026-10-26"]').trigger("click");
			await settleReads();
			const late = deferred();
			api.getSnapshotStatus.mockReturnValueOnce(late.promise);
			await wrapper.get('[aria-label="Next month"]').trigger("click");
			await flushPromises();
			await wrapper.get('[aria-label="Next month"]').trigger("click");
			late.resolve(refusal("not_found_or_expired", "expired"));
			await settleReads();
			expectManualRefresh();
		});
	});
	it("keeps the entered floor after a concurrent update refusal", async () => {
		await render();
		await wrapper.get("#floor-input").setValue("44.00");
		api.configureFloor.mockResolvedValueOnce(refusal("settings_changed", "scope_changed"));
		await wrapper.get("#floor-save").trigger("click");
		await settleReads();
		expect(wrapper.get("#floor-input").element.value).toBe("44.00");
		expect(wrapper.get("#floor-error").text()).toContain("changed by another user");
	});
	it("hides the floor mutation action for an accounts reader", async () => {
		const normal = await api.getCapabilities("Manufacturing");
		api.getCapabilities
			.mockResolvedValueOnce(normal)
			.mockResolvedValueOnce(ok({ ...normal.data, can_configure: false }));
		await render();
		expect(wrapper.find("#floor-save").exists()).toBe(false);
		expect(wrapper.get("#floor-input").attributes("readonly")).toBeDefined();
	});
	it("clears floor and account controls when company access is revoked", async () => {
		await render();
		api.getDayDetail.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		await wrapper.get('[data-date="2026-10-25"]').trigger("click");
		await settleReads();
		expect(wrapper.find("#floor-save").exists()).toBe(false);
		expect(wrapper.get("#floor-input").element.value).toBe("");
		expect(wrapper.get("#floor-input").attributes("disabled")).toBeDefined();
		expect(wrapper.get("#accounts-trigger").attributes("disabled")).toBeDefined();
	});
	it("keeps the calendar on screen with a refreshing status during automatic replacement", async () => {
		await render();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		await vi.advanceTimersByTimeAsync(270000);
		expect(wrapper.find("#lowest-card").exists()).toBe(true);
		expect(wrapper.text()).toContain("Refreshing data…");
		expect(wrapper.get("#refresh-button").attributes("disabled")).toBeDefined();
		const fresh = complete();
		fresh.expires_at = new Date(Date.now() + 300000).toISOString();
		late.resolve(ok(fresh));
		await settleReads();
		expect(wrapper.text()).not.toContain("Refreshing data…");
		expect(wrapper.text()).not.toContain("Calendar data expired");
	});
	it("shows no old-page copy that the template does not have", async () => {
		await render();
		for (const copy of [
			"Updates automatically while this page is visible",
			"Customer collections assume on-time payment",
			"Your payment schedule",
			"Click any date to update",
		])
			expect(wrapper.text()).not.toContain(copy);
		expect(wrapper.find(".pcal-asof").exists()).toBe(false);
	});
	it("shows the update status inside the header time, in its status role", async () => {
		await render();
		expect(wrapper.get(".pcal-updated").text()).not.toContain("Refreshing data…");
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		await vi.advanceTimersByTimeAsync(270000);
		expect(wrapper.get(".pcal-updated").text()).toMatch(/^As of .*Refreshing data…$/);
		expect(wrapper.get(".pcal-updated [role=status]").text()).toBe("Refreshing data…");
		late.resolve(ok(complete()));
		await settleReads();
	});
	it("does not own a hover-preview state or pass one to the calendar", async () => {
		await render();
		expect(wrapper.find("#quick-preview").exists()).toBe(false);
		const vm = wrapper.findComponent({ name: "CalendarGrid" });
		expect(vm.props()).not.toHaveProperty("hoverEnabled");
	});
	it("keeps native labelled checkbox selection and cancel/apply behavior", async () => {
		await render();
		await wrapper.get("#accounts-trigger").trigger("click");
		const box = wrapper.get('#accounts-options input[type="checkbox"]');
		expect(box.element.checked).toBe(true);
		expect(box.element.closest("label").textContent).toContain("Operating bank");
		await box.setValue(false);
		await wrapper.get("#accounts-cancel").trigger("click");
		expect(api.setPreferences).not.toHaveBeenCalled();
		await wrapper.get("#accounts-trigger").trigger("click");
		expect(wrapper.get("#accounts-options input").element.checked).toBe(true);
		await wrapper.get("#accounts-options input").setValue(false);
		await wrapper.get("#accounts-apply").trigger("click");
		await settleReads();
		expect(api.setPreferences).toHaveBeenLastCalledWith("Manufacturing", []);
	});
	it("keeps the calendar mounted through worker contention instead of showing a full-page error", async () => {
		await render();
		const grid = wrapper.get(".pcal-cal").element;
		api.requestSnapshot.mockResolvedValueOnce(refusal("worker_busy"));
		await vi.advanceTimersByTimeAsync(270000);
		expect(wrapper.get(".pcal-cal").element).toBe(grid);
		expect(wrapper.text()).not.toContain("Calendar unavailable");
		expect(wrapper.text()).toContain("Waiting to load");
	});
	it("keeps an empty calendar frame while expired balances await replacement", async () => {
		await render();
		const grid = wrapper.get(".pcal-cal").element;
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		await vi.advanceTimersByTimeAsync(300001);
		expect(wrapper.get(".pcal-cal").element).toBe(grid);
		expect(wrapper.get("#lowest-card").attributes("aria-busy")).toBe("true");
		expect(wrapper.get("#lowest-card").text()).toContain("Loading values…");
		expect(wrapper.text()).not.toContain("Preparing your calendar");
		expect(wrapper.text()).toContain("Loading current balances");
	});
	it("waits automatically when workers are busy on first open", async () => {
		api.requestSnapshot.mockResolvedValueOnce(refusal("worker_busy"));
		await render();
		expect(wrapper.text()).not.toContain("Calendar unavailable");
		expect(wrapper.text()).toContain("This page will update automatically.");
		await vi.advanceTimersByTimeAsync(1000);
		expect(wrapper.find(".pcal-cal").exists()).toBe(true);
	});
	describe("Headlines, filter bar and book cash row", () => {
		const position = (selector) => wrapper.html().indexOf(selector.slice(1));
		let restoreStatus;
		beforeEach(() => {
			const original = api.getSnapshotStatus.getMockImplementation();
			restoreStatus = () => api.getSnapshotStatus.mockImplementation(original);
		});
		afterEach(() => restoreStatus());
		// The template's Nov 2026 dataset served for the company and accounts the fake API selects.
		function serveTemplateSnapshot(mutate = () => {}) {
			vi.setSystemTime(new Date("2026-11-11T05:12:30Z"));
			const status = modernStatus();
			status.scope.company = "Manufacturing";
			status.scope.accounts = ["Bank A"];
			status.expires_at = "2026-11-11T05:17:00+00:00";
			mutate(status);
			api.getSnapshotStatus.mockImplementation(async () => ok(status));
		}

		it("mounts headlines, then the filter bar, then the book cash row", async () => {
			await render();
			const order = ["pcal-title", "lowest-card", "company-trigger", "header-book-cash"].map(
				(id) => position("#" + id)
			);
			expect(order.every((at) => at > 0)).toBe(true);
			expect([...order].sort((a, b) => a - b)).toEqual(order);
			expect(wrapper.find(".pcal-toolbar").exists()).toBe(false);
			expect(wrapper.find('[data-kpi="cash"]').exists()).toBe(false);
		});

		it("writes the header time in the template format", async () => {
			await render();
			expect(wrapper.get(".pcal-updated").text()).toBe("As of 24 Oct, 17:30 IST");
			expect(wrapper.get(".context-time").text()).toBe("Snapshot · 24 Oct, 17:30 IST");
			expect(wrapper.get("#header-book-cash").text()).toBe("₹100.00");
		});

		it("shows Unavailable, never ₹0, for an old-shape result", async () => {
			await render();
			for (const id of ["#lowest-cushion", "#month-flow", "#immediate-dues"])
				expect(wrapper.get(id).text()).toBe("Unavailable");
			expect(wrapper.get("#trough-jump").attributes("disabled")).toBeDefined();
		});

		it("shows the template's values from the server fields", async () => {
			serveTemplateSnapshot();
			await render();
			expect(wrapper.get("#lowest-cushion").text()).toBe("₹97,614.00");
			expect(wrapper.get("#lowest-note").text()).toBe("Lowest dip on 23 Nov");
			expect(wrapper.get("#trough-jump").text()).toBe("View 23 Nov →");
			expect(wrapper.get("#lowest-card").attributes("data-state")).toBe("green");
			expect(wrapper.get("#month-flow").text()).toBe("+₹3,29,702.00");
			expect(wrapper.get("#month-flow-scope").text()).toBe("November 2026");
			expect(wrapper.get("#immediate-dues").text()).toBe("₹27,500.00");
			expect(wrapper.get("#immediate-status").text()).toBe("Needs review");
			expect(wrapper.get("#header-book-cash").text()).toBe("₹2,46,850.00");
			expect(wrapper.get(".pcal-updated").text()).toBe("As of 11 Nov, 10:42 IST");
		});

		it("selects the trough date from View 23 Nov", async () => {
			serveTemplateSnapshot();
			await render();
			await wrapper.get("#trough-jump").trigger("click");
			await settleReads();
			expect(api.getDayDetail.mock.calls.at(-1)[1]).toBe("2026-11-23");
		});

		it("lands the calendar on the trough date: Payable side, marked as the jump target", async () => {
			serveTemplateSnapshot();
			await render();
			await wrapper.get("#trough-jump").trigger("click");
			await settleReads();
			expect(api.getDayDetail.mock.calls.at(-1)[2]).toBe("Payable");
			const targets = wrapper.findAll(".trough-target");
			expect(targets).toHaveLength(1);
			expect(targets[0].attributes("data-date")).toBe("2026-11-23");
			expect(targets[0].attributes("aria-pressed")).toBe("true");
		});

		it("opens the month's invoices in the details modal from View totals", async () => {
			serveTemplateSnapshot();
			await render();
			await wrapper.get("#month-flow-details").trigger("click");
			await settleReads();
			expect(document.body.querySelector(".pcal-modal")).not.toBeNull();
			expect(api.getBucketDetail.mock.calls.at(-1).slice(1, 3)).toEqual([
				"window",
				"Payable",
			]);
		});

		it("shows the immediate invoices in the side panel and focuses its heading", async () => {
			serveTemplateSnapshot();
			wrapper = mount(PaymentCalendarPage, { attachTo: document.body });
			await settleReads();
			await wrapper.get('[data-obligation="immediate"]').trigger("click");
			await settleReads();
			expect(api.getBucketDetail.mock.calls.at(-1).slice(1, 3)).toEqual([
				"immediate",
				"Payable",
			]);
			expect(document.activeElement).toBe(wrapper.get("#ledger-heading").element);
		});

		it("removes an account at once from its chip", async () => {
			await render();
			expect(wrapper.get("#account-chips").text()).toContain("Operating bank");
			await wrapper.get("#account-chips .account-chip").trigger("click");
			await settleReads();
			expect(api.setPreferences).toHaveBeenLastCalledWith("Manufacturing", []);
			expect(api.requestSnapshot).toHaveBeenLastCalledWith(
				"Manufacturing",
				[],
				expect.anything()
			);
			expect(wrapper.find("#accounts-menu").exists()).toBe(false);
		});

		it("switches company from the menu", async () => {
			await render();
			await wrapper.get("#company-trigger").trigger("click");
			const options = wrapper.findAll('#company-options [role="option"]');
			expect(options.map((option) => option.text().replace("✓", ""))).toEqual([
				"Manufacturing",
				"Services",
			]);
			await options[1].trigger("click");
			await settleReads();
			expect(api.getCapabilities).toHaveBeenLastCalledWith("Services");
			expect(api.requestSnapshot.mock.calls.at(-1)[0]).toBe("Services");
		});

		it("saves the minimum through the core with the comma-free amount", async () => {
			await render();
			expect(wrapper.get("#floor-input").element.value).toBe("20");
			await wrapper.get("#floor-input").setValue("2,00,000");
			expect(wrapper.get("#floor-save").attributes("hidden")).toBeUndefined();
			await wrapper.get("#floor-save").trigger("click");
			await settleReads();
			expect(api.configureFloor).toHaveBeenCalledWith("Manufacturing", "200000", 1, "floor");
		});

		it("does not touch the server for an invalid or unsaved minimum", async () => {
			await render();
			await wrapper.get("#floor-input").setValue("10.123");
			await wrapper.get("#floor-save").trigger("click");
			expect(api.configureFloor).not.toHaveBeenCalled();
			expect(wrapper.get("#floor-error").text()).toContain("up to two decimal places");
		});

		it("opens the balances dialog from the book cash row and runs the all-accounts job", async () => {
			await render();
			await wrapper.get("#header-book-cash").trigger("click");
			await settleReads();
			const modal = document.body.querySelector(".pcal-modal");
			expect(modal.querySelector("h2").textContent).toBe("Bank & cash account balances");
			expect(api.requestAccountBalances).not.toHaveBeenCalled();
			document.body.querySelector("#account-balance-toggle").click();
			await settleReads();
			expect(api.requestAccountBalances).toHaveBeenCalledWith("Manufacturing");
		});

		it("closes the balances dialog when the company changes", async () => {
			await render();
			await wrapper.get("#account-balances").trigger("click");
			await settleReads();
			expect(document.body.querySelector(".pcal-modal")).not.toBeNull();
			await wrapper.get("#company-trigger").trigger("click");
			await wrapper.findAll('#company-options [role="option"]')[1].trigger("click");
			await settleReads();
			expect(document.body.querySelector(".pcal-modal")).toBeNull();
		});
	});
});
