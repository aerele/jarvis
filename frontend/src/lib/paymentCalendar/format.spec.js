import { exactMoney, compactMoney } from "./format";

describe("Payment Calendar exact decimal display", () => {
	it("uses Indian grouping without floating point conversion", () => {
		expect(exactMoney("101955274.00", "INR", 2)).toBe("₹10,19,55,274.00");
		expect(exactMoney("9007199254740993.25", "INR", 2)).toBe("₹9,00,71,99,25,47,40,993.25");
	});
	it("keeps signs, zero and the source precision", () => {
		expect(exactMoney("-4000.00", "INR", 2)).toBe("-₹4,000.00");
		expect(exactMoney("0.00", "INR", 2)).toBe("₹0.00");
		expect(exactMoney("1234.567", "USD", 3)).toBe("$1,234.567");
		expect(exactMoney("1234", "JPY", 0)).toBe("¥1,234");
	});
	it.each([null, undefined, "NaN", "Infinity", "1e5", " 1", {}, 123.45])(
		"never turns invalid input %j into zero",
		(value) => {
			expect(exactMoney(value, "INR", 2)).toBe("—");
		}
	);
	it("does not round away unexpected additional source precision", () => {
		expect(exactMoney("1.234", "INR", 2)).toBe("—");
	});
	it("keeps full values available alongside compact calendar amounts", () => {
		expect(compactMoney("101955274.00", "INR", 2)).toBe("₹10.19Cr");
		expect(exactMoney("101955274.00", "INR", 2)).toBe("₹10,19,55,274.00");
	});
	it.each([
		["0.00", "₹0"],
		["4732.00", "₹4,732"],
		["75400.00", "₹75.4K"],
		["103373.00", "₹1.03L"],
		["10000000.00", "₹1Cr"],
		["-103373.00", "-₹1L"],
		["99999.99", "₹99.9K"],
	])("shortens %s for narrow cells without rounding up", (amount, expected) => {
		expect(compactMoney(amount, "INR", 2, 6)).toBe(expected);
		expect(exactMoney(amount, "INR", 2)).not.toBe("—");
	});
	it.each([
		["999.99", "₹999"],
		["1000.00", "₹1,000"],
		["100000.00", "₹1L"],
		["9999999.99", "₹99.9L"],
		["9990000000.00", "₹999Cr"],
		["10000000000.00", "₹1000Cr"],
		["-9999999.99", "-₹99L"],
		["-0.00", "₹0"],
	])("preserves signs and INR boundaries for %s", (amount, expected) => {
		expect(compactMoney(amount, "INR", 2, 6)).toBe(expected);
	});
	it("supports other currency scales and precisions without changing default formatting", () => {
		expect(compactMoney("1250000.123", "USD", 3, 6)).toBe("$1.25M");
		expect(compactMoney("1250000000", "JPY", 0, 6)).toBe("¥1.25B");
		expect(compactMoney("75400.00", "INR", 2)).toBe("₹75,400.00");
		expect(compactMoney("101955274.00", "INR", 2, "bad")).toBe("₹10.19Cr");
		expect(compactMoney("1.234", "INR", 2, 6)).toBe("—");
		expect(compactMoney(null, "INR", 2, 6)).toBe("—");
		expect(compactMoney("9000000.00", "UNKNOWN", 2, 6)).toContain("UNKNOWN");
	});
});
