import { describe, it, expect, vi } from "vitest";

// frappe-ui's real entry doesn't resolve under vitest, so mock the two datetime
// primitives datetime.js uses. dayjsLocal is made to THROW on "abc" — reproducing
// the real dayjs.tz() RangeError on an unparseable string, which is the bug
// toLocalMs's try/catch guards.
vi.mock("frappe-ui", () => ({
	dayjsLocal: (s) => {
		if (s === "abc") throw new RangeError("Invalid time value");
		if (s === "2020-01-01 10:00:00")
			return { isValid: () => true, valueOf: () => 1_577_872_800_000 };
		return { isValid: () => false, valueOf: () => NaN };
	},
	dayjs: () => ({ format: () => "" }),
	getConfig: () => null,
}));

import { toLocalMs } from "@/utils/datetime";

describe("toLocalMs", () => {
	it("returns null for empty / nullish input", () => {
		expect(toLocalMs(null)).toBeNull();
		expect(toLocalMs("")).toBeNull();
		expect(toLocalMs(undefined)).toBeNull();
	});

	it("returns epoch ms for a valid site-tz string", () => {
		expect(toLocalMs("2020-01-01 10:00:00")).toBe(1_577_872_800_000);
	});

	it("returns null (does NOT throw) when dayjsLocal throws on unparseable input", () => {
		// Without the try/catch this RangeError would crash the SLA panel's render.
		expect(() => toLocalMs("abc")).not.toThrow();
		expect(toLocalMs("abc")).toBeNull();
	});

	it("returns null for an invalid (non-throwing) date", () => {
		expect(toLocalMs("not-a-date")).toBeNull();
	});
});
