import { afterEach, beforeEach, vi } from "vitest";
import {
	mountGrid,
	resizeLayout,
	useFakeResizeObserver,
} from "../../../tests/payment-calendar/gridHarness";

// Landing of the "View 23 Nov" jump (template jumpToTrough, plan A11).
describe("Payment Calendar trough jump landing", () => {
	let wrapper, focus, scrollIntoView;
	useFakeResizeObserver();
	beforeEach(() => {
		focus = vi.spyOn(HTMLElement.prototype, "focus");
		scrollIntoView = vi.fn();
		Element.prototype.scrollIntoView = scrollIntoView;
	});
	afterEach(() => {
		wrapper?.unmount();
		focus.mockRestore();
		delete Element.prototype.scrollIntoView;
	});
	function render(options = {}) {
		wrapper = mountGrid(options);
		return wrapper;
	}
	// The page selects the date first, then asks the grid to land on it.
	async function jump(date, props = {}) {
		await wrapper.setProps({ selected: date, month: date.slice(0, 7), ...props });
		await wrapper.vm.revealTrough(date);
		await wrapper.vm.$nextTick();
	}
	const monthButton = (date) => wrapper.get(`[data-date="${date}"]`);
	const weekButton = (date) => wrapper.get(`[data-week-date="${date}"]`);
	const targets = () => wrapper.findAll(".trough-target");

	it("focuses the date button without scrolling and marks it as the target", async () => {
		render();
		await jump("2026-11-23");
		const button = monthButton("2026-11-23");
		expect(document.activeElement).toBe(button.element);
		expect(focus.mock.contexts.at(-1)).toBe(button.element);
		expect(focus.mock.calls.at(-1)[0]).toEqual({ preventScroll: true });
		expect(button.classes()).toContain("trough-target");
		expect(button.attributes("aria-pressed")).toBe("true");
	});
	it("scrolls the cell to the centre on desktop", async () => {
		render({ width: 1200 });
		await jump("2026-11-23");
		expect(scrollIntoView).toHaveBeenCalledTimes(1);
		expect(scrollIntoView.mock.contexts[0]).toBe(monthButton("2026-11-23").element);
		expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });
	});
	it("scrolls the calendar panel to the top on a phone", async () => {
		render({ width: 400 });
		await jump("2026-11-23");
		expect(scrollIntoView).toHaveBeenCalledTimes(1);
		expect(scrollIntoView.mock.contexts[0]).toBe(wrapper.get("section.pcal-cal").element);
		expect(scrollIntoView).toHaveBeenCalledWith({ block: "start" });
	});
	it("lands on the week strip button in week mode on a phone", async () => {
		render({ width: 400 });
		await jump("2026-11-23");
		const button = weekButton("2026-11-23");
		expect(document.activeElement).toBe(button.element);
		expect(button.classes()).toContain("trough-target");
		expect(wrapper.find('[data-date="2026-11-23"]').exists()).toBe(false);
	});
	it("returns from the list to the month grid", async () => {
		render();
		await wrapper.get("#list-button").trigger("click");
		expect(wrapper.find('[role="grid"]').exists()).toBe(false);
		await jump("2026-11-23");
		expect(wrapper.find(".list-view").exists()).toBe(false);
		expect(wrapper.get("#month-button").attributes("aria-pressed")).toBe("true");
		expect(document.activeElement).toBe(monthButton("2026-11-23").element);
	});
	it("returns from the list to the week view on a phone", async () => {
		render({ width: 400 });
		await wrapper.get("#list-button").trigger("click");
		await jump("2026-11-23");
		expect(wrapper.find(".list-view").exists()).toBe(false);
		expect(wrapper.get("#week-button").attributes("aria-pressed")).toBe("true");
		expect(document.activeElement).toBe(weekButton("2026-11-23").element);
	});
	it("leaves a month choice made on a phone for the default week view", async () => {
		render({ width: 400 });
		await wrapper.get("#month-button").trigger("click");
		await jump("2026-11-23");
		expect(wrapper.get("#week-button").attributes("aria-pressed")).toBe("true");
	});
	it("keeps exactly one target after a repeated jump", async () => {
		render();
		await jump("2026-11-23");
		await jump("2026-11-23");
		expect(targets()).toHaveLength(1);
		await jump("2026-11-19");
		expect(targets()).toHaveLength(1);
		expect(targets()[0].attributes("data-date")).toBe("2026-11-19");
	});
	it("clears the target on the first blur and keeps the selection", async () => {
		render();
		await jump("2026-11-23");
		wrapper.get("#today-button").element.focus();
		await wrapper.vm.$nextTick();
		expect(targets()).toHaveLength(0);
		expect(monthButton("2026-11-23").attributes("aria-pressed")).toBe("true");
		monthButton("2026-11-23").element.focus();
		await wrapper.vm.$nextTick();
		expect(targets()).toHaveLength(0);
	});
	it("clears the target when an arrow key moves focus", async () => {
		render();
		await jump("2026-11-23");
		await monthButton("2026-11-23").trigger("keydown", { key: "ArrowRight" });
		await wrapper.vm.$nextTick();
		expect(document.activeElement.dataset.date).toBe("2026-11-24");
		expect(targets()).toHaveLength(0);
	});
	it("clears the target when another day is chosen", async () => {
		render();
		await jump("2026-11-23");
		await monthButton("2026-11-24").trigger("click");
		expect(targets()).toHaveLength(0);
	});
	it("clears the target when the selection changes from outside", async () => {
		render();
		await jump("2026-11-23");
		await wrapper.setProps({ selected: "2026-11-24" });
		expect(targets()).toHaveLength(0);
	});
	it("clears the target when the view mode changes", async () => {
		render();
		await jump("2026-11-23");
		await wrapper.get("#list-button").trigger("click");
		expect(targets()).toHaveLength(0);
	});
	it("clears the target when the month changes", async () => {
		render();
		await jump("2026-11-23");
		await wrapper.setProps({ month: "2026-12", selected: "2026-12-01" });
		expect(targets()).toHaveLength(0);
	});
	it("clears the target when the layout crosses the phone breakpoint", async () => {
		render({ width: 1200 });
		await jump("2026-11-23");
		resizeLayout(400);
		await wrapper.vm.$nextTick();
		expect(targets()).toHaveLength(0);
	});
	it("waits for the month to be drawn when the date is in another month", async () => {
		render();
		const landing = wrapper.vm.revealTrough("2026-12-20");
		await landing;
		expect(targets()).toHaveLength(0);
		await wrapper.setProps({ month: "2026-12", selected: "2026-12-20" });
		await wrapper.vm.$nextTick();
		expect(document.activeElement).toBe(monthButton("2026-12-20").element);
		expect(monthButton("2026-12-20").classes()).toContain("trough-target");
	});
	it("does nothing for a date that is never drawn", async () => {
		render();
		await jump("2026-11-23");
		await wrapper.vm.revealTrough("2030-01-01");
		expect(targets()).toHaveLength(0);
	});

	describe("a jump that never lands", () => {
		// The page asked for 20 Dec but its selectDate was refused, so December never draws.
		async function pendingJump() {
			render();
			await wrapper.vm.revealTrough("2026-12-20");
			expect(targets()).toHaveLength(0);
		}
		async function expectNoLateLanding() {
			await wrapper.setProps({ month: "2026-12", selected: "2026-12-20" });
			await wrapper.vm.$nextTick();
			expect(targets()).toHaveLength(0);
			expect(document.activeElement.dataset.date).not.toBe("2026-12-20");
		}
		it("does not steal focus after another day was selected", async () => {
			await pendingJump();
			await wrapper.setProps({ selected: "2026-11-12" });
			await expectNoLateLanding();
		});
		it("does not steal focus after the user browsed", async () => {
			await pendingJump();
			await wrapper.get('[aria-label="Next month"]').trigger("click");
			await expectNoLateLanding();
		});
		it("does not steal focus after the user chose another view", async () => {
			await pendingJump();
			await wrapper.get("#list-button").trigger("click");
			await wrapper.get("#month-button").trigger("click");
			await expectNoLateLanding();
		});
		it("does not steal focus after the user moved with an arrow key", async () => {
			await pendingJump();
			monthButton("2026-11-11").element.focus();
			await monthButton("2026-11-11").trigger("keydown", { key: "ArrowRight" });
			await expectNoLateLanding();
		});
		it("still lands when the selection reaches the requested date", async () => {
			await pendingJump();
			await wrapper.setProps({ month: "2026-12", selected: "2026-12-20" });
			await wrapper.vm.$nextTick();
			expect(targets()).toHaveLength(1);
		});
	});
	it("does not throw when the grid unmounts during the jump", async () => {
		render();
		await wrapper.setProps({ selected: "2026-11-23" });
		const landing = wrapper.vm.revealTrough("2026-11-23");
		wrapper.unmount();
		wrapper = null;
		await expect(landing).resolves.toBeUndefined();
	});
});
