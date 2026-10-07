import { describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("@/utils/datetime", () => ({ timeAgo: () => "2 minutes ago" }));

import ReportRunCards from "./ReportRunCards.vue";

const RUN = {
	conversation: "c1",
	run: "PR-1",
	report_name: "Stock Balance",
	filters: "Fake Co · 01-09-2026",
	status: "preparing",
	ready_at: "",
};

const mountWith = (items, busy = false) => mount(ReportRunCards, { props: { items, busy } });

describe("ReportRunCards", () => {
	it("a run being prepared says so, with nothing to click", () => {
		const w = mountWith([RUN]);
		expect(w.find(".jv-rrc-name").text()).toBe("Stock Balance · Fake Co · 01-09-2026");
		expect(w.find(".jv-rrc-state").text()).toBe("Preparing in the background…");
		expect(w.find(".jv-rrc-show").exists()).toBe(false);
	});

	it("a ready run asks for its results", async () => {
		const ready = { ...RUN, status: "ready", ready_at: "2026-10-07 14:03:00" };
		const w = mountWith([ready]);
		expect(w.find(".jv-rrc-state").text()).toBe("Ready 2 minutes ago");
		await w.find(".jv-rrc-show").trigger("click");
		expect(w.emitted("show")[0]).toEqual([ready]);
	});

	it("a failed run says what to do", () => {
		const w = mountWith([{ ...RUN, status: "failed" }]);
		expect(w.find(".jv-rrc-state").text()).toBe(
			"Couldn't prepare this report. Ask Jarvis to run it again."
		);
		expect(w.find(".jv-rrc-show").exists()).toBe(false);
	});

	it("can't ask while a reply is running", () => {
		const w = mountWith([{ ...RUN, status: "ready" }], true);
		expect(w.find(".jv-rrc-show").attributes("disabled")).toBeDefined();
	});

	it("shows the report name as text", () => {
		const w = mountWith([{ ...RUN, report_name: "<img src=x onerror=alert(1)>" }]);
		expect(w.find("img").exists()).toBe(false);
		expect(w.find(".jv-rrc-name").text()).toContain("<img src=x onerror=alert(1)>");
	});
});
