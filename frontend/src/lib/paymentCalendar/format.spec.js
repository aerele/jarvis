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
});
