import { describe, it, expect } from "vitest";
import {
	reply,
	replyEnvelope,
	replyNames,
	replyRequest,
	scenario,
	scenarioNames,
	statusEnvelope,
} from "./templateFixtures";

// Contract test between PR 1 (golden JSON from the Python test) and PR 2.
describe("template golden scenario: default", () => {
	const projection = scenario("default").projection;

	it("lists the default scenario", () => {
		expect(scenarioNames()).toContain("default");
	});
	it("carries the month net total", () => {
		expect(projection.month_totals.net).toBe("329702.00");
	});
	it("carries the due-now figures", () => {
		expect(projection.due_now.immediate).toBe("27500.00");
		expect(projection.due_now.total).toBe("69548.00");
	});
	it("closes on the anchor day at the template balance", () => {
		expect(projection.anchor).toBe("2026-11-11");
		expect(projection.days["2026-11-11"].projected_closing).toBe("263552.00");
	});
	it("flags 19 Nov as a concentrated payable day", () => {
		expect(projection.days["2026-11-19"].payable_concentrated).toBe(true);
	});
	it("rejects an unknown scenario name", () => {
		expect(() => scenario("nope")).toThrow(/nope/);
	});
	it("wraps the status payload in an ok envelope", () => {
		const envelope = statusEnvelope("default");
		expect(envelope).toEqual({ ok: true, data: scenario("default") });
	});
	it("returns an independent copy on every call", () => {
		scenario("default").projection.month_totals.net = "0.00";
		expect(scenario("default").projection.month_totals.net).toBe("329702.00");
	});
});

describe("template golden API replies", () => {
	it("lists reply scenarios apart from status scenarios", () => {
		expect(replyNames()).toContain("default_bucket_now");
		expect(replyNames()).not.toContain("default");
		expect(scenarioNames()).not.toContain("default_bucket_now");
	});
	it("carries the due-now bucket totals", () => {
		expect(reply("default_bucket_immediate").total_remaining).toBe("27500.00");
		expect(reply("default_bucket_now").total_remaining).toBe("69548.00");
		expect(reply("default_bucket_today").total_remaining).toBe("42048.00");
	});
	it("exposes the request that produced a reply", () => {
		expect(replyRequest("default_bucket_now")).toEqual(expect.any(Object));
	});
	it("wraps the reply payload in an ok envelope", () => {
		expect(replyEnvelope("default_bucket_now")).toEqual({
			ok: true,
			data: reply("default_bucket_now"),
		});
	});
	it("rejects an unknown reply name", () => {
		expect(() => reply("nope")).toThrow(/nope/);
		expect(() => replyRequest("nope")).toThrow(/nope/);
	});
	it("returns independent copies", () => {
		reply("default_bucket_now").total_remaining = "0.00";
		expect(reply("default_bucket_now").total_remaining).toBe("69548.00");
		replyRequest("default_bucket_now").x = 1;
		expect(replyRequest("default_bucket_now").x).toBeUndefined();
	});
});
