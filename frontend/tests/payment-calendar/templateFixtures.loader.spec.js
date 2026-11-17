import { describe, it, expect } from "vitest";
import { indexScenarios } from "./templateFixtures";

describe("golden scenario loader", () => {
	const golden = (name) => ({ scenario: name, status: { snapshot_id: name } });
	const replyFile = (name) => ({
		scenario: name,
		request: { bucket: name },
		data: { total_remaining: name },
	});

	it("indexes each status file by its scenario key", () => {
		const { statuses } = indexScenarios({ "./a.json": golden("a"), "./b.json": golden("b") });
		expect(Object.keys(statuses)).toEqual(["a", "b"]);
		expect(statuses.b).toEqual({ snapshot_id: "b" });
	});
	it("indexes each API reply file by its scenario key", () => {
		const { statuses, replies } = indexScenarios({
			"./a.json": golden("a"),
			"./r.json": replyFile("r"),
		});
		expect(Object.keys(statuses)).toEqual(["a"]);
		expect(Object.keys(replies)).toEqual(["r"]);
		expect(replies.r).toEqual({ request: { bucket: "r" }, data: { total_remaining: "r" } });
	});
	it("throws when a golden file has no scenario key", () => {
		expect(() => indexScenarios({ "./bad.json": { status: {} } })).toThrow(/bad\.json/);
	});
	it("throws when a golden file has no status and no reply", () => {
		expect(() => indexScenarios({ "./bad.json": { scenario: "x" } })).toThrow(/bad\.json/);
	});
	it("throws when a file has a status and a reply", () => {
		const both = { ...golden("x"), ...replyFile("x") };
		expect(() => indexScenarios({ "./bad.json": both })).toThrow(/bad\.json/);
	});
	it("throws when a reply file has no request", () => {
		expect(() => indexScenarios({ "./bad.json": { scenario: "x", data: {} } })).toThrow(
			/bad\.json/
		);
	});
	it("throws when a reply file has no data", () => {
		expect(() => indexScenarios({ "./bad.json": { scenario: "x", request: {} } })).toThrow(
			/bad\.json/
		);
	});
	it("accepts a scenario named like an object property", () => {
		const { statuses, replies } = indexScenarios({
			"./a.json": golden("toString"),
			"./b.json": replyFile("valueOf"),
		});
		expect(statuses.toString).toEqual({ snapshot_id: "toString" });
		expect(replies.valueOf.data).toEqual({ total_remaining: "valueOf" });
	});
	it("throws when two status files share a scenario key", () => {
		expect(() =>
			indexScenarios({ "./a.json": golden("same"), "./b.json": golden("same") })
		).toThrow(/same/);
	});
	it("throws when two reply files share a scenario key", () => {
		expect(() =>
			indexScenarios({ "./a.json": replyFile("same"), "./b.json": replyFile("same") })
		).toThrow(/same/);
	});
	it("throws when a status file and a reply file share a scenario key", () => {
		expect(() =>
			indexScenarios({ "./a.json": golden("same"), "./b.json": replyFile("same") })
		).toThrow(/same/);
	});
});
