// A skill picked in the "+" menu rides on the message as its "/skill_name" token.
import { describe, expect, test } from "vitest";
import { hasSkillToken, withSkillToken } from "./skillToken.js";

describe("withSkillToken", () => {
	test("puts the token in front of the question", () => {
		expect(withSkillToken("close March", "month-end")).toBe("/month-end close March");
		expect(withSkillToken("  close March  ", "month-end")).toBe("/month-end close March");
	});
	test("the skill alone is a message", () => {
		expect(withSkillToken("", "month-end")).toBe("/month-end");
		expect(withSkillToken(null, "month-end")).toBe("/month-end");
	});
	test("never twice: a typed or resent token is kept as it is", () => {
		expect(withSkillToken("/month-end close March", "month-end")).toBe(
			"/month-end close March"
		);
		expect(withSkillToken("please /month-end now", "month-end")).toBe("please /month-end now");
		expect(withSkillToken("run /month-end.", "month-end")).toBe("run /month-end.");
	});
	test("a longer slug sharing the prefix is not the same skill", () => {
		expect(withSkillToken("/month-end-close it", "month-end")).toBe(
			"/month-end /month-end-close it"
		);
	});
	test("a name the server could not resolve leaves the text alone", () => {
		expect(withSkillToken("hi", "Bad Name")).toBe("hi");
		expect(withSkillToken("hi", "")).toBe("hi");
	});
	// Mirrors jarvis.chat.custom_skills._INVOKE_RE: at the start or after whitespace.
	test("matches the server's token pattern", () => {
		const SERVER = /(?:^|\s)\/([a-z0-9]+(?:-[a-z0-9]+)*)/g;
		const sent = withSkillToken("> quoted\n\nclose March", "month-end");
		expect([...sent.matchAll(SERVER)].map((m) => m[1])).toEqual(["month-end"]);
		expect(hasSkillToken("x/month-end", "month-end")).toBe(false);
	});
});
