import { describe, it, expect } from "vitest";
import { parseListField } from "./parseListField";

describe("parseListField", () => {
	it("parses a JSON-array string", () => {
		expect(parseListField('["GL Entry","Account"]')).toEqual(["GL Entry", "Account"]);
	});

	it("passes an already-parsed array through, coercing entries to strings", () => {
		expect(parseListField(["GL Entry", 1])).toEqual(["GL Entry", "1"]);
	});

	it("returns [] for an empty string", () => {
		expect(parseListField("")).toEqual([]);
	});

	it("returns [] for null/undefined", () => {
		expect(parseListField(null)).toEqual([]);
		expect(parseListField(undefined)).toEqual([]);
	});

	it("returns [] for malformed JSON instead of throwing", () => {
		expect(parseListField("{not json")).toEqual([]);
	});

	it("returns [] for valid JSON that isn't an array (e.g. an object)", () => {
		expect(parseListField('{"a":1}')).toEqual([]);
	});

	it("returns [] for a whitespace-only string", () => {
		expect(parseListField("   ")).toEqual([]);
	});
});
