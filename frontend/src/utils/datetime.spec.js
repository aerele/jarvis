import { describe, it, expect, vi } from "vitest";

// frappe-ui's ESM entry does not resolve under vitest (see LlmPoolEditor.spec.js) -
// fmtElapsed itself is pure, but datetime.js imports frappe-ui at module scope, so
// any spec importing this module needs the mock even though fmtElapsed never calls it.
vi.mock("frappe-ui", async () => ({
	call: vi.fn(),
	// frappe-ui re-exports dayjs; the real one, so formatLocalMs is checked for real.
	dayjs: (await vi.importActual("dayjs")).default,
	dayjsLocal: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	getConfig: () => null,
}));

import { fmtElapsed, formatLocalMs } from "./datetime";

// A reply stamped in the browser at run:end shows its time in the same format
// the server copy gets after the reload ("10:42 PM", never "22:42" first).
describe("formatLocalMs", () => {
	it("formats a browser epoch in the local zone with the caller's pattern", () => {
		const ms = new Date(2026, 8, 29, 22, 42).getTime();
		expect(formatLocalMs(ms, "h:mm A")).toBe("10:42 PM");
		expect(formatLocalMs(ms, "ddd, MMM D, YYYY h:mm A")).toBe("Tue, Sep 29, 2026 10:42 PM");
	});

	it("is empty without a stamp", () => {
		expect(formatLocalMs(0, "h:mm A")).toBe("");
		expect(formatLocalMs(undefined, "h:mm A")).toBe("");
	});
});

// jarvis#1062 C3: the running-run progress display's ticking elapsed-time
// label - mm:ss under an hour, h:mm at/above it.
describe("fmtElapsed", () => {
	it("renders mm:ss under an hour", () => {
		expect(fmtElapsed(0)).toBe("00:00");
		expect(fmtElapsed(5)).toBe("00:05");
		expect(fmtElapsed(65)).toBe("01:05");
		expect(fmtElapsed(3599)).toBe("59:59");
	});

	it("renders h:mm at and above an hour", () => {
		expect(fmtElapsed(3600)).toBe("1:00");
		expect(fmtElapsed(3661)).toBe("1:01");
		expect(fmtElapsed(7325)).toBe("2:02");
	});

	it("clamps negative/NaN/missing input to zero instead of printing garbage", () => {
		expect(fmtElapsed(-5)).toBe("00:00");
		expect(fmtElapsed(NaN)).toBe("00:00");
		expect(fmtElapsed(undefined)).toBe("00:00");
		expect(fmtElapsed(null)).toBe("00:00");
	});

	it("floors fractional seconds", () => {
		expect(fmtElapsed(65.9)).toBe("01:05");
	});
});
