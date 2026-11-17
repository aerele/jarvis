import { afterEach, describe, expect, it, vi } from "vitest";
import { nextPollDelay, safeError } from "./polling";

describe("safeError", () => {
	it("keeps only the code, state and retryable flag", () => {
		expect(
			safeError({ code: "worker_busy", state: "busy", retryable: false, trace: "x" })
		).toEqual({
			code: "worker_busy",
			state: "busy",
			retryable: false,
		});
	});
	it("falls back to a retryable internal error", () => {
		expect(safeError(undefined)).toEqual({
			code: "internal_error",
			state: "failed",
			retryable: true,
		});
	});
});

describe("nextPollDelay", () => {
	afterEach(() => vi.restoreAllMocks());
	it("backs off 1, 2, 3 then 5 seconds", () => {
		expect([0, 1, 2, 3, 4, 40].map(nextPollDelay)).toEqual([
			1000, 2000, 3000, 5000, 5000, 5000,
		]);
	});
	it("polls every 15 seconds while the tab is hidden", () => {
		vi.spyOn(document, "hidden", "get").mockReturnValue(true);
		expect(nextPollDelay(0)).toBe(15000);
	});
});
