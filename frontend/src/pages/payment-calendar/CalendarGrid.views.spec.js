import { afterEach, vi } from "vitest";
import {
	mountGrid,
	novemberSnapshot,
	resizeLayout,
	useFakeResizeObserver,
} from "../../../tests/payment-calendar/gridHarness";

describe("Payment Calendar list, week and legend", () => {
	let wrapper;
	useFakeResizeObserver();
	afterEach(() => wrapper?.unmount());
	function render(options = {}) {
		wrapper = mountGrid(options);
		return wrapper;
	}
	const list = (options = {}) =>
		render({ ...options, props: { mode: "list", ...options.props } });
	const row = (date) => wrapper.get(`[data-list-date="${date}"]`);

	describe("list view", () => {
		it("has one row per date of the month and no month grid", () => {
			list();
			expect(wrapper.find('[role="grid"]').exists()).toBe(false);
			expect(wrapper.findAll("[data-list-date]")).toHaveLength(30);
			expect(wrapper.findAll("[data-list-date]")[0].attributes("data-list-date")).toBe(
				"2026-11-01"
			);
		});
		it("puts a week label before day 1 and before every Monday", () => {
			list();
			const labels = wrapper.findAll(".list-week-label").map((l) => l.text());
			expect(labels).toEqual([
				"26 Oct – 1 Nov",
				"2 Nov – 8 Nov",
				"9 Nov – 15 Nov",
				"16 Nov – 22 Nov",
				"23 Nov – 29 Nov",
				"30 Nov – 6 Dec",
			]);
			const kids = [...wrapper.get(".list-view").element.children];
			expect(kids[0].className).toContain("list-week-label");
			expect(kids[2].className).toContain("list-week-label");
		});
		it("shows the date with a Today line from the anchor and the status", () => {
			list();
			expect(row("2026-11-11").get(".date-row-date").text()).toBe("11 NovToday · Partial");
			expect(row("2026-11-12").get(".date-row-date").text()).toBe("12 NovUnpaid");
		});
		it("says Data unavailable for a date with no data", () => {
			const snapshot = novemberSnapshot();
			delete snapshot.projection.days["2026-11-03"];
			list({ props: { snapshot } });
			expect(row("2026-11-03").get(".date-row-date").text()).toContain("Data unavailable");
			expect(row("2026-11-03").get(".list-net").text()).toBe("—");
		});
		it("shows Pay and Receive buttons that select the date and the side", async () => {
			list();
			const [pay, receive] = row("2026-11-11").findAll(".amount-line");
			expect(pay.text()).toBe("Pay₹42,048");
			expect(receive.text()).toBe("Receive₹86,250");
			await pay.trigger("click");
			await receive.trigger("click");
			expect(wrapper.emitted("select")).toEqual([
				["2026-11-11", "Payable"],
				["2026-11-11", "Receivable"],
			]);
		});
		it("selects the date from the date button", async () => {
			list();
			await row("2026-11-12").get(".date-row-date").trigger("click");
			expect(wrapper.emitted("select")).toEqual([["2026-11-12"]]);
		});
		it("shows the server net and colours a net outflow red", () => {
			list();
			expect(row("2026-11-11").get(".list-net").text()).toBe("+₹44.2k");
			expect(row("2026-11-11").get(".list-net").classes()).not.toContain("negative");
			expect(row("2026-11-23").get(".list-net").text()).toBe("−₹19.4k");
			expect(row("2026-11-23").get(".list-net").classes()).toContain("negative");
		});
		it("marks the selected row and the date button pressed", () => {
			list();
			expect(row("2026-11-11").classes()).toContain("selected");
			expect(row("2026-11-12").classes()).not.toContain("selected");
			expect(wrapper.get('[data-date="2026-11-11"]').attributes("aria-current")).toBe(
				"date"
			);
		});
		it("gives the list date button no aria-label, so its text is its name", () => {
			list();
			expect(
				wrapper.get('[data-date="2026-11-19"]').attributes("aria-label")
			).toBeUndefined();
		});
		it.each([
			["ArrowDown", "2026-11-12"],
			["ArrowUp", "2026-11-10"],
			["ArrowRight", "2026-11-12"],
			["ArrowLeft", "2026-11-10"],
		])("moves one row with %s", async (key, target) => {
			list();
			const date = wrapper.get('[data-date="2026-11-11"]');
			date.element.focus();
			await date.trigger("keydown", { key });
			expect(document.activeElement.dataset.date).toBe(target);
			expect(wrapper.emitted("select")).toBeUndefined();
		});
		it("scrolls the selected row to the centre of the list", async () => {
			const offsetTop = vi
				.spyOn(HTMLElement.prototype, "offsetTop", "get")
				.mockImplementation(function () {
					return this.classList.contains("selected") ? 400 : 0;
				});
			const clientHeight = vi
				.spyOn(HTMLElement.prototype, "clientHeight", "get")
				.mockImplementation(function () {
					return this.classList.contains("list-view") ? 420 : 40;
				});
			try {
				list();
				await wrapper.vm.$nextTick();
				expect(wrapper.get(".list-view").element.scrollTop).toBe(400 - (420 - 40) / 2);
			} finally {
				offsetTop.mockRestore();
				clientHeight.mockRestore();
			}
		});
	});

	describe("container width", () => {
		it("defaults to the week view when the layout is 540 px or narrower", () => {
			render({ width: 400 });
			expect(wrapper.get("#week-button").attributes("aria-pressed")).toBe("true");
			expect(wrapper.find('[role="grid"]').exists()).toBe(false);
			expect(wrapper.find(".week-strip").exists()).toBe(true);
			expect(wrapper.text()).toContain(
				"Select a day. Swipe the week to move forward or back."
			);
			expect(wrapper.find(".list-view").exists()).toBe(false);
		});
		it("lists Week | Month | List on a narrow layout", () => {
			render({ width: 540 });
			const names = wrapper.findAll('[role="group"] button').map((b) => b.text());
			expect(names).toEqual(["Week", "Month", "List"]);
		});
		it("defaults to the month grid and hides Week above 540 px", () => {
			render({ width: 541 });
			expect(wrapper.find("#week-button").exists()).toBe(false);
			expect(wrapper.find('[role="grid"]').exists()).toBe(true);
			expect(wrapper.find(".week-strip").exists()).toBe(false);
		});
		it("falls back to the month grid when the width cannot be measured", () => {
			render();
			expect(wrapper.find('[role="grid"]').exists()).toBe(true);
			expect(wrapper.find("#week-button").exists()).toBe(false);
		});
		it("works without ResizeObserver", () => {
			vi.stubGlobal("ResizeObserver", undefined);
			render({ width: 400 });
			expect(wrapper.find("#week-button").exists()).toBe(true);
		});
		it("follows a resize across the breakpoint until the user chooses a view", async () => {
			render({ width: 900 });
			expect(wrapper.find('[role="grid"]').exists()).toBe(true);
			resizeLayout(400);
			await wrapper.vm.$nextTick();
			expect(wrapper.find(".week-strip").exists()).toBe(true);
			resizeLayout(900);
			await wrapper.vm.$nextTick();
			expect(wrapper.find('[role="grid"]').exists()).toBe(true);
			expect(wrapper.find("#week-button").exists()).toBe(false);
		});
		it("keeps a chosen view over the width default", async () => {
			render({ width: 400 });
			await wrapper.get("#month-button").trigger("click");
			expect(wrapper.find('[role="grid"]').exists()).toBe(true);
			expect(wrapper.emitted("update:mode")).toEqual([["month"]]);
		});
		it("shows the week strip above the list on a narrow layout", async () => {
			render({ width: 400 });
			await wrapper.get("#list-button").trigger("click");
			expect(wrapper.find(".week-strip").exists()).toBe(true);
			expect(wrapper.find(".list-view").exists()).toBe(true);
		});
		it("moves the selection a week from the strip", async () => {
			render({ width: 400 });
			await wrapper.get('[aria-label="Next week"]').trigger("click");
			expect(wrapper.emitted("select")).toEqual([["2026-11-18"]]);
		});
		it("selects a tapped week day", async () => {
			render({ width: 400 });
			await wrapper.get('[data-week-date="2026-11-13"]').trigger("click");
			expect(wrapper.emitted("select")).toEqual([["2026-11-13"]]);
		});
		it("passes a swipe of more than 50 px to the strip and ignores 50 px or less", async () => {
			render({ width: 400 });
			const strip = wrapper.get(".week-strip");
			const swipe = async (from, to) => {
				await strip.trigger("touchstart", { changedTouches: [{ clientX: from }] });
				await strip.trigger("touchend", { changedTouches: [{ clientX: to }] });
			};
			await swipe(200, 151);
			expect(wrapper.emitted("select")).toBeUndefined();
			await swipe(200, 149);
			expect(wrapper.emitted("select")).toEqual([["2026-11-18"]]);
		});
	});

	describe("legend", () => {
		it("names the supplier statuses and the star rule", () => {
			render();
			const legend = wrapper.get('[aria-label="Supplier payment status"]');
			expect(legend.text()).toContain("Supplier status");
			for (const word of ["Settled", "Partial", "Unpaid", "No dues"])
				expect(legend.text()).toContain(word);
			expect(legend.text()).toContain("★ Single invoice >50%");
		});
		it("has no hover-preview checkbox (owner decision 7)", () => {
			render();
			expect(wrapper.find("#quick-preview").exists()).toBe(false);
			expect(wrapper.text()).not.toContain("Quick hover preview");
		});
	});
});
