import { describe, expect, it } from "vitest";
import { FLOOR_ERROR, FLOOR_LIMIT_ERROR, formatFloor, parseFloor, sameFloor } from "./floorInput";

describe("parseFloor", () => {
	it.each([
		["200000", "200000"],
		["2,00,000", "200000"],
		[" 1000.5 ", "1000.5"],
		["1000.50", "1000.50"],
		["0", "0"],
		["1000000000", "1000000000"],
		["1,000,000,000.00", "1000000000.00"],
	])("accepts %j", (text, value) => {
		expect(parseFloor(text)).toEqual({ value });
	});

	it.each(["", "  ", "-5", "1e5", "10.123", "abc", "5.", ".5"])(
		"rejects %j with the template message",
		(text) => {
			expect(parseFloor(text)).toEqual({ error: FLOOR_ERROR });
		}
	);

	it.each([
		["₹50,000", "50000"],
		["₹ 1,000.50", "1000.50"],
		["$5", "5"],
	])("strips a leading currency symbol and commas from %j", (text, value) => {
		expect(parseFloor(text)).toEqual({ value });
	});

	it.each(["1000000000.01", "1,000,000,001", "99999999999999999999"])(
		"rejects %j as too large, with its own message",
		(text) => {
			expect(parseFloor(text)).toEqual({ error: FLOOR_LIMIT_ERROR });
		}
	);

	it("keeps the decimal-places message for a malformed amount, even a large one", () => {
		expect(parseFloor("1000000000.123")).toEqual({ error: FLOOR_ERROR });
		expect(parseFloor("abc5")).toEqual({ error: FLOOR_ERROR });
	});

	it("uses the template's wording", () => {
		expect(FLOOR_ERROR).toBe("Enter a non-negative amount with up to two decimal places.");
	});
});

describe("formatFloor", () => {
	it.each([
		["50000.00", "50,000"],
		["200000", "2,00,000"],
		["1234.50", "1,234.5"],
		["1234.05", "1,234.05"],
		["0.00", "0"],
		["1000000000.00", "1,00,00,00,000"],
	])("formats %j as %j", (value, shown) => {
		expect(formatFloor(value)).toBe(shown);
	});
	it("returns an empty string for a missing or odd value", () => {
		expect(formatFloor(null)).toBe("");
		expect(formatFloor(undefined)).toBe("");
		expect(formatFloor("abc")).toBe("");
	});
});

describe("sameFloor", () => {
	it("compares the amount, not the spelling", () => {
		expect(sameFloor("50,000", "50000.00")).toBe(true);
		expect(sameFloor("50000.0", "50000")).toBe(true);
		expect(sameFloor("050000", "50000.00")).toBe(true);
		expect(sameFloor("50000.01", "50000.00")).toBe(false);
		expect(sameFloor("abc", "50000.00")).toBe(false);
	});
	it("treats a missing saved floor as an empty field", () => {
		expect(sameFloor("", null)).toBe(true);
		expect(sameFloor("5", null)).toBe(false);
	});
});
