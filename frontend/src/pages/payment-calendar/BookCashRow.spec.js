import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import BookCashRow from "./BookCashRow.vue";
import { scenario } from "../../../tests/payment-calendar/templateFixtures";
import { modernStatus } from "../../../tests/payment-calendar/headlineFixtures";

const render = (snapshot, timezone = "Asia/Kolkata") =>
	mount(BookCashRow, { props: { snapshot, timezone } });

describe("BookCashRow", () => {
	it("shows the template's context row", () => {
		const wrapper = render(modernStatus());
		expect(wrapper.get("#header-book-cash").text()).toBe("₹2,46,850.00");
		expect(wrapper.get("#account-balances").text()).toBe("View account balances →");
		expect(wrapper.get(".context-time").text()).toBe("Snapshot · 11 Nov, 10:42 IST");
		expect(wrapper.text()).toContain("Book cash now");
	});

	it("opens the balances from both buttons", async () => {
		const wrapper = render(modernStatus());
		await wrapper.get("#header-book-cash").trigger("click");
		await wrapper.get("#account-balances").trigger("click");
		expect(wrapper.emitted("open")).toHaveLength(2);
	});

	it("says Unavailable, never ₹0, when no account is selected", () => {
		const status = scenario("default");
		status.projection.book_cash = null;
		const wrapper = render(status);
		expect(wrapper.get("#header-book-cash").text()).toBe("Unavailable");
	});

	it("shows the loading state, like the cards, while the loading frame is shown", () => {
		const wrapper = render({
			snapshot_id: null,
			currency: "INR",
			generated_at: "2026-11-11T05:12:00+00:00",
			projection: { precision: 2, book_cash: null },
		});
		expect(wrapper.get("#header-book-cash").text()).toBe("Loading values…");
		expect(wrapper.get("#header-book-cash").attributes("disabled")).toBeDefined();
		expect(wrapper.get("#account-balances").attributes("disabled")).toBeDefined();
		expect(wrapper.text()).not.toContain("Unavailable");
		expect(wrapper.find(".context-time").exists()).toBe(false);
	});
});
