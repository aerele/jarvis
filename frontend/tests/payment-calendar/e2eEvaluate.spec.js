import { describe, expect, it } from "vitest";
import { createRequire } from "node:module";

const { evaluate } = createRequire(import.meta.url)("../e2e/payment-calendar/evaluate.cjs");

describe("e2e check evaluate", () => {
	it("turns a plain value into pass or fail", async () => {
		expect(await evaluate(() => true)).toEqual({ pass: true, detail: "" });
		expect(await evaluate(() => 0)).toEqual({ pass: false, detail: "" });
		expect(await evaluate(async () => "text")).toEqual({ pass: true, detail: "" });
	});

	it("passes a { pass, detail } result through", async () => {
		expect(await evaluate(() => ({ pass: false, detail: "(x)" }))).toEqual({
			pass: false,
			detail: "(x)",
		});
	});

	it("rejects a truthy object that has no boolean pass, with a clear message", async () => {
		const result = await evaluate(() => ({ count: 3, text: "a" }));
		expect(result.pass).toBe(false);
		expect(result.detail).toMatch(/returned an object without a boolean "pass"/);
		expect(result.detail).toContain("count, text");
	});

	it("rejects a non-boolean pass, so a truthy string cannot pass by accident", async () => {
		const result = await evaluate(() => ({ pass: "yes" }));
		expect(result.pass).toBe(false);
		expect(result.detail).toMatch(/boolean "pass"/);
	});

	it("reports a thrown error as a failure with its first line", async () => {
		const result = await evaluate(() => {
			throw new Error("boom\nsecond line");
		});
		expect(result).toEqual({ pass: false, detail: "boom" });
	});
});
