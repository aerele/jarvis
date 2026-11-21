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

	test("a tap sends that suggestion as it is", async () => {
		const w = mount(SuggestedReplies, { props: { replies } });
		const chips = w.findAll(".jv-followup");
		expect(chips.map((c) => c.text())).toEqual(replies);
		expect(w.find(".jv-followup-send").exists()).toBe(false);
		await chips[1].trigger("click");
		expect(w.emitted("send")).toEqual([["Create the timesheets"]]);
	});

	test("several answers tick, then one Send sends them together in their order", async () => {
		const w = mount(SuggestedReplies, { props: { replies, multiSelect: true } });
		const send = w.find(".jv-followup-send");
		expect(send.classes()).toContain("hidden");
		const chips = w.findAll(".jv-followup");
		await chips[2].trigger("click");
		await chips[0].trigger("click");
		expect(w.emitted("send")).toBeUndefined();
		expect(chips[0].attributes("aria-pressed")).toBe("true");
		expect(w.find(".jv-followup-send").classes()).not.toContain("hidden");
		await chips[2].trigger("click"); // untick
		await chips[2].trigger("click"); // tick again
		await w.find(".jv-followup-send").trigger("click");
		expect(w.emitted("send")).toEqual([["Show the missing dates, Only the last 30 days"]]);
	});

	test("nothing is sent while disabled", async () => {
		const w = mount(SuggestedReplies, { props: { replies, disabled: true } });
		await w.findAll(".jv-followup")[0].trigger("click");
		expect(w.emitted("send")).toBeUndefined();
	});

	test("on a touch screen a tap in the first moment is ignored", async () => {
		vi.useFakeTimers();
		window.matchMedia = () => ({ matches: true });
		const w = mount(SuggestedReplies, { props: { replies } });
		await w.findAll(".jv-followup")[0].trigger("click");
		expect(w.emitted("send")).toBeUndefined();
		vi.advanceTimersByTime(450);
		await w.findAll(".jv-followup")[0].trigger("click");
		expect(w.emitted("send")).toEqual([["Show the missing dates"]]);
	});
});
