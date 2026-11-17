// aerele/jarvis#2191: the quote card a Reply puts at the top of the composer.
import { describe, expect, test } from "vitest";
import { mount } from "@vue/test-utils";
import ReplyQuote from "./ReplyQuote.vue";

describe("ReplyQuote", () => {
	test("names who is quoted and shows the quoted text", () => {
		const w = mount(ReplyQuote, { props: { text: "Row 3: Acme\n1,200", who: "Jarvis" } });
		expect(w.find(".jv-rq-who").text()).toBe("Replying to Jarvis");
		expect(w.find(".jv-rq-text").text()).toBe("Row 3: Acme\n1,200");
		expect(w.attributes("aria-label")).toBe("Replying to Jarvis");
	});

	test("the close button asks to remove the quote", async () => {
		const w = mount(ReplyQuote, { props: { text: "x" } });
		await w.find("button[aria-label='Remove quote']").trigger("click");
		expect(w.emitted("close")).toHaveLength(1);
	});

	test("defaults to Jarvis when no name is given", () => {
		const w = mount(ReplyQuote, { props: { text: "x" } });
		expect(w.find(".jv-rq-who").text()).toBe("Replying to Jarvis");
	});
});
