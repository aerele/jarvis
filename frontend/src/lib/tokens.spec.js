import { describe, it, expect } from "vitest";
import { fmtTokens, contextReading } from "./tokens.js";

describe("fmtTokens", () => {
	it("renders an even thousand without a decimal", () => {
		expect(fmtTokens(42000)).toBe("42k");
	});
	it("renders an even hundred-thousand without a decimal", () => {
		expect(fmtTokens(200000)).toBe("200k");
	});
});

describe("contextReading", () => {
	it("reads the used/capacity pair when fresh", () => {
		const r = contextReading({ context: { used: 42000, capacity: 200000, fresh: true } });
		expect(r).toEqual({ fresh: true, text: "42k of 200k context in use" });
	});
	it("falls back to not measured yet when not fresh", () => {
		const r = contextReading({ context: { used: 0, capacity: 0, fresh: false } });
		expect(r).toEqual({ fresh: false, text: "Not measured yet" });
	});
});
