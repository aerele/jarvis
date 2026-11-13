import { afterEach, vi } from "vitest";
import { mountGrid, novemberSnapshot } from "../../../tests/payment-calendar/gridHarness";
import { complete } from "../../../tests/payment-calendar/fixtures";

describe("Payment Calendar toolbar and month grid", () => {
	let wrapper;
	afterEach(() => wrapper?.unmount());
	function render(options) {
		wrapper = mountGrid(options);
		return wrapper;
	}
	const trigger = (date) => wrapper.get(`[data-date="${date}"]`);
	const cell = (date) => wrapper.get(`[data-cell-date="${date}"]`);

	describe("toolbar", () => {
		it("shows the month, the INR tag and a Month/List switch with aria-pressed", () => {
			render();
			expect(wrapper.get("#month-heading").text()).toBe("November 2026");
			expect(wrapper.text()).toContain("INR");
			const group = wrapper.get('[role="group"][aria-label="Calendar display"]');
			expect(group.findAll("button").map((b) => b.text())).toEqual(["Month", "List"]);
			expect(wrapper.get("#month-button").attributes("aria-pressed")).toBe("true");
			expect(wrapper.get("#list-button").attributes("aria-pressed")).toBe("false");
		});
		it("emits browse(-1), browse(1) and today", async () => {
			render();
			await wrapper.get('[aria-label="Previous month"]').trigger("click");
			await wrapper.get('[aria-label="Next month"]').trigger("click");
			await wrapper.get("#today-button").trigger("click");
			expect(wrapper.emitted("browse")).toEqual([[-1], [1]]);
			expect(wrapper.emitted("today")).toHaveLength(1);
		});
		it("disables Next at the last selectable month with the range title", () => {
			render({ props: { month: "2027-10", selected: "2027-10-15" } });
			const next = wrapper.get('[aria-label="Next month"]');
			expect(next.attributes("disabled")).toBeDefined();
			expect(next.attributes("title")).toBe("Outside the calendar range");
			expect(
				wrapper.get('[aria-label="Previous month"]').attributes("title")
			).toBeUndefined();
		});
		it("disables Previous at the first selectable month with the range title", () => {
			render({ props: { month: "2025-11", selected: "2025-11-15" } });
			const previous = wrapper.get('[aria-label="Previous month"]');
			expect(previous.attributes("disabled")).toBeDefined();
			expect(previous.attributes("title")).toBe("Outside the calendar range");
		});
		it("switches to the list and reports the choice", async () => {
			render();
			await wrapper.get("#list-button").trigger("click");
			expect(wrapper.emitted("update:mode")).toEqual([["list"]]);
			expect(wrapper.find('[role="grid"]').exists()).toBe(false);
			expect(wrapper.get("#list-button").attributes("aria-pressed")).toBe("true");
		});
	});

	describe("state line", () => {
		it("says Loading values while the month loads and marks the section busy", () => {
			render({ props: { loading: true } });
			expect(wrapper.get('[role="status"]').text()).toBe("Loading values…");
			expect(wrapper.get("section").attributes("aria-busy")).toBe("true");
		});
		it("says it is waiting for the worker", () => {
			render({ props: { waiting: true } });
			expect(wrapper.get('[role="status"]').text()).toBe("Waiting to load this month…");
		});
		it("offers Retry after a failed load", async () => {
			render({ props: { loadError: true } });
			expect(wrapper.get('[role="status"]').text()).toContain("Couldn’t load this month.");
			await wrapper.get('[role="status"] button').trigger("click");
			expect(wrapper.emitted("retry")).toHaveLength(1);
		});
		it("explains why the update stopped, from the refresh issue's code", () => {
			render({ props: { loadError: true, issue: { code: "worker_busy" } } });
			expect(wrapper.get('[role="status"]').text()).toContain(
				"Your calendar jobs are still running."
			);
			expect(wrapper.find('[role="status"] button').exists()).toBe(true);
		});
		it("offers no Retry for a forbidden issue: it cannot succeed", () => {
			render({ props: { loadError: true, issue: { code: "forbidden" } } });
			expect(wrapper.get('[role="status"]').text()).toContain("no longer have access");
			expect(wrapper.find('[role="status"] button').exists()).toBe(false);
		});
		function withoutDay(date) {
			const snapshot = novemberSnapshot();
			delete snapshot.projection.days[date];
			return snapshot;
		}
		it("labels a missing date as loading, not as an empty day", () => {
			render({ props: { snapshot: withoutDay("2026-11-03"), loading: true } });
			expect(cell("2026-11-03").text()).toContain("Loading…");
			expect(cell("2026-11-03").text()).not.toContain("No dues");
		});
		it("labels a missing date as not loaded after a failure", () => {
			render({ props: { snapshot: withoutDay("2026-11-03"), loadError: true } });
			expect(cell("2026-11-03").text()).toContain("Not loaded");
		});
		it("labels an adjacent-month date as loadable", () => {
			render();
			expect(cell("2026-12-01").text()).toContain("Load month");
		});
		it("explains a month without any data once loading ends", () => {
			render({ props: { month: "2026-12", selected: "2026-12-05" } });
			expect(wrapper.text()).toContain(
				"No payment or forecast data is available for December 2026."
			);
		});
		it("does not claim missing data while loading", () => {
			render({ props: { month: "2026-12", selected: "2026-12-05", loading: true } });
			expect(wrapper.text()).not.toContain("No payment or forecast data");
		});
	});

	describe("weeks", () => {
		it.each([
			["2026-02", 4],
			["2026-11", 5],
			["2026-10", 5],
			["2026-08", 6],
		])("renders %s as %i week rows (A3)", (month, weeks) => {
			render({ props: { month, selected: month + "-10" } });
			const rows = wrapper
				.findAll('[role="row"]')
				.filter((r) => r.find('[role="gridcell"]').exists());
			expect(rows).toHaveLength(weeks);
			for (const row of rows) expect(row.findAll('[role="gridcell"]')).toHaveLength(7);
		});
		it("starts on Sunday with seven column headers", () => {
			render();
			const names = wrapper.findAll('[role="columnheader"]').map((h) => h.text());
			expect(names).toEqual(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]);
			expect(wrapper.get('[role="grid"]').attributes("aria-label")).toBe("November 2026");
			expect(wrapper.findAll("[data-date]")[0].attributes("data-date")).toBe("2026-11-01");
		});
		it("greys adjacent-month dates", () => {
			render();
			expect(cell("2026-12-01").classes()).toContain("adjacent");
			expect(cell("2026-11-30").classes()).not.toContain("adjacent");
		});
	});

	describe("cells", () => {
		it("labels the date button with the full date and both server amounts", () => {
			render();
			expect(trigger("2026-11-11").attributes("aria-label")).toBe(
				"Wednesday, 11 Nov, payable ₹42,048.00, receivable ₹86,250.00"
			);
			expect(trigger("2026-11-11").attributes("aria-pressed")).toBe("true");
			expect(trigger("2026-11-12").attributes("aria-pressed")).toBe("false");
		});
		it("says data unavailable on a date without data", () => {
			render();
			expect(trigger("2026-12-01").attributes("aria-label")).toBe(
				"Tuesday, 1 Dec, data unavailable"
			);
		});
		it("marks today from the snapshot anchor only", () => {
			render();
			expect(wrapper.findAll("[aria-current=date]")).toHaveLength(1);
			expect(trigger("2026-11-11").attributes("aria-current")).toBe("date");
			expect(cell("2026-11-11").classes()).toContain("today");
			expect(cell("2026-11-10").classes()).not.toContain("today");
		});
		it.each([
			["a browser clock a day ahead", "2026-11-12T23:30:00Z"],
			["a browser clock a day behind", "2026-11-10T00:30:00Z"],
			["a browser clock a month away", "2027-01-05T10:00:00Z"],
		])("ignores %s (AE1)", (_, now) => {
			vi.useFakeTimers({ now: new Date(now) });
			try {
				render();
				expect(
					wrapper.findAll(".day.today").map((d) => d.attributes("data-cell-date"))
				).toEqual(["2026-11-11"]);
				expect(wrapper.findAll("[aria-current=date]")).toHaveLength(1);
			} finally {
				vi.useRealTimers();
			}
		});
		it("keeps cell backgrounds neutral whatever the status", () => {
			render();
			for (const day of wrapper.findAll(".day"))
				expect(day.classes().join(" ")).not.toMatch(
					/settlement|risk|unpaid|partial|settled/
				);
		});
		it("disables dates outside the selectable range", () => {
			const snapshot = novemberSnapshot();
			snapshot.scope.selectable_end = "2026-11-30";
			render({ props: { snapshot } });
			expect(trigger("2026-11-30").attributes("disabled")).toBeUndefined();
			expect(trigger("2026-12-05").attributes("disabled")).toBeDefined();
			expect(cell("2026-12-05").find(".amount-line").exists()).toBe(false);
		});
	});

	describe("concentration star (A2)", () => {
		it("shows the star with the server's invoice and percentage in its title", () => {
			render();
			const star = cell("2026-11-19").get(".hotspot");
			expect(star.text()).toBe("★");
			expect(star.attributes("title")).toBe(
				"PINV-2026-0207: ₹1,02,236.00 · 100% of this date's supplier dues"
			);
			expect(trigger("2026-11-19").attributes("aria-label")).toBe(
				"Thursday, 19 Nov, payable ₹1,02,236.00, receivable ₹0.00, one invoice exceeds half of supplier dues"
			);
		});
		it("places the star before the status inside the markers", () => {
			render();
			const markers = cell("2026-11-19").get(".cell-markers");
			expect(markers.element.children[0].className).toContain("hotspot");
			expect(markers.element.children[1].className).toContain("status");
		});
		it("shows no star when the server says the date is not concentrated", () => {
			render();
			expect(cell("2026-11-11").find(".hotspot").exists()).toBe(false);
			expect(trigger("2026-11-11").attributes("aria-label")).not.toContain("exceeds half");
		});
		it("trusts the server flag at exactly 50 percent (no client threshold)", () => {
			const snapshot = novemberSnapshot();
			Object.assign(snapshot.projection.days["2026-11-19"], {
				payable_concentrated: false,
				payable_concentration_pct: "50.0",
			});
			render({ props: { snapshot } });
			expect(cell("2026-11-19").find(".hotspot").exists()).toBe(false);
		});
		it("shows no star without a top invoice to name", () => {
			const snapshot = novemberSnapshot();
			snapshot.projection.days["2026-11-19"].payable_top_invoice = null;
			render({ props: { snapshot } });
			expect(cell("2026-11-19").find(".hotspot").exists()).toBe(false);
		});
		it.each(["", null, undefined, "abc", "NaN", "Infinity", "-5"])(
			"leaves the percentage out of the title when the server value is %j",
			(pct) => {
				const snapshot = novemberSnapshot();
				snapshot.projection.days["2026-11-19"].payable_concentration_pct = pct;
				render({ props: { snapshot } });
				const title = cell("2026-11-19").get(".hotspot").attributes("title");
				expect(title).toBe("PINV-2026-0207: ₹1,02,236.00");
				expect(title).not.toMatch(/NaN|%|undefined|null/);
			}
		);
		it("escapes party and voucher text in the title", () => {
			const snapshot = novemberSnapshot();
			snapshot.projection.days["2026-11-19"].payable_top_invoice.voucher_no =
				'"><img src=x>';
			render({ props: { snapshot } });
			expect(cell("2026-11-19").find("img").exists()).toBe(false);
		});
	});

	describe("status marker", () => {
		it("shows a dot and the Unpaid / Partial / Settled word", () => {
			const snapshot = novemberSnapshot();
			snapshot.projection.days["2026-11-12"].settlement = "Settled";
			render({ props: { snapshot } });
			const status = (date) => cell(date).get(".status");
			expect(status("2026-11-15").text()).toBe("Unpaid");
			expect(status("2026-11-15").classes()).toContain("unpaid");
			expect(status("2026-11-11").text()).toBe("Partial");
			expect(status("2026-11-11").classes()).toContain("partial");
			expect(status("2026-11-12").text()).toBe("Settled");
			expect(status("2026-11-12").classes()).toContain("settled");
			expect(status("2026-11-12").find("i").exists()).toBe(true);
		});
		it("hides the marker for No dues", () => {
			const snapshot = novemberSnapshot();
			snapshot.projection.days["2026-11-24"].settlement = "No dues";
			render({ props: { snapshot } });
			expect(cell("2026-11-24").find(".status").exists()).toBe(false);
		});
	});

	describe("amount lines and net pill", () => {
		it("shows Pay and Receive buttons with whole-rupee values and exact accessible names", () => {
			render();
			const [pay, receive] = cell("2026-11-11").findAll(".amount-line");
			expect(pay.text()).toBe("Pay₹42,048");
			expect(pay.attributes("aria-label")).toBe(
				"View payable invoices for 11 Nov: ₹42,048.00"
			);
			expect(receive.text()).toBe("Receive₹86,250");
			expect(receive.attributes("aria-label")).toBe(
				"View receivable invoices for 11 Nov: ₹86,250.00"
			);
			expect(receive.classes()).toContain("incoming");
		});
		it("selects the date and the side from an amount line", async () => {
			render();
			const [pay, receive] = cell("2026-11-11").findAll(".amount-line");
			await pay.trigger("click");
			await receive.trigger("click");
			expect(wrapper.emitted("select")).toEqual([
				["2026-11-11", "Payable"],
				["2026-11-11", "Receivable"],
			]);
		});
		it("selects the date and keeps the side when the date number is clicked", async () => {
			render();
			await trigger("2026-11-12").trigger("click");
			expect(wrapper.emitted("select")).toEqual([["2026-11-12"]]);
		});
		it("renders the net from the server field with the template wording", () => {
			render();
			const pill = cell("2026-11-11").get(".day-net");
			expect(pill.text()).toBe("Net +₹44.2k");
			expect(pill.classes()).toContain("positive");
			expect(pill.attributes("title")).toBe(
				"Scheduled receipts minus scheduled payments: ₹44,202.00. This is not the closing cash balance."
			);
		});
		it("does no money arithmetic: it shows the server net even when it disagrees with the sides", () => {
			const snapshot = novemberSnapshot();
			snapshot.projection.days["2026-11-11"].net = "-100000.00";
			render({ props: { snapshot } });
			const pill = cell("2026-11-11").get(".day-net");
			expect(pill.text()).toBe("Net −₹1.0L");
			expect(pill.classes()).toContain("negative");
		});
		it("styles a negative net as negative", () => {
			render();
			expect(cell("2026-11-23").get(".day-net").text()).toBe("Net −₹19.4k");
			expect(cell("2026-11-23").get(".day-net").classes()).toContain("negative");
		});
		it("shows a neutral pill when both sides are non-zero and the net is zero", () => {
			const snapshot = novemberSnapshot();
			snapshot.projection.days["2026-11-11"].net = "0.00";
			render({ props: { snapshot } });
			const pill = cell("2026-11-11").get(".day-net");
			expect(pill.text()).toBe("Net ₹0");
			expect(pill.classes()).not.toContain("positive");
			expect(pill.classes()).not.toContain("negative");
		});
		it("shows a quiet dash for a date with no scheduled flow", () => {
			render();
			const dash = cell("2026-11-24").get(".quiet-day");
			expect(dash.text()).toBe("—");
			expect(dash.attributes("aria-label")).toBe("No scheduled net flow");
			expect(cell("2026-11-24").find(".amount-line").exists()).toBe(false);
			expect(cell("2026-11-24").find(".day-net").exists()).toBe(false);
		});
		it("shows a quiet dash that says data unavailable when the server net is null", () => {
			const snapshot = novemberSnapshot();
			snapshot.projection.days["2026-11-11"].net = null;
			render({ props: { snapshot } });
			expect(cell("2026-11-11").find(".day-net").exists()).toBe(false);
			expect(cell("2026-11-11").get(".quiet-day").attributes("aria-label")).toBe(
				"Data unavailable"
			);
		});
		it("shows a quiet dash that says data unavailable for a date with no data", () => {
			render();
			expect(cell("2026-12-01").get(".quiet-day").attributes("aria-label")).toBe(
				"Data unavailable"
			);
			expect(cell("2026-12-01").find(".amount-line").exists()).toBe(false);
		});
		it("never shows an unavailable side as zero", () => {
			const snapshot = novemberSnapshot();
			Object.assign(snapshot.projection.days["2026-11-11"], {
				receivable_remaining: null,
				net: null,
			});
			render({ props: { snapshot } });
			const [, receive] = cell("2026-11-11").findAll(".amount-line");
			expect(receive.text()).toBe("Receive—");
			expect(receive.attributes("aria-label")).toBe(
				"View receivable invoices for 11 Nov: unavailable"
			);
			expect(trigger("2026-11-11").attributes("aria-label")).toContain(
				"receivable unavailable"
			);
		});
	});

	describe("keyboard", () => {
		it("gives only the selected date a tab stop", () => {
			render();
			const stops = wrapper.findAll('[data-date][tabindex="0"]');
			expect(stops.map((s) => s.attributes("data-date"))).toEqual(["2026-11-11"]);
		});
		it.each([
			["ArrowRight", "2026-11-12"],
			["ArrowLeft", "2026-11-10"],
			["ArrowDown", "2026-11-18"],
			["ArrowUp", "2026-11-04"],
		])("moves focus with %s and changes nothing else", async (key, target) => {
			render();
			trigger("2026-11-11").element.focus();
			await trigger("2026-11-11").trigger("keydown", { key });
			expect(document.activeElement.dataset.date).toBe(target);
			expect(wrapper.emitted("select")).toBeUndefined();
			expect(wrapper.emitted("browse")).toBeUndefined();
			expect(wrapper.findAll('[data-date][tabindex="0"]')).toHaveLength(1);
			expect(wrapper.get('[data-date][tabindex="0"]').attributes("data-date")).toBe(target);
		});
		it("moves focus onto an adjacent-month date that is drawn", async () => {
			render();
			trigger("2026-11-30").element.focus();
			await trigger("2026-11-30").trigger("keydown", { key: "ArrowRight" });
			expect(document.activeElement.dataset.date).toBe("2026-12-01");
		});
		it("stays put when the target date is not drawn", async () => {
			render();
			trigger("2026-12-05").element.focus();
			await trigger("2026-12-05").trigger("keydown", { key: "ArrowDown" });
			expect(document.activeElement.dataset.date).toBe("2026-12-05");
		});
		it("does not move focus onto a date outside the selectable range", async () => {
			const snapshot = novemberSnapshot();
			snapshot.scope.selectable_end = "2026-11-30";
			render({ props: { snapshot } });
			trigger("2026-11-30").element.focus();
			await trigger("2026-11-30").trigger("keydown", { key: "ArrowRight" });
			expect(document.activeElement.dataset.date).toBe("2026-11-30");
		});
		it.each(["Enter", " "])("leaves %j to the native button click", async (key) => {
			render();
			await trigger("2026-11-12").trigger("keydown", { key });
			expect(wrapper.emitted("select")).toBeUndefined();
			await trigger("2026-11-12").trigger("click");
			expect(wrapper.emitted("select")).toEqual([["2026-11-12"]]);
		});
	});
});

describe("Payment Calendar grid at the range edge", () => {
	let wrapper;
	afterEach(() => wrapper?.unmount());
	it("disables next-month navigation and carry-window cells at the last month", () => {
		const snapshot = complete();
		wrapper = mountGrid({ props: { snapshot, month: "2027-09", selected: "2027-09-10" } });
		expect(wrapper.get('[aria-label="Next month"]').attributes("disabled")).toBeDefined();
		expect(wrapper.get('[data-date="2027-10-01"]').attributes("disabled")).toBeDefined();
	});
});
