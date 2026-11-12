import {
	exactMoney,
	compactMoney,
	netLabel,
	shortMoney,
	sign,
	signedMoney,
	snapshotMoney,
} from "./format";

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

describe("Payment Calendar net label", () => {
	it.each([
		["44200", "+₹44.2k"],
		["-100000", "\u2212₹1.0L"],
		["-100000.00", "\u2212₹1.0L"],
		["0", "₹0"],
		["0.00", "₹0"],
		["-0.00", "₹0"],
		["999", "+₹999"],
		["-4500.00", "\u2212₹4.5k"],
		["10000000", "+₹1.0Cr"],
		["123456789.00", "+₹12.3Cr"],
		["250000", "+₹2.5L"],
	])("shows %s as %s", (value, label) => {
		expect(netLabel(value, "INR")).toBe(label);
	});
	it("picks the unit from the unrounded value, like the template", () => {
		expect(netLabel("99950")).toBe("+₹100.0k");
		expect(netLabel("99999")).toBe("+₹100.0k");
		expect(netLabel("999950")).toBe("+₹10.0L");
		expect(netLabel("9999999")).toBe("+₹100.0L");
		expect(netLabel("999.50")).toBe("+₹1000");
		expect(netLabel("999.49")).toBe("+₹999");
	});
	it("rounds half away from zero on the decimal string, not on a float", () => {
		// A float division gives 1.1 for 1150 / 1000; the exact decimal is a tie.
		expect(netLabel("1150")).toBe("+₹1.2k");
		expect(netLabel("1050")).toBe("+₹1.1k");
		expect(netLabel("-1150")).toBe("\u2212₹1.2k");
		expect(netLabel("1049.99")).toBe("+₹1.0k");
	});
	it("stays exact beyond the float integer range", () => {
		expect(netLabel("9007199254740993.00")).toBe("+₹900719925.5Cr");
	});
	it("keeps the sign of a small negative that rounds to zero, like the template", () => {
		expect(netLabel("-0.40")).toBe("\u2212₹0");
	});
	it.each([null, undefined, "NaN", "1e5", " 1", 123.45, {}])(
		"returns a dash for %j",
		(value) => {
			expect(netLabel(value)).toBe("—");
		}
	);
});

describe("Payment Calendar short money", () => {
	it("groups en-IN with no decimals", () => {
		expect(shortMoney("42048.00", "INR")).toBe("₹42,048");
		expect(shortMoney("10195527.00", "INR")).toBe("₹1,01,95,527");
		expect(shortMoney("0", "INR")).toBe("₹0");
	});
	it("rounds half away from zero on the decimal string", () => {
		expect(shortMoney("42048.50", "INR")).toBe("₹42,049");
		expect(shortMoney("42048.49", "INR")).toBe("₹42,048");
		expect(shortMoney("-42048.50", "INR")).toBe("-₹42,049");
		expect(shortMoney("9007199254740992.50", "INR")).toBe("₹9,00,71,99,25,47,40,993");
	});
	it.each([null, undefined, "NaN", "1e5", 42048])("returns a dash for %j", (value) => {
		expect(shortMoney(value, "INR")).toBe("—");
	});
});

describe("Payment Calendar signed headline money", () => {
	it("uses U+2212 for negatives and no sign for positives by default", () => {
		expect(signedMoney("-4000.00", "INR", 2)).toBe("\u2212₹4,000.00");
		expect(signedMoney("4000.00", "INR", 2)).toBe("₹4,000.00");
		expect(signedMoney("0.00", "INR", 2)).toBe("₹0.00");
	});
	it("adds a plus for a positive net when asked", () => {
		expect(signedMoney("246850.00", "INR", 2, { plus: true })).toBe("+₹2,46,850.00");
		expect(signedMoney("-246850.00", "INR", 2, { plus: true })).toBe("\u2212₹2,46,850.00");
		expect(signedMoney("0.00", "INR", 2, { plus: true })).toBe("₹0.00");
	});
	it.each([null, undefined, "NaN", "1.234", 12])("says Unavailable for %j", (value) => {
		expect(signedMoney(value, "INR", 2)).toBe("Unavailable");
	});
});

describe("sign", () => {
	it.each([
		["12.50", 1],
		["-12.50", -1],
		["0", 0],
		["0.00", 0],
		["-0.00", 0],
		["1.1234567890", 1],
	])("reads %s as %i", (value, expected) => {
		expect(sign(value)).toBe(expected);
	});
	it.each([null, undefined, "", "abc1", "1e5", " 1", "+1", "1,000", {}, 5])(
		"is null for malformed input %j",
		(value) => {
			expect(sign(value)).toBeNull();
		}
	);
});

describe("snapshotMoney", () => {
	const snapshot = { currency: "INR", projection: { precision: 2 } };
	it("formats exact and signed amounts with the snapshot's currency and precision", () => {
		const money = snapshotMoney(snapshot);
		expect(money.exact("1234.50")).toBe("₹1,234.50");
		expect(money.signed("-1234.50")).toBe("−₹1,234.50");
		expect(money.signed("5.00", { plus: true })).toBe("+₹5.00");
	});
	it("shows the placeholders for malformed values and a snapshot without a projection", () => {
		expect(snapshotMoney(snapshot).exact("abc")).toBe("—");
		expect(snapshotMoney(snapshot).signed(null)).toBe("Unavailable");
		expect(snapshotMoney({ currency: "INR" }).exact("1.00")).toBe("₹1.00");
	});
});
