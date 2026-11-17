import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import CalendarToast from "./CalendarToast.vue";

const toast = () => document.body.querySelector(".pcal-toast");

describe("CalendarToast", () => {
	let wrapper;
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => {
		wrapper?.unmount();
		vi.useRealTimers();
	});

	it("is a hidden status region until there is a message", async () => {
		wrapper = mount(CalendarToast, { props: { message: "" } });
		expect(toast().getAttribute("role")).toBe("status");
		expect(toast().hidden).toBe(true);
		await wrapper.setProps({ message: "Company changed." });
		expect(toast().hidden).toBe(false);
		expect(toast().textContent).toBe("Company changed.");
		expect(toast().classList.contains("pcal-overlay")).toBe(true);
	});

	it("hides itself after 3.5 seconds and tells the page", async () => {
		wrapper = mount(CalendarToast, { props: { message: "" } });
		await wrapper.setProps({ message: "Minimum cash balance saved." });
		vi.advanceTimersByTime(3499);
		expect(wrapper.emitted("hidden")).toBeUndefined();
		vi.advanceTimersByTime(1);
		expect(wrapper.emitted("hidden")).toHaveLength(1);
	});

	it("restarts the timer when a new message replaces the old one", async () => {
		wrapper = mount(CalendarToast, { props: { message: "" } });
		await wrapper.setProps({ message: "One" });
		vi.advanceTimersByTime(3000);
		await wrapper.setProps({ message: "Two" });
		vi.advanceTimersByTime(3000);
		expect(wrapper.emitted("hidden")).toBeUndefined();
		vi.advanceTimersByTime(500);
		expect(wrapper.emitted("hidden")).toHaveLength(1);
	});

	it("stops the timer when it unmounts", () => {
		wrapper = mount(CalendarToast, { props: { message: "Bye" } });
		wrapper.unmount();
		vi.advanceTimersByTime(5000);
		expect(vi.getTimerCount()).toBe(0);
		wrapper = null;
	});
});
