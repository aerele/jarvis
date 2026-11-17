import { hasActivity, hasAmount, netSign } from "./calendarDay";

describe("calendar day amount signs", () => {
	it.each([
		["5.00", true],
		["-5.00", true],
		["0.00", false],
		["abc1", false],
		[null, false],
		[5, false],
	])("hasAmount(%j) is %s", (value, expected) => {
		expect(hasAmount(value)).toBe(expected);
	});

	it.each([
		["5.00", "positive"],
		["-5.00", "negative"],
		["0.00", "zero"],
		["-0.00", "zero"],
		["x-1", "zero"],
		[null, "zero"],
	])("netSign(%j) is %s", (value, expected) => {
		expect(netSign(value)).toBe(expected);
	});

	it("treats a malformed remaining as no activity", () => {
		expect(hasActivity({ payable_remaining: "x1", receivable_remaining: "0.00" })).toBe(false);
		expect(hasActivity({ payable_remaining: "1.00", receivable_remaining: "0.00" })).toBe(
			true
		);
	});
});
