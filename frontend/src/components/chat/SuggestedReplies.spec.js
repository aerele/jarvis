// The user's likely next messages under the latest reply (jarvis.chat.suggest_replies).
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount } from "@vue/test-utils";
import SuggestedReplies from "./SuggestedReplies.vue";

const replies = ["Show the missing dates", "Create the timesheets", "Only the last 30 days"];

describe("SuggestedReplies", () => {
	afterEach(() => {
		vi.useRealTimers();
		delete window.matchMedia;
	});

	test("a tap ticks it and lights Send, but sends nothing", async () => {
		const w = mount(SuggestedReplies, { props: { replies } });
		const chips = w.findAll(".jv-followup");
		expect(chips.map((c) => c.text())).toEqual(replies);
		expect(w.find(".jv-followup-send").classes()).toContain("hidden");
		await chips[1].trigger("click");
		expect(w.emitted("send")).toBeUndefined();
		expect(w.emitted("pick")).toEqual([["Create the timesheets"]]);
		expect(chips[1].classes()).toContain("on");
		expect(chips[1].attributes("aria-checked")).toBe("true");
		expect(chips[1].find("path").exists()).toBe(true); // the tick
		expect(w.find(".jv-followup-send").classes()).not.toContain("hidden");
	});

	test("one at a time: a second tap moves the tick, the same tap again clears it", async () => {
		const w = mount(SuggestedReplies, { props: { replies } });
		const chips = w.findAll(".jv-followup");
		await chips[0].trigger("click");
		await chips[2].trigger("click");
		expect(w.findAll(".jv-followup.on").map((c) => c.text())).toEqual([
			"Only the last 30 days",
		]);
		await chips[2].trigger("click");
		expect(w.findAll(".jv-followup.on")).toHaveLength(0);
		expect(w.find(".jv-followup-send").classes()).toContain("hidden");
		expect(w.emitted("pick")).toEqual([
			["Show the missing dates"],
			["Only the last 30 days"],
			[""],
		]);
	});

	test("Send sends exactly the chosen one and releases the composer", async () => {
		const w = mount(SuggestedReplies, { props: { replies } });
		await w.findAll(".jv-followup")[0].trigger("click");
		await w.find(".jv-followup-send").trigger("click");
		expect(w.emitted("send")).toEqual([["Show the missing dates"]]);
		expect(w.emitted("pick").at(-1)).toEqual([""]);
	});

	test("unmounting with one chosen releases the composer", async () => {
		const w = mount(SuggestedReplies, { props: { replies } });
		await w.findAll(".jv-followup")[0].trigger("click");
		w.unmount();
		expect(w.emitted("pick").at(-1)).toEqual([""]);
	});

	test("nothing is chosen or sent while disabled", async () => {
		const w = mount(SuggestedReplies, { props: { replies, disabled: true } });
		await w.findAll(".jv-followup")[0].trigger("click");
		await w.find(".jv-followup-send").trigger("click");
		expect(w.emitted("pick")).toBeUndefined();
		expect(w.emitted("send")).toBeUndefined();
	});

	test("on a touch screen a tap in the first moment is ignored", async () => {
		vi.useFakeTimers();
		window.matchMedia = () => ({ matches: true });
		const w = mount(SuggestedReplies, { props: { replies } });
		await w.findAll(".jv-followup")[0].trigger("click");
		expect(w.emitted("pick")).toBeUndefined();
		vi.advanceTimersByTime(450);
		await w.findAll(".jv-followup")[0].trigger("click");
		expect(w.emitted("pick")).toEqual([["Show the missing dates"]]);
	});
});
